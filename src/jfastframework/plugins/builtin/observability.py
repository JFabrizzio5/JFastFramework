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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")

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


class RequestContextMiddleware:
    """Assign or propagate a request id and log one line per request.

    Plain ASGI rather than ``BaseHTTPMiddleware``, which runs the app in a
    task group and streams the response through a memory channel: about 75 us
    of CPU per request, per middleware, measured. This does the same work by
    wrapping ``send``.
    """

    def __init__(self, app: ASGIApp, *, logger: logging.Logger, tenant_header: str) -> None:
        self.app = app
        self.logger = logger
        self.tenant_header = tenant_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        raw_id = headers.get(REQUEST_ID_HEADER_BYTES)
        request_id = raw_id.decode("latin-1") if raw_id else uuid.uuid4().hex

        # The tenant is never taken from the header here. It used to fill the
        # gap when nothing had resolved one, and `current_tenant`, the RLS
        # session and every Job/Event built in the request trust that gap:
        # with `auth` on and `tenancy` off, an anonymous request carrying
        # `X-Tenant-ID: victim` was served as tenant `victim`. A tenant comes
        # from a signed token (`auth`) or from `tenancy`, where `header` is a
        # source someone chose. The header survives only as a log field,
        # `tenant_claimed`, labelled as what it is: unverified.
        tenant_id = tenant_id_var.get()
        raw_tenant = headers.get(self.tenant_header)

        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        if raw_tenant:
            state["tenant_claimed"] = raw_tenant.decode("latin-1")[:128]

        rid_token = request_id_var.set(request_id)
        tid_token = tenant_id_var.set(tenant_id)
        status = 500
        encoded_id = request_id.encode("latin-1")

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [
                    *[
                        (k, v)
                        for k, v in message["headers"]
                        if k.lower() != REQUEST_ID_HEADER_BYTES
                    ],
                    (REQUEST_ID_HEADER_BYTES, encoded_id),
                ]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            self.logger.exception(
                "request failed",
                extra={
                    "http_method": scope["method"],
                    "http_path": scope["path"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        finally:
            request_id_var.reset(rid_token)
            tenant_id_var.reset(tid_token)

        if not self.logger.isEnabledFor(logging.INFO):
            return
        fields: dict[str, Any] = {
            "http_method": scope["method"],
            "http_path": scope["path"],
            "http_status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        # Read the tenant from the request state rather than from the context
        # variable: whichever middleware resolved it ran further in and has
        # already reset its own context by the time this line is written.
        if (resolved_tenant := state.get("tenant_id")) is not None:
            fields["tenant_id"] = resolved_tenant
        elif (named := state.get("tenant_requested")) is not None:
            # A subdomain or path the request named and tenancy did not
            # grant: logged as what it is, like the header below.
            fields["tenant_claimed"] = named
        elif (claimed := state.get("tenant_claimed")) is not None:
            fields["tenant_claimed"] = claimed
        self.logger.info("request", extra=fields)


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
