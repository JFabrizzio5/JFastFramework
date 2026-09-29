"""Retry rules, the retry budget, the circuit breaker and the bulkhead.

Transport-free: nothing here imports httpx or sends anything, and every piece
that reads time takes the clock as an argument, so its state machine can be
tested by moving a number instead of waiting.

All of it is **per process**. A breaker in one worker does not know what the
worker beside it saw; each opens on its own evidence. That is the usual
trade-off -- a shared breaker needs a shared store on the hot path of every
call -- and it means an upstream sees up to one probe per process while it
recovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jfastframework.http.errors import BulkheadFullError, CircuitOpenError

__all__ = [
    "IDEMPOTENT_METHODS",
    "BreakerPolicy",
    "Bulkhead",
    "CircuitBreaker",
    "CircuitState",
    "RetryBudget",
    "RetryPolicy",
    "may_retry",
]

Clock = Callable[[], float]

#: Methods whose repetition leaves the server as one call would (RFC 9110
#: 9.2.2). POST and PATCH are not: retrying one after a timeout can charge a
#: card twice, because a timeout says nothing about whether the server acted.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


def may_retry(method: str, headers: Mapping[str, str]) -> bool:
    """Whether a request may be sent again. An ``Idempotency-Key`` makes any method safe."""
    if method.upper() in IDEMPOTENT_METHODS:
        return True
    return any(name.lower() == "idempotency-key" for name in headers)


@dataclass(frozen=True)
class RetryPolicy:
    # Tries in total, the first included.
    attempts: int = 3
    backoff_base: float = 0.1
    backoff_max: float = 2.0
    # 502/503/504 say the upstream (or its proxy) is not there right now; 429
    # says it is there and asked us to slow down. A 500 is the upstream
    # answering with a bug, and sending the same request again gets the same bug.
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    # A Retry-After longer than this is not waited for: the response goes back
    # to the caller, which has a deadline of its own.
    max_retry_after: float = 30.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts counts the first try and must be at least 1")
        if self.backoff_base < 0 or self.backoff_max < 0:
            raise ValueError("backoff times cannot be negative")

    def backoff(self, retry: int, rng: random.Random) -> float:
        """Full jitter: uniform between zero and the capped exponential.

        Every client that failed at the same moment otherwise retries at the
        same moment, and the upstream meets the whole spike again on each
        round. Spreading over the full range is what breaks the rhythm.
        """
        ceiling = min(self.backoff_max, self.backoff_base * 2 ** max(retry - 1, 0))
        return rng.uniform(0, ceiling)  # nosec B311 - jitter, not a secret


class RetryBudget:
    """Caps retries at a fraction of recent traffic, so an outage cannot multiply load.

    Three attempts per call means that when an upstream goes down, every
    caller sends it three times the traffic -- at the moment it can least take
    it. The budget allows ``ratio`` retries per request seen in the last
    ``window`` seconds, plus ``min_per_second`` so a quiet client can still
    retry at all. Past that, a failure is returned instead of retried.
    """

    def __init__(
        self,
        *,
        ratio: float = 0.2,
        min_per_second: float = 1.0,
        window: float = 10.0,
        clock: Clock = time.monotonic,
    ) -> None:
        if ratio < 0 or min_per_second < 0 or window <= 0:
            raise ValueError("retry budget values must be positive")
        self.ratio = ratio
        self.min_per_second = min_per_second
        self.window = window
        self._clock = clock
        # One-second buckets: [second, requests, retries]. Bounded by the
        # window, however much traffic goes through.
        self._buckets: deque[list[int]] = deque()

    def _bucket(self) -> list[int]:
        second = int(self._clock())
        horizon = second - int(self.window)
        while self._buckets and self._buckets[0][0] <= horizon:
            self._buckets.popleft()
        if not self._buckets or self._buckets[-1][0] != second:
            self._buckets.append([second, 0, 0])
        return self._buckets[-1]

    def record_request(self) -> None:
        self._bucket()[1] += 1

    def try_spend(self) -> bool:
        """Take one retry from the budget. False when there is none left."""
        current = self._bucket()
        requests = sum(bucket[1] for bucket in self._buckets)
        retries = sum(bucket[2] for bucket in self._buckets)
        if retries >= self.min_per_second * self.window + self.ratio * requests:
            return False
        current[2] += 1
        return True


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerPolicy:
    # Opens after this many failures in a row...
    failure_threshold: int = 5
    # ...or when this share of the calls in the window failed, once there
    # were enough calls for a share to mean anything.
    failure_rate: float = 0.5
    minimum_calls: int = 20
    window: float = 30.0
    # How long it stays open before letting a probe through.
    cool_down: float = 15.0
    # Probes allowed at once while half-open. One is the point: a recovering
    # upstream is shown one request, not the queue that built up.
    half_open_max_calls: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.minimum_calls < 1 or self.half_open_max_calls < 1:
            raise ValueError("breaker thresholds must be at least 1")
        if not 0 < self.failure_rate <= 1:
            raise ValueError("failure_rate is a share of calls, in (0, 1]")
        if self.window <= 0 or self.cool_down <= 0:
            raise ValueError("breaker window and cool_down must be positive")


@dataclass
class Permit:
    """One call's admission through the breaker; settled exactly once."""

    probe: bool
    generation: int
    settled: bool = field(default=False)


