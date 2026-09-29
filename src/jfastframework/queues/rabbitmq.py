"""Job queue on RabbitMQ.

Pick this when the queue has to outlive and out-scale the services around it:
real routing, per-queue policies, priorities, and an operator UI. RabbitMQ owns
acknowledgement, redelivery and dead-lettering, so this backend is mostly
translation rather than reimplementation.

**Delays** -- ``Job(available_at=...)`` and every retry's backoff -- are held
by the broker, with no plugin. The obvious design, one delay queue and a
per-message TTL, is wrong: RabbitMQ only expires the message at the *head* of a
queue, so a job delayed five minutes blocks a job delayed two seconds that was
published after it, and a retry backoff of 300 s holds up every shorter one.

What this does instead is a binary cascade. Level ``n`` is a queue whose
*queue-wide* TTL is ``2**n`` delay units, so every message in it expires in
the order it arrived and nothing waits behind a longer one. A delay is
written in binary into the routing key -- one word per level, most significant
first -- and published to the top level's topic exchange. Each level's
exchange sends the message into its queue when that level's bit is 1, and on
to the next level's exchange when it is 0; a queue dead-letters expired
messages to the next level down, and level 0 hands them to the work queue. The
delay a message spends in the cascade is the sum of the levels whose bit is
set, which is the delay it asked for, rounded up to a unit. All of it happens
inside the broker: no consumer, no process that has to stay up.

The unit is 100 ms and there are 25 levels, so the cascade holds up to about
38 days. A longer delay goes through the cascade at its maximum and carries its
due time in a header; the worker that receives it early sends it round again.
The unit and the level count are part of the broker topology -- queues are
declared with their TTL, and redeclaring one with a different TTL is refused
-- so changing either needs a new queue name.

Requires: ``pip install jfastframework[rabbitmq]``

Verified against RabbitMQ 3.13 in ``tests/test_rabbitmq_queue.py``: delays,
their ordering, the head-of-line case, retry backoff and dead-lettering.
Dead-lettering between the levels is not covered by publisher confirms, so a
broker that crashes while a delayed message moves from one level to the next
can lose it; the queues are classic, durable, and the messages persistent.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import Any

from jfastframework.queues.base import Job

#: The smallest delay the cascade can express, in milliseconds. A delay is
#: rounded *up* to it: `available_at` means "not before", so a job may start
#: up to one unit late and never early.
DELAY_UNIT_MS = 100

#: Levels in the cascade. Level ``n`` holds a message ``2**n`` units, so the
#: top one is ``2**24 * 100 ms``, about 19 days, and the cascade as a whole
#: about 38. Kept under 2**31 ms because that is the TTL every RabbitMQ
#: release accepts.
DELAY_LEVELS = 25

#: Carries a due time the cascade could not hold in one pass, as epoch
#: milliseconds. Only set on delays longer than the cascade: comparing a
#: producer's clock with a consumer's is what the broker-side delay avoids, so
#: it is only done when there is no alternative.
NOT_BEFORE_HEADER = "jfast-not-before"

#: How many early messages one ``dequeue`` sends round again before it gives
#: the worker an answer. Bounds the time a single call can take.
_REROUTE_LIMIT = 100

# No visibility timeout here, and none is needed: RabbitMQ redelivers
# unacknowledged messages when the channel closes, which is what a dead
# worker does. So the class takes no such parameter: one that does nothing is
# a promise the caller believes.


def delay_units(delay_ms: float) -> int:
    """A delay in cascade units, rounded up so a job never starts early."""
    if delay_ms <= 0:
        return 0
    return math.ceil(delay_ms / DELAY_UNIT_MS)


def delay_routing_key(units: int, levels: int = DELAY_LEVELS) -> str:
    """The delay as one ``0``/``1`` word per level, most significant first."""
    if not 0 <= units < 2**levels:
        raise ValueError(f"{units} units does not fit in {levels} levels")
    return ".".join("1" if units >> level & 1 else "0" for level in reversed(range(levels)))


def level_pattern(level: int, bit: str, levels: int = DELAY_LEVELS) -> str:
    """The topic binding that matches one level's bit and ignores the rest."""
    position = levels - 1 - level
    return ".".join(bit if index == position else "*" for index in range(levels))


