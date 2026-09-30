"""Monthly rollups: one bucket recomputed from its rows, idempotently, in local months.

SQLite covers the arithmetic, the zone boundaries and idempotency; the
PostgreSQL test covers what only a server can -- two refreshes of the same
bucket at once, which without the lock would both insert.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jfastframework.db.base import UTCDateTime
from jfastframework.db.rollups import MonthlyRollup, month_bounds, month_of, rollup_table

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
ZONE = "America/Mexico_City"  # UTC-6 all year since 2022

metadata = MetaData()
expenses = Table(
    "rollup_test_expenses",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", String(64)),
    Column("category", String(32), nullable=False),
    Column("amount", Numeric(12, 2), nullable=False),
    Column("paid_at", UTCDateTime, nullable=False),
    Column("status", String(16), nullable=False, default="ok"),
)
monthly = rollup_table(
    "rollup_test_monthly",
    metadata,
    group_by={"category": String(32)},
    measures={"total": Numeric(14, 2), "count": Integer},
)


def _rollup() -> MonthlyRollup:
    return MonthlyRollup(
        source=expenses,
        target=monthly,
        at=expenses.c.paid_at,
        tenant=expenses.c.tenant_id,
        group_by=[expenses.c.category],
        measures={"total": func.sum(expenses.c.amount), "count": func.count()},
        where=expenses.c.status != "cancelled",
        zone=ZONE,
    )


def _at(text_: str) -> datetime:
    return datetime.fromisoformat(text_).replace(tzinfo=UTC)


async def _add(engine: AsyncEngine, *rows: tuple[Any, ...]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(expenses),
            [
                {
                    "tenant_id": tenant,
                    "category": category,
                    "amount": Decimal(amount),
                    "paid_at": _at(at),
                    "status": status,
                }
                for tenant, category, amount, at, *rest in rows
                for status in [rest[0] if rest else "ok"]
            ],
        )


async def _buckets(engine: AsyncEngine) -> set[tuple[Any, ...]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                monthly.c.tenant_id,
                monthly.c.month,
                monthly.c.category,
                monthly.c.total,
                monthly.c["count"],
            )
        )
        return {(t, m, c, Decimal(str(total)), n) for t, m, c, total, n in rows}


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    built = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollups.db'}")
    async with built.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield built
    await built.dispose()


def test_months_are_local_months() -> None:
    # 02:00 UTC on 1 October is 20:00 on 30 September in Mexico City.
    assert month_of(_at("2026-10-01T02:00:00"), ZONE) == date(2026, 9, 1)
    assert month_of(_at("2026-10-01T06:00:00"), ZONE) == date(2026, 10, 1)
    start, end = month_bounds(date(2026, 9, 1), ZONE)
    assert (start, end) == (_at("2026-09-01T06:00:00"), _at("2026-10-01T06:00:00"))
    # December rolls into January of the next year.
    assert month_bounds(date(2026, 12, 1), "UTC")[1] == _at("2027-01-01T00:00:00")
    with pytest.raises(ValueError, match="naive"):
        month_of(datetime(2026, 9, 1), ZONE)


async def test_a_refresh_writes_one_bucket_from_its_rows(engine: AsyncEngine) -> None:
    await _add(
        engine,
        ("acme", "fuel", "100.50", "2026-09-10T12:00:00"),
        ("acme", "fuel", "20.25", "2026-09-30T23:00:00"),  # still September locally
        ("acme", "food", "7.00", "2026-09-11T12:00:00"),
        ("acme", "food", "999.00", "2026-09-12T12:00:00", "cancelled"),
        ("acme", "fuel", "1.00", "2026-08-31T12:00:00"),  # August
        ("globex", "fuel", "50.00", "2026-09-10T12:00:00"),  # another tenant
    )
    async with engine.begin() as conn:
        written = await _rollup().refresh(conn, tenant_id="acme", month=date(2026, 9, 15))
    assert written == 2
    assert await _buckets(engine) == {
        ("acme", date(2026, 9, 1), "fuel", Decimal("120.75"), 2),
        ("acme", date(2026, 9, 1), "food", Decimal("7.00"), 1),
    }


async def test_a_refresh_is_idempotent_and_drops_vanished_groups(engine: AsyncEngine) -> None:
    await _add(
        engine,
        ("acme", "fuel", "10.00", "2026-09-10T12:00:00"),
        ("acme", "food", "5.00", "2026-09-10T12:00:00"),
    )
    rollup = _rollup()
    moment = _at("2026-09-10T12:00:00")
    for _ in range(3):  # an event delivered three times
        async with engine.begin() as conn:
            await rollup.refresh_at(conn, tenant_id="acme", moment=moment)
    assert len(await _buckets(engine)) == 2

    async with engine.begin() as conn:
        await conn.execute(delete(expenses).where(expenses.c.category == "food"))
        await rollup.refresh_at(conn, tenant_id="acme", moment=moment)
    assert await _buckets(engine) == {("acme", date(2026, 9, 1), "fuel", Decimal("10.00"), 1)}


async def test_a_month_with_no_rows_leaves_no_bucket(engine: AsyncEngine) -> None:
    ungrouped = rollup_table(
        "rollup_test_totals", metadata, group_by={}, measures={"total": Numeric(14, 2)}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    rollup = MonthlyRollup(
        source=expenses,
        target=ungrouped,
        at=expenses.c.paid_at,
        tenant=expenses.c.tenant_id,
        measures={"total": func.sum(expenses.c.amount)},
        zone=ZONE,
    )
    await _add(engine, ("acme", "fuel", "3.00", "2026-09-10T12:00:00"))
    async with engine.begin() as conn:
        assert await rollup.refresh(conn, tenant_id="acme", month=date(2026, 9, 1)) == 1
        assert await rollup.refresh(conn, tenant_id="acme", month=date(2026, 7, 1)) == 0
        count = await conn.scalar(select(func.count()).select_from(ungrouped))
    assert count == 1


async def test_single_tenant_rows_roll_up_under_a_null_tenant(engine: AsyncEngine) -> None:
    await _add(
        engine,
        (None, "fuel", "4.00", "2026-09-10T12:00:00"),
        ("acme", "fuel", "9.00", "2026-09-10T12:00:00"),
    )
    async with engine.begin() as conn:
        await _rollup().refresh(conn, tenant_id=None, month=date(2026, 9, 1))
        await _rollup().refresh(conn, tenant_id=None, month=date(2026, 9, 1))
    assert await _buckets(engine) == {(None, date(2026, 9, 1), "fuel", Decimal("4.00"), 1)}


async def test_rebuild_refreshes_every_month_in_the_range(engine: AsyncEngine) -> None:
    await _add(
        engine,
        ("acme", "fuel", "1.00", "2026-11-10T12:00:00"),
        ("acme", "fuel", "2.00", "2026-12-10T12:00:00"),
        ("acme", "fuel", "3.00", "2027-01-10T12:00:00"),
    )
    async with engine.begin() as conn:
        written = await _rollup().rebuild(
            conn, tenant_id="acme", since=date(2026, 10, 1), until=date(2027, 1, 31)
        )
    assert written == 3
    months = {m for _, m, *_ in await _buckets(engine)}
    assert months == {date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)}


def test_a_target_without_the_declared_columns_is_refused() -> None:
    wrong = rollup_table("rollup_test_wrong", MetaData(), group_by={}, measures={"sum": Integer})
    with pytest.raises(ValueError, match="total"):
        MonthlyRollup(
            source=expenses,
            target=wrong,
            at=expenses.c.paid_at,
            tenant=expenses.c.tenant_id,
            measures={"total": func.sum(expenses.c.amount)},
        )


async def test_concurrent_refreshes_of_one_bucket_on_postgres() -> None:
    """Ten handlers of the same event at once: one row per group, no conflict."""
    engine = create_async_engine(f"{PG_BASE}/jfast", pool_size=12)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TABLE IF EXISTS rollup_test_monthly, rollup_test_expenses")
            )
            await conn.run_sync(lambda sync: metadata.create_all(sync, tables=[expenses, monthly]))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    try:
        await _add(
            engine,
            ("acme", "fuel", "10.00", "2026-09-10T12:00:00"),
            ("acme", "food", "5.00", "2026-09-11T12:00:00"),
        )
        rollup = _rollup()

        async def handler() -> None:
            async with engine.begin() as conn:
                await rollup.refresh(conn, tenant_id="acme", month=date(2026, 9, 1))

        await asyncio.gather(*(handler() for _ in range(10)))
        assert await _buckets(engine) == {
            ("acme", date(2026, 9, 1), "fuel", Decimal("10.00"), 1),
            ("acme", date(2026, 9, 1), "food", Decimal("5.00"), 1),
        }
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TABLE IF EXISTS rollup_test_monthly, rollup_test_expenses")
            )
        await engine.dispose()
