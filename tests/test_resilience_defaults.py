"""Deadlines, retries and breakers that are on by default, tested without servers.

A dependency that is *down* is easy to fake: a closed port. One that is
*hung* -- accepts the connection and never answers, which is what a paused
container, a frozen VM or a full accept queue looks like -- is the case that
used to hold requests forever, and it is faked here with a socket that reads
and never writes. The drills in test_failure_drills.py do the same against the
real servers.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends

from jfastframework.auth import Principal, require_auth
from jfastframework.auth.jwks import JWKSClient, JWKSError
from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta
from jfastframework.plugins.builtin.auth import AuthPlugin
from jfastframework.plugins.builtin.cache import Cache, CacheSettings, build_client
from jfastframework.plugins.builtin.database import DbSession
from jfastframework.plugins.builtin.mail import MailPlugin, is_permanent
from jfastframework.queues.base import Job, _current_job
from jfastframework.queues.worker import TaskRegistry
from jfastframework.testing import build_test_app, client_for

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"


@pytest.fixture
async def silent_port() -> AsyncIterator[int]:
    """A TCP server that accepts, reads, and never answers."""
    held: list[asyncio.StreamWriter] = []

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)
        while await reader.read(4096):
            pass

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        yield port
    finally:
        for writer in held:
            writer.close()
        server.close()


def closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# -- cache -----------------------------------------------------------------


def _redis(port: int, **overrides: Any) -> Any:
    settings = CacheSettings(
        url=f"redis://127.0.0.1:{port}/0",
        _env_file=None,  # type: ignore[call-arg]
        **{"command_timeout": 0.2, "breaker_failures": 2, "breaker_cool_down": 0.3, **overrides},
    )
    return build_client(settings)


async def test_a_hung_redis_times_out_then_the_breaker_fails_fast(silent_port: int) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    client = _redis(silent_port)
    for _ in range(2):
        started = time.monotonic()
        with pytest.raises(RedisTimeoutError, match=r"within 0\.2s"):
            await client.get("k")
        assert time.monotonic() - started < 1.0

    started = time.monotonic()
    with pytest.raises(RedisConnectionError) as caught:
        await client.get("k")
    assert time.monotonic() - started < 0.05
    # Both a redis error and the framework's 503.
    from jfastframework.http.errors import CircuitOpenError

    assert isinstance(caught.value, CircuitOpenError)
    await client.aclose()


async def test_blocking_commands_are_not_cut_short(silent_port: int) -> None:
    client = _redis(silent_port, command_timeout=0.05, breaker_failures=0)
    # The outer deadline fires, not the command's: BLMOVE is allowed to wait.
    with pytest.raises(TimeoutError) as caught:
        await asyncio.wait_for(client.blmove("a", "b", timeout=5), timeout=0.4)
    assert type(caught.value) is TimeoutError
    await client.aclose()


async def test_get_or_set_fails_open_on_a_hung_redis(silent_port: int) -> None:
    client = _redis(silent_port)
    cache = Cache(client, stampede_wait=0)

    async def loader() -> str:
        return "computed"

    values = [await cache.get_or_set("k", loader) for _ in range(4)]
    assert values == ["computed"] * 4
    # Once open, a miss costs microseconds rather than a timeout.
    started = time.monotonic()
    assert await cache.get_or_set("k", loader) == "computed"
    assert time.monotonic() - started < 0.05
    await client.aclose()


async def test_the_breaker_closes_when_redis_answers_again(silent_port: int) -> None:
    client = _redis(closed_port())
    for _ in range(2):
        with pytest.raises(Exception):  # noqa: B017 - refused, however redis-py words it
            await client.get("k")
    assert client.jfast_breaker.state.value == "open"
    await asyncio.sleep(0.35)
    assert client.jfast_breaker.state.value == "half_open"
    await client.aclose()


async def test_cache_health_is_degraded_never_critical() -> None:
    app = build_test_app(
        plugins=["cache"],
        raw={
            "plugin": {
                "cache": {
                    "url": f"redis://127.0.0.1:{closed_port()}/0",
                    "command_timeout": 0.2,
                }
            }
        },
    )
    async with client_for(app) as http:
        response = await http.get("/ready")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["checks"]["cache"]["healthy"] is False
    assert body["checks"]["cache"]["critical"] is False


# -- /ready ------------------------------------------------------------------


class _Broken(Plugin):
    meta = PluginMeta(name="broken", health_critical=False)

    async def health(self, ctx: Any) -> HealthReport:
        raise RuntimeError("bug in the probe")


class _Forgetful(Plugin):
    # Declared non-critical, but a branch of its check forgot critical=False.
    meta = PluginMeta(name="forgetful", health_critical=False)

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.fail("down")


class _Vital(Plugin):
    meta = PluginMeta(name="vital")

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.fail("down")


async def test_a_non_critical_plugin_cannot_fail_readiness() -> None:
    app = build_test_app(plugins=["broken", "forgetful"], extra_plugins=[_Broken, _Forgetful])
    async with client_for(app) as http:
        response = await http.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["broken"]["status"] == "error"


async def test_a_critical_plugin_still_fails_readiness() -> None:
    app = build_test_app(plugins=["forgetful", "vital"], extra_plugins=[_Forgetful, _Vital])
    async with client_for(app) as http:
        response = await http.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


# -- database ----------------------------------------------------------------


def _db_app(dsn: str, **database: Any) -> Any:
    router = APIRouter()

    @router.get("/count")
    async def count(session: DbSession) -> dict[str, int]:
        from sqlalchemy import text

        return {"n": int((await session.execute(text("select 1"))).scalar_one())}

    @router.get("/pool-full")
    async def pool_full() -> None:
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        raise PoolTimeout("QueuePool limit reached")

    @router.get("/integrity")
    async def integrity() -> None:
        from sqlalchemy.exc import IntegrityError

        raise IntegrityError("insert", {}, Exception("duplicate key"))

    return build_test_app(
        plugins=["database"],
        routers=[router],
        raw={"plugin": {"database": {"dsn": dsn, **database}}},
    )


async def test_a_database_that_is_down_answers_503() -> None:
    app = _db_app(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port()}/db")
    async with client_for(app) as http:
        response = await http.get("/count")
    assert response.status_code == 503
    assert response.json()["title"] == "Database Unavailable"


async def test_a_hung_database_answers_503_within_connect_timeout(silent_port: int) -> None:
    app = _db_app(f"postgresql+asyncpg://u:p@127.0.0.1:{silent_port}/db", connect_timeout=0.5)
    async with client_for(app) as http:
        started = time.monotonic()
        response = await http.get("/count")
        elapsed = time.monotonic() - started
        ready = await http.get("/ready")
    assert response.status_code == 503
    assert elapsed < 3
    assert ready.status_code == 503
    assert ready.json()["checks"]["database"]["healthy"] is False


async def test_a_full_pool_answers_503() -> None:
    app = _db_app(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port()}/db")
    async with client_for(app) as http:
        response = await http.get("/pool-full")
    assert response.status_code == 503
    assert "in use" in response.json()["detail"]


async def test_other_database_errors_stay_500() -> None:
    app = _db_app(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port()}/db")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        response = await http.get("/integrity")
    assert response.status_code == 500


# -- JWKS --------------------------------------------------------------------


class Issuer:
    """An identity provider whose behaviour the test sets."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "ok"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # A real round trip yields to the loop; without this every caller
        # would run to completion before the next one started.
        await asyncio.sleep(0.01)
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if self.mode == "503":
            return httpx.Response(503)
        if self.mode == "404":
            return httpx.Response(404)
        return httpx.Response(200, json={"keys": [{"kid": "k1", "kty": "oct"}]})


