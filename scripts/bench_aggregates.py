"""A dashboard's aggregates on a large table: computed per request vs rolled up.

    python scripts/bench_aggregates.py --pg postgresql+asyncpg://jfast:jfast@localhost:5487 \\
        --rows 3000000 --tenants 1000 --big-share 0.33

Builds an ``expenses`` table (tenant, category, amount, paid_at, status)
over 36 months, with one large tenant holding ``--big-share`` of the rows
and the rest spread evenly -- the shape that hurts, because the average
tenant looks fine and the customer who pays most does not.

The panel is six aggregates, the ones a spending dashboard shows (Cuadra's
runs six): this month by category, the last twelve months, year to date,
this month's count, average ticket, top category of the quarter. It is timed
two ways for the big tenant and for a typical one:

* **on the fly** -- each aggregate over ``expenses``, with the index a
  careful team would add, ``(tenant_id, paid_at)``;
* **rolled up** -- the same six over the monthly table kept by
  :class:`jfastframework.db.rollups.MonthlyRollup`.

Also measured: what keeping the rollup costs -- one bucket refresh, which is
what an event handler pays per write -- and a full rebuild of a tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import UTC, date, datetime
from typing import Any

MONTHS = 36
CATEGORIES = 12


def _pct(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(_pct(values, 0.95), 2),
    }


ON_THE_FLY = [
    # this month by category
    """SELECT category, sum(amount), count(*) FROM expenses
       WHERE tenant_id = :t AND status <> 'cancelled' AND paid_at >= :m0 AND paid_at < :m1
       GROUP BY category""",
    # last twelve months
    """SELECT date_trunc('month', paid_at AT TIME ZONE 'America/Mexico_City') AS m, sum(amount)
       FROM expenses WHERE tenant_id = :t AND status <> 'cancelled'
       AND paid_at >= :y0 AND paid_at < :m1 GROUP BY m ORDER BY m""",
    # year to date
    """SELECT sum(amount) FROM expenses WHERE tenant_id = :t AND status <> 'cancelled'
       AND paid_at >= :ytd AND paid_at < :m1""",
    # this month's count
    """SELECT count(*) FROM expenses WHERE tenant_id = :t AND status <> 'cancelled'
       AND paid_at >= :m0 AND paid_at < :m1""",
    # average ticket this month
    """SELECT avg(amount) FROM expenses WHERE tenant_id = :t AND status <> 'cancelled'
       AND paid_at >= :m0 AND paid_at < :m1""",
    # top category of the quarter
    """SELECT category, sum(amount) AS s FROM expenses WHERE tenant_id = :t
       AND status <> 'cancelled' AND paid_at >= :q0 AND paid_at < :m1
       GROUP BY category ORDER BY s DESC LIMIT 1""",
]

ROLLED_UP = [
    """SELECT category, total, "count" FROM expenses_monthly
       WHERE tenant_id = :t AND month = :month""",
    """SELECT month, sum(total) FROM expenses_monthly WHERE tenant_id = :t
       AND month > :y0d AND month <= :month GROUP BY month ORDER BY month""",
    """SELECT sum(total) FROM expenses_monthly WHERE tenant_id = :t
       AND month >= :ytdd AND month <= :month""",
    """SELECT sum("count") FROM expenses_monthly WHERE tenant_id = :t AND month = :month""",
    """SELECT sum(total) / nullif(sum("count"), 0) FROM expenses_monthly
       WHERE tenant_id = :t AND month = :month""",
    """SELECT category, sum(total) AS s FROM expenses_monthly WHERE tenant_id = :t
       AND month > :q0d AND month <= :month GROUP BY category ORDER BY s DESC LIMIT 1""",
]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from sqlalchemy import (
        BigInteger,
        Column,
        Integer,
        MetaData,
        Numeric,
        String,
        Table,
        func,
        text,
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    from jfastframework.db.base import UTCDateTime
    from jfastframework.db.rollups import MonthlyRollup, month_bounds, rollup_table

    admin = create_async_engine(f"{args.pg}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        if not await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": args.database}
        ):
            await conn.execute(text(f'CREATE DATABASE "{args.database}"'))
    await admin.dispose()
    engine = create_async_engine(f"{args.pg}/{args.database}", pool_size=4)

    metadata = MetaData()
    expenses = Table(
        "expenses",
        metadata,
        Column("id", BigInteger, primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("category", String(32), nullable=False),
        Column("amount", Numeric(12, 2), nullable=False),
        Column("paid_at", UTCDateTime, nullable=False),
        Column("status", String(16), nullable=False),
    )
    monthly = rollup_table(
        "expenses_monthly",
        metadata,
        group_by={"category": String(32)},
        measures={"total": Numeric(14, 2), "count": Integer},
    )
    rollup = MonthlyRollup(
        source=expenses,
        target=monthly,
        at=expenses.c.paid_at,
        tenant=expenses.c.tenant_id,
        group_by=[expenses.c.category],
        measures={"total": func.sum(expenses.c.amount), "count": func.count()},
        where=expenses.c.status != "cancelled",
        zone="America/Mexico_City",
    )

    result: dict[str, Any] = {
        "config": {"rows": args.rows, "tenants": args.tenants, "big_share": args.big_share}
    }
    # "Now" is fixed, so every run times the same months.
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    first = datetime(2023, 10, 1, 6, tzinfo=UTC)

    started = time.perf_counter()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS expenses, expenses_monthly"))
        await conn.run_sync(metadata.create_all)
        big = int(args.rows * args.big_share)
        rest = args.rows - big
        # Set-based: generating 3M rows through the driver would time the
        # driver. Amounts and categories are pseudo-random but repeatable.
        await conn.execute(
            text(
                """
                INSERT INTO expenses (id, tenant_id, category, amount, paid_at, status)
                SELECT g,
                       CASE WHEN g <= :big THEN 'big'
                            ELSE 't' || lpad(((g - :big) % (:tenants - 1))::text, 4, '0') END,
                       'cat' || (hashint4(g::int) & 2147483647) % :cats,
                       round(((hashint4((g * 7)::int) & 2147483647) % 500000) / 100.0, 2),
                       CAST(:first AS timestamptz) + ((hashint4((g * 13)::int) & 2147483647) % (:months * 30 * 86400))
                                * interval '1 second',
                       CASE WHEN g % 50 = 0 THEN 'cancelled' ELSE 'ok' END
                FROM generate_series(1, :rows) AS g
                """
            ),
            {
                "big": big,
                "tenants": args.tenants,
                "cats": CATEGORIES,
                "first": first,
                "months": MONTHS,
                "rows": big + rest,
            },
        )
        await conn.execute(
            text("CREATE INDEX ix_expenses_tenant_paid ON expenses (tenant_id, paid_at)")
        )
        await conn.execute(text("ANALYZE expenses"))
    result["load_s"] = round(time.perf_counter() - started, 1)
    print(f"loaded {args.rows:,} rows in {result['load_s']} s", flush=True)

    typical = "t0042"
    month = date(2026, 9, 1)
    m0, m1 = month_bounds(month, "America/Mexico_City")
    params = {
        "m0": m0,
        "m1": m1,
        "y0": month_bounds(date(2025, 10, 1), "America/Mexico_City")[0],
        "ytd": month_bounds(date(2026, 1, 1), "America/Mexico_City")[0],
        "q0": month_bounds(date(2026, 7, 1), "America/Mexico_City")[0],
        "month": month,
        "y0d": date(2025, 9, 1),
        "ytdd": date(2026, 1, 1),
        "q0d": date(2026, 6, 1),
    }

    counts = {}
    async with engine.connect() as conn:
        for tenant in ("big", typical):
            counts[tenant] = await conn.scalar(
                text("SELECT count(*) FROM expenses WHERE tenant_id = :t"), {"t": tenant}
            )
    result["rows_per_tenant"] = counts

    # Keeping the rollup: a full rebuild of each measured tenant, then the
    # per-event cost -- one bucket.
    result["rebuild_s"] = {}
    for tenant in ("big", typical):
        started = time.perf_counter()
        async with engine.begin() as conn:
            await rollup.rebuild(
                conn, tenant_id=tenant, since=date(2023, 9, 1), until=date(2026, 10, 1)
            )
        result["rebuild_s"][tenant] = round(time.perf_counter() - started, 2)
    result["refresh_one_bucket"] = {}
    for tenant in ("big", typical):
        timings = []
        for _ in range(args.reps):
            started = time.perf_counter()
            async with engine.begin() as conn:
                await rollup.refresh_at(conn, tenant_id=tenant, moment=now)
            timings.append((time.perf_counter() - started) * 1000)
        result["refresh_one_bucket"][tenant] = _summary(timings)
    print(f"rollup upkeep: {json.dumps(result['refresh_one_bucket'])}", flush=True)

    async def panel(queries: list[str], tenant: str) -> float:
        started = time.perf_counter()
        async with engine.connect() as conn:
            for query in queries:
                await conn.execute(text(query), {**params, "t": tenant})
        return (time.perf_counter() - started) * 1000

    # The two ways must agree, or the faster one is measuring a bug.
    async with engine.connect() as conn:
        live = await conn.scalar(text(ON_THE_FLY[2]), {**params, "t": "big"})
        rolled = await conn.scalar(text(ROLLED_UP[2]), {**params, "t": "big"})
    if live != rolled:
        raise RuntimeError(f"year to date differs: on the fly {live}, rolled up {rolled}")

    result["panel_six_aggregates"] = {}
    for tenant in ("big", typical):
        for name, queries in (("on_the_fly", ON_THE_FLY), ("rolled_up", ROLLED_UP)):
            await panel(queries, tenant)  # warm
            timings = [await panel(queries, tenant) for _ in range(args.reps)]
            result["panel_six_aggregates"][f"{tenant}/{name}"] = _summary(timings)
            print(f"panel {tenant}/{name}: {_summary(timings)}", flush=True)

    async with engine.connect() as conn:
        sizes = (
            await conn.execute(
                text(
                    "SELECT pg_total_relation_size('expenses'), "
                    "pg_total_relation_size('expenses_monthly')"
                )
            )
        ).one()
    result["size_mb"] = {
        "expenses": round(sizes[0] / 2**20, 1),
        "expenses_monthly": round(sizes[1] / 2**20, 2),
    }
    await engine.dispose()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pg", default=os.environ.get("JFAST_TEST_PG_URL", ""))
    parser.add_argument("--database", default="jfast_aggregates_bench")
    parser.add_argument("--rows", type=int, default=3_000_000)
    parser.add_argument("--tenants", type=int, default=1000)
    parser.add_argument("--big-share", type=float, default=0.33)
    parser.add_argument("--reps", type=int, default=30)
    parser.add_argument("--json", type=str)
    args = parser.parse_args(argv)
    if not args.pg:
        parser.error("--pg (or JFAST_TEST_PG_URL) is required")
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, default=str))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
