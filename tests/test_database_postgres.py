"""The parts that only a real PostgreSQL can answer.

Two databases on one server stand in for a primary and its standby. The stand-in
is deliberate and its limits are worth stating: it reproduces *lag* exactly --
the replica does not have the row until something applies it -- and it does not
reproduce streaming replication, failover or `pg_last_wal_replay_lsn`. Lag is
what breaks read-after-write, so lag is what is simulated, and it is simulated
as the worst case: unbounded until the test applies the change itself.

Skipped when no server answers. Bring one up with::

    docker run -d --name pg -p 5499:5432 \\
      -e POSTGRES_USER=jfast -e POSTGRES_PASSWORD=jfast -e POSTGRES_DB=jfast postgres:16
    psql "postgresql://jfast:jfast@localhost:5499/jfast" \\
      -c 'create database primarydb' -c 'create database replicadb' \\
      -c 'create database tenant_a' -c 'create database tenant_b'
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from jfastframework.plugins.builtin.database import (
    DatabasePlugin,
    TenantEngines,
    mark_write,
    read_session_dependency,
    session_dependency,
)
from jfastframework.testing import build_test_app, client_for

BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
PRIMARY = f"{BASE}/primarydb"
REPLICA = f"{BASE}/replicadb"


async def _reachable() -> bool:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PRIMARY)
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
    if not asyncio.run(_reachable()):
        pytest.skip(f"no PostgreSQL at {BASE}")
    return None


async def _reset() -> None:
    """Both databases hold the table, and neither holds a row."""
    from sqlalchemy.ext.asyncio import create_async_engine

    for dsn in (PRIMARY, REPLICA):
        engine = create_async_engine(dsn)
        async with engine.begin() as conn:
            await conn.execute(
                text("create table if not exists notes (id serial primary key, label text)")
            )
            await conn.execute(text("truncate notes"))
        await engine.dispose()


async def _apply_to_replica(label: str) -> None:
    """What WAL replay would have done, done by hand so the delay is the test's."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(REPLICA)
    async with engine.begin() as conn:
        await conn.execute(text("insert into notes (label) values (:label)"), {"label": label})
    await engine.dispose()


async def _app(**overrides: Any) -> Any:
    config: dict[str, Any] = {
        "read_write_split": True,
        "connections": {
            "primary": {"dsn": PRIMARY},
            "replica": {"dsn": REPLICA, "read_only": True},
        },
    }
    config.update(overrides)

    app = build_test_app()
    DatabasePlugin(config).register(app.state.jfast)

    @app.post("/notes")
    async def create(
        request: Request, session: Any = Depends(session_dependency, scope="function")
    ) -> dict[str, str]:
        await session.execute(text("insert into notes (label) values ('one')"))
        # Raw SQL flushes nothing the ORM can see, so the write says so itself.
        mark_write(request)
        return {"ok": "yes"}

    @app.get("/notes")
    async def read(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, Any]:
        rows = await session.execute(text("select label from notes order by id"))
        bind = str(session.get_bind().url)
        return {"labels": [row[0] for row in rows], "bind": bind}

    @app.post("/notes-on-a-read-session")
    async def smuggle(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, str]:
        await session.execute(text("insert into notes (label) values ('smuggled')"))
        return {"ok": "no"}

    return app


# -- the lag is real -----------------------------------------------------


async def test_the_replica_is_genuinely_behind(postgres: None) -> None:
    """Without this the read-after-write test below would prove nothing."""
    await _reset()
    app = await _app()

    async with client_for(app) as writer:
        await writer.post("/notes")
    async with client_for(app) as reader:
        body = (await reader.get("/notes")).json()

    assert body["bind"].endswith("/replicadb")
    assert body["labels"] == []


async def test_read_after_write_survives_a_lagging_replica(postgres: None) -> None:
    await _reset()
    app = await _app()

    async with client_for(app) as client:
        await client.post("/notes")
        body = (await client.get("/notes")).json()

    assert body["bind"].endswith("/primarydb")
    assert body["labels"] == ["one"]


