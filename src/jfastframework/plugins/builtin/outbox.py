"""The transactional outbox, as a plugin.

    [plugins]
    enabled = ["database", "queue", "outbox"]

Publishes ``outbox``. Write through the request's session and the message
commits with the rows it is about::

    from jfastframework.plugins.builtin.database import DbSession

    @router.post("/orders", status_code=201)
    async def create(payload: OrderIn, request: Request, session: DbSession):
        order = await OrderRepository(session).create(**payload.model_dump())
        outbox = request.app.state.jfast.require("outbox")
        await outbox.enqueue(session, Job(task="send_receipt", payload={"id": order.id}))
        return order

A relay runs in every process and moves committed messages to the queue and
the event bus; see :mod:`jfastframework.outbox` for the delivery guarantees.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext
    from jfastframework.outbox import OutboxRelay


class OutboxSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_OUTBOX_", env_file=".env", extra="ignore")

    # Run the relay inside this process. Off for a service that runs it as a
    # separate worker; on everywhere else, and safe in every worker at once.
    relay: bool = True
    interval_seconds: float = 1.0
    batch_size: int = 100
    # After this many failed sends a message is set aside as dead rather than
    # retried forever. /ready reports it.
    max_attempts: int = 20
    retention_days: int = 7
    # /ready turns degraded when the oldest unsent message is older than this:
    # a relay that stopped is otherwise silent until somebody asks where their
    # email went.
    stale_after_seconds: int = 300


class OutboxPlugin(Plugin):
    meta = PluginMeta(
        name="outbox",
        version="0.1.0",
        description="Transactional outbox and inbox: messages commit with the rows they are about.",
        requires=("database",),
        after=("database", "queue", "events"),
        provides=("outbox", "outbox.relay"),
        default_enabled=False,
        extra="jfastframework[db]",
    )
    Settings = OutboxSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._relay: OutboxRelay | None = None
        self._engine: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def register(self, ctx: AppContext) -> None:
        from datetime import timedelta

        from jfastframework.outbox import Outbox, OutboxRelay

        if not ctx.has("db.engine"):
            raise PluginError("the outbox plugin needs the 'database' plugin enabled first")
        settings: OutboxSettings = self.settings
        self._engine = ctx.require("db.engine")
        queue = ctx.require("queue") if ctx.has("queue") else None
        events = ctx.require("events") if ctx.has("events") else None

        self._relay = OutboxRelay(
            self._engine,
            queue=queue,
            events=events,
            batch_size=settings.batch_size,
            max_attempts=settings.max_attempts,
            retention=timedelta(days=settings.retention_days),
        )
        ctx.provide("outbox", Outbox(queue=queue))
        ctx.provide("outbox.relay", self._relay)

    async def startup(self, ctx: AppContext) -> None:
        from jfastframework.db.framework import ensure_tables
        from jfastframework.outbox import INBOX_TABLE, OUTBOX_TABLE

        await ensure_tables(self._engine, OUTBOX_TABLE, INBOX_TABLE)
        settings: OutboxSettings = self.settings
        if settings.relay and self._relay is not None:
            self._stop.clear()
            self._task = asyncio.create_task(
                self._relay.run(self._stop, interval=settings.interval_seconds),
                name="jfast-outbox-relay",
            )

    async def shutdown(self, ctx: AppContext) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._relay is None:
            return HealthReport.fail("outbox not initialised")
        settings: OutboxSettings = self.settings
        try:
            stats = await self._relay.stats()
            oldest = await self._relay.oldest_pending_seconds()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return HealthReport.fail(f"outbox unreadable: {exc}", critical=False)
        meta: dict[str, Any] = {**stats, "oldest_pending_seconds": oldest}
        if stats["dead"]:
            return HealthReport.fail(
                f"{stats['dead']} message(s) failed permanently", critical=False, **meta
            )
        if oldest is not None and oldest > settings.stale_after_seconds:
            return HealthReport.fail(
                f"oldest unsent message is {int(oldest)}s old; is the relay running?",
                critical=False,
                **meta,
            )
        return HealthReport.ok("outbox draining", **meta)
