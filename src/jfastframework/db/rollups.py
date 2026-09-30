"""Monthly totals kept in a table of their own, so a dashboard stops scanning.

A panel that sums a tenant's expenses by category for this month, the last
twelve months and the year runs the same aggregates on every request. At a
few hundred rows nobody notices; at a million rows per tenant it is a scan
per widget per page view. The answer is old and boring: keep the totals.

:class:`MonthlyRollup` keeps one row per ``(tenant, month, group...)`` with
the measures you declare, and refreshes **one bucket at a time from the
source rows**::

    expenses_monthly = rollup_table(
        "expenses_monthly", metadata,
        group_by={"category": String(64)},
        measures={"total": Numeric(14, 2), "count": Integer},
    )
    rollup = MonthlyRollup(
        source=expenses, target=expenses_monthly,
        at=expenses.c.paid_at, tenant=expenses.c.tenant_id,
        group_by=[expenses.c.category],
        measures={"total": func.sum(expenses.c.amount), "count": func.count()},
        zone="America/Mexico_City",
    )

    # In the handler of "expense.recorded" (or after the write, in the same
    # transaction):
    await rollup.refresh_at(session, tenant_id=event.tenant_id, moment=paid_at)

Three decisions, each the reason this is a helper rather than a snippet:

**Recompute the bucket, never add a delta.** ``total = total + :amount`` is
cheaper and wrong the first time an event is delivered twice -- and the
queue and the outbox deliver at least once, never exactly once. Recomputing
one tenant-month from its rows is idempotent: run it twice, or late, or out
of order, and the bucket ends up right. It costs one indexed range scan of
that month, not of the table.

**One bucket at a time, serialised.** Two refreshes of the same bucket take
an advisory lock on it (PostgreSQL), so the second one waits and then reads
what the first committed instead of both inserting.

**Months in the business zone.** An expense at 02:00 UTC on the 1st belongs
to the previous month in Mexico City. The bounds are the local month's first
instant to the next month's, in UTC, half-open -- the same rule as
:func:`jfastframework.time.day_bounds`.

For totals that drifted (a bulk import that skipped the events, a bug) run
:meth:`MonthlyRollup.rebuild` from a scheduled job; it is the same refresh,
month by month.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, tzinfo
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    delete,
    func,
    insert,
    literal,
    select,
    text,
)
from sqlalchemy.sql.elements import ColumnElement

from jfastframework.time import zone as resolve_zone

__all__ = ["MonthlyRollup", "month_bounds", "month_of", "rollup_table"]


def month_of(moment: datetime, tz: str | tzinfo | None = None) -> date:
    """The first day of the local month ``moment`` falls in."""
    if moment.tzinfo is None:
        raise ValueError(f"naive datetime {moment!r}: attach a tzinfo, it has no month")
    local = moment.astimezone(resolve_zone(tz))
    return date(local.year, local.month, 1)


def month_bounds(month: date, tz: str | tzinfo | None = None) -> tuple[datetime, datetime]:
    """The UTC half-open range ``[start, end)`` of one local month."""
    tzone = resolve_zone(tz)
    first = date(month.year, month.month, 1)
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    start = datetime.combine(first, time.min, tzinfo=tzone)
    end = datetime.combine(following, time.min, tzinfo=tzone)
    return start.astimezone(UTC), end.astimezone(UTC)


def rollup_table(
    name: str,
    metadata: MetaData,
    *,
    group_by: Mapping[str, Any],
    measures: Mapping[str, Any],
    tenant_type: Any = None,
) -> Table:
    """A target table for :class:`MonthlyRollup`: tenant, month, groups, measures.

    The unique key is the bucket. On PostgreSQL NULLs are not distinct in it,
    so a single-tenant service (``tenant_id`` NULL) still has one row per
    bucket.
    """
    columns: list[Any] = [
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", tenant_type if tenant_type is not None else String(255)),
        Column("month", Date, nullable=False),
    ]
    columns += [Column(column, kind) for column, kind in group_by.items()]
    columns += [Column(column, kind) for column, kind in measures.items()]
    return Table(
        name,
        metadata,
        *columns,
        UniqueConstraint(
            "tenant_id",
            "month",
            *group_by,
            name=f"uq_{name}_bucket",
            postgresql_nulls_not_distinct=True,
        ),
    )


class MonthlyRollup:
    """Keeps ``target`` equal to the monthly aggregate of ``source``, bucket by bucket."""

    def __init__(
        self,
        *,
        source: Table,
        target: Table,
        at: ColumnElement[Any],
        tenant: ColumnElement[Any] | None,
        measures: Mapping[str, ColumnElement[Any]],
        group_by: Sequence[ColumnElement[Any]] = (),
        where: ColumnElement[bool] | None = None,
        zone: str | tzinfo | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.at = at
        self.tenant = tenant
        self.group_by = list(group_by)
        self.measures = dict(measures)
        self.where = where
        self.zone = zone
        missing = [
            name
            for name in ["tenant_id", "month", *self._group_names(), *self.measures]
            if name not in target.c
        ]
        if missing:
            raise ValueError(f"{target.name} has no column(s) {', '.join(missing)}")

    def _group_names(self) -> list[str]:
        return [str(getattr(column, "key", None) or column.name) for column in self.group_by]

    def _bucket_filter(self, tenant_id: str | None, month: date) -> ColumnElement[bool]:
        return and_(
            self.target.c.tenant_id.is_not_distinct_from(tenant_id),
            self.target.c.month == month,
        )

    async def refresh(self, executor: Any, *, tenant_id: str | None, month: date) -> int:
        """Recompute one tenant-month from the source rows. Returns the groups written.

        ``executor`` is an ``AsyncSession`` or ``AsyncConnection`` inside a
        transaction; the refresh commits with it. Idempotent.
        """
        month = date(month.year, month.month, 1)
        start, end = month_bounds(month, self.zone)
        if _dialect(executor) == "postgresql":
            await executor.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"rollup:{self.target.name}:{tenant_id}:{month.isoformat()}"},
            )

        await executor.execute(delete(self.target).where(self._bucket_filter(tenant_id, month)))

        conditions: list[ColumnElement[bool]] = [self.at >= start, self.at < end]
        if self.tenant is not None:
            conditions.append(self.tenant.is_not_distinct_from(tenant_id))
        if self.where is not None:
            conditions.append(self.where)
        selected = (
            select(
                literal(tenant_id, type_=self.target.c.tenant_id.type).label("tenant_id"),
                literal(month, type_=Date).label("month"),
                *self.group_by,
                *(expression.label(name) for name, expression in self.measures.items()),
            )
            .select_from(self.source)
            .where(and_(*conditions))
        )
        if self.group_by:
            selected = selected.group_by(*self.group_by)
        else:
            # Without groups an empty month still aggregates to one row of
            # NULL/0; skip it so "no rows" stays "no bucket".
            selected = selected.having(func.count() > 0)

        names = ["tenant_id", "month", *self._group_names(), *self.measures]
        result = await executor.execute(
            insert(self.target).from_select([self.target.c[n] for n in names], selected)
        )
        return int(result.rowcount or 0)

    async def refresh_at(self, executor: Any, *, tenant_id: str | None, moment: datetime) -> int:
        """Refresh the bucket ``moment`` falls in: what an event handler calls."""
        return await self.refresh(executor, tenant_id=tenant_id, month=month_of(moment, self.zone))

    async def rebuild(
        self, executor: Any, *, tenant_id: str | None, since: date, until: date
    ) -> int:
        """Refresh every month from ``since`` to ``until`` inclusive, for one tenant.

        For a scheduled reconciliation, one tenant per transaction: a single
        transaction over every tenant would hold its locks for the whole run.
        """
        written = 0
        month = date(since.year, since.month, 1)
        last = date(until.year, until.month, 1)
        while month <= last:
            written += await self.refresh(executor, tenant_id=tenant_id, month=month)
            month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        return written


def _dialect(executor: Any) -> str:
    dialect = getattr(executor, "dialect", None)
    if dialect is None:
        dialect = executor.get_bind().dialect
    return str(dialect.name)
