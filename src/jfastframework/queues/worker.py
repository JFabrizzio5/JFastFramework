"""Task registry and the worker loop.

Backend-agnostic: the worker only knows the ``QueueBackend`` protocol, so the
same handlers run on PostgreSQL in development and RabbitMQ in production
without an edit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from jfastframework import tracing
from jfastframework.queues.base import Job, QueueBackend, _current_job
from jfastframework.queues.schedule import Schedule

Handler = Callable[[dict[str, Any]], Awaitable[Any]]

logger = logging.getLogger("jfast.queue")


class UnknownTask(RuntimeError):
    """A job named a task nothing is registered for."""


class TaskRegistry:
    """Maps task names to handlers.

    The name is the wire contract: a job queued by yesterday's deploy is still
    in the queue when today's rolls out. Rename a task and those jobs become
    undeliverable -- add the new name and keep the old one until the queue has
    drained.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._schedules: dict[str, Schedule] = {}
        #: Names bound from module-level ``@task``/``@subscribe`` declarations,
        #: so binding again skips them instead of colliding with itself.
        self.declared: set[str] = set()

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"Task {name!r} is already registered")
        self._handlers[name] = handler

    def task(
        self,
        name: str,
        *,
        every: timedelta | None = None,
        cron: str | None = None,
        timezone: str = "UTC",
        payload: Mapping[str, Any] | None = None,
        catch_up: bool = True,
    ) -> Callable[[Handler], Handler]:
        """Decorator form, optionally recurring::

        @tasks.task("send_invoice_email")
        async def send_invoice_email(payload): ...

        @tasks.task("refresh_rates", every=timedelta(minutes=5))
        async def refresh_rates(payload): ...

        @tasks.task("nightly_report", cron="0 3 * * *", timezone="America/Mexico_City")
        async def nightly_report(payload): ...

        A recurring task runs only where the scheduler does: ``[plugin.queue]
        scheduler = true``. See :meth:`schedule`.
        """
        # Parsed now, so a malformed expression fails at import rather than
        # in a scheduler loop nobody is watching.
        recurring = None
        if every is not None or cron is not None:
            recurring = Schedule.build(
                name,
                name,
                every=every,
                cron=cron,
                timezone=timezone,
                payload=payload,
                catch_up=catch_up,
            )

        def decorator(handler: Handler) -> Handler:
            self.register(name, handler)
            if recurring is not None:
                self._add_schedule(recurring)
            return handler

        return decorator

    def schedule(
        self,
        task: str,
        *,
        every: timedelta | None = None,
        cron: str | None = None,
        timezone: str = "UTC",
        payload: Mapping[str, Any] | None = None,
        name: str | None = None,
        catch_up: bool = True,
        max_attempts: int = 3,
    ) -> Schedule:
        """Run ``task`` on a schedule: ``every`` an interval, or on a ``cron`` expression.

        ``name`` identifies the schedule and defaults to the task's; give one to
        run the same task on two schedules, e.g. with different payloads. Like
        a task name it is a contract: ticks are claimed under it, so renaming a
        schedule makes it look new, and a new schedule does not catch up.

        Intervals count from the Unix epoch in UTC, so ``every=timedelta(hours=1)``
        fires on the hour and every replica agrees on when that is. Cron
        expressions are read in ``timezone`` (a zoneinfo name, UTC by default).
        After downtime the most recent missed tick runs once, never a burst;
        ``catch_up=False`` skips it.

        The handler receives ``payload`` plus ``scheduled_for``, the tick's time
        as ISO-8601 UTC -- the time the run is *for*, which after a catch-up is
        not now.
        """
        schedule = Schedule.build(
            name or task,
            task,
            every=every,
            cron=cron,
            timezone=timezone,
            payload=payload,
            catch_up=catch_up,
            max_attempts=max_attempts,
        )
        self._add_schedule(schedule)
        return schedule

    def _add_schedule(self, schedule: Schedule) -> None:
        if schedule.name in self._schedules:
            raise ValueError(
                f"Schedule {schedule.name!r} is already registered; pass name= to run "
                f"the same task on a second schedule"
            )
        self._schedules[schedule.name] = schedule

    @property
    def schedules(self) -> tuple[Schedule, ...]:
        return tuple(self._schedules[name] for name in sorted(self._schedules))

    def get(self, name: str) -> Handler:
        try:
            return self._handlers[name]
        except KeyError:
            raise UnknownTask(
                f"No handler for task {name!r}. Registered: "
                f"{', '.join(sorted(self._handlers)) or '<none>'}"
            ) from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def has(self, name: str) -> bool:
        return name in self._handlers


