"""What a transaction can fail with, and the tools for failing correctly.

Three failures look alike from the outside and need different answers:

* **A constraint said no** -- a duplicate, a missing parent. The request is
  wrong for the current state; the client gets a 409 and nothing is retried.
  ``conflict_from`` turns the driver's exception into that.
* **The database gave up on the transaction** -- a serialisation failure
  (``40001``) or a deadlock (``40P01``). Nothing was wrong with the request; the
  same work run again will most likely succeed. ``run_in_transaction`` does
  that, and only for the whole unit of work, because replaying half of one is
  how a charge is taken twice.
* **Two writers raced for the same thing** -- ``advisory_lock`` serialises them
  on a key of your choosing, for the cases a unique constraint cannot express
  ("one open invoice per customer per month").

None of this is a circuit breaker. A breaker stops calling something that is
down; it does nothing for work that failed half way. That is what the
transaction boundary is for.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from jfastframework.errors import ConflictError, PreconditionFailedError

__all__ = [
    "RETRYABLE_SQLSTATES",
    "advisory_lock",
    "conflict_from",
    "is_retryable",
    "run_in_transaction",
    "sqlstate",
]

T = TypeVar("T")

#: serialization_failure and deadlock_detected. Both mean "the database rolled
#: this transaction back to protect another one", which is exactly the case
#: where running the same work again is correct.
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})

_UNIQUE = "23505"
_FOREIGN_KEY = "23503"
_EXCLUSION = "23P01"


def sqlstate(exc: BaseException) -> str | None:
    """The five-character SQLSTATE behind a SQLAlchemy error, if a driver gave one.

    asyncpg and psycopg spell it differently and SQLAlchemy wraps both, so this
    walks ``orig`` and the cause chain rather than trusting one attribute name.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for name in ("sqlstate", "pgcode"):
            code = getattr(current, name, None)
            if isinstance(code, str) and len(code) == 5:
                return code
        current = getattr(current, "orig", None) or current.__cause__
    return None


def is_retryable(exc: BaseException) -> bool:
    return sqlstate(exc) in RETRYABLE_SQLSTATES


def conflict_from(exc: BaseException) -> ConflictError | None:
    """A 409 for a constraint violation, or None for anything else.

    SQLite reports no SQLSTATE, so its messages are matched instead -- it is
    what the test suites of generated services run on.
    """
    code = sqlstate(exc)
    message = str(getattr(exc, "orig", None) or exc)
    if code == _UNIQUE or "UNIQUE constraint failed" in message:
        return ConflictError("a row with these values already exists")
    if code == _FOREIGN_KEY or "FOREIGN KEY constraint failed" in message:
        return ConflictError(
            "this change references a row that does not exist, or removes one "
            "that something else still references"
        )
    if code == _EXCLUSION:
        return ConflictError("this change overlaps a row that already exists")
    return None


def stale_version(entity: str) -> ConflictError:
    return ConflictError(
        f"{entity} was changed by another request while this one was running; "
        f"read it again and retry"
    )


def version_mismatch(entity: str, expected: int, actual: int) -> PreconditionFailedError:
    return PreconditionFailedError(
        f"{entity} is at version {actual}, not {expected}; read it again before writing",
        current_version=actual,
    )


async def advisory_lock(session: Any, key: str) -> None:
    """Hold a lock on ``key`` until the current transaction ends.

    ``pg_advisory_xact_lock`` releases itself on commit or rollback, so there
    is no unlock to forget and no lock outliving a crashed request. Two
    transactions asking for the same key run one after the other; different
    keys do not wait on each other.

    SQLite allows one writer at a time already, so there it does nothing.
    Any other database raises rather than pretending to lock.
    """
    from sqlalchemy import text

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise NotImplementedError(f"advisory_lock supports PostgreSQL, not {dialect!r}")
    # hashtextextended gives a 64-bit key; hashtext's 32 bits collide sooner
    # than anyone expects once keys carry ids.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def run_in_transaction(
    sessionmaker: Callable[[], Any],
    work: Callable[[Any], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
) -> T:
    """Run ``work`` in its own transaction, again if the database asks for it.

    Every attempt gets a fresh session and a fresh transaction, and ``work``
    runs from its first line: a retry never resumes half a unit of work. Only
    serialisation failures and deadlocks are retried; a constraint violation or
    a bug fails on the first attempt, as it should.

    ``work`` must not have effects outside the database -- an email sent from
    inside it is sent once per attempt. Queue those, and send them after.

    For work outside a request (jobs, scripts). A request already has its
    transaction; retrying inside it would replay only part of the request.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            async with sessionmaker() as session, session.begin():
                return await work(session)
        except Exception as exc:
            if attempt == attempts or not is_retryable(exc):
                raise
            # Full jitter: two transactions that deadlocked each other and
            # sleep the same time collide again.
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            await asyncio.sleep(random.uniform(0, delay))  # nosec B311 - jitter, not security
    raise AssertionError("unreachable")  # pragma: no cover
