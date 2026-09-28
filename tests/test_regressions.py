"""Defects that reached a published release, and the checks that keep them out.

Every one of the first six shipped in 0.1.0a1. None was caught by the existing
suite, because the suite installs the framework from the checkout with every
extra present and never drives a generated service through a real request.
These are the cheapest possible guards for the exact failures.

The last one never raised anywhere: it is a report that answers a different day
depending on which region the container runs in. It needs a real PostgreSQL and
is skipped without one.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import DateTime, func, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework.db.base import TimestampMixin
from jfastframework.db.repository import BaseRepository
from jfastframework.deploy.compose import render_dockerfile
from jfastframework.plugins.builtin.database import (
    DatabasePlugin,
    connect_args_for,
    server_timezone,
    session_dependency,
)
from jfastframework.testing import build_test_app
from jfastframework.workspace import WORKSPACE_FILE, Workspace

# -- the session dependency must not become a query parameter ----------


def test_session_dependency_is_not_a_query_parameter() -> None:
    """Annotated `Any`, FastAPI made `request` a required query parameter.

    Every route that took a database session then answered 422 to every call.
    Nothing raised; the schema was simply wrong.
    """
    app = FastAPI()

    @app.get("/items")
    async def list_items(
        session: object = Depends(session_dependency, scope="function"),
    ) -> dict[str, str]:
        return {"ok": "yes"}

    parameters = app.openapi()["paths"]["/items"]["get"].get("parameters", [])
    names = [p["name"] for p in parameters]
    assert "request" not in names, f"`request` leaked into the schema as {parameters}"
    assert parameters == [], f"the dependency contributed parameters: {parameters}"


# -- the Dockerfile must build what the generator writes ---------------


def test_dockerfile_copies_are_optional() -> None:
    """`COPY pyproject.toml ./` failed: the generator only writes requirements."""
    dockerfile = render_dockerfile()
    assert "COPY pyproject.toml* requirements.txt* ./" in dockerfile
    assert "COPY pyproject.toml ./" not in dockerfile
    assert "COPY requirements.txt ./" not in dockerfile


def test_the_compiler_does_not_reach_the_final_image() -> None:
    """build-essential is ~200 MB and a toolchain for whoever gets a shell."""
    dockerfile = render_dockerfile()
    builder, _, runtime = dockerfile.partition("FROM python:3.12-slim AS runtime")

    assert "build-essential" in builder, "something has to build the wheels"
    assert runtime, "the image is single-stage again"
    assert "build-essential" not in runtime
    assert "COPY --from=builder /opt/venv /opt/venv" in runtime


def test_the_image_runs_as_a_non_root_user() -> None:
    dockerfile = render_dockerfile()
    assert dockerfile.rstrip().endswith('CMD ["/entrypoint.sh"]')
    # The last USER before the CMD decides who the process is.
    assert dockerfile.rindex("USER appuser") > dockerfile.rindex("useradd")


def test_the_image_migrates_before_it_serves() -> None:
    """A container against an empty database answers 500 to everything."""
    dockerfile = render_dockerfile()
    assert "alembic upgrade head" in dockerfile
    assert "set -e" in dockerfile, "a failed migration must stop the container"
    assert "exec uvicorn" in dockerfile, "uvicorn must be PID 1 to receive SIGTERM"


# -- writes must not expire a column into a lazy load ------------------


class Base(DeclarativeBase):
    pass


class Thing(TimestampMixin, Base):
    __tablename__ = "regression_things"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="")
    stamped: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ThingRepository(BaseRepository[Thing]):
    model = Thing
    tenant_scoped = False


async def test_a_server_default_is_readable_after_update() -> None:
    """`onupdate` expired `updated_at`; reading it raised MissingGreenlet.

    The read is normally Pydantic serialising the response, so every PATCH
    returned 500 while the write itself had succeeded.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with maker() as session:
            repository = ThingRepository(session)
            thing = await repository.create(label="before")
            assert thing.updated_at is not None

            await repository.update(thing, label="after")
            # This attribute access is the whole test.
            assert thing.updated_at is not None
            assert thing.stamped is not None
    finally:
        await engine.dispose()


def test_the_mixin_asks_for_eager_defaults() -> None:
    assert TimestampMixin.__mapper_args__.get("eager_defaults") is True


# -- the workspace search must not leave the project -------------------


def test_the_search_stops_at_a_repository_root(tmp_path: Path) -> None:
    """A workspace above a `.git` belongs to a different project."""
    outer = tmp_path / "outer"
    project = outer / "project"
    nested = project / "service"
    nested.mkdir(parents=True)
    (outer / WORKSPACE_FILE).write_text("[workspace]\nname='outer'\n", encoding="utf-8")
    (project / ".git").mkdir()

    assert Workspace.find(nested) is None