def _jwks(issuer: Issuer, **overrides: Any) -> JWKSClient:
    options: dict[str, Any] = {
        "url": "https://id.example.com/jwks.json",
        "transport": httpx.MockTransport(issuer.handler),
        "breaker_cool_down": 0.2,
        **overrides,
    }
    return JWKSClient(**options)


async def test_jwks_retries_a_transient_failure_once() -> None:
    issuer = Issuer()
    issuer.mode = "503"
    client = _jwks(issuer)
    with pytest.raises(JWKSError):
        await client.key_for("k1")
    assert issuer.calls == 2


async def test_jwks_does_not_retry_a_404() -> None:
    issuer = Issuer()
    issuer.mode = "404"
    client = _jwks(issuer)
    with pytest.raises(JWKSError):
        await client.key_for("k1")
    assert issuer.calls == 1


async def test_jwks_outage_opens_the_breaker_and_cached_keys_keep_working() -> None:
    issuer = Issuer()
    client = _jwks(issuer, attempts=1)
    assert await client.key_for("k1") == {"kid": "k1", "kty": "oct"}

    issuer.mode = "down"
    for _ in range(3):
        client._fetched_at -= 7200  # expired: every call wants a refresh
        assert await client.key_for("k1") == {"kid": "k1", "kty": "oct"}
    assert issuer.calls == 4
    assert client.breaker is not None and client.breaker.state.value == "open"

    # Open: the expired cache is served without asking the issuer.
    client._fetched_at -= 7200
    assert await client.key_for("k1") == {"kid": "k1", "kty": "oct"}
    assert issuer.calls == 4
    healthy, detail = await client.health()
    assert not healthy and "breaker open" in detail

    # Recovery: after the cool-down one probe goes through and closes it.
    issuer.mode = "ok"
    await asyncio.sleep(0.25)
    client._fetched_at -= 7200
    await client.key_for("k1")
    assert issuer.calls == 5
    assert client.breaker.state.value == "closed"
    assert (await client.health())[0]


