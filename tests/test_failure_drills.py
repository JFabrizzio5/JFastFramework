"""Failure drills: take a dependency away from a running service, and give it back.

Each drill checks the three things docs/resilience.md promises: the route
answers the documented status inside its deadline instead of hanging, `/ready`
names the dependency, and the next request after the dependency returns
succeeds -- same app, same pools, no restart.

The PostgreSQL and Redis drills *pause* a real container (`docker pause` keeps
the socket open and stops answering, which is the hard case: a refused
connection fails fast on its own). They run only when told which container is
theirs, because pausing a shared server would break every other suite using
it::

    JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5499 \\
    JFAST_DRILL_PG_CONTAINER=jfast-ci-postgres \\
    JFAST_TEST_REDIS_URL=redis://localhost:6379/0 \\
    JFAST_DRILL_REDIS_CONTAINER=jfast-ci-redis \\
    pytest -q -s tests/test_failure_drills.py

The identity-provider drill needs no container: the issuer is an httpx mock
transport that answers, hangs or fails on command. Timings are printed with
``-s``; the numbers in docs/resilience.md came from these prints.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends

from jfastframework.auth import Principal, require_auth
from jfastframework.plugins.builtin.database import DbSession
from jfastframework.testing import build_test_app, client_for

PG_URL = os.environ.get("JFAST_TEST_PG_URL", "")
PG_CONTAINER = os.environ.get("JFAST_DRILL_PG_CONTAINER", "")
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "")
REDIS_CONTAINER = os.environ.get("JFAST_DRILL_REDIS_CONTAINER", "")
DOCKER = (
    os.environ.get("JFAST_DRILL_DOCKER")
    or shutil.which("docker")
    or "/Applications/Docker.app/Contents/Resources/bin/docker"
)


def _docker(*args: str) -> None:
    subprocess.run([DOCKER, *args], check=True, capture_output=True, timeout=60)


@contextlib.contextmanager
def paused(container: str) -> Iterator[None]:
    """The container frozen for the block, and always thawed after it."""
    _docker("pause", container)
    try:
        yield
    finally:
        _docker("unpause", container)


def report(drill: str, step: str, seconds: float, status: int) -> None:
    print(f"[drill] {drill:<9} {step:<34} {seconds:6.2f}s  HTTP {status}")


async def timed(http: Any, path: str, **kwargs: Any) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = await http.get(path, **kwargs)
    return response, time.monotonic() - started


needs_pg = pytest.mark.skipif(
    not (PG_URL and PG_CONTAINER and os.path.exists(DOCKER)),
    reason="set JFAST_TEST_PG_URL and JFAST_DRILL_PG_CONTAINER (a container this run owns)",
)
needs_redis = pytest.mark.skipif(
    not (REDIS_URL and REDIS_CONTAINER and os.path.exists(DOCKER)),
    reason="set JFAST_TEST_REDIS_URL and JFAST_DRILL_REDIS_CONTAINER (a container this run owns)",
)


# -- PostgreSQL --------------------------------------------------------------


@needs_pg
async def test_postgres_paused_answers_503_names_the_database_and_recovers() -> None:
    router = APIRouter()

    @router.get("/db")
    async def db(session: DbSession) -> dict[str, int]:
        from sqlalchemy import text

        return {"one": int((await session.execute(text("select 1"))).scalar_one())}

    # The framework's defaults, on purpose: the drill measures what ships.
    app = build_test_app(
        plugins=["database"],
        routers=[router],
        raw={"plugin": {"database": {"dsn": f"{PG_URL}/jfast"}}},
    )
    async with client_for(app) as http:
        response, seconds = await timed(http, "/db")
        assert response.status_code == 200
        report("postgres", "warm", seconds, response.status_code)

        with paused(PG_CONTAINER):
            # A pooled connection: bounded ping (2 s), then a new connection
            # under connect_timeout (10 s). Well inside the 30 s request
            # timeout, so the service answers 503 itself.
            response, seconds = await timed(http, "/db")
            report("postgres", "paused, pooled connection", seconds, response.status_code)
            assert response.status_code == 503
            assert response.json()["title"] == "Database Unavailable"
            assert seconds < 2 + 10 + 3

            ready, ready_seconds = await timed(http, "/ready")
            report("postgres", "paused, /ready", ready_seconds, ready.status_code)
            assert ready.status_code == 503
            assert ready.json()["checks"]["database"]["healthy"] is False
            assert ready_seconds < 2 + 1

            response, seconds = await timed(http, "/db")
            report("postgres", "paused, no pooled connection", seconds, response.status_code)
            assert response.status_code == 503
            assert seconds < 10 + 3

            # Two failed connects opened the connect breaker: from here a
            # request answers 503 without waiting on the server at all.
            response, seconds = await timed(http, "/db")
            report("postgres", "paused, breaker open", seconds, response.status_code)
            assert response.status_code == 503
            assert seconds < 0.5
            ready, ready_seconds = await timed(http, "/ready")
            report("postgres", "paused, /ready, breaker open", ready_seconds, ready.status_code)
            assert ready.status_code == 503
            assert "database" in ready.json()["checks"]["database"]["detail"]

        # One probe per breaker_cool_down (5 s) finds the server back.
        await asyncio.sleep(5.2)
        response, seconds = await timed(http, "/db")
        report("postgres", "unpaused, first request", seconds, response.status_code)
        assert response.status_code == 200
        ready, ready_seconds = await timed(http, "/ready")
        report("postgres", "unpaused, /ready", ready_seconds, ready.status_code)
        assert ready.status_code == 200


# -- Redis -------------------------------------------------------------------


def _redis_app(*, fail_open: bool) -> Any:
    router = APIRouter()
    loads = {"count": 0}

    @router.get("/cached")
    async def cached() -> dict[str, int]:
        cache = app.state.jfast.require("cache")

        async def loader() -> int:
            loads["count"] += 1
            return loads["count"]

        return {"value": await cache.get_or_set("drill:value", loader, ttl=1)}

    app = build_test_app(
        app_name=f"drill-{'open' if fail_open else 'closed'}",
        plugins=["cache", "ratelimit"],
        routers=[router],
        raw={
            "plugin": {
                "cache": {"url": REDIS_URL},
                "ratelimit": {"limit": 10_000, "window": 60, "fail_open": fail_open},
            }
        },
    )
    return app


@needs_redis
async def test_redis_paused_fails_open_reports_degraded_and_recovers() -> None:
    app = _redis_app(fail_open=True)
    closed = _redis_app(fail_open=False)
    async with client_for(app) as http, client_for(closed) as strict:
        response, seconds = await timed(http, "/cached")
        assert response.status_code == 200
        report("redis", "warm", seconds, response.status_code)

        with paused(REDIS_CONTAINER):
            # Rate limit check and cache read each wait command_timeout (1 s)
            # until three failures open the breaker; then nothing waits.
            timings = []
            for attempt in range(3):
                response, seconds = await timed(http, "/cached")
                report("redis", f"paused, request {attempt + 1}", seconds, response.status_code)
                assert response.status_code == 200  # fail open: served by the loader
                timings.append(seconds)
            assert timings[0] < 1 * 2 + 1.5
            assert timings[-1] < 0.5

            ready, ready_seconds = await timed(http, "/ready")
            report("redis", "paused, /ready", ready_seconds, ready.status_code)
            body = ready.json()
            assert ready.status_code == 200
            assert body["status"] == "degraded"
            assert body["checks"]["cache"]["healthy"] is False
            assert body["checks"]["ratelimit"]["healthy"] is False

            refused, seconds = await timed(strict, "/cached")
            report("redis", "paused, fail_open = false", seconds, refused.status_code)
            assert refused.status_code == 429
            assert "retry-after" in refused.headers

        # The breaker lets a probe through after breaker_cool_down (5 s).
        await asyncio.sleep(5.2)
        response, seconds = await timed(http, "/cached")
        report("redis", "unpaused, first request", seconds, response.status_code)
        assert response.status_code == 200
        ready, ready_seconds = await timed(http, "/ready")
        report("redis", "unpaused, /ready", ready_seconds, ready.status_code)
        assert ready.status_code == 200
        assert ready.json()["status"] == "ok", ready.json()


# -- identity provider -------------------------------------------------------


class _Issuer:
    def __init__(self, jwk: dict[str, Any]) -> None:
        self.jwk = jwk
        self.mode = "ok"
        self.calls = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "slow":
            # Longer than jwks_timeout: httpx gives up first.
            await asyncio.sleep(5)
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        await asyncio.sleep(0.005)
        return httpx.Response(200, json={"keys": [self.jwk]})


async def test_identity_provider_slow_then_down_cached_keys_keep_working() -> None:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "drill", "alg": "RS256", "use": "sig"})
    token = jwt.encode(
        {"sub": "user-1", "aud": "drill", "iat": int(time.time()), "exp": int(time.time()) + 600},
        private,
        algorithm="RS256",
        headers={"kid": "drill"},
    )
    headers = {"Authorization": f"Bearer {token}"}

    router = APIRouter()

    @router.get("/me")
    async def me(caller: Principal = Depends(require_auth)) -> dict[str, str]:
        return {"subject": caller.subject}

    app = build_test_app(
        plugins=["auth"],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "jwks",
                    "jwks_url": "https://id.example.com/.well-known/jwks.json",
                    "audience": "drill",
                    "jwks_timeout": 0.5,
                    "jwks_breaker_cool_down": 1.0,
                }
            }
        },
    )
    issuer = _Issuer(jwk)
    plugin = app.state.jfast.require("auth")
    plugin._jwks.transport = httpx.MockTransport(issuer.handler)

    async with client_for(app) as http:
        response, seconds = await timed(http, "/me", headers=headers)
        report("jwks", "warm (keys fetched)", seconds, response.status_code)
        assert response.status_code == 200 and issuer.calls == 1

        issuer.mode = "slow"
        timings = []
        for attempt in range(5):
            plugin._jwks._fetched_at -= 7200  # expired: each request wants a refresh
            response, seconds = await timed(http, "/me", headers=headers)
            report("jwks", f"issuer hangs, request {attempt + 1}", seconds, response.status_code)
            assert response.status_code == 200  # the cached key still verifies
            timings.append(seconds)
        # Two tries of 0.5 s plus backoff until three failures open the
        # breaker; after that no request waits on the issuer at all.
        assert timings[0] < 0.5 * 2 + 1.5
        assert timings[-1] < 0.2
        calls_while_open = issuer.calls

        issuer.mode = "down"
        plugin._jwks._fetched_at -= 7200
        response, seconds = await timed(http, "/me", headers=headers)
        report("jwks", "issuer down, breaker open", seconds, response.status_code)
        assert response.status_code == 200
        assert issuer.calls == calls_while_open  # open: nothing sent

        ready, ready_seconds = await timed(http, "/ready")
        report("jwks", "issuer down, /ready", ready_seconds, ready.status_code)
        # Degraded, not unavailable: every replica still verifies with the
        # cached key, so taking them out of rotation would help nobody.
        assert ready.status_code == 200
        assert ready.json()["status"] == "degraded"
        assert "last refresh failed" in ready.json()["checks"]["auth"]["detail"]

        issuer.mode = "ok"
        await asyncio.sleep(1.1)
        plugin._jwks._fetched_at -= 7200
        response, seconds = await timed(http, "/me", headers=headers)
        report("jwks", "issuer back, probe", seconds, response.status_code)
        assert response.status_code == 200
        assert plugin._jwks.breaker.state.value == "closed"
        assert (await plugin._jwks.health())[0]
        ready, _ = await timed(http, "/ready")
        assert ready.status_code == 200
        assert "refresh failed" not in ready.json()["checks"]["auth"]["detail"]