def test_a_workspace_inside_the_project_is_still_found(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "service" / "modules"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()
    marker = project / WORKSPACE_FILE
    marker.write_text("[workspace]\nname='p'\n", encoding="utf-8")

    assert Workspace.find(nested) == marker


def test_the_home_directory_is_never_a_workspace(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Running `jfast start` once in $HOME used to enrol every project below it."""
    home = tmp_path / "home"
    work = home / "somewhere" / "deep"
    work.mkdir(parents=True)
    (home / WORKSPACE_FILE).write_text("[workspace]\nname='accident'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert Workspace.find(work) is None


# -- a daily report must not depend on where the container runs --------

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
# Its own database, because the whole point is a server-side TimeZone that is
# deliberately wrong, and no other test should inherit it.
TZ_DATABASE = "jfast_tz"
TZ_DSN = f"{PG_BASE}/{TZ_DATABASE}"
SERVER_ZONE = "America/Santiago"

# 02:30 UTC on the 1st is 23:30 on the previous day in Santiago, so the two
# readings of this instant do not even land in the same month.
INSTANT = "timestamptz '2026-03-01 02:30:00+00'"
REPORT_DAY = text(
    f"SELECT current_setting('TimeZone'), date_trunc('day', {INSTANT})::date, ({INSTANT})::date"
)


async def _non_utc_database() -> bool:
    """Create the database if it is missing and set its TimeZone. False if no server."""
    engine = create_async_engine(f"{PG_BASE}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TZ_DATABASE}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TZ_DATABASE}"'))
            # A database-level setting, so it applies to connections this test
            # has not opened yet -- which is what a differently configured
            # server looks like from the client's side.
            await conn.execute(
                text(f"ALTER DATABASE \"{TZ_DATABASE}\" SET TimeZone = '{SERVER_ZONE}'")
            )
        return True
    except Exception:  # noqa: BLE001 - any failure means "no server here"
        return False
    finally:
        await engine.dispose()


@pytest.fixture
async def non_utc_postgres() -> None:
    if not await _non_utc_database():
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    return None


async def test_an_unpinned_session_reports_the_wrong_day(non_utc_postgres: None) -> None:
    """The failure, asserted as a failure. Nothing raises; the date is just wrong.

    This is what every connection did before the pin, and what any other client
    of this database still does. Two replicas configured differently produce
    two different daily totals from byte-identical rows, and the only symptom
    is a reconciliation that does not add up weeks later.
    """
    engine = create_async_engine(TZ_DSN)
    try:
        async with engine.connect() as conn:
            session_tz, truncated, as_date = (await conn.execute(REPORT_DAY)).one()
    finally:
        await engine.dispose()

    assert session_tz == SERVER_ZONE
    assert str(truncated) == "2026-02-28"
    assert str(as_date) == "2026-02-28"


async def test_the_plugin_pins_every_session_to_utc(non_utc_postgres: None) -> None:
    """The same server, the same row, through an engine the plugin built."""
    app = build_test_app()
    DatabasePlugin({"dsn": TZ_DSN}).register(app.state.jfast)
    engine: Any = app.state.jfast.require("db.engine")

    try:
        async with engine.connect() as conn:
            session_tz, truncated, as_date = (await conn.execute(REPORT_DAY)).one()
    finally:
        await engine.dispose()

    assert session_tz == "UTC"
    assert str(truncated) == "2026-03-01"
    assert str(as_date) == "2026-03-01"


async def test_doctor_can_see_both_sides_of_the_pin(non_utc_postgres: None) -> None:
    """The pair `jfast doctor` needs: what the server hands out, and what we use."""
    assert await server_timezone(TZ_DSN) == (SERVER_ZONE, "UTC")


async def test_opting_out_leaves_the_server_setting_alone(non_utc_postgres: None) -> None:
    """An empty session_timezone must not quietly keep pinning."""
    app = build_test_app()
    DatabasePlugin({"dsn": TZ_DSN, "session_timezone": ""}).register(app.state.jfast)
    engine: Any = app.state.jfast.require("db.engine")
    try:
        async with engine.connect() as conn:
            session_tz = await conn.scalar(text("SELECT current_setting('TimeZone')"))
    finally:
        await engine.dispose()

    assert session_tz == SERVER_ZONE


def test_a_non_postgres_driver_is_given_no_server_settings() -> None:
    """`server_settings` is an asyncpg argument; another driver rejects it."""
    assert connect_args_for("sqlite+aiosqlite:///x.db", session_timezone="UTC") == {}
    assert connect_args_for(TZ_DSN, session_timezone="UTC") == {
        "server_settings": {"timezone": "UTC"}
    }
    assert connect_args_for(TZ_DSN, session_timezone="UTC", read_only=True) == {
        "server_settings": {"timezone": "UTC", "default_transaction_read_only": "on"}
    }
