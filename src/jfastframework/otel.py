"""OpenTelemetry behind :mod:`jfastframework.tracing`.

The ``telemetry`` plugin builds these; nothing else in the framework imports
this module, so a service without the ``telemetry`` extra never loads
OpenTelemetry at all. Four pieces:

* :class:`OtelBackend` -- the real :class:`~jfastframework.tracing.TracingBackend`.
  Every ``tracing.span``/``inject``/``attach`` in the framework goes through it.
* :class:`ServerSpanMiddleware` -- one server span per HTTP request, continuing
  the caller's ``traceparent``.
* :class:`SqlSpans` -- one client span per SQL statement, from SQLAlchemy's
  engine events.
* :class:`TrackingExporter` -- wraps the real exporter so ``/ready`` can say
  whether spans are leaving the process.

The rule every piece keeps: attributes are ids, names, counts and durations.
Never a prompt, a document, an answer, a request body or a bound SQL parameter;
and nothing here may raise into the request it is observing.

Requires: ``pip install jfastframework[telemetry]``
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jfastframework.plugins.builtin.metrics import route_template

logger = logging.getLogger("jfast.telemetry")

#: W3C ``traceparent`` + ``tracestate`` and nothing else. Explicit rather than
#: the process-global propagator: another library configuring baggage or B3
#: globally must not change what crosses between our services.
PROPAGATOR = TraceContextTextMapPropagator()

#: A reserved attribute a ``tracing.span`` call site may pass to say what kind
#: of span it is (``span.kind="client"`` around an outbound call,
#: ``"consumer"`` around a job). The backend reads it and does not record it.
KIND_ATTRIBUTE = "span.kind"
_KINDS = {
    "internal": SpanKind.INTERNAL,
    "server": SpanKind.SERVER,
    "client": SpanKind.CLIENT,
    "producer": SpanKind.PRODUCER,
    "consumer": SpanKind.CONSUMER,
}
_SCALARS = (str, bool, int, float)

#: Longest string attribute kept. An id or a name is far shorter; anything
#: longer is more likely content that should not be here at all.
MAX_ATTRIBUTE_LENGTH = 256


def clean_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Only what OpenTelemetry can record, and only small.

    ``None`` is dropped (not an error: "no tenant" is a normal state), a
    non-scalar is dropped rather than ``str()``-ed -- the string form of a dict
    is exactly how a document ends up in a trace -- and long strings are cut.
    """
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None or key == KIND_ATTRIBUTE:
            continue
        if isinstance(value, str):
            cleaned[key] = value[:MAX_ATTRIBUTE_LENGTH]
        elif isinstance(value, _SCALARS):
            cleaned[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, _SCALARS) for v in value):
            cleaned[key] = [v[:MAX_ATTRIBUTE_LENGTH] if isinstance(v, str) else v for v in value]
    return cleaned


def _kind(attributes: Mapping[str, Any]) -> SpanKind:
    requested = attributes.get(KIND_ATTRIBUTE)
    return _KINDS.get(str(requested).lower(), SpanKind.INTERNAL) if requested else SpanKind.INTERNAL


def record_failure(span: Any, exc: BaseException) -> None:
    """Mark a span failed. Cancellation is not a failure, so it is not one here."""
    if not isinstance(exc, Exception):
        return
    try:
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
    except Exception:  # telemetry must never break the caller
        logger.debug("could not record an exception on a span", exc_info=True)


