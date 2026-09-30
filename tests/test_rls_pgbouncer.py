"""Row-level security behind PgBouncer in transaction mode.

Transaction pooling is where a per-connection setting goes wrong: the pooler
hands one server connection to many clients, one transaction at a time, so
anything a client leaves on the connection is inherited by the next client.
The tenant is set with ``set_config('jfast.tenant_id', ..., true)`` -- the
``true`` makes it transaction-local -- and these tests prove that claim through
a real PgBouncer rather than assuming it.

Needs two URLs, and skips without the first:

* ``JFAST_TEST_PGBOUNCER_URL`` -- PgBouncer in ``pool_mode = transaction``,
  as a role that is **not** a superuser and **not** BYPASSRLS (the only kind a
  policy binds), e.g. ``postgresql+asyncpg://jfast_bouncer:jfast_bouncer@
  localhost:6435/jfast``. The role is created here if it does not exist.
* ``JFAST_TEST_PG_URL`` -- the same server directly, as a superuser, to set the
  table up.

With ``default_pool_size = 1`` every transaction lands on one backend, which is
the strongest version of the test: two tenants, alternating, on literally the
same server connection. The CI snippet in docs/multitenancy.md configures it
that way.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from jfastframework.db.rls import TenantScopedSession, enable_tenant_rls
from jfastframework.plugins.builtin.database import DatabasePlugin, connect_args_for
from jfastframework.plugins.builtin.observability import tenant_id_var
from jfastframework.testing import build_test_app

BOUNCER = os.environ.get("JFAST_TEST_PGBOUNCER_URL", "")
PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
ADMIN_DSN = f"{PG_BASE}/jfast"
TABLE = "rls_bouncer_notes"

pytestmark = pytest.mark.skipif(
    not BOUNCER, reason="JFAST_TEST_PGBOUNCER_URL is not set (PgBouncer in transaction mode)"
)


class _Op:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _bouncer_engine(**overrides: Any) -> AsyncEngine:
    """An engine configured the way the database plugin does with pgbouncer = true."""
    args = connect_args_for(BOUNCER, session_timezone="UTC", pgbouncer=True)
    return create_async_engine(BOUNCER, connect_args=args, **overrides)


@pytest.fixture
async def notes() -> AsyncIterator[None]:
    url = make_url(BOUNCER)
    role, password = url.username, url.password
    assert role and password, "JFAST_TEST_PGBOUNCER_URL needs a user and a password"
    admin = create_async_engine(ADMIN_DSN)
    op = _Op()
    enable_tenant_rls(op, TABLE)
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                    f"'{role}') THEN CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOBYPASSRLS; END IF; END $$"
                )
            )
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            await conn.execute(
                text(f"CREATE TABLE {TABLE} (id serial PRIMARY KEY, tenant_id text, body text)")
            )
            await conn.execute(text(f"GRANT ALL ON {TABLE} TO {role}"))
            await conn.execute(text(f"GRANT ALL ON SEQUENCE {TABLE}_id_seq TO {role}"))
            await conn.execute(
                text(
                    f"INSERT INTO {TABLE} (tenant_id, body) VALUES "
                    "('acme', 'a1'), ('acme', 'a2'), ('globex', 'g1')"
                )
            )
            for statement in op.statements:
                await conn.execute(text(statement))
    finally:
        await admin.dispose()
    yield None


async def test_the_role_is_one_the_policies_bind(notes: None) -> None:
    engine = _bouncer_engine()
    try:
        async with engine.connect() as conn:
            superuser, bypass = (
                await conn.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
    finally:
        await engine.dispose()
    # Otherwise every assertion below passes for the wrong reason.
    assert not superuser and not bypass


async def test_the_tenant_is_transaction_local_through_pgbouncer(notes: None) -> None:
    first, second = _bouncer_engine(), _bouncer_engine()
    try:
        async with first.begin() as conn:
            await conn.execute(text("SELECT set_config('jfast.tenant_id', 'acme', true)"))
            before = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
            seen = (await conn.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar()
        assert seen == 2

        # Another client, next transaction. If the setting outlived the
        # transaction it would be sitting on the server connection now.
        async with second.begin() as conn:
            after = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()
            setting = (
                await conn.execute(text("SELECT current_setting('jfast.tenant_id', true)"))
            ).scalar()
            leaked = (await conn.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar()
        assert setting in ("", None)
        assert leaked == 0
        # With one server connection in the pool (the documented CI setup) this
        # is the very connection the first transaction set the tenant on.
        assert isinstance(before, int) and isinstance(after, int)
    finally:
        await first.dispose()
        await second.dispose()


async def test_interleaved_tenants_never_see_each_other(notes: None) -> None:
    """Two tenants, many concurrent transactions, through the framework's session."""
    engine = _bouncer_engine(pool_size=8, max_overflow=0)
    maker = async_sessionmaker(engine, sync_session_class=TenantScopedSession)
    pids: dict[str, set[int]] = {"acme": set(), "globex": set()}
    expected = {"acme": ["a1", "a2"], "globex": ["g1"]}

    async def one(tenant: str, round_: int) -> None:
        async with maker() as session:
            session.info["tenant_id"] = tenant
            async with session.begin():
                pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                rows = await session.execute(text(f"SELECT body FROM {TABLE} ORDER BY body"))
                bodies = [row[0] for row in rows]
                assert bodies == expected[tenant], (tenant, round_, bodies)
                pids[tenant].add(int(pid))

    try:
        await asyncio.gather(*(one(tenant, i) for i in range(25) for tenant in ("acme", "globex")))
        # The point of the test: both tenants ran on at least one common
        # server connection and still never saw each other's rows. With
        # default_pool_size = 1 that is every transaction.
        assert pids["acme"] & pids["globex"], pids

        # And a transaction with no tenant, after all of that, sees nothing.
        async with maker() as session, session.begin():
            rows = await session.execute(text(f"SELECT body FROM {TABLE}"))
            assert list(rows) == []
    finally:
        await engine.dispose()


