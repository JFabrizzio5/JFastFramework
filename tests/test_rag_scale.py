"""Found by scripts/bench_rag.py: the pgvector store's writes do not use the tenant index.

``sync_document`` and ``existing_hashes`` filter with ``tenant_id IS NOT
DISTINCT FROM :tenant``. PostgreSQL cannot use a btree for ``IS NOT DISTINCT
FROM``, so the ``(tenant_id, document_id)`` index is walked end to end with
the tenant as a filter: on 300,000 chunks the DELETE of one document took
19.2 ms instead of 0.03 ms, and it grows with the table -- every document
written pays for every chunk already stored.

This test captures the statements a write sends and asks the planner about
each one. It is expected to fail until the store writes ``tenant_id =
:tenant`` (and ``IS NULL`` for an unscoped store); ``strict`` makes the fix
flip it red, so the marker is removed with the fix.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
TABLE = "rag_scale_probe"


def _walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in _walk(child))]


@pytest.mark.xfail(
    strict=True,
    reason="PgVectorStore filters tenant_id with IS NOT DISTINCT FROM, which no index serves",
)
async def test_a_document_write_finds_its_rows_through_the_tenant_index() -> None:
    from jfastframework.vectors.base import Chunk
    from jfastframework.vectors.pgvector import PgVectorStore

    engine = create_async_engine(f"{PG_BASE}/jfast")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    except Exception as exc:  # noqa: BLE001 - no server, or no pgvector
        await engine.dispose()
        pytest.skip(f"no PostgreSQL with pgvector at {PG_BASE}: {exc}")
    try:
        store = PgVectorStore(engine, table=TABLE, dimensions=3)
        await store.ensure_schema()
        async with engine.begin() as conn:
            # Enough rows across enough tenants that a plan which ignores the
            # tenant has to read many of them.
            await conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, document_id, chunk_index, content, "
                    "embedding) SELECT 't' || (g % 200), 'd' || (g / 10), g % 10, 'x', "
                    "'[1,0,0]' FROM generate_series(1, 20000) g"
                )
            )
            await conn.execute(text(f"ANALYZE {TABLE}"))

        sent: list[tuple[str, Any]] = []

        def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
            if TABLE in statement and statement.lstrip().upper().startswith(("DELETE", "SELECT")):
                sent.append((statement, parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        await store.upsert([Chunk("d1", 0, "hello", {}, "t1")], [[1.0, 0.0, 0.0]])
        await store.existing_hashes("d1", tenant_id="t1")
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert sent, "the store sent no DELETE or SELECT on its table"

        unindexed = []
        async with engine.connect() as conn:
            raw = await conn.exec_driver_sql("SELECT 1")  # warm the connection
            raw.close()
            driver = await conn.get_raw_connection()
            for statement, parameters in sent:
                args = parameters if isinstance(parameters, (list, tuple)) else ()
                plan = await driver.driver_connection.fetchval(
                    f"EXPLAIN (FORMAT JSON) {statement}", *args
                )
                parsed = plan if isinstance(plan, list) else json.loads(plan)
                nodes = _walk(parsed[0]["Plan"])
                uses_tenant = any("tenant_id" in n.get("Index Cond", "") for n in nodes)
                if not uses_tenant:
                    unindexed.append(statement.split("WHERE")[0].strip()[:60])
        assert unindexed == [], f"statements that do not reach the tenant by index: {unindexed}"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await engine.dispose()
