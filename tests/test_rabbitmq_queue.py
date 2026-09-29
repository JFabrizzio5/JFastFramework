"""The RabbitMQ queue: delays held by the broker, in due order, retries included.

The arithmetic runs everywhere. Everything else needs a broker and skips
without ``JFAST_TEST_RABBITMQ_URL`` -- a delay built from queue TTLs and
dead-letter exchanges exists only inside RabbitMQ, and a double of it would
only show that the double does what this module expects.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest

from jfastframework.queues.base import Job, utcnow
from jfastframework.queues.rabbitmq import (
    DELAY_LEVELS,
    DELAY_UNIT_MS,
    RabbitMQQueue,
    delay_routing_key,
    delay_units,
    level_pattern,
)
from jfastframework.queues.worker import TaskRegistry, Worker

RABBITMQ_URL = os.environ.get("JFAST_TEST_RABBITMQ_URL", "")

needs_broker = pytest.mark.skipif(
    not RABBITMQ_URL,
    reason="set JFAST_TEST_RABBITMQ_URL: the delay cascade exists only inside RabbitMQ",
)


# -- the arithmetic ----------------------------------------------------------


def test_a_delay_is_rounded_up_so_a_job_never_starts_early() -> None:
    assert delay_units(0) == 0
    assert delay_units(-5_000) == 0
    assert delay_units(1) == 1
    assert delay_units(DELAY_UNIT_MS) == 1
    assert delay_units(DELAY_UNIT_MS + 1) == 2


def test_the_routing_key_is_the_delay_in_binary_most_significant_first() -> None:
    assert delay_routing_key(5, levels=4) == "0.1.0.1"
    assert delay_routing_key(0, levels=3) == "0.0.0"
    assert len(delay_routing_key(1).split(".")) == DELAY_LEVELS


@pytest.mark.parametrize("units", [1, 2, 3, 37, 1000, 2**DELAY_LEVELS - 1])
def test_the_levels_a_key_passes_through_add_up_to_the_delay(units: int) -> None:
    words = delay_routing_key(units).split(".")
    held = sum(2**level for level in range(DELAY_LEVELS) if words[DELAY_LEVELS - 1 - level] == "1")
    assert held == units


def test_a_delay_the_cascade_cannot_hold_is_refused_by_the_key() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        delay_routing_key(2**4, levels=4)


def test_each_level_matches_its_own_bit_and_nothing_else() -> None:
    assert level_pattern(0, "1", levels=3) == "*.*.1"
    assert level_pattern(2, "0", levels=3) == "0.*.*"


def test_the_longest_level_stays_inside_the_ttl_every_release_accepts() -> None:
    assert DELAY_UNIT_MS * 2 ** (DELAY_LEVELS - 1) < 2**31


# -- against a real broker ---------------------------------------------------


class _ShortCascade(RabbitMQQueue):
    """Three levels: 700 ms at most, so a longer delay needs the header path."""

    delay_levels = 3


@pytest.fixture
async def connection():  # type: ignore[no-untyped-def]
    import aio_pika

    conn = await aio_pika.connect_robust(RABBITMQ_URL)
    yield conn
    await conn.close()


async def _drop(conn: Any, queue: RabbitMQQueue, name: str) -> None:
    channel = await conn.channel()
    for queue_name in (name, f"{name}.dead", *queue.delay_queues):
        await channel.queue_delete(queue_name)
    for exchange_name in (f"{name}.retry", *queue.delay_queues):
        await channel.exchange_delete(exchange_name)
    await channel.close()


@pytest.fixture
async def make_queue(connection):  # type: ignore[no-untyped-def]
    made: list[tuple[RabbitMQQueue, str]] = []

    async def make(cls: type[RabbitMQQueue] = RabbitMQQueue) -> RabbitMQQueue:
        name = f"jfast.test.{uuid.uuid4().hex[:8]}"
        queue = cls(connection, name=name)
        await queue.setup()
        made.append((queue, name))
        return queue

    yield make
    for queue, name in made:
        await queue.close()
        await _drop(connection, queue, name)


async def _next(queue: RabbitMQQueue, *, within: float) -> tuple[Job | None, float]:
    """Poll until a job arrives or ``within`` seconds pass; report when."""
    started = time.monotonic()
    while time.monotonic() - started < within:
        job = await queue.dequeue(timeout=1.0)
        if job is not None:
            return job, time.monotonic() - started
        await asyncio.sleep(0.02)
    return None, time.monotonic() - started


@needs_broker
async def test_a_job_with_no_delay_is_delivered_at_once(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue()
    await queue.enqueue(Job(task="now", payload={"n": 1}))
    job, elapsed = await _next(queue, within=2)
    assert job is not None and job.payload == {"n": 1}
    assert elapsed < 0.5
    await queue.ack(job)


@needs_broker
async def test_a_delayed_job_waits_for_its_time(make_queue) -> None:  # type: ignore[no-untyped-def]
    """The bug this closes: `available_at` was ignored and the job ran at once."""
    queue = await make_queue()
    enqueued = time.monotonic()
    await queue.enqueue(Job(task="later", available_at=utcnow() + timedelta(seconds=1.5)))

    assert await queue.dequeue(timeout=1.0) is None
    assert (await queue.stats())["delayed"] == 1

    job, _ = await _next(queue, within=5)
    assert job is not None and job.task == "later"
    assert time.monotonic() - enqueued >= 1.5
    assert time.monotonic() - enqueued < 3.0
    await queue.ack(job)


@needs_broker
async def test_a_long_delay_does_not_hold_up_a_short_one_behind_it(make_queue) -> None:  # type: ignore[no-untyped-def]
    """The head-of-line case: one queue with per-message TTLs fails exactly this."""
    queue = await make_queue()
    await queue.enqueue(Job(task="slow", available_at=utcnow() + timedelta(minutes=10)))
    await queue.enqueue(Job(task="fast", available_at=utcnow() + timedelta(seconds=1)))

    job, elapsed = await _next(queue, within=5)
    assert job is not None and job.task == "fast"
    assert elapsed < 3.0
    await queue.ack(job)
    assert (await queue.stats())["delayed"] == 1


@needs_broker
async def test_delayed_jobs_arrive_in_the_order_they_are_due(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue()
    now = utcnow()
    for seconds in (2.0, 0.4, 1.2):
        await queue.enqueue(
            Job(task=f"after-{seconds}", available_at=now + timedelta(seconds=seconds))
        )

    arrived: list[str] = []
    while len(arrived) < 3:
        job, _ = await _next(queue, within=5)
        assert job is not None, f"only {arrived} arrived"
        arrived.append(job.task)
        await queue.ack(job)
    assert arrived == ["after-0.4", "after-1.2", "after-2.0"]


@needs_broker
async def test_a_retry_waits_out_its_backoff_in_the_broker(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue()
    await queue.enqueue(Job(task="flaky", max_attempts=3))
    first, _ = await _next(queue, within=2)
    assert first is not None and first.attempts == 1

    nacked = time.monotonic()
    await queue.nack(first, retry=True)
    assert await queue.dequeue(timeout=1.0) is None

    second, _ = await _next(queue, within=6)
    assert second is not None and second.id == first.id
    assert second.attempts == 2
    # The first retry's backoff is two seconds.
    assert time.monotonic() - nacked >= first.backoff().total_seconds()
    await queue.ack(second)


@needs_broker
async def test_an_exhausted_job_is_dead_lettered_not_delayed(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue()
    await queue.enqueue(Job(task="poison", max_attempts=1))
    job, _ = await _next(queue, within=2)
    assert job is not None and job.exhausted
    await queue.nack(job, retry=True)

    stats = await queue.stats()
    assert stats == {"pending": 0, "delayed": 0, "dead": 1}


@needs_broker
async def test_a_delay_longer_than_the_cascade_goes_round_again(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue(_ShortCascade)
    assert queue.max_delay_ms == 700
    enqueued = time.monotonic()
    await queue.enqueue(Job(task="beyond", available_at=utcnow() + timedelta(seconds=1.6)))

    job, _ = await _next(queue, within=5)
    assert job is not None and job.task == "beyond"
    assert job.attempts == 1, "sending an early job round again is not an attempt"
    assert time.monotonic() - enqueued >= 1.6
    await queue.ack(job)


@needs_broker
async def test_a_worker_retries_a_failed_handler_through_the_cascade(make_queue) -> None:  # type: ignore[no-untyped-def]
    queue = await make_queue()
    registry = TaskRegistry()
    calls: list[int] = []
    done = asyncio.Event()

    @registry.task("sometimes")
    async def sometimes(payload: dict[str, Any]) -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        done.set()

    worker = Worker(queue, registry, poll_timeout=0.2)
    # setup() again from the worker is idempotent: every declaration matches.
    running = asyncio.create_task(worker.run())
    try:
        await queue.enqueue(Job(task="sometimes"))
        await asyncio.wait_for(done.wait(), timeout=8)
    finally:
        worker.stop()
        await asyncio.wait_for(running, timeout=5)
    assert calls == [0, 1]
