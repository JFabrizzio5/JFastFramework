"""Guards for the two costs the 0.1.0a10 benchmark found, so they do not return.

Measured with ab on one uvicorn worker: every ``BaseHTTPMiddleware`` cost
about 75 us of CPU per request, and every plain ``def`` dependency about
80 us more, because FastAPI runs it in the threadpool. Together they made a
service with auth and tenancy serve 2,400 requests a second where the same
FastAPI app written by hand served 9,500. Neither shows up in a functional
test, so these tests check the shape instead.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import APIRouter

from jfastframework.plugins.builtin import auth as auth_plugin
from jfastframework.plugins.builtin import tenancy as tenancy_plugin
from jfastframework.testing import build_test_app, client_for

SRC = Path(__file__).resolve().parents[1] / "src" / "jfastframework"


def test_no_middleware_is_built_on_base_http_middleware() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if name == "BaseHTTPMiddleware":
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {node.name}")
    assert offenders == [], f"write these as plain ASGI (see middleware.py): {offenders}"


@pytest.mark.parametrize(
    "dependency",
    [
        auth_plugin.require_auth,
        auth_plugin.optional_auth,
        auth_plugin.require_scopes("a"),
        auth_plugin.require_roles("r"),
        tenancy_plugin.current_tenant,
        tenancy_plugin.tenant_zone,
    ],
    ids=lambda d: getattr(d, "__qualname__", repr(d)),
)
def test_framework_dependencies_do_not_hop_to_the_threadpool(dependency: object) -> None:
    assert inspect.iscoroutinefunction(dependency)


@pytest.mark.parametrize("layout", ["modular", "layered", "screaming", "hexagonal"])
def test_generated_service_factories_are_async(layout: str) -> None:
    templates = SRC / "templates" / f"module_{layout}"
    factories = [
        line
        for path in templates.rglob("*.j2")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith(("def get_service(", "def get_use_cases(", "async def get_"))
    ]
    assert factories, f"no service factory found in the {layout} templates"
    assert all(line.lstrip().startswith("async def") for line in factories), factories


async def test_metrics_label_the_route_template_not_the_path() -> None:
    """One series per route. Through 0.1.0a9 it was one series per id."""
    router = APIRouter()

    @router.get("/users/{user_id}")
    async def user(user_id: int) -> dict[str, int]:
        return {"id": user_id}

    app = build_test_app(routers=[router], plugins=["metrics"], disabled=["observability"])
    async with client_for(app) as client:
        for user_id in (41, 42, 43):
            await client.get(f"/users/{user_id}")
        await client.get("/no/such/path/probe-123")
        text = (await client.get("/metrics")).text

    series = [line for line in text.splitlines() if line.startswith("http_requests_total{")]
    assert any('endpoint="/users/{user_id}"' in s and "} 3.0" in s for s in series), series
    assert not any("/users/41" in s for s in series)
    # A scanner's random paths share one label instead of minting their own.
    assert any('endpoint="<unmatched>"' in s for s in series)
    assert not any("probe-123" in s for s in series)


async def test_the_request_id_still_reaches_the_response() -> None:
    router = APIRouter()

    @router.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app = build_test_app(routers=[router], plugins=["observability"])
    async with client_for(app) as client:
        echoed = await client.get("/ping", headers={"X-Request-ID": "abc123"})
        fresh = await client.get("/ping")
    assert echoed.headers["x-request-id"] == "abc123"
    assert len(fresh.headers["x-request-id"]) == 32


async def test_a_mounted_route_keeps_its_prefix_in_the_label() -> None:
    from fastapi import FastAPI

    inner = FastAPI()

    @inner.get("/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    app = build_test_app(plugins=["metrics"], disabled=["observability"])
    app.mount("/auth", inner)
    async with client_for(app) as client:
        await client.get("/auth/login")
        text = (await client.get("/metrics")).text
    assert 'endpoint="/auth/login"' in text


async def test_an_included_router_prefix_is_kept_in_the_label() -> None:
    """FastAPI 0.121+ includes a router lazily; its routes' paths lack the prefix."""
    inner = APIRouter()

    @inner.get("/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    app = build_test_app(plugins=["metrics"], disabled=["observability"])
    app.include_router(inner, prefix="/auth")
    async with client_for(app) as client:
        await client.get("/auth/login")
        text = (await client.get("/metrics")).text
    assert 'endpoint="/auth/login"' in text, [
        line for line in text.splitlines() if line.startswith("http_requests_total{")
    ]
