"""The transactional outbox, and the inbox that makes redelivery harmless.

A request that saves an order and queues its confirmation email does two
writes. Done as two transactions, either can happen without the other: the
job is queued and the order rolls back, and the worker looks for an order that
never existed; or the order commits and the process dies before the job is
queued, and the email is never sent. Nothing retries either, because nothing
knows.

The outbox makes them one write. :meth:`Outbox.enqueue` and
:meth:`Outbox.publish` insert through the request's own session, so the
message commits with the order or not at all. A relay then moves committed
messages to the broker. When the queue is the PostgreSQL one in the same
database, there is nothing to relay: the job goes straight into ``jfast_jobs``
through the same session.

An event is delivered twice over, to two audiences. Each module of this
service that subscribes to its type (``@subscribe``) gets a job of its own, written the
same way as ``enqueue`` -- so a modular monolith needs no broker to have
modules react to each other. When an event bus (Kafka) is configured the event
also goes to its topic, for other services. With neither, there is nobody to
deliver it to, and ``publish`` says so instead of writing a row that can only
die: see :class:`~jfastframework.events.UndeliverableEvent`.

Delivery stays at-least-once -- the relay can publish and die before marking
the row -- so consumers deduplicate on the message id with :func:`claim_once`,
inside their own transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Column,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    select,
    update,
)

from jfastframework.db.base import UTCDateTime
from jfastframework.db.framework import (
    JSONType,
    dialect_of,
    framework_metadata,
    insert_ignoring_conflicts,
)
from jfastframework.queues.base import Job

if TYPE_CHECKING:
    from jfastframework.events import Event

__all__ = [
    "INBOX_TABLE",
    "OUTBOX_TABLE",
    "Outbox",
    "OutboxRelay",
    "Undeliverable",
    "claim_once",
    "inbox",
    "outbox",
]

logger = logging.getLogger("jfast.outbox")

OUTBOX_TABLE = "jfast_outbox"
INBOX_TABLE = "jfast_inbox"

PENDING = "pending"
PUBLISHED = "published"
DEAD = "dead"

outbox = Table(
    OUTBOX_TABLE,
    framework_metadata,
    Column("id", String(32), primary_key=True),
    # "job" goes to the queue; "event" goes to the event bus.
    Column("kind", String(16), nullable=False),
    Column("destination", String(255), nullable=False),
    Column("payload", JSONType, nullable=False),
    Column("message_key", String(255)),
    Column("request_id", String(64)),
    Column("tenant_id", String(255)),
    Column("status", String(16), nullable=False, default=PENDING),
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Column("available_at", UTCDateTime, nullable=False, server_default=func.now()),
    Column("published_at", UTCDateTime),
    Column("last_error", Text),
)

# The relay's predicate, and nothing else: published rows are the bulk of the
# table and are never read again.
Index(
    "ix_jfast_outbox_pending",
    outbox.c.available_at,
    postgresql_where=outbox.c.status == PENDING,
    sqlite_where=outbox.c.status == PENDING,
)

inbox = Table(
    INBOX_TABLE,
    framework_metadata,
    Column("consumer", String(128), nullable=False),
    Column("message_id", String(128), nullable=False),
    Column("processed_at", UTCDateTime, nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("consumer", "message_id"),
)


class Undeliverable(RuntimeError):
    """A row that no retry can deliver with this configuration.

    Marked dead on the first attempt, with the reason, instead of retried
    twenty times with backoff: the answer will not change until someone
    changes the configuration, and hours of retries only hide that.
    """


class Outbox:
    """Write messages in the caller's transaction; the relay sends them.

    ``queue`` is the queue backend, when there is one. If it is the
    PostgreSQL queue on the same database as the session, a job is inserted
    into it directly and needs no relay. ``events`` is the event bus, or
    anything truthy standing for one that a relay elsewhere holds; without it
    an event reaches only this service's own subscribers.
    """

    def __init__(self, *, queue: Any = None, events: Any = None) -> None:
        self._queue = queue
        self._events = events

    async def enqueue(self, session: Any, job: Job) -> str:
        """Queue ``job`` so that it exists if and only if this transaction commits."""
        direct = getattr(self._queue, "enqueue_in", None)
        if direct is not None and self._same_database(session):
            await direct(session, job)
            return job.id
        await self._write(
            session,
            id=job.id,
            kind="job",
            destination=job.task,
            payload={
                "payload": job.payload,
                "max_attempts": job.max_attempts,
                "trace": dict(job.trace),
            },
            request_id=job.request_id,
            tenant_id=job.tenant_id,
            available_at=job.available_at,
        )
        return job.id

    async def publish(self, session: Any, topic: str, event: Event) -> str:
        """Publish ``event`` if and only if this transaction commits.

        One job per local subscriber of ``event.type``, and the event itself on
        ``topic`` when there is a bus. Raises ``UndeliverableEvent`` when there
        is neither: nothing would ever receive it.
        """
        from jfastframework.events import UndeliverableEvent, subscribers_for

        local = subscribers_for(event.type)
        if local and self._queue is None:
            names = ", ".join(s.name for s in local)
            raise UndeliverableEvent(
                f"event {event.type!r} has subscribers in this service ({names}) but no "
                f'queue to run them on. Add "queue" to [plugins].enabled in jfast.toml '
                f"(the PostgreSQL backend needs nothing else)."
            )
        if not local and self._events is None:
            raise UndeliverableEvent(
                f"event {event.type!r} was published but nothing can receive it: no module "
                f"in this service subscribes to it and no event bus is configured. Add "
                f'@subscribe("{event.type}") in the module that reacts (in its tasks.py), '
                f'or enable the "events" plugin (Kafka) if another service consumes it.'
            )

        for subscriber in local:
            await self.enqueue(
                session,
                Job(
                    id=subscriber.job_id(event.id),
                    task=subscriber.task,
                    payload={"topic": topic, "event": event.to_dict()},
                    max_attempts=subscriber.max_attempts,
                    # The event's, not whatever this code runs under: a handler
                    # re-publishing an event it rebuilt must not lose them.
                    request_id=event.request_id,
                    tenant_id=event.tenant_id,
                    trace=dict(event.trace),
                ),
            )

        if self._events is not None:
            await self._write(
                session,
                id=event.id,
                kind="event",
                destination=topic,
                payload=event.to_dict(),
                message_key=event.key,
                request_id=event.request_id,
                tenant_id=event.tenant_id,
            )
        return event.id

    def _same_database(self, session: Any) -> bool:
        engine = getattr(self._queue, "engine", None)
        if engine is None:
            return False
        return bool(session.get_bind() is engine.sync_engine)

    async def _write(self, session: Any, **values: Any) -> None:
        if values.get("available_at") is None:
            values.pop("available_at", None)
        values.setdefault("status", PENDING)
        values.setdefault("attempts", 0)
        await session.execute(outbox.insert().values(**values))


async def claim_once(session: Any, message_id: str, *, consumer: str = "default") -> bool:
    """True the first time this consumer sees this message; False after.

    Call it inside the transaction that does the work. If the work rolls back,
    so does the claim, and the redelivery is processed; if it commits, the
    claim commits with it, and every redelivery after that is skipped::

        async with sessionmaker() as session, session.begin():
            if not await claim_once(session, current_job().id, consumer="emails"):
                return
            ...
    """
    statement = insert_ignoring_conflicts(dialect_of(session), inbox).values(
        consumer=consumer, message_id=message_id
    )
    result = await session.execute(statement)
    return bool(result.rowcount == 1)


class OutboxRelay:
    """Moves committed outbox rows to the queue and the event bus.

    Safe to run in every process: rows are claimed with ``FOR UPDATE SKIP
    LOCKED``, so two relays never send the same row at once. A row whose send
    fails is retried with backoff and, after ``max_attempts``, set aside as
    ``dead`` for someone to look at rather than retried forever.
    """

    def __init__(
        self,
        engine: Any,
        *,
        queue: Any = None,
        events: Any = None,
        batch_size: int = 100,
        max_attempts: int = 20,
        retention: timedelta = timedelta(days=7),
    ) -> None:
        self._engine = engine
        self._queue = queue
        self._events = events
        self._batch = batch_size
        self._max_attempts = max_attempts
        self._retention = retention

    async def relay_once(self) -> int:
        """Send one batch. Returns how many rows were published."""
        from sqlalchemy.ext.asyncio import AsyncSession

        now = datetime.now(UTC)
        published = 0
        async with (
            AsyncSession(self._engine, expire_on_commit=False) as session,
            session.begin(),
        ):
            query = (
                select(outbox)
                .where(and_(outbox.c.status == PENDING, outbox.c.available_at <= now))
                .order_by(outbox.c.created_at)
                .limit(self._batch)
            )
            if session.get_bind().dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            rows = (await session.execute(query)).mappings().all()

            for row in rows:
                try:
                    await self._send(row)
                except Exception as exc:  # noqa: BLE001 - recorded on the row
                    attempts = int(row["attempts"]) + 1
                    dead = isinstance(exc, Undeliverable) or attempts >= self._max_attempts
                    delay = min(2**attempts, 300)
                    reason = f"{type(exc).__name__}: {exc}"[:2000]
                    await session.execute(
                        update(outbox)
                        .where(outbox.c.id == row["id"])
                        .values(
                            attempts=attempts,
                            status=DEAD if dead else PENDING,
                            available_at=now + timedelta(seconds=delay),
                            last_error=reason,
                        )
                    )
                    log = logger.error if dead else logger.warning
                    # The cause is in the message itself: a log line that says
                    # only "not sent" sends whoever reads it to the database.
                    log(
                        "outbox %s %s to %r not sent%s: %s",
                        row["kind"],
                        row["id"],
                        row["destination"],
                        " and is now dead" if dead else "",
                        reason,
                        extra={
                            "message_id": row["id"],
                            "kind": row["kind"],
                            "destination": row["destination"],
                            "attempts": attempts,
                            "dead": dead,
                            "error": reason,
                        },
                    )
                    continue
                await session.execute(
                    update(outbox)
                    .where(outbox.c.id == row["id"])
                    .values(status=PUBLISHED, published_at=now, last_error=None)
                )
                published += 1
        return published

    async def _send(self, row: Any) -> None:
        if row["kind"] == "job":
            if self._queue is None:
                raise Undeliverable(
                    'a job is in the outbox and no queue is configured: add "queue" to '
                    "[plugins].enabled"
                )
            body = row["payload"] or {}
            await self._queue.enqueue(
                Job(
                    id=row["id"],
                    task=row["destination"],
                    payload=body.get("payload", {}),
                    max_attempts=int(body.get("max_attempts", 3)),
                    request_id=row["request_id"],
                    tenant_id=row["tenant_id"],
                    trace=dict(body.get("trace") or {}),
                )
            )
            return
        if row["kind"] == "event":
            if self._events is None:
                raise Undeliverable(
                    "an event is in the outbox and no event bus is configured: enable the "
                    '"events" plugin, or have a module @subscribe to it (local subscribers '
                    "are queued when the event is published, not relayed)"
                )
            from jfastframework.events import Event

            event = Event.from_dict(dict(row["payload"] or {}), key=row["message_key"])
            await self._events.publish(row["destination"], event)
            return
        raise Undeliverable(f"unknown outbox kind {row['kind']!r}")

    async def purge(self) -> int:
        """Delete published rows older than the retention window."""
        cutoff = datetime.now(UTC) - self._retention
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(outbox).where(
                    and_(outbox.c.status == PUBLISHED, outbox.c.published_at < cutoff)
                )
            )
        return int(result.rowcount or 0)

    async def stats(self) -> dict[str, int]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(outbox.c.status, func.count()).group_by(outbox.c.status)
            )
            counts = {str(status): int(count) for status, count in rows}
        return {state: counts.get(state, 0) for state in (PENDING, PUBLISHED, DEAD)}

    async def latest_dead_error(self) -> str | None:
        """Why the most recent dead row died, for /ready to quote."""
        async with self._engine.connect() as conn:
            reason: str | None = (
                await conn.execute(
                    select(outbox.c.last_error)
                    .where(outbox.c.status == DEAD)
                    .order_by(outbox.c.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return reason

    async def failing(self) -> tuple[int, str | None]:
        """Pending rows that have failed at least once, and the latest reason.

        A message being retried is not yet dead and may not be old, so neither
        of the other two signals sees it -- and a relay failing every attempt
        is the state an operator most needs to hear about early.
        """
        async with self._engine.connect() as conn:
            count = (
                await conn.execute(
                    select(func.count()).where(
                        and_(outbox.c.status == PENDING, outbox.c.attempts > 0)
                    )
                )
            ).scalar_one()
            reason = None
            if count:
                reason = (
                    await conn.execute(
                        select(outbox.c.last_error)
                        .where(and_(outbox.c.status == PENDING, outbox.c.attempts > 0))
                        .order_by(outbox.c.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        return int(count), reason

    async def oldest_pending_seconds(self) -> float | None:
        async with self._engine.connect() as conn:
            oldest = (
                await conn.execute(
                    select(func.min(outbox.c.created_at)).where(outbox.c.status == PENDING)
                )
            ).scalar_one_or_none()
        if oldest is None:
            return None
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        age: float = (datetime.now(UTC) - oldest).total_seconds()
        return age

    async def run(self, stop: asyncio.Event, *, interval: float = 1.0) -> None:
        """Relay until ``stop`` is set. Purges once an hour."""
        last_purge = datetime.now(UTC)
        while not stop.is_set():
            try:
                sent = await self.relay_once()
                if datetime.now(UTC) - last_purge > timedelta(hours=1):
                    await self.purge()
                    last_purge = datetime.now(UTC)
            except Exception:
                # Logged, and the loop goes on: a relay that dies silently is
                # worse than one that fails loudly and tries again.
                logger.exception("outbox relay pass failed")
                sent = 0
            if sent >= self._batch:
                continue  # a full batch: there is probably more waiting
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
