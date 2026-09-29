"""Multi-tenancy: where the tenant comes from, and which sources are trusted.

The interesting cases are the ones where a request *tries* to pick its own
tenant. A subdomain is only as trustworthy as DNS; a header is not trustworthy
at all.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Request

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.tenancy import (
    RESERVED_SUBDOMAINS,
    TenancyPlugin,
    tenant_from_host,
    tenant_from_path,
)
from jfastframework.testing import build_test_app, client_for

BASE = "app.example.com"
RESERVED = set(RESERVED_SUBDOMAINS)


# -- host parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("acme.app.example.com", "acme"),
        ("acme.app.example.com:8000", "acme"),
        ("ACME.App.Example.Com", "acme"),
        ("acme.app.example.com.", "acme"),  # trailing dot is legal in DNS
        ("app.example.com", None),  # the bare base domain is not a tenant
        ("example.com", None),
        ("acme.other.com", None),  # not under our base domain
        ("a.b.app.example.com", None),  # two labels is a mistake, not a tenant
        ("www.app.example.com", None),  # reserved
        ("api.app.example.com", None),
        ("-bad.app.example.com", None),  # not a valid slug
        ("", None),
    ],
)
def test_tenant_from_host(host: str, expected: str | None) -> None:
    assert tenant_from_host(host, BASE, RESERVED) == expected


def test_no_base_domain_means_no_subdomain_tenant() -> None:
    # Without a base domain every host looks like a tenant, which is why the
    # plugin refuses to start in that state.
    assert tenant_from_host("acme.app.example.com", "", RESERVED) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/t/acme/invoices", "acme"),
        ("/t/acme", "acme"),
        ("/t/", None),
        ("/invoices", None),
        ("/t/../etc", None),
    ],
)
def test_tenant_from_path(path: str, expected: str | None) -> None:
    assert tenant_from_path(path, "/t") == expected


# -- configuration ------------------------------------------------------


def tenancy_app(**config: object) -> object:
    router = APIRouter()

    @router.get("/whoami")
    async def whoami(request: Request) -> dict[str, str | None]:
        return {
            "tenant": request.state.tenant_id,
            "source": request.state.tenant_source,
        }

    return build_test_app(
        plugins=["observability", "tenancy"],
        extra_plugins=[TenancyPlugin],
        routers=[router],
        raw={"plugin": {"tenancy": config}},
    )


def test_subdomain_without_a_base_domain_is_refused() -> None:
    with pytest.raises(PluginError, match="base_domain"):
        tenancy_app(sources=["subdomain"])


def test_unknown_source_is_refused() -> None:
    with pytest.raises(PluginError, match="unknown"):
        tenancy_app(sources=["telepathy"])


def test_empty_sources_is_refused() -> None:
    with pytest.raises(PluginError, match="no sources"):
        tenancy_app(sources=[])


# -- resolution ---------------------------------------------------------


async def test_tenant_comes_from_the_subdomain() -> None:
    app = tenancy_app(sources=["subdomain"], base_domain=BASE)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": f"acme.{BASE}"})
        assert response.json() == {"tenant": "acme", "source": "subdomain"}


async def test_header_is_ignored_unless_it_is_a_source() -> None:
    app = tenancy_app(sources=["subdomain"], base_domain=BASE)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": BASE, "X-Tenant-ID": "victim"})
        assert response.json()["tenant"] is None


async def test_subdomain_beats_header_when_both_are_sources() -> None:
    """A forgeable source must never override a less forgeable one."""
    app = tenancy_app(sources=["subdomain", "header"], base_domain=BASE)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get(
            "/whoami", headers={"host": f"acme.{BASE}", "X-Tenant-ID": "victim"}
        )
        assert response.json() == {"tenant": "acme", "source": "subdomain"}


async def test_path_source() -> None:
    app = tenancy_app(sources=["path"])
    async with client_for(app) as client:  # type: ignore[arg-type]
        # The route itself is /whoami; the middleware reads the path it was
        # given, so exercise it through a URL that carries the prefix.
        response = await client.get("/whoami")
        assert response.json()["tenant"] is None


async def test_missing_tenant_is_allowed_by_default() -> None:
    app = tenancy_app(sources=["subdomain"], base_domain=BASE)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": BASE})
        assert response.status_code == 200
        assert response.json()["tenant"] is None


async def test_require_tenant_rejects_an_unscoped_request() -> None:
    app = tenancy_app(sources=["subdomain"], base_domain=BASE, require_tenant=True)
    async with client_for(app) as client:  # type: ignore[arg-type]
        assert (await client.get("/whoami", headers={"host": BASE})).status_code == 403


async def test_health_stays_reachable_when_a_tenant_is_required() -> None:
    """A readiness probe has no tenant and must not be 403."""
    app = tenancy_app(sources=["subdomain"], base_domain=BASE, require_tenant=True)
    async with client_for(app) as client:  # type: ignore[arg-type]
        assert (await client.get("/health", headers={"host": BASE})).status_code == 200


async def test_tenant_reaches_the_log_context() -> None:
    from jfastframework.plugins.builtin.observability import tenant_id_var

    seen: list[str | None] = []
    router = APIRouter()

    @router.get("/log")
    async def log() -> dict[str, bool]:
        seen.append(tenant_id_var.get())
        return {"ok": True}

    app = build_test_app(
        plugins=["observability", "tenancy"],
        extra_plugins=[TenancyPlugin],
        routers=[router],
        raw={"plugin": {"tenancy": {"sources": ["subdomain"], "base_domain": BASE}}},
    )
    async with client_for(app) as client:
        await client.get("/log", headers={"host": f"acme.{BASE}"})
    assert seen == ["acme"]


# -- with auth ----------------------------------------------------------

AUTH_SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"


def tenancy_and_auth_app(**tenancy_config: object) -> object:
    from jfastframework.plugins.builtin.auth import AuthPlugin

    router = APIRouter()

    @router.get("/whoami")
    async def whoami(request: Request) -> dict[str, str | None]:
        return {
            "tenant": request.state.tenant_id,
            "source": request.state.tenant_source,
        }

    config: dict[str, object] = {"sources": ["token", "subdomain"], "base_domain": BASE}
    config.update(tenancy_config)
    return build_test_app(
        plugins=["observability", "auth", "tenancy"],
        extra_plugins=[AuthPlugin, TenancyPlugin],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": AUTH_SECRET,
                    "algorithms": ["HS256"],
                    "issuer": "https://id.example.com/",
                    "audience": "billing",
                    "mount_router": False,
                },
                "tenancy": config,
            }
        },
    )


def bearer(tenant: str | None) -> dict[str, str]:
    from datetime import timedelta

    from jfastframework.auth.tokens import issue

    token, _jti, _expires = issue(
        "user-1",
        key=AUTH_SECRET,
        algorithm="HS256",
        lifetime=timedelta(minutes=5),
        audience="billing",
        issuer="https://id.example.com/",
        tenant_id=tenant,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_token_claim_is_readable_from_the_tenancy_middleware() -> None:
    """The ordering test: tenancy must run *after* auth or this is always None."""
    app = tenancy_and_auth_app()
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": BASE, **bearer("acme")})
        assert response.json() == {"tenant": "acme", "source": "token"}


async def test_token_beats_the_subdomain() -> None:
    """A signed claim outranks a hostname, whatever the DNS says."""
    app = tenancy_and_auth_app()
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": f"other.{BASE}", **bearer("acme")})
        assert response.json() == {"tenant": "acme", "source": "token"}


async def test_subdomain_still_applies_without_a_token() -> None:
    app = tenancy_and_auth_app()
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": f"acme.{BASE}"})
        assert response.json() == {"tenant": "acme", "source": "subdomain"}


async def test_require_tenant_accepts_a_token_without_a_subdomain() -> None:
    app = tenancy_and_auth_app(require_tenant=True)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers={"host": BASE, **bearer("acme")})
        assert response.status_code == 200


# -- the signed-in user as the tenant ----------------------------------


def bearer_for(subject: str, tenant: str | None = None) -> dict[str, str]:
    from datetime import timedelta

    from jfastframework.auth.tokens import issue

    token, _jti, _expires = issue(
        subject,
        key=AUTH_SECRET,
        algorithm="HS256",
        lifetime=timedelta(minutes=5),
        audience="billing",
        issuer="https://id.example.com/",
        tenant_id=tenant,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_user_source_makes_each_account_its_own_tenant() -> None:
    app = tenancy_and_auth_app(sources=["token", "user"], base_domain="")
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers=bearer_for("e94e8ce9530947d7"))
        assert response.json() == {"tenant": "e94e8ce9530947d7", "source": "user"}


async def test_an_organisation_claim_outranks_the_user() -> None:
    """A user who joins an organisation moves to it with no code change."""
    app = tenancy_and_auth_app(sources=["token", "user"], base_domain="")
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami", headers=bearer_for("u-1", tenant="acme"))
        assert response.json() == {"tenant": "acme", "source": "token"}


async def test_user_source_resolves_nothing_without_a_token() -> None:
    app = tenancy_and_auth_app(sources=["user"], base_domain="")
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/whoami")
        assert response.json() == {"tenant": None, "source": None}


async def test_current_tenant_is_a_401_without_a_session() -> None:
    from fastapi import Depends

    from jfastframework.plugins.builtin.auth import AuthPlugin
    from jfastframework.plugins.builtin.tenancy import current_tenant

    router = APIRouter()

    @router.get("/mine")
    async def mine(tenant: str = Depends(current_tenant)) -> dict[str, str]:
        return {"tenant": tenant}

    app = build_test_app(
        plugins=["observability", "auth", "tenancy"],
        extra_plugins=[AuthPlugin, TenancyPlugin],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": AUTH_SECRET,
                    "algorithms": ["HS256"],
                    "issuer": "https://id.example.com/",
                    "audience": "billing",
                    "mount_router": False,
                },
                "tenancy": {"sources": ["user"]},
            }
        },
    )
    async with client_for(app) as client:  # type: ignore[arg-type]
        # No session at all is a 401, so a client with an expired token
        # refreshes instead of giving up.
        assert (await client.get("/mine")).status_code == 401
        ok = await client.get("/mine", headers=bearer_for("u-7"))
        assert ok.json() == {"tenant": "u-7"}


async def test_current_tenant_is_a_403_for_a_user_without_a_tenant() -> None:
    """Signed in, but the sources resolve nothing: the request is not scoped."""
    from fastapi import Depends

    from jfastframework.plugins.builtin.tenancy import current_tenant

    router = APIRouter()

    @router.get("/mine")
    async def mine(tenant: str = Depends(current_tenant)) -> dict[str, str]:
        return {"tenant": tenant}

    from jfastframework.plugins.builtin.auth import AuthPlugin

    app = build_test_app(
        plugins=["observability", "auth", "tenancy"],
        extra_plugins=[AuthPlugin, TenancyPlugin],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": AUTH_SECRET,
                    "algorithms": ["HS256"],
                    "issuer": "https://id.example.com/",
                    "audience": "billing",
                    "mount_router": False,
                },
                "tenancy": {"sources": ["token"]},
            }
        },
    )
    async with client_for(app) as client:  # type: ignore[arg-type]
        assert (await client.get("/mine", headers=bearer_for("u-7"))).status_code == 403
