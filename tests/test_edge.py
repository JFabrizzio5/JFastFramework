"""Edge protections: body limits, request timeouts, CORS, trusted hosts, docs.

Everything here is off unless configured, so the first assertion in most of
these is that the default behaviour did not change.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from jfastframework.middleware import BodyTooLarge
from jfastframework.settings import JFastSettings
from jfastframework.testing import build_test_app, client_for

router = APIRouter()


@router.get("/quick")
async def quick() -> dict[str, str]:
    return {"ok": "yes"}


@router.get("/slow")
async def slow() -> dict[str, str]:
    await asyncio.sleep(5)
    return {"ok": "eventually"}


@router.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(await request.body())}


@router.post("/answer-then-read")
async def answer_then_read(request: Request) -> StreamingResponse:
    """A handler that commits to a status before it has seen the whole body.

    Rare, and the only shape in which the limit can be exceeded once there is
    no longer a status code left to change.
    """

    async def stream():  # type: ignore[no-untyped-def]
        yield b"begin"
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        yield str(total).encode()

    return StreamingResponse(stream(), media_type="text/plain")


def _app(**overrides: object):  # type: ignore[no-untyped-def]
    return build_test_app(plugins=[], routers=[router], **overrides)


# -- defaults ----------------------------------------------------------


async def test_nothing_is_applied_by_default() -> None:
    async with client_for(_app()) as client:
        assert (await client.get("/quick")).status_code == 200
        assert (await client.post("/echo", content=b"x" * 10_000)).json() == {"size": 10_000}


# -- body size ---------------------------------------------------------


async def test_a_declared_body_over_the_limit_is_refused() -> None:
    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Payload Too Large"


async def test_a_body_under_the_limit_passes() -> None:
    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"size": 100}


async def test_a_streamed_body_over_the_limit_is_refused() -> None:
    """No Content-Length to check, so the bytes are counted as they arrive."""

    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(4):
            yield b"x" * 512

    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=chunks())
    assert response.status_code == 413


async def _post_streaming_body(spec_version: str | None) -> BaseException | None:
    """POST a 2 KB body to /answer-then-read with a 1 KB limit; return what failed."""
    import httpx

    app = _app(max_body_bytes=1024)

    async def declaring(scope, receive, send):  # type: ignore[no-untyped-def]
        if spec_version is not None and scope["type"] == "http":
            scope = {**scope, "asgi": {"version": "3.0", "spec_version": spec_version}}
        await app(scope, receive, send)

    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(4):
            yield b"x" * 512

    transport = httpx.ASGITransport(app=declaring)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        try:
            response = await client.post("/answer-then-read", content=chunks())
        except Exception as exc:  # noqa: BLE001 - what failed is the result
            return exc
    raise AssertionError(f"the response completed: {response.status_code} {response.text!r}")


async def test_a_response_already_started_fails_rather_than_truncating() -> None:
    """The one case where 413 is no longer available.

    Dropping the remaining chunks was the previous behaviour, and it produced
    a 200 that looked complete and was computed from a truncated request --
    the outcome the limit exists to prevent, wearing a success code. There is
    nothing honest left but to fail the connection. ASGI 2.4, so the body is
    read only by the handler.
    """
    failed = await _post_streaming_body("2.4")
    assert isinstance(failed, BodyTooLarge), failed


async def test_under_asgi_2_3_the_response_still_never_completes() -> None:
    """What uvicorn declares today (2.3), and httpx's test transport too.

    Below 2.4 StreamingResponse runs a disconnect listener that reads the
    body alongside the handler, so which exception surfaces first is a race
    -- ClientDisconnect or BodyTooLarge. Through 0.1.0a9 BaseHTTPMiddleware
    held those reads back and hid the race. What must hold either way: no
    complete response computed from half a body.
    """
    assert await _post_streaming_body(None) is not None


# -- request timeout ---------------------------------------------------


async def test_a_request_that_outlives_the_timeout_gets_504() -> None:
    async with client_for(_app(request_timeout=0.05)) as client:
        response = await client.get("/slow")
    assert response.status_code == 504
    assert response.json()["title"] == "Gateway Timeout"


async def test_a_fast_request_is_untouched() -> None:
    async with client_for(_app(request_timeout=1.0)) as client:
        response = await client.get("/quick")
    assert response.status_code == 200


# -- CORS and hosts ----------------------------------------------------


async def test_cors_headers_when_origins_are_configured() -> None:
    app = _app(cors_origins=["https://app.example.com"])
    async with client_for(app) as client:
        response = await client.get("/quick", headers={"Origin": "https://app.example.com"})
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"


async def test_no_cors_headers_when_not_configured() -> None:
    async with client_for(_app()) as client:
        response = await client.get("/quick", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_wildcard_origins_with_credentials_is_refused_at_boot() -> None:
    """Browsers reject the pair, so failing here beats failing in a console."""
    with pytest.raises(ValidationError, match="cors_origins cannot be"):
        JFastSettings(cors_origins=["*"], cors_allow_credentials=True, _env_file=None)  # type: ignore[call-arg]


async def test_an_untrusted_host_is_rejected() -> None:
    app = _app(trusted_hosts=["api.example.com"])
    async with client_for(app) as client:
        allowed = await client.get("/quick", headers={"Host": "api.example.com"})
        refused = await client.get("/quick", headers={"Host": "evil.example.com"})
    assert allowed.status_code == 200
    assert refused.status_code == 400


# -- docs in production ------------------------------------------------


def test_docs_are_closed_in_production_by_default() -> None:
    settings = JFastSettings(env="prod", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url is None
    assert settings.effective_openapi_url is None


def test_docs_stay_open_when_asked_for_explicitly() -> None:
    settings = JFastSettings(env="prod", docs_url="/internal/docs", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url == "/internal/docs"
    # Still closed: each one is its own decision.
    assert settings.effective_openapi_url is None


def test_docs_are_open_outside_production() -> None:
    settings = JFastSettings(env="staging", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url == "/docs"


async def test_the_app_serves_no_openapi_in_production() -> None:
    async with client_for(_app(env="prod")) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        # /info closes in production too; they are now one rule.
        assert (await client.get("/info")).status_code == 404