async def test_jwks_waiters_share_a_failed_fetch() -> None:
    issuer = Issuer()
    issuer.mode = "down"
    client = _jwks(issuer, attempts=1, breaker_failures=0)
    results = await asyncio.gather(
        *(client.key_for("k1") for _ in range(20)), return_exceptions=True
    )
    assert all(isinstance(result, JWKSError) for result in results)
    # One request to an issuer that is down, not twenty.
    assert issuer.calls == 1


# -- S3 ----------------------------------------------------------------------


class _DeadS3:
    def __init__(self) -> None:
        self.calls = 0

    def put_object(self, **kwargs: Any) -> None:
        from botocore.exceptions import EndpointConnectionError

        self.calls += 1
        raise EndpointConnectionError(endpoint_url="http://minio:9000")

    def get_object(self, **kwargs: Any) -> None:
        from botocore.exceptions import ClientError

        self.calls += 1
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    class exceptions:  # mirrors boto3 client.exceptions
        class NoSuchKey(Exception):
            pass


async def test_s3_breaker_opens_on_connection_errors_and_not_on_404() -> None:
    from botocore.exceptions import ClientError

    from jfastframework.http.errors import CircuitOpenError
    from jfastframework.storage.s3 import S3Storage

    disk = S3Storage("files", bucket="b", breaker_failures=2)
    fake = _DeadS3()
    disk._client = fake

    for _ in range(3):
        with pytest.raises(ClientError):
            await disk.get("missing.txt")
    assert disk.breaker is not None and disk.breaker.state.value == "closed"

    for _ in range(2):
        with pytest.raises(Exception, match="Could not connect"):
            await disk.write("a.txt", b"x")
    calls = fake.calls
    with pytest.raises(CircuitOpenError):
        await disk.write("a.txt", b"x")
    assert fake.calls == calls
    healthy, detail = await disk.health()
    assert not healthy and "breaker open" in detail


