"""The scheduler: turns due ticks of recurring tasks into jobs, once.

Runs inside the service when ``[plugin.queue] scheduler = true``, and is meant
to run in **every** replica and every worker process. Nothing elects a leader;
each tick is *claimed* in a store all of them share before it is enqueued, and
only the process whose claim lands enqueues it:

- with the ``database`` plugin, a row in ``jfast_schedule_ticks`` whose
  primary key is ``(name, fire_at)``, inserted with ``ON CONFLICT DO
  NOTHING``. When the queue is the PostgreSQL one on the same engine, the
  claim and the job commit in one transaction: both happen or neither does.
- with only the ``cache`` plugin, ``SET NX`` on a key per tick, with a TTL.
- ``memory``: one process only, for tests and single-process development.

Leader election would have one process fire and N-1 idle, and a failover gap
every time the leader dies. Claiming per tick has no leader to lose.

Each tick's job has an id derived from ``(name, fire time)``, so the rare
double enqueue -- a claim that landed but whose answer was lost -- is one job
id twice, which the PostgreSQL queue inserts once and a handler can
deduplicate with ``claim_once``.

**Missed ticks.** A tick is due when its time has passed and it is newer than
the last one claimed. After downtime that is the most recent missed tick,
which fires once; the ones before it are skipped, never replayed as a burst.
A schedule with no claim on record at all is new, and starts with the next
tick rather than one in the past.

**Where it falls short.** A process that dies between claiming a tick and
enqueueing it -- possible whenever the claim store and the queue are
different systems -- loses that tick. A failed enqueue releases the claim, so
another pass or another replica takes it, but a killed process cannot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from jfastframework.queues.base import Job, QueueBackend, utcnow
from jfastframework.queues.schedule import Schedule
from jfastframework.queues.worker import TaskRegistry

__all__ = [
    "MemoryTickStore",
    "RedisTickStore",
    "Scheduler",
    "TickStore",
]

logger = logging.getLogger("jfast.scheduler")

#: Receives the store's open transaction, or None when the store has none to
#: share, and enqueues the tick's job.
Enqueue = Callable[[Any], Awaitable[None]]

#: A Redis tick key outlives its tick by at least this, so a replica whose
#: clock is behind still finds the claim. Two periods of the schedule when
#: that is longer.
_MIN_HOLD = timedelta(minutes=10)


def default_owner() -> str:
    """Recorded with each claim, so an operator can see which process fired."""
    return os.environ.get("JFAST_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


class TickStore(Protocol):
    """Where replicas agree on who fires a tick."""

    kind: str

    async def setup(self) -> None: ...

    async def last(self, name: str) -> datetime | None:
        """The latest tick claimed for this schedule, by anyone."""
        ...

    async def fire(
        self, name: str, fire_at: datetime, *, hold: timedelta, enqueue: Enqueue
    ) -> bool:
        """Claim the tick and, if this process won it, enqueue.

        Returns whether this process fired it. If ``enqueue`` raises, the
        claim is released and the exception propagates, so the tick is
        retried rather than recorded as fired.
        """
        ...

    async def prune(self, before: datetime) -> None:
        """Forget claims older than ``before``, keeping each schedule's latest."""
        ...


class MemoryTickStore:
    """Claims held in this process. Two processes each fire every tick."""

    kind = "memory"

    def __init__(self) -> None:
        self._claims: dict[str, set[datetime]] = {}
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        return None

    async def last(self, name: str) -> datetime | None:
        claims = self._claims.get(name)
        return max(claims) if claims else None

    async def fire(
        self, name: str, fire_at: datetime, *, hold: timedelta, enqueue: Enqueue
    ) -> bool:
        async with self._lock:
            claims = self._claims.setdefault(name, set())
            if fire_at in claims:
                return False
            claims.add(fire_at)
        try:
            await enqueue(None)
        except BaseException:
            claims.discard(fire_at)
            raise
        return True

    async def prune(self, before: datetime) -> None:
        for claims in self._claims.values():
            latest = max(claims, default=None)
            claims.difference_update({tick for tick in claims if tick < before and tick != latest})


