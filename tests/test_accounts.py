"""The accounts plugin, through HTTP: login, lockout, roles, refresh, tenants."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.accounts import require_permission
from jfastframework.accounts.models import users
from jfastframework.auth.principal import Principal
from jfastframework.errors import PluginError
from jfastframework.testing import build_test_app

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ADMIN = ("admin@example.com", "correct horse battery")


def _guarded() -> APIRouter:
    router = APIRouter()

    @router.post("/invoices")
    async def create(
        caller: Principal = Depends(require_permission("invoices:write")),
    ) -> dict[str, str]:
        return {"by": caller.subject}

    return router


def _app(dsn: str, **accounts: Any) -> Any:
    config = {
        "bootstrap_admin_email": ADMIN[0],
        "bootstrap_admin_password": ADMIN[1],
        "max_failed_logins": 3,
        **accounts,
    }
    app = build_test_app(
        plugins=["database", "auth", "accounts"],
        routers=[_guarded()],
        raw={
            "plugin": {
                "database": {"dsn": dsn},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issue_tokens": True,
                },
                "accounts": config,
            }
        },
    )

    @app.middleware("http")
    async def tenant_from_header(request: Request, call_next: Any) -> Any:
        request.state.tenant_id = request.headers.get("x-tenant")
        return await call_next(request)

    return app


class _Running:
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


PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")


@pytest.fixture(params=["sqlite", "postgresql"])
async def dsn(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """Every test on both backends: the unique constraints and the lockout's
    timestamps are exactly where SQLite and PostgreSQL disagree."""
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}"
    dsn = f"{PG_BASE}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DROP TABLE IF EXISTS jfast_user_roles, jfast_role_permissions, "
                    "jfast_roles, jfast_users"
                )
            )
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await engine.dispose()
    return dsn


async def _login(client: httpx.AsyncClient, email: str, password: str, **headers: str) -> Any:
    return await client.post(
        "/auth/login", json={"email": email, "password": password}, headers=headers
    )


def _bearer(pair: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair['access_token']}"}


async def test_the_bootstrap_admin_signs_in_with_its_permission(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        response = await _login(client, *ADMIN)
        assert response.status_code == 200
        me = (await client.get("/auth/account", headers=_bearer(response.json()))).json()
    assert me["email"] == ADMIN[0]
    assert (me["roles"], me["permissions"]) == (["admin"], ["accounts:admin"])


async def test_a_wrong_password_and_an_unknown_email_look_the_same(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        wrong = await _login(client, ADMIN[0], "not the password")
        unknown = await _login(client, "nobody@example.com", "whatever at all")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_repeated_failures_lock_the_account_even_for_the_right_password(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        for _ in range(3):
            assert (await _login(client, ADMIN[0], "guess guess guess")).status_code == 401
        # Locked: the right password is refused too, with the same answer.
        assert (await _login(client, *ADMIN)).status_code == 401

        engine = create_async_engine(dsn)
        async with engine.begin() as conn:
            await conn.execute(update(users).values(locked_until=None))
        await engine.dispose()
        assert (await _login(client, *ADMIN)).status_code == 200


async def test_roles_become_permissions_in_the_token(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        admin = _bearer((await _login(client, *ADMIN)).json())
        role = await client.post(
            "/accounts/roles",
            json={"name": "billing", "permissions": ["invoices:write", "invoices:read"]},
            headers=admin,
        )
        assert role.status_code == 201
        for email, roles in (("clerk@example.com", ["billing"]), ("viewer@example.com", [])):
            created = await client.post(
                "/accounts/users",
                json={"email": email, "password": "a long enough password", "roles": roles},
                headers=admin,
            )
            assert created.status_code == 201

        clerk = _bearer(
            (await _login(client, "clerk@example.com", "a long enough password")).json()
        )
        viewer = _bearer(
            (await _login(client, "Viewer@Example.com", "a long enough password")).json()
        )
        assert (await client.post("/invoices", headers=clerk)).status_code == 200
        assert (await client.post("/invoices", headers=viewer)).status_code == 403
        # And only an administrator reaches the administration.
        assert (await client.get("/accounts/users", headers=clerk)).status_code == 403


async def test_a_refresh_rereads_permissions_and_ends_a_deactivated_session(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        admin = _bearer((await _login(client, *ADMIN)).json())
        role = (
            await client.post(
                "/accounts/roles", json={"name": "billing", "permissions": []}, headers=admin
            )
        ).json()
        user = (
            await client.post(
                "/accounts/users",
                json={
                    "email": "c@example.com",
                    "password": "a long enough password",
                    "roles": ["billing"],
                },
                headers=admin,
            )
        ).json()
        pair = (await _login(client, "c@example.com", "a long enough password")).json()
        assert (await client.post("/invoices", headers=_bearer(pair))).status_code == 403

        # Granted now; the next refresh carries it.
        await client.patch(
            f"/accounts/roles/{role['id']}", json={"permissions": ["invoices:write"]}, headers=admin
        )
        pair = (
            await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        ).json()
        assert (await client.post("/invoices", headers=_bearer(pair))).status_code == 200

        # Deactivated: the next refresh ends the session.
        await client.patch(
            f"/accounts/users/{user['id']}", json={"is_active": False}, headers=admin
        )
        refused = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert refused.status_code == 401
        assert (await _login(client, "c@example.com", "a long enough password")).status_code == 401


async def test_changing_a_password_needs_the_current_one(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        me = _bearer((await _login(client, *ADMIN)).json())
        wrong = await client.post(
            "/auth/password",
            json={"current_password": "nope", "new_password": "a brand new password"},
            headers=me,
        )
        short = await client.post(
            "/auth/password",
            json={"current_password": ADMIN[1], "new_password": "short"},
            headers=me,
        )
        ok = await client.post(
            "/auth/password",
            json={"current_password": ADMIN[1], "new_password": "a brand new password"},
            headers=me,
        )
        assert (wrong.status_code, short.status_code, ok.status_code) == (422, 422, 204)
        assert (await _login(client, *ADMIN)).status_code == 401
        assert (await _login(client, ADMIN[0], "a brand new password")).status_code == 200


async def test_registration_is_off_unless_asked_for(dsn: str) -> None:
    body = {"email": "new@example.com", "password": "a long enough password"}
    async with _Running(_app(dsn)) as client:
        assert (await client.post("/auth/register", json=body)).status_code == 404


async def test_registration_gives_the_default_roles(dsn: str) -> None:
    body = {"email": "new@example.com", "password": "a long enough password"}
    async with _Running(_app(dsn, allow_registration=True, default_roles=["admin"])) as client:
        created = await client.post("/auth/register", json=body)
        again = await client.post("/auth/register", json=body)
        me = (await client.get("/auth/account", headers=_bearer(created.json()))).json()
    assert (created.status_code, again.status_code) == (201, 409)
    assert me["roles"] == ["admin"]


async def test_tenants_do_not_see_each_others_users(dsn: str) -> None:
    app = _app(dsn)
    async with _Running(app) as client:
        plugin = app.state.jfast.require("accounts")
        maker = app.state.jfast.require("db.sessionmaker")
        async with maker() as session, session.begin():
            service = plugin.service(session)
            for tenant in ("acme", "globex"):
                await service.ensure_role("admin", tenant_id=tenant, permissions=["accounts:admin"])
                await service.create_user(
                    "boss@example.com",
                    password="a long enough password",
                    tenant_id=tenant,
                    role_names=["admin"],
                )
            await service.create_user(
                "worker@example.com", password="a long enough password", tenant_id="acme"
            )

        acme = _bearer(
            (
                await _login(
                    client, "boss@example.com", "a long enough password", **{"x-tenant": "acme"}
                )
            ).json()
        )
        listed = (await client.get("/accounts/users", headers=acme)).json()
        # Same email in another tenant is another person, and invisible here.
        assert listed["total"] == 2
        assert {u["tenant_id"] for u in listed["items"]} == {"acme"}
        # And without the tenant, that email is not an account at all.
        assert (
            await _login(client, "boss@example.com", "a long enough password")
        ).status_code == 401


def test_accounts_without_token_issuance_refuses_to_start(dsn: str) -> None:
    with pytest.raises(PluginError, match="issue_tokens"):
        build_test_app(
            plugins=["database", "auth", "accounts"],
            raw={
                "plugin": {
                    "database": {"dsn": dsn},
                    "auth": {"mode": "secret", "secret": SECRET, "algorithms": ["HS256"]},
                }
            },
        )