# -- mail --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (smtplib.SMTPRecipientsRefused({"a@x": (550, b"no such user")}), True),
        (smtplib.SMTPRecipientsRefused({"a@x": (450, b"greylisted")}), False),
        (smtplib.SMTPDataError(554, b"rejected as spam"), True),
        (smtplib.SMTPDataError(451, b"try later"), False),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), True),
        (smtplib.SMTPServerDisconnected("dropped"), False),
        (TimeoutError("timed out"), False),
        (ConnectionRefusedError(), False),
        (ValueError("bad header"), True),
    ],
)
def test_mail_failures_are_classified(error: Exception, permanent: bool) -> None:
    assert is_permanent(error) is permanent


async def _run_mail_job(error: Exception) -> Job:
    app = build_test_app()
    ctx = app.state.jfast
    registry = TaskRegistry()
    ctx.provide("tasks", registry)
    plugin = MailPlugin({"backend": "memory", "queued": False})
    plugin.register(ctx)
    assert plugin._mailer is not None

    async def fail(message: Any) -> None:
        raise error

    plugin._mailer.backend.send = fail  # type: ignore[method-assign]
    handler = registry.get("jfast.mail.send")
    job = Job(task="jfast.mail.send", payload={}, attempts=1, max_attempts=3)
    token = _current_job.set(job)
    try:
        from jfastframework.mail.message import EmailMessage

        message = EmailMessage(to=["a@example.com"], subject="s", text="t")
        with pytest.raises(type(error)):
            await handler({"message": message.to_json()})
    finally:
        _current_job.reset(token)
    return job


async def test_a_permanent_smtp_failure_is_dead_lettered_on_the_first_attempt() -> None:
    job = await _run_mail_job(smtplib.SMTPRecipientsRefused({"a@x": (550, b"no user")}))
    assert job.exhausted


async def test_a_transient_smtp_failure_keeps_its_retries() -> None:
    job = await _run_mail_job(smtplib.SMTPServerDisconnected("dropped"))
    assert not job.exhausted


# -- auth revocation store ---------------------------------------------------


async def _auth_app(**auth: Any) -> tuple[Any, str]:
    router = APIRouter()

    @router.get("/private")
    async def private(caller: Principal = Depends(require_auth)) -> dict[str, str]:
        return {"subject": caller.subject}

    app = build_test_app(
        plugins=["auth"],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issue_tokens": True,
                    **auth,
                }
            }
        },
    )
    plugin: AuthPlugin = app.state.jfast.require("auth")
    assert plugin._issuer is not None
    pair = await plugin._issuer.issue_pair("user-1")

    async def down(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("redis is down")

    assert plugin._store is not None
    plugin._store.is_revoked = down  # type: ignore[method-assign]
    return app, pair.access_token


async def test_revocation_store_down_fails_open_by_default() -> None:
    app, token = await _auth_app()
    async with client_for(app) as http:
        response = await http.get("/private", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


async def test_revocation_store_down_answers_503_when_closed() -> None:
    app, token = await _auth_app(revocation_fail_open=False)
    async with client_for(app) as http:
        response = await http.get("/private", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json()["title"] == "Revocation Store Unavailable"


def test_plugin_error_is_what_boot_raises() -> None:
    # The validations above raise PluginError, which `create_app` lets escape:
    # the process exits before it binds a port.
    with pytest.raises(PluginError):
        build_test_app(plugins=["cache"], raw={"plugin": {"cache": {"url": "http://x"}}})


async def test_repeated_connect_failures_open_the_database_breaker() -> None:
    app = _db_app(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port()}/db")
    async with client_for(app) as http:
        for _ in range(2):
            assert (await http.get("/count")).status_code == 503
        started = time.monotonic()
        response = await http.get("/count")
    assert response.status_code == 503
    assert "last connection attempts" in response.json()["detail"]
    assert time.monotonic() - started < 0.5
