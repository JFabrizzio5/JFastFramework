"""The outbox: a message exists if and only if the transaction that wrote it did.

SQLite covers the write, the relay's bookkeeping and the inbox. PostgreSQL
covers what only a server can: that the PostgreSQL queue takes a job through
the request's own session, and that two relays running at once send every
row exactly once.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from jfastframework.db.framework import ensure_tables
from jfastframework.outbox import (
    INBOX_TABLE,
    OUTBOX_TABLE,
    Outbox,
    OutboxRelay,
    claim_once,
    outbox,
)
from jfastframework.plugins.builtin.events import Event
from jfastframework.queues.base import Job, current_job
from jfastframework.queues.worker import TaskRegistry, Worker

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")


class RecordingQueue:
    """A queue that remembers what it was given, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.jobs: list[Job] = []
        self.fail = fail

    async def enqueue(self, job: Job) -> str:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.jobs.append(job)
        return job.id


class RecordingBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Event]] = []

    async def publish(self, topic: str, event: Event) -> None:
        self.sent.append((topic, event))


@pytest.fixture
async def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    await ensure_tables(engine, OUTBOX_TABLE, INBOX_TABLE)
    yield engine
    await engine.dispose()


async def _rows(engine: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(outbox))).mappings().all())


async def test_a_rolled_back_transaction_leaves_no_message(engine) -> None:  # type: ignore[no-untyped-def]
    async with AsyncSession(engine) as session:
        await session.begin()
        await Outbox().enqueue(session, Job(task="send_receipt", payload={"id": 1}))
        await session.rollback()
    assert await _rows(engine) == []


async def test_a_committed_job_is_relayed_with_its_own_id(engine) -> None:  # type: ignore[no-untyped-def]
    job = Job(task="send_receipt", payload={"id": 7}, tenant_id="acme", request_id="r1")
    async with AsyncSession(engine) as session, session.begin():
        await Outbox().enqueue(session, job)

    queue = RecordingQueue()
    assert await OutboxRelay(engine, queue=queue).relay_once() == 1

    [sent] = queue.jobs
    # The same id end to end: a relay that dies after sending and before
    # marking the row sends it again, and the queue and the consumer
    # deduplicate on this.
    assert (sent.id, sent.task, sent.payload) == (job.id, "send_receipt", {"id": 7})
    assert (sent.tenant_id, sent.request_id) == ("acme", "r1")
    [row] = await _rows(engine)
    assert row["status"] == "published"
    # And nothing is sent twice.
    assert await OutboxRelay(engine, queue=queue).relay_once() == 0


async def test_an_event_is_relayed_to_its_topic_with_its_key(engine) -> None:  # type: ignore[no-untyped-def]
    event = Event(type="order.placed", data={"id": 3}, key="order-3")
    bus = RecordingBus()
    async with AsyncSession(engine) as session, session.begin():
        await Outbox(events=bus).publish(session, "orders", event)

    await OutboxRelay(engine, events=bus).relay_once()
    [(topic, sent)] = bus.sent
    assert topic == "orders"
    assert (sent.id, sent.type, sent.data, sent.key) == (
        event.id,
        "order.placed",
        {"id": 3},
        "order-3",
    )


async def test_a_failed_send_backs_off_and_then_goes_dead(engine) -> None:  # type: ignore[no-untyped-def]
    async with AsyncSession(engine) as session, session.begin():
        await Outbox().enqueue(session, Job(task="t"))

    relay = OutboxRelay(engine, queue=RecordingQueue(fail=True), max_attempts=2)
    assert await relay.relay_once() == 0
    [row] = await _rows(engine)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert "broker unreachable" in row["last_error"]

    # Backed off: not due yet, so the next pass does not touch it.
    assert await relay.relay_once() == 0
    [row] = await _rows(engine)
    assert row["attempts"] == 1

    async with engine.begin() as conn:
        await conn.execute(outbox.update().values(available_at=func.datetime("now", "-1 hour")))
    await relay.relay_once()
    [row] = await _rows(engine)
    assert (row["status"], row["attempts"]) == ("dead", 2)
    assert await relay.stats() == {"pending": 0, "published": 0, "dead": 1}


