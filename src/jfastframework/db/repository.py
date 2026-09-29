"""Generic async repository.

Kills the per-module CRUD boilerplate. Subclass it and you get list, get,
create, update, delete and pagination typed to your model::

    class OrderRepository(BaseRepository[Order]):
        model = Order

        async def pending(self) -> list[Order]:
            return await self.find(status="pending")

Three rules this class enforces rather than assumes, because all three fail
silently when they are only conventions:

**Pagination is ordered.** ``LIMIT``/``OFFSET`` without an ``ORDER BY`` does
not give stable pages in PostgreSQL. The planner is free to return rows in a
different order on the second query, so a row can appear on page one and page
two while another is never returned at all. The primary key is the default;
override ``order_by`` when the natural order is something else.

**Tenant scoping cannot silently do nothing.** A repository handed a tenant id
for a model with no ``tenant_id`` column would otherwise return every row of
every tenant. That is a data leak in the shape of a no-op, so it raises. A
model that genuinely is global says so with ``tenant_scoped = False``.

**Ordering is total over NULLs too.** A nullable ordering column -- and
``last_message_at`` or ``edited_at`` is exactly the column a feed sorts by --
has no default position the backends agree on: PostgreSQL puts NULLs last
ascending and first descending, SQLite puts them first either way. Every
ordering this class builds spells ``NULLS LAST`` on the nullable columns and
every keyset comparison is written against that, so a cursor that lands on a
NULL keeps paging instead of reporting the table finished.

Three ways to page, in rising order of what they cost the database:

``paginate(..., with_total=False)`` is the one most list endpoints want. It
fetches ``limit + 1`` rows and returns ``limit`` of them, so it answers "is
there another page" without the COUNT -- which, on a table worth paginating,
is the expensive half of the response and is usually thrown away.

``paginate_keyset(after=...)`` is the one deep pages want. ``OFFSET n`` makes
the database walk and discard n rows before it returns anything, so page 200
costs 200 pages of work; a keyset page is a range scan from a known point and
costs the same wherever it lands. The trade is random access: there is a next
page, not a page 40.

That flat cost holds while the ordering columns are NOT NULL. A nullable one
buys correctness with it: the predicate grows an ``OR c IS NULL`` disjunct,
PostgreSQL demotes the index range scan to an index scan with a filter, and
the walk is back to reading from the start of the index. Measured on 200k rows
with an index on the ordering columns, one page at depth 100k: 4 buffers with
NOT NULL columns, 840 with a nullable one, against 1049 for the same page by
``OFFSET``. Still the cheapest of the three, no longer flat. Splitting the
scan into the non-NULL range and the terminal NULL block would get it back and
is not built.

``paginate()`` unchanged, exact total, one COUNT. Keep it where a client
genuinely renders "1-50 of 4,812" and can afford it.

Writes fail as HTTP-shaped errors, not driver exceptions. A unique or foreign
key violation raised by the flush is a ``ConflictError`` (409); a row changed
by another request since this one read it -- ``VersionedMixin`` -- is a 409
too; and ``update(expected_version=...)`` against a row that has moved on is a
``PreconditionFailedError`` (412). Checking for a duplicate before inserting
is still worth doing for the message, but the constraint is what makes it
true: two requests can both pass the check.

After one of those the transaction is finished -- PostgreSQL refuses every
statement until it rolls back, which the request-scoped session does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import and_, false, func, inspect, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from jfastframework.db.transactions import conflict_from, stale_version, version_mismatch
from jfastframework.errors import NotFoundError

TModel = TypeVar("TModel")


def _is_nullable(column: Any) -> bool:
    """Whether an ordering column can hold NULL.

    ``NULLS LAST`` and the OR chain are only paid for where a NULL can turn
    up, so anything that will not answer is treated as nullable: the cheaper
    branch is the one that drops rows without saying so.
    """
    element = getattr(column, "__clause_element__", None)
    target = element() if element is not None else column
    return bool(getattr(target, "nullable", True))


# The ordering values of the last row on a page, in order-column order.
# Opaque to callers: hand it back to `paginate_keyset(after=...)` untouched.
# Encoding it for a URL is the application's call, because whether it needs
# signing depends on whether the ordering columns are secrets -- and that
# encoding has to survive a None, which is a position in the order (the NULL
# block, last) and not a missing value.
Cursor = tuple[Any, ...]


@dataclass
class Page(Generic[TModel]):
    """One page of rows, and enough to ask for the next one.

    ``total`` is ``None`` on every page fetched without a COUNT. That is not a
    value waiting to be filled in later -- the query deliberately never asked,
    so nothing downstream can render "page 3 of 40" out of it.
    """

    items: list[TModel]
    total: int | None
    limit: int
    offset: int
    # Set by the modes that skip the COUNT: they read one row past the page, so
    # "is there a next page" is answered without counting the rows behind it.
    # None means the answer comes from `total` instead.
    more: bool | None = None
    # Feed back to `paginate_keyset(after=...)`. None on the last page, and on
    # every page that was not keyset-paginated.
    next_cursor: Cursor | None = None

    def __post_init__(self) -> None:
        if self.total is None and self.more is None:
            raise ValueError(
                "a Page with no total must say whether more rows follow: pass more=True/False"
            )

    @property
    def has_more(self) -> bool:
        if self.more is not None:
            return self.more
        total = self.total
        return total is not None and self.offset + len(self.items) < total


class BaseRepository(Generic[TModel]):
    model: ClassVar[Any]
    # False for reference data every tenant shares -- currencies, countries,
    # plans. Saying so is the only way to tell "global on purpose" apart from
    # "somebody forgot the column".
    tenant_scoped: ClassVar[bool] = True
    # Columns to order by, as names. A leading "-" sorts that column
    # descending. Defaults to the primary key, ascending.
    order_by: ClassVar[tuple[str, ...]] = ()

    def __init__(self, session: AsyncSession, *, tenant_id: str | None = None) -> None:
        self.session = session
        # When set, every query built by this repository is tenant-filtered.
        self.tenant_id = tenant_id
        if tenant_id is not None and self.tenant_scoped and not hasattr(self.model, "tenant_id"):
            raise TypeError(
                f"{type(self).__name__} was given a tenant_id but {self.model.__name__} "
                f"has no tenant_id column. Add the column, or declare "
                f"tenant_scoped = False if this model is shared across tenants."
            )

    # -- query building ------------------------------------------------

    def _order_spec(self) -> list[tuple[Any, bool]]:
        """``(column, descending)`` pairs.

        Direction is carried alongside the column rather than baked into it:
        keyset paging has to know which way each column sorts to build the
        comparison, and an ``UnaryExpression`` from ``.desc()`` will not say.
        """
        if self.order_by:
            spec: list[tuple[Any, bool]] = []
            for name in self.order_by:
                descending = name.startswith("-")
                spec.append((getattr(self.model, name[1:] if descending else name), descending))
            return spec
        return [(column, False) for column in inspect(self.model).primary_key]

    def _keyset_spec(self) -> list[tuple[Any, bool]]:
        """The order spec with the primary key appended as a tiebreaker.

        Keyset paging compares each row against the last row of the page
        before it, so the ordering has to be a *total* order. Two rows that
        compare equal straddle the boundary in whatever sequence the planner
        picked, and one of them is dropped from both pages -- the same silent
        row loss ``ORDER BY`` is there to prevent, one level down.

        The tiebreaker inherits the direction of the column it follows, so a
        newest-first feed stays uniformly descending and keeps the row-value
        comparison below.
        """
        spec = self._order_spec()
        present = {column.key for column, _ in spec}
        trailing_descending = spec[-1][1] if spec else False
        for column in inspect(self.model).primary_key:
            if column.key not in present:
                spec.append((column, trailing_descending))
        return spec

    @staticmethod
    def _directed(spec: list[tuple[Any, bool]]) -> list[Any]:
        """The ORDER BY terms, with the NULL group pinned to the end.

        ``NULLS LAST`` on both directions, because the default is not one
        thing: PostgreSQL sorts NULLs last ascending and first descending,
        SQLite sorts them first either way. Pinning them makes the keyset
        comparison below expressible at all -- there is nothing to compare a
        cursor against if the NULL block can sit at either end.

        Only nullable columns get the clause. A NOT NULL ``ORDER BY c DESC``
        still matches a plain descending index; spelling it
        ``DESC NULLS LAST`` would not, and would cost a sort for a guarantee
        the column already gives.
        """
        columns = []
        for column, descending in spec:
            directed = column.desc() if descending else column.asc()
            columns.append(directed.nulls_last() if _is_nullable(column) else directed)
        return columns

    def _order_columns(self) -> list[Any]:
        return self._directed(self._order_spec())

    @staticmethod
    def _after_cursor(spec: list[tuple[Any, bool]], cursor: Cursor) -> Any:
        """Rows strictly past ``cursor`` in the order ``spec`` describes.

        Row-value comparison when every ordering column is NOT NULL and they
        all sort the same way, because PostgreSQL turns ``(a, b) > (:a, :b)``
        into a single index range scan on a composite index -- the whole point
        of paging this way. Mixed directions have no row-value spelling, and
        neither does a NULL, so both expand to the OR chain: correct on every
        backend and merely slower.

        The OR chain is NULL-aware on both halves, against the ``NULLS LAST``
        that ``_directed`` emits -- a tie on a NULL is ``IS NULL``, and a step
        past a non-NULL value also admits the NULL block that follows it. That
        is not a refinement of the plain comparison, it is the difference
        between paging and stopping: a NULL in an ordering column compares
        UNKNOWN against every row, so the untreated version returns an *empty*
        page with ``has_more`` False, and every row from the cursor onward --
        180 of 200 on SQLite, where NULLs sort first, 40 of 200 on PostgreSQL,
        where they sort last -- is unreachable from any cursor. The rows do not
        arrive late on a later page. They never arrive, and nothing says so.
        """
        columns = [column for column, _ in spec]
        directions = {descending for _, descending in spec}
        nullable = any(_is_nullable(column) for column in columns)
        if len(directions) == 1 and not nullable and not any(v is None for v in cursor):
            row = tuple_(*columns)
            values = tuple_(*cursor)
            return row < values if directions.pop() else row > values

        clauses = []
        for index, (column, descending) in enumerate(spec):
            value = cursor[index]
            # NULLS LAST: a NULL here is already at the end of its group, so
            # nothing steps past it on this column. Later columns still can.
            if value is None:
                continue
            ties = [
                earlier.is_(None) if cursor[i] is None else earlier == cursor[i]
                for i, (earlier, _) in enumerate(spec[:index])
            ]
            step = column < value if descending else column > value
            if _is_nullable(column):
                step = or_(step, column.is_(None))
            clauses.append(and_(*ties, step))
        # An all-NULL cursor is the last row of the last page: every column
        # sits in its terminal group, so nothing follows it.
        return or_(*clauses) if clauses else false()

    def _base_query(self) -> Any:
        query = select(self.model)
        if self.tenant_id is not None and self.tenant_scoped:
            query = query.where(self.model.tenant_id == self.tenant_id)
        return query

    def _filtered(self, filters: dict[str, Any]) -> Any:
        query = self._base_query()
        for field, value in filters.items():
            query = query.where(getattr(self.model, field) == value)
        return query

    # -- reads ---------------------------------------------------------

    async def get(self, pk: Any) -> TModel | None:
        query = self._base_query().where(self.model.id == pk)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_or_raise(self, pk: Any) -> TModel:
        instance = await self.get(pk)
        if instance is None:
            raise NotFoundError(f"{self.model.__name__} {pk!r} not found")
        return instance

    async def find(self, **filters: Any) -> list[TModel]:
        query = self._filtered(filters).order_by(*self._order_columns())
        return list((await self.session.execute(query)).scalars().all())

    async def find_one(self, **filters: Any) -> TModel | None:
        results = await self.find(**filters)
        return results[0] if results else None

    async def paginate(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        with_total: bool = True,
        **filters: Any,
    ) -> Page[TModel]:
        """One page by offset. ``with_total=False`` drops the COUNT.

        The default still counts, because changing what an existing caller
        gets back is worse than making it opt in.
        """
        query = self._filtered(filters)
        ordered = query.order_by(*self._order_columns())

        if not with_total:
            # limit + 1: the extra row is the whole answer and is never
            # returned. It costs one row, against a COUNT that visits every
            # row behind the page to produce a number most clients only use
            # to decide whether to draw a "next" button.
            # Annotated because SQLAlchemy 2.1 types `scalars()` more tightly
            # than 2.0 and mypy no longer infers it on its own.
            rows: list[TModel] = list(
                (await self.session.execute(ordered.limit(limit + 1).offset(offset)))
                .scalars()
                .all()
            )
            return Page(
                items=rows[:limit],
                total=None,
                limit=limit,
                offset=offset,
                more=len(rows) > limit,
            )

        # Count before ordering: the ORDER BY is dead weight in a COUNT, and
        # some databases refuse it inside a subquery.
        total_query = select(func.count()).select_from(query.subquery())
        total = (await self.session.execute(total_query)).scalar_one()

        result = await self.session.execute(ordered.limit(limit).offset(offset))
        return Page(
            items=list(result.scalars().all()),
            total=int(total),
            limit=limit,
            offset=offset,
        )

    async def paginate_keyset(
        self, *, limit: int = 50, after: Cursor | None = None, **filters: Any
    ) -> Page[TModel]:
        """One page positioned by the last row of the previous one.

        Built on ``_filtered`` like every other read, so the tenant ``where``
        clause is applied here too. It has to be: a keyset page that quietly
        dropped it would serve another tenant's rows on page two while page
        one looked correct.

        ``Page.offset`` is 0 on every keyset page. There is no offset to
        report -- that is the cost this method exists to avoid -- so read
        ``has_more`` and ``next_cursor``, never ``offset``.
        """
        spec = self._keyset_spec()
        query = self._filtered(filters)
        if after is not None:
            if len(after) != len(spec):
                raise ValueError(
                    f"cursor has {len(after)} values but the ordering needs "
                    f"{len(spec)}; it belongs to a different query"
                )
            query = query.where(self._after_cursor(spec, after))

        rows: list[TModel] = list(
            (await self.session.execute(query.order_by(*self._directed(spec)).limit(limit + 1)))
            .scalars()
            .all()
        )
        items = rows[:limit]
        more = len(rows) > limit
        return Page(
            items=items,
            total=None,
            limit=limit,
            offset=0,
            more=more,
            next_cursor=self.cursor_for(items[-1]) if more and items else None,
        )

    def cursor_for(self, instance: TModel) -> Cursor:
        """The cursor that resumes paging just after ``instance``."""
        return tuple(getattr(instance, column.key) for column, _ in self._keyset_spec())

    async def count(self, **filters: Any) -> int:
        query = self._filtered(filters)
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())

    # -- writes --------------------------------------------------------

    async def create(self, **values: Any) -> TModel:
        if self.tenant_id is not None and self.tenant_scoped:
            values.setdefault("tenant_id", self.tenant_id)
        instance = self.model(**values)
        self.session.add(instance)
        # flush, not commit: the request-scoped session owns the transaction.
        await self._flush()
        return instance  # type: ignore[no-any-return]

    async def update(
        self, instance: TModel, *, expected_version: int | None = None, **values: Any
    ) -> TModel:
        """Apply ``values`` and flush.

        ``expected_version`` is the version the client read. Passing it turns
        "somebody else saved in between" into a 412 instead of their change
        being overwritten; it needs a ``version`` column (``VersionedMixin``).
        """
        if expected_version is not None:
            current = getattr(instance, "version", None)
            if current is None:
                raise TypeError(
                    f"{self.model.__name__} has no version column, so expected_version "
                    f"cannot be checked. Add VersionedMixin to the model."
                )
            if int(current) != int(expected_version):
                raise version_mismatch(self.model.__name__, int(expected_version), int(current))
        for field, value in values.items():
            setattr(instance, field, value)
        await self._flush()
        return instance

    async def delete(self, instance: TModel) -> None:
        await self.session.delete(instance)
        await self._flush()

    async def get_for_update(
        self, pk: Any, *, nowait: bool = False, skip_locked: bool = False
    ) -> TModel:
        """Read one row and lock it until the transaction ends.

        For read-modify-write on a value that cannot be expressed as one
        UPDATE -- a balance, a stock count, a sequence. The second request
        waits here instead of reading the same number and overwriting the
        first one's result. ``nowait`` fails immediately instead of waiting.
        """
        query = (
            self._base_query()
            .where(self.model.id == pk)
            .with_for_update(nowait=nowait, skip_locked=skip_locked)
        )
        # Annotated rather than `type: ignore`d: SQLAlchemy 2.0 returns Any
        # here and 2.1 does not, and an ignore is an error on one of them.
        instance: TModel | None = (await self.session.execute(query)).scalar_one_or_none()
        if instance is None:
            raise NotFoundError(f"{self.model.__name__} {pk!r} not found")
        return instance

    async def _flush(self) -> None:
        try:
            await self.session.flush()
        except IntegrityError as exc:
            conflict = conflict_from(exc)
            if conflict is None:
                raise
            raise conflict from exc
        except StaleDataError as exc:
            raise stale_version(self.model.__name__) from exc
