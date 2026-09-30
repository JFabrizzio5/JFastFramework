"""The framework's fixed routes sit behind the application's, and still win their paths.

Starlette tries routes in order. With ``/health``, ``/ready``, ``/info``,
``/metrics`` and the docs in front, every request to an application route
paid for failing to match each of them first -- about 8 us per request on
0.1.0a10. These tests pin the new order and, more importantly, that it
changes nobody's answer: a catch-all application route still does not get
to answer ``/health``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI
from starlette.routing import Route

from jfastframework.app import order_framework_routes_last
from jfastframework.testing import build_test_app, client_for

FRAMEWORK_PATHS = {"/health", "/ready", "/info", "/metrics", "/openapi.json", "/docs"}


def _paths(app: FastAPI) -> list[str]:
    return [route.path if isinstance(route, Route) else repr(route) for route in app.router.routes]


def _app_router() -> APIRouter:
    router = APIRouter()

    @router.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return router


async def test_application_routes_are_tried_before_the_framework_routes() -> None:
    app = build_test_app(routers=[_app_router()])
    async with client_for(app):
        routes = app.router.routes
        first_framework = min(
            i for i, r in enumerate(routes) if isinstance(r, Route) and r.path in FRAMEWORK_PATHS
        )
    app_positions = [
        i for i, r in enumerate(routes) if getattr(r, "path", None) not in FRAMEWORK_PATHS
    ]
    # The application's router (whatever FastAPI makes of an include) comes first.
    assert app_positions and min(app_positions) < first_framework, _paths(app)
    assert all(
        isinstance(r, Route) and r.path in FRAMEWORK_PATHS | {"/docs/oauth2-redirect", "/redoc"}
        for r in routes[first_framework:]
    ), _paths(app)


async def test_the_framework_routes_still_answer_at_the_same_urls() -> None:
    app = build_test_app(routers=[_app_router()])
    async with client_for(app) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
        info = await client.get("/info")
        metrics = await client.get("/metrics")
        ping = await client.get("/ping")
        schema = (await client.get("/openapi.json")).json()
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert ready.status_code == 200 and ready.json()["status"] == "ok"
    assert info.status_code == 200 and "plugins" in info.json()
    assert metrics.status_code == 200 and "http_requests_total" in metrics.text
    assert ping.json() == {"ok": True}
    # Still documented, still tagged.
    assert schema["paths"]["/health"]["get"]["tags"] == ["system"]
    assert "/ping" in schema["paths"]


async def test_a_catch_all_application_route_does_not_capture_health() -> None:
    router = APIRouter()

    @router.get("/{slug}")
    async def page(slug: str) -> dict[str, str]:
        return {"page": slug}

    app = build_test_app(routers=[router])
    async with client_for(app) as client:
        health = await client.get("/health")
        metrics = await client.get("/metrics")
        page = await client.get("/about")
    assert health.json()["status"] == "ok"
    assert "http_requests_total" in metrics.text
    assert page.json() == {"page": "about"}


async def test_a_partial_match_in_front_keeps_the_framework_405() -> None:
    """``POST /health`` answered 405 ``Allow: GET`` before; it still must.

    A ``PUT /{slug}`` route matches the path of ``/health`` with the wrong
    method. Had ``/health`` moved behind it, the first partial match -- the
    one Starlette answers 405 with -- would be the application's.
    """
    router = APIRouter()

    @router.put("/{slug}")
    async def replace(slug: str) -> dict[str, str]:
        return {"replaced": slug}

    app = build_test_app(routers=[router])
    async with client_for(app) as client:
        response = await client.post("/health")
        replaced = await client.put("/anything")
        paths = _paths(app)
    assert response.status_code == 405
    assert replaced.json() == {"replaced": "anything"}
    # The problem+json handler drops the Allow header (it did before this
    # change too), so which route answered is checked on the table itself:
    # /health went back in front of the route that would otherwise claim it.
    health = paths.index("/health")
    assert any("slug" in p for p in paths[health:]), paths
    # A route nothing claims -- two segments, which /{slug} cannot match --
    # still moved behind the application's.
    assert paths[-1] == "/docs/oauth2-redirect", paths


async def test_a_router_included_after_create_app_is_reordered_too() -> None:
    app = build_test_app()
    app.include_router(_app_router())
    last_before = app.router.routes[-1]
    assert not (isinstance(last_before, Route) and last_before.path in FRAMEWORK_PATHS)

    async with client_for(app) as client:
        assert (await client.get("/ping")).json() == {"ok": True}
        assert (await client.get("/health")).status_code == 200
    last = app.router.routes[-1]
    assert isinstance(last, Route) and last.path in FRAMEWORK_PATHS | {
        "/redoc",
        "/docs/oauth2-redirect",
    }


async def test_routes_with_parameters_are_not_moved() -> None:
    """Nobody can enumerate what ``/files/{key:path}`` matches, so no probe
    can prove moving it is harmless."""
    from jfastframework.plugins.base import Plugin, PluginMeta

    class Files(Plugin):
        meta = PluginMeta(name="files-test", description="test")

        def register(self, ctx: Any) -> None:
            @ctx.app.get("/files/{key:path}")
            async def file(key: str) -> dict[str, str]:
                return {"key": key}

    app = build_test_app(plugins=["files-test"], extra_plugins=[Files], routers=[_app_router()])
    async with client_for(app):
        paths = _paths(app)
    assert paths.index("/files/{key:path}") < paths.index("/health")


# That the ordering runs after startup, and not before ``ratelimit`` installs
# its default limit, is held by tests/test_ratelimit.py: probing an included
# router earlier caches its routes without the limit, and seven of its tests
# fail (seen while writing this).


async def test_ordering_is_idempotent() -> None:
    app = build_test_app(routers=[_app_router()])
    async with client_for(app):
        before = list(app.router.routes)
        order_framework_routes_last(app)
        order_framework_routes_last(app)
        assert app.router.routes == before


def test_an_app_not_built_by_create_app_is_left_alone() -> None:
    app = FastAPI()
    app.include_router(_app_router())
    before = list(app.router.routes)
    order_framework_routes_last(app)
    assert app.router.routes == before
