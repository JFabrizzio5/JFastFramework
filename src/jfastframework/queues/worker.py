"""Task registry and the worker loop.

Backend-agnostic: the worker only knows the ``QueueBackend`` protocol, so the
same handlers run on PostgreSQL in development and RabbitMQ in production
without an edit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jfastframework.queues.base import Job, QueueBackend

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

    def register(self, name: str, handler: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"Task {name!r} is already registered")
        self._handlers[name] = handler

    def task(self, name: str) -> Callable[[Handler], Handler]:
        """Decorator form::

        @tasks.task("send_invoice_email")
        async def send_invoice_email(payload): ...
        """

        def decorator(handler: Handler) -> Handler:
            self.register(name, handler)
            return handler

        return decorator

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


#: How much of the visibility timeout a job may use when nobody says.
#:
#: A claim is invisible to other workers for the backend's visibility timeout
#: and nothing extends it while the handler runs. So a handler that outlives
#: that window is claimed *again*, by another worker, while the first one is
#: still inside it -- and a queue that runs a job twice is a charge taken
#: twice, an email sent twice, a row written twice. Nothing fails, and nothing
#: in the log says "this ran concurrently".
#:
#: The two numbers used to default to the same 300 seconds and live in
#: different places -- `[plugin.queue] visibility_timeout` and the worker's
#: `job_timeout` -- so they raced at the boundary, and raising one without the
#: other made duplicate execution certain rather than likely. The worker
#: derives its own ceiling from the backend now: a job is cancelled with a
#: fifth of the window still to spare, which is the margin `nack` needs to land
#: before anybody else may claim.
JOB_TIMEOUT_SHARE = 0.8


class Worker:
    """Claims jobs and runs their handlers until told to stop."""

    def __init__(
        self,
        backend: QueueBackend,
        registry: TaskRegistry,
        *,
        concurrency: int = 4,
        poll_timeout: float = 5.0,
        job_timeout: float | None = None,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.concurrency = concurrency
        self.poll_timeout = poll_timeout
        self.job_timeout = self._resolve_job_timeout(backend, job_timeout)
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
            await limiter.acquire()
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

            task = asyncio.create_task(self._run_job(job, limiter))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # Let in-flight jobs finish; killing them mid-write is how a queue
        # produces half-applied side effects.
        if self._tasks:
            logger.info("draining %d in-flight job(s)", len(self._tasks))
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.backend.close()
        logger.info("worker stopped")

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
        try:
            await asyncio.wait_for(handler(job.payload), timeout=self.job_timeout)
        except TimeoutError:
            logger.error("job timed out", extra={"job_id": job.id, "task": job.task})
            await self.backend.nack(job, retry=True)
        except Exception:
            logger.exception(
                "job failed",
                extra={"job_id": job.id, "task": job.task, "attempts": job.attempts},
            )
            await self.backend.nack(job, retry=True)
        else:
            await self.backend.ack(job)
            logger.info("job done", extra={"job_id": job.id, "task": job.task})
        finally:
            tenant_id_var.reset(tenant_token)
            request_id_var.reset(request_token)
            limiter.release()

    def stop(self) -> None:
        self._stopping.set()

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
