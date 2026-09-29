"""Prometheus metrics.

Default-enabled, and the first thing you turn off for a service that should
not carry the dependency::

    [plugins]
    disabled = ["metrics"]

Exposes the RED signals (Rate, Errors, Duration) plus whatever the service
registers on the shared registry, which is published as the ``metrics.registry``
provider.

Requires: ``pip install jfastframework[metrics]``
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class MetricsSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_METRICS_", env_file=".env", extra="ignore")

    path: str = "/metrics"
    # Emit a docker-compose Prometheus + Grafana pair when generating deploys.
    include_infra: bool = False
    prometheus_port_offset: int = 5
    grafana_port_offset: int = 6


#: The label for a request no route matched. Scanners probing random paths
#: must not mint one time series per path they try.
UNMATCHED = "<unmatched>"


def route_template(scope: Scope, root_path: str = "") -> str:
    """``/users/{user_id}``, not ``/users/42``: one series per route, not per id.

    Only known once the router has run -- it writes ``scope["route"]`` when it
    matches -- so it is read after the app, never before.
    """
    # FastAPI 0.121+ includes routers lazily: the route keeps its own path
    # (/login) and the prefix (/auth) lives in the effective route context it
    # puts in its private scope. Read that when it is there, and fall back to
    # the route itself -- the public, older shape -- when it is not.
    context = (scope.get("fastapi") or {}).get("effective_route_context")
    path = getattr(context, "path_format", None) or getattr(context, "path", None)
    if not isinstance(path, str) or not path:
        path = getattr(scope.get("route"), "path", None)
    if not isinstance(path, str):
        return UNMATCHED
    # A Mount appends its prefix to root_path in the shared scope, and its
    # routes' paths are relative to it: /login under /auth. What the mounts
    # added goes back in front; the deployment's own root_path (/api behind a
    # proxy) does not, so the same route is the same series everywhere.
    mounted = scope.get("root_path", "")
    prefix = mounted[len(root_path) :] if mounted.startswith(root_path) else ""
    return prefix + path


class PrometheusMiddleware:
    """RED metrics per route template.

    Plain ASGI rather than ``BaseHTTPMiddleware`` (about 75 us of CPU per
    request each, measured), and it labels by the matched route *after* the
    app has run. Through 0.1.0a9 it read the route before routing, found
    none, and fell back to the raw path: ``/users/41``, ``/users/42``... one
    series each, a registry that grew without bound. The in-flight gauge is
    labelled by method only, because the route is not known while a request
    is still in flight.
    """

    def __init__(self, app: ASGIApp, *, requests: Any, latency: Any, in_progress: Any) -> None:
        self.app = app
        self.requests = requests
        self.latency = latency
        self.in_progress = in_progress

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        root_path = scope.get("root_path", "")
        status = 500

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        gauge = self.in_progress.labels(method=method)
        gauge.inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            elapsed = time.perf_counter() - started
            gauge.dec()
            endpoint = route_template(scope, root_path)
            self.requests.labels(method=method, endpoint=endpoint, status=str(status)).inc()
            self.latency.labels(method=method, endpoint=endpoint).observe(elapsed)


class MetricsPlugin(Plugin):
    meta = PluginMeta(
        name="metrics",
        version="0.1.0",
        description="Prometheus RED metrics and /metrics endpoint.",
        after=("observability",),
        provides=("metrics.registry",),
        default_enabled=True,
        extra="jfastframework[metrics]",
    )
    Settings = MetricsSettings

    def register(self, ctx: AppContext) -> None:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
        )
        from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST
        from starlette.responses import PlainTextResponse

        registry = CollectorRegistry()
        labels = {"service": ctx.settings.app_name}

        requests = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
            registry=registry,
        )
        latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency",
            ["method", "endpoint"],
            registry=registry,
        )
        in_progress = Gauge(
            "http_requests_in_progress",
            "In-flight HTTP requests",
            ["method"],
            registry=registry,
        )
        info = Gauge("service_info", "Service metadata", ["service", "version"], registry=registry)
        info.labels(service=labels["service"], version=ctx.settings.version).set(1)

        ctx.provide("metrics.registry", registry)
        ctx.app.add_middleware(
            PrometheusMiddleware,
            requests=requests,
            latency=latency,
            in_progress=in_progress,
        )

        path = self.settings.path

        @ctx.app.get(path, include_in_schema=False)
        async def metrics_endpoint() -> PlainTextResponse:
            return PlainTextResponse(
                generate_latest(registry).decode("utf-8"),
                media_type=CONTENT_TYPE_LATEST.split(";")[0],
            )

    async def health(self, ctx: AppContext) -> HealthReport:
        return HealthReport.ok("metrics exposed", path=self.settings.path)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        if not self.settings.include_infra:
            return []
        return [
            InfraService(
                name="prometheus",
                image="prom/prometheus:v2.55.0",
                port_offset=self.settings.prometheus_port_offset,
                internal_port=9090,
                volumes=["./prometheus.yml:/etc/prometheus/prometheus.yml:ro"],
            ),
            InfraService(
                name="grafana",
                image="grafana/grafana:11.3.0",
                port_offset=self.settings.grafana_port_offset,
                internal_port=3000,
                environment={"GF_AUTH_ANONYMOUS_ENABLED": "true"},
                depends_on=["prometheus"],
            ),
        ]