#: How much of the visibility timeout a job may use when nobody says.
#:
#: A claim is invisible to other workers for the backend's visibility timeout
#: and nothing extends it while the handler runs. So a handler that outlives
#: that window is claimed *again*, by another worker, while the first one is
#: still inside it -- and a queue that runs a job twice is a charge taken
#: twice, an email sent twice, a row written twice. Nothing fails, and nothing
#: in the log says "this ran concurrently".
#:
#: The two numbers live in different places -- `[plugin.queue]
#: visibility_timeout` and the worker's `job_timeout` -- and two equal defaults
#: race at the boundary, while raising one without the other makes duplicate
#: execution certain rather than likely. So the worker derives its own ceiling
#: from the backend: a job is cancelled with a fifth of the window still to
#: spare, which is the margin `nack` needs to land before anybody else may
#: claim.
JOB_TIMEOUT_SHARE = 0.8

#: How long a stopping worker waits for its in-flight jobs when nobody says.
#:
#: Kubernetes sends SIGKILL 30 s after SIGTERM by default, and a process killed
#: mid-job releases nothing: its jobs sit invisible until the visibility
#: timeout runs out. Five seconds short of that leaves room to hand the
#: unfinished ones back.
DEFAULT_DRAIN_SECONDS = 25.0


class Worker:
    """Claims jobs and runs their handlers until told to stop.

    Stopping is graceful: no new job is claimed, jobs already running get up
    to ``drain_timeout`` seconds to finish, and whatever is still running then
    is cancelled and *released* -- returned to the queue at once, without
    spending an attempt -- so the next worker takes it immediately instead of
    after the visibility timeout.
    """

    def __init__(
        self,
        backend: QueueBackend,
        registry: TaskRegistry,
        *,
        concurrency: int = 4,
        poll_timeout: float = 5.0,
        job_timeout: float | None = None,
        drain_timeout: float = DEFAULT_DRAIN_SECONDS,
        close_backend: bool = True,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.concurrency = concurrency
        self.poll_timeout = poll_timeout
        self.job_timeout = self._resolve_job_timeout(backend, job_timeout)
        self.drain_timeout = drain_timeout
        # False when the backend belongs to someone else -- the queue plugin,
        # which closes it at shutdown. Closing it twice is harmless for
        # PostgreSQL and not for a broker connection.
        self.close_backend = close_backend
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _resolve_job_timeout(backend: QueueBackend, requested: float | None) -> float:
        """The ceiling a handler runs under, checked against the claim's.

        A backend that redelivers on the connection rather than on a clock --
        RabbitMQ -- publishes no visibility timeout, and there is nothing to
        compare against: the claim lasts as long as the connection does.
        """
        visibility = getattr(backend, "visibility_timeout", None)
        if visibility is None:
            return 300.0 if requested is None else requested
        if requested is None:
            return float(visibility) * JOB_TIMEOUT_SHARE
        if requested >= float(visibility):
            raise ValueError(
                f"job_timeout={requested:g}s is not shorter than the queue's "
                f"visibility_timeout={visibility:g}s. A handler that runs past the "
                f"visibility timeout is claimed again by another worker while this "
                f"one is still inside it, so the job runs twice and neither run "
                f"knows. Lower job_timeout, or raise [plugin.queue] "
                f"visibility_timeout above the longest job this worker runs."
            )
        return requested

    async def run(self) -> None:
        """Run until :meth:`stop` is called."""
        await self.backend.setup()
        logger.info("worker started", extra={"tasks": list(self.registry.names)})

        # A semaphore rather than N loops: one claim path is easier to reason
        # about, and the concurrency limit is then exact.
        limiter = asyncio.Semaphore(self.concurrency)

        loop = asyncio.get_running_loop()

        while not self._stopping.is_set():
            # Interruptible: with every slot busy, a plain acquire would sit
            # until a job finished and then claim *another* one -- a stopping
            # worker taking new work is exactly what SIGTERM must prevent.
            if not await self._acquire_or_stop(limiter):
                break
            started = loop.time()
            try:
                job = await self.backend.dequeue(timeout=self.poll_timeout)
            except Exception:
                limiter.release()
                logger.exception("dequeue failed")
                # Do not spin on a broken broker.
                await asyncio.sleep(1.0)
                continue

            if job is None:
                limiter.release()
                # Redis and RabbitMQ block for `timeout` themselves. PostgreSQL
                # polls and returns instantly, so without sleeping the
                # remainder this loop never yields: it burns a core and starves
                # the event loop, which is how "the worker is running" and
                # "nothing else responds" happen at the same time.
                idle = self.poll_timeout - (loop.time() - started)
                if idle > 0:
                    await self._sleep_or_stop(idle)
                else:
                    await asyncio.sleep(0)
                continue

            if self._stopping.is_set():
                # Claimed while the stop arrived: hand it straight back.
                await self._release(job)
                limiter.release()
                break

            task = asyncio.create_task(self._run_job(job, limiter))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        await self._drain()
        if self.close_backend:
            await self.backend.close()
        logger.info("worker stopped")

    async def _drain(self) -> None:
        """Let in-flight jobs finish, up to ``drain_timeout``; release the rest.

        Killing a handler mid-write is how a queue produces half-applied side
        effects, so the first choice is to wait. Waiting forever is not a
        choice: the orchestrator's SIGKILL arrives on its own clock, and a job
        still running then is neither finished nor handed back.
        """
        if not self._tasks:
            return
        running = set(self._tasks)
        logger.info(
            "draining %d in-flight job(s), up to %gs", len(running), self.drain_timeout
        )
        _, unfinished = await asyncio.wait(running, timeout=self.drain_timeout)
        if not unfinished:
            return
        logger.warning(
            "%d job(s) did not finish within %gs; releasing them to the queue",
            len(unfinished),
            self.drain_timeout,
        )
        for task in unfinished:
            task.cancel()
        # Each cancelled job releases itself in `_run_job`; wait for that to
        # land before the backend is closed underneath it.
        await asyncio.gather(*unfinished, return_exceptions=True)

    async def _acquire_or_stop(self, limiter: asyncio.Semaphore) -> bool:
        """A concurrency slot, or False as soon as the worker is stopping."""
        if self._stopping.is_set():
            return False
        acquire = asyncio.ensure_future(limiter.acquire())
        stop = asyncio.ensure_future(self._stopping.wait())
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not acquire.done():
            acquire.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await acquire
        got = acquire.done() and not acquire.cancelled()
        if got and self._stopping.is_set():
            limiter.release()
            return False
        return got

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Idle, but wake immediately on stop().

        A plain sleep here would add up to `poll_timeout` to every shutdown,
        which an orchestrator counts against its termination grace period.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _run_job(self, job: Job, limiter: asyncio.Semaphore) -> None:
        try:
            handler = self.registry.get(job.task)
        except UnknownTask as exc:
            # Never retry: no deploy will make this job deliverable, and
            # retrying it forever masks the real problem.
            logger.error("%s", exc, extra={"job_id": job.id, "task": job.task})
            job.error = f"UnknownTask: {exc}"
            await self.backend.nack(job, retry=False)
            limiter.release()
            return

        # The handler runs as the request that queued it: same tenant, same
        # request id. Without this a job ran with no tenant at all, so every
        # repository it opened read every tenant's rows, and its log lines
        # could not be traced back to anything.
        from jfastframework.plugins.builtin.observability import request_id_var, tenant_id_var

        request_token = request_id_var.set(job.request_id)
        tenant_token = tenant_id_var.set(job.tenant_id)
        job_token = _current_job.set(job)
        try:
            # The trace of the request that queued it, so this job's spans are
            # children of that request instead of an orphan trace of their own.
            with (
                tracing.attach(job.trace),
                tracing.span(
                    f"job {job.task}",
                    job_id=job.id,
                    task=job.task,
                    attempt=job.attempts,
                    tenant_id=job.tenant_id or "",
                ),
            ):
                await asyncio.wait_for(handler(job.payload), timeout=self.job_timeout)
        except asyncio.CancelledError:
            # Stopped by a shutdown that could not wait any longer. The job did
            # not fail; hand it back without spending an attempt.
            logger.warning("job released on shutdown", extra={"job_id": job.id, "task": job.task})
            await self._release(job)
            raise
        except TimeoutError:
            logger.error("job timed out", extra={"job_id": job.id, "task": job.task})
            job.error = f"TimeoutError: ran longer than {self.job_timeout:g}s"
            await self.backend.nack(job, retry=True)
        except Exception as exc:
            logger.exception(
                "job failed",
                extra={"job_id": job.id, "task": job.task, "attempts": job.attempts},
            )
            job.error = f"{type(exc).__name__}: {exc}"
            await self.backend.nack(job, retry=True)
        else:
            await self.backend.ack(job)
            logger.info("job done", extra={"job_id": job.id, "task": job.task})
        finally:
            _current_job.reset(job_token)
            tenant_id_var.reset(tenant_token)
            request_id_var.reset(request_token)
            limiter.release()

    async def _release(self, job: Job) -> None:
        release = getattr(self.backend, "release", None)
        try:
            if release is not None:
                await release(job)
            else:
                await self.backend.nack(job, retry=True)
        except Exception:
            # The visibility timeout is the backstop: the job comes back when
            # the claim expires, later than it should but not lost.
            logger.exception("could not release job", extra={"job_id": job.id})

    def stop(self) -> None:
        """Stop claiming. :meth:`run` returns once in-flight jobs are drained."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns True if one was handled.

        Exists for tests and for cron-style workers, where a long-lived
        process is the wrong shape.
        """
        job = await self.backend.dequeue(timeout=0.1)
        if job is None:
            return False
        limiter = asyncio.Semaphore(1)
        await limiter.acquire()
        await self._run_job(job, limiter)
        return True


@contextlib.asynccontextmanager
async def running(worker: Worker):  # type: ignore[no-untyped-def]
    """Run a worker in the background for the duration of the block."""
    task = asyncio.create_task(worker.run())
    try:
        yield worker
    finally:
        worker.stop()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=10)
