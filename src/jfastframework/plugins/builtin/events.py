"""Event streaming over Kafka.

A queue and an event stream are not the same thing, which is why this is a
separate plugin from ``queue``:

| | Queue (`queue`) | Stream (`events`) |
| --- | --- | --- |
| Message means | "do this" | "this happened" |
| Consumers | exactly one wins | every group gets its own copy |
| After consuming | gone | still there, replayable |
| Use it for | send the email, resize the image | tell the other services an order was paid |

Using a queue for events means adding a second queue every time a new service
cares. Using a stream for jobs means reimplementing retries and dead-lettering
on top of offsets. Pick by which of the two rows above you are in.

    [plugins]
    enabled = ["observability", "events"]

    [plugin.events]
    bootstrap_servers = "localhost:9092"
    consumer_group = "billing"

Handlers are declared at import time with the module-level ``on``; the plugin
binds them when it registers, before the consumer joins its group::

    @on("orders")
    async def handle(event: Event) -> None: ...

``@on`` listens to a *topic* and is how another service hears this one. Two
modules of the same service do not need a broker at all: they use
``jfastframework.events.subscribe``, delivered through the queue, and keep
working unchanged when this plugin is turned on.

Requires: ``pip install jfastframework[kafka]``

Verified: written against aiokafka's documented API, **not** run against a
real broker in CI. Treat the first deployment as the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework import tracing

# Re-exported: this is where `Event` lived before local events existed, and
# every consumer written against Kafka imports it from here.
from jfastframework.events import Event
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.events")

__all__ = ["Event", "EventBus", "EventsPlugin", "clear_pending", "on", "pending_handlers"]

EventHandler = Callable[[Event], Awaitable[None]]

# Mirrors ``JFastSettings.port``. Only used to derive the advertised external
# address when ``infra()`` is called without a context, which is what
# ``deploy.compose.collect_infra`` does.
_DEFAULT_BASE_PORT = 8000


class EventBus:
    """Publish events and register handlers for topics."""

    def __init__(self, producer: Any, *, source: str, topic_prefix: str) -> None:
        self._producer = producer
        self._source = source
        self._prefix = topic_prefix
        self._handlers: dict[str, list[EventHandler]] = {}

    def topic(self, name: str) -> str:
        return f"{self._prefix}{name}" if self._prefix else name

    async def publish(self, topic: str, event: Event) -> None:
        if not event.source:
            event.source = self._source
        await self._producer.send_and_wait(
            self.topic(topic),
            value=event.to_json().encode(),
            key=event.key.encode() if event.key else None,
        )

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Bind a handler to a topic.

        Idempotent by handler identity: the plugin drains the pending list in
        both ``register`` and ``startup``, and a handler bound twice would be
        dispatched twice for one event.
        """
        handlers = self._handlers.setdefault(self.topic(topic), [])
        if handler not in handlers:
            handlers.append(handler)

    def on(self, topic: str) -> Callable[[EventHandler], EventHandler]:
        """Register a handler on this bus::

        @bus.on("orders")
        async def handle(event: Event) -> None: ...

        Only usable once the bus exists. Prefer the module-level ``on`` for
        handlers declared at import time.
        """

        def decorator(handler: EventHandler) -> EventHandler:
            self.subscribe(topic, handler)
            return handler

        return decorator

    @property
    def topics(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    async def dispatch(self, topic: str, event: Event) -> None:
        # The handlers run as the request that published the event: its
        # tenant, its request id, its trace. A consumer in another service
        # otherwise reads every tenant's rows and logs lines nobody can trace.
        from jfastframework.plugins.builtin.observability import request_id_var, tenant_id_var

        request_token = request_id_var.set(event.request_id)
        tenant_token = tenant_id_var.set(event.tenant_id)
        try:
            with (
                tracing.attach(event.trace),
                tracing.span(f"event {event.type}", topic=topic, event_id=event.id),
            ):
                for handler in self._handlers.get(topic, []):
                    await handler(event)
        finally:
            tenant_id_var.reset(tenant_token)
            request_id_var.reset(request_token)


# Handlers declared by decorator before the bus exists. The plugin drains this
# at register and again at startup; importing a module must not require a
# running app, and the consumer must know its topics before it joins the group.
_PENDING: list[tuple[str, EventHandler]] = []


def on(topic: str) -> Callable[[EventHandler], EventHandler]:
    """Declare a handler at import time::

        from jfastframework.plugins.builtin.events import Event, on

        @on("orders")
        async def handle(event: Event) -> None: ...

    The topic is the unprefixed name; ``topic_prefix`` is applied when the
    handler is bound to a bus.
    """

    def decorator(handler: EventHandler) -> EventHandler:
        _PENDING.append((topic, handler))
        return handler

    return decorator


def pending_handlers() -> list[tuple[str, EventHandler]]:
    return list(_PENDING)


def clear_pending() -> None:
    """For tests. Decorators accumulate across a session otherwise."""
    _PENDING.clear()


class EventsSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_EVENTS_", env_file=".env", extra="ignore")

    bootstrap_servers: str = "localhost:9092"
    consumer_group: str = ""
    topic_prefix: str = ""
    # Consume from the start on a brand-new group. "latest" silently skips
    # everything that happened before the service first deployed.
    auto_offset_reset: str = "earliest"
    # Commit after handling, not on a timer: at-least-once rather than
    # at-most-once. A handler that runs twice is a bug you can fix; an event
    # that never ran is one you never see.
    enable_auto_commit: bool = False
    consume: bool = True
    include_infra: bool = True
    port_offset: int = 2
    # Overridable because a broker image is a moving target: Bitnami moved its
    # catalogue to `bitnamilegacy/` in 2025 and the old tags stopped resolving.
    image: str = "bitnamilegacy/kafka:3.9"
    # What the broker advertises to clients outside the compose network.
    # `host_port` is the port compose publishes *and* the port advertised --
    # one setting, so the two cannot be moved apart. None derives it from the
    # base port; see `EventsPlugin._published_port`.
    advertised_host: str = "localhost"
    host_port: int | None = None


class EventsPlugin(Plugin):
    meta = PluginMeta(
        name="events",
        version="0.1.0",
        description="Kafka event streaming: publish, subscribe, replay.",
        after=("observability",),
        provides=("events",),
        default_enabled=False,
        extra="jfastframework[kafka]",
    )
    Settings = EventsSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._producer: Any = None
        self._consumer: Any = None
        self._bus: EventBus | None = None
        self._task: asyncio.Task[None] | None = None

    def register(self, ctx: AppContext) -> None:
        settings: EventsSettings = self.settings
        # The producer is constructed here but connected in startup(), because
        # register() must not do I/O.
        self._bus = EventBus(
            _LazyProducer(self),
            source=ctx.settings.app_name,
            topic_prefix=settings.topic_prefix,
        )
        ctx.provide("events", self._bus)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Bind handlers declared with the module-level ``on``.

        Called from both hooks because a module can be imported either side of
        ``register``: the app's own modules before it, a lazily imported router
        after it. ``EventBus.subscribe`` makes the second pass a no-op.
        """
        assert self._bus is not None
        for topic, handler in pending_handlers():
            self._bus.subscribe(topic, handler)

    async def startup(self, ctx: AppContext) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        settings: EventsSettings = self.settings
        self._producer = AIOKafkaProducer(bootstrap_servers=settings.bootstrap_servers)
        await self._producer.start()

        assert self._bus is not None
        self._drain_pending()
        topics = self._bus.topics
        if not settings.consume:
            ctx.logger.info("events: consume is disabled; publishing only")
            return
        if not topics:
            # Silence here is the failure mode this warning exists for: the
            # consumer would join the group and receive nothing, forever.
            ctx.logger.warning(
                "events: consume is enabled but no handlers are registered; "
                "nothing will be received. Declare handlers with "
                "`@jfastframework.plugins.builtin.events.on(topic)`."
            )
            return

        group = settings.consumer_group or ctx.settings.app_name
        self._consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=settings.bootstrap_servers,
            group_id=group,
            auto_offset_reset=settings.auto_offset_reset,
            enable_auto_commit=settings.enable_auto_commit,
        )
        await self._consumer.start()
        self._task = asyncio.create_task(self._consume(ctx))
        ctx.logger.info("events: consuming %s as group %r", ", ".join(topics), group)

    async def _consume(self, ctx: AppContext) -> None:
        assert self._consumer is not None and self._bus is not None
        try:
            async for message in self._consumer:
                key = message.key.decode() if message.key else None
                try:
                    event = Event.from_json(message.value, key=key)
                    await self._bus.dispatch(message.topic, event)
                except Exception:
                    # Do not commit: the event is redelivered rather than
                    # silently skipped. A poison message will block the
                    # partition, which is visible -- unlike losing it.
                    logger.exception(
                        "event handler failed",
                        extra={"topic": message.topic, "offset": message.offset},
                    )
                    continue
                if not self.settings.enable_auto_commit:
                    await self._consumer.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event consumer stopped unexpectedly")

    async def shutdown(self, ctx: AppContext) -> None:
        if self._task is not None:
            self._task.cancel()
            # The consume loop is being torn down: whatever it raises on the
            # way out must not stop the producer and consumer from closing.
            with contextlib.suppress(BaseException):
                await self._task
        if self._consumer is not None:
            await self._consumer.stop()
        if self._producer is not None:
            await self._producer.stop()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._producer is None:
            return HealthReport.fail("kafka producer not started")
        topics = self._bus.topics if self._bus else ()
        return HealthReport.ok(
            "kafka connected",
            servers=self.settings.bootstrap_servers,
            topics=list(topics),
        )

    def _published_port(self, ctx: AppContext | None = None) -> int:
        """The host port compose publishes the broker on.

        Handed to ``InfraService.host_port`` as well as advertised, so the
        compose mapping and the advertised address are the same number by
        construction rather than by two settings agreeing. ``collect_infra``
        calls ``infra()`` without a context, so a service on a base port other
        than the default has to set ``host_port`` explicitly.
        """
        settings: EventsSettings = self.settings
        if settings.host_port is not None:
            return settings.host_port
        base = ctx.settings.port if ctx is not None else _DEFAULT_BASE_PORT
        return base + settings.port_offset

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: EventsSettings = self.settings
        if not settings.include_infra:
            return []
        external = f"{settings.advertised_host}:{self._published_port(ctx)}"
        return [
            InfraService(
                name="kafka",
                # KRaft mode: no ZooKeeper. One container instead of two, and
                # one fewer thing to operate.
                image=settings.image,
                # The published port is the EXTERNAL listener's, not the
                # internal one: a host client has no route to `kafka:9092`.
                port_offset=settings.port_offset,
                internal_port=9094,
                # Same field the advertised address above was derived from.
                # Advertising a port compose does not publish is the failure
                # this setting exists to prevent, not one it may cause.
                host_port=settings.host_port,
                environment={
                    "KAFKA_CFG_NODE_ID": "0",
                    "KAFKA_CFG_PROCESS_ROLES": "controller,broker",
                    "KAFKA_CFG_CONTROLLER_QUORUM_VOTERS": "0@kafka:9093",
                    "KAFKA_CFG_LISTENERS": "INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:9094",
                    # Two listeners because the broker is reached from two
                    # networks. A client bootstraps once and then reconnects to
                    # whatever is advertised, so advertising only `kafka:9092`
                    # makes a client on the host connect and then hang.
                    "KAFKA_CFG_ADVERTISED_LISTENERS": (
                        f"INTERNAL://kafka:9092,EXTERNAL://{external}"
                    ),
                    "KAFKA_CFG_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                    "KAFKA_CFG_INTER_BROKER_LISTENER_NAME": "INTERNAL",
                    "KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP": (
                        "CONTROLLER:PLAINTEXT,INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT"
                    ),
                },
                volumes=["kafka_data:/bitnami/kafka"],
                # The INTERNAL listener, which is the one a container on this
                # network can route to. `external` above is for a host client.
                client_env={"JFAST_EVENTS_BOOTSTRAP_SERVERS": "kafka:9092"},
            )
        ]


class _LazyProducer:
    """Defers the Kafka connection until ``startup``."""

    def __init__(self, plugin: EventsPlugin) -> None:
        self._plugin = plugin

    async def send_and_wait(self, topic: str, **kwargs: Any) -> Any:
        if self._plugin._producer is None:
            raise RuntimeError("Kafka producer is not started yet")
        return await self._plugin._producer.send_and_wait(topic, **kwargs)
