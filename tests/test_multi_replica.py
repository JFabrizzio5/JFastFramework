"""Two replicas, one PostgreSQL, one Redis: what must happen once, happens once.

Each "replica" here is its own engine, its own Redis connection, its own
relay, scheduler, LLM client or app -- sharing nothing in memory, as two pods
share nothing. What they share is what production replicas share: the
database and the cache. Nothing is mocked on that side; the only fake is the
model provider, because the budget is what is under test, not OpenAI.

    JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5487 \\
    JFAST_TEST_REDIS_URL=redis://localhost:6387/0 pytest tests/test_multi_replica.py

The queue and scheduler are driven through their public contract
(``enqueue``/``dequeue``/``ack``, ``Scheduler.tick``), not through the
worker class, so the proof holds whatever runs the loop.
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "redis://localhost:6379/0")
DATABASE = "jfast_multi_replica"
REPLICAS = 2


# -- shared infrastructure ------------------------------------------------


@pytest.fixture
async def engines() -> AsyncIterator[list[AsyncEngine]]:
    """One engine per replica, on a database of this file's own."""
    admin = create_async_engine(f"{PG_BASE}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": DATABASE}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{DATABASE}"'))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await admin.dispose()

    built = [create_async_engine(f"{PG_BASE}/{DATABASE}", pool_size=10) for _ in range(REPLICAS)]
    async with built[0].begin() as conn:
        await conn.execute(
            text("DROP TABLE IF EXISTS jfast_outbox, jfast_inbox, jfast_jobs, jfast_schedule_ticks")
        )
    yield built
    for engine in built:
        await engine.dispose()


@pytest.fixture
async def redis_clients() -> AsyncIterator[list[Any]]:
    from redis.asyncio import from_url

    clients = [from_url(REDIS_URL, decode_responses=True) for _ in range(REPLICAS)]
    try:
        await clients[0].ping()
    except Exception:  # noqa: BLE001 - no server
        for client in clients:
            await client.aclose()
        pytest.skip(f"no Redis at {REDIS_URL}")
    yield clients
    for client in clients:
        await client.aclose()


# -- outbox relay ---------------------------------------------------------


async def test_the_outbox_is_relayed_and_consumed_once_across_replicas(
    engines: list[AsyncEngine], redis_clients: list[Any]
) -> None:
    """400 messages written through both replicas, two relays, two workers.

    Every message reaches the queue once (the relays' counts add up to 400
    and no row is left pending) and is consumed once (the workers see each
    payload exactly once between them).
    """
    from jfastframework.db.framework import ensure_tables
    from jfastframework.outbox import INBOX_TABLE, OUTBOX_TABLE, Outbox, OutboxRelay, outbox
    from jfastframework.queues.base import Job
    from jfastframework.queues.redis import RedisQueue

    await ensure_tables(engines[0], OUTBOX_TABLE, INBOX_TABLE)
    name = f"jfast:test:replicas:{uuid.uuid4().hex[:8]}"
    queues = [
        RedisQueue(client, name=name, consumer=f"worker-{i}")
        for i, client in enumerate(redis_clients)
    ]
    total = 400

    async def request(n: int) -> None:
        replica = n % REPLICAS
        async with AsyncSession(engines[replica]) as session, session.begin():
            await Outbox(queue=queues[replica]).enqueue(
                session, Job(task="notify", payload={"n": n})
            )

    await asyncio.gather(*(request(n) for n in range(total)))

    relays = [
        OutboxRelay(engine, queue=queue, batch_size=20)
        for engine, queue in zip(engines, queues, strict=True)
    ]
    published = [0] * REPLICAS
    consumed: list[tuple[int, int]] = []
    relayed = asyncio.Event()

    async def relay(i: int) -> None:
        while True:
            sent = await relays[i].relay_once()
            published[i] += sent
            if sent == 0 and sum(published) >= total:
                relayed.set()
                return
            await asyncio.sleep(0)

    async def worker(i: int) -> None:
        while True:
            job = await queues[i].dequeue(timeout=0.2)
            if job is None:
                if relayed.is_set() and len(consumed) >= total:
                    return
                if relayed.is_set() and await redis_clients[i].llen(f"{name}:pending") == 0:
                    return
                continue
            consumed.append((i, int(job.payload["n"])))
            await queues[i].ack(job)

    await asyncio.wait_for(
        asyncio.gather(*(relay(i) for i in range(REPLICAS)), *(worker(i) for i in range(REPLICAS))),
        timeout=120,
    )

    assert sum(published) == total, published
    async with engines[0].connect() as conn:
        pending = await conn.scalar(
            select(func.count()).select_from(outbox).where(outbox.c.status != "published")
        )
    assert pending == 0
    assert sorted(n for _, n in consumed) == list(range(total))
    # Both relays and both workers took part: otherwise this proved nothing
    # about two of them.
    assert all(published), published
    assert {worker for worker, _ in consumed} == set(range(REPLICAS))


