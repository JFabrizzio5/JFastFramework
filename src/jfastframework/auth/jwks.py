"""JWKS client: fetch and cache an issuer's public keys.

Asymmetric verification is what makes JWT workable across services. The
identity provider holds the private key; every service fetches the public keys
from its JWKS endpoint and verifies locally. No shared secret, no network hop
per request, and key rotation is a publish rather than a redeploy.

Two failure modes this guards against, both of which are easy to build in by
accident:

**Refresh amplification.** A token carrying an unknown ``kid`` should trigger a
refresh -- that is how rotation is picked up. Refreshing on *every* unknown
``kid`` turns a stream of forged tokens into a denial-of-service against your
identity provider. Refreshes are therefore rate-limited, and a token whose
``kid`` is still unknown afterwards is simply rejected.

**Serving stale keys forever.** If the endpoint is unreachable, the cached keys
keep working -- a JWKS outage must not take every service down with it -- but
the staleness is reported through the health check rather than hidden.

**Waiting on an outage.** Cached keys only help if reaching them does not
first cost a timeout. Every fetch has a deadline, one bounded retry for what
looks transient, and a circuit breaker: after ``breaker_failures`` failed
fetches in a row the issuer is not called again for ``breaker_cool_down``
seconds, so a request whose key is cached answers at once instead of waiting
out ``timeout`` on every expiry check. A request that needs a key nobody has
fetched still fails -- there is nothing to verify it with.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from jfastframework.http.resilience import BreakerPolicy, CircuitBreaker, RetryPolicy

#: Answers worth one more try: the issuer (or its proxy) is briefly not there.
#: A 404 or a 401 is configuration, and asking again gets the same answer.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


class JWKSError(RuntimeError):
    """The key set could not be fetched or does not contain the key."""


@dataclass
class JWKSClient:
    url: str
    # How long a fetched key set is considered fresh.
    cache_seconds: int = 3600
    # Floor between refreshes triggered by an unknown kid.
    min_refresh_seconds: int = 60
    # Seconds one fetch may take, connect included. An identity provider on
    # the same continent answers in tens of milliseconds; five seconds is a
    # slow one, not a normal one. Paid at most once per breaker window.
    timeout: float = 5.0
    # Tries per refresh, the first included. Two: one retry catches the
    # dropped connection and the 502 from a proxy mid-deploy, and a third
    # would only stretch the wait of the request that triggered the refresh.
    attempts: int = 2
    # Failed refreshes in a row that open the breaker, and how long it stays
    # open. Thirty seconds of serving cached keys is harmless -- they were
    # valid a moment ago -- and it is thirty seconds of requests that do not
    # each wait `timeout` on an issuer that is down.
    breaker_failures: int = 3
    breaker_cool_down: float = 30.0
    #: An ``httpx`` transport, for tests and for a proxy; None uses httpx's own.
    transport: Any = field(default=None, repr=False)

    _keys: dict[str, Any] = field(default_factory=dict, repr=False)
    _fetched_at: float = 0.0
    _last_attempt: float = 0.0
    _last_error: str | None = None
    #: Bumped by every successful fetch. What a waiter compares to decide
    #: whether somebody else already fetched -- not the clock: on Windows
    #: `time.monotonic()` moves in ~15.6 ms steps, so a fetch that finishes in
    #: the same tick it started in looked like no fetch at all, and the second
    #: waiter fetched again.
    _generation: int = field(default=0, repr=False)
    #: One refresh at a time. Without it, the cache expiring under load sends
    #: every in-flight request to the issuer at once -- a stampede aimed at
    #: the one dependency whose being down makes every token unverifiable.
    #: The second waiter re-checks freshness after the lock and finds the keys
    #: already there, so it costs one HTTP call rather than one per request.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Bumped by every refresh attempt, failed or not. A waiter that finds it
    #: moved knows somebody else just asked the issuer, and does not ask again
    #: -- during an outage as much as after a success.
    _attempts: int = field(default=0, repr=False)
    _breaker: CircuitBreaker | None = field(default=None, repr=False)
    _retry: RetryPolicy | None = field(default=None, repr=False)
    _rng: random.Random = field(default_factory=random.Random, repr=False)

    def __post_init__(self) -> None:
        if self.timeout <= 0 or self.attempts < 1:
            raise JWKSError("JWKS timeout must be positive and attempts at least 1")
        self._retry = RetryPolicy(attempts=self.attempts, backoff_base=0.2, backoff_max=1.0)
        if self.breaker_failures > 0:
            self._breaker = CircuitBreaker(
                f"jwks {self.url}",
                BreakerPolicy(
                    failure_threshold=self.breaker_failures, cool_down=self.breaker_cool_down
                ),
            )

    @property
    def breaker(self) -> CircuitBreaker | None:
        return self._breaker

    async def _fetch(self) -> None:
        """One request to the issuer. Raises on anything but a usable key set."""
        import httpx

        self._last_attempt = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            document = response.json()

        keys = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if kid:
                keys[kid] = entry

        if not keys:
            raise JWKSError(f"{self.url} returned no usable keys")

        self._keys = keys
        self._fetched_at = time.monotonic()
        self._last_error = None

    @staticmethod
    def _transient(exc: Exception) -> bool:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _TRANSIENT_STATUSES
        return isinstance(exc, httpx.TransportError | TimeoutError | OSError)

    async def _fetch_guarded(self) -> None:
        """``_fetch`` with the retry policy, behind the breaker."""
        retry = self._retry or RetryPolicy(attempts=1)
        permit = self._breaker.acquire() if self._breaker is not None else None
        try:
            for attempt in range(1, retry.attempts + 1):
                try:
                    await self._fetch()
                    break
                except Exception as exc:
                    if attempt >= retry.attempts or not self._transient(exc):
                        raise
                    await asyncio.sleep(retry.backoff(attempt, self._rng))
        except asyncio.CancelledError:
            if self._breaker is not None and permit is not None:
                self._breaker.release(permit)
            raise
        except Exception:
            if self._breaker is not None and permit is not None:
                self._breaker.record(permit, failed=True)
            raise
        if self._breaker is not None and permit is not None:
            self._breaker.record(permit, failed=False)

    async def _refresh(
        self, *, force: bool = False, seen: int | None = None, tried: int | None = None
    ) -> None:
        """Fetch the key set, one caller at a time.

        ``seen`` is the generation the caller read when it decided a refresh
        was needed. Whoever was waiting on the lock re-reads it after and, if
        somebody else has fetched since, returns without a second call --
        which is what turns a stampede into one request.

        ``tried`` is the same for attempts. When the fetch the waiter queued
        behind *failed*, asking again a millisecond later only doubles the
        load on an issuer that is already not answering, so the waiter takes
        that failure as its own.
        """
        async with self._lock:
            if seen is not None and self._generation != seen:
                return
            if tried is not None and self._attempts != tried:
                if self._keys and not force:
                    return
                raise JWKSError(f"cannot fetch JWKS from {self.url}: {self._last_error}")
            try:
                await self._fetch_guarded()
                self._generation += 1
            except Exception as exc:
                self._last_error = str(exc) or type(exc).__name__
                if force or not self._keys:
                    # Nothing cached to fall back on: this request cannot be
                    # verified, and saying so beats guessing.
                    raise JWKSError(f"cannot fetch JWKS from {self.url}: {exc}") from exc
            finally:
                # Counted when the attempt ends, not when it starts: a caller
                # that read the counter while this fetch was in flight must
                # see it move, and take this outcome instead of asking again.
                self._attempts += 1

    async def key_for(self, kid: str | None) -> Any:
        """The signing key for this ``kid``, fetching the set if needed."""
        now = time.monotonic()
        seen = self._generation
        tried = self._attempts
        expired = now - self._fetched_at > self.cache_seconds

        if not self._keys or expired:
            await self._refresh(force=not self._keys, seen=seen, tried=tried)
            seen = self._generation
            tried = self._attempts

        if kid is None:
            if len(self._keys) == 1:
                return next(iter(self._keys.values()))
            raise JWKSError(
                "the token has no 'kid' and the key set has several keys; "
                "which one signed it is unknowable"
            )

        if kid not in self._keys and (now - self._last_attempt) > self.min_refresh_seconds:
            # Unknown kid: the issuer may have rotated. Refresh at most once
            # per window, so forged kids cannot be used to hammer the issuer.
            # `seen` again, so a hundred requests carrying the same unknown
            # kid produce one fetch rather than a hundred inside the window.
            await self._refresh(seen=seen, tried=tried)

        try:
            return self._keys[kid]
        except KeyError:
            raise JWKSError(f"no key with kid {kid!r} in {self.url}") from None

    async def health(self) -> tuple[bool, str]:
        state = ""
        if self._breaker is not None and self._breaker.state.value != "closed":
            state = f" (breaker {self._breaker.state.value})"
        if not self._keys:
            return False, (self._last_error or "no keys fetched yet") + state
        age = int(time.monotonic() - self._fetched_at)
        if self._last_error:
            # Serving cached keys through an outage is correct; hiding that it
            # is happening is not.
            return (
                False,
                f"serving keys cached {age}s ago; last refresh failed: {self._last_error}{state}",
            )
        return True, f"{len(self._keys)} key(s), cached {age}s ago"

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))