def _epoch_ms(moment: datetime) -> float:
    if moment.tzinfo is None:
        # A naive datetime has no zone to read; the rest of the queue package
        # works in UTC, so that is the only reading that agrees with it.
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() * 1000


class RabbitMQQueue:
    #: Class attributes rather than constructor arguments: they are the broker
    #: topology, and two processes sharing a queue name must agree on them.
    delay_levels = DELAY_LEVELS

    def __init__(
        self,
        connection: Any,
        *,
        name: str = "jfast.jobs",
        prefetch: int = 10,
    ) -> None:
        self._connection = connection
        self._name = name
        self._dead_queue = f"{name}.dead"
        self._exchange_name = f"{name}.retry"
        self._prefetch = prefetch
        self._channel: Any = None
        self._queue: Any = None
        self._exchange: Any = None
        self._delay_entry: Any = None

    def _level_name(self, level: int) -> str:
        return f"{self._name}.delay.{level}"

    @property
    def delay_queues(self) -> tuple[str, ...]:
        return tuple(self._level_name(level) for level in range(self.delay_levels))

    @property
    def max_delay_ms(self) -> int:
        return ((1 << self.delay_levels) - 1) * DELAY_UNIT_MS

    async def setup(self) -> None:
        import aio_pika

        # on_return_raises: a delayed message that no binding routes -- a level
        # deleted by hand, a topology half-declared -- would otherwise be
        # dropped with a warning in the client's log and a publish that
        # reported success.
        self._channel = await self._connection.channel(on_return_raises=True)
        # Prefetch bounds how many unacked messages one worker holds. Without
        # it RabbitMQ hands a single worker the whole queue and the others idle.
        await self._channel.set_qos(prefetch_count=self._prefetch)

        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )

        self._queue = await self._channel.declare_queue(self._name, durable=True)
        await self._queue.bind(self._exchange, routing_key=self._name)
        await self._channel.declare_queue(self._dead_queue, durable=True)
        await self._declare_cascade()

    async def _declare_cascade(self) -> None:
        import aio_pika

        levels = self.delay_levels
        exchanges = [
            await self._channel.declare_exchange(
                self._level_name(level), aio_pika.ExchangeType.TOPIC, durable=True
            )
            for level in range(levels)
        ]
        for level, exchange in enumerate(exchanges):
            if level == 0:
                expired: dict[str, Any] = {
                    "x-dead-letter-exchange": self._exchange_name,
                    "x-dead-letter-routing-key": self._name,
                }
            else:
                # No routing key override: the message keeps its delay key,
                # which is what the level below reads.
                expired = {"x-dead-letter-exchange": self._level_name(level - 1)}
            queue = await self._channel.declare_queue(
                self._level_name(level),
                durable=True,
                arguments={"x-message-ttl": DELAY_UNIT_MS * 2**level, **expired},
            )
            await queue.bind(exchange, routing_key=level_pattern(level, "1", levels))
            if level == 0:
                # A message whose last bit is 0 has served its delay.
                await self._queue.bind(exchange, routing_key=level_pattern(0, "0", levels))
            else:
                await exchanges[level - 1].bind(
                    exchange, routing_key=level_pattern(level, "0", levels)
                )
        self._delay_entry = exchanges[-1]

    def _message(self, body: bytes, job_id: str, *, headers: dict[str, Any] | None = None) -> Any:
        import aio_pika

        return aio_pika.Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=job_id,
            headers=headers or None,
        )

    async def _publish(
        self, body: bytes, job_id: str, *, not_before_ms: float | None, correlation: str | None
    ) -> None:
        """Send a message to the work queue now, or through the cascade."""
        delay_ms = 0.0 if not_before_ms is None else not_before_ms - time.time() * 1000
        units = delay_units(delay_ms)
        if units == 0:
            message = self._message(body, job_id)
            message.correlation_id = correlation
            await self._exchange.publish(message, routing_key=self._name)
            return

        headers: dict[str, Any] = {}
        ceiling = (1 << self.delay_levels) - 1
        if units > ceiling and not_before_ms is not None:
            headers[NOT_BEFORE_HEADER] = math.ceil(not_before_ms)
            units = ceiling
        message = self._message(body, job_id, headers=headers)
        message.correlation_id = correlation
        await self._delay_entry.publish(
            message, routing_key=delay_routing_key(units, self.delay_levels)
        )

    async def enqueue(self, job: Job) -> str:
        not_before = None if job.available_at is None else _epoch_ms(job.available_at)
        await self._publish(
            job.to_json().encode(), job.id, not_before_ms=not_before, correlation=job.request_id
        )
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        # basic.get answers at once, empty or not; `timeout` bounds the round
        # trip, and the worker sleeps out the rest of its poll interval itself.
        for _ in range(_REROUTE_LIMIT):
            message = await self._queue.get(fail=False, timeout=timeout)
            if message is None:
                return None

            not_before = (message.headers or {}).get(NOT_BEFORE_HEADER)
            if not_before is not None and float(not_before) > time.time() * 1000:
                # A delay longer than the cascade, back after one pass. Publish
                # before acking: a crash in between delivers it twice, which
                # at-least-once allows, rather than never.
                await self._publish(
                    message.body,
                    message.message_id or "",
                    not_before_ms=float(not_before),
                    correlation=message.correlation_id,
                )
                await message.ack()
                continue

            job = Job.from_json(message.body, receipt=message)
            job.attempts += 1
            return job
        # Every message looked at was early. Report an empty poll rather than
        # spend the worker's whole turn walking the queue.
        await asyncio.sleep(0)
        return None

    async def ack(self, job: Job) -> None:
        await job.receipt.ack()

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        # The retry is a *new* message, through the same cascade as any delayed
        # job. Nacking with requeue=True would redeliver immediately and spin a
        # poison message at full speed.
        #
        # Published before the original is acked: a crash between the two
        # leaves both, and the job runs again -- at-least-once. The other order
        # leaves neither, and the job is gone. If the publish itself fails the
        # original stays unacked, and the broker returns it when this channel
        # closes.
        body = job.to_json().encode()
        if retry and not job.exhausted:
            due = time.time() * 1000 + job.backoff().total_seconds() * 1000
            await self._publish(body, job.id, not_before_ms=due, correlation=job.request_id)
        else:
            message = self._message(body, job.id)
            message.correlation_id = job.request_id
            await self._channel.default_exchange.publish(message, routing_key=self._dead_queue)
        await job.receipt.ack()

    async def _depth(self, name: str) -> int:
        # The raw channel, not aio-pika's: its robust channel hands back the
        # queue object it declared at setup, whose message count is the one
        # the broker reported then -- zero, forever.
        underlay = await self._channel.get_underlay_channel()
        declared = await underlay.queue_declare(name, passive=True)
        return int(declared.message_count or 0)

    async def stats(self) -> dict[str, int]:
        return {
            "pending": await self._depth(self._name),
            "delayed": sum([await self._depth(name) for name in self.delay_queues]),
            "dead": await self._depth(self._dead_queue),
        }

    async def health(self) -> tuple[bool, str]:
        if self._channel is None or self._channel.is_closed:
            return False, "rabbitmq channel is closed"
        return True, f"rabbitmq queue {self._name} reachable"

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()

    def __repr__(self) -> str:
        return f"<RabbitMQQueue name={self._name!r}>"