async def test_the_inbox_lets_a_message_through_once(engine) -> None:  # type: ignore[no-untyped-def]
    async with AsyncSession(engine) as session, session.begin():
        assert await claim_once(session, "m-1", consumer="emails")
        assert not await claim_once(session, "m-1", consumer="emails")
        # Another consumer has its own record of what it has seen.
        assert await claim_once(session, "m-1", consumer="billing")


async def test_a_claim_rolls_back_with_the_work(engine) -> None:  # type: ignore[no-untyped-def]
    async with AsyncSession(engine) as session:
        await session.begin()
        assert await claim_once(session, "m-2")
        await session.rollback()
    async with AsyncSession(engine) as session, session.begin():
        # The work never committed, so the redelivery is processed.
        assert await claim_once(session, "m-2")


async def test_a_handler_can_reach_its_own_job() -> None:
    seen: dict[str, str] = {}
    registry = TaskRegistry()

    @registry.task("t")
    async def handler(payload: dict[str, Any]) -> None:
        seen["id"] = current_job().id

    job = Job(task="t")

    class OneJob:
        visibility_timeout = 30.0

        def __init__(self) -> None:
            self.job: Job | None = job

        async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
            taken, self.job = self.job, None
            return taken

        async def ack(self, job: Job) -> None:
            return None

        async def nack(self, job: Job, *, retry: bool = True) -> None:
            raise AssertionError("handler failed")

    assert await Worker(OneJob(), registry).run_once()  # type: ignore[arg-type]
    assert seen == {"id": job.id}
    with pytest.raises(RuntimeError, match="outside a queue handler"):
        current_job()


# -- PostgreSQL --------------------------------------------------------------


@pytest.fixture
async def pg_engine():  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"{PG_BASE}/jfast", pool_size=10)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS jfast_outbox, jfast_inbox, jfast_jobs"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    await ensure_tables(engine, OUTBOX_TABLE, INBOX_TABLE)
    yield engine
    await engine.dispose()


async def test_the_postgres_queue_takes_the_job_in_the_same_transaction(pg_engine) -> None:  # type: ignore[no-untyped-def]
    from jfastframework.queues.postgres import PostgresQueue

    queue = PostgresQueue(pg_engine)
    await queue.setup()
    box = Outbox(queue=queue)

    async with AsyncSession(pg_engine) as session:
        await session.begin()
        await box.enqueue(session, Job(task="rolled_back"))
        await session.rollback()
    async with AsyncSession(pg_engine) as session, session.begin():
        await box.enqueue(session, Job(task="committed"))

    async with pg_engine.connect() as conn:
        tasks = (await conn.execute(text("SELECT task FROM jfast_jobs"))).scalars().all()
        relayed = (await conn.execute(text("SELECT count(*) FROM jfast_outbox"))).scalar_one()
    # Straight into the queue, with nothing left for a relay to do.
    assert tasks == ["committed"]
    assert relayed == 0


async def test_two_relays_at_once_send_every_row_exactly_once(pg_engine) -> None:  # type: ignore[no-untyped-def]
    async with AsyncSession(pg_engine) as session, session.begin():
        for n in range(60):
            await Outbox().enqueue(session, Job(task="t", payload={"n": n}))

    class SlowQueue(RecordingQueue):
        async def enqueue(self, job: Job) -> str:
            await asyncio.sleep(0.001)
            return await super().enqueue(job)

    queue = SlowQueue()
    relays = [OutboxRelay(pg_engine, queue=queue, batch_size=10) for _ in range(3)]
    while True:
        sent = await asyncio.gather(*(relay.relay_once() for relay in relays))
        if not any(sent):
            break

    ids = [job.id for job in queue.jobs]
    assert len(ids) == 60
    assert len(set(ids)) == 60