# Sets the "latest tick" key only forward. Two replicas can win two different
# ticks in either order when one of them is behind; the later tick has to win.
_ADVANCE_LAST = """
local current = redis.call('GET', KEYS[1])
if (not current) or tonumber(current) < tonumber(ARGV[1]) then
    redis.call('SET', KEYS[1], ARGV[1])
end
return 1
"""


class RedisTickStore:
    """Claims as ``SET NX`` keys, for a service with a cache and no database."""

    kind = "cache"

    def __init__(self, client: Any, *, prefix: str, owner: str | None = None) -> None:
        self._client = client
        self._prefix = prefix
        self._owner = owner or default_owner()

    def _tick_key(self, name: str, fire_at: datetime) -> str:
        return f"{self._prefix}:tick:{name}:{_millis(fire_at)}"

    def _last_key(self, name: str) -> str:
        return f"{self._prefix}:last:{name}"

    async def setup(self) -> None:
        return None

    async def last(self, name: str) -> datetime | None:
        raw = await self._client.get(self._last_key(name))
        if raw is None:
            return None
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)

    async def fire(
        self, name: str, fire_at: datetime, *, hold: timedelta, enqueue: Enqueue
    ) -> bool:
        key = self._tick_key(name, fire_at)
        won = await self._client.set(key, self._owner, nx=True, px=int(hold.total_seconds() * 1000))
        if not won:
            return False
        try:
            await enqueue(None)
        except BaseException:
            await self._client.delete(key)
            raise
        await self._client.eval(_ADVANCE_LAST, 1, self._last_key(name), _millis(fire_at))
        return True

    async def prune(self, before: datetime) -> None:
        # Tick keys expire on their own; the latest-tick key is one per
        # schedule and is the history catch-up reads.
        return None


