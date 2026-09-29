"""Idempotency keys, as a plugin.

    [plugins]
    enabled = ["database", "idempotency"]

    [plugin.idempotency]
    ttl_hours = 24

Then depend on ``IdempotencyKey`` (or ``RequiredIdempotencyKey``) from
:mod:`jfastframework.idempotency` on the routes that create things. See that
module for what each outcome means.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class IdempotencySettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_IDEMPOTENCY_", env_file=".env", extra="ignore"
    )

    header: str = "Idempotency-Key"
    # How long a key is remembered. Long enough to outlast any client's retry
    # policy; a day is what Stripe uses.
    ttl_hours: int = 24
    # A response larger than this is not kept: the key still stops a second
    # execution, and a retry is told to read the resource instead.
    max_body_bytes: int = 256 * 1024
    purge_interval_seconds: int = 600


class IdempotencyPlugin(Plugin):
    meta = PluginMeta(
        name="idempotency",
        version="0.1.0",
        description="Idempotency-Key for POSTs: a retry gets the first answer, not a second write.",
        requires=("database",),
        after=("database", "tenancy", "auth"),
        provides=("idempotency.settings",),
        default_enabled=False,
        extra="jfastframework[db]",
    )
    Settings = IdempotencySettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._engine: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def register(self, ctx: AppContext) -> None:
        from jfastframework.idempotency import IdempotencyRecorder, IdempotentReplay

        if not ctx.has("db.engine"):
            raise PluginError("the idempotency plugin needs the 'database' plugin enabled first")
        settings: IdempotencySettings = self.settings
        self._engine = ctx.require("db.engine")
        ctx.provide("idempotency.settings", settings)

        async def replay(request: Any, exc: Exception) -> Any:
            assert isinstance(exc, IdempotentReplay)
            return exc.response()

        ctx.app.add_exception_handler(IdempotentReplay, replay)
        ctx.app.add_middleware(
            IdempotencyRecorder, engine=self._engine, max_body_bytes=settings.max_body_bytes
        )

    async def startup(self, ctx: AppContext) -> None:
        from jfastframework.db.framework import ensure_tables
        from jfastframework.idempotency import IDEMPOTENCY_TABLE

        await ensure_tables(self._engine, IDEMPOTENCY_TABLE)
        self._stop.clear()
        self._task = asyncio.create_task(self._purge_loop(), name="jfast-idempotency-purge")

    async def _purge_loop(self) -> None:
        from jfastframework.idempotency import purge_expired

        settings: IdempotencySettings = self.settings
        while not self._stop.is_set():
            try:
                await purge_expired(self._engine, ttl=timedelta(hours=settings.ttl_hours))
            except Exception:
                import logging

                logging.getLogger("jfast.idempotency").exception("purging expired keys failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=settings.purge_interval_seconds)

    async def shutdown(self, ctx: AppContext) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._engine is None:
            return HealthReport.fail("idempotency not initialised")
        return HealthReport.ok("idempotency keys recorded", ttl_hours=self.settings.ttl_hours)
