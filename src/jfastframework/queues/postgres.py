"""Job queue on PostgreSQL.

The default, and the right first choice for most services: the database is
already there, jobs are visible to `SELECT`, and enqueueing can share the
transaction that produced the work — so a job never references a row that was
rolled back.

Claiming uses ``FOR UPDATE SKIP LOCKED``, which is what makes a SQL table a
correct queue: concurrent workers take different rows instead of blocking on
each other.

It is not the right choice at very high throughput. Every claim is a write, so
past a few hundred jobs a second the queue starts competing with the
application for the same connections and the same WAL. Move to Redis or
RabbitMQ then — and be able to say which number you hit.

Requires: ``pip install jfastframework[db]``

Table and column names cannot be bound as parameters, so the table name
is interpolated. It is validated by safe_identifier() at construction and
every value below is bound -- hence the `# nosec B608` waivers.
"""

from __future__ import annotations

import json
from typing import Any

from jfastframework.queues.base import DeadJob, Job
from jfastframework.sql import safe_identifier

# The claim, as a named statement rather than an f-string buried in a method:
# it is the only interesting SQL in this file and it is easier to review here.
# `FOR UPDATE SKIP LOCKED` is what makes a table behave like a queue, and the
# `locked_until` branch is what returns a job whose worker died mid-flight.
CLAIM_SQL = """
    UPDATE {table} SET
        status = 'running',
        attempts = attempts + 1,
        locked_until = NOW() + make_interval(secs => :visibility)
    WHERE id = (
        SELECT id FROM {table}
        WHERE available_at <= NOW()
          AND (
            status = 'pending'
            -- Reclaim a job whose worker died mid-flight.
            OR (status = 'running' AND locked_until < NOW())
          )
        ORDER BY available_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, task, payload, attempts, max_attempts,
              request_id, tenant_id, trace
"""


