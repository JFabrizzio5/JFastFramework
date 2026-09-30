"""`jfast tenancy enable`, end to end, against a real PostgreSQL.

The claim being tested is the one that matters: after the switch, a second
tenant cannot read or write the first tenant's rows **because the database
refuses**, not because a repository remembered to filter. So nothing here goes
through a repository. A generated single-tenant service gets data in two
tables and in a RAG chunks table, the command writes its revision, Alembic
applies it, and then raw SQL -- no ``WHERE`` at all -- runs as a role that is
neither a superuser nor BYPASSRLS, the only kind a policy binds.

Each run gets a database of its own, dropped at the end, so the generated
project's tables never meet the rest of the suite's.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from jfastframework.cli.main import app as jfast_app
from jfastframework.cli.scaffold import (
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)
from jfastframework.db.rls import TenantScopedSession, role_problem
from jfastframework.vectors.pgvector import schema_sql

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
ADMIN_DSN = f"{PG_BASE}/jfast"
ROLE = "jfast_mt_app"


def _alembic(root: Path, dsn: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env={**os.environ, "JFAST_DB_DSN": dsn},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def database() -> AsyncIterator[str]:
    """A fresh database, and the DSN of its owner."""
    admin = create_async_engine(ADMIN_DSN, isolation_level="AUTOCOMMIT")
    name = f"jfast_mt_{uuid.uuid4().hex[:10]}"
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE {name}"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await admin.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    try:
        yield f"{PG_BASE}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await admin.dispose()


@pytest.fixture
def service(tmp_path: Path) -> Path:
    """A generated single-tenant service with two modules and RAG on pgvector."""
    root = tmp_path / "shop"
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        service_trees("api", None, root),
        service_context("shop", plugins=["database", "auth", "rag"]),
    )
    for module in ("invoice", "customer"):
        scaffolder.render_trees(
            module_trees("modular", "api", root / "modules", root), module_context(module)
        )
    config = tomllib.loads((root / "jfast.toml").read_text(encoding="utf-8"))
    assert "tenancy" not in config["plugins"]["enabled"]
    return root


async def _as_app(dsn: str) -> AsyncEngine:
    """The role the service would run as after the switch -- created like the docs say."""
    owner = create_async_engine(dsn)
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                f"'{ROLE}') THEN CREATE ROLE {ROLE} LOGIN PASSWORD '{ROLE}' "
                "NOSUPERUSER NOBYPASSRLS; END IF; END $$"
            )
        )
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {ROLE}"))
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
        )
        await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}"))
    await owner.dispose()
    user, _, rest = dsn.partition("@")
    scheme = user.split("://", 1)[0]
    return create_async_engine(f"{scheme}://{ROLE}:{ROLE}@{rest}")


async def _scalar(engine: AsyncEngine, tenant: str | None, sql: str, **params: Any) -> Any:
    """One statement in a transaction scoped to *tenant*, the framework's way."""
    maker = async_sessionmaker(engine, sync_session_class=TenantScopedSession)
    async with maker() as session:
        if tenant is not None:
            session.info["tenant_id"] = tenant
        async with session.begin():
            result = await session.execute(text(sql), params)
            return result.scalar() if result.returns_rows else result.rowcount


async def test_the_switch_locks_a_second_tenant_out_at_the_database(
    service: Path, database: str
) -> None:
    # -- a single-tenant service, running, with data ------------------------
    _alembic(service, database, "revision", "--autogenerate", "-m", "init")
    _alembic(service, database, "upgrade", "head")
    owner = create_async_engine(database)
    async with owner.begin() as conn:
        for statement in schema_sql("rag_chunks", dimensions=3):
            await conn.execute(text(statement))
        await conn.execute(
            text("INSERT INTO invoices (name, is_active) VALUES ('i1', true), ('i2', true)")
        )
        await conn.execute(text("INSERT INTO customers (name, is_active) VALUES ('c1', true)"))
        await conn.execute(
            text(
                "INSERT INTO rag_chunks (document_id, chunk_index, content, embedding) "
                "VALUES ('contract', 0, 'late delivery', '[1,2,3]'), "
                "('contract', 1, 'penalty', '[3,2,1]')"
            )
        )
        nulls = await conn.execute(text("SELECT count(*) FROM invoices WHERE tenant_id IS NULL"))
        assert nulls.scalar() == 2  # single-tenant rows carry no tenant
    await owner.dispose()

    # -- the command ---------------------------------------------------------
    result = CliRunner().invoke(
        jfast_app, ["tenancy", "enable", "--tenant", "acme", "--path", str(service)]
    )
    assert result.exit_code == 0, result.output
    assert "customers, invoices" in result.output
    config = tomllib.loads((service / "jfast.toml").read_text(encoding="utf-8"))
    assert "tenancy" in config["plugins"]["enabled"]
    assert config["plugin"]["tenancy"]["sources"] == ["token", "user"]
    assert config["plugin"]["database"]["rls"] is True
    assert config["plugin"]["rag"]["tenant_scoped"] is True

    _alembic(service, database, "upgrade", "head")

    # -- proved by the database, as a role the policies bind ----------------
    app = await _as_app(database)
    try:
        assert await role_problem(app) is None

        # The initial tenant owns everything that existed, rag chunks included.
        assert await _scalar(app, "acme", "SELECT count(*) FROM invoices") == 2
        assert await _scalar(app, "acme", "SELECT count(*) FROM customers") == 1
        assert await _scalar(app, "acme", "SELECT count(*) FROM rag_chunks") == 2

        # A second tenant: no WHERE clause anywhere, and nothing comes back.
        for table in ("invoices", "customers", "rag_chunks"):
            assert await _scalar(app, "globex", f"SELECT count(*) FROM {table}") == 0, table
        # Writes to the first tenant's rows touch nothing ...
        assert await _scalar(app, "globex", "UPDATE invoices SET name = 'mine'") == 0
        assert await _scalar(app, "globex", "DELETE FROM customers") == 0
        assert await _scalar(app, "globex", "DELETE FROM rag_chunks") == 0
        # ... and a row claimed for the first tenant is refused outright.
        with pytest.raises(DBAPIError, match="row-level security"):
            await _scalar(
                app,
                "globex",
                "INSERT INTO invoices (tenant_id, name, is_active) VALUES ('acme', 'x', true)",
            )
        # Its own rows work as before.
        assert (
            await _scalar(
                app,
                "globex",
                "INSERT INTO invoices (tenant_id, name, is_active) VALUES ('globex', 'g', true)",
            )
            == 1
        )
        assert await _scalar(app, "globex", "SELECT count(*) FROM invoices") == 1
        assert await _scalar(app, "acme", "SELECT count(*) FROM invoices") == 2
        # A transaction with no tenant at all sees nothing: the failure mode of
        # a query the readiness report missed is empty, not a leak.
        assert await _scalar(app, None, "SELECT count(*) FROM invoices") == 0
        # Nothing was changed for the first tenant by the attempts above.
        assert await _scalar(app, "acme", "SELECT count(*) FROM invoices WHERE name = 'mine'") == 0
    finally:
        await app.dispose()

    # -- and the switch goes back down cleanly ------------------------------
    _alembic(service, database, "downgrade", "-1")
    owner = create_async_engine(database)
    try:
        async with owner.connect() as conn:
            enabled = await conn.execute(
                text(
                    "SELECT relname FROM pg_class WHERE relrowsecurity "
                    "AND relname IN ('invoices', 'customers', 'rag_chunks')"
                )
            )
            assert list(enabled) == []
    finally:
        await owner.dispose()