class CircuitBreaker:
    """Closed, then open after failures, then half-open to probe, then closed again.

    A call asks :meth:`acquire` for a permit before it sends anything and
    settles it with :meth:`record` or :meth:`release`. While open, ``acquire``
    raises :class:`CircuitOpenError` and nothing reaches the network -- the
    upstream gets time to recover instead of a queue of retries.
    """

    def __init__(
        self, name: str, policy: BreakerPolicy | None = None, *, clock: Clock = time.monotonic
    ) -> None:
        self.name = name
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._consecutive = 0
        self._probes = 0
        # Bumped on every transition, so a permit taken in one state cannot
        # settle the accounting of the next.
        self._generation = 0
        self._window: deque[list[int]] = deque()

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._clock() - self._opened_at >= self.policy.cool_down
        ):
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    def _transition(self, state: CircuitState) -> None:
        self._state = state
        self._generation += 1
        self._consecutive = 0
        self._probes = 0
        self._window.clear()
        if state is CircuitState.OPEN:
            self._opened_at = self._clock()

    def acquire(self) -> Permit:
        state = self.state
        if state is CircuitState.OPEN:
            remaining = self.policy.cool_down - (self._clock() - self._opened_at)
            raise CircuitOpenError(upstream=self.name, retry_after=max(remaining, 0.0))
        if state is CircuitState.HALF_OPEN:
            if self._probes >= self.policy.half_open_max_calls:
                # The probe is in flight; everyone else fails fast until it
                # says whether the upstream is back.
                raise CircuitOpenError(upstream=self.name, retry_after=0.0)
            self._probes += 1
            return Permit(probe=True, generation=self._generation)
        return Permit(probe=False, generation=self._generation)

    def record(self, permit: Permit, *, failed: bool) -> None:
        if permit.settled:
            return
        permit.settled = True
        if permit.generation != self._generation:
            # Taken before a transition; its verdict belongs to a state that
            # is gone.
            return
        if permit.probe:
            self._transition(CircuitState.OPEN if failed else CircuitState.CLOSED)
            return
        self._count(failed)
        # Checked after a success too: the call that fills the window up to
        # `minimum_calls` can be either.
        if self._should_open():
            self._transition(CircuitState.OPEN)

    def release(self, permit: Permit) -> None:
        """Settle a permit with no verdict: the call was cancelled, not answered."""
        if permit.settled:
            return
        permit.settled = True
        if permit.probe and permit.generation == self._generation:
            self._probes -= 1

    def _count(self, failed: bool) -> None:
        self._consecutive = self._consecutive + 1 if failed else 0
        second = int(self._clock())
        horizon = second - int(self.policy.window)
        while self._window and self._window[0][0] <= horizon:
            self._window.popleft()
        if not self._window or self._window[-1][0] != second:
            self._window.append([second, 0, 0])
        self._window[-1][1] += 1
        self._window[-1][2] += int(failed)

    def _should_open(self) -> bool:
        if self._consecutive >= self.policy.failure_threshold:
            return True
        calls = sum(bucket[1] for bucket in self._window)
        failures = sum(bucket[2] for bucket in self._window)
        return calls >= self.policy.minimum_calls and failures / calls >= self.policy.failure_rate

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        entry: dict[str, Any] = {"state": state.value}
        if state is CircuitState.OPEN:
            entry["retry_after"] = round(
                max(self.policy.cool_down - (self._clock() - self._opened_at), 0.0), 3
            )
        return entry


class Bulkhead:
    """At most ``max_concurrent`` calls to one upstream in flight from this process.

    One slow upstream otherwise takes every connection, every worker and every
    request of the caller down with it: requests that never touch that
    upstream queue behind the ones waiting on it. A call that cannot get a
    slot within ``max_wait`` seconds fails with a 503 instead of joining the
    queue.
    """

    def __init__(self, name: str, *, max_concurrent: int = 50, max_wait: float = 0.5) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.name = name
        self.max_concurrent = max_concurrent
        self.max_wait = max_wait
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self.max_wait <= 0:
            if self._semaphore.locked():
                raise self._full()
            # Unlocked, so this returns without yielding: nothing can take
            # the slot between the check and the acquire.
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self.max_wait)
            except TimeoutError:
                raise self._full() from None
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._semaphore.release()

    def _full(self) -> BulkheadFullError:
        return BulkheadFullError(
            f"{self.max_concurrent} calls to {self.name!r} are already in flight",
            upstream=self.name,
        )
