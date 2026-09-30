"""Startup schema work that every worker process and replica runs at once.

Found by the compose smoke in CI: the generated image runs uvicorn with one
process per CPU, each running its plugins' startup. The queue ran
``ALTER TABLE jfast_jobs ADD COLUMN IF NOT EXISTS trace`` at every boot, and
that takes an ACCESS EXCLUSIVE lock *before* it notices the column exists. A
process still booting then queued behind any open transaction on the table,
and every later query -- another process's ``/ready`` included -- queued
behind it: ``/ready`` answered 200, then 503.

These need a real PostgreSQL (``JFAST_TEST_PG_URL``); pgvector for the last.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")


@pytest.fixture
async def engine():  # type: ignore[no-untyped-def]
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"{PG_BASE}/jfast", pool_size=12, max_overflow=4)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - no server
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}: {exc}")
    yield engine
    await engine.dispose()


def _table() -> str:
    return f"jobs_{uuid.uuid4().hex[:10]}"


async def _drop(engine, table: str) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


async def test_a_second_boot_does_not_wait_on_a_reader(engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from jfastframework.queues.postgres import PostgresQueue

    table = _table()
    queue = PostgresQueue(engine, table=table)
    await queue.setup()
    try:
        # A transaction that has read the table holds ACCESS SHARE until it
        # ends -- a claim in progress, or a /ready in another process.
        async with engine.connect() as reader:
            transaction = await reader.begin()
            await reader.execute(text(f"SELECT count(*) FROM {table}"))
            # Before the fix this waited for the reader forever: the ALTER's
            # exclusive lock cannot be granted while the read is open.
            await asyncio.wait_for(PostgresQueue(engine, table=table).setup(), timeout=5)
            await transaction.rollback()
    finally:
        await _drop(engine, table)


async def test_many_processes_booting_on_an_empty_database_all_succeed(  # type: ignore[no-untyped-def]
    engine,
) -> None:
    from jfastframework.queues.postgres import PostgresQueue

    table = _table()
    try:
        # Without the advisory lock, concurrent CREATE TABLE IF NOT EXISTS for
        # one new table can fail on pg_type's unique index.
        await asyncio.gather(*(PostgresQueue(engine, table=table).setup() for _ in range(8)))
    finally:
        await _drop(engine, table)


async def test_an_old_queue_table_still_gains_the_trace_column(engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from jfastframework.db.framework import column_exists
    from jfastframework.queues.postgres import PostgresQueue

    table = _table()
    await PostgresQueue(engine, table=table).setup()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN trace"))
        await PostgresQueue(engine, table=table).setup()
        async with engine.connect() as conn:
            assert await column_exists(conn, table, "trace")
    finally:
        await _drop(engine, table)


async def test_a_current_vector_table_is_left_alone_at_boot(engine) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from jfastframework.vectors.pgvector import PgVectorStore

    table = f"chunks_{uuid.uuid4().hex[:10]}"
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as exc:  # noqa: BLE001 - no pgvector on this server
        pytest.skip(f"no pgvector: {exc}")
    store = PgVectorStore(engine, table=table, dimensions=8)
    await store.ensure_schema()
    try:
        async with engine.connect() as reader:
            transaction = await reader.begin()
            await reader.execute(text(f"SELECT count(*) FROM {table}"))
            await asyncio.wait_for(
                PgVectorStore(engine, table=table, dimensions=8).ensure_schema(), timeout=5
            )
            await transaction.rollback()
    finally:
        await _drop(engine, table)


async def test_framework_tables_survive_many_processes_booting_at_once(  # type: ignore[no-untyped-def]
    engine,
) -> None:
    from sqlalchemy import text

    import jfastframework.idempotency  # noqa: F401 - registers jfast_idempotency
    from jfastframework.db.framework import ensure_columns, ensure_tables

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS jfast_idempotency"))
    await asyncio.gather(*(ensure_tables(engine, "jfast_idempotency") for _ in range(8)))
    await asyncio.gather(*(ensure_columns(engine, "jfast_idempotency") for _ in range(8)))
