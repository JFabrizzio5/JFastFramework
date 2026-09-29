"""Job queue on Redis.

Faster than the PostgreSQL queue and with a real blocking wait, so a worker
picks up a job in microseconds instead of on the next poll.

The reliability comes from ``BLMOVE``: a claimed job is moved atomically to a
per-worker processing list, so a worker that dies leaves the job visible for
recovery instead of losing it. A naive ``BRPOP`` queue drops that job on the
floor, which is why this one is more code than you might expect.

**Recovery is cross-worker, and has to be.** A worker that recovers only its
*own* processing list recovers nothing after a crash, because the process that
comes back has a different name. Every worker registers in a hash with a
heartbeat, and any worker returns the in-flight jobs of a consumer whose
heartbeat has gone stale. That is what makes the visibility
timeout documented in ``queues.base`` true here rather than aspirational.

Time comes from the Redis server (``TIME``), not from each worker's clock. Two
workers disagreeing about the time is the ordinary case, and that disagreement
would decide both when a delayed job is due and when a peer counts as dead.

Trade-off against PostgreSQL: enqueueing cannot share the transaction that
produced the work. Commit the row, crash before the ``LPUSH``, and the job
never exists. Where that matters -- money, state machines -- keep the queue in
the database.

Requires: ``pip install jfastframework[cache]``
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from typing import Any

from jfastframework.queues.base import Job

# A worker that has not touched the registry in this multiple of the visibility
# timeout counts as dead. Slack on purpose: a slow heartbeat should not cause a
# job to be delivered twice while its owner is still working on it.
DEAD_AFTER = 2.0


def default_consumer() -> str:
    """A name unique per process and stable for as long as it lives.

    ``JFAST_WORKER_ID`` wins when set, which is how a Kubernetes Deployment or
    a compose replica gets a name you can grep for in a log line.
    """
    return os.environ.get("JFAST_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


class RedisQueue:
    def __init__(
        self,
        client: Any,
        *,
        name: str = "jfast:jobs",
        visibility_timeout: int = 300,
        consumer: str | None = None,
    ) -> None:
        self._client = client
        self._name = name
        self._pending = f"{name}:pending"
        self._delayed = f"{name}:delayed"
        self._dead = f"{name}:dead"
        # consumer -> last seen, on server time. The list of who might be
        # holding a job, and therefore whose jobs might need returning.
        self._workers = f"{name}:workers"
        self._consumer = consumer or default_consumer()
        self._processing = self._processing_key(self._consumer)
        self._visibility = visibility_timeout
        self._last_reap = 0.0

    @property
    def visibility_timeout(self) -> int:
        """How long a claim is this worker's before a peer may reclaim it.

        Published so the worker can keep its handlers inside the window: a
        heartbeat marks this consumer alive, not this job, so a handler that
        outlives the window is reclaimed while it is still running.
        """
        return self._visibility

    def _processing_key(self, consumer: str) -> str:
        return f"{self._name}:processing:{consumer}"

    # -- time ----------------------------------------------------------

    async def _now(self) -> float:
        """Server time, so every worker reads the same clock."""
        try:
            seconds, microseconds = await self._client.time()
        except Exception:  # noqa: BLE001 - an older server, or a stub client
            return time.time()
        return float(seconds) + float(microseconds) / 1_000_000

    # -- worker registry -----------------------------------------------

    async def _heartbeat(self, now: float | None = None) -> None:
        stamp = now if now is not None else await self._now()
        await self._client.hset(self._workers, self._consumer, stamp)

    async def _drain(self, consumer: str) -> int:
        """Return one consumer's in-flight jobs to the pending list."""
        key = self._processing_key(consumer)
        recovered = 0
        while await self._client.rpoplpush(key, self._pending):
            recovered += 1
        return recovered

    async def reap(self, now: float | None = None, *, force: bool = False) -> int:
        """Return the in-flight jobs of every consumer that stopped reporting.

        Rate-limited to once per quarter of the visibility timeout: it reads
        the whole registry, and a worker polling an empty queue should not pay
        for that on every pass.
        """
        stamp = now if now is not None else await self._now()
        if not force and stamp - self._last_reap < max(1.0, self._visibility / 4):
            return 0
        self._last_reap = stamp

        deadline = stamp - self._visibility * DEAD_AFTER
        registry = await self._client.hgetall(self._workers)
        recovered = 0
        for raw_consumer, raw_seen in registry.items():
            consumer = _text(raw_consumer)
            if consumer == self._consumer:
                continue
            try:
                seen = float(_text(raw_seen))
            except (TypeError, ValueError):
                seen = 0.0
            if seen > deadline:
                continue
            recovered += await self._drain(consumer)
            # Forget the worker only after its list is drained, so a crash
            # midway through leaves the remainder to the next pass.
            await self._client.hdel(self._workers, consumer)
        return recovered

    # -- protocol ------------------------------------------------------

    async def setup(self) -> None:
        # Redis needs no schema. Announce this worker, take back anything an
        # earlier process under the same name left behind, then sweep for peers
        # that died while nobody was looking.
        now = await self._now()
        await self._heartbeat(now)
        await self._drain(self._consumer)
        await self.reap(now, force=True)

    async def _promote_due(self, now: float) -> None:
        """Move delayed jobs whose time has come into the pending list."""
        due = await self._client.zrangebyscore(self._delayed, 0, now, start=0, num=100)
        for raw in due:
            # Only the client that wins the ZREM enqueues it, so a job cannot
            # be promoted twice by two workers racing.
            if await self._client.zrem(self._delayed, raw):
                await self._client.lpush(self._pending, raw)

    async def enqueue(self, job: Job) -> str:
        raw = job.to_json()
        if job.available_at is not None:
            await self._client.zadd(self._delayed, {raw: job.available_at.timestamp()})
        else:
            await self._client.lpush(self._pending, raw)
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        now = await self._now()
        await self._heartbeat(now)
        await self._promote_due(now)
        await self.reap(now)

        raw = await self._client.blmove(
            self._pending, self._processing, timeout=timeout, src="RIGHT", dest="LEFT"
        )
        if raw is None:
            return None

        job = Job.from_json(raw, receipt=raw)
        job.attempts += 1
        return job

    async def ack(self, job: Job) -> None:
        # Remove this exact payload from the processing list. LREM by value is
        # correct here because the receipt is the payload that was moved.
        await self._client.lrem(self._processing, 1, job.receipt)
        await self._heartbeat()

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        await self._client.lrem(self._processing, 1, job.receipt)

        if not retry or job.exhausted:
            await self._client.lpush(self._dead, job.to_json())
            await self._heartbeat()
            return

        now = await self._now()
        await self._client.zadd(self._delayed, {job.to_json(): now + job.backoff().total_seconds()})
        await self._heartbeat(now)

    async def stats(self) -> dict[str, int]:
        registry = await self._client.hgetall(self._workers)
        running = 0
        for raw_consumer in registry:
            running += int(await self._client.llen(self._processing_key(_text(raw_consumer))))
        return {
            "pending": int(await self._client.llen(self._pending)),
            "delayed": int(await self._client.zcard(self._delayed)),
            "running": running,
            "dead": int(await self._client.llen(self._dead)),
            "workers": len(registry),
        }

    async def health(self) -> tuple[bool, str]:
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"redis queue unreachable: {exc}"
        return True, f"redis queue {self._pending} reachable"

    async def close(self) -> None:
        # Return unfinished work and leave the registry, so a rolling deploy
        # hands jobs back immediately instead of parking them until the
        # visibility timeout expires.
        with contextlib.suppress(Exception):
            await self._drain(self._consumer)
        with contextlib.suppress(Exception):
            await self._client.hdel(self._workers, self._consumer)

    def __repr__(self) -> str:
        return f"<RedisQueue name={self._pending!r} consumer={self._consumer!r}>"


def _text(value: Any) -> str:
    """Redis hands back bytes or str depending on how the client was built."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
