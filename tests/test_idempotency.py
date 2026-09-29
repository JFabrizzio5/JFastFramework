"""Idempotency keys, through the whole app: dependency, transaction, recorder.

SQLite for the rules; PostgreSQL for the race, where two requests with one key
arrive together and exactly one of them may act.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.idempotency import IdempotencyKey, RequiredIdempotencyKey
from jfastframework.plugins.builtin.database import DbSession
from jfastframework.plugins.builtin.idempotency import IdempotencyPlugin
from jfastframework.testing import build_test_app

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")


def _router(delay: float = 0.0) -> APIRouter:
    router = APIRouter()

    @router.post("/payments", status_code=201)
    async def pay(request: Request, session: DbSession, key: IdempotencyKey) -> dict[str, Any]:
        body = await request.json()
        if body.get("explode"):
            await session.execute(text("INSERT INTO payments (amount) VALUES (-1)"))
            raise RuntimeError("the handler failed after writing")
        result = await session.execute(
            text("INSERT INTO payments (amount) VALUES (:a) RETURNING id"), {"a": body["amount"]}
        )
        if delay:
            await asyncio.sleep(delay)
        return {"id": result.scalar_one(), "amount": body["amount"]}

    @router.post("/strict", status_code=201)
    async def strict(session: DbSession, key: RequiredIdempotencyKey) -> dict[str, str | None]:
        return {"key": key}

    return router


async def _app(dsn: str, delay: float = 0.0) -> Any:
    app = build_test_app(
        plugins=["database", "idempotency"],
        extra_plugins=[IdempotencyPlugin],
        routers=[_router(delay)],
        raw={"plugin": {"database": {"dsn": dsn}}},
    )

    @app.middleware("http")
    async def tenant_from_header(request: Request, call_next: Any) -> Any:
        request.state.tenant_id = request.headers.get("x-tenant")
        return await call_next(request)

    return app


class _Running:
    """The app with its lifespan, and a client that sees what a real one would."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __aenter__(self) -> httpx.AsyncClient:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        self._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()
        await self._lifespan.__aexit__(*exc)


@pytest.fixture
async def sqlite_dsn(tmp_path: Path) -> str:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}"
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, amount INTEGER)")
        )
    await engine.dispose()
    return dsn


async def _count(dsn: str) -> int:
    engine = create_async_engine(dsn)
    async with engine.connect() as conn:
        count = int((await conn.execute(text("SELECT count(*) FROM payments"))).scalar_one())
    await engine.dispose()
    return count


async def test_without_a_key_every_request_acts(sqlite_dsn: str) -> None:
    async with _Running(await _app(sqlite_dsn)) as client:
        for _ in range(2):
            assert (await client.post("/payments", json={"amount": 5})).status_code == 201
    assert await _count(sqlite_dsn) == 2


async def test_a_retry_gets_the_first_answer_and_acts_once(sqlite_dsn: str) -> None:
    headers = {"Idempotency-Key": "pay-1"}
    async with _Running(await _app(sqlite_dsn)) as client:
        first = await client.post("/payments", json={"amount": 5}, headers=headers)
        second = await client.post("/payments", json={"amount": 5}, headers=headers)

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert await _count(sqlite_dsn) == 1


async def test_the_same_key_for_a_different_request_is_refused(sqlite_dsn: str) -> None:
    headers = {"Idempotency-Key": "pay-2"}
    async with _Running(await _app(sqlite_dsn)) as client:
        await client.post("/payments", json={"amount": 5}, headers=headers)
        other = await client.post("/payments", json={"amount": 9}, headers=headers)
    assert other.status_code == 422
    assert await _count(sqlite_dsn) == 1


async def test_a_failed_request_leaves_the_key_free(sqlite_dsn: str) -> None:
    headers = {"Idempotency-Key": "pay-3"}
    async with _Running(await _app(sqlite_dsn)) as client:
        failed = await client.post("/payments", json={"explode": True}, headers=headers)
        assert failed.status_code == 500
        # The key rolled back with the work, so this is a first attempt.
        retried = await client.post(
            "/payments", json={"explode": False, "amount": 1}, headers=headers
        )
    # Different body from the failed attempt is fine: nothing was recorded.
    assert retried.status_code == 201
    assert await _count(sqlite_dsn) == 1


async def test_keys_are_per_tenant(sqlite_dsn: str) -> None:
    async with _Running(await _app(sqlite_dsn)) as client:
        for tenant in ("acme", "globex"):
            response = await client.post(
                "/payments",
                json={"amount": 5},
                headers={"Idempotency-Key": "same", "X-Tenant": tenant},
            )
            assert response.status_code == 201
            assert "idempotent-replayed" not in response.headers
    assert await _count(sqlite_dsn) == 2


async def test_a_required_key_must_be_sent(sqlite_dsn: str) -> None:
    async with _Running(await _app(sqlite_dsn)) as client:
        missing = await client.post("/strict")
        present = await client.post("/strict", headers={"Idempotency-Key": "k"})
        malformed = await client.post("/strict", headers={"Idempotency-Key": "has space"})
    assert missing.status_code == 422
    assert present.status_code == 201
    assert malformed.status_code == 422


async def test_two_requests_with_one_key_at_once_act_once() -> None:
    dsn = f"{PG_BASE}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS payments, jfast_idempotency"))
            await conn.execute(text("CREATE TABLE payments (id serial PRIMARY KEY, amount int)"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await engine.dispose()

    headers = {"Idempotency-Key": "race"}
    async with _Running(await _app(dsn, delay=0.3)) as client:
        responses = await asyncio.gather(
            *(client.post("/payments", json={"amount": 5}, headers=headers) for _ in range(4))
        )

    statuses = sorted(r.status_code for r in responses)
    # One acted. The others waited on its insert and then found the key -- in
    # progress (409) or completed (a replayed 201) -- and none acted again.
    assert statuses.count(201) >= 1
    assert set(statuses) <= {201, 409}
    replayed = [r for r in responses if r.headers.get("idempotent-replayed")]
    assert statuses.count(201) == 1 + len(replayed)
    assert await _count(dsn) == 1