class PostgresQueue:
    def __init__(self, engine: Any, *, table: str = "jfast_jobs", visibility_timeout: int = 300):
        self._engine = engine
        self._table = safe_identifier(table, kind="queue table")
        self._visibility = visibility_timeout

    @property
    def visibility_timeout(self) -> int:
        """How long a claim stays invisible to other workers.

        Published because the worker has to finish inside it: nothing extends
        the lease while a handler runs, so a job that outlives this window is
        claimed again while the first run is still going.
        """
        return self._visibility

    async def setup(self) -> None:
        from sqlalchemy import text

        from jfastframework.db.framework import column_exists, relation_exists, serialize_setup

        async with self._engine.begin() as conn:
            # Every worker process and replica boots at once; see serialize_setup.
            await serialize_setup(conn, f"jfast:setup:{self._table}")
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        id            TEXT PRIMARY KEY,
                        task          TEXT NOT NULL,
                        payload       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        attempts      INTEGER NOT NULL DEFAULT 0,
                        max_attempts  INTEGER NOT NULL DEFAULT 3,
                        available_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        locked_until  TIMESTAMPTZ,
                        request_id    TEXT,
                        tenant_id     TEXT,
                        status        TEXT NOT NULL DEFAULT 'pending',
                        last_error    TEXT,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        trace         JSONB
                    )
                    """
                )
            )
            # Added in 0.1.0a11, for tables created before it. Nullable with no
            # default, so PostgreSQL records it in the catalogue without
            # rewriting a single row: safe on a queue with millions in it.
            # Asked of the catalogue first: the ALTER, even as IF NOT EXISTS,
            # takes an exclusive lock that every claim and /ready then waits on.
            if not await column_exists(conn, self._table, "trace"):
                await conn.execute(
                    text(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS trace JSONB")
                )
            # Partial index on exactly the claim predicate. Without it every
            # dequeue scans the dead-letter rows too, and the queue slows down
            # as failures accumulate -- the worst possible time.
            if not await relation_exists(conn, f"{self._table}_claim_idx"):
                await conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {self._table}_claim_idx "
                        f"ON {self._table} (available_at) WHERE status = 'pending'"
                    )
                )

    @property
    def engine(self) -> Any:
        """The engine this queue writes through, so an outbox can tell whether
        a request's session is on the same database."""
        return self._engine

    async def enqueue(self, job: Job) -> str:
        async with self._engine.begin() as conn:
            await conn.execute(*self._insert(job))
        return job.id

    async def enqueue_in(self, session: Any, job: Job) -> str:
        """Enqueue through the caller's session, inside the caller's transaction.

        The job then exists if and only if that transaction commits: a request
        that rolls back takes its job with it, and one that commits cannot lose
        it. ``Outbox.enqueue`` uses this when the queue and the session share a
        database, which is what makes the relay unnecessary for them.
        """
        await session.execute(*self._insert(job))
        return job.id

    def _insert(self, job: Job) -> tuple[Any, dict[str, Any]]:
        from sqlalchemy import text

        statement = text(
            f"INSERT INTO {self._table} "  # nosec B608
            f"(id, task, payload, attempts, max_attempts, available_at, "
            f" request_id, tenant_id, trace) "
            f"VALUES (:id, :task, CAST(:payload AS jsonb), :attempts, :max_attempts, "
            f" COALESCE(:available_at, NOW()), :request_id, :tenant_id, "
            f" CAST(:trace AS jsonb)) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        return statement, {
            "id": job.id,
            "task": job.task,
            "payload": json.dumps(job.payload, default=str),
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "available_at": job.available_at,
            "request_id": job.request_id,
            "tenant_id": job.tenant_id,
            "trace": json.dumps(job.trace) if job.trace else None,
        }

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        """Claim one job.

        No blocking wait: PostgreSQL has no BRPOP. The worker polls, which is
        why the poll interval is a setting and why Redis wins on latency.
        """
        from sqlalchemy import text

        claim = CLAIM_SQL.format(table=self._table)  # nosec B608
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(claim),
                {"visibility": self._visibility},
            )
            row = result.mappings().first()

        if row is None:
            return None
        return Job(
            id=row["id"],
            task=row["task"],
            payload=row["payload"] or {},
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            trace=dict(row["trace"] or {}),
            receipt=row["id"],
        )

    async def ack(self, job: Job) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {self._table} WHERE id = :id"), {"id": job.id})  # nosec B608

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        from sqlalchemy import text

        error = job.error[:2000] if job.error else None
        if not retry or job.exhausted:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        f"UPDATE {self._table} SET status = 'dead', locked_until = NULL, "  # nosec B608
                        f"last_error = COALESCE(:error, last_error) WHERE id = :id"
                    ),
                    {"id": job.id, "error": error},
                )
            return

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET status = 'pending', locked_until = NULL, "  # nosec B608
                    f"available_at = NOW() + make_interval(secs => :delay), "
                    f"last_error = COALESCE(:error, last_error) WHERE id = :id"
                ),
                {"id": job.id, "delay": job.backoff().total_seconds(), "error": error},
            )

    async def release(self, job: Job) -> None:
        """Return a claimed job at once, giving back the attempt its claim took.

        For a job the worker stopped on shutdown: it did not fail, so it must
        not move towards the dead-letter queue, and it should not wait out a
        backoff it never earned.
        """
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET status = 'pending', locked_until = NULL, "  # nosec B608
                    f"available_at = NOW(), attempts = GREATEST(attempts - 1, 0) "
                    f"WHERE id = :id AND status = 'running'"
                ),
                {"id": job.id},
            )

    async def dead(self, *, limit: int = 100) -> list[DeadJob]:
        """The dead letters, newest first."""
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT id, task, attempts, max_attempts, tenant_id, last_error, "  # nosec B608
                    f"created_at FROM {self._table} WHERE status = 'dead' "
                    f"ORDER BY created_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [
                DeadJob(
                    id=row["id"],
                    task=row["task"],
                    attempts=int(row["attempts"]),
                    max_attempts=int(row["max_attempts"]),
                    tenant_id=row["tenant_id"],
                    error=row["last_error"],
                    created_at=row["created_at"],
                )
                for row in result.mappings()
            ]

    async def retry_dead(self, ids: list[str] | None = None) -> int:
        """Back to pending with a clean slate of attempts. ``None`` retries all.

        The error is kept: a replay that fails again overwrites it, and one
        that succeeds deletes the row.
        """
        from sqlalchemy import bindparam, text

        statement = (
            f"UPDATE {self._table} SET status = 'pending', attempts = 0, "  # nosec B608
            f"locked_until = NULL, available_at = NOW() WHERE status = 'dead'"
        )
        async with self._engine.begin() as conn:
            if ids is None:
                result = await conn.execute(text(statement))
            else:
                result = await conn.execute(
                    text(statement + " AND id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": list(ids)},
                )
        return int(result.rowcount or 0)

    async def stats(self) -> dict[str, int]:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT status, COUNT(*) AS total FROM {self._table} GROUP BY status")  # nosec B608
            )
            counts = {row["status"]: int(row["total"]) for row in result.mappings()}
        return {
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "dead": counts.get("dead", 0),
        }

    async def health(self) -> tuple[bool, str]:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text(f"SELECT 1 FROM {self._table} LIMIT 1"))  # nosec B608
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"queue table unreachable: {exc}"
        return True, f"postgres queue {self._table} reachable"

    async def close(self) -> None:
        # The engine belongs to the database plugin, which disposes of it.
        return None

    def __repr__(self) -> str:
        return f"<PostgresQueue table={self._table!r}>"
