"""The client for sibling services.

Each call goes through, in order: a total deadline around everything, the
circuit breaker (no network call while it is open), the bulkhead (a bounded
number in flight), then the attempt itself under httpx's per-phase timeouts.
A failed attempt is retried only when retrying is safe and useful:

- the request is idempotent -- GET, HEAD, PUT, DELETE, OPTIONS -- or carries
  an ``Idempotency-Key``;
- the failure is a transport error or timeout, or a 429, 502, 503 or 504;
- the retry budget has room, and the deadline has time for the wait.

The wait is exponential backoff with full jitter, or exactly the upstream's
``Retry-After`` when it sends one. A response the upstream sent is returned
as it is once retrying stops -- a 404, a 500, the last 503 -- and only a call
that got no response raises, with a 503 :class:`~jfastframework.http.errors.UpstreamError`.

Requires: ``pip install jfastframework[http]``
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from jfastframework import tracing
from jfastframework.errors import PluginError
from jfastframework.http.context import inbound_authorization
from jfastframework.http.errors import UpstreamTimeoutError, UpstreamUnreachableError
from jfastframework.http.resilience import (
    BreakerPolicy,
    Bulkhead,
    CircuitBreaker,
    CircuitState,
    RetryBudget,
    RetryPolicy,
    may_retry,
)

__all__ = ["HttpClients", "ServiceClient", "Timeouts", "Upstream", "retry_after_seconds"]

logger = logging.getLogger("jfast.http")

REQUEST_ID_HEADER = "X-Request-ID"

#: Transport failures worth another attempt. Not ``UnsupportedProtocol`` or a
#: proxy misconfiguration: those fail the same way every time.
RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

#: Answers that count against the breaker. The ones that mean the upstream is
#: not there; a 500 is the upstream answering, which proves it is up, and a
#: 429 is it pacing us, which an open breaker would turn into an outage.
BREAKER_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class Timeouts:
    """Every one of them is required to be finite. A call with no deadline is a
    worker that can be held forever by an upstream that accepted the
    connection and never answered."""

    connect: float = 2.0
    read: float = 10.0
    write: float = 10.0
    pool: float = 2.0
    # Around the whole call: every attempt and every wait between them.
    total: float = 30.0

    def __post_init__(self) -> None:
        for name in ("connect", "read", "write", "pool", "total"):
            value = getattr(self, name)
            if value is None or value <= 0:
                raise ValueError(f"timeout {name!r} must be a positive number of seconds")

    def for_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.write, pool=self.pool)


@dataclass(frozen=True)
class Upstream:
    """One sibling service and how to call it."""

    name: str
    base_url: str
    timeouts: Timeouts = field(default_factory=Timeouts)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: BreakerPolicy = field(default_factory=BreakerPolicy)
    retry_budget_ratio: float = 0.2
    retry_budget_min_per_second: float = 1.0
    max_concurrent: int = 50
    bulkhead_wait: float = 0.5
    # Send the caller's bearer token on. Off unless asked for: a token is
    # issued for an audience, and forwarding it to a service outside that
    # audience hands the caller's identity to it.
    forward_authorization: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds from now: either form RFC 9110 allows."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def _current_request_id() -> str | None:
    # Imported late: the observability plugin owns the variable, and this
    # package must not import a plugin at module load.
    from jfastframework.plugins.builtin.observability import current_request_id

    return current_request_id()


class ServiceClient:
    """Calls one upstream. Share one per upstream per process: the breaker,
    the bulkhead and the budget only work if every call sees them."""

    def __init__(
        self,
        upstream: Upstream,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.upstream = upstream
        self.breaker = CircuitBreaker(upstream.name, upstream.breaker, clock=clock)
        self.budget = RetryBudget(
            ratio=upstream.retry_budget_ratio,
            min_per_second=upstream.retry_budget_min_per_second,
            clock=clock,
        )
        self.bulkhead = Bulkhead(
            upstream.name, max_concurrent=upstream.max_concurrent, max_wait=upstream.bulkhead_wait
        )
        self._transport = transport
        self._host = httpx.URL(upstream.base_url).host
        self._sleep = sleep
        self._rng = rng or random.Random()  # nosec B311 - jitter, not a secret
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return self.upstream.name

    def _http(self) -> httpx.AsyncClient:
        # Built on first use rather than in __init__, so a configured upstream
        # that a process never calls holds no connection pool.
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.upstream.base_url,
                timeout=self.upstream.timeouts.for_httpx(),
                limits=httpx.Limits(
                    max_connections=self.upstream.max_concurrent,
                    max_keepalive_connections=self.upstream.max_concurrent,
                ),
                transport=self._transport,
                # A redirect to another host would carry the forwarded token
                # with it; a sibling service has no business redirecting.
                follow_redirects=False,
            )
        return self._client

    def _headers(
        self, headers: Mapping[str, str] | None, idempotency_key: str | None
    ) -> httpx.Headers:
        outgoing = httpx.Headers(dict(self.upstream.headers))
        request_id = _current_request_id()
        if request_id:
            outgoing[REQUEST_ID_HEADER] = request_id
        if self.upstream.forward_authorization:
            token = inbound_authorization.get()
            if token:
                outgoing["Authorization"] = token
        if idempotency_key is not None:
            outgoing["Idempotency-Key"] = idempotency_key
        # W3C traceparent/tracestate of the current span -- the client span
        # this call runs in -- so the upstream's server span is its child and
        # one trace spans both services. {} when telemetry is off.
        outgoing.update(tracing.inject())
        # The caller's own headers win over anything propagated.
        outgoing.update(headers or {})
        return outgoing

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        retry: bool | None = None,
    ) -> httpx.Response:
        """Send one call, with the retries the rules allow.

        ``retry=False`` forbids retrying a call the rules would retry;
        ``retry=True`` asserts that a non-idempotent one is safe to repeat.
        Prefer ``idempotency_key``, which also tells the upstream.
        """
        verb = method.upper()
        # One client span around the whole call, retries included: the span's
        # context is what `_headers` injects, and every attempt carries it.
        # The path is not an attribute -- it can hold an id per call; the
        # upstream's own server span names the route template.
        with tracing.span(
            f"{verb} {self.name}",
            **{
                "span.kind": "client",
                "http.request.method": verb,
                "jfast.upstream": self.name,
                "server.address": self._host,
            },
        ):
            client = self._http()
            request = client.build_request(
                verb,
                url,
                params=params,
                headers=self._headers(headers, idempotency_key),
                json=json,
                content=content,
                data=data,
            )
            retryable = retry if retry is not None else may_retry(request.method, request.headers)
            self.budget.record_request()

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.upstream.timeouts.total
            try:
                async with asyncio.timeout_at(deadline):
                    response = await self._attempts(client, request, retryable, deadline)
            except TimeoutError:
                raise UpstreamTimeoutError(
                    f"{request.method} {self.name}{request.url.path} did not complete within "
                    f"{self.upstream.timeouts.total:g}s",
                    upstream=self.name,
                ) from None
            tracing.annotate(**{"http.response.status_code": response.status_code})
            return response

    async def _attempts(
        self, client: httpx.AsyncClient, request: httpx.Request, retryable: bool, deadline: float
    ) -> httpx.Response:
        policy = self.upstream.retry
        attempt = 0
        while True:
            attempt += 1
            if attempt > 1:
                tracing.annotate(**{"http.request.resend_count": attempt - 1})
            response: httpx.Response | None = None
            failure: Exception | None = None

            permit = self.breaker.acquire()
            try:
                async with self.bulkhead.slot():
                    response = await client.send(request)
            except RETRYABLE_ERRORS as exc:
                self.breaker.record(permit, failed=True)
                failure = exc
            except BaseException:
                # Cancelled, bulkhead full, or a request that could never be
                # sent: nothing the upstream said, so no verdict on it.
                self.breaker.release(permit)
                raise
            else:
                self.breaker.record(permit, failed=response.status_code in BREAKER_STATUSES)
                if response.status_code not in policy.retry_statuses:
                    return response

            reason = f"HTTP {response.status_code}" if response is not None else repr(failure)
            delay = self._next_delay(attempt, response, retryable)
            if delay is None or asyncio.get_running_loop().time() + delay >= deadline:
                return self._give_up(request, response, failure, attempt, reason)
            if not self.budget.try_spend():
                logger.warning(
                    "retry budget spent; not retrying",
                    extra={"upstream": self.name, "attempt": attempt, "reason": reason},
                )
                return self._give_up(request, response, failure, attempt, reason)

            logger.info(
                "retrying upstream call",
                extra={
                    "upstream": self.name,
                    "method": request.method,
                    "path": request.url.path,
                    "attempt": attempt,
                    "reason": reason,
                    "delay": round(delay, 3),
                },
            )
            await self._sleep(delay)

    def _next_delay(
        self, attempt: int, response: httpx.Response | None, retryable: bool
    ) -> float | None:
        """Seconds to wait before the next attempt, or None to stop."""
        policy = self.upstream.retry
        if not retryable or attempt >= policy.attempts:
            return None
        if response is not None:
            asked = retry_after_seconds(response.headers.get("Retry-After"))
            if asked is not None:
                # Waiting less than asked is the retry the upstream just
                # refused; waiting longer than we can afford is no retry.
                return asked if asked <= policy.max_retry_after else None
        return policy.backoff(attempt, self._rng)

    def _give_up(
        self,
        request: httpx.Request,
        response: httpx.Response | None,
        failure: Exception | None,
        attempts: int,
        reason: str,
    ) -> httpx.Response:
        if response is not None:
            return response
        raise UpstreamUnreachableError(
            f"{request.method} {self.name}{request.url.path} failed after {attempts} "
            f"attempt(s): {reason}",
            upstream=self.name,
            attempts=attempts,
        ) from failure

    async def get(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("GET", url, **options)

    async def head(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("HEAD", url, **options)

    async def options(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("OPTIONS", url, **options)

    async def post(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("POST", url, **options)

    async def put(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("PUT", url, **options)

    async def patch(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("PATCH", url, **options)

    async def delete(self, url: str, **options: Any) -> httpx.Response:
        return await self.request("DELETE", url, **options)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ServiceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class HttpClients:
    """One :class:`ServiceClient` per configured upstream, shared by the process."""

    def __init__(
        self,
        upstreams: Mapping[str, Upstream],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._clients = {
            name: ServiceClient(upstream, transport=transport, clock=clock, sleep=sleep, rng=rng)
            for name, upstream in upstreams.items()
        }

    def client(self, name: str) -> ServiceClient:
        try:
            return self._clients[name]
        except KeyError:
            configured = ", ".join(sorted(self._clients)) or "<none>"
            raise PluginError(
                f"no upstream named {name!r}; configured: {configured}. Add "
                f"[plugin.http.upstreams.{name}] with a base_url."
            ) from None

    __getitem__ = client

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._clients))

    def breakers(self) -> dict[str, dict[str, Any]]:
        return {name: client.breaker.snapshot() for name, client in sorted(self._clients.items())}

    def open_breakers(self) -> list[str]:
        return [
            name
            for name, client in sorted(self._clients.items())
            if client.breaker.state is not CircuitState.CLOSED
        ]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