class OtelBackend:
    """:class:`jfastframework.tracing.TracingBackend` over an OpenTelemetry tracer."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def inject(self) -> dict[str, str]:
        carrier: dict[str, str] = {}
        PROPAGATOR.inject(carrier)
        return carrier

    @contextmanager
    def attach(self, carrier: Mapping[str, str]) -> Iterator[None]:
        token = None
        try:
            token = otel_context.attach(PROPAGATOR.extract(dict(carrier)))
        except Exception:
            logger.debug("could not attach a trace context", exc_info=True)
        try:
            yield
        finally:
            if token is not None:
                _detach(token)

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, Any]) -> Iterator[None]:
        # Started by hand rather than with ``start_as_current_span`` so that a
        # failure to start is caught here: ``tracing.span`` guards building the
        # context manager, not entering it.
        span = token = None
        try:
            span = self.tracer.start_span(
                name, kind=_kind(attributes), attributes=clean_attributes(attributes)
            )
            token = otel_context.attach(trace.set_span_in_context(span))
        except Exception:
            logger.debug("could not start span %s", name, exc_info=True)
        try:
            yield
        except BaseException as exc:
            if span is not None:
                record_failure(span, exc)
            raise
        finally:
            if token is not None:
                _detach(token)
            if span is not None:
                _end(span)

    def annotate(self, attributes: Mapping[str, Any]) -> None:
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attributes(clean_attributes(attributes))


def _detach(token: Any) -> None:
    try:
        otel_context.detach(token)
    except Exception:
        logger.debug("could not detach a trace context", exc_info=True)


def _end(span: Any) -> None:
    try:
        span.end()
    except Exception:
        logger.debug("could not end a span", exc_info=True)


# -- HTTP server spans ------------------------------------------------------

_TRACEPARENT = b"traceparent"
_TRACESTATE = b"tracestate"


def _incoming_carrier(scope: Scope) -> dict[str, str]:
    carrier: dict[str, str] = {}
    for name, value in scope.get("headers", ()):
        if name == _TRACEPARENT:
            carrier["traceparent"] = value.decode("latin-1")
        elif name == _TRACESTATE:
            carrier["tracestate"] = value.decode("latin-1")
    return carrier


class ServerSpanMiddleware:
    """One ``SERVER`` span per HTTP request, named after its route template.

    Plain ASGI, like every middleware in the framework. The span is started
    before the app runs, so everything inside -- SQL, outbound calls, model
    calls -- is its child; its name and the route, status, tenant and request
    id are filled in after, because none of them is known before the router
    and the tenancy middleware have run.

    ``holder.tracer`` is None until the plugin's startup has built the
    provider, and after its shutdown: then this is a pass-through.
    """

    def __init__(self, app: ASGIApp, *, holder: Any, exclude_paths: Sequence[str] = ()) -> None:
        self.app = app
        self.holder = holder
        self.exclude = frozenset(exclude_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        tracer = self.holder.tracer
        if scope["type"] != "http" or tracer is None or scope.get("path") in self.exclude:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        root_path = scope.get("root_path", "")
        span = token = None
        try:
            carrier = _incoming_carrier(scope)
            parent = PROPAGATOR.extract(carrier) if carrier else None
            span = tracer.start_span(
                method,
                context=parent,
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "url.scheme": scope.get("scheme", "")},
            )
            token = otel_context.attach(trace.set_span_in_context(span, parent))
        except Exception:  # a broken tracer serves the request untraced
            logger.debug("could not start a server span", exc_info=True)

        status: int | None = None

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        except BaseException as exc:
            if span is not None:
                record_failure(span, exc)
                if status is None and isinstance(exc, Exception):
                    # The error handler outside this middleware answers 500.
                    status = 500
            raise
        finally:
            if token is not None:
                _detach(token)
            if span is not None:
                self._finish(span, scope, method, root_path, status)

    @staticmethod
    def _finish(
        span: Any,
        scope: Scope,
        method: str,
        root_path: str,
        status: int | None,
    ) -> None:
        try:
            route = route_template(scope, root_path)
            span.update_name(f"{method} {route}")
            state = scope.get("state") or {}
            attributes = clean_attributes(
                {
                    "http.route": route,
                    "http.response.status_code": status,
                    "jfast.request_id": state.get("request_id"),
                    "jfast.tenant_id": state.get("tenant_id"),
                }
            )
            span.set_attributes(attributes)
            if status is not None and status >= 500:
                span.set_status(Status(StatusCode.ERROR))
        except Exception:
            logger.debug("could not finish a server span", exc_info=True)
        _end(span)


# -- SQL spans --------------------------------------------------------------

_LEADING_COMMENT = re.compile(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*)*", re.S)
_OPERATION = re.compile(r"\w+")
_TABLE = re.compile(
    r"\b(?:from|into|update|join|table)\s+(?:only\s+)?(?:\"?\w+\"?\.)?\"?(\w+)\"?", re.I
)
#: How much of a statement is read to name its table: the first table is near
#: the start, and a bulk INSERT with ten thousand rows must not be scanned.
_SCAN = 512
#: Longest statement recorded when ``record_statement`` is on.
MAX_STATEMENT_LENGTH = 2048
_SPAN_KEY = "_jfast_span"


def describe_statement(statement: str) -> tuple[str, str | None]:
    """``("SELECT", "notes")`` for ``SELECT ... FROM notes ...``, cheaply.

    A best effort over the text, not a parser: a CTE names its first table, a
    statement with none (``SELECT 1``, ``BEGIN``) has no table.
    """
    head = statement[:_SCAN]
    start = _LEADING_COMMENT.match(head)
    offset = start.end() if start else 0
    operation = _OPERATION.match(head, offset)
    table = _TABLE.search(head, offset)
    return (
        operation.group(0).upper() if operation else "SQL",
        table.group(1) if table else None,
    )


class SqlSpans:
    """A ``CLIENT`` span per SQL statement, for every engine in the process.

    Listens on the ``Engine`` class rather than on one engine: the database
    plugin builds one per connection and one per tenant on demand, and a
    listener on the class sees the ones that do not exist yet. Under an
    ``AsyncEngine`` the events fire in the greenlet SQLAlchemy runs the driver
    in, which carries the caller's context, so the span's parent is the
    request's span.

    Bound parameters are never read. The statement text is recorded only when
    asked for, because a statement written with literals in it carries them.
    """

    def __init__(self, tracer: Tracer, *, record_statement: bool = False) -> None:
        self.tracer = tracer
        self.record_statement = record_statement
        self._installed = False
        self._lock = threading.Lock()

    def install(self) -> None:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        with self._lock:
            if self._installed:
                return
            event.listen(Engine, "before_cursor_execute", self._before)
            event.listen(Engine, "after_cursor_execute", self._after)
            event.listen(Engine, "handle_error", self._error)
            self._installed = True

    def remove(self) -> None:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        with self._lock:
            if not self._installed:
                return
            event.remove(Engine, "before_cursor_execute", self._before)
            event.remove(Engine, "after_cursor_execute", self._after)
            event.remove(Engine, "handle_error", self._error)
            self._installed = False

    def _before(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if context is None:
            return
        try:
            operation, table = describe_statement(statement)
            attributes: dict[str, Any] = {
                "db.system": conn.dialect.name,
                "db.operation": operation,
            }
            database = conn.engine.url.database
            if database:
                attributes["db.name"] = database
            if table:
                attributes["db.sql.table"] = table
            if executemany:
                attributes["db.executemany"] = True
            if self.record_statement:
                attributes["db.statement"] = statement[:MAX_STATEMENT_LENGTH]
            span = self.tracer.start_span(
                f"{operation} {table}" if table else operation,
                kind=SpanKind.CLIENT,
                attributes=attributes,
            )
            setattr(context, _SPAN_KEY, span)
        except Exception:  # a statement is never refused for its span
            logger.debug("could not start a SQL span", exc_info=True)

    def _after(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        span = getattr(context, _SPAN_KEY, None)
        if span is None:
            return
        setattr(context, _SPAN_KEY, None)
        try:
            rowcount = getattr(cursor, "rowcount", -1)
            if isinstance(rowcount, int) and rowcount >= 0:
                span.set_attribute("db.rows_affected", rowcount)
        except Exception:
            logger.debug("could not read a rowcount", exc_info=True)
        _end(span)

    def _error(self, exception_context: Any) -> None:
        context = getattr(exception_context, "execution_context", None)
        span = getattr(context, _SPAN_KEY, None) if context is not None else None
        if span is None:
            return
        setattr(context, _SPAN_KEY, None)
        exc = getattr(exception_context, "original_exception", None)
        if isinstance(exc, BaseException):
            # The driver's message can quote the failing value; the type is
            # what a trace needs, and the logs have the rest.
            try:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            except Exception:
                logger.debug("could not mark a SQL span failed", exc_info=True)
        _end(span)


# -- export -----------------------------------------------------------------


class TrackingExporter(SpanExporter):
    """The real exporter, plus what ``/ready`` needs to know about it.

    Runs in the batch processor's thread: counters only, no locks worth the
    name -- a slightly stale count on ``/ready`` is fine, a blocked exporter
    thread is not.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner
        self.exported = 0
        self.failed_batches = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self.inner.export(spans)
        except Exception as exc:  # noqa: BLE001 - the processor would only log it
            self.last_error = type(exc).__name__
            result = SpanExportResult.FAILURE
        else:
            if result is not SpanExportResult.SUCCESS:
                # The OTLP exporter has already logged why, with the status.
                self.last_error = "the exporter reported a failure; see the jfast log"
        if result is SpanExportResult.SUCCESS:
            self.exported += len(spans)
            self.consecutive_failures = 0
            self.last_error = None
        else:
            self.failed_batches += 1
            self.consecutive_failures += 1
        return result

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return bool(self.inner.force_flush(timeout_millis))


__all__ = [
    "KIND_ATTRIBUTE",
    "PROPAGATOR",
    "OtelBackend",
    "ServerSpanMiddleware",
    "SqlSpans",
    "TrackingExporter",
    "clean_attributes",
    "describe_statement",
]
