"""Read/write split, and the pinning without which it is a latent bug.

Reads on a replica are free throughput right up to the moment somebody saves a
row and reads it back. The replica is behind, the row is not there, and the
bug only appears under load on a machine nobody is looking at. So the split
here is inseparable from the pin: after a write, that client's reads go to the
primary for a bounded window.

Routing is what these tests assert -- which engine served the request -- against
two SQLite files standing in for two servers. Whether the pin actually covers
replication lag is a PostgreSQL question and is asserted in
``test_database_postgres.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework.plugins.builtin.database import (
    PIN_HEADER,
    DatabasePlugin,
    ReadOnlySessionError,
    read_session_dependency,
    session_dependency,
)
from jfastframework.testing import build_test_app, client_for


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="")


async def _build(tmp_path: Path, **overrides: Any) -> Any:
    """An app with a primary and a replica, both empty and both real files."""
    primary = f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    replica = f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}"
    config: dict[str, Any] = {
        "read_write_split": True,
        "connections": {
            "primary": {"dsn": primary},
            "replica": {"dsn": replica, "read_only": True},
        },
    }
    config.update(overrides)

    app = build_test_app()
    DatabasePlugin(config).register(app.state.jfast)
    databases = app.state.jfast.require("db.databases")

    for name in databases.names:
        async with databases.engine(name).begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @app.get("/where")
    async def where(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, str]:
        return {"bind": str(session.get_bind().url)}

    @app.get("/items")
    async def items(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, list[str]]:
        rows = await session.execute(select(Item.label))
        return {"labels": [row[0] for row in rows]}

    @app.post("/items")
    async def create(
        session: Any = Depends(session_dependency, scope="function"),
    ) -> dict[str, str]:
        session.add(Item(label="one"))
        return {"bind": str(session.get_bind().url)}

    @app.post("/write-through-raw-sql")
    async def raw(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, str]:
        session.add(Item(label="smuggled"))
        return {"ok": "no"}

    return app


# -- routing -------------------------------------------------------------


async def test_a_read_is_served_by_the_replica(tmp_path: Path) -> None:
    app = await _build(tmp_path)
    async with client_for(app) as client:
        body = (await client.get("/where")).json()
    assert body["bind"].endswith("replica.db")


async def test_a_write_is_served_by_the_primary(tmp_path: Path) -> None:
    app = await _build(tmp_path)
    async with client_for(app) as client:
        body = (await client.post("/items")).json()
    assert body["bind"].endswith("primary.db")


async def test_without_the_split_a_read_uses_the_only_database(tmp_path: Path) -> None:
    """The unnamed configuration has no replica, and nothing changes for it."""
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'only.db'}"
    app = build_test_app()
    plugin = DatabasePlugin({"dsn": dsn})
    plugin.register(app.state.jfast)

    @app.get("/where")
    async def where(
        session: Any = Depends(read_session_dependency, scope="function"),
    ) -> dict[str, str]:
        return {"bind": str(session.get_bind().url)}

    async with client_for(app) as client:
        body = (await client.get("/where")).json()
    assert body["bind"].endswith("only.db")


# -- the pin -------------------------------------------------------------


async def test_the_read_after_a_write_is_served_by_the_primary(tmp_path: Path) -> None:
    """Save, redirect, read. The row must be there."""
    app = await _build(tmp_path)
    async with client_for(app) as client:
        await client.post("/items")
        where = (await client.get("/where")).json()
        items = (await client.get("/items")).json()

    assert where["bind"].endswith("primary.db")
    assert items["labels"] == ["one"]


async def test_a_client_that_did_not_write_is_not_pinned(tmp_path: Path) -> None:
    """A pin for everybody is a split for nobody."""
    app = await _build(tmp_path)
    async with client_for(app) as writer:
        await writer.post("/items")
    async with client_for(app) as reader:
        body = (await reader.get("/where")).json()
    assert body["bind"].endswith("replica.db")


async def test_the_pin_expires(tmp_path: Path) -> None:
    """The write hands back a deadline, and past it the replica serves again.

    The pin is a wall-clock deadline, so a test that asserts *both* directions
    against one window is racing the runner: with `pin_window = 0.2` the "still
    pinned" leg needs the gap between two HTTP calls to stay under 200ms, and a
    cold Windows agent spent a second there. It failed in CI asserting the
    replica was the primary -- a correctly expired pin, read as a routing bug.

    Split by direction, because the two are not equally fragile. The token
    below proves the pin was established without consulting a clock at all, and
    the expiry leg only needs *at least* the window to have passed, which a slow
    machine helps rather than breaks. That the pin routes to the primary while
    it is live is `test_the_read_after_a_write_is_served_by_the_primary`, at the
    default window.
    """
    app = await _build(tmp_path, pin_window=0.2)
    async with client_for(app) as client:
        written = await client.post("/items")
        assert PIN_HEADER in written.headers

        await asyncio.sleep(0.35)
        assert (await client.get("/where")).json()["bind"].endswith("replica.db")


async def test_a_forged_pin_cannot_outlast_the_window(tmp_path: Path) -> None:
    """The cookie is client-controlled, so its value is clamped, not trusted."""
    app = await _build(tmp_path, pin_window=0.2)
    async with client_for(app) as client:
        client.cookies.set("jfast_rw", "99999999999")
        # Reading the primary is harmless in itself -- the clamp is what stops
        # one client holding a permanent share of the primary's capacity.
        assert (await client.get("/where")).json()["bind"].endswith("primary.db")
        client.cookies.delete("jfast_rw")
        await asyncio.sleep(0.35)
        assert (await client.get("/where")).json()["bind"].endswith("replica.db")


async def test_an_api_client_carries_the_pin_as_a_header(tmp_path: Path) -> None:
    """A client that keeps no cookies is handed the same token in a header."""
    app = await _build(tmp_path)
    async with client_for(app) as client:
        written = await client.post("/items")
        token = written.headers[PIN_HEADER]
        client.cookies.clear()

        loose = (await client.get("/where")).json()
        carried = (await client.get("/where", headers={PIN_HEADER: token})).json()

    assert loose["bind"].endswith("replica.db")
    assert carried["bind"].endswith("primary.db")


# -- the replica is not a place to write ---------------------------------


async def test_a_write_smuggled_onto_a_read_session_is_refused(tmp_path: Path) -> None:
    app = await _build(tmp_path)
    async with client_for(app) as client:
        with pytest.raises(ReadOnlySessionError):
            await client.post("/write-through-raw-sql")
