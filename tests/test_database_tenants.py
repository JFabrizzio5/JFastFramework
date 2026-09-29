"""A database per tenant, and the pool explosion that makes the naive version wrong.

A dict of engines keyed by tenant is the obvious implementation and it takes
PostgreSQL down: 200 tenants at ``pool_size = 10`` is 2000 connections against
a server whose default ceiling is 100. So the map is bounded, the ceiling is a
number you can read off ``jfast describe``, and eviction never closes an engine
a request is still holding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.database import (
    DatabasePlugin,
    TenantEngines,
    TenantPoolExhausted,
    tenant_session_dependency,
)
from jfastframework.plugins.builtin.tenancy import TenancyPlugin
from jfastframework.testing import build_test_app, client_for


class FakeEngine:
    """Stands in for an AsyncEngine so a lease can be observed without a server."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.disposed = False

    async def dispose(self) -> None:
        # A real dispose closes sockets, so it suspends. Without that here the
        # fake hides every ordering bug that only exists around the suspension.
        await asyncio.sleep(0)
        self.disposed = True


def _engines(**overrides: Any) -> TenantEngines:
    created: dict[str, FakeEngine] = {}

    def create(tenant: str, dsn: str) -> FakeEngine:
        created[tenant] = FakeEngine(dsn)
        return created[tenant]

    options: dict[str, Any] = {
        "resolve": lambda tenant: f"postgresql+asyncpg://app:pw@db:5432/{tenant}",
        "create": create,
        "max_engines": 2,
        "pool_size": 2,
        "max_overflow": 2,
    }
    options.update(overrides)
    return TenantEngines(**options)


# -- the map -------------------------------------------------------------


async def test_each_tenant_gets_its_own_engine() -> None:
    engines = _engines()
    async with engines.lease("acme") as acme, engines.lease("globex") as globex:
        assert acme.engine is not globex.engine
        assert acme.engine.dsn.endswith("/acme")


async def test_the_same_tenant_reuses_one_engine() -> None:
    engines = _engines()
    async with engines.lease("acme") as first:
        engine = first.engine
    async with engines.lease("acme") as second:
        assert second.engine is engine
    assert engines.size == 1


# -- the ceiling ---------------------------------------------------------


async def test_the_map_never_grows_past_the_ceiling() -> None:
    engines = _engines(max_engines=2)
    for tenant in ("a", "b", "c", "d"):
        async with engines.lease(tenant):
            pass
    assert engines.size == 2


async def test_the_least_recently_used_engine_is_the_one_evicted() -> None:
    engines = _engines(max_engines=2)
    async with engines.lease("a") as a:
        first = a.engine
    async with engines.lease("b"):
        pass
    # Touching "a" again makes "b" the least recently used.
    async with engines.lease("a"):
        pass
    async with engines.lease("c"):
        pass

    assert engines.tenants == ("a", "c")
    assert first.disposed is False


async def test_the_connection_ceiling_is_a_number_not_a_hope() -> None:
    engines = _engines(max_engines=25, pool_size=2, max_overflow=2)
    # 25 engines x (2 + 2) is the most connections this process can open for
    # tenants, and it is what has to fit under PostgreSQL's max_connections.
    assert engines.max_connections == 100


async def test_a_full_map_of_busy_engines_refuses_rather_than_overflowing() -> None:
    """Exceeding the ceiling under load is exactly the failure being prevented."""
    engines = _engines(max_engines=2)
    async with engines.lease("a"), engines.lease("b"):
        with pytest.raises(TenantPoolExhausted, match="max_engines"):
            async with engines.lease("c"):
                pass


# -- eviction while a request is still holding the engine ----------------


async def test_an_engine_in_use_is_never_evicted_under_pressure() -> None:
    engines = _engines(max_engines=2)
    async with engines.lease("a") as held:
        async with engines.lease("b"):
            pass
        async with engines.lease("c"):
            pass
        assert held.engine.disposed is False
        assert "a" in engines.tenants


async def test_an_explicit_eviction_waits_for_the_request_to_finish() -> None:
    engines = _engines(max_engines=4)
    async with engines.lease("a") as held:
        await engines.evict("a")
        # Out of the map, so nothing new checks it out, but still open for the
        # request that is mid-flight. Disposing here is a closed connection
        # under a running query.
        assert "a" not in engines.tenants
        assert held.engine.disposed is False
    assert held.engine.disposed is True