async def test_writes_for_another_tenant_are_refused_through_pgbouncer(notes: None) -> None:
    engine = _bouncer_engine()
    maker = async_sessionmaker(engine, sync_session_class=TenantScopedSession)
    token = tenant_id_var.set("globex")
    try:
        async with maker() as session:
            changed = await session.execute(text(f"UPDATE {TABLE} SET body = 'x'"))
            assert changed.rowcount == 1  # its own row, not acme's two
            await session.rollback()
        async with maker() as session:
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.execute(
                    text(f"INSERT INTO {TABLE} (tenant_id, body) VALUES ('acme', 'stolen')")
                )
    finally:
        tenant_id_var.reset(token)
        await engine.dispose()


async def test_the_database_plugin_speaks_pgbouncer_from_settings(notes: None) -> None:
    """`[plugin.database] pgbouncer = true` is the whole configuration.

    Eight concurrent workers against a transaction pool: without the setting,
    asyncpg's statement cache fails here with ``prepared statement
    "__asyncpg_stmt_N__" does not exist`` whenever PgBouncer is not tracking
    prepared statements itself (``max_prepared_statements = 0``, or before
    1.21) and a transaction lands on a server connection other than the one
    that prepared it.
    """
    app = build_test_app()
    plugin = DatabasePlugin({"dsn": BOUNCER, "pgbouncer": True, "rls": True, "pool_size": 8})
    plugin.register(app.state.jfast)
    try:
        maker = app.state.jfast.require("db.sessionmaker")

        async def worker(tenant: str) -> None:
            for _ in range(10):
                token = tenant_id_var.set(tenant)
                try:
                    async with maker() as session, session.begin():
                        rows = await session.execute(text(f"SELECT tenant_id FROM {TABLE}"))
                        assert {row[0] for row in rows} == {tenant}
                finally:
                    tenant_id_var.reset(token)

        await asyncio.gather(*(worker(t) for t in ("acme", "globex") * 4))
    finally:
        await plugin.shutdown(app.state.jfast)


MULTI = os.environ.get("JFAST_TEST_PGBOUNCER_MULTI_URL", "")


@pytest.mark.skipif(
    not MULTI,
    reason=(
        "JFAST_TEST_PGBOUNCER_MULTI_URL is not set: a PgBouncer database with several server "
        "connections and max_prepared_statements = 0"
    ),
)
async def test_asyncpg_defaults_break_on_a_transaction_pool_and_the_setting_fixes_it() -> None:
    """The failure the setting exists for, reproduced rather than described.

    Needs a pool of more than one server connection and PgBouncer not tracking
    prepared statements: a statement asyncpg cached on one backend is then
    executed on another, where it was never prepared.
    """

    async def hammer(engine: AsyncEngine) -> None:
        async def worker(n: int) -> None:
            for i in range(20):
                async with engine.begin() as conn:
                    await conn.execute(text(f"SELECT {n} + :i"), {"i": i})

        try:
            await asyncio.gather(*(worker(n) for n in range(8)))
        finally:
            await engine.dispose()

    with pytest.raises(DBAPIError, match="prepared statement"):
        await hammer(create_async_engine(MULTI, pool_size=4))

    args = connect_args_for(MULTI, session_timezone="UTC", pgbouncer=True)
    await hammer(create_async_engine(MULTI, pool_size=4, connect_args=args))
