"""Domain events: one module says what happened, others react -- durably.

A module that has to tell another one "a receipt was registered" has two
honest options. It can call the other module's ``public.py``, which makes it
depend on that module; or it can publish an event and let whoever cares
subscribe, which makes it depend on nothing. The second is how a cycle
between two modules is broken, and it has to work in the default stack --
PostgreSQL and its queue, no Kafka -- or the advice to use it is a dead end::

    # modules/comprobante/services/comprobante_service.py
    from jfastframework.events import Event

    await outbox.publish(session, "comprobantes", Event(
        type="comprobante.registrado", data={"id": comprobante.id},
    ))

    # modules/alerta/tasks.py
    from jfastframework.events import Event, subscribe
    from jfastframework.tasks import TaskSession

    @subscribe("comprobante.registrado")
    async def revisar_presupuesto(event: Event, session: TaskSession) -> None: ...

**Delivery.** ``outbox.publish`` looks up the local subscribers of the event's
*type* and queues one job per subscriber through the request's own session:
the jobs exist if and only if the transaction that published commits. The
worker runs each one with the event rebuilt, the publishing request's tenant
and request id restored, and its trace attached. When a Kafka bus is
configured the event also goes to its *topic* through the outbox relay, for
other services; local subscribers are still served by the queue, so turning
Kafka on does not change who runs what.

**Matching is on the event type, never the topic.** The type is the domain
fact (``comprobante.registrado``) and is what ``contracts.toml`` declares; the
topic is a transport detail -- how Kafka partitions and prefixes -- that a
module inside the same service has no reason to know.

**At least once.** A worker can die between committing and acknowledging, so
a subscriber can run twice for one event. A subscriber that takes a
``TaskSession`` is deduplicated for you: the event id is claimed in the inbox
inside the same transaction as its writes, so a redelivery after a commit is
skipped. One without a session must be idempotent by itself.

**Undeliverable is an error.** Publishing an event with no subscriber in this
service and no bus raises :class:`UndeliverableEvent` inside the request --
the alternative was a 201 and a row retried until it died, which is what
happened before this module existed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jfastframework import tracing
from jfastframework.errors import PluginError

__all__ = [
    "Event",
    "Subscriber",
    "UndeliverableEvent",
    "clear_subscribers",
    "subscribe",
    "subscribers",
    "subscribers_for",
]


def _context(name: str) -> str | None:
    # Imported late: the observability plugin owns these variables, and this
    # module must not import a plugin at module load.
    from jfastframework.plugins.builtin import observability

    value: str | None = getattr(observability, name).get()
    return value


@dataclass
class Event:
    """Something that happened. Past tense, always.

    Built inside a request, it carries that request's tenant, request id and
    trace context -- exactly as a ``Job`` does -- so whoever reacts to it runs
    as the same tenant and its logs and spans join the request's. Built
    outside one, it carries none of them unless it is given them.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = field(default_factory=lambda: _context("request_id_var"))
    tenant_id: str | None = field(default_factory=lambda: _context("tenant_id_var"))
    # W3C trace context of the code that built the event; {} without telemetry.
    trace: dict[str, str] = field(default_factory=tracing.inject)
    # Kafka partitions by key: same key, same partition, order preserved.
    # Use the aggregate id, or events about one order can be processed out of
    # order by two consumers.
    key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "occurred_at": self.occurred_at.isoformat(),
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "trace": dict(self.trace),
            "key": self.key,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, key: str | None = None) -> Event:
        occurred = payload.get("occurred_at")
        return cls(
            id=payload.get("id") or uuid.uuid4().hex,
            type=payload["type"],
            source=payload.get("source", ""),
            occurred_at=datetime.fromisoformat(occurred) if occurred else datetime.now(UTC),
            # Explicit, including None: a rebuilt event is the one that was
            # sent, never the context of whoever happens to rebuild it.
            request_id=payload.get("request_id"),
            tenant_id=payload.get("tenant_id"),
            trace=dict(payload.get("trace") or {}),
            data=payload.get("data", {}),
            key=key if key is not None else payload.get("key"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes, *, key: str | None = None) -> Event:
        return cls.from_dict(json.loads(raw), key=key)


class UndeliverableEvent(PluginError):
    """An event was published that nothing can ever receive.

    Raised in the request that publishes it, so the caller sees the mistake
    while it can still be fixed -- rather than a 201 and a row that is retried
    until it dies with nobody watching.
    """

    title = "Undeliverable Event"


SubscriberHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Subscriber:
    """One reaction to one event type, run as a queue job."""

    event_type: str
    name: str
    handler: SubscriberHandler = field(compare=False)
    #: The handler's parameter annotated ``TaskSession``, if it takes one.
    session_param: str | None = None
    max_attempts: int = 5

    @property
    def task(self) -> str:
        """The queue task name. Part of the wire contract, like any task name:
        a job queued before a rename is undeliverable after it."""
        return f"{self.event_type}->{self.name}"

    @property
    def module(self) -> str | None:
        """The project module that declared it, from ``modules.<name>....``."""
        parts = self.handler.__module__.split(".")
        return parts[1] if len(parts) > 1 and parts[0] == "modules" else None

    def job_id(self, event_id: str) -> str:
        """Deterministic per (event, subscriber), 32 characters.

        Publishing the same event twice then queues each subscriber's job once:
        the second insert conflicts on the id instead of running it again.
        """
        return hashlib.sha256(f"{event_id}/{self.task}".encode()).hexdigest()[:32]


# Declared at import time, keyed by event type. Read by ``outbox.publish`` in
# the API process and bound to the worker's task registry by the queue
# plugin: both import the same modules, so both see the same map.
_SUBSCRIBERS: dict[str, list[Subscriber]] = {}


def _default_name(handler: SubscriberHandler) -> str:
    """``<module>.<function>`` for a handler in ``modules/<module>/``, else the
    dotted path. Short, because it is what an operator reads in a dead letter."""
    parts = handler.__module__.split(".")
    if len(parts) > 1 and parts[0] == "modules":
        return f"{parts[1]}.{handler.__name__}"
    return f"{handler.__module__}.{handler.__qualname__}"


def subscribe(
    event_type: str, *, name: str | None = None, max_attempts: int = 5
) -> Callable[[SubscriberHandler], SubscriberHandler]:
    """React to every ``event_type`` published in this service::

        @subscribe("comprobante.registrado")
        async def revisar(event: Event) -> None: ...

        @subscribe("comprobante.registrado")
        async def revisar(event: Event, session: TaskSession) -> None: ...

    Declare it in the reacting module's ``tasks.py``: that file is imported by
    the API and by ``jfast worker`` alike (see :mod:`jfastframework.tasks`).
    ``name`` defaults to ``<module>.<function>``; it is part of the queue task
    name, so renaming the function strands jobs already queued under the old
    one -- pass ``name=`` to keep it across a rename.
    """
    if not event_type or not isinstance(event_type, str):
        raise ValueError("subscribe() needs the event type as a non-empty string")

    def decorator(handler: SubscriberHandler) -> SubscriberHandler:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(
                f"subscriber {handler.__qualname__} must be `async def`: it runs on the "
                f"worker's event loop"
            )
        from jfastframework.tasks import task_session_param

        subscriber = Subscriber(
            event_type=event_type,
            name=name or _default_name(handler),
            handler=handler,
            session_param=task_session_param(handler),
            max_attempts=max_attempts,
        )
        registered = _SUBSCRIBERS.setdefault(event_type, [])
        for index, existing in enumerate(registered):
            if existing.name != subscriber.name:
                continue
            if _same_function(existing.handler, handler):
                # The module was imported again (a reload, a test importing it
                # under a second name): replace, never duplicate.
                registered[index] = subscriber
                return handler
            raise ValueError(
                f"two subscribers to {event_type!r} are both named {subscriber.name!r}; "
                f"pass name= to one of them"
            )
        registered.append(subscriber)
        return handler

    return decorator


def _same_function(a: Callable[..., Any], b: Callable[..., Any]) -> bool:
    return (a.__module__, a.__qualname__) == (b.__module__, b.__qualname__)


def subscribers_for(event_type: str) -> list[Subscriber]:
    return list(_SUBSCRIBERS.get(event_type, ()))


def subscribers() -> list[Subscriber]:
    return [s for event_type in sorted(_SUBSCRIBERS) for s in _SUBSCRIBERS[event_type]]


def clear_subscribers() -> None:
    """For tests. Declarations accumulate across a session otherwise."""
    _SUBSCRIBERS.clear()
