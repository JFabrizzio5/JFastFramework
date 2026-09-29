"""The job queue contract.

Same shape as the vector stores: one protocol, several backends, chosen in
configuration.

    [plugin.queue]
    backend = "postgres"    # or "redis", "rabbitmq", or your own class

Two properties every backend must provide, because code written against one
and run against another otherwise breaks in production:

**At-least-once delivery.** A job can be delivered twice — the worker can die
after doing the work and before acknowledging. Handlers must be idempotent.
No backend here promises exactly-once, because none of them can.

**Visibility timeout.** A job taken by a worker is invisible to other workers
for a bounded time, then returns to the queue. That is what makes a killed
worker's job get retried instead of lost.

Retries are bounded by ``max_attempts``; a job that exhausts them goes to the
dead-letter queue rather than looping forever. A retry loop with no ceiling is
how one poison message consumes a whole worker pool.
"""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

#: The job the current handler is running. A handler receives only its
#: payload; this is how it reaches the job id to deduplicate on
#: (``claim_once(session, current_job().id)``) without every handler in every
#: service changing signature.
_current_job: ContextVar[Job | None] = ContextVar("jfast_current_job", default=None)


def current_job() -> Job:
    """The job whose handler is running. Raises outside a handler."""
    job = _current_job.get()
    if job is None:
        raise RuntimeError("current_job() was called outside a queue handler")
    return job


def _context(name: str) -> str | None:
    # Imported late: the observability plugin owns these variables, and the
    # queue package must not import a plugin at module load.
    from jfastframework.plugins.builtin import observability

    value: str | None = getattr(observability, name).get()
    return value


@dataclass
class Job:
    """One unit of work."""

    task: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempts: int = 0
    max_attempts: int = 3
    # Set for delayed jobs; None means "as soon as a worker is free".
    available_at: datetime | None = None
    # Taken from the request that queued the job, so its logs correlate with
    # that request and the worker runs it as the same tenant. A job built
    # outside a request carries neither unless it is given them.
    request_id: str | None = field(default_factory=lambda: _context("request_id_var"))
    tenant_id: str | None = field(default_factory=lambda: _context("tenant_id_var"))
    # Backend-specific handle needed to ack/nack this exact delivery.
    receipt: Any = field(default=None, repr=False, compare=False)

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "task": self.task,
                "payload": self.payload,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "request_id": self.request_id,
                "tenant_id": self.tenant_id,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str | bytes, *, receipt: Any = None) -> Job:
        data = json.loads(raw)
        return cls(
            id=data["id"],
            task=data["task"],
            payload=data.get("payload", {}),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            request_id=data.get("request_id"),
            tenant_id=data.get("tenant_id"),
            receipt=receipt,
        )

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    def backoff(self, base_seconds: float = 2.0, cap_seconds: float = 300.0) -> timedelta:
        """Exponential backoff, capped.

        Uncapped exponential backoff on a job with 10 retries schedules the
        last one days out, which looks like the job silently vanished.
        """
        delay = min(base_seconds * (2 ** max(self.attempts - 1, 0)), cap_seconds)
        return timedelta(seconds=delay)


def utcnow() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class QueueBackend(Protocol):
    """What the ``queue`` plugin needs from a broker."""

    async def setup(self) -> None:
        """Create tables, streams or exchanges if they do not exist."""
        ...

    async def enqueue(self, job: Job) -> str:
        """Publish a job. Returns its id."""
        ...

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        """Claim one job, or return None when the wait elapses.

        The claim must be invisible to other workers for the visibility
        timeout, and must return to the queue if never acknowledged.
        """
        ...

    async def ack(self, job: Job) -> None:
        """Mark the job done. It must never be delivered again."""
        ...

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        """Return the job for retry, or dead-letter it when exhausted."""
        ...

    async def stats(self) -> dict[str, int]:
        """Queue depths, for the health check and for operators."""
        ...

    async def health(self) -> tuple[bool, str]: ...

    async def close(self) -> None: ...
