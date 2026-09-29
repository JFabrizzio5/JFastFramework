"""Structured logging and request correlation.

Default-enabled and dependency-free. Every log line carries the request id, so
a trace across services is one grep. The ``http`` plugin's client forwards
``X-Request-ID`` on every call; any other client has to forward it itself.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str | None] = ContextVar("jfast_request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("jfast_tenant_id", default=None)


def current_request_id() -> str | None:
    """Request id of the in-flight request, or None outside a request."""
    return request_id_var.get()


def current_tenant_id() -> str | None:
    return tenant_id_var.get()


class JsonFormatter(logging.Formatter):
    """Log records as one JSON object per line."""

    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "env": self.env,
        }
        if (rid := request_id_var.get()) is not None:
            payload["request_id"] = rid
        if (tid := tenant_id_var.get()) is not None:
            payload["tenant_id"] = tid
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything attached via logger.info("...", extra={"order_id": 1}).
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


_LOG_RECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign or propagate a request id and log one line per request."""

    def __init__(self, app: Any, *, logger: logging.Logger, tenant_header: str) -> None:
        super().__init__(app)
        self.logger = logger
        self.tenant_header = tenant_header

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex

        # The tenancy plugin, when enabled, is the authority on which tenant
        # this is: it can read a signed claim, which a header never is. This
        # middleware only fills the gap when nothing has resolved one, so a
        # header cannot quietly overwrite a tenant that came from a token.
        resolved = tenant_id_var.get()
        tenant_id = resolved if resolved is not None else request.headers.get(self.tenant_header)

        rid_token = request_id_var.set(request_id)
        tid_token = tenant_id_var.set(tenant_id)
        request.state.request_id = request_id
        if getattr(request.state, "tenant_id", None) is None:
            request.state.tenant_id = tenant_id

        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            self.logger.exception(
                "request failed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": round(elapsed, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(rid_token)
            tenant_id_var.reset(tid_token)

        elapsed = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        fields: dict[str, Any] = {
            "http_method": request.method,
            "http_path": request.url.path,
            "http_status": response.status_code,
            "duration_ms": round(elapsed, 2),
        }
        # Read the tenant from the request rather than from the context
        # variable: whichever middleware resolved it ran further in and has
        # already reset its own context by the time this line is written.
        if (resolved_tenant := getattr(request.state, "tenant_id", None)) is not None:
            fields["tenant_id"] = resolved_tenant
        self.logger.info("request", extra=fields)
        return response


class ObservabilitySettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_LOG_", env_file=".env", extra="ignore")

    level: str = "INFO"
    json_logs: bool = True
    access_log: bool = True
    tenant_header: str = "X-Tenant-ID"


class ObservabilityPlugin(Plugin):
    meta = PluginMeta(
        name="observability",
        version="0.1.0",
        description="Structured JSON logging with request-id and tenant correlation.",
        provides=("logger",),
        default_enabled=True,
    )
    Settings = ObservabilitySettings

    def register(self, ctx: AppContext) -> None:
        settings: ObservabilitySettings = self.settings
        root = logging.getLogger()
        root.setLevel(settings.level.upper())

        handler = logging.StreamHandler(sys.stdout)
        if settings.json_logs:
            handler.setFormatter(JsonFormatter(service=ctx.settings.app_name, env=ctx.settings.env))
        else:
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")
            )
        # Replace handlers rather than append: uvicorn installs its own and we
        # would otherwise emit every line twice.
        root.handlers = [handler]

        service_logger = logging.getLogger(ctx.settings.app_name)
        ctx.logger = service_logger
        ctx.provide("logger", service_logger)

        if settings.access_log:
            ctx.app.add_middleware(
                RequestContextMiddleware,
                logger=service_logger,
                tenant_header=settings.tenant_header,
            )

    async def health(self, ctx: AppContext) -> HealthReport:
        return HealthReport.ok("logging configured", level=self.settings.level)
