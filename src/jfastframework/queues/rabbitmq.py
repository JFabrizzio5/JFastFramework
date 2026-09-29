"""Job queue on RabbitMQ.

Pick this when the queue has to outlive and out-scale the services around it:
real routing, per-queue policies, priorities, and an operator UI. RabbitMQ owns
acknowledgement, redelivery and dead-lettering, so this backend is mostly
translation rather than reimplementation.

Retries use a **dead-letter exchange with a TTL**, not an in-process sleep:
a rejected job goes to a delay queue whose messages expire back onto the main
queue. Sleeping in the worker would hold a connection and lose the delay on a
restart.

Requires: ``pip install jfastframework[rabbitmq]``

Verified: the code is written against aio-pika's documented API but has **not**
been run against a real broker in CI. Treat the first deployment as the test.
"""

from __future__ import annotations

from typing import Any

from jfastframework.queues.base import Job

# No visibility timeout here, and none is needed: RabbitMQ redelivers
# unacknowledged messages when the channel closes, which is what a dead
# worker does. So the class takes no such parameter: one that does nothing is
# a promise the caller believes.


class RabbitMQQueue:
    def __init__(
        self,
        connection: Any,
        *,
        name: str = "jfast.jobs",
        prefetch: int = 10,
    ) -> None:
        self._connection = connection
        self._name = name
        self._delay_queue = f"{name}.delay"
        self._dead_queue = f"{name}.dead"
        self._exchange_name = f"{name}.retry"
        self._prefetch = prefetch
        self._channel: Any = None
        self._queue: Any = None
        self._exchange: Any = None

    async def setup(self) -> None:
        import aio_pika

        self._channel = await self._connection.channel()
        # Prefetch bounds how many unacked messages one worker holds. Without
        # it RabbitMQ hands a single worker the whole queue and the others idle.
        await self._channel.set_qos(prefetch_count=self._prefetch)

        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )

        self._queue = await self._channel.declare_queue(self._name, durable=True)
        await self._queue.bind(self._exchange, routing_key=self._name)

        # Messages that expire in the delay queue are dead-lettered straight
        # back onto the main queue. That is the retry delay, implemented by
        # the broker instead of by a sleeping worker.
        await self._channel.declare_queue(
            self._delay_queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self._exchange_name,
                "x-dead-letter-routing-key": self._name,
            },
        )
        await self._channel.declare_queue(self._dead_queue, durable=True)

    async def enqueue(self, job: Job) -> str:
        import aio_pika

        message = aio_pika.Message(
            body=job.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=job.id,
            correlation_id=job.request_id,
        )
        await self._exchange.publish(message, routing_key=self._name)
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        message = await self._queue.get(fail=False, timeout=timeout)
        if message is None:
            return None

        job = Job.from_json(message.body, receipt=message)
        job.attempts += 1
        return job

    async def ack(self, job: Job) -> None:
        await job.receipt.ack()

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        import aio_pika

        # Ack the original either way: the retry is a *new* message on the
        # delay queue. Nacking with requeue=True would redeliver immediately
        # and spin a poison message at full speed.
        await job.receipt.ack()

        target = self._delay_queue if (retry and not job.exhausted) else self._dead_queue
        message = aio_pika.Message(
            body=job.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=job.id,
            expiration=job.backoff() if target == self._delay_queue else None,
        )
        await self._channel.default_exchange.publish(message, routing_key=target)

    async def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label, name in (
            ("pending", self._name),
            ("delayed", self._delay_queue),
            ("dead", self._dead_queue),
        ):
            queue = await self._channel.declare_queue(name, durable=True, passive=True)
            counts[label] = int(queue.declaration_result.message_count or 0)
        return counts

    async def health(self) -> tuple[bool, str]:
        if self._channel is None or self._channel.is_closed:
            return False, "rabbitmq channel is closed"
        return True, f"rabbitmq queue {self._name} reachable"

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            await self._channel.close()

    def __repr__(self) -> str:
        return f"<RabbitMQQueue name={self._name!r}>"
