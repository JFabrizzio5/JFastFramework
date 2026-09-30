"""Redis cache, pub/sub and queue.

Publishes ``cache`` (a small typed facade) and ``cache.client`` (the raw redis
client, for anything the facade does not cover).

Convention inherited from the CometaX stack: DB 0 = cache, DB 1 = pub/sub,
DB 2 = queue. One Redis container, three logical namespaces.

Every command has a deadline and the client has a circuit breaker, both on by
default. Redis that stops answering -- paused, partitioned, swapping -- used to
hold each request for as long as the socket lived, because redis-py sets no
read timeout; now a command gives up after ``command_timeout`` and, after
``breaker_failures`` of those in a row, every caller fails in microseconds
until ``breaker_cool_down`` has passed and one probe finds Redis back. Blocking
commands (``BLMOVE``, ``BLPOP``, ``XREAD``...) and pub/sub are exempt: waiting
is what they are for, and the queue and ``channels`` read them from this same
client. See docs/resilience.md.

``get_or_set`` is the resilient read path and the one to reach for: it absorbs
a backend failure by falling back to the loader, which is what makes
``health_critical=False`` an honest claim. The primitives below it --
``get``, ``set``, ``delete``, ``exists`` -- still raise, because a service that
cannot tell "cached nothing" from "Redis is gone" is a service that serves
stale answers forever without anyone noticing.

Requires: ``pip install jfastframework[cache]``
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.http.resilience import BreakerPolicy, CircuitBreaker
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext


# Distinguishes "no value stored" from a stored ``null``, which a caller is
# entitled to cache and which ``default=None`` would otherwise hide.
_MISS: Any = object()

# How often a caller waiting on the lock holder re-reads the key. Short enough
# that the wait is dominated by the loader rather than by the poll.
_POLL_INTERVAL = 0.02


class CacheSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_CACHE_", env_file=".env", extra="ignore")

    url: str = "redis://localhost:6379/0"
    default_ttl: int = 300
    key_prefix: str = ""

    # Upper bound on how long one caller may hold the recompute lock. It only
    # has to outlive a normal loader; a loader slower than this produces
    # duplicate work, never a wrong answer.
    stampede_lock_ttl: int = 10
    # How long a caller that lost the race waits for the winner's value before
    # loading for itself. Zero disables the lock entirely.
    stampede_wait: float = 2.0

    # -- deadlines -----------------------------------------------------
    # Seconds to open a TCP connection. A Redis on the same network answers a
    # SYN in well under a millisecond; two seconds covers a cold container and
    # DNS, and anything slower is a Redis that is not there.
    connect_timeout: float = 2.0
    # Seconds one command may take, connecting included. A cache read slower
    # than this costs more than the recompute it saves, and the rate limiter
    # and the token store sit in front of every request. Blocking commands and
    # pub/sub are exempt. 0 turns the deadline off.
    command_timeout: float = 2.0
    # Consecutive failures (timeouts, refused or dropped connections) that
    # open the breaker. While it is open no command is sent: callers get a
    # `CircuitOpenError` -- a 503 if it escapes a route, "Redis is down" to
    # the code that already fails open -- instead of each waiting out
    # `command_timeout` on a Redis that is not answering. 0 turns it off.
    breaker_failures: int = 5
    # Seconds the breaker stays open before letting one probe through. Short,
    # because a cache is cheap to probe and expensive to go without.
    breaker_cool_down: float = 5.0

    include_infra: bool = True
    image: str = "redis:7-alpine"
    port_offset: int = 3

    def validate_for_boot(self) -> None:
        """Every value that would otherwise fail on the first command, refused now."""
        scheme = self.url.partition("://")[0].lower()
        if scheme not in ("redis", "rediss", "unix"):
            raise PluginError(
                f"[plugin.cache] url must start with redis://, rediss:// or unix://, "
                f"not {self.url.split('@')[-1]!r}. Set JFAST_CACHE_URL."
            )
        if self.default_ttl < 0:
            raise PluginError(
                "[plugin.cache] default_ttl cannot be negative; use 0 to store without expiry."
            )
        if self.stampede_lock_ttl < 1:
            raise PluginError(
                "[plugin.cache] stampede_lock_ttl must be at least 1 second: Redis "
                "cannot expire a lock in less, and 0 would hold it forever."
            )
        if self.stampede_wait < 0:
            raise PluginError("[plugin.cache] stampede_wait cannot be negative; 0 disables it.")
        if self.connect_timeout <= 0:
            raise PluginError(
                "[plugin.cache] connect_timeout must be positive: without one a "
                "connect to an unreachable Redis waits for the operating system."
            )
        if self.command_timeout < 0 or self.breaker_failures < 0:
            raise PluginError(
                "[plugin.cache] command_timeout and breaker_failures cannot be "
                "negative; 0 turns each off."
            )
        if self.breaker_cool_down <= 0:
            raise PluginError("[plugin.cache] breaker_cool_down must be positive.")


class CacheMetrics:
    """Cache counters on the shared Prometheus registry.

    Constructed with ``registry=None`` when the ``metrics`` plugin is disabled,
    and every method is then a no-op. The cache never asks which case it is in.
    """

    def __init__(self, registry: Any = None) -> None:
        self._hits: Any = None
        self._misses: Any = None
        self._errors: Any = None
        self._suppressed: Any = None
        if registry is None:
            return

        from prometheus_client import Counter

        self._hits = Counter(
            "cache_hits_total", "Cache reads served from the cache.", registry=registry
        )
        self._misses = Counter(
            "cache_misses_total", "Cache reads that found nothing stored.", registry=registry
        )
        self._errors = Counter(
            "cache_errors_total", "Cache operations the backend refused.", registry=registry
        )
        self._suppressed = Counter(
            "cache_stampede_suppressed_total",
            "Loader runs skipped because another caller was already computing the key.",
            registry=registry,
        )

    @staticmethod
    def _inc(counter: Any) -> None:
        if counter is not None:
            counter.inc()

    def hit(self) -> None:
        self._inc(self._hits)

    def miss(self) -> None:
        self._inc(self._misses)

    def error(self) -> None:
        self._inc(self._errors)

    def suppressed(self) -> None:
        self._inc(self._suppressed)


class Cache:
    """Namespaced JSON cache over a redis client."""

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "",
        default_ttl: int = 300,
        stampede_lock_ttl: int = 10,
        stampede_wait: float = 2.0,
        metrics: CacheMetrics | None = None,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._default_ttl = default_ttl
        self._stampede_lock_ttl = stampede_lock_ttl
        self._stampede_wait = stampede_wait
        self._metrics = metrics if metrics is not None else CacheMetrics()

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}" if self._prefix else key

    def _lock_key(self, key: str) -> str:
        # Suffixed rather than prefixed so a lock cannot collide with a real
        # key: nothing legitimate ends in this.
        return f"{self._key(key)}:__jfast_lock"

    def _expiry(self, ttl: int | None) -> int | None:
        if ttl is None:
            return self._default_ttl
        if ttl < 0:
            raise ValueError(f"ttl must be >= 0, got {ttl}. Use ttl=0 to store without expiry.")
        # Redis has no "expire in zero seconds", so zero is free to mean the
        # one thing `ttl=None` cannot: store it until something deletes it.
        return ttl or None

    async def _read(self, key: str) -> Any:
        """Uncounted read. ``_MISS`` when nothing is stored."""
        raw = await self._client.get(self._key(key))
        if raw is None:
            return _MISS
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Another system writes to this Redis and does not owe us JSON.
            return raw

    async def get(self, key: str, default: Any = None) -> Any:
        try:
            found = await self._read(key)
        except Exception:
            self._metrics.error()
            raise
        if found is _MISS:
            self._metrics.miss()
            return default
        self._metrics.hit()
        return found

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._client.set(
            self._key(key),
            json.dumps(value, default=str),
            ex=self._expiry(ttl),
        )

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*(self._key(k) for k in keys)))

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._key(key)))

    async def publish(self, channel: str, message: Any) -> None:
        # Deliberately unprefixed. A channel name is a contract with whoever
        # else is on this Redis -- often a Laravel app that never heard of our
        # key prefix. Namespacing channels is the `channels` plugin's job,
        # under its own setting.
        await self._client.publish(channel, json.dumps(message, default=str))

    # -- read-through --------------------------------------------------

    async def get_or_set(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        ttl: int | None = None,
    ) -> Any:
        """Return the cached value, or run ``loader`` and cache what it gives.

        Every backend failure degrades to calling ``loader``: a cache that is
        down costs this call a round trip and a recomputation, never a 500.
        Errors raised by ``loader`` itself propagate -- masking those would
        turn a broken query into a quiet wrong answer.

        Concurrent misses on the same key are collapsed: one caller takes a
        short lock and recomputes while the others wait for its result, up to
        ``stampede_wait``. A caller that waits that long stops waiting and
        loads for itself, so a stalled loader costs duplicated work rather
        than a queue of stalled requests.
        """
        try:
            found = await self.get(key, _MISS)
        except Exception:  # noqa: BLE001
            found = _MISS
        if found is not _MISS:
            return found

        if self._stampede_wait <= 0:
            return await self._load(key, loader, ttl)

        if await self._acquire(key):
            try:
                return await self._load(key, loader, ttl)
            finally:
                await self._release(key)

        leader_value = await self._await_leader(key)
        if leader_value is not _MISS:
            self._metrics.suppressed()
            return leader_value
        return await self._load(key, loader, ttl)

    async def _load(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl: int | None,
    ) -> Any:
        value = await loader()
        try:
            await self.set(key, value, ttl)
        except Exception:  # noqa: BLE001
            # The caller asked for a value, not for it to be cached.
            self._metrics.error()
        return value

    async def _acquire(self, key: str) -> bool:
        """True if this caller may recompute.

        A backend that cannot answer grants the lock: the fallback for "we do
        not know" has to be doing the work, not refusing to.
        """
        try:
            taken = await self._client.set(
                self._lock_key(key),
                uuid.uuid4().hex,
                ex=self._stampede_lock_ttl,
                nx=True,
            )
        except Exception:  # noqa: BLE001
            self._metrics.error()
            return True
        return bool(taken)

    async def _release(self, key: str) -> None:
        # Unconditional: this is an optimisation, not mutual exclusion. If the
        # TTL already expired and another caller holds the lock, deleting it
        # costs one duplicate loader run and nothing else.
        try:
            await self._client.delete(self._lock_key(key))
        except Exception:  # noqa: BLE001
            self._metrics.error()

    async def _await_leader(self, key: str) -> Any:
        """Poll for the lock holder's value until ``stampede_wait`` runs out."""
        deadline = time.monotonic() + self._stampede_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                found = await self._read(key)
            except Exception:  # noqa: BLE001
                self._metrics.error()
                return _MISS
            if found is not _MISS:
                return found
        return _MISS