async def test_reads_return_to_the_replica_once_the_window_passes(postgres: None) -> None:
    await _reset()
    app = await _app(pin_window=0.3)

    async with client_for(app) as client:
        await client.post("/notes")
        await _apply_to_replica("one")
        await asyncio.sleep(0.45)
        body = (await client.get("/notes")).json()

    assert body["bind"].endswith("/replicadb")
    assert body["labels"] == ["one"]


# -- the replica refuses writes, at the server ---------------------------


async def test_postgresql_itself_refuses_a_write_on_a_read_connection(postgres: None) -> None:
    """`default_transaction_read_only` catches the raw SQL the ORM cannot see."""
    await _reset()
    app = await _app()

    async with client_for(app) as client:
        with pytest.raises(DBAPIError, match="read-only"):
            await client.post("/notes-on-a-read-session")


# -- pools are per instance, counted on the server -----------------------


async def test_each_instance_opens_its_own_pool(postgres: None) -> None:
    """Counted on the server: two instances, two pools, two sets of backends."""
    from sqlalchemy.ext.asyncio import create_async_engine

    await _reset()
    app = await _app(
        connections={
            "primary": {"dsn": PRIMARY, "pool_size": 3, "max_overflow": 0},
            "replica": {"dsn": REPLICA, "read_only": True, "pool_size": 1, "max_overflow": 0},
        }
    )
    databases = app.state.jfast.require("db.databases")
    counter = create_async_engine(PRIMARY)

    async def backends() -> dict[str, int]:
        async with counter.connect() as conn:
            rows = await conn.execute(
                text(
                    "select datname, count(*) from pg_stat_activity "
                    "where datname in ('primarydb', 'replicadb') group by datname"
                )
            )
            return {name: count for name, count in rows.all()}

    async def hold(name: str, count: int) -> list[Any]:
        conns = [await databases.engine(name).connect() for _ in range(count)]
        for conn in conns:
            await conn.execute(text("select 1"))
        return conns

    before = await backends()
    held = await hold("primary", 3) + await hold("replica", 1)
    try:
        during = await backends()
    finally:
        for conn in held:
            await conn.close()
        await counter.dispose()
        await databases.dispose()

    assert during.get("primarydb", 0) - before.get("primarydb", 0) == 3
    assert during.get("replicadb", 0) - before.get("replicadb", 0) == 1


async def test_a_pool_stops_at_its_own_size(postgres: None) -> None:
    """`pool_size` is per instance, and the server never sees the extra backend."""
    await _reset()
    app = await _app(
        connections={
            "primary": {"dsn": PRIMARY},
            "replica": {
                "dsn": REPLICA,
                "read_only": True,
                "pool_size": 1,
                "max_overflow": 0,
                "pool_timeout": 0.5,
            },
        }
    )
    databases = app.state.jfast.require("db.databases")
    engine = databases.engine("replica")

    held = await engine.connect()
    try:
        with pytest.raises(PoolTimeout):
            await engine.connect()
    finally:
        await held.close()
        await databases.dispose()


# -- a database per tenant, against real databases -----------------------


async def test_a_tenant_writes_land_in_that_tenants_database(postgres: None) -> None:
    engines = TenantEngines(
        resolve=lambda tenant: f"{BASE}/{tenant}",
        max_engines=1,
        pool_size=1,
        max_overflow=0,
    )
    try:
        for tenant in ("tenant_a", "tenant_b"):
            async with engines.session(tenant) as session:
                await session.execute(
                    text("create table if not exists rows_ (id serial primary key, who text)")
                )
                await session.execute(text("truncate rows_"))
                await session.execute(
                    text("insert into rows_ (who) values (:who)"), {"who": tenant}
                )
                await session.commit()

        # max_engines = 1, so reaching tenant_a again evicted tenant_b's engine
        # and built a new one. The rows must not have moved.
        for tenant in ("tenant_a", "tenant_b"):
            async with engines.session(tenant) as session:
                rows = await session.execute(text("select who from rows_"))
                assert [row[0] for row in rows] == [tenant]
        assert engines.size == 1
    finally:
        await engines.dispose()