async def test_two_requests_for_one_new_tenant_build_one_engine() -> None:
    """Eviction suspends, and two coroutines used to race through the gap.

    The loser's engine stayed open with nothing referencing it, which is a leak
    that only shows up as connections the map cannot account for.
    """
    built: list[FakeEngine] = []

    def create(tenant: str, dsn: str) -> FakeEngine:
        engine = FakeEngine(dsn)
        built.append(engine)
        return engine

    engines = _engines(create=create, max_engines=1)
    async with engines.lease("filler"):
        pass

    async def touch() -> Any:
        async with engines.lease("acme") as entry:
            await asyncio.sleep(0)
            return entry.engine

    first, second = await asyncio.gather(touch(), touch())

    assert first is second
    assert [engine for engine in built if engine.dsn.endswith("/acme")] == [first]


async def test_a_tenant_evicted_mid_request_gets_a_fresh_engine_next_time() -> None:
    engines = _engines(max_engines=4)
    async with engines.lease("a") as held:
        await engines.evict("a")
        async with engines.lease("a") as replacement:
            assert replacement.engine is not held.engine


# -- resolution ----------------------------------------------------------


async def test_an_unresolvable_tenant_names_the_settings_that_would_fix_it() -> None:
    def resolve(tenant: str) -> str:
        raise LookupError(tenant)

    engines = _engines(resolve=resolve)
    with pytest.raises(PluginError, match="tenant_dsn_template"):
        async with engines.lease("ghost"):
            pass


# -- the request path ----------------------------------------------------


def _tenant_app(tmp_path: Path, **overrides: Any) -> Any:
    app = build_test_app()
    TenancyPlugin({"sources": ["header"]}).register(app.state.jfast)
    config: dict[str, Any] = {
        "dsn": f"sqlite+aiosqlite:///{tmp_path / 'main.db'}",
        "tenant_dsn_template": f"sqlite+aiosqlite:///{tmp_path}/{{tenant}}.db",
    }
    config.update(overrides)
    DatabasePlugin(config).register(app.state.jfast)

    @app.get("/who")
    async def who(
        session: Any = Depends(tenant_session_dependency, scope="function"),
    ) -> dict[str, str]:
        return {"bind": str(session.get_bind().url)}

    return app


async def test_the_dependency_opens_the_tenants_own_database(tmp_path: Path) -> None:
    # max_engines = 1 forces an eviction between the two requests, which is the
    # state the naive dict never reaches and therefore never gets wrong.
    app = _tenant_app(tmp_path, tenant_max_engines=1)
    async with client_for(app) as client:
        acme = (await client.get("/who", headers={"X-Tenant-ID": "acme"})).json()
        globex = (await client.get("/who", headers={"X-Tenant-ID": "globex"})).json()

    assert acme["bind"].endswith("acme.db")
    assert globex["bind"].endswith("globex.db")


async def test_a_request_with_no_tenant_has_no_database_to_guess_at(tmp_path: Path) -> None:
    app = _tenant_app(tmp_path)
    async with client_for(app) as client:
        answer = await client.get("/who")

    # A configuration error, surfaced as one, rather than a query silently run
    # against whichever database was open.
    assert answer.status_code == 500


async def test_disposing_closes_every_engine() -> None:
    engines = _engines(max_engines=4)
    held: list[FakeEngine] = []
    for tenant in ("a", "b"):
        async with engines.lease(tenant) as entry:
            held.append(entry.engine)
    await engines.dispose()

    assert all(engine.disposed for engine in held)
    assert engines.size == 0


# -- what the numbers mean once the image runs more than one process ----


def test_pool_exhaustion_is_backpressure_not_a_bug(tmp_path) -> None:
    """503, not 500.

    Every engine being busy means the service is healthy and at capacity: the
    same request succeeds a moment later. As a bare RuntimeError it reached the
    unhandled handler and came back `500 "An unexpected error occurred"`, which
    tells a client to stop and a reader to go looking for a defect -- while
    hiding the one signal that says raise tenant_max_engines.
    """
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from jfastframework import create_app

    router = APIRouter()

    @router.get("/at-capacity")
    async def at_capacity() -> None:
        raise TenantPoolExhausted("every tenant engine is serving a request")

    config = tmp_path / "jfast.toml"
    config.write_text(
        '[app]\nname = "t"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability"]\ndisabled = []\n',
        encoding="utf-8",
    )
    app = create_app(config_path=str(config), routers=[router])

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/at-capacity")

    assert response.status_code == 503
    assert response.json()["title"] == "Service Unavailable"


def test_it_is_still_caught_by_code_expecting_a_runtime_error() -> None:
    """The base is kept so an existing `except RuntimeError` around a lease
    does not stop catching it."""
    assert issubclass(TenantPoolExhausted, RuntimeError)
