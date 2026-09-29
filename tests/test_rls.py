"""Row-level security, against a real PostgreSQL and a role it applies to.

Everything here runs as ``jfast_app``, a role that is neither a superuser nor
BYPASSRLS -- the only kind the policies bind. The fixture creates it; the
superuser only sets the table up.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from jfastframework.db.rls import (
    TenantScopedSession,
    bypass_rls,
    enable_tenant_rls,
    role_problem,
    tenant_policy_sql,
)
from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.database import DatabasePlugin, DbSession
from jfastframework.plugins.builtin.observability import tenant_id_var
from jfastframework.testing import build_test_app

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
ADMIN_DSN = f"{PG_BASE}/jfast"
APP_DSN = ADMIN_DSN.replace("://jfast:jfast@", "://jfast_app:jfast_app@")


class _Op:
    """Enough of alembic's ``op`` for the helpers: they only call execute."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_the_policy_binds_the_owner_too() -> None:
    statements = tenant_policy_sql("invoices")
    assert "ALTER TABLE invoices FORCE ROW LEVEL SECURITY" in statements
    assert statements[-1].startswith("CREATE POLICY jfast_tenant_isolation ON invoices")
    assert "jfast.rls_bypass" not in statements[-1]
    assert "jfast.rls_bypass" in tenant_policy_sql("invoices", allow_bypass=True)[-1]


def test_identifiers_are_validated() -> None:
    with pytest.raises(ValueError):
        tenant_policy_sql("invoices; DROP TABLE users")


@pytest.fixture
async def tables() -> AsyncIterator[None]:
    admin = create_async_engine(ADMIN_DSN)
    try:
        async with admin.begin() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await admin.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")

    op = _Op()
    enable_tenant_rls(op, "rls_invoices")
    enable_tenant_rls(op, "rls_totals", allow_bypass=True)
    async with admin.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jfast_app') "
                "THEN CREATE ROLE jfast_app LOGIN PASSWORD 'jfast_app' "
                "NOSUPERUSER NOBYPASSRLS; END IF; END $$"
            )
        )
        for table in ("rls_invoices", "rls_totals"):
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(
                text(f"CREATE TABLE {table} (id serial PRIMARY KEY, tenant_id text, label text)")
            )
            await conn.execute(text(f"GRANT ALL ON {table} TO jfast_app"))
            await conn.execute(text(f"GRANT ALL ON SEQUENCE {table}_id_seq TO jfast_app"))
            await conn.execute(
                text(
                    f"INSERT INTO {table} (tenant_id, label) "
                    f"VALUES ('acme', 'a1'), ('acme', 'a2'), ('globex', 'g1')"
                )
            )
        for statement in op.statements:
            await conn.execute(text(statement))
    await admin.dispose()
    yield None


@pytest.fixture
async def app_engine(tables: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(APP_DSN)
    yield engine
    await engine.dispose()


async def _labels(engine: AsyncEngine, table: str = "rls_invoices") -> list[str]:
    maker = async_sessionmaker(engine, sync_session_class=TenantScopedSession)
    async with maker() as session:
        rows = await session.execute(text(f"SELECT label FROM {table} ORDER BY label"))
        return [row[0] for row in rows]


async def test_raw_sql_sees_only_the_current_tenant(app_engine: AsyncEngine) -> None:
    token = tenant_id_var.set("acme")
    try:
        # No WHERE clause at all: the database adds it.
        assert await _labels(app_engine) == ["a1", "a2"]
    finally:
        tenant_id_var.reset(token)


async def test_no_tenant_means_no_rows(app_engine: AsyncEngine) -> None:
    assert await _labels(app_engine) == []


async def test_a_write_for_another_tenant_is_refused(app_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(app_engine, sync_session_class=TenantScopedSession)
    token = tenant_id_var.set("acme")
    try:
        async with maker() as session:
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.execute(
                    text("INSERT INTO rls_invoices (tenant_id, label) VALUES ('globex', 'x')")
                )
    finally:
        tenant_id_var.reset(token)


async def test_the_tenant_does_not_leak_into_the_next_transaction(
    app_engine: AsyncEngine,
) -> None:
    """set_config(..., true) is transaction-local: a pooled connection forgets it."""
    maker = async_sessionmaker(app_engine, sync_session_class=TenantScopedSession)
    token = tenant_id_var.set("acme")
    try:
        async with maker() as session:
            await session.execute(text("SELECT 1"))
    finally:
        tenant_id_var.reset(token)
    async with app_engine.connect() as conn:
        rows = await conn.execute(text("SELECT label FROM rls_invoices"))
        assert list(rows) == []


async def test_bypass_only_works_where_the_policy_allows_it(app_engine: AsyncEngine) -> None:
    with bypass_rls():
        assert await _labels(app_engine, "rls_totals") == ["a1", "a2", "g1"]
        assert await _labels(app_engine, "rls_invoices") == []


async def test_the_role_check_names_a_superuser(app_engine: AsyncEngine) -> None:
    assert await role_problem(app_engine) is None
    admin = create_async_engine(ADMIN_DSN)
    try:
        problem = await role_problem(admin)
    finally:
        await admin.dispose()
    assert problem is not None and "superuser" in problem


async def test_production_refuses_rls_under_a_superuser(tables: None) -> None:
    app = build_test_app(env="prod")
    plugin = DatabasePlugin({"dsn": ADMIN_DSN, "rls": True})
    plugin.register(app.state.jfast)
    try:
        with pytest.raises(PluginError, match="superuser"):
            await plugin.startup(app.state.jfast)
    finally:
        await plugin.shutdown(app.state.jfast)


async def test_a_request_reads_only_its_tenant(tables: None) -> None:
    router = APIRouter()

    @router.get("/labels")
    async def labels(session: DbSession) -> list[str]:
        rows = await session.execute(text("SELECT label FROM rls_invoices ORDER BY label"))
        return [row[0] for row in rows]

    app = build_test_app(routers=[router])
    plugin = DatabasePlugin({"dsn": APP_DSN, "rls": True})
    plugin.register(app.state.jfast)

    @app.middleware("http")
    async def tenant_from_header(request: Request, call_next: Any) -> Any:
        token = tenant_id_var.set(request.headers.get("x-tenant"))
        try:
            return await call_next(request)
        finally:
            tenant_id_var.reset(token)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            acme = (await client.get("/labels", headers={"x-tenant": "acme"})).json()
            globex = (await client.get("/labels", headers={"x-tenant": "globex"})).json()
            nobody = (await client.get("/labels")).json()
    finally:
        await plugin.shutdown(app.state.jfast)
    assert (acme, globex, nobody) == (["a1", "a2"], ["g1"], [])


async def test_no_tenant_means_no_writes_either(app_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(app_engine, sync_session_class=TenantScopedSession)
    async with maker() as session:
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text("INSERT INTO rls_invoices (tenant_id, label) VALUES ('acme', 'x')")
            )
