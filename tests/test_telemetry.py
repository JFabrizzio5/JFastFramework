"""The telemetry plugin: free until configured, one trace across every hop, no content.

Spans are read from the SDK's in-memory exporter (``exporter = "memory"``),
the same switch a service's own tests use. The SQL tests need PostgreSQL at
``JFAST_TEST_PG_URL`` and skip without it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import text

from jfastframework import tracing
from jfastframework.http.client import ServiceClient, Upstream
from jfastframework.llm import LLMClient
from jfastframework.otel import OtelBackend, ServerSpanMiddleware, describe_statement
from jfastframework.plugins.builtin.telemetry import TelemetryPlugin, TelemetrySettings
from jfastframework.rag import RagService
from jfastframework.testing import build_test_app, client_for
from jfastframework.vectors.base import Chunk, SearchHit

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
PG_DSN = f"{PG_BASE}/postgres"

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"

OTEL_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "JFAST_TELEMETRY_ENDPOINT",
    "JFAST_TELEMETRY_TRACES_ENDPOINT",
    "JFAST_TELEMETRY_EXPORTER",
    "OTEL_SERVICE_NAME",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in OTEL_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    tracing.reset_backend()


def telemetry_config(**settings: Any) -> dict[str, Any]:
    # Not the global provider: a test process would keep the first one forever.
    return {"telemetry": {"exporter": "memory", "set_global_provider": False, **settings}}


def traced_app(
    *,
    routers: tuple[APIRouter, ...] = (),
    plugins: tuple[str, ...] = ("observability", "telemetry"),
    plugin_config: dict[str, Any] | None = None,
    app_name: str = "svc",
    **telemetry: Any,
) -> FastAPI:
    raw = {"plugin": {**telemetry_config(**telemetry), **(plugin_config or {})}}
    return build_test_app(plugins=plugins, routers=list(routers), raw=raw, app_name=app_name)


def spans_of(app: FastAPI) -> list[ReadableSpan]:
    return list(app.state.jfast.require("telemetry").exporter.get_finished_spans())


def one(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    matching = [s for s in spans if s.name == name]
    assert len(matching) == 1, [s.name for s in spans]
    return matching[0]


def all_attribute_values(spans: list[ReadableSpan]) -> str:
    """Every attribute value and event attribute value, as one string to search."""
    values: list[str] = []
    for span in spans:
        values.extend(str(v) for v in (span.attributes or {}).values())
        for event in span.events:
            values.extend(str(v) for v in (event.attributes or {}).values())
        values.append(span.name)
    return "\n".join(values)


@asynccontextmanager
async def lenient_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Like ``client_for``, but a crashing route answers 500 instead of raising."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


def users_router() -> APIRouter:
    router = APIRouter()

    @router.get("/users/{user_id}")
    async def user(user_id: int) -> dict[str, int]:
        return {"id": user_id}

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    return router


# -- free until configured --------------------------------------------------


async def test_without_an_endpoint_nothing_is_installed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    # Without observability, which replaces the root handlers caplog reads.
    app = build_test_app(plugins=["telemetry"], routers=[users_router()])
    async with client_for(app) as client:
        assert (await client.get("/users/1")).status_code == 200
        assert not tracing.enabled()
        ready = (await client.get("/ready")).json()

    assert not any(m.cls is ServerSpanMiddleware for m in app.user_middleware)
    state = app.state.jfast.require("telemetry")
    assert state.tracer is None and state.provider is None
    assert "no OTEL_EXPORTER_OTLP_ENDPOINT" in caplog.text
    report = ready["checks"]["telemetry"] if "checks" in ready else ready
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in str(report)


def test_the_endpoint_comes_from_the_standard_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
    assert TelemetrySettings().traces_url() == "http://collector:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://other:4318/custom")
    assert TelemetrySettings().traces_url() == "http://other:4318/custom"
    # jfast.toml wins over the environment, as for every plugin.
    assert TelemetrySettings(traces_endpoint="http://toml/x").traces_url() == "http://toml/x"


def test_a_variable_merely_called_endpoint_does_not_switch_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENDPOINT", "http://somewhere-else")
    assert TelemetrySettings().traces_url() is None


def test_an_endpoint_without_opentelemetry_names_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from jfastframework.errors import PluginError

    monkeypatch.setitem(sys.modules, "jfastframework.otel", None)
    with pytest.raises(PluginError, match=r"pip install \"jfastframework\[telemetry\]\""):
        build_test_app(
            plugins=["telemetry"],
            raw={"plugin": {"telemetry": {"endpoint": "http://collector:4318"}}},
        )


async def test_an_otlp_endpoint_builds_a_batch_exporter_and_reports_it() -> None:
    app = build_test_app(
        plugins=["telemetry"],
        raw={
            "plugin": {
                "telemetry": {
                    "endpoint": "http://user:secret@127.0.0.1:9/",
                    "set_global_provider": False,
                    "shutdown_timeout": 0.5,
                    "export_timeout": 0.5,
                }
            }
        },
    )
    async with client_for(app) as client:
        assert tracing.enabled()
        ready = (await client.get("/ready")).json()
    text_ = str(ready)
    assert "exporting to http://127.0.0.1:9/v1/traces" in text_
    # The user-info of the URL never reaches /ready.
    assert "secret" not in text_
    assert not tracing.enabled()


# -- server spans -----------------------------------------------------------


async def test_a_request_is_one_server_span_named_by_its_route() -> None:
    app = traced_app(routers=(users_router(),))
    async with client_for(app) as client:
        response = await client.get("/users/42", headers={"X-Tenant-ID": "acme"})
    assert response.status_code == 200

    span = one(spans_of(app), "GET /users/{user_id}")
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.SERVER
    assert attributes["http.route"] == "/users/{user_id}"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200
    assert attributes["jfast.tenant_id"] == "acme"
    assert attributes["jfast.request_id"] == response.headers["x-request-id"]
    # The raw path, with the id in it, is not a span attribute.
    assert "/users/42" not in all_attribute_values([span])
    resource = dict(span.resource.attributes)
    assert resource["service.name"] == "svc"
    assert resource["deployment.environment.name"] == "local"


async def test_an_incoming_traceparent_is_continued() -> None:
    app = traced_app(routers=(users_router(),))
    async with client_for(app) as client:
        await client.get("/users/1", headers={"traceparent": TRACEPARENT})
    span = one(spans_of(app), "GET /users/{user_id}")
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert span.parent is not None and format(span.parent.span_id, "016x") == PARENT_ID
    assert span.parent.is_remote


async def test_probes_are_not_traced() -> None:
    app = traced_app()
    async with client_for(app) as client:
        await client.get("/health")
        await client.get("/ready")
    assert spans_of(app) == []


async def test_an_exception_is_recorded_on_the_server_span() -> None:
    app = traced_app(routers=(users_router(),))
    async with lenient_client(app) as client:
        response = await client.get("/boom")
    assert response.status_code == 500

    span = one(spans_of(app), "GET /boom")
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes or {})["http.response.status_code"] == 500
    exception = next(e for e in span.events if e.name == "exception")
    assert (exception.attributes or {})["exception.type"] == "RuntimeError"


async def test_a_server_error_answer_marks_the_span_failed() -> None:
    router = APIRouter()

    @router.get("/unavailable")
    async def unavailable() -> None:
        from jfastframework.errors import ServiceUnavailableError

        raise ServiceUnavailableError("down")

    app = traced_app(routers=(router,))
    async with client_for(app) as client:
        assert (await client.get("/unavailable")).status_code == 503
    assert one(spans_of(app), "GET /unavailable").status.status_code is StatusCode.ERROR


async def test_sampling_drops_new_traces_but_keeps_a_sampled_parent() -> None:
    app = traced_app(routers=(users_router(),), sample_ratio=0.0)
    async with client_for(app) as client:
        await client.get("/users/1")
        await client.get("/users/2", headers={"traceparent": TRACEPARENT})
    spans = spans_of(app)
    assert len(spans) == 1
    assert format(spans[0].context.trace_id, "032x") == TRACE_ID


async def test_shutdown_puts_the_no_op_backend_back() -> None:
    app = traced_app()
    async with client_for(app):
        assert tracing.enabled()
    assert not tracing.enabled()
    assert app.state.jfast.require("telemetry").tracer is None


# -- failures never reach the request --------------------------------------


class _Exploding:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"telemetry backend broke in {name}")


async def test_a_broken_tracer_still_serves_the_request() -> None:
    app = traced_app(routers=(users_router(),))
    async with client_for(app) as client:
        app.state.jfast.require("telemetry").tracer = _Exploding()
        tracing.set_backend(_Exploding())
        assert (await client.get("/users/7")).json() == {"id": 7}
        assert tracing.inject() == {}
        with tracing.attach({"traceparent": TRACEPARENT}), tracing.span("x", a=1):
            tracing.annotate(b=2)


async def test_an_exporter_that_fails_is_reported_not_raised() -> None:
    class Failing(InMemorySpanExporter):
        def export(self, spans: Any) -> SpanExportResult:
            raise ConnectionError("collector is down")

    app = traced_app(routers=(users_router(),))
    app.state.jfast.require("telemetry").exporter = Failing()
    async with client_for(app) as client:
        assert (await client.get("/users/1")).status_code == 200
        ready = await client.get("/ready")
    # Not critical: a collector being down never takes the service out.
    assert ready.status_code == 200
    assert "span export failing (ConnectionError)" in ready.text


# -- SQL --------------------------------------------------------------------


async def _pg_reachable() -> bool:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "no server here"
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def postgres() -> None:
    if not asyncio.run(_pg_reachable()):
        pytest.skip(f"no PostgreSQL at {PG_BASE}")


def sql_router() -> APIRouter:
    from jfastframework.plugins.builtin.database import session_dependency

    router = APIRouter()

    @router.post("/notes")
    async def create(session: Any = Depends(session_dependency, scope="function")) -> dict[str, int]:
        await session.execute(
            text("create temporary table if not exists telemetry_notes (label text)")
        )
        await session.execute(
            text("insert into telemetry_notes (label) values (:label)"),
            {"label": "a-very-secret-parameter"},
        )
        rows = await session.execute(text("select count(*) from telemetry_notes"))
        return {"count": int(rows.scalar_one())}

    @router.get("/bad-sql")
    async def bad(session: Any = Depends(session_dependency, scope="function")) -> None:
        await session.execute(text("select * from no_such_table_here"))

    return router


@pytest.mark.usefixtures("postgres")
async def test_sql_statements_are_child_spans_without_their_parameters() -> None:
    app = traced_app(
        routers=(sql_router(),),
        plugins=("observability", "database", "telemetry"),
        plugin_config={"database": {"dsn": PG_DSN}},
    )
    async with client_for(app) as client:
        assert (await client.post("/notes")).json() == {"count": 1}

    spans = spans_of(app)
    server = one(spans, "POST /notes")
    insert = one(spans, "INSERT telemetry_notes")
    select = one(spans, "SELECT telemetry_notes")
    for span in (insert, select):
        assert span.kind is SpanKind.CLIENT
        assert span.parent is not None and span.parent.span_id == server.context.span_id
        assert span.context.trace_id == server.context.trace_id
        attributes = dict(span.attributes or {})
        assert attributes["db.system"] == "postgresql"
        assert attributes["db.sql.table"] == "telemetry_notes"
        assert "db.statement" not in attributes
        assert span.end_time is not None and span.start_time is not None
    assert dict(insert.attributes or {})["db.operation"] == "INSERT"
    assert "a-very-secret-parameter" not in all_attribute_values(spans)


@pytest.mark.usefixtures("postgres")
async def test_the_statement_text_is_recorded_only_when_asked() -> None:
    app = traced_app(
        routers=(sql_router(),),
        plugins=("observability", "database", "telemetry"),
        plugin_config={"database": {"dsn": PG_DSN}},
        record_sql_statement=True,
    )
    async with client_for(app) as client:
        await client.post("/notes")
    insert = one(spans_of(app), "INSERT telemetry_notes")
    statement = dict(insert.attributes or {})["db.statement"]
    assert "insert into telemetry_notes" in str(statement)
    # Placeholders, never the bound value.
    assert "a-very-secret-parameter" not in all_attribute_values(spans_of(app))


@pytest.mark.usefixtures("postgres")
async def test_a_failing_statement_marks_its_span_and_not_with_the_message() -> None:
    app = traced_app(
        routers=(sql_router(),),
        plugins=("observability", "database", "telemetry"),
        plugin_config={"database": {"dsn": PG_DSN}},
    )
    async with lenient_client(app) as client:
        assert (await client.get("/bad-sql")).status_code == 500
    failed = one(spans_of(app), "SELECT no_such_table_here")
    assert failed.status.status_code is StatusCode.ERROR
    assert "error.type" in dict(failed.attributes or {})


@pytest.mark.usefixtures("postgres")
async def test_shutdown_removes_the_sql_listener() -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    app = traced_app(
        plugins=("database", "telemetry"), plugin_config={"database": {"dsn": PG_DSN}}
    )
    async with client_for(app):
        sql = app.state.jfast.require("telemetry").sql
        assert event.contains(Engine, "before_cursor_execute", sql._before)
    assert not event.contains(Engine, "before_cursor_execute", sql._before)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SELECT a FROM notes WHERE id = $1", ("SELECT", "notes")),
        ('select * from "public"."notes"', ("SELECT", "notes")),
        ("INSERT INTO jobs (a) VALUES ($1)", ("INSERT", "jobs")),
        ("UPDATE accounts SET x = 1", ("UPDATE", "accounts")),
        ("DELETE FROM sessions", ("DELETE", "sessions")),
        ("/* jfast */ SELECT 1", ("SELECT", None)),
        ("BEGIN", ("BEGIN", None)),
        ("", ("SQL", None)),
    ],
)
def test_statements_are_described_cheaply(statement: str, expected: tuple[str, Any]) -> None:
    assert describe_statement(statement) == expected


# -- across services --------------------------------------------------------


def service_b() -> FastAPI:
    router = APIRouter()

    @router.get("/orders/{order_id}")
    async def order(order_id: int, request: Request) -> dict[str, Any]:
        return {"id": order_id, "traceparent": request.headers.get("traceparent")}

    return traced_app(routers=(router,), app_name="orders")


def service_a(b: FastAPI) -> FastAPI:
    router = APIRouter()
    client = ServiceClient(
        Upstream(name="orders", base_url="http://orders"), transport=ASGITransport(app=b)
    )

    @router.get("/checkout/{order_id}")
    async def checkout(order_id: int) -> dict[str, Any]:
        response = await client.get(f"/orders/{order_id}")
        return {"upstream": response.json()}

    return traced_app(routers=(router,), app_name="checkout")


async def test_one_trace_spans_two_services_through_the_http_client() -> None:
    b = service_b()
    a = service_a(b)
    async with client_for(b), client_for(a) as client:
        answer = (await client.get("/checkout/9", headers={"traceparent": TRACEPARENT})).json()

    spans = spans_of(a) + spans_of(b)
    a_server = one(spans, "GET /checkout/{order_id}")
    a_client = one(spans, "GET orders")
    b_server = one(spans, "GET /orders/{order_id}")

    assert {format(s.context.trace_id, "032x") for s in spans} == {TRACE_ID}
    assert a_client.kind is SpanKind.CLIENT
    assert a_client.parent is not None and a_client.parent.span_id == a_server.context.span_id
    # B's server span is the child of A's client span, not of A's server span.
    assert b_server.parent is not None and b_server.parent.span_id == a_client.context.span_id
    assert dict(a_client.attributes or {})["http.response.status_code"] == 200
    assert dict(a_client.attributes or {})["jfast.upstream"] == "orders"
    assert answer["upstream"]["traceparent"].split("-")[2] == format(
        a_client.context.span_id, "016x"
    )
    assert b_server.resource.attributes["service.name"] == "orders"
    assert a_server.resource.attributes["service.name"] == "checkout"


async def test_without_telemetry_the_client_sends_no_trace_headers() -> None:
    seen: dict[str, str] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    client = ServiceClient(
        Upstream(name="b", base_url="http://b"), transport=httpx.MockTransport(upstream)
    )
    await client.get("/x")
    assert "traceparent" not in seen


def gateway_pair() -> tuple[FastAPI, FastAPI]:
    upstream = service_b()
    gateway = traced_app(
        plugins=("observability", "gateway", "telemetry"),
        plugin_config={"gateway": {"routes": [{"prefix": "/shop", "target": "http://orders"}]}},
        app_name="gateway",
    )
    plugin = next(p for p in gateway.state.plugins if p.meta.name == "gateway")
    plugin._client = httpx.AsyncClient(transport=ASGITransport(app=upstream), timeout=2.0)
    return gateway, upstream


async def test_the_gateway_forwards_its_own_span_as_the_parent() -> None:
    gateway, upstream = gateway_pair()
    async with client_for(upstream), client_for(gateway) as client:
        answer = (await client.get("/shop/orders/3", headers={"traceparent": TRACEPARENT})).json()

    spans = spans_of(gateway) + spans_of(upstream)
    hop = one(spans, "GET /shop")
    served = one(spans, "GET /orders/{order_id}")
    assert {format(s.context.trace_id, "032x") for s in spans} == {TRACE_ID}
    assert hop.kind is SpanKind.CLIENT
    assert served.parent is not None and served.parent.span_id == hop.context.span_id
    # One traceparent reached the upstream, the gateway's, not the client's.
    assert answer["traceparent"].split("-")[2] == format(hop.context.span_id, "016x")


async def test_a_gateway_without_telemetry_relays_the_callers_traceparent() -> None:
    router = APIRouter()

    @router.get("/echo")
    async def echo(request: Request) -> dict[str, Any]:
        return {"traceparent": request.headers.getlist("traceparent")}

    upstream = build_test_app(routers=[router], app_name="up")
    gateway = build_test_app(
        plugins=["gateway"],
        raw={"plugin": {"gateway": {"routes": [{"prefix": "/up", "target": "http://up"}]}}},
    )
    plugin = next(p for p in gateway.state.plugins if p.meta.name == "gateway")
    plugin._client = httpx.AsyncClient(transport=ASGITransport(app=upstream), timeout=2.0)
    async with client_for(gateway) as client:
        answer = (await client.get("/up/echo", headers={"traceparent": TRACEPARENT})).json()
    assert answer == {"traceparent": [TRACEPARENT]}


# -- AI calls ---------------------------------------------------------------

SECRET_PROMPT = "the patient's diagnosis is confidential-7781"
SECRET_ANSWER = "treatment plan confidential-9932"
SECRET_DOCUMENT = "clause 4: the penalty is confidential-4410 per day of delay"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.set_backend(OtelBackend(provider.get_tracer("test")))
    return memory


def provider_transport(retry_once: bool = False) -> httpx.MockTransport:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if retry_once and calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        if request.url.path.endswith("/embeddings"):
            inputs = __import__("json").loads(request.content)["input"]
            return httpx.Response(
                200,
                json={
                    "data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(inputs))],
                    "usage": {"prompt_tokens": 12},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.4-mini",
                "choices": [{"message": {"content": SECRET_ANSWER}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 7},
            },
        )

    return httpx.MockTransport(handler)


async def test_a_chat_call_is_a_span_with_its_cost_and_never_its_content(
    exporter: InMemorySpanExporter,
) -> None:
    llm = LLMClient(api_key="k", transport=provider_transport(retry_once=True))
    result = await llm.chat(
        [{"role": "user", "content": SECRET_PROMPT}], tenant_id="acme", purpose="triage"
    )
    assert result.text == SECRET_ANSWER

    spans = list(exporter.get_finished_spans())
    span = one(spans, "llm.chat")
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.CLIENT
    assert attributes["llm.model"] == "gpt-5.4-mini"
    assert attributes["llm.purpose"] == "triage"
    assert attributes["jfast.tenant_id"] == "acme"
    assert attributes["llm.usage.input_tokens"] == 30
    assert attributes["llm.usage.output_tokens"] == 7
    assert attributes["llm.usd"] == result.usage.usd
    assert attributes["llm.ms"] >= 0
    assert attributes["llm.retries"] == 1
    everything = all_attribute_values(spans)
    assert "confidential" not in everything


async def test_a_refused_chat_records_the_error_not_the_prompt(
    exporter: InMemorySpanExporter,
) -> None:
    from jfastframework.llm import BudgetExceededError

    llm = LLMClient(api_key="k", budget_usd=0.000001, transport=provider_transport())
    with pytest.raises(BudgetExceededError):
        await llm.chat([{"role": "user", "content": SECRET_PROMPT}])
    span = one(list(exporter.get_finished_spans()), "llm.chat")
    assert span.status.status_code is StatusCode.ERROR
    assert "confidential" not in all_attribute_values([span])


class _Embedder:
    dimensions = 2
    model_id = "fake:2"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(t) % 3)] for t in texts]


class _Store:
    supports_hybrid = False

    def __init__(self) -> None:
        self.chunks: dict[int, Chunk] = {}

    async def existing_hashes(self, document_id: str, *, tenant_id: str | None) -> dict[int, str]:
        return {i: c.content_hash or "" for i, c in self.chunks.items()}

    async def sync_document(
        self, document_id: str, *, tenant_id: str | None, chunks: list[Chunk], embeddings: Any
    ) -> int:
        self.chunks = {c.chunk_index: c for c in chunks}
        return len(chunks)

    async def search(self, embedding: list[float], **options: Any) -> list[SearchHit]:
        return [SearchHit("doc", i, c.content, 0.9) for i, c in self.chunks.items()]

    async def delete_document(self, document_id: str, *, tenant_id: str | None) -> None:
        self.chunks = {}


async def test_rag_spans_count_chunks_and_hits_and_carry_no_text(
    exporter: InMemorySpanExporter,
) -> None:
    rag = RagService(_Store(), _Embedder(), chunk_size=40, chunk_overlap=0)  # type: ignore[arg-type]
    first = await rag.ingest("doc", SECRET_DOCUMENT, tenant_id="acme")
    await rag.ingest("doc", SECRET_DOCUMENT, tenant_id="acme")
    hits = await rag.search(SECRET_PROMPT, tenant_id="acme", limit=3)

    spans = list(exporter.get_finished_spans())
    ingests = [s for s in spans if s.name == "rag.ingest"]
    assert [dict(s.attributes or {})["rag.embedded"] for s in ingests] == [first.chunks, 0]
    assert dict(ingests[1].attributes or {})["rag.reused"] == first.chunks
    search = one(spans, "rag.search")
    attributes = dict(search.attributes or {})
    assert attributes["rag.hits"] == len(hits)
    assert attributes["rag.limit"] == 3
    assert attributes["rag.hybrid"] is False
    assert attributes["jfast.tenant_id"] == "acme"
    assert "confidential" not in all_attribute_values(spans)


async def test_an_embedding_call_inside_rag_is_its_child(exporter: InMemorySpanExporter) -> None:
    from jfastframework.llm import LLMEmbedder

    llm = LLMClient(api_key="k", transport=provider_transport())
    rag = RagService(_Store(), LLMEmbedder(llm, dimensions=2), chunk_size=40, chunk_overlap=0)  # type: ignore[arg-type]
    await rag.ingest("doc", SECRET_DOCUMENT, tenant_id="acme")

    spans = list(exporter.get_finished_spans())
    ingest = one(spans, "rag.ingest")
    embed = one(spans, "llm.embed")
    assert embed.parent is not None and embed.parent.span_id == ingest.context.span_id
    assert dict(embed.attributes or {})["llm.purpose"] == "rag-embed"
    assert dict(embed.attributes or {})["llm.usage.input_tokens"] == 12
    assert "confidential" not in all_attribute_values(spans)


# -- the contract ------------------------------------------------------------


def test_attach_and_inject_carry_the_context_across_a_boundary(
    exporter: InMemorySpanExporter,
) -> None:
    with tracing.attach({"traceparent": TRACEPARENT}):
        carried = tracing.inject()
        with tracing.span("job.run", **{"span.kind": "consumer", "job.task": "t"}):
            pass
    span = one(list(exporter.get_finished_spans()), "job.run")
    assert carried["traceparent"] == TRACEPARENT
    assert span.kind is SpanKind.CONSUMER
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert "span.kind" not in dict(span.attributes or {})


def test_attributes_that_are_not_small_scalars_are_dropped(
    exporter: InMemorySpanExporter,
) -> None:
    with tracing.span("x", ok="id-1", count=3, none=None, blob={"text": SECRET_DOCUMENT}):
        tracing.annotate(long="y" * 1000)
    attributes = dict(one(list(exporter.get_finished_spans()), "x").attributes or {})
    assert attributes["ok"] == "id-1" and attributes["count"] == 3
    assert "none" not in attributes and "blob" not in attributes
    assert len(str(attributes["long"])) == 256


def test_annotate_without_a_backend_is_free() -> None:
    tracing.reset_backend()
    tracing.annotate(a=1)  # nothing to annotate, nothing raised


# -- infra -------------------------------------------------------------------


def test_infra_is_only_emitted_on_request() -> None:
    assert TelemetryPlugin().infra() == []
    services = TelemetryPlugin({"include_infra": True}).infra()
    by_name = {s.name: s for s in services}
    assert set(by_name) == {"otel-collector", "jaeger"}
    assert by_name["otel-collector"].client_env == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318"
    }
    assert by_name["jaeger"].port_mappings(8000) == [(16686, 16686)]
    assert by_name["otel-collector"].port_mappings(8000) == [(4318, 4318)]


def test_the_compose_file_points_the_api_at_the_collector() -> None:
    from jfastframework.deploy.compose import build_compose, render_compose
    from jfastframework.testing import make_config

    config = make_config(plugins=["telemetry"])
    compose = build_compose(config, [TelemetryPlugin({"include_infra": True})])
    assert compose["services"]["api"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "http://otel-collector:4318"
    )
    assert compose["services"]["otel-collector"]["depends_on"] == ["jaeger"]
    rendered = render_compose(compose)
    assert "exporters::otlp/jaeger::endpoint: jaeger:4317" in rendered
