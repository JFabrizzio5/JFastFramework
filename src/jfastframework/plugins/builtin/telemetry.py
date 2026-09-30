"""OpenTelemetry traces: one request followed through every service it touches.

    [plugins]
    enabled = ["observability", "database", "http", "telemetry"]

    # .env -- the only line needed to start exporting
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

**Free until configured.** With no endpoint the plugin installs nothing: no
middleware, no SQL listener, no tracer. Every ``tracing.span`` in the framework
stays the no-op it is without this plugin, and one INFO line at startup says
so. Setting the endpoint is what turns it on.

What is traced once it is on: each HTTP request (named by its route template,
with tenant and request id), each SQL statement (operation and table, never the
parameters), each call through the ``http`` client and the ``gateway`` (with
W3C ``traceparent`` sent on, so the next service continues the same trace),
each ``llm`` call and ``rag`` search (model, tokens, cost, hit counts), and
each job and event handler, which run inside the trace of the request that
created them. **Never prompt text, documents, answers or request bodies.**

    [plugin.telemetry]
    sample_ratio = 0.1            # keep one trace in ten; children follow the parent
    record_sql_statement = false  # statement text; literals in it would be recorded
    include_infra = true          # OpenTelemetry Collector + Jaeger in the compose file

Requires: ``pip install jfastframework[telemetry]``
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from jfastframework import tracing
from jfastframework.errors import PluginError
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext

#: What an OTLP/HTTP exporter appends to a base endpoint.
TRACES_PATH = "/v1/traces"

COLLECTOR_IMAGE = "otel/opentelemetry-collector:0.136.0"
JAEGER_IMAGE = "jaegertracing/jaeger:2.10.0"

#: The collector's configuration, inline: a generated compose file should not
#: depend on a config file nothing generates. OTLP in on both protocols, a
#: batch, OTLP out to Jaeger. Each ``--config=yaml:`` is merged into the last.
COLLECTOR_COMMAND = " ".join(
    f"'--config=yaml:{line}'"
    for line in (
        "receivers::otlp::protocols::http::endpoint: 0.0.0.0:4318",
        "receivers::otlp::protocols::grpc::endpoint: 0.0.0.0:4317",
        "processors::batch::timeout: 1s",
        "exporters::otlp/jaeger::endpoint: jaeger:4317",
        "exporters::otlp/jaeger::tls::insecure: true",
        "service::pipelines::traces::receivers: [otlp]",
        "service::pipelines::traces::processors: [batch]",
        "service::pipelines::traces::exporters: [otlp/jaeger]",
    )
)


class TelemetrySettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_TELEMETRY_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # The OTLP/HTTP base URL (``/v1/traces`` is appended). Read from
    # OTEL_EXPORTER_OTLP_ENDPOINT, the variable every OpenTelemetry SDK
    # reads, so an existing platform configuration just works -- including
    # from the .env file, which OpenTelemetry's own SDK would not read.
    endpoint: str | None = Field(
        default=None,
        # Environment names only: with the bare field name as an alias, any
        # variable called ENDPOINT would switch exporting on. jfast.toml's
        # ``endpoint = ...`` still works through ``populate_by_name``.
        validation_alias=AliasChoices("JFAST_TELEMETRY_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    # A full URL for traces only, as OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    # means it. Wins over ``endpoint``.
    traces_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "JFAST_TELEMETRY_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
        ),
    )
    # Sent with every export: an API key for Honeycomb, Grafana Cloud, Datadog.
    # Put it in the environment (JFAST_TELEMETRY_HEADERS='{"x-honeycomb-team":
    # "..."}'), not in jfast.toml. OTEL_EXPORTER_OTLP_HEADERS also works.
    headers: dict[str, str] = Field(default_factory=dict)
    # ``otlp`` exports to the endpoint. ``memory`` keeps finished spans in the
    # process for tests (``ctx.require("telemetry").exporter``); ``console``
    # prints them. Both of those are on without an endpoint: asking for them
    # is the configuration.
    exporter: Literal["otlp", "memory", "console"] = "otlp"
    # Fraction of new traces kept, 0.0-1.0. A request that arrives with a
    # sampled traceparent is always kept, so a trace is never half-recorded.
    sample_ratio: float = Field(1.0, ge=0.0, le=1.0)
    # One span per SQL statement when the database plugin is loaded.
    sql: bool = True
    # The statement text on SQL spans. Off: a statement built with literals
    # rather than bound parameters carries them, and bound parameters are
    # never recorded either way.
    record_sql_statement: bool = False
    # Probes and scrapes: several a second, forever, and nobody reads them.
    exclude_paths: list[str] = Field(default_factory=lambda: ["/health", "/ready", "/metrics"])
    # Seconds a single export may take, and what shutdown waits to flush.
    export_timeout: float = Field(10.0, gt=0)
    shutdown_timeout: float = Field(5.0, gt=0)
    # Make this the process's global OpenTelemetry tracer provider, so spans
    # your own code creates with ``trace.get_tracer(__name__)`` join the same
    # traces. Only when nothing else set one first.
    set_global_provider: bool = True
    # Emit an OpenTelemetry Collector and Jaeger when generating deploys. One
    # of each for a whole workspace: every service names the same containers.
    include_infra: bool = False
    collector_host_port: int = 4318
    jaeger_ui_host_port: int = 16686

    def traces_url(self) -> str | None:
        if self.traces_endpoint:
            return self.traces_endpoint
        if not self.endpoint:
            return None
        base = self.endpoint.rstrip("/")
        return base if base.endswith(TRACES_PATH) else base + TRACES_PATH


class TelemetryState:
    """What the plugin publishes as ``telemetry``.

    ``tracer`` and ``provider`` exist between startup and shutdown; before and
    after, the middleware reads ``tracer is None`` and passes requests through.
    ``exporter`` is the configured exporter itself -- with ``exporter =
    "memory"``, ``exporter.get_finished_spans()`` is what a test asserts on.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.tracer: Any = None
        self.provider: Any = None
        self.exporter: Any = None
        self.tracking: Any = None
        self.backend: Any = None
        self.sql: Any = None
        self.target: str | None = None


