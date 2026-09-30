"""Background tasks declared in the module that owns them.

A handler used to live in a ``worker.py`` at the root of the service, far from
the logic it runs, because the only task registry existed inside a running
app. Every one of them opened its own session, committed by hand and checked
the tenant by hand. This module moves the declaration next to the code::

    # modules/alerta/tasks.py
    from jfastframework.tasks import TaskSession, task

    @task("alerta.revisar_presupuesto", idempotent_on=lambda p: p["comprobante_id"])
    async def revisar_presupuesto(payload: dict, session: TaskSession) -> None:
        ...  # committed when this returns, rolled back if it raises

**Discovery.** Every ``modules/<name>/tasks.py`` (or ``tasks/`` package) is
imported when the app is built -- by the API and by ``jfast worker`` alike, so
both see the same tasks and the same ``@subscribe`` declarations. Nothing
else is imported: a task declared anywhere else is only seen if something
imports it first. A ``tasks.py`` that fails to import stops the boot, rather
than leaving a task that silently never runs.

**The session.** A parameter annotated ``TaskSession`` receives a session on
the primary database, opened for the job's tenant: with ``[plugin.database]
rls = true`` every transaction is scoped to it, exactly as a request's is. It
is committed when the handler returns and rolled back when it raises, and the
job is then retried.

**Idempotency.** Delivery is at least once. ``idempotent_on`` extracts a key
from the payload and claims it in the inbox *inside the handler's
transaction*: if the work commits, so does the claim, and every redelivery
after that is skipped; if it rolls back, so does the claim, and the retry
runs. It needs the outbox's inbox table, which the queue plugin creates.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import logging
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jfastframework.errors import PluginError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from jfastframework.context import AppContext
    from jfastframework.events import Subscriber
    from jfastframework.queues.worker import TaskRegistry

    #: What a handler declares to receive its transaction. To a type checker it
    #: is the ``AsyncSession`` it is.
    TaskSession = AsyncSession
else:

    class TaskSession:
        """Marker: annotate a handler parameter with it to receive a session.

        A class at run time so the worker can find it in the signature, and
        ``AsyncSession`` to a type checker, which is what the handler uses.
        """


__all__ = [
    "TaskSession",
    "TaskSpec",
    "bind",
    "clear_declared",
    "declared_tasks",
    "discover",
    "task",
    "task_session_param",
]

logger = logging.getLogger("jfast.queue")

TASKS_MODULE = "tasks"

#: Inbox consumer names are 128 characters.
_CONSUMER_MAX = 128


def task_session_param(handler: Callable[..., Any]) -> str | None:
    """The name of the handler parameter annotated ``TaskSession``, if any.

    Resolved through ``get_type_hints`` so ``from __future__ import
    annotations`` -- a string annotation -- is read the same as a real one;
    when the hints cannot be resolved the annotation's text is compared.
    """
    try:
        hints = typing.get_type_hints(handler)
    except Exception:  # noqa: BLE001 - an unresolvable hint elsewhere in the signature
        hints = {}
    for name, parameter in inspect.signature(handler).parameters.items():
        hint = hints.get(name, parameter.annotation)
        if hint is TaskSession:
            return name
        if isinstance(hint, str) and hint.rsplit(".", 1)[-1] == "TaskSession":
            return name
    return None


@dataclass(frozen=True)
class TaskSpec:
    """One ``@task`` declaration, before any app exists to bind it to."""

    name: str
    handler: Callable[..., Awaitable[Any]] = field(compare=False)
    session_param: str | None = None
    idempotent_on: Callable[[dict[str, Any]], Any] | None = field(default=None, compare=False)
    every: timedelta | None = None
    cron: str | None = None
    timezone: str = "UTC"
    schedule_payload: Mapping[str, Any] | None = field(default=None, compare=False)
    catch_up: bool = True

    @property
    def module(self) -> str | None:
        parts = self.handler.__module__.split(".")
        return parts[1] if len(parts) > 1 and parts[0] == "modules" else None

    @property
    def needs_transaction(self) -> bool:
        return self.session_param is not None or self.idempotent_on is not None


_DECLARED: dict[str, TaskSpec] = {}


def task(
    name: str,
    *,
    idempotent_on: Callable[[dict[str, Any]], Any] | None = None,
    every: timedelta | None = None,
    cron: str | None = None,
    timezone: str = "UTC",
    payload: Mapping[str, Any] | None = None,
    catch_up: bool = True,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Declare a task at import time, in the module that owns it.

    ``name`` is the wire contract: jobs are queued under it, and a job queued
    before a rename is undeliverable after it. Prefix it with the module
    (``alerta.revisar_presupuesto``); ``contracts check`` reads ownership from
    where the decorator is, and reports a module that queues another's task
    without declaring the dependency.

    ``every``/``cron`` make it recurring, exactly as ``TaskRegistry.task``
    does; see :mod:`jfastframework.queues.scheduler`.
    """
    if not name or not isinstance(name, str):
        raise ValueError("task() needs the task name as a non-empty string")
    if every is not None or cron is not None:
        # Parsed now, so a malformed expression fails at import rather than in
        # a scheduler loop nobody is watching.
        from jfastframework.queues.schedule import Schedule

        Schedule.build(
            name, name, every=every, cron=cron, timezone=timezone, payload=payload,
            catch_up=catch_up,
        )  # fmt: skip

    def decorator(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"task {name!r}: {handler.__qualname__} must be `async def`")
        spec = TaskSpec(
            name=name,
            handler=handler,
            session_param=task_session_param(handler),
            idempotent_on=idempotent_on,
            every=every,
            cron=cron,
            timezone=timezone,
            schedule_payload=payload,
            catch_up=catch_up,
        )
        existing = _DECLARED.get(name)
        if existing is not None and (
            existing.handler.__module__,
            existing.handler.__qualname__,
        ) != (handler.__module__, handler.__qualname__):
            raise ValueError(
                f"task {name!r} is declared twice: by {existing.handler.__module__}."
                f"{existing.handler.__qualname__} and by {handler.__module__}."
                f"{handler.__qualname__}. A task name belongs to one module."
            )
        # The same function imported again (a reload, a second import path)
        # replaces its own declaration instead of failing.
        _DECLARED[name] = spec
        return handler

    return decorator


