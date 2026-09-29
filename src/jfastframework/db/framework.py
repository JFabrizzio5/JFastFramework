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
        await conn.run_sync(framework_metadata.create_all, tables=tables, checkfirst=True)


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
