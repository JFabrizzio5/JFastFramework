"""Background jobs.

    [plugin.queue]
    backend = "postgres"     # or "redis", "rabbitmq", or your own class

Publishes ``queue`` (the backend) and ``tasks`` (the registry). Enqueue from a
route, run the handler in a worker::

    tasks = request.app.state.jfast.require("tasks")

    @tasks.task("send_invoice_email")
    async def send_invoice_email(payload: dict) -> None: ...

    queue = request.app.state.jfast.require("queue")
    await queue.enqueue(Job(task="send_invoice_email", payload={"id": 7}))

Delivery is **at-least-once**. A worker can die after doing the work and
before acknowledging, so a handler that charges a card twice is a bug in the
handler, not in the queue. Make them idempotent.

Recurring tasks are declared on the same registry and run by a scheduler loop
in the service when ``scheduler = true``; see
:mod:`jfastframework.queues.scheduler`::

    @tasks.task("refresh_rates", every=timedelta(minutes=5))
    async def refresh_rates(payload: dict) -> None: ...

Requires the backend's extra: ``[db]``, ``[cache]`` or ``[rabbitmq]``.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)
from jfastframework.queues.base import QueueBackend
from jfastframework.queues.worker import TaskRegistry

if TYPE_CHECKING:
    from jfastframework.context import AppContext
    from jfastframework.queues.scheduler import Scheduler, TickStore

BACKENDS = ("postgres", "redis", "rabbitmq")


class QueueSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_QUEUE_", env_file=".env", extra="ignore")

    # "postgres" | "redis" | "rabbitmq" | "package.module:ClassName"
    backend: str = "postgres"
    name: str = "jfast_jobs"
    # How long a claimed job stays invisible before another worker may take
    # it. Shorter than your slowest handler and you get duplicate work.
    visibility_timeout: int = 300
    max_attempts: int = 3
    prefetch: int = 10

    rabbitmq_url: SecretStr = SecretStr("amqp://guest:guest@localhost:5672/")
    rabbitmq_include_infra: bool = True
    rabbitmq_port_offset: int = 6

    # Exposes GET /queue/stats. Handy in development, noise in production.
    expose_stats: bool = True

    # Run the scheduler loop in this process, enqueueing the recurring tasks
    # declared on the registry. Safe in every replica and every worker: each
    # tick is claimed in a shared store before it is enqueued.
    scheduler: bool = False
    # Where ticks are claimed. "auto" is the database when the database
    # plugin is enabled and the cache when only that one is. "memory" claims
    # per process, so every process fires every tick; production refuses it.
    scheduler_store: Literal["auto", "database", "cache", "memory"] = "auto"
    # Claims older than this are pruned, keeping each schedule's latest --
    # which is what catch-up reads after a restart.
    scheduler_retention_days: int = 7


class QueuePlugin(Plugin):
    meta = PluginMeta(
        name="queue",
        version="0.1.0",
        description="Background jobs over PostgreSQL, Redis or RabbitMQ.",
        # Which backend it needs depends on configuration, so the check lives
        # in register() where the config is known.
        after=("observability", "database", "cache"),
        provides=("queue", "tasks"),
        default_enabled=False,
        extra="jfastframework[queue]",
    )
    Settings = QueueSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._backend: QueueBackend | None = None
        self._registry = TaskRegistry()
        self._connection: Any = None
        self._setup_error: str | None = None
        self._scheduler: Scheduler | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_stop = asyncio.Event()

    def _build_backend(self, ctx: AppContext) -> QueueBackend:
        settings: QueueSettings = self.settings

        if settings.backend == "postgres":
            if not ctx.has("db.engine"):
                raise PluginError(
                    "queue backend 'postgres' needs the 'database' plugin. "
                    'Add "database" to [plugins].enabled, or set '
                    '[plugin.queue] backend = "redis".'
                )
            from jfastframework.queues.postgres import PostgresQueue

            return PostgresQueue(
                ctx.require("db.engine"),
                table=settings.name,
                visibility_timeout=settings.visibility_timeout,
            )

        if settings.backend == "redis":
            if not ctx.has("cache.client"):
                raise PluginError(
                    "queue backend 'redis' needs the 'cache' plugin. "
                    'Add "cache" to [plugins].enabled.'
                )
            from jfastframework.queues.redis import RedisQueue

            return RedisQueue(
                ctx.require("cache.client"),
                name=settings.name.replace("_", ":"),
                visibility_timeout=settings.visibility_timeout,
            )

        if settings.backend == "rabbitmq":
            from jfastframework.queues.rabbitmq import RabbitMQQueue

            # Connection is opened in startup(); register must not do I/O.
            return RabbitMQQueue(
                _LazyConnection(self),
                name=settings.name.replace("_", "."),
                prefetch=settings.prefetch,
            )

        module_path, _, attr = settings.backend.partition(":")
        if not module_path or not attr:
            raise PluginError(
                f"Invalid queue backend {settings.backend!r}. "
                f"Use one of {', '.join(BACKENDS)} or 'package.module:ClassName'."
            )
        try:
            cls = getattr(importlib.import_module(module_path), attr)
        except (ImportError, AttributeError) as exc:
            raise PluginError(f"Cannot load queue backend {settings.backend!r}: {exc}") from exc
        backend: QueueBackend = cls(ctx)
        return backend

    def _build_tick_store(self, ctx: AppContext) -> TickStore:
        settings: QueueSettings = self.settings
        choice = settings.scheduler_store
        if choice == "auto":
            if ctx.has("db.engine"):
                choice = "database"
            elif ctx.has("cache.client"):
                choice = "cache"
            else:
                choice = "memory"

        if choice == "database":
            if not ctx.has("db.engine"):
                raise PluginError(
                    "scheduler_store = \"database\" needs the 'database' plugin enabled."
                )
            from jfastframework.queues.sql_ticks import SqlTickStore

            return SqlTickStore(ctx.require("db.engine"))

        if choice == "cache":
            if not ctx.has("cache.client"):
                raise PluginError("scheduler_store = \"cache\" needs the 'cache' plugin enabled.")
            from jfastframework.queues.scheduler import RedisTickStore

            # The service name in the key: two services sharing a Redis may
            # each have a schedule called "cleanup".
            prefix = f"{ctx.settings.app_name}:{settings.name}:schedule"
            return RedisTickStore(ctx.require("cache.client"), prefix=prefix)

        from jfastframework.queues.scheduler import MemoryTickStore

        if ctx.settings.is_production:
            raise PluginError(
                "[plugin.queue] scheduler = true with nothing shared to claim ticks in: every "
                "replica and every worker process would enqueue every tick. Enable the "
                "'database' or 'cache' plugin, or set scheduler = false and run the scheduler "
                "in exactly one process."
            )
        ctx.logger.warning(
            "queue scheduler claims ticks in memory: correct for one process only; "
            "enable the database or cache plugin before running more than one"
        )
        return MemoryTickStore()

    def register(self, ctx: AppContext) -> None:
        self._backend = self._build_backend(ctx)
        ctx.provide("queue", self._backend)
        ctx.provide("tasks", self._registry)

        if self.settings.scheduler:
            from jfastframework.queues.scheduler import Scheduler

            self._scheduler = Scheduler(
                self._backend,
                self._registry,
                self._build_tick_store(ctx),
                retention=timedelta(days=self.settings.scheduler_retention_days),
            )

        if self.settings.expose_stats:
            ctx.app.include_router(self._build_router(), prefix="/queue", tags=["queue"])

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/stats", summary="Queue depths")
        async def stats() -> dict[str, Any]:
            if self._backend is None:
                return {"error": "queue not initialised"}
            return {
                "backend": self.settings.backend,
                "tasks": list(self._registry.names),
                "depths": await self._backend.stats(),
            }

        return router

    async def startup(self, ctx: AppContext) -> None:
        # A broker that is not up yet must not crash the process. An
        # orchestrator handles "started but not ready" gracefully; it handles
        # a crash loop by backing off and paging someone. The failure is
        # recorded and surfaced by /ready instead, with the reason.
        try:
            if self.settings.backend == "rabbitmq":
                import aio_pika

                self._connection = await aio_pika.connect_robust(
                    self.settings.rabbitmq_url.get_secret_value()
                )
            if self._backend is not None:
                await self._backend.setup()
        except Exception as exc:  # noqa: BLE001 - reported through /ready
            self._setup_error = str(exc)
            ctx.logger.error(
                "queue setup failed; the service is serving but not ready",
                extra={"backend": self.settings.backend, "error": str(exc)},
            )
        else:
            self._setup_error = None

        # Started even when setup failed: the loop retries its own store and
        # reports through /ready, and a broker that comes up later is used.
        if self._scheduler is not None:
            self._scheduler_stop.clear()
            self._scheduler_task = asyncio.create_task(
                self._scheduler.run(self._scheduler_stop), name="jfast-queue-scheduler"
            )

    async def shutdown(self, ctx: AppContext) -> None:
        # The scheduler first: it enqueues through the backend closed below.
        self._scheduler_stop.set()
        if self._scheduler_task is not None:
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=10)
            except TimeoutError:
                self._scheduler_task.cancel()
            self._scheduler_task = None
        if self._backend is not None:
            await self._backend.close()
        if self._connection is not None:
            await self._connection.close()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._backend is None:
            return HealthReport.fail("queue not initialised")
        meta: dict[str, Any] = {"backend": self.settings.backend}
        if self._scheduler is not None:
            meta["scheduler"] = self._scheduler.status()
        if self._setup_error is not None:
            return HealthReport.fail(f"queue setup failed: {self._setup_error}", **meta)
        healthy, detail = await self._backend.health()
        meta["tasks"] = list(self._registry.names)
        if not healthy:
            return HealthReport.fail(detail, **meta)

        depths = await self._backend.stats()
        # A growing dead-letter queue is a problem to see, not to page on:
        # the service is still serving.
        if depths.get("dead", 0) > 0:
            return HealthReport.fail(
                f"{depths['dead']} job(s) in the dead-letter queue",
                critical=False,
                depths=depths,
                **meta,
            )
        # Likewise a scheduler that stopped: recurring work is late, and the
        # requests this replica serves are not.
        if self._scheduler is not None and not self._scheduler.healthy:
            error = self._scheduler.status()["error"] or "the scheduler loop is not running"
            return HealthReport.fail(f"scheduler: {error}", critical=False, depths=depths, **meta)
        return HealthReport.ok(detail, depths=depths, **meta)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: QueueSettings = self.settings
        if settings.backend != "rabbitmq" or not settings.rabbitmq_include_infra:
            # postgres and redis backends reuse the container their own plugin
            # already declares; declaring it twice would collide on the name.
            return []
        return [
            InfraService(
                name="rabbitmq",
                image="rabbitmq:3.13-management-alpine",
                port_offset=settings.rabbitmq_port_offset,
                internal_port=5672,
                environment={
                    "RABBITMQ_DEFAULT_USER": "app",
                    "RABBITMQ_DEFAULT_PASS": "${RABBITMQ_PASSWORD:?set RABBITMQ_PASSWORD}",
                },
                volumes=["rabbitmq_data:/var/lib/rabbitmq"],
                client_env={
                    "JFAST_QUEUE_RABBITMQ_URL": "amqp://app:${RABBITMQ_PASSWORD}@rabbitmq:5672/"
                },
                healthcheck={
                    "test": ["CMD", "rabbitmq-diagnostics", "-q", "ping"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 10,
                },
            )
        ]


class _LazyConnection:
    """Defers the AMQP connection until ``startup``.

    ``register`` must not do I/O -- it runs before the event loop is serving,
    and a blocking connect there stalls startup for the whole service.
    """

    def __init__(self, plugin: QueuePlugin) -> None:
        self._plugin = plugin

    async def channel(self, **options: Any) -> Any:
        if self._plugin._connection is None:
            raise PluginError("RabbitMQ connection is not open yet")
        return await self._plugin._connection.channel(**options)
