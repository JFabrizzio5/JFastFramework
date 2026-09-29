"""Declarative base with a stable constraint naming convention.

Why this matters: without an explicit ``naming_convention``, PostgreSQL invents
constraint names and Alembic's ``--autogenerate`` produces different diffs on
different machines. Pinning the convention here makes migrations reproducible
across every service built on JFast.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware on every backend, in both directions.

    ``DateTime(timezone=True)`` on its own gets the DDL right -- PostgreSQL
    gets ``TIMESTAMPTZ`` -- and stops there. Whether the value handed back
    carries an offset is up to the driver, and SQLite's ``CURRENT_TIMESTAMP``
    has none to give. A naive value serialises as ``2026-08-29T20:55:15``,
    which every JavaScript client reads as *local* time, so a row written now
    renders hours away for anyone off UTC.

    Reads attach UTC rather than guess at it: both write paths this framework
    supports store UTC already -- PostgreSQL normalises ``TIMESTAMPTZ`` to it
    on the way in, and SQLite's ``CURRENT_TIMESTAMP`` is UTC by definition.

    Writes refuse a naive datetime instead of assuming one. There is no
    correct zone to pick for it, and picking silently is how a row lands hours
    off with nothing in the logs.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime written to a timezone-aware column: "
                f"{value!r}. Use datetime.now(UTC), or attach a tzinfo."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class TimestampMixin:
    """created_at / updated_at maintained by the database, not the app."""

    # Fetch server-side defaults as part of the INSERT/UPDATE rather than
    # on the next attribute access. Without this, `onupdate` expires
    # `updated_at` at flush time, and the first read -- usually Pydantic
    # serialising the response -- triggers lazy IO inside a coroutine and
    # raises MissingGreenlet. PostgreSQL returns the values with RETURNING,
    # so this costs no extra round trip; `session.refresh()` would cost one
    # SELECT on every write, including the writes that never read a
    # timestamp.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    # `func.now()` stays: on PostgreSQL it is the transaction timestamp and it
    # is already `timestamptz`, so every row written in one transaction agrees
    # with itself. What it does not do is guarantee an offset comes back --
    # that is UTCDateTime's job, and the reason the column type is spelled out
    # instead of being inferred from `Mapped[datetime]`, which infers
    # TIMESTAMP WITHOUT TIME ZONE.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VersionedMixin:
    """Optimistic locking: a write that lost a race fails instead of winning it.

    Two requests read version 3 of a row and both write. Without a version the
    second write silently replaces the first -- the lost update. With one,
    SQLAlchemy's UPDATE carries ``WHERE version = 3``, the second matches no row,
    and the flush raises; ``BaseRepository`` turns that into a 409. The same
    column is what a client sends back to say which version it edited, which
    ``BaseRepository.update(expected_version=...)`` checks as a 412.

    List it **before** ``TimestampMixin``. Both set ``__mapper_args__`` and the
    first one in the bases wins, so this one carries the timestamp mixin's
    ``eager_defaults`` too; the other order would drop the versioning without
    a word, which is why that order raises instead.
    """

    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")

    @declared_attr.directive
    def __mapper_args__(cls: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"version_id_col": cls.__table__.c.version}
        if issubclass(cls, TimestampMixin):
            args["eager_defaults"] = True
        return args

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Static lookup: the first __mapper_args__ in the MRO is the one the
        # mapper will use. Anything but ours -- a plain dict from
        # TimestampMixin, or one the model wrote itself -- would lose the
        # version column unless it names it.
        found = inspect.getattr_static(cls, "__mapper_args__", None)
        if isinstance(found, dict) and "version_id_col" not in found:
            raise TypeError(
                f"{cls.__name__} lists a __mapper_args__ ahead of VersionedMixin, "
                f"so its version column would never be checked. Put VersionedMixin "
                f"before TimestampMixin in the bases, or add version_id_col to "
                f"{cls.__name__}.__mapper_args__ yourself."
            )
        super().__init_subclass__(**kwargs)


class TenantMixin:
    """Tenant scoping column.

    Adding the column is the easy half. Enforcing that no query forgets the
    filter is the hard half -- ``BaseRepository`` does it for repository
    traffic, but raw ``session.execute`` calls bypass it. Row-level security
    is the phase-2 answer; see PLAN.md.
    """

    tenant_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