def _redacted(url: str) -> str:
    """Scheme, host and path only: a URL's user-info or query can hold a key."""
    parts = urlsplit(url)
    if not parts.scheme:
        return url  # "memory", "console"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


class TelemetryPlugin(Plugin):
    meta = PluginMeta(
        name="telemetry",
        version="0.1.0",
        description="OpenTelemetry traces across requests, SQL, HTTP calls, jobs and AI calls.",
        # Registered after every plugin that adds a middleware, so the server
        # span is the outermost one and everything they do is inside it.
        after=(
            "observability",
            "metrics",
            "sentry",
            "database",
            "cache",
            "tenancy",
            "auth",
            "accounts",
            "ratelimit",
            "idempotency",
            "http",
            "gateway",
            "web",
            "websocket",
            "llm",
            "rag",
        ),
        provides=("telemetry",),
        default_enabled=False,
        extra="jfastframework[telemetry]",
        # Traces are for people; a collector being down must never take the
        # service out of rotation.
        health_critical=False,
    )
    Settings = TelemetrySettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.state = TelemetryState()

    def register(self, ctx: AppContext) -> None:
        settings: TelemetrySettings = self.settings
        ctx.provide("telemetry", self.state)
        target = settings.traces_url() if settings.exporter == "otlp" else settings.exporter
        if target is None:
            ctx.logger.info(
                "telemetry: no OTEL_EXPORTER_OTLP_ENDPOINT, so no traces are recorded or "
                "exported (the plugin costs nothing until one is set)"
            )
            return

        try:
            from jfastframework.otel import ServerSpanMiddleware
        except ModuleNotFoundError as exc:
            raise PluginError(
                f"telemetry is configured to export to {_redacted(target)} but OpenTelemetry "
                f'is not installed ({exc.name}): pip install "jfastframework[telemetry]"'
            ) from exc

        self.state.enabled = True
        self.state.target = target
        self.state.exporter = self._build_exporter(settings, target)
        ctx.app.add_middleware(
            ServerSpanMiddleware, holder=self.state, exclude_paths=settings.exclude_paths
        )

    @staticmethod
    def _build_exporter(settings: TelemetrySettings, target: str) -> Any:
        """Built at register because it opens nothing; the processor waits for startup."""
        if settings.exporter == "memory":
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                InMemorySpanExporter,
            )

            return InMemorySpanExporter()
        if settings.exporter == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            return ConsoleSpanExporter()

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(
            endpoint=target,
            # None lets OTEL_EXPORTER_OTLP_HEADERS apply, as the SDK documents.
            headers=dict(settings.headers) or None,
            timeout=settings.export_timeout,
        )

    async def startup(self, ctx: AppContext) -> None:
        if not self.state.enabled:
            return
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        from jfastframework.otel import OtelBackend, SqlSpans, TrackingExporter

        settings: TelemetrySettings = self.settings
        attributes: dict[str, Any] = {
            "service.version": ctx.settings.version,
            "deployment.environment.name": ctx.settings.env,
        }
        # OTEL_SERVICE_NAME, when the platform sets it, is the more deliberate
        # answer; otherwise the service is called what jfast.toml calls it.
        if not os.environ.get("OTEL_SERVICE_NAME"):
            attributes["service.name"] = ctx.settings.app_name
        tracking = TrackingExporter(self.state.exporter)
        provider = TracerProvider(
            resource=Resource.create(attributes),
            sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio)),
        )
        if settings.exporter == "otlp":
            processor: Any = BatchSpanProcessor(
                tracking, export_timeout_millis=settings.export_timeout * 1000
            )
        else:
            # Synchronous, so a test sees a span the moment it ends.
            processor = SimpleSpanProcessor(tracking)
        provider.add_span_processor(processor)
        tracer = provider.get_tracer("jfastframework")

        if settings.set_global_provider and isinstance(
            trace.get_tracer_provider(), trace.ProxyTracerProvider
        ):
            trace.set_tracer_provider(provider)

        self.state.provider = provider
        self.state.tracking = tracking
        self.state.tracer = tracer
        self.state.backend = OtelBackend(tracer)
        tracing.set_backend(self.state.backend)

        if settings.sql and (ctx.has("db.engine") or ctx.has("db.databases")):
            self.state.sql = SqlSpans(tracer, record_statement=settings.record_sql_statement)
            self.state.sql.install()

        ctx.logger.info(
            "telemetry: exporting traces to %s (sample ratio %s)",
            _redacted(self.state.target or ""),
            settings.sample_ratio,
        )

    async def shutdown(self, ctx: AppContext) -> None:
        import asyncio

        state = self.state
        if state.sql is not None:
            state.sql.remove()
            state.sql = None
        # Only if it is still ours: in a process holding two apps (a test, a
        # gateway beside a service) the other may have installed its own since.
        if state.backend is not None and tracing._backend is state.backend:
            tracing.reset_backend()
        state.tracer = None
        state.backend = None
        provider, state.provider = state.provider, None
        if provider is None:
            return
        timeout_ms = int(self.settings.shutdown_timeout * 1000)

        def flush() -> None:
            # Off the event loop: flushing is a blocking HTTP call to the
            # collector, and a slow collector must not stall the other
            # plugins' shutdown behind it.
            provider.force_flush(timeout_ms)
            provider.shutdown()

        try:
            await asyncio.wait_for(
                asyncio.to_thread(flush), timeout=self.settings.shutdown_timeout + 1
            )
        except Exception:  # noqa: BLE001 - spans lost at exit are not worth a failed stop
            ctx.logger.warning(
                "telemetry: could not flush the last spans within %ss",
                self.settings.shutdown_timeout,
            )

    async def health(self, ctx: AppContext) -> HealthReport:
        state = self.state
        if not state.enabled:
            return HealthReport.ok(
                "not exporting: set OTEL_EXPORTER_OTLP_ENDPOINT to send traces", exporting=False
            )
        target = _redacted(state.target or "")
        tracking = state.tracking
        if tracking is None:
            return HealthReport.fail("telemetry not started", critical=False, target=target)
        if tracking.consecutive_failures:
            return HealthReport.fail(
                f"span export failing ({tracking.last_error}); spans are being dropped",
                critical=False,
                target=target,
                exported=tracking.exported,
                failed_batches=tracking.failed_batches,
            )
        return HealthReport.ok(
            f"exporting to {target}",
            exporting=True,
            target=target,
            exported=tracking.exported,
            failed_batches=tracking.failed_batches,
            sample_ratio=self.settings.sample_ratio,
        )

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: TelemetrySettings = self.settings
        if not settings.include_infra:
            return []
        # Fixed host ports rather than offsets in the service's block: there is
        # one collector for the whole workspace, not one per service, and the
        # block has no two offsets left. port_offset only has to be in range.
        return [
            InfraService(
                name="jaeger",
                image=JAEGER_IMAGE,
                port_offset=9,
                host_port=settings.jaeger_ui_host_port,
                internal_port=16686,
            ),
            InfraService(
                name="otel-collector",
                image=COLLECTOR_IMAGE,
                port_offset=9,
                host_port=settings.collector_host_port,
                internal_port=4318,
                command=COLLECTOR_COMMAND,
                depends_on=["jaeger"],
                client_env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318"},
            ),
        ]