def declared_tasks() -> list[TaskSpec]:
    return [_DECLARED[name] for name in sorted(_DECLARED)]


def clear_declared() -> None:
    """For tests. Declarations accumulate across a session otherwise."""
    _DECLARED.clear()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(ctx: AppContext | None = None) -> list[str]:
    """Import every ``modules.<name>.tasks`` of this project. Idempotent.

    "This project" is the ``modules`` package importable from here, and only
    when a ``jfast.toml`` sits beside it: an unrelated package that happens to
    be called ``modules`` somewhere on ``sys.path`` is not a JFast project, and
    importing its insides would be a surprise with side effects.
    """
    try:
        spec = importlib.util.find_spec("modules")
    except (ImportError, ValueError):
        return []
    if spec is None or not spec.submodule_search_locations:
        return []

    imported: list[str] = []
    for location in spec.submodule_search_locations:
        base = Path(location)
        if not (base.parent / "jfast.toml").is_file() or not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            has_tasks = (child / f"{TASKS_MODULE}.py").is_file() or (
                child / TASKS_MODULE / "__init__.py"
            ).is_file()
            if not has_tasks:
                continue
            dotted = f"modules.{child.name}.{TASKS_MODULE}"
            # Not caught: a tasks.py that does not import is a task that never
            # runs, and the boot is the moment to say so.
            importlib.import_module(dotted)
            imported.append(dotted)
    if imported and ctx is not None:
        ctx.logger.debug("tasks discovered in %s", ", ".join(imported))
    return imported


# ---------------------------------------------------------------------------
# Binding declarations to a running app
# ---------------------------------------------------------------------------


