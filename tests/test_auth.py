"""Auth: what a token must prove, and the attacks the defaults refuse.

Most of these are negative tests. That is deliberate — the failure modes here
are the ones that do not look like failures: a token that verifies when it
should not still returns 200.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Depends

from jfastframework.auth import (
    Grant,
    MemoryTokenStore,
    Principal,
    RedisTokenStore,
    TokenError,
    TokenStore,
    issue,
    verify,
)
from jfastframework.auth.jwks import JWKSClient
from jfastframework.auth.tokens import TokenClaims
from jfastframework.errors import PluginError, UnauthorizedError
from jfastframework.plugins.builtin.auth import (
    AuthPlugin,
    TokenIssuer,
    TokenPair,
    optional_auth,
    require_auth,
    require_roles,
    require_scopes,
)
from jfastframework.testing import build_test_app, client_for

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ISSUER = "https://id.example.com/"
AUDIENCE = "billing"


def mint(**overrides: Any) -> str:
    params: dict[str, Any] = {
        "key": SECRET,
        "algorithm": "HS256",
        "lifetime": timedelta(minutes=5),
        "audience": AUDIENCE,
        "issuer": ISSUER,
    }
    params.update(overrides)
    subject = params.pop("subject", "user-1")
    token, _, _ = issue(subject, **params)
    return token


def check(token: str, **overrides: Any) -> Principal:
    params: dict[str, Any] = {
        "key": SECRET,
        "algorithms": ["HS256"],
        "audience": AUDIENCE,
        "issuer": ISSUER,
    }
    params.update(overrides)
    return verify(token, **params)


# -- the happy path -----------------------------------------------------


def test_a_valid_token_identifies_the_caller() -> None:
    token = mint(scopes=["invoices:read", "invoices:write"], roles=["admin"], tenant_id="acme")
    caller = check(token)

    assert caller.subject == "user-1"
    assert caller.has_scope("invoices:read")
    assert caller.has_any_role("admin")
    assert caller.tenant_id == "acme"
    assert caller.token_id is not None


def test_scopes_are_read_from_a_list_or_a_space_delimited_string() -> None:
    # Issuers disagree about this and both spellings are in the wild.
    import jwt

    payload = {
        "sub": "u",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "scope": ["a", "b"],
    }
    caller = check(jwt.encode(payload, SECRET, algorithm="HS256"), audience=None, issuer=None)
    assert caller.scopes == {"a", "b"}


def test_claim_names_are_configurable() -> None:
    claims = TokenClaims(scopes="permissions", roles="groups", tenant="org")
    token = mint(scopes=["x"], roles=["y"], tenant_id="z", claims=claims)
    caller = check(token, claims=claims)

    assert caller.scopes == {"x"} and caller.roles == {"y"} and caller.tenant_id == "z"


# -- the attacks --------------------------------------------------------


def test_an_unsigned_token_is_rejected() -> None:
    import jwt

    # `alg: none` is the oldest JWT attack. Passing an explicit algorithm
    # allow-list is what refuses it.
    payload = {
        "sub": "attacker",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    unsigned = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(TokenError):
        check(unsigned, audience=None, issuer=None)


def test_a_token_signed_with_another_key_is_rejected() -> None:
    with pytest.raises(TokenError, match="invalid"):
        check(mint(key="another-secret-also-long-enough-for-sha256-here"))


def test_a_token_for_another_audience_is_rejected() -> None:
    # Without this, one compromised low-value service's token opens a
    # high-value one from the same issuer.
    with pytest.raises(TokenError, match="different audience"):
        check(mint(audience="some-other-service"))


def test_a_token_from_another_issuer_is_rejected() -> None:
    with pytest.raises(TokenError, match="unexpected issuer"):
        check(mint(issuer="https://evil.example.com/"))


def test_an_expired_token_is_rejected() -> None:
    with pytest.raises(TokenError, match="expired"):
        check(mint(lifetime=timedelta(seconds=-120)))


def test_the_expiry_leeway_is_seconds_not_minutes() -> None:
    # A token 20s past expiry passes with the default 30s leeway; one two
    # minutes past does not. A generous leeway is extra life for a stolen token.
    assert check(mint(lifetime=timedelta(seconds=-20))).subject == "user-1"
    with pytest.raises(TokenError):
        check(mint(lifetime=timedelta(seconds=-120)))


def test_a_token_without_a_subject_is_rejected() -> None:
    import jwt

    payload = {
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    with pytest.raises(TokenError, match="missing a required claim"):
        check(jwt.encode(payload, SECRET, algorithm="HS256"), audience=None, issuer=None)


def test_an_unsupported_algorithm_cannot_be_configured() -> None:
    with pytest.raises(TokenError, match="unsupported algorithm"):
        check(mint(), algorithms=["none"])


def test_reserved_claims_cannot_be_overridden_when_issuing() -> None:
    # Otherwise a caller passing extra={"sub": "admin"} widens its own token.
    with pytest.raises(TokenError, match="reserved claim"):
        mint(extra={"sub": "admin"})


def test_mixing_symmetric_and_asymmetric_algorithms_is_refused() -> None:
    plugin = AuthPlugin({"mode": "secret", "secret": SECRET, "algorithms": ["HS256", "RS256"]})
    # This configuration *is* the algorithm-confusion attack: a token signed
    # with the RSA public key as an HMAC secret would verify.
    with pytest.raises(PluginError, match="mixes symmetric and asymmetric"):
        plugin._validate()


def test_jwks_mode_refuses_hmac_algorithms() -> None:
    plugin = AuthPlugin({"mode": "jwks", "jwks_url": "https://x/jwks", "algorithms": ["HS256"]})
    with pytest.raises(PluginError, match="cannot verify HMAC"):
        plugin._validate()


def test_jwks_mode_refuses_to_issue_tokens() -> None:
    plugin = AuthPlugin({"mode": "jwks", "jwks_url": "https://x/jwks", "issue_tokens": True})
    with pytest.raises(PluginError, match="needs a private key"):
        plugin._validate()


def test_secret_mode_requires_a_secret() -> None:
    with pytest.raises(PluginError, match="JFAST_AUTH_SECRET"):
        AuthPlugin({"mode": "secret", "algorithms": ["HS256"]})._validate()


# -- the HTTP surface ---------------------------------------------------


def guarded_router() -> APIRouter:
    router = APIRouter()

    @router.get("/public")
    async def public(caller: Principal | None = Depends(optional_auth)) -> dict[str, Any]:
        return {"caller": caller.subject if caller else None}

    @router.get("/private")
    async def private(caller: Principal = Depends(require_auth)) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/write")
    async def write(
        caller: Principal = Depends(require_scopes("invoices:write")),
    ) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/admin")
    async def admin(caller: Principal = Depends(require_roles("admin"))) -> dict[str, str]:
        return {"subject": caller.subject}

    return router


def auth_app():  # type: ignore[no-untyped-def]
    return build_test_app(
        plugins=["auth"],
        app_name="billing",
        routers=[guarded_router()],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                }
            }
        },
    )


def issuing_app():  # type: ignore[no-untyped-def]
    """The same app, allowed to mint its own tokens."""
    return build_test_app(
        plugins=["auth"],
        app_name="billing",
        routers=[guarded_router()],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                    "issue_tokens": True,
                }
            }
        },
    )


def issuer_of(app: Any) -> TokenIssuer:
    plugin = next(p for p in app.state.plugins if p.meta.name == "auth")
    issuer = plugin._issuer
    assert issuer is not None
    return issuer  # type: ignore[no-any-return]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_public_route_works_without_a_token() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/public")
    assert response.status_code == 200
    assert response.json() == {"caller": None}


async def test_a_public_route_still_works_with_a_bad_token() -> None:
    # The middleware must never reject: rejection is the dependency's job, or
    # every route becomes private.
    async with client_for(auth_app()) as client:
        response = await client.get("/public", headers=bearer("not-a-token"))
    assert response.status_code == 200


async def test_a_private_route_without_a_token_is_401() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/private")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_private_route_with_a_valid_token_succeeds() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/private", headers=bearer(mint()))
    assert response.json() == {"subject": "user-1"}


async def test_a_missing_scope_is_403_not_401() -> None:
    # 401 means "I do not know who you are"; 403 means "I do, and you may not".
    # Collapsing them makes every permissions bug guesswork.
    async with client_for(auth_app()) as client:
        response = await client.get("/write", headers=bearer(mint(scopes=["invoices:read"])))
    assert response.status_code == 403
    assert "invoices:write" in response.json()["detail"]


async def test_the_right_scope_passes() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/write", headers=bearer(mint(scopes=["invoices:write"])))
    assert response.status_code == 200


async def test_roles_are_any_of_not_all_of() -> None:
    async with client_for(auth_app()) as client:
        allowed = await client.get("/admin", headers=bearer(mint(roles=["admin", "auditor"])))
        denied = await client.get("/admin", headers=bearer(mint(roles=["auditor"])))
    assert allowed.status_code == 200
    assert denied.status_code == 403


async def test_the_rejection_reason_does_not_reach_the_client() -> None:
    # Telling an attacker *why* their token failed is free reconnaissance.
    async with client_for(auth_app()) as client:
        response = await client.get("/private", headers=bearer(mint(issuer="https://evil/")))
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


async def test_the_tenant_comes_from_the_token_not_the_header() -> None:
    router = APIRouter()

    @router.get("/tenant")
    async def tenant(caller: Principal = Depends(require_auth)) -> dict[str, Any]:
        return {"tenant": caller.tenant_id}

    app = build_test_app(
        plugins=["auth"],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                }
            }
        },
    )
    async with client_for(app) as client:
        response = await client.get(
            "/tenant",
            headers={**bearer(mint(tenant_id="acme")), "X-Tenant-ID": "attacker-corp"},
        )
    # Anyone can send X-Tenant-ID. Only the issuer can sign a claim.
    assert response.json() == {"tenant": "acme"}


# -- refresh rotation ---------------------------------------------------


def make_issuer(store: TokenStore, *, grace: float = 0) -> TokenIssuer:
    # Grace off unless a test is about it: a replay that lands inside the
    # window is deliberately *not* a replay, so every test that presents a used
    # token to prove reuse detection has to present it outside one.
    return TokenIssuer(
        key=SECRET,
        algorithm="HS256",
        issuer=ISSUER,
        audience=AUDIENCE,
        access_lifetime=timedelta(minutes=15),
        refresh_lifetime=timedelta(days=30),
        store=store,
        claims=TokenClaims(),
        refresh_grace=timedelta(seconds=grace),
    )


async def test_a_refresh_returns_a_new_pair() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"], tenant_id="acme")

    principal = check(pair.refresh_token)
    rotated = await issuer.rotate(principal)

    assert rotated.refresh_token != pair.refresh_token
    assert check(rotated.access_token).subject == "user-1"
    assert check(rotated.access_token).has_scope("a")


async def test_an_access_token_cannot_be_used_to_refresh() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")

    with pytest.raises(UnauthorizedError, match="cannot be used to refresh"):
        await issuer.rotate(check(pair.access_token))


async def test_replaying_a_refresh_token_revokes_the_whole_session() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")
    first = check(pair.refresh_token)

    await issuer.rotate(first)

    # A used refresh token presented again is either a retry or a theft, and
    # they are indistinguishable. Killing the family is the safe answer.
    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(first)

    family = str(first.claims["fam"])
    assert await store.is_family_revoked(family)
    # The family is this session, not this person: revoking it must not reach
    # the same subject's other sessions, nor their next login.
    assert family != "user-1"


async def test_a_rotated_token_from_a_revoked_family_is_refused() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")
    second = await issuer.rotate(check(pair.refresh_token))

    await store.revoke_family(str(check(second.refresh_token).claims["fam"]), ttl=3600)

    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(check(second.refresh_token))


async def test_a_rotated_access_token_keeps_the_scopes_it_was_issued_with() -> None:
    # The whole point of a refresh: the new access token must be able to do
    # what the old one could, or every refresh is a silent downgrade to 403.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair(
        "user-1", scopes=["invoices:read", "invoices:write"], roles=["admin"], tenant_id="acme"
    )

    rotated = await issuer.rotate(check(pair.refresh_token))
    caller = check(rotated.access_token)

    assert caller.has_scope("invoices:read", "invoices:write")
    assert caller.has_any_role("admin")
    assert caller.tenant_id == "acme"


async def test_scopes_survive_a_second_rotation() -> None:
    # Once, not twice, is the shape of a bug where the grant is read from the
    # access token instead of carried by the session.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"], roles=["admin"])

    once = await issuer.rotate(check(pair.refresh_token))
    twice = await issuer.rotate(check(once.refresh_token))

    assert check(twice.access_token).has_scope("a")
    assert check(twice.access_token).has_any_role("admin")


async def test_a_refresh_token_is_not_authorized_for_anything_it_carries() -> None:
    # The grant rides along as rotation bookkeeping. It must never come back
    # through verify() as authorization, or a 30-day token gains an access
    # token's rights.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["invoices:write"], roles=["admin"])

    carrier = check(pair.refresh_token)

    assert carrier.scopes == frozenset()
    assert carrier.roles == frozenset()
    assert carrier.claims["grt"]["scopes"] == ["invoices:write"]
    assert carrier.claims["grt"]["roles"] == ["admin"]


async def test_a_replay_on_one_device_does_not_end_the_other_devices_session() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    phone = await issuer.issue_pair("user-1")
    laptop = await issuer.issue_pair("user-1")

    used = check(phone.refresh_token)
    await issuer.rotate(used)
    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(used)

    # The phone is compromised, not the person. The laptop keeps working.
    rotated = await issuer.rotate(check(laptop.refresh_token))
    assert check(rotated.access_token).subject == "user-1"


async def test_a_revoked_session_does_not_poison_the_next_login() -> None:
    # A family keyed on the subject outlives the session it revoked: the next
    # login is born revoked, for the whole refresh lifetime.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    old = await issuer.issue_pair("user-1")
    await store.revoke_family(str(check(old.refresh_token).claims["fam"]), ttl=3600)

    fresh = await issuer.issue_pair("user-1")
    rotated = await issuer.rotate(check(fresh.refresh_token))

    assert check(rotated.access_token).subject == "user-1"


async def test_two_sessions_for_one_person_get_different_families() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    phone = await issuer.issue_pair("user-1")
    laptop = await issuer.issue_pair("user-1")

    assert check(phone.refresh_token).claims["fam"] != check(laptop.refresh_token).claims["fam"]
    # And the access token carries it too, or logout has no session to end.
    assert check(phone.access_token).claims["fam"] == check(phone.refresh_token).claims["fam"]


async def test_a_refresh_token_from_before_the_grant_claim_is_refused() -> None:
    # Rotating it would mint the zero-scope token that is the bug, and the 403
    # would land far from the cause.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    legacy = check(mint(token_type="refresh", lifetime=timedelta(days=30), extra={"fam": "fam-1"}))
    assert legacy.token_id is not None
    await store.remember_refresh(legacy.token_id, family="fam-1", ttl=3600)

    with pytest.raises(UnauthorizedError, match="predates"):
        await issuer.rotate(legacy)


async def test_a_refresh_token_without_a_family_is_refused() -> None:
    # No subject fallback: that fallback is what bricked the next login.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    orphan = check(mint(token_type="refresh", lifetime=timedelta(days=30)))

    with pytest.raises(UnauthorizedError, match="no session family"):
        await issuer.rotate(orphan)


async def test_a_refresh_token_is_refused_as_a_bearer_token() -> None:
    # It verifies -- same key, same issuer, same audience -- so nothing but an
    # explicit typ check stops a 30-day token from opening a session.
    app = issuing_app()
    pair = await issuer_of(app).issue_pair("user-1", scopes=["invoices:write"])

    async with client_for(app) as client:
        response = await client.get("/private", headers=bearer(pair.refresh_token))

    assert response.status_code == 401


async def test_logging_out_ends_this_session_and_not_the_other_one() -> None:
    app = issuing_app()
    issuer = issuer_of(app)
    phone = await issuer.issue_pair("user-1")
    laptop = await issuer.issue_pair("user-1")

    async with client_for(app) as client:
        out = await client.post("/auth/logout", headers=bearer(phone.access_token))
        elsewhere = await client.post("/auth/refresh", json={"refresh_token": laptop.refresh_token})

    assert out.status_code == 204
    assert elsewhere.status_code == 200


async def test_logging_out_kills_the_refresh_token_issued_with_it() -> None:
    # Logout reads the family off the *access* token. If the access token does
    # not carry one, logout silently stops ending sessions.
    app = issuing_app()
    pair = await issuer_of(app).issue_pair("user-1")

    async with client_for(app) as client:
        await client.post("/auth/logout", headers=bearer(pair.access_token))
        response = await client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})

    assert response.status_code == 401


# -- the refresh hook ---------------------------------------------------


async def test_a_refresh_hook_re_reads_the_callers_current_rights() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"])

    @issuer.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        assert principal.subject == "user-1"
        return Grant(scopes=("a", "b"), roles=("admin",))

    rotated = await issuer.rotate(check(pair.refresh_token))
    caller = check(rotated.access_token)

    assert caller.has_scope("a", "b")
    assert caller.has_any_role("admin")


async def test_a_refresh_hook_can_end_the_session() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"])

    @issuer.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        return None

    with pytest.raises(UnauthorizedError, match="no longer valid"):
        await issuer.rotate(check(pair.refresh_token))

    # Declining revokes the family, so the presented token is not merely
    # spent -- there is nothing left of the session to present it to.
    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(check(pair.refresh_token))


async def test_a_refresh_hook_overrides_what_the_token_carried() -> None:
    # A permission taken away today must not survive in a token minted before
    # it was taken away.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["invoices:write"])

    @issuer.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        return Grant(scopes=("invoices:read",))

    caller = check((await issuer.rotate(check(pair.refresh_token))).access_token)

    assert caller.has_scope("invoices:read")
    assert not caller.has_scope("invoices:write")


async def test_a_declined_refresh_revokes_the_family() -> None:
    # The 401 is the half that already worked. The half that was broken is
    # this one: without the revocation the session outlives its own end.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"])
    family = str(check(pair.refresh_token).claims["fam"])

    @issuer.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        return None

    with pytest.raises(UnauthorizedError, match="no longer valid"):
        await issuer.rotate(check(pair.refresh_token))

    assert await store.is_family_revoked(family)


async def test_a_banned_users_outstanding_access_token_stops_working() -> None:
    # Refusing the refresh is not a ban: the access token in the client's hands
    # is good for another fifteen minutes, and that is the window an
    # application that called revoke_family by hand did not leave open.
    app = issuing_app()
    plugin = next(p for p in app.state.plugins if p.meta.name == "auth")
    pair = await issuer_of(app).issue_pair("user-1", scopes=["invoices:write"])

    @plugin.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        return None

    async with client_for(app) as client:
        before = await client.get("/private", headers=bearer(pair.access_token))
        refused = await client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
        after = await client.get("/private", headers=bearer(pair.access_token))

    assert before.status_code == 200
    assert refused.status_code == 401
    assert after.status_code == 401


async def test_a_hook_that_fails_does_not_burn_the_refresh_token() -> None:
    # A momentary database blink is indistinguishable from a stolen token only
    # if the token was already consumed when the blink happened. The client's
    # natural retry then reads as a replay and costs the whole family.
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"])
    family = str(check(pair.refresh_token).claims["fam"])
    calls = 0

    @issuer.on_refresh
    async def rights(principal: Principal) -> Grant | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection reset by peer")
        return Grant(scopes=("a",))

    with pytest.raises(RuntimeError):
        await issuer.rotate(check(pair.refresh_token))

    rotated = await issuer.rotate(check(pair.refresh_token))

    assert check(rotated.access_token).has_scope("a")
    assert not await store.is_family_revoked(family)


# -- concurrent refresh -------------------------------------------------

REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "")


@pytest.mark.skipif(
    not REDIS_URL,
    reason="set JFAST_TEST_REDIS_URL: MemoryTokenStore is one process and cannot show this race",
)
async def test_two_concurrent_refreshes_do_not_lock_the_account() -> None:
    # A browser with two tabs does exactly this. One of them has to lose; what
    # cannot happen is that the winner's brand-new pair is dead on arrival
    # because the loser's attempt read as a replay.
    import uuid

    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    store = RedisTokenStore(client, prefix=f"jfast-test-{uuid.uuid4().hex}:")
    issuer = make_issuer(store, grace=10)
    try:
        pair = await issuer.issue_pair("user-1", scopes=["a"])
        presented = check(pair.refresh_token)
        family = str(presented.claims["fam"])

        results = await asyncio.gather(
            issuer.rotate(presented), issuer.rotate(presented), return_exceptions=True
        )
        won = [r for r in results if isinstance(r, TokenPair)]

        assert len(won) == 1
        assert not await store.is_family_revoked(family)

        again = await issuer.rotate(check(won[0].refresh_token))
        assert check(again.access_token).has_scope("a")
    finally:
        await client.aclose()


@pytest.mark.skipif(
    not REDIS_URL,
    reason="set JFAST_TEST_REDIS_URL: MemoryTokenStore is one process and cannot show this race",
)
async def test_a_replay_outside_the_grace_window_still_kills_the_family() -> None:
    # The window is what makes the two-tab race survivable. If it also made a
    # replay survivable, reuse detection would be gone rather than narrowed.
    import uuid

    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    store = RedisTokenStore(client, prefix=f"jfast-test-{uuid.uuid4().hex}:")
    issuer = make_issuer(store, grace=1)
    try:
        pair = await issuer.issue_pair("user-1", scopes=["a"])
        presented = check(pair.refresh_token)
        family = str(presented.claims["fam"])
        await issuer.rotate(presented)

        # Redis expires the grace mark on its own clock, so this waits it out
        # rather than reaching into the store.
        await asyncio.sleep(1.2)

        with pytest.raises(UnauthorizedError, match="revoked"):
            await issuer.rotate(presented)
        assert await store.is_family_revoked(family)
    finally:
        await client.aclose()


async def test_the_grace_window_is_on_by_default() -> None:
    # Configuration, not code, is where this can silently regress: a default of
    # 0 restores exactly the behaviour that locks a two-tab browser out.
    app = issuing_app()
    pair = await issuer_of(app).issue_pair("user-1", scopes=["invoices:write"])

    async with client_for(app) as client:
        first = await client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
        second = await client.post("/auth/refresh", json={"refresh_token": pair.refresh_token})
        survivor = await client.get("/write", headers=bearer(first.json()["access_token"]))

    assert first.status_code == 200
    assert second.status_code == 401
    assert survivor.status_code == 200


# -- revocation ---------------------------------------------------------


async def test_a_revoked_token_stops_working() -> None:
    token = mint()
    app = auth_app()
    plugin = next(p for p in app.state.plugins if p.meta.name == "auth")

    async with client_for(app) as client:
        assert (await client.get("/private", headers=bearer(token))).status_code == 200
        await client.post("/auth/logout", headers=bearer(token))
        after = await client.get("/private", headers=bearer(token))

    assert after.status_code == 401
    assert plugin is not None


async def test_the_in_memory_store_reports_itself_as_not_shared() -> None:
    # A logout that silently applies to one replica is worse than one that
    # says it does.
    healthy, detail = await MemoryTokenStore().health()
    assert healthy is False
    assert "other replicas" in detail


async def test_me_returns_identity_and_never_the_token() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get(
            "/auth/me", headers=bearer(mint(scopes=["a"], tenant_id="acme"))
        )
    body = response.json()
    assert body["subject"] == "user-1"
    assert body["tenant_id"] == "acme"
    assert "claims" not in body and "token" not in body


# -- JWKS: one fetch per expiry, not one per request --------------------


class _CountingJWKS(JWKSClient):
    """A client whose network call is counted and deliberately slow.

    The delay is what makes the race real: without it the first caller
    finishes before the second is scheduled, and a broken implementation
    passes.
    """

    calls: int = 0

    async def _fetch(self) -> None:  # type: ignore[override]
        # The same clock the client reads, so a test that freezes it freezes
        # both sides of the comparison -- which is what Windows does.
        from jfastframework.auth import jwks as jwks_module

        type(self).calls += 1
        self._last_attempt = jwks_module.time.monotonic()
        await asyncio.sleep(0.01)
        self._keys = {"k1": {"kid": "k1"}}
        self._fetched_at = jwks_module.time.monotonic()
        self._last_error = None


async def test_a_cold_cache_under_load_fetches_once() -> None:
    # Fifty requests arriving together on a fresh replica is the ordinary
    # case, not a pathological one. Without a lock each of them opens its own
    # connection to the identity service -- a stampede aimed at the single
    # dependency whose being down makes every token unverifiable.
    _CountingJWKS.calls = 0
    client = _CountingJWKS(url="https://id.example.com/jwks.json")

    keys = await asyncio.gather(*(client.key_for("k1") for _ in range(50)))

    assert all(key == {"kid": "k1"} for key in keys)
    assert _CountingJWKS.calls == 1


async def test_a_coarse_clock_does_not_break_the_single_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows failure, reproduced on any OS: a clock that does not move.

    `time.monotonic()` advances in ~15.6 ms steps on Windows, and the fetch
    above takes 10 ms, so start and finish read the same value. The waiters
    compared those readings to decide whether somebody had already fetched,
    and fetched again. A frozen clock is the limit of that, and must still
    produce exactly one fetch.
    """
    from types import SimpleNamespace

    from jfastframework.auth import jwks as jwks_module

    # Frozen for the JWKS client only. Patching time.monotonic itself would
    # freeze the event loop too, and asyncio.sleep would never return.
    monkeypatch.setattr(jwks_module, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    _CountingJWKS.calls = 0
    client = _CountingJWKS(url="https://id.example.com/jwks.json")

    await asyncio.gather(*(client.key_for("k1") for _ in range(50)))

    assert _CountingJWKS.calls == 1


async def test_an_expired_cache_under_load_also_fetches_once() -> None:
    _CountingJWKS.calls = 0
    client = _CountingJWKS(url="https://id.example.com/jwks.json", cache_seconds=60)
    await client.key_for("k1")
    assert _CountingJWKS.calls == 1

    # The cache is aged by hand rather than by waiting or by setting
    # cache_seconds=0: `time.monotonic()` advances in ~15.6 ms steps on
    # Windows, so a zero lifetime is not reliably expired on the very next
    # call and the test measured the clock instead of the lock.
    client._fetched_at -= 3600

    await asyncio.gather(*(client.key_for("k1") for _ in range(50)))

    # One more for the expiry, and one only.
    assert _CountingJWKS.calls == 2