#: Commands that wait on purpose: a deadline would cut a queue's long poll or a
#: stream read short. Pub/sub does not go through ``execute_command`` at all.
BLOCKING_COMMANDS = frozenset(
    {
        "BLPOP",
        "BRPOP",
        "BRPOPLPUSH",
        "BLMOVE",
        "BLMPOP",
        "BZPOPMIN",
        "BZPOPMAX",
        "BZMPOP",
        "XREAD",
        "XREADGROUP",
        "WAIT",
        "WAITAOF",
        "MONITOR",
    }
)

_CLIENT_CLASS: Any = None
_OPEN_ERROR: Any = None


def _redis_classes() -> tuple[Any, Any]:
    """The bounded client and its circuit-open error, built on first use.

    Built lazily so importing this module does not import redis: the plugin
    only needs it once it is enabled, and ``jfast describe`` should not.
    """
    global _CLIENT_CLASS, _OPEN_ERROR
    if _CLIENT_CLASS is not None:
        return _CLIENT_CLASS, _OPEN_ERROR

    import redis.asyncio as aioredis
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from jfastframework.http.errors import CircuitOpenError

    class RedisCircuitOpenError(CircuitOpenError, RedisConnectionError):  # type: ignore[misc]
        """The cache breaker is open. Both a 503 and a ``redis.ConnectionError``.

        A 503 so a route that lets it escape answers ``Service Unavailable``;
        a redis ``ConnectionError`` so code written against redis-py --
        ``except RedisError`` -- treats it as the outage it stands for.
        """

    class BoundedRedis(aioredis.Redis):  # type: ignore[misc]
        """redis-py's client with a deadline per command and a breaker around it."""

        jfast_command_timeout: float = 0.0
        jfast_breaker: CircuitBreaker | None = None

        async def execute_command(self, *args: Any, **options: Any) -> Any:
            name = str(args[0]).upper() if args else ""
            breaker = self.jfast_breaker
            timeout = self.jfast_command_timeout
            if name in BLOCKING_COMMANDS or (breaker is None and not timeout):
                return await super().execute_command(*args, **options)

            permit = None
            if breaker is not None:
                try:
                    permit = breaker.acquire()
                except CircuitOpenError as exc:
                    raise RedisCircuitOpenError(
                        upstream=breaker.name, retry_after=exc.retry_after
                    ) from None
            try:
                if timeout:
                    try:
                        async with asyncio.timeout(timeout):
                            result = await super().execute_command(*args, **options)
                    except TimeoutError as exc:
                        # redis-py's own class, so `except RedisError` sees it.
                        # The cancelled connection was already dropped by
                        # redis-py; the next command opens a fresh one.
                        raise RedisTimeoutError(
                            f"redis did not answer {name} within {timeout}s"
                        ) from exc
                else:
                    result = await super().execute_command(*args, **options)
            except (RedisConnectionError, RedisTimeoutError, OSError):
                if breaker is not None and permit is not None:
                    breaker.record(permit, failed=True)
                raise
            except asyncio.CancelledError:
                if breaker is not None and permit is not None:
                    breaker.release(permit)
                raise
            except Exception:
                # WRONGTYPE, NOSCRIPT, a script error: Redis answered, which is
                # all the breaker is asked to know.
                if breaker is not None and permit is not None:
                    breaker.record(permit, failed=False)
                raise
            if breaker is not None and permit is not None:
                breaker.record(permit, failed=False)
            return result

    _CLIENT_CLASS, _OPEN_ERROR = BoundedRedis, RedisCircuitOpenError
    return _CLIENT_CLASS, _OPEN_ERROR


