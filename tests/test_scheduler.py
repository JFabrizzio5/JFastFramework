"""Recurring tasks: each tick fires once across replicas, and downtime costs one run.

Time is passed in rather than read, so every case here is exact. Replicas are
two :class:`Scheduler` instances sharing one tick store -- in memory, in
SQLite, and at the end against a real PostgreSQL, where two engines stand in
for two processes and the claim is decided by the server.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.errors import PluginError
from jfastframework.queues.base import Job
from jfastframework.queues.schedule import Schedule
from jfastframework.queues.scheduler import MemoryTickStore, RedisTickStore, Scheduler
from jfastframework.queues.sql_ticks import TICKS_TABLE, SqlTickStore, ticks
from jfastframework.queues.worker import TaskRegistry
from jfastframework.testing import build_test_app, client_for

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "")


def at(text_: str) -> datetime:
    return datetime.fromisoformat(text_).replace(tzinfo=UTC)


class ListQueue:
    """Records what it is given; can be told to fail the next N enqueues."""

    def __init__(self, *, failures: int = 0) -> None:
        self.jobs: list[Job] = []
        self.failures = failures

    async def enqueue(self, job: Job) -> str:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("broker unreachable")
        self.jobs.append(job)
        return job.id


def hourly_registry(**options: Any) -> TaskRegistry:
    registry = TaskRegistry()

    @registry.task("report", every=timedelta(hours=1), **options)
    async def report(payload: dict[str, Any]) -> None: ...

    return registry


# -- declaring schedules -----------------------------------------------------


def test_a_task_can_declare_its_own_schedule() -> None:
    registry = TaskRegistry()

    @registry.task("nightly", cron="0 3 * * *", timezone="America/Mexico_City")
    async def nightly(payload: dict[str, Any]) -> None: ...

    (schedule,) = registry.schedules
    assert schedule.name == schedule.task == "nightly"
    assert str(schedule.cron) == "0 3 * * *"
    assert registry.names == ("nightly",)


def test_a_malformed_expression_fails_where_it_is_written() -> None:
    registry = TaskRegistry()
    with pytest.raises(ValueError, match="has 4 fields"):

        @registry.task("broken", cron="0 3 * *")
        async def broken(payload: dict[str, Any]) -> None: ...

    assert registry.names == ()


def test_one_task_on_two_schedules_needs_a_second_name() -> None:
    registry = hourly_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.schedule("report", cron="@daily")
    registry.schedule("report", cron="@daily", name="report-daily", payload={"span": "day"})
    assert [s.name for s in registry.schedules] == ["report", "report-daily"]


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({}, "exactly one of"),
        ({"every": timedelta(minutes=1), "cron": "* * * * *"}, "exactly one of"),
        ({"every": timedelta(milliseconds=500)}, "shorter than a second"),
        ({"every": timedelta(hours=1), "timezone": "Europe/Madrid"}, "applies to cron"),
        ({"cron": "0 3 * * *", "timezone": "Mars/Olympus_Mons"}, "unknown time zone"),
    ],
)
def test_a_schedule_that_cannot_mean_anything_is_refused(
    options: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        Schedule.build("s", "t", **options)


def test_interval_ticks_count_from_the_epoch_so_replicas_agree() -> None:
    schedule = Schedule.build("s", "t", every=timedelta(minutes=5))
    assert schedule.latest_at_or_before(at("2026-09-29 10:07:30")) == at("2026-09-29 10:05")
    assert schedule.latest_at_or_before(at("2026-09-29 10:10")) == at("2026-09-29 10:10")
    assert schedule.next_after(at("2026-09-29 10:07:30")) == at("2026-09-29 10:10")
    assert schedule.next_after(at("2026-09-29 10:10")) == at("2026-09-29 10:15")


def test_the_job_id_of_a_tick_is_the_same_everywhere_and_different_per_tick() -> None:
    schedule = Schedule.build("s", "t", every=timedelta(minutes=5))
    first = schedule.job_id(at("2026-09-29 10:05"))
    assert first == Schedule.build("s", "t", cron="*/5 * * * *").job_id(at("2026-09-29 10:05"))
    assert len(first) == 32 and int(first, 16) >= 0
    assert first != schedule.job_id(at("2026-09-29 10:10"))
    assert first != Schedule.build("other", "t", every=timedelta(minutes=5)).job_id(
        at("2026-09-29 10:05")
    )


# -- firing ------------------------------------------------------------------


async def test_a_new_schedule_waits_for_its_next_tick() -> None:
    queue = ListQueue()
    scheduler = Scheduler(queue, hourly_registry(), MemoryTickStore())  # type: ignore[arg-type]

    assert await scheduler.tick(at("2026-09-29 10:20")) == []
    assert await scheduler.tick(at("2026-09-29 10:59:59")) == []
    fired = await scheduler.tick(at("2026-09-29 11:00:00.01"))
    assert [job.payload["scheduled_for"] for job in fired] == ["2026-09-29T11:00:00+00:00"]
    assert await scheduler.tick(at("2026-09-29 11:30")) == []


async def test_the_job_carries_the_payload_and_the_time_it_is_for() -> None:
    registry = TaskRegistry()
    registry.schedule("report", every=timedelta(hours=1), payload={"span": "hour"})
    queue = ListQueue()
    scheduler = Scheduler(queue, registry, MemoryTickStore())  # type: ignore[arg-type]
    await scheduler.tick(at("2026-09-29 10:20"))
    (job,) = await scheduler.tick(at("2026-09-29 11:00"))
    assert job.task == "report"
    assert job.payload == {"span": "hour", "scheduled_for": "2026-09-29T11:00:00+00:00"}
    assert job.id == registry.schedules[0].job_id(at("2026-09-29 11:00"))
    assert job.tenant_id is None


async def test_two_replicas_fire_each_tick_once() -> None:
    store = MemoryTickStore()
    queue = ListQueue()
    replicas = [
        Scheduler(queue, hourly_registry(), store)  # type: ignore[arg-type]
        for _ in range(2)
    ]
    for replica in replicas:
        await replica.tick(at("2026-09-29 10:20"))

    for hour in ("11:00", "12:00", "13:00"):
        now = at(f"2026-09-29 {hour}:00.2")
        results = await asyncio.gather(*(replica.tick(now) for replica in replicas))
        assert sum(len(fired) for fired in results) == 1
    assert [job.payload["scheduled_for"][11:16] for job in queue.jobs] == [
        "11:00",
        "12:00",
        "13:00",
    ]


async def test_downtime_costs_one_catch_up_run_not_a_burst() -> None:
    store = MemoryTickStore()
    first = Scheduler(ListQueue(), hourly_registry(), store)  # type: ignore[arg-type]
    await first.tick(at("2026-09-29 09:30"))
    await first.tick(at("2026-09-29 10:00"))
    assert await store.last("report") == at("2026-09-29 10:00")

    # Down from 10:05 until 15:20: five ticks missed.
    queue = ListQueue()
    restarted = Scheduler(queue, hourly_registry(), store)  # type: ignore[arg-type]
    fired = await restarted.tick(at("2026-09-29 15:20"))
    assert [job.payload["scheduled_for"] for job in fired] == ["2026-09-29T15:00:00+00:00"]
    assert await restarted.tick(at("2026-09-29 15:40")) == []
    assert len(await restarted.tick(at("2026-09-29 16:00"))) == 1


async def test_catch_up_can_be_declined() -> None:
    store = MemoryTickStore()
    first = Scheduler(ListQueue(), hourly_registry(catch_up=False), store)  # type: ignore[arg-type]
    await first.tick(at("2026-09-29 09:30"))
    await first.tick(at("2026-09-29 10:00"))

    restarted = Scheduler(ListQueue(), hourly_registry(catch_up=False), store)  # type: ignore[arg-type]
    assert await restarted.tick(at("2026-09-29 15:20")) == []
    assert len(await restarted.tick(at("2026-09-29 16:00"))) == 1


async def test_a_stalled_process_fires_once_when_it_wakes() -> None:
    """An event loop blocked for hours is downtime too, and is owed one run."""
    queue = ListQueue()
    scheduler = Scheduler(queue, hourly_registry(), MemoryTickStore())  # type: ignore[arg-type]
    await scheduler.tick(at("2026-09-29 09:30"))
    await scheduler.tick(at("2026-09-29 10:00"))
    fired = await scheduler.tick(at("2026-09-29 14:10"))
    assert [job.payload["scheduled_for"][11:16] for job in fired] == ["14:00"]


async def test_a_failed_enqueue_releases_the_tick_for_the_next_pass() -> None:
    store = MemoryTickStore()
    queue = ListQueue(failures=1)
    scheduler = Scheduler(queue, hourly_registry(), store)  # type: ignore[arg-type]
    await scheduler.tick(at("2026-09-29 10:20"))
    with pytest.raises(ConnectionError):
        await scheduler.tick(at("2026-09-29 11:00"))
    assert await store.last("report") is None

    # Another replica, or this one on its next pass, takes the tick.
    other = Scheduler(queue, hourly_registry(), store)  # type: ignore[arg-type]
    await other.tick(at("2026-09-29 10:20"))
    assert len(await other.tick(at("2026-09-29 11:00:01"))) == 1
    assert await scheduler.tick(at("2026-09-29 11:00:02")) == []
    assert len(queue.jobs) == 1


async def test_one_schedule_failing_does_not_hold_up_the_others() -> None:
    class FailsFor(ListQueue):
        async def enqueue(self, job: Job) -> str:
            if job.task == "broken":
                raise ConnectionError("broker unreachable")
            return await super().enqueue(job)

    registry = TaskRegistry()
    registry.schedule("broken", every=timedelta(hours=1))
    registry.schedule("healthy", every=timedelta(hours=1))
    queue = FailsFor()
    scheduler = Scheduler(queue, registry, MemoryTickStore())  # type: ignore[arg-type]
    await scheduler.tick(at("2026-09-29 10:20"))
    with pytest.raises(ConnectionError):
        await scheduler.tick(at("2026-09-29 11:00"))
    assert [job.task for job in queue.jobs] == ["healthy"]


async def test_a_schedule_for_a_task_this_process_lacks_still_fires(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = TaskRegistry()
    registry.schedule("rebuild_index", every=timedelta(minutes=10))
    queue = ListQueue()
    scheduler = Scheduler(queue, registry, MemoryTickStore())  # type: ignore[arg-type]
    await scheduler.tick(at("2026-09-29 10:05"))
    await scheduler.tick(at("2026-09-29 10:10"))
    assert len(queue.jobs) == 1
    assert "does not register" in caplog.text


async def test_the_loop_sleeps_to_the_next_tick_and_stops_at_once() -> None:
    registry = TaskRegistry()
    registry.schedule("every_second", every=timedelta(seconds=1))
    queue = ListQueue()
    scheduler = Scheduler(queue, registry, MemoryTickStore(), max_sleep=5)  # type: ignore[arg-type]
    # To the next tick, and a little past it.
    assert scheduler.seconds_until_next(at("2026-09-29 10:00:00.25")) == pytest.approx(0.8)

    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(2.3)
    assert scheduler.healthy
    stop.set()
    await asyncio.wait_for(loop, timeout=0.5)
    assert not scheduler.healthy
    # Two or three ticks passed in 2.3 s; each fired once.
    assert 2 <= len(queue.jobs) <= 3
    assert len({job.id for job in queue.jobs}) == len(queue.jobs)


async def test_a_failing_pass_is_reported_and_retried(caplog: pytest.LogCaptureFixture) -> None:
    queue = ListQueue(failures=1)
    now = [at("2026-09-29 10:20")]
    scheduler = Scheduler(
        queue,  # type: ignore[arg-type]
        hourly_registry(),
        MemoryTickStore(),
        clock=lambda: now[0],
        retry_delay=0.05,
        max_sleep=0.05,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0.1)
    now[0] = at("2026-09-29 11:00:01")
    await asyncio.sleep(0.3)
    stop.set()
    await loop
    assert "scheduler pass failed" in caplog.text
    assert len(queue.jobs) == 1
    assert scheduler.status()["error"] is None


def test_status_names_each_schedule_and_its_next_tick() -> None:
    registry = hourly_registry()
    scheduler = Scheduler(
        ListQueue(),  # type: ignore[arg-type]
        registry,
        MemoryTickStore(),
        clock=lambda: at("2026-09-29 10:20"),
    )
    status = scheduler.status()
    assert status["store"] == "memory"
    assert status["running"] is False
    assert status["schedules"][0]["next"] == "2026-09-29T11:00:00+00:00"


# -- the SQL store, on SQLite ------------------------------------------------


@pytest.fixture
async def sqlite_engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ticks.db'}")
    yield engine
    await engine.dispose()


async def test_the_sql_store_fires_each_tick_once_for_two_replicas(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    queue = ListQueue()
    replicas = []
    for owner in ("a", "b"):
        store = SqlTickStore(sqlite_engine, owner=owner)
        await store.setup()
        replicas.append(Scheduler(queue, hourly_registry(), store))  # type: ignore[arg-type]
    for replica in replicas:
        await replica.tick(at("2026-09-29 10:20"))
    for replica in replicas:
        await replica.tick(at("2026-09-29 11:00:01"))
    assert len(queue.jobs) == 1
    assert await replicas[1].store.last("report") == at("2026-09-29 11:00")


async def test_the_sql_store_releases_a_claim_whose_enqueue_failed(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    store = SqlTickStore(sqlite_engine)
    await store.setup()

    async def refuse(_: Any) -> None:
        raise ConnectionError("broker unreachable")

    with pytest.raises(ConnectionError):
        await store.fire("report", at("2026-09-29 11:00"), hold=timedelta(), enqueue=refuse)
    assert await store.last("report") is None


async def test_pruning_keeps_each_schedule_s_latest_tick(sqlite_engine) -> None:  # type: ignore[no-untyped-def]
    store = SqlTickStore(sqlite_engine)
    await store.setup()

    async def nothing(_: Any) -> None:
        return None

    for stamp in ("2026-01-01 00:00", "2026-02-01 00:00", "2026-09-28 00:00"):
        await store.fire("daily", at(stamp), hold=timedelta(), enqueue=nothing)
    await store.fire("monthly", at("2026-08-01 00:00"), hold=timedelta(), enqueue=nothing)

    await store.prune(at("2026-09-22 00:00"))
    async with sqlite_engine.connect() as conn:
        rows = (await conn.execute(select(ticks.c.name, ticks.c.fire_at))).all()
    assert sorted((name, stamp.date().isoformat()) for name, stamp in rows) == [
        ("daily", "2026-09-28"),
        ("monthly", "2026-08-01"),
    ]


# -- the plugin --------------------------------------------------------------


class _Stub(ListQueue):
    """A queue backend the plugin can load by path, with nothing behind it."""

    def __init__(self, ctx: Any) -> None:
        super().__init__()

    async def setup(self) -> None:
        return None

    async def health(self) -> tuple[bool, str]:
        return True, "stub"

    async def stats(self) -> dict[str, int]:
        return {"pending": len(self.jobs), "dead": 0}

    async def close(self) -> None:
        return None


STUB = "tests.test_scheduler:_Stub"


async def test_the_plugin_runs_the_scheduler_and_reports_it(tmp_path: Path) -> None:
    app = build_test_app(
        plugins=["database", "queue"],
        raw={
            "plugin": {
                "database": {"dsn": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"},
                "queue": {"backend": STUB, "scheduler": True, "expose_stats": False},
            }
        },
    )
    tasks = app.state.jfast.require("tasks")
    tasks.schedule("tidy", every=timedelta(hours=1))
    async with client_for(app) as client:
        await asyncio.sleep(0.2)
        body = (await client.get("/ready")).json()
    check = body["checks"]["queue"]
    assert check["status"] == "ok", check
    scheduler = check["meta"]["scheduler"]
    assert scheduler["store"] == "database"
    assert scheduler["running"] is True
    assert [s["name"] for s in scheduler["schedules"]] == ["tidy"]


async def test_a_stopped_scheduler_degrades_readiness_without_failing_it() -> None:
    app = build_test_app(
        plugins=["queue"],
        raw={"plugin": {"queue": {"backend": STUB, "scheduler": True, "expose_stats": False}}},
    )
    async with client_for(app) as client:
        plugin = next(p for p in app.state.plugins if p.meta.name == "queue")
        plugin._scheduler_stop.set()
        await asyncio.sleep(0.1)
        response = await client.get("/ready")
    check = response.json()["checks"]["queue"]
    assert response.status_code == 200
    assert check["status"] == "fail" and check["critical"] is False
    assert "not running" in check["detail"]
    assert check["meta"]["scheduler"]["store"] == "memory"


def test_production_refuses_a_scheduler_with_nothing_shared() -> None:
    with pytest.raises(PluginError, match="every replica"):
        build_test_app(
            plugins=["queue"],
            env="prod",
            raw={"plugin": {"queue": {"backend": STUB, "scheduler": True}}},
        )


def test_asking_for_a_store_whose_plugin_is_off_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="needs the 'cache' plugin"):
        build_test_app(
            plugins=["database", "queue"],
            raw={
                "plugin": {
                    "database": {"dsn": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"},
                    "queue": {"backend": STUB, "scheduler": True, "scheduler_store": "cache"},
                }
            },
        )


# -- against real servers ----------------------------------------------------


@pytest.fixture
async def pg_engines():  # type: ignore[no-untyped-def]
    engines = [create_async_engine(f"{PG_BASE}/jfast", pool_size=10) for _ in range(2)]
    try:
        async with engines[0].begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TICKS_TABLE}, jfast_jobs"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        for engine in engines:
            await engine.dispose()
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    yield engines
    for engine in engines:
        await engine.dispose()


async def test_twenty_concurrent_claims_of_one_tick_on_postgres_land_once(pg_engines) -> None:  # type: ignore[no-untyped-def]
    stores = [SqlTickStore(engine, owner=f"replica-{i}") for i, engine in enumerate(pg_engines)]
    await stores[0].setup()
    await stores[1].setup()
    enqueued: list[str] = []

    async def record(_: Any) -> None:
        enqueued.append("job")

    tick = at("2026-09-29 11:00")
    results = await asyncio.gather(
        *(stores[i % 2].fire("report", tick, hold=timedelta(), enqueue=record) for i in range(20))
    )
    assert results.count(True) == 1
    assert enqueued == ["job"]
    assert await stores[1].last("report") == tick


async def test_on_postgres_the_claim_and_the_job_commit_together(pg_engines) -> None:  # type: ignore[no-untyped-def]
    from jfastframework.queues.postgres import PostgresQueue

    engine = pg_engines[0]
    queue = PostgresQueue(engine)
    await queue.setup()
    store = SqlTickStore(engine)
    await store.setup()
    registry = hourly_registry()

    replicas = [Scheduler(queue, registry, store) for _ in range(3)]
    for replica in replicas:
        await replica.tick(at("2026-09-29 10:20"))
    results = await asyncio.gather(
        *(replica.tick(at("2026-09-29 11:00:01")) for replica in replicas)
    )
    assert sum(len(fired) for fired in results) == 1

    async with engine.connect() as conn:
        jobs = (await conn.execute(text("SELECT id, task FROM jfast_jobs"))).all()
        claims = (await conn.execute(select(func.count()).select_from(ticks))).scalar()
    assert [(job_id, task) for job_id, task in jobs] == [
        (registry.schedules[0].job_id(at("2026-09-29 11:00")), "report")
    ]
    assert claims == 1

    # One transaction: a process that dies after writing the job and before
    # committing leaves neither the job nor the claim. Enqueued apart from
    # the claim, the job would already be in the queue here.
    dying = Scheduler(queue, registry, _DiesBeforeCommit(engine))
    await dying.tick(at("2026-09-29 11:00:02"))
    with pytest.raises(RuntimeError, match="died before the commit"):
        await dying.tick(at("2026-09-29 12:00:01"))
    async with engine.connect() as conn:
        jobs_after = (await conn.execute(text("SELECT count(*) FROM jfast_jobs"))).scalar()
    assert jobs_after == 1
    assert await store.last("report") == at("2026-09-29 11:00")


class _DiesBeforeCommit(SqlTickStore):
    async def fire(self, name: str, fire_at: datetime, *, hold: timedelta, enqueue: Any) -> bool:
        async def then_die(transaction: Any) -> None:
            await enqueue(transaction)
            raise RuntimeError("the process died before the commit")

        return await super().fire(name, fire_at, hold=hold, enqueue=then_die)


async def test_the_same_tick_enqueued_twice_is_one_job_on_postgres(pg_engines) -> None:  # type: ignore[no-untyped-def]
    """The second line of defence: a double enqueue deduplicates on the job id."""
    from jfastframework.queues.postgres import PostgresQueue

    queue = PostgresQueue(pg_engines[0])
    await queue.setup()
    schedule = Schedule.build("report", "report", every=timedelta(hours=1))
    tick = at("2026-09-29 11:00")
    for _ in range(2):
        await queue.enqueue(Job(id=schedule.job_id(tick), task="report"))
    assert (await queue.stats())["pending"] == 1


async def test_pruning_on_postgres_keeps_each_schedule_s_latest(pg_engines) -> None:  # type: ignore[no-untyped-def]
    store = SqlTickStore(pg_engines[0])
    await store.setup()

    async def nothing(_: Any) -> None:
        return None

    for stamp in ("2026-01-01 00:00", "2026-09-28 00:00"):
        await store.fire("daily", at(stamp), hold=timedelta(), enqueue=nothing)
    await store.fire("monthly", at("2026-08-01 00:00"), hold=timedelta(), enqueue=nothing)
    await store.prune(at("2026-09-22 00:00"))
    async with pg_engines[0].connect() as conn:
        rows = (await conn.execute(select(ticks.c.name).order_by(ticks.c.name))).scalars().all()
    assert rows == ["daily", "monthly"]


@pytest.mark.skipif(not REDIS_URL, reason="set JFAST_TEST_REDIS_URL to claim ticks in a real Redis")
async def test_the_redis_store_fires_each_tick_once_and_remembers_the_latest() -> None:
    import uuid

    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    prefix = f"jfast-test:{uuid.uuid4().hex[:8]}:schedule"
    stores = [RedisTickStore(client, prefix=prefix, owner=f"replica-{i}") for i in range(2)]
    enqueued: list[int] = []

    async def record(_: Any) -> None:
        enqueued.append(1)

    async def refuse(_: Any) -> None:
        raise ConnectionError("broker unreachable")

    try:
        tick = at("2026-09-29 11:00")
        results = await asyncio.gather(
            *(
                stores[i % 2].fire("report", tick, hold=timedelta(minutes=10), enqueue=record)
                for i in range(20)
            )
        )
        assert results.count(True) == 1 and enqueued == [1]

        # An older tick won late does not move "latest" backwards.
        await stores[0].fire(
            "report", at("2026-09-29 10:00"), hold=timedelta(minutes=10), enqueue=record
        )
        assert await stores[1].last("report") == tick

        # A failed enqueue leaves the tick claimable.
        later = at("2026-09-29 12:00")
        with pytest.raises(ConnectionError):
            await stores[0].fire("report", later, hold=timedelta(minutes=10), enqueue=refuse)
        assert await stores[1].fire("report", later, hold=timedelta(minutes=10), enqueue=record)

        ttl = await client.pttl(f"{prefix}:tick:report:{int(later.timestamp() * 1000)}")
        assert 0 < ttl <= 600_000
    finally:
        keys = [key async for key in client.scan_iter(f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_two_running_services_on_postgres_enqueue_each_tick_once(pg_engines) -> None:  # type: ignore[no-untyped-def]
    """The whole path: two apps with the plugin on, each running its loop."""

    def replica() -> Any:
        app = build_test_app(
            plugins=["database", "queue"],
            raw={
                "plugin": {
                    "database": {"dsn": f"{PG_BASE}/jfast"},
                    "queue": {"backend": "postgres", "scheduler": True, "expose_stats": False},
                }
            },
        )
        app.state.jfast.require("tasks").schedule("heartbeat", every=timedelta(seconds=1))
        return app

    async with client_for(replica()) as first, client_for(replica()) as second:
        await asyncio.sleep(3.3)
        for client in (first, second):
            check = (await client.get("/ready")).json()["checks"]["queue"]
            assert check["meta"]["scheduler"]["running"] is True, check

    engine = pg_engines[0]
    async with engine.connect() as conn:
        jobs = (await conn.execute(text("SELECT id FROM jfast_jobs"))).scalars().all()
        claimed = (
            await conn.execute(
                select(ticks.c.claimed_by, ticks.c.fire_at).order_by(ticks.c.fire_at)
            )
        ).all()
    assert len(claimed) >= 2
    assert len(jobs) == len(set(jobs)) == len(claimed)