def _needs_database(ctx: AppContext, what: str) -> None:
    if not ctx.has("db.sessionmaker"):
        raise PluginError(
            f"{what} needs a database session, but the 'database' plugin is not enabled. "
            f'Add "database" to [plugins].enabled, or drop the TaskSession parameter '
            f"(and idempotent_on) from the handler."
        )


@contextlib.asynccontextmanager
async def _transaction(ctx: AppContext) -> AsyncIterator[Any]:
    """A session for the job's tenant, committed on exit, rolled back on error."""
    from jfastframework.plugins.builtin.observability import tenant_id_var

    session: Any = ctx.require("db.sessionmaker")()
    # Explicit rather than left to the context variable: the transaction's
    # tenant is the job's, whatever the handler does to the context inside.
    tenant = tenant_id_var.get()
    if tenant:
        session.info["tenant_id"] = tenant
    async with session, session.begin():
        yield session


def _task_runner(spec: TaskSpec, ctx: AppContext) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    async def run(payload: dict[str, Any]) -> Any:
        if not spec.needs_transaction:
            return await spec.handler(payload)
        from jfastframework.outbox import claim_once

        async with _transaction(ctx) as session:
            if spec.idempotent_on is not None:
                key = str(spec.idempotent_on(payload))
                consumer = f"task:{spec.name}"[:_CONSUMER_MAX]
                if not await claim_once(session, key, consumer=consumer):
                    logger.info(
                        "task skipped: already done for this key",
                        extra={"task": spec.name, "idempotency_key": key},
                    )
                    return None
            if spec.session_param is None:
                return await spec.handler(payload)
            return await spec.handler(payload, **{spec.session_param: session})

    run.__qualname__ = f"task[{spec.name}]"
    return run


def _subscriber_runner(
    subscriber: Subscriber, ctx: AppContext
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    from jfastframework.events import Event

    async def run(payload: dict[str, Any]) -> Any:
        event = Event.from_dict(dict(payload.get("event") or {}))
        if subscriber.session_param is None:
            return await subscriber.handler(event)
        from jfastframework.outbox import claim_once

        async with _transaction(ctx) as session:
            # One effect per (event, subscriber): the claim commits with the
            # handler's writes, so a redelivery after the commit is skipped.
            consumer = subscriber.task[:_CONSUMER_MAX]
            if not await claim_once(session, event.id, consumer=consumer):
                logger.info(
                    "subscriber skipped: event already handled",
                    extra={"task": subscriber.task, "event_id": event.id},
                )
                return None
            return await subscriber.handler(event, **{subscriber.session_param: session})

    run.__qualname__ = f"subscriber[{subscriber.task}]"
    return run


def bind(registry: TaskRegistry, ctx: AppContext) -> bool:
    """Register every declared task and subscriber on ``registry``.

    Safe to call again: a name bound once is skipped, so a module imported
    after the first pass is picked up by the second. Returns whether anything
    bound needs the inbox table.

    A name already registered some other way -- ``@tasks.task`` on the
    registry itself -- is a conflict and raises: two handlers for one task
    name means one of them never runs.
    """
    from jfastframework.events import subscribers

    needs_inbox = False
    for spec in declared_tasks():
        needs_inbox |= spec.idempotent_on is not None
        if spec.name in registry.declared:
            continue
        if spec.needs_transaction:
            _needs_database(ctx, f"task {spec.name!r}")
        registry.register(spec.name, _task_runner(spec, ctx))
        registry.declared.add(spec.name)
        if spec.every is not None or spec.cron is not None:
            registry.schedule(
                spec.name,
                every=spec.every,
                cron=spec.cron,
                timezone=spec.timezone,
                payload=spec.schedule_payload,
                catch_up=spec.catch_up,
            )

    for subscriber in subscribers():
        needs_inbox |= subscriber.session_param is not None
        if subscriber.task in registry.declared:
            continue
        if subscriber.session_param is not None:
            _needs_database(ctx, f"subscriber {subscriber.task!r}")
        registry.register(subscriber.task, _subscriber_runner(subscriber, ctx))
        registry.declared.add(subscriber.task)
    return needs_inbox
