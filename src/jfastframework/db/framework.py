"""Tables the framework owns: the outbox, the inbox, idempotency keys, accounts.

They live on their own ``MetaData`` and are created by the plugin that uses
them, at startup, with ``CREATE TABLE IF NOT EXISTS`` -- the same contract the
PostgreSQL queue has for ``jfast_jobs``. A service's Alembic history
is the service's; a framework upgrade should not need a migration written in
somebody else's repository.

Every one of them is named ``jfast_*``. The generated ``env.py`` passes
:func:`is_framework_table` to Alembic, so ``--autogenerate`` neither creates
them nor -- the part that matters -- proposes to drop them because they are
absent from the service's own models.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, MetaData
from sqlalchemy.dialects.postgresql import JSONB

from jfastframework.db.base import NAMING_CONVENTION

__all__ = [
    "FRAMEWORK_PREFIX",
    "JSONType",
    "dialect_of",
    "ensure_columns",
    "ensure_tables",
    "framework_metadata",
    "include_name",
    "insert_ignoring_conflicts",
    "is_framework_table",
]

FRAMEWORK_PREFIX = "jfast_"

framework_metadata = MetaData(naming_convention=NAMING_CONVENTION)

#: JSONB on PostgreSQL, where it can be indexed and compared; JSON elsewhere,
#: which is what the SQLite test suites of generated services run on.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def is_framework_table(name: str | None) -> bool:
    return bool(name) and str(name).startswith(FRAMEWORK_PREFIX)


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Alembic ``include_name`` hook: leave the framework's tables alone."""
    if type_ == "table":
        return not is_framework_table(name)
    return True


async def ensure_tables(engine: Any, *names: str) -> None:
    """Create these framework tables if they do not exist yet. Idempotent."""
    tables = [framework_metadata.tables[name] for name in names]
    async with engine.begin() as conn:
        # checkfirst reads, then creates: two processes booting on an empty
        # database both read "absent" without this, and one fails.
        await serialize_setup(conn, "jfast:setup:framework-tables")
        await conn.run_sync(framework_metadata.create_all, tables=tables, checkfirst=True)


async def serialize_setup(conn: Any, key: str) -> None:
    """Hold a transaction-scoped advisory lock on *key* (PostgreSQL only).

    Every uvicorn worker process and every replica runs its plugins' startup
    at once. Two ``CREATE TABLE IF NOT EXISTS`` for one new table race and one
    fails on ``pg_type``; two ``ALTER TABLE`` queue for an exclusive lock that
    every read of the table then queues behind. Holding this for the rest of
    the setup transaction makes the second starter wait, then find the work
    done. Readers never take it, so serving is not blocked.
    """
    from sqlalchemy import text

    if conn.dialect.name != "postgresql":
        return
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


async def relation_exists(conn: Any, name: str) -> bool:
    """Whether a table or index exists, read from the catalogue without locking it."""
    from sqlalchemy import text

    found = await conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name})
    return bool(found.scalar())


async def column_exists(conn: Any, table: str, column: str) -> bool:
    """Whether *table* has *column*, from the catalogue: no ``ALTER``, no lock.

    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` takes an ACCESS EXCLUSIVE
    lock before it finds the column already there, so running it at every
    startup queues every query on the table behind each booting process.
    """
    from sqlalchemy import text

    found = await conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(:table) "
            "AND attname = :column AND NOT attisdropped)"
        ),
        {"table": table, "column": column},
    )
    return bool(found.scalar())


async def ensure_columns(engine: Any, name: str) -> list[str]:
    """Add the columns this framework table has gained since it was created.

    ``CREATE TABLE IF NOT EXISTS`` stops at the table: a ``jfast_users``
    created by an earlier release keeps its old columns forever, and the first
    query naming a new one fails. This is the other half of "a framework
    upgrade needs no migration in your repository": every column the table
    definition has and the database does not is added, at startup.

    Only nullable columns without a server default can be added this way --
    that is what makes the ``ALTER`` instant on PostgreSQL and safe on a table
    with rows. A definition that breaks the rule is refused here, in the
    framework's own tests, rather than at somebody's deploy.

    Returns the names of the columns it added.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError, ProgrammingError
    from sqlalchemy.schema import CreateColumn

    table = framework_metadata.tables[name]

    def _missing(sync_conn: Any) -> list[Any]:
        present = {column["name"] for column in inspect(sync_conn).get_columns(name)}
        return [column for column in table.columns if column.name not in present]

    added: list[str] = []
    async with engine.begin() as conn:
        await serialize_setup(conn, f"jfast:setup:{name}")
        missing = await conn.run_sync(_missing)
        for column in missing:
            if not column.nullable or column.server_default is not None:
                raise ValueError(
                    f"{name}.{column.name} cannot be added to an existing table: a column "
                    f"added after release has to be nullable, with no server default"
                )
            ddl = str(CreateColumn(column).compile(dialect=conn.dialect))
            guard = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
            try:
                await conn.execute(text(f"ALTER TABLE {name} ADD COLUMN {guard}{ddl}"))
            except (OperationalError, ProgrammingError) as exc:  # pragma: no cover - a race
                # Another replica added it between the inspection and here.
                # SQLite has no IF NOT EXISTS for columns; its error says so.
                if "duplicate column" not in str(exc).lower():
                    raise
            added.append(column.name)
    return added


def insert_ignoring_conflicts(dialect: str, table: Any) -> Any:
    """``INSERT ... ON CONFLICT DO NOTHING`` in the dialect's own spelling.

    PostgreSQL and SQLite both have it; they are the two backends this
    framework runs against.
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table).on_conflict_do_nothing()
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(table).on_conflict_do_nothing()
    raise NotImplementedError(f"no ON CONFLICT DO NOTHING for {dialect!r}")


def dialect_of(session: Any) -> str:
    bind = session.get_bind()
    return str(bind.dialect.name)