def build_client(settings: CacheSettings, *, name: str = "redis") -> Any:
    """A redis client with the deadlines and breaker ``settings`` describe."""
    client_class, _ = _redis_classes()
    client = client_class.from_url(
        settings.url,
        decode_responses=True,
        socket_connect_timeout=settings.connect_timeout,
        # Keepalive so a connection the network silently dropped is found by
        # the kernel rather than by the next request that borrows it.
        socket_keepalive=True,
    )
    client.jfast_command_timeout = settings.command_timeout
    if settings.breaker_failures:
        client.jfast_breaker = CircuitBreaker(
            name,
            BreakerPolicy(
                failure_threshold=settings.breaker_failures,
                cool_down=settings.breaker_cool_down,
            ),
        )
    return client


class CachePlugin(Plugin):
    meta = PluginMeta(
        name="cache",
        version="0.1.0",
        description="Redis cache, pub/sub and queue backend.",
        # `metrics` is soft, not required: it publishes the registry the cache
        # counters live on, but a service that wants a cache should not have
        # to carry prometheus-client to get one.
        after=("observability", "metrics"),
        provides=("cache", "cache.client"),
        default_enabled=False,
        extra="jfastframework[cache]",
        # A cache that stops answering degrades the service. It does not
        # break it, and taking every replica out of rotation would.
        health_critical=False,
    )
    Settings = CacheSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None

    def register(self, ctx: AppContext) -> None:
        settings: CacheSettings = self.settings
        settings.validate_for_boot()
        prefix = settings.key_prefix or f"{ctx.settings.app_name}:"
        client = build_client(settings)

        self._client = client
        ctx.provide("cache.client", client)
        ctx.provide(
            "cache",
            Cache(
                client,
                prefix=prefix,
                default_ttl=settings.default_ttl,
                stampede_lock_ttl=settings.stampede_lock_ttl,
                stampede_wait=settings.stampede_wait,
                # `after` puts metrics first when it is loaded; absent is the
                # normal case for a service that disabled it, not an error.
                metrics=CacheMetrics(ctx.get("metrics.registry")),
            ),
        )

    async def shutdown(self, ctx: AppContext) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._client is None:
            return HealthReport.fail("redis client not initialised", critical=False)
        breaker: CircuitBreaker | None = getattr(self._client, "jfast_breaker", None)
        meta = {"breaker": breaker.snapshot()} if breaker is not None else {}
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001
            # Cache being down degrades the service; it does not break it.
            if breaker is not None:
                meta = {"breaker": breaker.snapshot()}
            return HealthReport.fail(f"redis unreachable: {exc}", critical=False, **meta)
        return HealthReport.ok("redis reachable", **meta)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: CacheSettings = self.settings
        if not settings.include_infra:
            return []
        return [
            InfraService(
                name="redis",
                image=settings.image,
                port_offset=settings.port_offset,
                internal_port=6379,
                command="redis-server --appendonly yes",
                volumes=["redis_data:/data"],
                client_env={"JFAST_CACHE_URL": "redis://redis:6379/0"},
                healthcheck={
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 10,
                },
            )
        ]
