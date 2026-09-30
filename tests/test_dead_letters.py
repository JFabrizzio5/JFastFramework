"""Dead letters, release and the trace column, on real PostgreSQL and Redis.

Each backend is checked for the three things the worker and `jfast jobs`
now rely on: a failure reason that survives to the dead-letter queue, a
replay that resets attempts, and a release that gives back the attempt a
shutdown interrupted.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.queues.base import DeadLetters, Job

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "")
TABLE = "jfast_jobs_dead"


@pytest.fixture
async def pg_engine() -> Any:
    engine = create_async_engine(f"{PG_BASE}/jfast")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    yield engine
    await engine.dispose()


async def test_setup_adds_the_trace_column_to_a_table_from_before_it(pg_engine: Any) -> None:
    from jfastframework.queues.postgres import PostgresQueue

    async with pg_engine.begin() as conn:
        # The a10 shape, without `trace`.
        await conn.execute(
            text(
                f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, task TEXT NOT NULL, "
                f"payload JSONB NOT NULL DEFAULT '{{}}'::jsonb, attempts INTEGER NOT NULL "
                f"DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, available_at TIMESTAMPTZ "
                f"NOT NULL DEFAULT NOW(), locked_until TIMESTAMPTZ, request_id TEXT, "
                f"tenant_id TEXT, status TEXT NOT NULL DEFAULT 'pending', last_error TEXT, "
                f"created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
        )
    queue = PostgresQueue(pg_engine, table=TABLE)
    await queue.setup()
    await queue.setup()  # and again, on every start
    carrier = {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}
    await queue.enqueue(Job(task="t", trace=carrier))
    claimed = await queue.dequeue()
    assert claimed is not None and claimed.trace == carrier


async def test_postgres_dead_letters_keep_the_reason_and_replay(pg_engine: Any) -> None:
    from jfastframework.queues.postgres import PostgresQueue

    queue = PostgresQueue(pg_engine, table=TABLE)
    await queue.setup()
    assert isinstance(queue, DeadLetters)
    job = Job(task="charge", max_attempts=1, tenant_id="acme")
    await queue.enqueue(job)
    claimed = await queue.dequeue()
    assert claimed is not None
    claimed.error = "ValueError: card declined"
    await queue.nack(claimed)

    [dead] = await queue.dead()
    assert (dead.id, dead.task, dead.tenant_id, dead.error) == (
        job.id,
        "charge",
        "acme",
        "ValueError: card declined",
    )
    assert await queue.retry_dead(["not-a-job"]) == 0
    assert await queue.retry_dead([job.id]) == 1
    assert await queue.dead() == []
    again = await queue.dequeue()
    assert again is not None and again.attempts == 1


async def test_postgres_release_refunds_the_attempt(pg_engine: Any) -> None:
    from jfastframework.queues.postgres import PostgresQueue

    queue = PostgresQueue(pg_engine, table=TABLE)
    await queue.setup()
    await queue.enqueue(Job(task="t"))
    claimed = await queue.dequeue()
    assert claimed is not None and claimed.attempts == 1
    await queue.release(claimed)
    # Available at once, not after a backoff, and with the attempt given back.
    again = await queue.dequeue()
    assert again is not None and again.attempts == 1


# -- Redis -------------------------------------------------------------------


@pytest.fixture
async def redis_queue() -> Any:
    if not REDIS_URL:
        pytest.skip("set JFAST_TEST_REDIS_URL")
    import redis.asyncio as redis

    from jfastframework.queues.redis import RedisQueue

    client = redis.from_url(REDIS_URL)
    name = "jfast:test:deadletters"
    keys = await client.keys(f"{name}*")
    if keys:
        await client.delete(*keys)
    queue = RedisQueue(client, name=name, visibility_timeout=30)
    await queue.setup()
    yield queue
    keys = await client.keys(f"{name}*")
    if keys:
        await client.delete(*keys)
    await client.aclose()


async def test_redis_dead_letters_keep_the_reason_and_replay(redis_queue: Any) -> None:
    job = Job(task="charge", max_attempts=1)
    await redis_queue.enqueue(job)
    claimed = await redis_queue.dequeue(timeout=1)
    claimed.error = "ValueError: card declined"
    await redis_queue.nack(claimed)

    [dead] = await redis_queue.dead()
    assert (dead.id, dead.error, dead.attempts) == (job.id, "ValueError: card declined", 1)
    assert await redis_queue.retry_dead(["other"]) == 0
    assert await redis_queue.retry_dead(None) == 1
    assert await redis_queue.dead() == []
    again = await redis_queue.dequeue(timeout=1)
    assert again.id == job.id and again.attempts == 1


async def test_redis_release_refunds_the_attempt(redis_queue: Any) -> None:
    await redis_queue.enqueue(Job(task="t"))
    claimed = await redis_queue.dequeue(timeout=1)
    assert claimed.attempts == 1
    await redis_queue.release(claimed)
    again = await redis_queue.dequeue(timeout=1)
    assert again.id == claimed.id and again.attempts == 1
    assert (await redis_queue.stats())["running"] == 1