# -- scheduler ------------------------------------------------------------


def _registry() -> Any:
    from jfastframework.queues.worker import TaskRegistry

    registry = TaskRegistry()

    @registry.task("minutely", every=timedelta(minutes=1))
    async def minutely(payload: dict[str, Any]) -> None:
        return None

    return registry


async def _tick_both(schedulers: list[Any], start: datetime, steps: int) -> list[int]:
    """Every replica runs the pass for the same instant at the same time."""
    fired: list[int] = []
    for step in range(steps):
        now = start + timedelta(minutes=step)
        results = await asyncio.gather(*(s.tick(now) for s in schedulers))
        fired.append(sum(len(jobs) for jobs in results))
    return fired


START = datetime(2026, 9, 30, 12, 0, 30, tzinfo=UTC)


async def test_the_scheduler_enqueues_each_tick_once_on_postgres(
    engines: list[AsyncEngine],
) -> None:
    from jfastframework.queues.postgres import PostgresQueue
    from jfastframework.queues.scheduler import Scheduler
    from jfastframework.queues.sql_ticks import SqlTickStore

    queues = [PostgresQueue(engine) for engine in engines]
    await queues[0].setup()
    stores = [SqlTickStore(engine, owner=f"replica-{i}") for i, engine in enumerate(engines)]
    await stores[0].setup()
    schedulers = [
        Scheduler(queue, _registry(), store) for queue, store in zip(queues, stores, strict=True)
    ]

    fired = await _tick_both(schedulers, START, 40)

    # The first pass only notes the schedule (new schedules start with the
    # next tick); every minute after that fires exactly once, in one replica.
    assert fired == [0] + [1] * 39, fired
    async with engines[0].connect() as conn:
        claims = (
            await conn.execute(
                text("SELECT claimed_by, count(*) FROM jfast_schedule_ticks GROUP BY claimed_by")
            )
        ).all()
        jobs = await conn.scalar(text("SELECT count(*) FROM jfast_jobs"))
    assert sum(count for _, count in claims) == 39
    assert jobs == 39


async def test_the_scheduler_enqueues_each_tick_once_on_redis(
    redis_clients: list[Any],
) -> None:
    """The cache-only setup, and a queue that does not deduplicate job ids:
    a double enqueue would show up as a 40th job."""
    from jfastframework.queues.redis import RedisQueue
    from jfastframework.queues.scheduler import RedisTickStore, Scheduler

    name = f"jfast:test:ticks:{uuid.uuid4().hex[:8]}"
    schedulers = [
        Scheduler(
            RedisQueue(client, name=name, consumer=f"replica-{i}"),
            _registry(),
            RedisTickStore(client, prefix=name, owner=f"replica-{i}"),
        )
        for i, client in enumerate(redis_clients)
    ]

    fired = await _tick_both(schedulers, START, 40)

    assert fired == [0] + [1] * 39, fired
    assert await redis_clients[0].llen(f"{name}:pending") == 39


# -- LLM budget -----------------------------------------------------------


def _llm_client(redis: Any, prefix: str, *, budget: float, tenant_budget: float = 0.0) -> Any:
    import httpx

    from jfastframework.llm import LLMClient, RedisLedger

    async def provider(request: httpx.Request) -> httpx.Response:
        # Slow enough that calls from both replicas overlap in flight.
        await asyncio.sleep(random.uniform(0.005, 0.03))
        return httpx.Response(
            200,
            json={
                "model": "replica-model",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                # Exactly what was reserved: 8 estimated prompt tokens + 92.
                "usage": {"prompt_tokens": 8, "completion_tokens": 92},
            },
        )

    return LLMClient(
        api_key="test",
        chat_model="replica-model",
        # $0.001 a token: a call reserves and costs $0.10.
        prices={"replica-model": [1000.0, 1000.0]},
        budget_usd=budget,
        tenant_budget_usd=tenant_budget,
        ledger=RedisLedger(redis, prefix=prefix),
        transport=httpx.MockTransport(provider),
        max_retries=0,
    )


