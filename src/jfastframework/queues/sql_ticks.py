"""Schedule ticks claimed in the database: ``jfast_schedule_ticks``.

One row per tick fired, primary key ``(name, fire_at)``. A replica claims a
tick by inserting its row with ``ON CONFLICT DO NOTHING``; the insert that
wrote a row is the one that fires. The table is created at startup like the
other framework tables, and the generated Alembic ``env.py`` leaves it alone.

Requires: ``pip install jfastframework[db]``
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Column, PrimaryKeyConstraint, String, Table, delete, func, select

from jfastframework.db.base import UTCDateTime
from jfastframework.db.framework import ensure_tables, framework_metadata, insert_ignoring_conflicts
from jfastframework.queues.scheduler import Enqueue, default_owner

__all__ = ["TICKS_TABLE", "SqlTickStore", "ticks"]

TICKS_TABLE = "jfast_schedule_ticks"

ticks = Table(
    TICKS_TABLE,
    framework_metadata,
    Column("name", String(255), nullable=False),
    Column("fire_at", UTCDateTime, nullable=False),
    Column("claimed_by", String(255)),
    Column("claimed_at", UTCDateTime, nullable=False, server_default=func.now()),
    # The primary key is the claim. The same index answers "latest tick of
    # this schedule", which is all catch-up ever asks.
    PrimaryKeyConstraint("name", "fire_at"),
)


class SqlTickStore:
    kind = "database"

    def __init__(self, engine: Any, *, owner: str | None = None) -> None:
        self._engine = engine
        self._owner = owner or default_owner()

    @property
    def engine(self) -> Any:
        """Compared with the queue's, to enqueue in the claim's transaction."""
        return self._engine

    async def setup(self) -> None:
        await ensure_tables(self._engine, TICKS_TABLE)

    async def last(self, name: str) -> datetime | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(func.max(ticks.c.fire_at)).where(ticks.c.name == name)
            )
            latest: datetime | None = result.scalar()
        return latest

    async def fire(
        self, name: str, fire_at: datetime, *, hold: timedelta, enqueue: Enqueue
    ) -> bool:
        async with self._engine.begin() as conn:
            claim = insert_ignoring_conflicts(conn.dialect.name, ticks).values(
                name=name, fire_at=fire_at, claimed_by=self._owner
            )
            result = await conn.execute(claim)
            if result.rowcount != 1:
                return False
            # Inside the claim's transaction: if this raises, the claim rolls
            # back with it and the tick is still there to fire.
            await enqueue(conn)
        return True

    async def prune(self, before: datetime) -> None:
        # Each schedule's newest row stays however old it is: it is what tells
        # a restarted service that a monthly job already ran.
        newest = ticks.alias("newest")
        latest_of_schedule = (
            select(func.max(newest.c.fire_at))
            .where(newest.c.name == ticks.c.name)
            .scalar_subquery()
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(ticks).where(ticks.c.fire_at < before, ticks.c.fire_at < latest_of_schedule)
            )
