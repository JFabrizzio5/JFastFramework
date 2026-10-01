"""Local domain events and module-owned tasks.

The field report behind this file: in a modular monolith on the default
stack, ``outbox.publish`` answered 201 and the event died after twenty
retries, because only Kafka could deliver it. Here an event reaches every
``@subscribe`` of this service as a queue job written in the publishing
transaction, runs as the publishing tenant inside its trace, and an event
nobody can receive is an error in the request that published it.

SQLite covers the bookkeeping; PostgreSQL covers what only a server can: the
jobs share the request's transaction, and the worker restores tenant and
trace and deduplicates on redelivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from jfastframework import tracing
from jfastframework.context import AppContext
from jfastframework.db.framework import ensure_tables
from jfastframework.errors import PluginError
from jfastframework.events import (
    Event,
    UndeliverableEvent,
    clear_subscribers,
    subscribe,
    subscribers_for,
)
from jfastframework.outbox import INBOX_TABLE, OUTBOX_TABLE, Outbox, OutboxRelay, outbox
from jfastframework.plugins.builtin.database import DbSession
from jfastframework.plugins.builtin.observability import request_id_var, tenant_id_var
from jfastframework.queues.base import Job, current_job
from jfastframework.queues.worker import TaskRegistry, Worker
from jfastframework.tasks import (
    TaskContext,
    TaskSession,
    clear_declared,
    declared_tasks,
    task,
    task_context_param,
)
from jfastframework.testing import build_test_app

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")


@pytest.fixture(autouse=True)
def _fresh_declarations() -> Iterator[None]:
    clear_subscribers()
    clear_declared()
    yield
    clear_subscribers()
    clear_declared()


@contextmanager
def _as_request(tenant: str | None, request_id: str | None) -> Iterator[None]:
    tenant_token = tenant_id_var.set(tenant)
    request_token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(request_token)
        tenant_id_var.reset(tenant_token)


class RecordingTracer:
    """A tracing backend that remembers what it was asked to carry."""

    def __init__(self, carrier: dict[str, str]) -> None:
        self.carrier = carrier
        self.attached: list[dict[str, str]] = []
        self.spans: list[tuple[str, dict[str, Any]]] = []

    def inject(self) -> dict[str, str]:
        return dict(self.carrier)

    def attach(self, carrier: Mapping[str, str]) -> Any:
        self.attached.append(dict(carrier))
        return nullcontext()

    def span(self, name: str, attributes: Mapping[str, Any]) -> Any:
        self.spans.append((name, dict(attributes)))
        return nullcontext()


@pytest.fixture
def tracer() -> Iterator[RecordingTracer]:
    backend = RecordingTracer({"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
    tracing.set_backend(backend)
    yield backend
    tracing.reset_backend()


# -- Event -------------------------------------------------------------------


def test_an_event_inherits_tenant_request_and_trace_like_a_job(tracer: RecordingTracer) -> None:
    with _as_request("acme", "req-1"):
        event = Event(type="receipt.registered")
        job = Job(task="t")
    assert (event.tenant_id, event.request_id) == ("acme", "req-1")
    assert event.trace == tracer.carrier
    assert (job.tenant_id, job.request_id, job.trace) == ("acme", "req-1", tracer.carrier)


def test_outside_a_request_an_event_carries_nothing() -> None:
    event = Event(type="receipt.registered")
    assert (event.tenant_id, event.request_id, event.trace) == (None, None, {})


def test_event_and_job_round_trip_their_trace(tracer: RecordingTracer) -> None:
    with _as_request("acme", "req-1"):
        event = Event(type="x", data={"n": 1}, key="k")
        job = Job(task="t", payload={"n": 1})
    rebuilt = Event.from_json(event.to_json())
    assert (rebuilt.id, rebuilt.trace, rebuilt.tenant_id, rebuilt.key) == (
        event.id,
        tracer.carrier,
        "acme",
        "k",
    )
    again = Job.from_json(job.to_json())
    assert (again.trace, again.tenant_id) == (tracer.carrier, "acme")


def test_rebuilding_an_event_never_takes_the_current_context() -> None:
    raw = Event(type="x").to_json()
    with _as_request("other", "req-2"):
        rebuilt = Event.from_json(raw)
    assert (rebuilt.tenant_id, rebuilt.request_id) == (None, None)


def test_the_kafka_plugin_still_exports_the_same_event() -> None:
    from jfastframework.plugins.builtin import events as kafka

    assert kafka.Event is Event


# -- @subscribe --------------------------------------------------------------


def test_subscribe_records_the_handler_and_whether_it_takes_a_session() -> None:
    @subscribe("receipt.registered")
    async def plain(event: Event) -> None: ...

    @subscribe("receipt.registered")
    async def with_session(event: Event, session: TaskSession) -> None: ...

    found = {s.name.rsplit(".", 1)[-1]: s for s in subscribers_for("receipt.registered")}
    assert found["plain"].session_param is None
    assert found["with_session"].session_param == "session"
    assert found["plain"].task.startswith("receipt.registered->")


def test_a_handler_asks_for_the_app_context_by_annotation() -> None:
    """``TaskContext`` is ``AppContext``; either spelling, real or as a string."""

    async def aliased(payload: dict[str, Any], ctx: TaskContext) -> None: ...

    async def direct(payload: dict[str, Any], app: AppContext) -> None: ...

    async def unresolvable(payload: dict[str, Any], c: NotImportedHere) -> None: ...  # type: ignore[name-defined]  # noqa: F821

    async def written_out(payload: dict[str, Any], c: jfastframework.tasks.TaskContext) -> None: ...  # type: ignore[name-defined]  # noqa: F821

    async def neither(payload: dict[str, Any], session: TaskSession) -> None: ...

    assert TaskContext is AppContext
    assert task_context_param(aliased) == "ctx"
    assert task_context_param(direct) == "app"
    assert task_context_param(unresolvable) is None
    assert task_context_param(written_out) == "c"
    assert task_context_param(neither) is None

    @task("billing.summarise")
    async def summarise(
        payload: dict[str, Any], session: TaskSession, ctx: TaskContext
    ) -> None: ...

    @subscribe("receipt.registered")
    async def react(event: Event, ctx: TaskContext) -> None: ...

    [spec] = declared_tasks()
    assert (spec.session_param, spec.context_param) == ("session", "ctx")
    [subscriber] = subscribers_for("receipt.registered")
    assert (subscriber.session_param, subscriber.context_param) == (None, "ctx")


def test_two_subscribers_with_one_name_are_refused() -> None:
    @subscribe("x", name="same")
    async def first(event: Event) -> None: ...

    with pytest.raises(ValueError, match="both named 'same'"):

        @subscribe("x", name="same")
        async def second(event: Event) -> None: ...


def test_a_subscriber_must_be_async() -> None:
    with pytest.raises(TypeError, match="async def"):

        @subscribe("x")
        def sync(event: Event) -> None: ...


def test_a_task_name_belongs_to_one_function() -> None:
    @task("billing.charge")
    async def charge(payload: dict[str, Any]) -> None: ...

    with pytest.raises(ValueError, match="declared twice"):

        @task("billing.charge")
        async def other(payload: dict[str, Any]) -> None: ...

    assert [spec.name for spec in declared_tasks()] == ["billing.charge"]


def test_a_malformed_schedule_fails_at_import() -> None:
    with pytest.raises(ValueError):
        task("broken", cron="not a cron")


# -- publish -----------------------------------------------------------------


@pytest.fixture
async def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await ensure_tables(engine, OUTBOX_TABLE, INBOX_TABLE)
    yield engine
    await engine.dispose()


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> str:
        self.jobs.append(job)
        return job.id


async def _rows(engine: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(outbox))).mappings().all())


async def test_an_event_nobody_can_receive_is_refused_in_the_request(engine: Any) -> None:
    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(UndeliverableEvent, match=re.escape('@subscribe("receipt.registered")')):
            await Outbox(queue=RecordingQueue()).publish(
                session, "receipts", Event(type="receipt.registered")
            )
    assert await _rows(engine) == []


async def test_subscribers_without_a_queue_name_the_missing_plugin(engine: Any) -> None:
    @subscribe("receipt.registered")
    async def react(event: Event) -> None: ...

    async with AsyncSession(engine) as session, session.begin():
        with pytest.raises(UndeliverableEvent, match=re.escape('"queue" to [plugins].enabled')):
            await Outbox().publish(session, "receipts", Event(type="receipt.registered"))


async def test_one_job_per_subscriber_and_no_event_row_without_a_bus(engine: Any) -> None:
    @subscribe("receipt.registered")
    async def first(event: Event) -> None: ...

    @subscribe("receipt.registered")
    async def second(event: Event) -> None: ...

    with _as_request("acme", "req-1"):
        event = Event(type="receipt.registered", data={"id": 9})
    async with AsyncSession(engine) as session, session.begin():
        await Outbox(queue=RecordingQueue()).publish(session, "receipts", event)

    rows = await _rows(engine)
    # Not the PostgreSQL queue, so the jobs travel through the outbox -- and
    # no event row, because there is no bus that could take one.
    assert sorted(r["kind"] for r in rows) == ["job", "job"]
    assert {r["tenant_id"] for r in rows} == {"acme"}
    tasks = {r["destination"] for r in rows}
    assert tasks == {s.task for s in subscribers_for("receipt.registered")}

    queue = RecordingQueue()
    await OutboxRelay(engine, queue=queue).relay_once()
    assert {job.payload["event"]["id"] for job in queue.jobs} == {event.id}
    assert {job.request_id for job in queue.jobs} == {"req-1"}


async def test_with_a_bus_the_event_also_goes_to_its_topic(engine: Any) -> None:
    @subscribe("receipt.registered")
    async def react(event: Event) -> None: ...

    async with AsyncSession(engine) as session, session.begin():
        await Outbox(queue=RecordingQueue(), events=object()).publish(
            session, "receipts", Event(type="receipt.registered")
        )
    kinds = sorted(r["kind"] for r in await _rows(engine))
    assert kinds == ["event", "job"]


async def test_an_event_row_with_no_bus_dies_at_once_and_says_why(
    engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # A row written by a version, or a configuration, that had a bus.
    async with AsyncSession(engine) as session, session.begin():
        await Outbox(events=object()).publish(session, "receipts", Event(type="x"))

    with caplog.at_level(logging.WARNING, logger="jfast.outbox"):
        await OutboxRelay(engine, max_attempts=20).relay_once()
    [row] = await _rows(engine)
    assert (row["status"], row["attempts"]) == ("dead", 1)
    assert "no event bus is configured" in row["last_error"]
    [record] = [r for r in caplog.records if r.name == "jfast.outbox"]
    assert "no event bus is configured" in record.getMessage()
    assert "dead" in record.getMessage()


async def test_failing_reports_a_row_that_has_failed_but_is_not_dead(engine: Any) -> None:
    class Down:
        async def enqueue(self, job: Job) -> str:
            raise ConnectionError("broker unreachable")

    async with AsyncSession(engine) as session, session.begin():
        await Outbox().enqueue(session, Job(task="t"))
    relay = OutboxRelay(engine, queue=Down())
    assert await relay.failing() == (0, None)
    await relay.relay_once()
    count, reason = await relay.failing()
    assert count == 1 and reason is not None and "broker unreachable" in reason


# -- the worker --------------------------------------------------------------


async def _until(condition: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 - polling a plain list, not an event
            await asyncio.sleep(0.01)


class MemoryBackend:
    """A queue in a list, recording what the worker did with each job."""

    visibility_timeout = 30

    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = list(jobs)
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str | None]] = []
        self.released: list[str] = []

    async def setup(self) -> None: ...

    async def enqueue(self, job: Job) -> str:
        self.jobs.append(job)
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        if not self.jobs:
            await asyncio.sleep(min(timeout, 0.01))
            return None
        job = self.jobs.pop(0)
        job.attempts += 1
        return job

    async def ack(self, job: Job) -> None:
        self.acked.append(job.id)

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        self.nacked.append((job.id, job.error))

    async def release(self, job: Job) -> None:
        self.released.append(job.id)

    async def stats(self) -> dict[str, int]:
        return {}

    async def health(self) -> tuple[bool, str]:
        return True, "ok"

    async def close(self) -> None: ...


async def test_the_worker_runs_a_job_inside_the_trace_that_queued_it(
    tracer: RecordingTracer,
) -> None:
    with _as_request("acme", "req-1"):
        job = Job(task="work")
    seen: dict[str, Any] = {}
    registry = TaskRegistry()

    async def work(payload: dict[str, Any]) -> None:
        seen["tenant"] = tenant_id_var.get()

    registry.register("work", work)
    backend = MemoryBackend([job])
    assert await Worker(backend, registry).run_once()
    assert seen["tenant"] == "acme"
    assert tracer.attached == [tracer.carrier]
    [(name, attributes)] = tracer.spans
    assert name == "job work" and attributes["job_id"] == job.id


async def test_a_failure_is_recorded_on_the_job_before_the_nack() -> None:
    registry = TaskRegistry()

    async def boom(payload: dict[str, Any]) -> None:
        raise ValueError("no such receipt")

    registry.register("boom", boom)
    backend = MemoryBackend([Job(task="boom")])
    await Worker(backend, registry).run_once()
    [(_, error)] = backend.nacked
    assert error == "ValueError: no such receipt"


async def test_stopping_drains_what_finishes_and_releases_what_does_not() -> None:
    registry = TaskRegistry()
    finished: list[str] = []

    async def quick(payload: dict[str, Any]) -> None:
        await asyncio.sleep(0.05)
        finished.append("quick")

    async def slow(payload: dict[str, Any]) -> None:
        await asyncio.sleep(30)

    registry.register("quick", quick)
    registry.register("slow", slow)
    quick_job, slow_job = Job(task="quick"), Job(task="slow")
    backend = MemoryBackend([quick_job, slow_job])
    worker = Worker(backend, registry, concurrency=2, poll_timeout=0.01, drain_timeout=0.3)
    running = asyncio.create_task(worker.run())
    await _until(lambda: not backend.jobs)
    # Both claimed and running; a third job arriving now must not be taken.
    late = Job(task="quick")
    backend.jobs.append(late)
    worker.stop()
    await asyncio.wait_for(running, timeout=5)

    assert finished == ["quick"]
    assert backend.acked == [quick_job.id]
    assert backend.released == [slow_job.id]
    assert backend.nacked == []
    assert backend.jobs == [late]


async def test_a_stopped_worker_with_every_slot_busy_claims_nothing_more() -> None:
    registry = TaskRegistry()
    gate = asyncio.Event()

    async def wait(payload: dict[str, Any]) -> None:
        await gate.wait()

    registry.register("wait", wait)
    backend = MemoryBackend([Job(task="wait"), Job(task="wait")])
    worker = Worker(backend, registry, concurrency=1, poll_timeout=0.01, drain_timeout=5)
    running = asyncio.create_task(worker.run())
    await _until(lambda: len(backend.jobs) <= 1)
    worker.stop()
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(running, timeout=5)
    assert len(backend.acked) == 1
    assert len(backend.jobs) == 1


# -- binding -----------------------------------------------------------------


def test_a_task_that_needs_a_session_refuses_to_boot_without_a_database() -> None:
    @task("alert.check")
    async def check(payload: dict[str, Any], session: TaskSession) -> None: ...

    with pytest.raises(PluginError, match="'database' plugin is not enabled"):
        build_test_app(
            plugins=["observability", "cache", "queue"],
            raw={"plugin": {"queue": {"backend": "redis"}}},
        )


# -- PostgreSQL --------------------------------------------------------------


@pytest.fixture
async def pg_dsn() -> Any:
    dsn = f"{PG_BASE}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TABLE IF EXISTS jfast_outbox, jfast_inbox, jfast_jobs, alerts, receipts")
            )
            await conn.execute(
                text("CREATE TABLE receipts (id SERIAL PRIMARY KEY, tenant_id TEXT, total INT)")
            )
            await conn.execute(
                text(
                    "CREATE TABLE alerts (id SERIAL PRIMARY KEY, tenant_id TEXT, "
                    "receipt_id INT, note TEXT)"
                )
            )
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    await engine.dispose()
    return dsn


def _pg_app(dsn: str, **overrides: Any) -> Any:
    return build_test_app(
        plugins=["observability", "database", "queue", "outbox"],
        raw={"plugin": {"database": {"dsn": dsn}, "outbox": {"relay": False}}},
        **overrides,
    )


async def _scalar(dsn: str, sql: str) -> Any:
    engine = create_async_engine(dsn)
    async with engine.connect() as conn:
        value = (await conn.execute(text(sql))).scalar()
    await engine.dispose()
    return value


async def _drain(app: Any, *, rounds: int = 10) -> None:
    ctx = app.state.jfast
    worker = Worker(ctx.require("queue"), ctx.require("tasks"), close_backend=False)
    for _ in range(rounds):
        if not await worker.run_once():
            return


async def test_publish_writes_one_job_per_subscriber_in_the_same_transaction(
    pg_dsn: str, tracer: RecordingTracer
) -> None:
    ran: list[tuple[str | None, str | None, str]] = []

    @subscribe("receipt.registered")
    async def alert(event: Event, session: TaskSession) -> None:
        ran.append((tenant_id_var.get(), request_id_var.get(), current_job().task))
        await session.execute(
            text("INSERT INTO alerts (tenant_id, receipt_id, note) VALUES (:t, :r, 'over')"),
            {"t": event.tenant_id, "r": event.data["id"]},
        )

    @subscribe("receipt.registered")
    async def audit(event: Event) -> None:
        ran.append((tenant_id_var.get(), request_id_var.get(), current_job().task))

    app = _pg_app(pg_dsn)
    ctx = app.state.jfast
    async with app.router.lifespan_context(app):
        box = ctx.require("outbox")
        sessionmaker = ctx.require("db.sessionmaker")

        with _as_request("acme", "req-7"):
            async with sessionmaker() as session:
                await session.begin()
                await box.publish(session, "receipts", Event(type="receipt.registered"))
                await session.rollback()
            async with sessionmaker() as session, session.begin():
                receipt = (
                    await session.execute(
                        text("INSERT INTO receipts (tenant_id, total) VALUES ('acme', 900) "
                             "RETURNING id")
                    )
                ).scalar_one()  # fmt: skip
                event = Event(type="receipt.registered", data={"id": receipt})
                await box.publish(session, "receipts", event)

        # Straight into jfast_jobs: nothing in the outbox, nothing from the
        # rolled-back publish.
        assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_jobs") == 2
        assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_outbox") == 0

        # Publishing the same event again is the same two jobs, not four.
        async with sessionmaker() as session, session.begin():
            await box.publish(session, "receipts", event)
        assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_jobs") == 2

        await _drain(app)

    assert sorted(ran) == sorted(
        [(("acme", "req-7", s.task)) for s in subscribers_for("receipt.registered")]
    )
    assert await _scalar(pg_dsn, "SELECT count(*) FROM alerts WHERE tenant_id = 'acme'") == 1
    assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_jobs") == 0
    # Each job ran inside the request's trace.
    assert tracer.attached and all(c == tracer.carrier for c in tracer.attached)


async def test_a_session_subscriber_runs_its_effect_once_per_event(pg_dsn: str) -> None:
    calls: list[str] = []

    @subscribe("receipt.registered")
    async def alert(event: Event, session: TaskSession) -> None:
        calls.append(event.id)
        await session.execute(
            text("INSERT INTO alerts (tenant_id, receipt_id, note) VALUES ('t', 1, 'x')")
        )

    app = _pg_app(pg_dsn)
    ctx = app.state.jfast
    async with app.router.lifespan_context(app):
        event = Event(type="receipt.registered")
        [subscriber] = subscribers_for("receipt.registered")
        # The same delivery twice: a worker that committed and died before it
        # acknowledged, and the redelivery that follows.
        for attempt in range(2):
            await ctx.require("queue").enqueue(
                Job(
                    id=f"{subscriber.job_id(event.id)[:30]}{attempt:02d}",
                    task=subscriber.task,
                    payload={"topic": "receipts", "event": event.to_dict()},
                )
            )
        await _drain(app)
    assert calls == [event.id]
    assert await _scalar(pg_dsn, "SELECT count(*) FROM alerts") == 1


async def test_a_task_session_commits_on_return_and_rolls_back_on_error(pg_dsn: str) -> None:
    @task("alert.note", idempotent_on=lambda payload: payload["receipt"])
    async def note(payload: dict[str, Any], session: TaskSession) -> None:
        await session.execute(
            text("INSERT INTO alerts (tenant_id, receipt_id, note) VALUES (:t, :r, 'n')"),
            {"t": tenant_id_var.get(), "r": payload["receipt"]},
        )
        if payload.get("explode"):
            raise RuntimeError("after writing")

    app = _pg_app(pg_dsn)
    ctx = app.state.jfast
    async with app.router.lifespan_context(app):
        queue = ctx.require("queue")
        await queue.enqueue(Job(task="alert.note", payload={"receipt": 1}, tenant_id="acme"))
        await queue.enqueue(Job(task="alert.note", payload={"receipt": 1}, tenant_id="acme"))
        await queue.enqueue(
            Job(task="alert.note", payload={"receipt": 2, "explode": True}, max_attempts=1)
        )
        await _drain(app)

    # Receipt 1 once, as acme, despite two jobs; receipt 2 rolled back whole.
    assert await _scalar(pg_dsn, "SELECT count(*) FROM alerts WHERE receipt_id = 1") == 1
    assert await _scalar(pg_dsn, "SELECT tenant_id FROM alerts WHERE receipt_id = 1") == "acme"
    assert await _scalar(pg_dsn, "SELECT count(*) FROM alerts WHERE receipt_id = 2") == 0
    assert (
        await _scalar(pg_dsn, "SELECT last_error FROM jfast_jobs WHERE status = 'dead'")
        == "RuntimeError: after writing"
    )
    # The failed claim rolled back with the work: a replay would run.
    assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_inbox WHERE message_id = '2'") == 0


async def test_tasks_and_subscribers_receive_the_running_apps_context(pg_dsn: str) -> None:
    """What a route reaches through ``get_context(request.app)``, a job reaches
    through a ``TaskContext`` parameter: the outbox here, ``llm`` or
    ``storage`` in a service that enables them. Run by the API's own app.
    """
    seen: dict[str, Any] = {}

    @task("alert.summarise", idempotent_on=lambda payload: payload["receipt"])
    async def summarise(payload: dict[str, Any], session: TaskSession, ctx: TaskContext) -> None:
        seen["task"] = ctx
        # A provider, used from inside the job's own transaction.
        await ctx.require("outbox").publish(
            session, "receipts", Event(type="receipt.summarised", data=payload)
        )

    @task("alert.plain")
    async def plain(payload: dict[str, Any], ctx: TaskContext) -> None:
        seen["plain"] = ctx

    @subscribe("receipt.summarised")
    async def react(event: Event, ctx: AppContext) -> None:
        seen["subscriber"] = (ctx, event.data["receipt"])

    @subscribe("receipt.summarised")
    async def react_in_transaction(
        event: Event, session: TaskSession, context: TaskContext
    ) -> None:
        seen["subscriber_session"] = context

    app = _pg_app(pg_dsn)
    ctx = app.state.jfast
    async with app.router.lifespan_context(app):
        queue = ctx.require("queue")
        await queue.enqueue(Job(task="alert.summarise", payload={"receipt": 7}))
        await queue.enqueue(Job(task="alert.plain", payload={}))
        await _drain(app)

    assert seen["task"] is ctx
    assert seen["plain"] is ctx
    assert seen["subscriber"] == (ctx, 7)
    assert seen["subscriber_session"] is ctx


async def test_publish_in_a_request_with_no_receiver_is_a_500_that_names_the_fix(
    pg_dsn: str,
) -> None:
    router = APIRouter()

    @router.post("/receipts", status_code=201)
    async def create(request: Request, session: DbSession) -> dict[str, str]:
        box = request.app.state.jfast.require("outbox")
        await box.publish(session, "receipts", Event(type="receipt.registered"))
        return {"ok": "yes"}

    app = build_test_app(
        plugins=["observability", "database", "queue", "outbox"],
        routers=[router],
        raw={"plugin": {"database": {"dsn": pg_dsn}, "outbox": {"relay": False}}},
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.post("/receipts")
    assert response.status_code == 500
    assert "subscribe" in response.json()["detail"]
    assert await _scalar(pg_dsn, "SELECT count(*) FROM jfast_outbox") == 0


async def test_ready_is_degraded_while_an_outbox_message_is_failing(pg_dsn: str) -> None:
    # A generous readiness deadline: under a loaded full-suite run the database
    # check could pass the 2 s default, turn "unavailable", and hide what this
    # asserts -- that a failing outbox message degrades readiness, no more.
    app = _pg_app(pg_dsn, readiness_timeout=15.0)
    ctx = app.state.jfast
    async with app.router.lifespan_context(app):
        engine = ctx.require("db.engine")
        async with AsyncSession(engine) as session, session.begin():
            await Outbox(events=object()).publish(session, "t", Event(type="x"))
        await ctx.require("outbox.relay").relay_once()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            body = (await client.get("/ready")).json()
    assert body["status"] == "degraded"
    assert "no event bus" in str(body["checks"]["outbox"])