async def _fire(clients: list[Any], calls: int, tenant: str | None = None) -> tuple[int, int]:
    from jfastframework.llm import BudgetExceededError

    async def one(n: int) -> bool:
        try:
            await clients[n % len(clients)].chat(
                [{"role": "user", "content": "hi"}], max_tokens=92, tenant_id=tenant
            )
        except BudgetExceededError:
            return False
        return True

    outcomes = await asyncio.gather(*(one(n) for n in range(calls)))
    return outcomes.count(True), outcomes.count(False)


async def test_one_llm_budget_holds_across_replicas(redis_clients: list[Any]) -> None:
    """60 concurrent calls over two replicas against a $1.00 cap at $0.10 each.

    Exactly ten are sent: not eleven (overshoot), not nine (a reservation
    leaked or rolled back wrongly). The ledger ends at what was really
    spent, so no reservation is left behind.
    """
    prefix = f"jfast:test:llm:{uuid.uuid4().hex[:8]}:"
    clients = [_llm_client(redis, prefix, budget=1.0) for redis in redis_clients]

    sent, refused = await _fire(clients, 60)

    assert (sent, refused) == (10, 50)
    spend = await clients[1].spend(recent=0)
    assert spend["spent_usd"] == pytest.approx(1.0)
    assert spend["spent_usd"] <= 1.0 + 1e-9


async def test_a_tenant_budget_holds_across_replicas(redis_clients: list[Any]) -> None:
    prefix = f"jfast:test:llm:{uuid.uuid4().hex[:8]}:"
    clients = [
        _llm_client(redis, prefix, budget=10.0, tenant_budget=0.3) for redis in redis_clients
    ]

    acme = await _fire(clients, 30, tenant="acme")
    globex = await _fire(clients, 30, tenant="globex")

    assert acme == (3, 27)
    assert globex == (3, 27)
    spend = await clients[0].spend("acme", recent=0)
    assert spend["tenant_spent_usd"] == pytest.approx(0.3)
    assert spend["spent_usd"] == pytest.approx(0.6)


# -- token revocation -----------------------------------------------------

SECRET = "multi-replica-secret-long-enough-for-hs256-32-bytes"


def _auth_app() -> Any:
    from jfastframework.testing import build_test_app

    return build_test_app(
        plugins=["cache", "auth"],
        app_name="replicated",
        raw={
            "plugin": {
                "cache": {"url": REDIS_URL},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": "https://id.replicas.test/",
                    "audience": "replicated",
                    "issue_tokens": True,
                },
            }
        },
    )


def _issuer(app: Any) -> Any:
    plugin = next(p for p in app.state.plugins if p.meta.name == "auth")
    return plugin._issuer


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_logout_on_one_replica_is_honoured_by_the_other(
    redis_clients: list[Any],
) -> None:
    from jfastframework.testing import client_for

    first, second = _auth_app(), _auth_app()
    async with client_for(first) as a, client_for(second) as b:
        pair = await _issuer(first).issue_pair("user-1", tenant_id="acme")

        assert (await b.get("/auth/me", headers=_bearer(pair.access_token))).status_code == 200
        assert (await a.post("/auth/logout", headers=_bearer(pair.access_token))).status_code == 204

        # The other replica never saw the logout; it reads the same store.
        assert (await b.get("/auth/me", headers=_bearer(pair.access_token))).status_code == 401
        refreshed = await b.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
        assert refreshed.status_code == 401


async def test_one_refresh_token_presented_to_both_replicas_rotates_once(
    redis_clients: list[Any],
) -> None:
    """Two tabs, two replicas, the same refresh token at the same moment.

    One rotation wins; the other is told it lost the race (401) without the
    session being treated as stolen -- the winner's new pair keeps working
    on both replicas.
    """
    from jfastframework.testing import client_for

    first, second = _auth_app(), _auth_app()
    async with client_for(first) as a, client_for(second) as b:
        pair = await _issuer(first).issue_pair("user-1", tenant_id="acme")
        body = {"refresh_token": pair.refresh_token}
        answers = await asyncio.gather(
            a.post("/auth/refresh", json=body), b.post("/auth/refresh", json=body)
        )

        statuses = sorted(r.status_code for r in answers)
        assert statuses == [200, 401], [r.text for r in answers]
        winner = next(r for r in answers if r.status_code == 200).json()
        for client in (a, b):
            me = await client.get("/auth/me", headers=_bearer(winner["access_token"]))
            assert me.status_code == 200
