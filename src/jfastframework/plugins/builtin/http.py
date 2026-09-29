"""Calls to sibling services, configured by name.

    [plugins]
    enabled = ["observability", "http"]

    [plugin.http.upstreams.billing]
    base_url = "http://billing:8010"
    read_timeout = 5.0
    retries = 2
    forward_authorization = true

Publishes ``http``, a factory holding one client per upstream for the whole
process::

    billing = request.app.state.jfast.require("http").client("billing")
    response = await billing.get(f"/invoices/{invoice_id}")

The base URL can come from the environment instead of the file --
``JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL`` -- and a value in ``jfast.toml``
wins over one in the environment, as for every plugin. ``/ready`` reports an
open or half-open breaker as degraded, never as unavailable: this service is
still up, and taking it out of rotation because a dependency is down turns
one outage into two.

Requires: ``pip install jfastframework[http]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import SettingsConfigDict

from jfastframework.http.context import inbound_authorization
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from jfastframework.context import AppContext
    from jfastframework.http.client import HttpClients, Upstream


class UpstreamSettings(BaseModel):
    # A misspelt key would otherwise fall back to the default in silence --
    # `read_timout = 60` and a read timeout of ten seconds.
    model_config = ConfigDict(extra="forbid")

    base_url: str
    connect_timeout: float = Field(2.0, gt=0)
    read_timeout: float = Field(10.0, gt=0)
    write_timeout: float = Field(10.0, gt=0)
    pool_timeout: float = Field(2.0, gt=0)
    # Around the whole call, retries and waits included.
    total_timeout: float = Field(30.0, gt=0)
    # Retries after the first attempt, for the calls that may be retried.
    retries: int = Field(2, ge=0)
    backoff_base: float = Field(0.1, ge=0)
    backoff_max: float = Field(2.0, ge=0)
    max_retry_after: float = Field(30.0, ge=0)
    retry_budget_ratio: float = Field(0.2, ge=0)
    retry_budget_min_per_second: float = Field(1.0, ge=0)
    breaker_failures: int = Field(5, ge=1)
    breaker_failure_rate: float = Field(0.5, gt=0, le=1)
    breaker_minimum_calls: int = Field(20, ge=1)
    breaker_window: float = Field(30.0, gt=0)
    breaker_cool_down: float = Field(15.0, gt=0)
    max_concurrent: int = Field(50, ge=1)
    bulkhead_wait: float = Field(0.5, ge=0)
    forward_authorization: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    def build(self, name: str) -> Upstream:
        from jfastframework.http.client import Timeouts, Upstream
        from jfastframework.http.resilience import BreakerPolicy, RetryPolicy

        return Upstream(
            name=name,
            base_url=self.base_url,
            timeouts=Timeouts(
                connect=self.connect_timeout,
                read=self.read_timeout,
                write=self.write_timeout,
                pool=self.pool_timeout,
                total=self.total_timeout,
            ),
            retry=RetryPolicy(
                attempts=self.retries + 1,
                backoff_base=self.backoff_base,
                backoff_max=self.backoff_max,
                max_retry_after=self.max_retry_after,
            ),
            breaker=BreakerPolicy(
                failure_threshold=self.breaker_failures,
                failure_rate=self.breaker_failure_rate,
                minimum_calls=self.breaker_minimum_calls,
                window=self.breaker_window,
                cool_down=self.breaker_cool_down,
            ),
            retry_budget_ratio=self.retry_budget_ratio,
            retry_budget_min_per_second=self.retry_budget_min_per_second,
            max_concurrent=self.max_concurrent,
            bulkhead_wait=self.bulkhead_wait,
            forward_authorization=self.forward_authorization,
            headers=dict(self.headers),
        )


class HttpSettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_HTTP_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

    upstreams: dict[str, UpstreamSettings] = Field(default_factory=dict)


class CaptureBearerToken:
    """Keeps the request's bearer token where the client can forward it.

    Pure ASGI rather than ``BaseHTTPMiddleware``: the variable has to be set
    in the task that runs the endpoint, and it is, because nothing here
    spawns another. Only a ``Bearer`` credential is kept; Basic credentials
    are a password, and no upstream gets one of those passed along.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = None
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                text = value.decode("latin-1")
                if text[:7].lower() == "bearer ":
                    token = text
                break
        reset = inbound_authorization.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            inbound_authorization.reset(reset)


class HttpPlugin(Plugin):
    meta = PluginMeta(
        name="http",
        version="0.1.0",
        description="Calls to sibling services: deadlines, retries, circuit breakers, bulkheads.",
        # After observability so the request id it sets is there to forward.
        after=("observability",),
        provides=("http",),
        default_enabled=False,
        extra="jfastframework[http]",
        # A dependency that is down degrades this service; it does not make
        # it unready, or one outage takes every caller of every caller out.
        health_critical=False,
    )
    Settings = HttpSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._clients: HttpClients | None = None

    def register(self, ctx: AppContext) -> None:
        from jfastframework.http.client import HttpClients

        settings: HttpSettings = self.settings
        # No I/O: each client opens its connection pool on first use.
        self._clients = HttpClients(
            {name: upstream.build(name) for name, upstream in settings.upstreams.items()}
        )
        ctx.provide("http", self._clients)
        if any(upstream.forward_authorization for upstream in settings.upstreams.values()):
            ctx.app.add_middleware(CaptureBearerToken)

    async def shutdown(self, ctx: AppContext) -> None:
        if self._clients is not None:
            await self._clients.aclose()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._clients is None:
            return HealthReport.fail("http clients not initialised", critical=False)
        breakers = self._clients.breakers()
        suspended = self._clients.open_breakers()
        if suspended:
            return HealthReport.fail(
                f"circuit open for {', '.join(suspended)}", critical=False, breakers=breakers
            )
        return HealthReport.ok(f"{len(breakers)} upstream(s) closed", breakers=breakers)
