"""A subdomain, path or header names a tenant; with `auth` on it never grants one.

Found building a multi-company help desk on 0.1.0a12 (F1): with the default
`sources = ["token", "subdomain"]`, `current_tenant` returned the subdomain's
tenant with nobody signed in, so

    curl -H "Host: acme.localhost" localhost:8700/tickets

listed and created acme's rows. The `Host` header is the client's to choose;
no DNS is involved. Every row of the rule in `plugins/builtin/tenancy.py` is
exercised here over HTTP:

| request                                   | granted | current_tenant |
| ----------------------------------------- | ------- | -------------- |
| no session, subdomain/path/header         | none    | 401            |
| claim a, no unsigned source or a          | a       | a              |
| claim a, unsigned source b                | none    | 403            |
| no claim, unsigned source b               | none    | 403            |
| no claim, b, trust_unscoped_principals    | b       | b              |
| no `auth` plugin at all, subdomain b      | b       | b (public site) |
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from jfastframework.plugins.builtin.auth import AuthPlugin
from jfastframework.plugins.builtin.database import TenantSession
from jfastframework.plugins.builtin.observability import current_tenant_id
from jfastframework.plugins.builtin.tenancy import (
    TenancyPlugin,
    current_tenant,
    requested_tenant,
)
from jfastframework.testing import build_test_app, client_for

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
AUTH = {
    "mode": "secret",
    "secret": SECRET,
    "algorithms": ["HS256"],
    "issuer": "https://id.example.com/",
    "audience": "mesa",
    "mount_router": False,
}
# What the help desk had: the documented example plus the path source.
SOURCES = ["token", "subdomain", "path"]


def tickets_router() -> APIRouter:
    """What `jfast new module ticket` generates, reduced to its tenant handling."""
    router = APIRouter()
    rows: list[dict[str, Any]] = []

    @router.get("/tickets")
    async def listing(tenant: str = Depends(current_tenant)) -> list[dict[str, Any]]:
        return [row for row in rows if row["tenant_id"] == tenant]

    @router.post("/tickets", status_code=201)
    async def create(request: Request, tenant: str = Depends(current_tenant)) -> dict[str, Any]:
        row = {"id": len(rows) + 1, "tenant_id": tenant, **(await request.json())}
        rows.append(row)
        return row

    @router.get("/context")
    async def context(request: Request) -> dict[str, Any]:
        # What a route that skips current_tenant sees -- and what the RLS
        # session, jobs and events read: the context variable.
        return {
            "state": request.state.tenant_id,
            "context": current_tenant_id(),
            "requested": request.state.tenant_requested,
        }

    @router.get("/branding")
    async def branding(tenant: str = Depends(requested_tenant)) -> dict[str, str]:
        return {"tenant": tenant}

    @router.get("/per-tenant-db")
    async def per_tenant_db(session: TenantSession) -> dict[str, bool]:
        return {"opened": session is not None}

    return router


def app_with(*, auth: bool = True, **tenancy: Any) -> Any:
    config: dict[str, Any] = {"sources": SOURCES, "base_domain": "localhost", **tenancy}
    plugins = ["observability", "auth", "tenancy"] if auth else ["observability", "tenancy"]
    extra = [AuthPlugin, TenancyPlugin] if auth else [TenancyPlugin]
    raw: dict[str, Any] = {"plugin": {"tenancy": config}}
    if auth:
        raw["plugin"]["auth"] = AUTH
    return build_test_app(plugins=plugins, extra_plugins=extra, routers=[tickets_router()], raw=raw)


def bearer(tenant: str | None, subject: str = "user-1") -> dict[str, str]:
    from jfastframework.auth.tokens import issue

    token, _jti, _expires = issue(
        subject,
        key=SECRET,
        algorithm="HS256",
        lifetime=timedelta(minutes=5),
        audience="mesa",
        issuer="https://id.example.com/",
        tenant_id=tenant,
    )
    return {"Authorization": f"Bearer {token}"}


ACME = {"host": "acme.localhost:8700"}
GLOBEX = {"host": "globex.localhost:8700"}
BARE = {"host": "localhost:8700"}


# -- row 1: no session ----------------------------------------------------


async def test_the_curl_from_the_log_is_a_401() -> None:
    """`curl -H "Host: acme.localhost" localhost:8700/tickets`, and its POST."""
    app = app_with()
    async with client_for(app) as client:
        assert (await client.get("/tickets", headers=BARE)).status_code == 401
        listed = await client.get("/tickets", headers=ACME)
        created = await client.post("/tickets", headers=ACME, json={"subject": "hola"})
        assert listed.status_code == 401
        assert created.status_code == 401
        assert created.json()["detail"] == "authentication required"
        # Nothing was written: the owner of acme sees an empty list.
        mine = await client.get("/tickets", headers={**ACME, **bearer("acme")})
        assert mine.json() == []


async def test_no_session_on_a_subdomain_acts_in_no_tenant() -> None:
    app = app_with()
    async with client_for(app) as client:
        response = await client.get("/context", headers=ACME)
    # Named, not granted: the RLS session and every job see no tenant.
    assert response.json() == {"state": None, "context": None, "requested": "acme"}


async def test_no_session_on_a_path_or_a_header_is_a_401() -> None:
    app = app_with(sources=["token", "path", "header"])
    async with client_for(app) as client:
        by_path = await client.get("/t/acme/tickets", headers=BARE)
        by_header = await client.get("/tickets", headers={**BARE, "X-Tenant-ID": "acme"})
    # /t/acme/tickets is not a mounted route (404); what matters is that the
    # header, on a route that exists, does not become a tenant.
    assert by_path.status_code in (401, 404)
    assert by_header.status_code == 401


async def test_the_per_tenant_database_is_not_opened_without_a_session() -> None:
    app = app_with()
    async with client_for(app) as client:
        response = await client.get("/per-tenant-db", headers=ACME)
    assert response.status_code == 401


# -- row 2: the token's tenant, agreed with ------------------------------


async def test_a_token_on_its_own_subdomain_is_served() -> None:
    app = app_with()
    async with client_for(app) as client:
        created = await client.post(
            "/tickets", headers={**ACME, **bearer("acme")}, json={"subject": "hola"}
        )
        listed = await client.get("/tickets", headers={**ACME, **bearer("acme")})
        context = await client.get("/context", headers={**ACME, **bearer("acme")})
    assert created.status_code == 201
    assert [row["tenant_id"] for row in listed.json()] == ["acme"]
    assert context.json() == {"state": "acme", "context": "acme", "requested": "acme"}


async def test_a_token_on_the_bare_host_is_served() -> None:
    app = app_with()
    async with client_for(app) as client:
        response = await client.get("/tickets", headers={**BARE, **bearer("acme")})
    assert response.status_code == 200


# -- row 3: the token's tenant, contradicted -----------------------------


async def test_a_token_of_acme_on_globex_is_a_403() -> None:
    app = app_with()
    async with client_for(app) as client:
        await client.post("/tickets", headers={**GLOBEX, **bearer("globex")}, json={"s": "x"})
        listed = await client.get("/tickets", headers={**GLOBEX, **bearer("acme")})
        created = await client.post(
            "/tickets", headers={**GLOBEX, **bearer("acme")}, json={"s": "y"}
        )
        context = await client.get("/context", headers={**GLOBEX, **bearer("acme")})
        per_tenant = await client.get("/per-tenant-db", headers={**GLOBEX, **bearer("acme")})
    assert listed.status_code == 403
    assert created.status_code == 403
    assert "'acme'" in listed.json()["detail"] and "'globex'" in listed.json()["detail"]
    # Not served as either tenant.
    assert context.json() == {"state": None, "context": None, "requested": "globex"}
    assert per_tenant.status_code == 403


async def test_order_in_sources_does_not_let_the_subdomain_outrank_the_token() -> None:
    app = app_with(sources=["subdomain", "token"])
    async with client_for(app) as client:
        wrong = await client.get("/tickets", headers={**GLOBEX, **bearer("acme")})
        right = await client.get("/tickets", headers={**ACME, **bearer("acme")})
    assert wrong.status_code == 403
    assert right.status_code == 200


# -- row 4: a token with no tenant ---------------------------------------


async def test_an_unscoped_token_on_a_subdomain_is_a_403() -> None:
    """The log's "worse" case: signed in, no claim, someone else's host."""
    app = app_with()
    async with client_for(app) as client:
        listed = await client.get("/tickets", headers={**GLOBEX, **bearer(None)})
        created = await client.post("/tickets", headers={**GLOBEX, **bearer(None)}, json={})
    assert listed.status_code == 403
    assert created.status_code == 403
    assert "not scoped to tenant 'globex'" in listed.json()["detail"]
    assert "trust_unscoped_principals" in listed.json()["detail"]


async def test_an_unscoped_token_without_a_subdomain_is_still_the_old_403() -> None:
    app = app_with()
    async with client_for(app) as client:
        response = await client.get("/tickets", headers={**BARE, **bearer(None)})
    assert response.status_code == 403
    assert "not scoped to a tenant" in response.json()["detail"]


async def test_the_user_source_is_a_signed_tenant_the_subdomain_must_match() -> None:
    app = app_with(sources=["token", "user", "subdomain"])
    async with client_for(app) as client:
        own = await client.get("/tickets", headers={**BARE, **bearer(None, subject="u-7")})
        other = await client.get("/tickets", headers={**ACME, **bearer(None, subject="u-7")})
    assert own.status_code == 200
    assert other.status_code == 403


# -- row 5: the opt-in ----------------------------------------------------


async def test_trust_unscoped_principals_lets_a_signed_in_user_into_the_named_tenant() -> None:
    app = app_with(trust_unscoped_principals=True)
    async with client_for(app) as client:
        listed = await client.get("/tickets", headers={**GLOBEX, **bearer(None)})
        context = await client.get("/context", headers={**GLOBEX, **bearer(None)})
        anonymous = await client.get("/tickets", headers=GLOBEX)
        contradicted = await client.get("/tickets", headers={**GLOBEX, **bearer("acme")})
    assert listed.status_code == 200
    assert context.json()["context"] == "globex"
    # The opt-in is about tokens without a tenant, nothing else.
    assert anonymous.status_code == 401
    assert contradicted.status_code == 403


# -- row 6: no auth plugin -------------------------------------------------


async def test_without_auth_a_public_site_keeps_its_subdomain_tenant() -> None:
    app = app_with(auth=False, sources=["subdomain"])
    async with client_for(app) as client:
        created = await client.post("/tickets", headers=ACME, json={"subject": "hola"})
        listed = await client.get("/tickets", headers=ACME)
        context = await client.get("/context", headers=ACME)
    assert created.status_code == 201
    assert [row["tenant_id"] for row in listed.json()] == ["acme"]
    assert context.json() == {"state": "acme", "context": "acme", "requested": "acme"}


# -- what still reads the named tenant -------------------------------------


async def test_requested_tenant_is_the_named_one_for_public_pages() -> None:
    app = app_with()
    async with client_for(app) as client:
        named = await client.get("/branding", headers=ACME)
        unnamed = await client.get("/branding", headers=BARE)
    assert named.json() == {"tenant": "acme"}
    assert unnamed.status_code == 404


async def test_require_tenant_still_lets_the_sign_in_page_through() -> None:
    app = app_with(require_tenant=True)
    async with client_for(app) as client:
        named = await client.get("/branding", headers=ACME)
        unnamed = await client.get("/branding", headers=BARE)
    assert named.status_code == 200
    assert unnamed.status_code == 403


async def test_accounts_sign_in_reads_the_subdomain_and_the_session_is_then_scoped(
    tmp_path: Path,
) -> None:
    """The legitimate use: log in on acme.localhost, act in acme and nowhere else."""
    from jfastframework.accounts.service import AccountsService
    from jfastframework.plugins.builtin.accounts import AccountsPlugin

    app = build_test_app(
        plugins=["observability", "database", "auth", "accounts", "tenancy"],
        extra_plugins=[AuthPlugin, AccountsPlugin, TenancyPlugin],
        routers=[tickets_router()],
        raw={
            "plugin": {
                "database": {"dsn": f"sqlite+aiosqlite:///{tmp_path / 'mesa.db'}"},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issue_tokens": True,
                },
                "accounts": {},
                "tenancy": {"sources": SOURCES, "base_domain": "localhost"},
            }
        },
    )
    async with app.router.lifespan_context(app):
        plugin: AccountsPlugin = app.state.jfast.require("accounts", AccountsPlugin)
        async with plugin._sessionmaker() as session, session.begin():
            service: AccountsService = plugin.service(session)
            await service.create_user(
                "agente@acme.test", password="a long enough password", tenant_id="acme"
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"email": "agente@acme.test", "password": "a long enough password"}
            wrong_tenant = await client.post("/auth/login", headers=GLOBEX, json=body)
            login = await client.post("/auth/login", headers=ACME, json=body)
            assert wrong_tenant.status_code == 401
            assert login.status_code == 200, login.text
            session_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            own = await client.get("/tickets", headers={**ACME, **session_headers})
            other = await client.get("/tickets", headers={**GLOBEX, **session_headers})
            anonymous = await client.get("/tickets", headers=ACME)
    assert own.status_code == 200
    assert other.status_code == 403
    assert anonymous.status_code == 401