def _millis(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


class Scheduler:
    """Enqueues each due tick of every schedule in a :class:`TaskRegistry`."""

    def __init__(
        self,
        queue: QueueBackend,
        registry: TaskRegistry,
        store: TickStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_sleep: float = 30.0,
        retry_delay: float = 5.0,
        retention: timedelta = timedelta(days=7),
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.store = store
        self.clock = clock
        # The loop recomputes at least this often, so a wall-clock jump or a
        # schedule added after start is noticed without waiting for a tick.
        self.max_sleep = max_sleep
        self.retry_delay = retry_delay
        self.retention = retention
        # Per schedule, the newest tick this process has dealt with, whether
        # it fired it or another replica did.
        self._seen: dict[str, datetime] = {}
        self._fired: dict[str, datetime] = {}
        self._previous_pass: datetime | None = None
        self._warned_unknown: set[str] = set()
        self._ready = False
        self._running = False
        self._error: str | None = None
        self._last_prune = 0.0

    # -- one pass ------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> list[Job]:
        """Fire whatever is due at ``now``. Returns the jobs this process enqueued."""
        now = now if now is not None else self.clock()
        if self._previous_pass is None:
            self._previous_pass = now

        fired: list[Job] = []
        failures: list[Exception] = []
        for schedule in self.registry.schedules:
            try:
                job = await self._consider(schedule, now, self._previous_pass)
            except Exception as exc:
                # One schedule's failure -- its enqueue, its claim -- must not
                # hold up the others in the same pass. Its tick stays unseen,
                # so the next pass tries it again.
                logger.exception("schedule %s failed to fire", schedule.name)
                failures.append(exc)
                continue
            if job is not None:
                fired.append(job)

        self._previous_pass = now
        if failures:
            raise failures[0]
        return fired

    async def _consider(
        self, schedule: Schedule, now: datetime, previous_pass: datetime
    ) -> Job | None:
        due = schedule.latest_at_or_before(now)
        if due is None:
            return None
        seen = self._seen.get(schedule.name)
        if seen is None:
            seen = await self._starting_point(schedule, previous_pass)
            self._seen[schedule.name] = seen
        if due <= seen:
            return None
        job = await self._fire(schedule, due)
        self._seen[schedule.name] = due
        return job

    async def _starting_point(self, schedule: Schedule, previous_pass: datetime) -> datetime:
        """The newest tick to treat as already dealt with, on first sight.

        With a claim on record, that claim: anything newer and already due is
        a missed tick, and fires once. With none the schedule is new, and the
        ticks before this process started looking are not owed to anyone.
        """
        reference = schedule.latest_at_or_before(previous_pass)
        last = await self.store.last(schedule.name)
        floor = reference if reference is not None else datetime.min.replace(tzinfo=UTC)
        if last is None:
            return floor
        if schedule.catch_up:
            return last
        return max(last, floor)

    async def _fire(self, schedule: Schedule, due: datetime) -> Job | None:
        if schedule.task not in self.registry.names and schedule.task not in self._warned_unknown:
            # Not an error: the handler may live in a separate worker service.
            # A typo, though, dead-letters every tick, so say it once.
            self._warned_unknown.add(schedule.task)
            logger.warning(
                "schedule %s names task %s, which this process does not register",
                schedule.name,
                schedule.task,
            )
        job = Job(
            id=schedule.job_id(due),
            task=schedule.task,
            payload={**schedule.payload, "scheduled_for": due.isoformat()},
            max_attempts=schedule.max_attempts,
            # The tick's own id, so every log line of this run greps together.
            request_id=schedule.job_id(due),
            tenant_id=None,
        )
        following = schedule.next_after(due)
        hold = max(2 * (following - due), _MIN_HOLD)

        async def enqueue(transaction: Any) -> None:
            # The PostgreSQL queue on the claim's own engine takes the job in
            # the claim's transaction: both commit or neither does.
            direct = getattr(self.queue, "enqueue_in", None)
            shared = getattr(self.queue, "engine", None) is getattr(self.store, "engine", object())
            if transaction is not None and direct is not None and shared:
                await direct(transaction, job)
            else:
                await self.queue.enqueue(job)

        if not await self.store.fire(schedule.name, due, hold=hold, enqueue=enqueue):
            return None
        self._fired[schedule.name] = due
        logger.info(
            "scheduled task enqueued",
            extra={"schedule": schedule.name, "task": schedule.task, "fire_at": due.isoformat()},
        )
        return job

    # -- the loop ------------------------------------------------------

    def seconds_until_next(self, now: datetime) -> float:
        """How long the loop may sleep: to the nearest tick, and no longer than max_sleep."""
        waits = [
            (schedule.next_after(now) - now).total_seconds() for schedule in self.registry.schedules
        ]
        # A little past the tick rather than on it: an event loop timer may
        # wake a hair early, and a pass just before the tick finds nothing.
        return max(0.05, min([*waits, self.max_sleep]) + 0.05)

    async def run(self, stop: asyncio.Event) -> None:
        """Run passes until ``stop`` is set. Never raises on a failed pass."""
        self._running = True
        try:
            while not stop.is_set():
                try:
                    if not self._ready:
                        await self.store.setup()
                        self._ready = True
                    await self.tick()
                    await self._maybe_prune()
                    self._error = None
                    delay = self.seconds_until_next(self.clock())
                except Exception as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
                    logger.exception("scheduler pass failed; retrying")
                    delay = self.retry_delay
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
        finally:
            self._running = False

    async def _maybe_prune(self) -> None:
        if time.monotonic() - self._last_prune < 3600:
            return
        self._last_prune = time.monotonic()
        await self.store.prune(self.clock() - self.retention)

    # -- reporting -----------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._running and self._error is None

    def status(self) -> dict[str, Any]:
        now = self.clock()
        schedules = []
        for schedule in self.registry.schedules:
            entry = schedule.describe()
            try:
                entry["next"] = schedule.next_after(now).isoformat()
            except ValueError as exc:
                entry["next"] = None
                entry["error"] = str(exc)
            fired = self._fired.get(schedule.name)
            entry["last_fired_here"] = fired.isoformat() if fired is not None else None
            schedules.append(entry)
        return {
            "running": self._running,
            "store": self.store.kind,
            "error": self._error,
            "schedules": schedules,
        }
