"""Accounts a SaaS can launch with: verification, reset, MFA, rate limits.

Every flow runs through HTTP against SQLite and PostgreSQL, because the
single-use guarantees are conditional UPDATEs and the two databases are where
those differ. Rate limiting needs a real Redis (``JFAST_TEST_REDIS_URL``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework import encryption
from jfastframework.accounts import totp
from jfastframework.accounts.models import one_time_tokens, recovery_codes, users
from jfastframework.errors import PluginError
from jfastframework.testing import build_test_app

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ADMIN = ("admin@example.com", "correct horse battery")
PASSWORD = "a long enough password"
FRONTEND = "https://app.example.com"
PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "")

ACCOUNT_TABLES_DROP = (
    "DROP TABLE IF EXISTS jfast_user_sessions, jfast_recovery_codes, jfast_account_tokens, "
    "jfast_user_roles, jfast_role_permissions, jfast_roles, jfast_users"
)


# -- harness ---------------------------------------------------------------


@pytest.fixture(params=["sqlite", "postgresql"])
async def dsn(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}"
    dsn = f"{PG_BASE}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(ACCOUNT_TABLES_DROP))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await engine.dispose()
    return dsn


@pytest.fixture(autouse=True)
def encryption_key() -> Any:
    box = encryption.SecretBox({"k1": b"k" * 32}, primary="k1")
    encryption.configure(box)
    yield box
    encryption.configure(None)


@pytest.fixture
def frozen_step(monkeypatch: pytest.MonkeyPatch) -> int:
    """TOTP time stands still, so a test is never cut in half by a 30 s boundary."""
    step = totp.step_at()
    monkeypatch.setattr(totp, "step_at", lambda moment=None, *, period=30: step)
    return step


def _app(
    dsn: str, *, mail: bool = True, cache: bool = False, name: str | None = None, **accounts: Any
) -> Any:
    config = {
        "bootstrap_admin_email": ADMIN[0],
        "bootstrap_admin_password": ADMIN[1],
        "max_failed_logins": 3,
        "frontend_url": FRONTEND,
        **accounts,
    }
    plugins = ["database", "auth", "accounts"]
    if mail:
        plugins.insert(2, "mail")
    if cache:
        plugins.insert(1, "cache")
    return build_test_app(
        plugins=plugins,
        app_name=name or f"accounts-{uuid.uuid4().hex[:8]}",
        raw={
            "plugin": {
                "database": {"dsn": dsn},
                "cache": {"url": REDIS_URL},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issue_tokens": True,
                },
                "mail": {
                    "backend": "memory",
                    "queued": False,
                    "templates_dir": "no-such-dir",
                },
                "accounts": config,
            }
        },
    )


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


def _outbox(app: Any) -> list[Any]:
    outbox: list[Any] = app.state.jfast.require("mail").backend.outbox
    return outbox


def _token_in(message: Any) -> str:
    found = re.search(r"\?token=([A-Za-z0-9_\-]+)", message.text)
    assert found, message.text
    return found.group(1)


async def _login(client: httpx.AsyncClient, email: str, password: str) -> Any:
    return await client.post("/auth/login", json={"email": email, "password": password})


def _bearer(pair: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair['access_token']}"}


async def _db(dsn: str, statement: Any) -> Any:
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(statement)
            return result.mappings().all() if result.returns_rows else None
    finally:
        await engine.dispose()


def _code(secret: str, step: int) -> str:
    return totp.hotp(totp.secret_bytes(secret), step)


async def _create(client: httpx.AsyncClient, email: str, roles: list[str] | None = None) -> Any:
    admin = _bearer((await _login(client, *ADMIN)).json())
    response = await client.post(
        "/accounts/users",
        json={"email": email, "password": PASSWORD, "roles": roles or [], "email_verified": True},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- TOTP, against the RFCs --------------------------------------------------


def test_hotp_matches_rfc_4226() -> None:
    key = b"12345678901234567890"
    expected = ["755224", "287082", "359152", "969429", "338314"]
    assert [totp.hotp(key, counter) for counter in range(5)] == expected


@pytest.mark.parametrize(
    ("moment", "code"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_rfc_6238(moment: int, code: str) -> None:
    assert totp.hotp(b"12345678901234567890", totp.step_at(moment), digits=8) == code


def test_a_code_is_good_one_step_either_side_and_no_further() -> None:
    secret = totp.new_secret()
    now = 1_700_000_000.0
    step = totp.step_at(now)
    for offset, accepted in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
        found = totp.verify_totp(secret, _code(secret, step + offset), moment=now)
        assert (found == step + offset) is accepted, offset


def test_a_code_at_or_before_the_last_accepted_step_is_refused() -> None:
    secret = totp.new_secret()
    now = 1_700_000_000.0
    step = totp.step_at(now)
    code = _code(secret, step)
    assert totp.verify_totp(secret, code, moment=now, after_step=step - 1) == step
    assert totp.verify_totp(secret, code, moment=now, after_step=step) is None


def test_the_secret_and_uri_are_what_authenticator_apps_read() -> None:
    secret = totp.new_secret()
    assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20
    uri = totp.otpauth_uri(secret, account="ana@example.com", issuer="Cuadra App")
    assert uri.startswith("otpauth://totp/Cuadra%20App:ana@example.com?secret=")
    assert "issuer=Cuadra%20App" in uri and "digits=6" in uri and "period=30" in uri


def test_recovery_codes_are_eighty_bits_and_forgiving_to_type() -> None:
    codes = totp.generate_recovery_codes(10)
    assert len(set(codes)) == 10
    assert all(re.fullmatch(r"[A-Z0-9]{4}(-[A-Z0-9]{4}){3}", code) for code in codes)
    assert totp.normalize_recovery_code(codes[0].lower().replace("-", " ")) == codes[0].replace(
        "-", ""
    )


# -- boot-time refusals -----------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "mail", "match"),
    [
        ({"email_verification": "required"}, False, "'mail' plugin is not enabled"),
        ({"password_reset": True}, False, "password_reset = true send email"),
        ({"email_verification": "optional", "frontend_url": ""}, True, "frontend_url is empty"),
        ({"mfa_required_roles": ["admin"]}, True, "mfa_required_roles is set and mfa is off"),
        ({"mfa": True, "mfa_token_minutes": 0}, True, "mfa_token_minutes must be positive"),
    ],
)
def test_settings_that_cannot_work_refuse_to_boot(
    dsn: str, settings: dict[str, Any], mail: bool, match: str
) -> None:
    with pytest.raises(PluginError, match=match):
        _app(dsn, mail=mail, **settings)


def test_mfa_without_an_encryption_key_refuses_to_boot(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    encryption.configure(None)
    monkeypatch.delenv("JFAST_ENCRYPTION_KEYS", raising=False)
    with pytest.raises(PluginError, match="JFAST_ENCRYPTION_KEYS"):
        _app(dsn, mfa=True)


# -- an upgrade: the a10 table gains its columns ----------------------------


async def test_a_users_table_from_before_gains_the_new_columns(dsn: str) -> None:
    await _db(
        dsn,
        text(
            "CREATE TABLE jfast_users (id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(255), "
            "email VARCHAR(320) NOT NULL, password_hash VARCHAR(255), display_name VARCHAR(200), "
            "federated_id VARCHAR(320) UNIQUE, is_active BOOLEAN NOT NULL, "
            "failed_logins INTEGER NOT NULL, locked_until TIMESTAMP WITH TIME ZONE, "
            "last_login_at TIMESTAMP WITH TIME ZONE, "
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ),
    )
    async with _Running(_app(dsn)) as client:
        response = await _login(client, *ADMIN)
        assert response.status_code == 200
        me = (await client.get("/auth/account", headers=_bearer(response.json()))).json()
    assert me["email_verified"] is True and me["mfa_enabled"] is False
    # And a second start finds nothing left to add.
    async with _Running(_app(dsn)) as client:
        assert (await _login(client, *ADMIN)).status_code == 200


# -- features ---------------------------------------------------------------


async def test_the_frontend_can_ask_what_is_on(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="optional", mfa=True)
    async with _Running(app) as client:
        features = (await client.get("/auth/features")).json()
    assert features == {
        "registration": True,
        "email_verification": "optional",
        "password_reset": False,
        "mfa": True,
        "min_password_length": 10,
    }


async def test_endpoints_of_features_that_are_off_do_not_exist(dsn: str) -> None:
    async with _Running(_app(dsn, mail=False)) as client:
        for path in ("/auth/verify", "/auth/password/forgot", "/auth/login/mfa", "/auth/mfa/setup"):
            assert (await client.post(path, json={})).status_code == 404, path


# -- email verification -----------------------------------------------------


async def test_required_verification_holds_the_session_until_the_link_is_used(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required")
    async with _Running(app) as client:
        created = await client.post(
            "/auth/register", json={"email": "Ana@Example.com", "password": PASSWORD}
        )
        assert (created.status_code, created.json()) == (202, {"status": "accepted"})
        [message] = _outbox(app)
        assert message.to == ["ana@example.com"]
        assert f"{FRONTEND}/verify-email?token=" in message.text
        token = _token_in(message)

        # The right password, not yet verified: 403 with a code the front reads.
        early = await _login(client, "ana@example.com", PASSWORD)
        assert early.status_code == 403
        assert early.json()["code"] == "email_not_verified"
        # The wrong password says nothing about verification.
        wrong = await _login(client, "ana@example.com", "not the password at all")
        assert wrong.status_code == 401 and "code" not in wrong.json()

        verified = await client.post("/auth/verify", json={"token": token})
        assert (verified.status_code, verified.json()) == (200, {"status": "verified"})
        again = await client.post("/auth/verify", json={"token": token})
        assert again.status_code == 422 and again.json()["code"] == "token_invalid"

        signed_in = await _login(client, "ana@example.com", PASSWORD)
        assert signed_in.status_code == 200
        me = (await client.get("/auth/account", headers=_bearer(signed_in.json()))).json()
        assert me["email_verified"] is True


async def test_the_token_is_stored_only_as_a_hash(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required")
    async with _Running(app) as client:
        await client.post("/auth/register", json={"email": "b@example.com", "password": PASSWORD})
        token = _token_in(_outbox(app)[0])
    rows = await _db(dsn, select(one_time_tokens))
    assert [row["token_hash"] for row in rows] == [totp.hash_code(token)]
    assert all(token not in str(dict(row)) for row in rows)


async def test_signing_up_with_a_taken_address_says_nothing_and_tells_the_owner(
    dsn: str,
) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required", password_reset=True)
    async with _Running(app) as client:
        body = {"email": ADMIN[0], "password": "somebody else's password"}
        taken = await client.post("/auth/register", json=body)
        fresh = await client.post(
            "/auth/register", json={"email": "new@example.com", "password": PASSWORD}
        )
        assert (taken.status_code, taken.json()) == (fresh.status_code, fresh.json())
        # The owner hears, with a way back in; the address still has one account.
        owner = [m for m in _outbox(app) if m.to == [ADMIN[0]]]
        assert len(owner) == 1 and "already" in owner[0].subject
        assert f"{FRONTEND}/reset-password?token=" in owner[0].text
        assert (await _login(client, *ADMIN)).status_code == 200


async def test_optional_verification_signs_in_at_once_and_sends_the_link(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="optional")
    async with _Running(app) as client:
        created = await client.post(
            "/auth/register", json={"email": "c@example.com", "password": PASSWORD}
        )
        assert created.status_code == 201
        headers = _bearer(created.json())
        assert (await client.get("/auth/account", headers=headers)).json()[
            "email_verified"
        ] is False
        token = _token_in(_outbox(app)[0])
        assert (await client.post("/auth/verify", json={"token": token})).status_code == 200
        assert (await client.get("/auth/account", headers=headers)).json()["email_verified"] is True


async def test_resend_answers_the_same_for_anyone_and_mails_only_who_needs_it(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required")
    async with _Running(app) as client:
        await client.post("/auth/register", json={"email": "d@example.com", "password": PASSWORD})
        assert len(_outbox(app)) == 1
        answers = [
            await client.post("/auth/verify/resend", json={"email": email})
            for email in ("nobody@example.com", ADMIN[0], "not an email", "d@example.com")
        ]
        assert {(r.status_code, r.text) for r in answers} == {(202, '{"status":"accepted"}')}
        # Unknown, already verified, malformed: nothing. Still inside the
        # cooldown for d@: nothing either.
        assert len(_outbox(app)) == 1

    app = _app(
        dsn, allow_registration=True, email_verification="required", email_cooldown_seconds=0
    )
    async with _Running(app) as client:
        await client.post("/auth/verify/resend", json={"email": "d@example.com"})
        [message] = _outbox(app)
        # Both links work: the second email does not break the first.
        assert (
            await client.post("/auth/verify", json={"token": _token_in(message)})
        ).status_code == 200


async def test_an_expired_link_is_refused(dsn: str) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required")
    async with _Running(app) as client:
        await client.post("/auth/register", json={"email": "e@example.com", "password": PASSWORD})
        token = _token_in(_outbox(app)[0])
        await _db(dsn, update(one_time_tokens).values(expires_at=text("created_at")))
        refused = await client.post("/auth/verify", json={"token": token})
    assert refused.status_code == 422 and refused.json()["code"] == "token_invalid"


async def test_an_administrator_created_user_is_sent_the_link_unless_vouched_for(dsn: str) -> None:
    app = _app(dsn, email_verification="optional")
    async with _Running(app) as client:
        admin = _bearer((await _login(client, *ADMIN)).json())
        for email, vouched in (("f@example.com", False), ("g@example.com", True)):
            await client.post(
                "/accounts/users",
                json={"email": email, "password": PASSWORD, "email_verified": vouched},
                headers=admin,
            )
    assert [m.to for m in _outbox(app)] == [["f@example.com"]]


# -- password reset ---------------------------------------------------------


async def test_forgot_answers_the_same_whether_or_not_the_address_has_an_account(
    dsn: str,
) -> None:
    app = _app(dsn, password_reset=True)
    async with _Running(app) as client:
        known = await client.post("/auth/password/forgot", json={"email": ADMIN[0]})
        unknown = await client.post("/auth/password/forgot", json={"email": "no@example.com"})
    assert (known.status_code, known.text) == (unknown.status_code, unknown.text)
    assert known.status_code == 202
    assert [m.to for m in _outbox(app)] == [[ADMIN[0]]]


async def test_the_answer_leaves_before_the_address_is_even_looked_up(dsn: str) -> None:
    """Same timing, by construction: nothing that depends on the address runs
    before the response is sent."""
    app = _app(dsn, password_reset=True)
    async with _Running(app):
        plugin = app.state.jfast.require("accounts")
        original = plugin._email_later

        async def slow(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.4)
            await original(*args, **kwargs)

        plugin._email_later = slow
        body = json.dumps({"email": ADMIN[0]}).encode()
        stamps: dict[str, float] = {}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                stamps.setdefault("body", time.monotonic())

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/auth/password/forgot",
            "raw_path": b"/auth/password/forgot",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
            "client": ("127.0.0.1", 5000),
            "server": ("test", 80),
        }
        started = time.monotonic()
        await app(scope, receive, send)
        finished = time.monotonic()
    assert stamps["body"] - started < 0.3
    assert finished - started >= 0.4
    assert len(_outbox(app)) == 1


async def test_a_reset_sets_the_password_and_ends_every_session(dsn: str) -> None:
    app = _app(dsn, password_reset=True)
    async with _Running(app) as client:
        await _create(client, "h@example.com")
        laptop = (await _login(client, "h@example.com", PASSWORD)).json()
        phone = (await _login(client, "h@example.com", PASSWORD)).json()

        await client.post("/auth/password/forgot", json={"email": "h@example.com"})
        token = _token_in(_outbox(app)[-1])
        # Too short: refused, and the link still works afterwards.
        short = await client.post(
            "/auth/password/reset", json={"token": token, "new_password": "short"}
        )
        assert short.status_code == 422
        done = await client.post(
            "/auth/password/reset", json={"token": token, "new_password": "a brand new password"}
        )
        assert done.status_code == 204
        reused = await client.post(
            "/auth/password/reset", json={"token": token, "new_password": "another new password"}
        )
        assert reused.status_code == 422

        for pair in (laptop, phone):
            # The access token stops now, not at its expiry...
            assert (await client.get("/auth/account", headers=_bearer(pair))).status_code == 401
            # ...and the refresh token cannot mint another.
            refreshed = await client.post(
                "/auth/refresh", json={"refresh_token": pair["refresh_token"]}
            )
            assert refreshed.status_code == 401
        assert (await _login(client, "h@example.com", PASSWORD)).status_code == 401
        assert (await _login(client, "h@example.com", "a brand new password")).status_code == 200


async def test_a_reset_unlocks_the_account(dsn: str) -> None:
    app = _app(dsn, password_reset=True)
    async with _Running(app) as client:
        for _ in range(3):
            await _login(client, ADMIN[0], "wrong guess wrong")
        assert (await _login(client, *ADMIN)).status_code == 401
        await client.post("/auth/password/forgot", json={"email": ADMIN[0]})
        token = _token_in(_outbox(app)[-1])
        await client.post(
            "/auth/password/reset", json={"token": token, "new_password": "a brand new password"}
        )
        assert (await _login(client, ADMIN[0], "a brand new password")).status_code == 200


async def test_sessions_minted_elsewhere_end_at_their_next_refresh(dsn: str) -> None:
    """A session accounts did not record -- application code calling the
    issuer, or one from before this release -- still ends at its next refresh."""
    app = _app(dsn, password_reset=True)
    async with _Running(app) as client:
        user = await _create(client, "i@example.com")
        issuer = app.state.jfast.require("auth.issuer")
        stray = await issuer.issue_pair(user["id"], scopes=[], roles=[])
        await asyncio.sleep(1.05)  # iat has whole-second resolution
        await client.post("/auth/password/forgot", json={"email": "i@example.com"})
        token = _token_in(_outbox(app)[-1])
        await client.post(
            "/auth/password/reset", json={"token": token, "new_password": "a brand new password"}
        )
        refused = await client.post("/auth/refresh", json={"refresh_token": stray.refresh_token})
        assert refused.status_code == 401
        # A session opened after the reset is not caught by it.
        fresh = (await _login(client, "i@example.com", "a brand new password")).json()
        again = await client.post("/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
        assert again.status_code == 200


async def test_nothing_secret_reaches_the_log(dsn: str, caplog: pytest.LogCaptureFixture) -> None:
    app = _app(dsn, allow_registration=True, email_verification="required", password_reset=True)
    caplog.set_level(logging.DEBUG)
    async with _Running(app) as client:
        await client.post("/auth/register", json={"email": "j@example.com", "password": PASSWORD})
        await client.post("/auth/verify", json={"token": _token_in(_outbox(app)[-1])})
        await client.post("/auth/password/forgot", json={"email": "j@example.com"})
        reset = _token_in(_outbox(app)[-1])
        await client.post(
            "/auth/password/reset", json={"token": reset, "new_password": "a brand new password"}
        )
        await _login(client, "j@example.com", "a wrong password here")
    secrets_seen = [reset, PASSWORD, "a brand new password", "a wrong password here"]
    for record in caplog.records:
        rendered = record.getMessage() + " " + json.dumps(record.__dict__, default=str)
        assert not any(value in rendered for value in secrets_seen), rendered


async def test_logout_everywhere_ends_every_session(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        first = (await _login(client, *ADMIN)).json()
        second = (await _login(client, *ADMIN)).json()
        assert (await client.post("/auth/logout/all", headers=_bearer(first))).status_code == 204
        for pair in (first, second):
            assert (await client.get("/auth/account", headers=_bearer(pair))).status_code == 401


async def test_deactivating_a_user_ends_their_access_token_at_once(dsn: str) -> None:
    async with _Running(_app(dsn)) as client:
        user = await _create(client, "k@example.com")
        pair = (await _login(client, "k@example.com", PASSWORD)).json()
        admin = _bearer((await _login(client, *ADMIN)).json())
        await client.patch(
            f"/accounts/users/{user['id']}", json={"is_active": False}, headers=admin
        )
        assert (await client.get("/auth/account", headers=_bearer(pair))).status_code == 401


# -- MFA ---------------------------------------------------------------------


async def _enrol(
    client: httpx.AsyncClient, pair: dict[str, Any], step: int
) -> tuple[str, list[str]]:
    setup = await client.post("/auth/mfa/setup", json={"password": PASSWORD}, headers=_bearer(pair))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    confirmed = await client.post(
        "/auth/mfa/confirm", json={"code": _code(secret, step - 1)}, headers=_bearer(pair)
    )
    assert confirmed.status_code == 200, confirmed.text
    return secret, confirmed.json()["recovery_codes"]


async def test_mfa_turns_sign_in_into_two_steps(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        await _create(client, "m@example.com")
        pair = (await _login(client, "m@example.com", PASSWORD)).json()
        secret, codes = await _enrol(client, pair, frozen_step)
        assert len(codes) == 10
        assert (await client.get("/auth/account", headers=_bearer(pair))).json()["mfa_enabled"]

        first = (await _login(client, "m@example.com", PASSWORD)).json()
        assert first["mfa_required"] is True and "access_token" not in first
        done = await client.post(
            "/auth/login/mfa",
            json={"mfa_token": first["mfa_token"], "code": _code(secret, frozen_step)},
        )
        assert done.status_code == 200 and "access_token" in done.json()
        # The MFA token was single use.
        reused = await client.post(
            "/auth/login/mfa",
            json={"mfa_token": first["mfa_token"], "code": _code(secret, frozen_step + 1)},
        )
        assert reused.status_code == 401 and reused.json()["code"] == "mfa_token_invalid"


async def test_the_same_code_does_not_work_twice(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True, max_failed_logins=10)) as client:
        await _create(client, "n@example.com")
        secret, _ = await _enrol(
            client, (await _login(client, "n@example.com", PASSWORD)).json(), frozen_step
        )
        code = _code(secret, frozen_step)
        first = (await _login(client, "n@example.com", PASSWORD)).json()
        ok = await client.post(
            "/auth/login/mfa", json={"mfa_token": first["mfa_token"], "code": code}
        )
        assert ok.status_code == 200

        second = (await _login(client, "n@example.com", PASSWORD)).json()
        replay = await client.post(
            "/auth/login/mfa", json={"mfa_token": second["mfa_token"], "code": code}
        )
        assert replay.status_code == 401 and replay.json()["code"] == "mfa_code_invalid"
        # The code for the next step is fine; the one confirmed with before it is not.
        stale = await client.post(
            "/auth/login/mfa",
            json={"mfa_token": second["mfa_token"], "code": _code(secret, frozen_step - 1)},
        )
        assert stale.status_code == 401
        later = await client.post(
            "/auth/login/mfa",
            json={"mfa_token": second["mfa_token"], "code": _code(secret, frozen_step + 1)},
        )
        assert later.status_code == 200


async def test_a_recovery_code_works_once(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        await _create(client, "o@example.com")
        _, codes = await _enrol(
            client, (await _login(client, "o@example.com", PASSWORD)).json(), frozen_step
        )
        results = []
        for _ in range(2):
            challenge = (await _login(client, "o@example.com", PASSWORD)).json()
            results.append(
                (
                    await client.post(
                        "/auth/login/mfa",
                        # Typed lower-case and with spaces: still the code.
                        json={
                            "mfa_token": challenge["mfa_token"],
                            "code": codes[0].lower().replace("-", " "),
                        },
                    )
                ).status_code
            )
    assert results == [200, 401]
    rows = await _db(dsn, select(recovery_codes.c.code_hash))
    assert all(len(row["code_hash"]) == 64 for row in rows)
    assert not any(codes[0].replace("-", "") in row["code_hash"] for row in rows)


async def test_wrong_codes_spend_the_sign_in_and_then_lock_the_account(
    dsn: str, frozen_step: int
) -> None:
    app = _app(dsn, mfa=True, mfa_max_attempts=2, max_failed_logins=3)
    async with _Running(app) as client:
        await _create(client, "p@example.com")
        secret, _ = await _enrol(
            client, (await _login(client, "p@example.com", PASSWORD)).json(), frozen_step
        )
        challenge = (await _login(client, "p@example.com", PASSWORD)).json()
        for _ in range(2):
            wrong = await client.post(
                "/auth/login/mfa", json={"mfa_token": challenge["mfa_token"], "code": "000000"}
            )
            assert wrong.json()["code"] == "mfa_code_invalid"
        # Two wrong codes: this sign-in is spent even for the right one.
        spent = await client.post(
            "/auth/login/mfa",
            json={"mfa_token": challenge["mfa_token"], "code": _code(secret, frozen_step)},
        )
        assert spent.json()["code"] == "mfa_token_invalid"

        # A third wrong code, on a new sign-in, reaches max_failed_logins.
        challenge = (await _login(client, "p@example.com", PASSWORD)).json()
        await client.post(
            "/auth/login/mfa", json={"mfa_token": challenge["mfa_token"], "code": "000000"}
        )
        assert (await _login(client, "p@example.com", PASSWORD)).status_code == 401


async def test_the_totp_secret_is_encrypted_at_rest(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        await _create(client, "q@example.com")
        secret, _ = await _enrol(
            client, (await _login(client, "q@example.com", PASSWORD)).json(), frozen_step
        )
    [row] = await _db(dsn, select(users.c.mfa_secret).where(users.c.email == "q@example.com"))
    assert row["mfa_secret"].startswith("jf1.") and secret not in row["mfa_secret"]


async def test_turning_mfa_off_takes_the_password_and_a_code(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        await _create(client, "r@example.com")
        pair = (await _login(client, "r@example.com", PASSWORD)).json()
        secret, _ = await _enrol(client, pair, frozen_step)
        no_password = await client.post(
            "/auth/mfa/disable",
            json={"password": "not it at all", "code": _code(secret, frozen_step)},
            headers=_bearer(pair),
        )
        assert no_password.status_code == 422
        assert no_password.json()["code"] == "password_invalid"
        no_code = await client.post(
            "/auth/mfa/disable",
            json={"password": PASSWORD, "code": "123456"},
            headers=_bearer(pair),
        )
        assert no_code.status_code == 422 and no_code.json()["code"] == "mfa_code_invalid"
        off = await client.post(
            "/auth/mfa/disable",
            json={"password": PASSWORD, "code": _code(secret, frozen_step)},
            headers=_bearer(pair),
        )
        assert off.status_code == 204
        assert "access_token" in (await _login(client, "r@example.com", PASSWORD)).json()


async def test_a_role_that_requires_mfa_sends_its_holders_to_enrol(
    dsn: str, frozen_step: int
) -> None:
    async with _Running(_app(dsn, mfa=True, mfa_required_roles=["admin"])) as client:
        challenge = (await _login(client, *ADMIN)).json()
        assert challenge["mfa_enrollment_required"] is True
        assert "access_token" not in challenge

        setup = await client.post("/auth/mfa/setup", json={"mfa_token": challenge["mfa_token"]})
        secret = setup.json()["secret"]
        done = await client.post(
            "/auth/mfa/confirm",
            json={"mfa_token": challenge["mfa_token"], "code": _code(secret, frozen_step)},
        )
        assert done.status_code == 200
        body = done.json()
        assert len(body["recovery_codes"]) == 10
        pair = body["session"]
        assert (await client.get("/auth/account", headers=_bearer(pair))).status_code == 200

        # And the role will not let it be turned off.
        refused = await client.post(
            "/auth/mfa/disable",
            json={"password": ADMIN[1], "code": _code(secret, frozen_step + 1)},
            headers=_bearer(pair),
        )
        assert refused.status_code == 403 and refused.json()["code"] == "mfa_required_by_role"


async def test_gaining_a_role_that_requires_mfa_ends_the_session_at_its_refresh(
    dsn: str,
) -> None:
    app = _app(dsn, mfa=True, mfa_required_roles=["billing"])
    async with _Running(app) as client:
        user = await _create(client, "s@example.com")
        pair = (await _login(client, "s@example.com", PASSWORD)).json()
        admin_challenge = (await _login(client, *ADMIN)).json()
        # The admin role does not require MFA here; the billing role does.
        admin = _bearer(admin_challenge)
        await client.post("/accounts/roles", json={"name": "billing"}, headers=admin)
        await client.patch(
            f"/accounts/users/{user['id']}", json={"roles": ["billing"]}, headers=admin
        )
        refused = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert refused.status_code == 401
        assert (await _login(client, "s@example.com", PASSWORD)).json()["mfa_enrollment_required"]


async def test_an_administrator_can_take_mfa_off_a_user(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        user = await _create(client, "t@example.com")
        await _enrol(client, (await _login(client, "t@example.com", PASSWORD)).json(), frozen_step)
        admin = _bearer((await _login(client, *ADMIN)).json())
        reset = await client.delete(f"/accounts/users/{user['id']}/mfa", headers=admin)
        assert reset.status_code == 204
        assert "access_token" in (await _login(client, "t@example.com", PASSWORD)).json()


async def test_new_recovery_codes_replace_the_old_ones(dsn: str, frozen_step: int) -> None:
    async with _Running(_app(dsn, mfa=True)) as client:
        await _create(client, "u@example.com")
        pair = (await _login(client, "u@example.com", PASSWORD)).json()
        secret, old = await _enrol(client, pair, frozen_step)
        fresh = await client.post(
            "/auth/mfa/recovery-codes",
            json={"password": PASSWORD, "code": _code(secret, frozen_step)},
            headers=_bearer(pair),
        )
        new = fresh.json()["recovery_codes"]
        assert len(new) == 10 and not set(new) & set(old)
        challenge = (await _login(client, "u@example.com", PASSWORD)).json()
        stale = await client.post(
            "/auth/login/mfa", json={"mfa_token": challenge["mfa_token"], "code": old[0]}
        )
        assert stale.status_code == 401


# -- rate limits ------------------------------------------------------------


@pytest.mark.skipif(not REDIS_URL, reason="set JFAST_TEST_REDIS_URL to run against Redis")
async def test_sign_in_is_rate_limited_per_account(dsn: str) -> None:
    app = _app(dsn, cache=True, login_limit_per_account=3, max_failed_logins=50)
    async with _Running(app) as client:
        answers = [
            (await _login(client, "whoever@example.com", "a guess at it")).status_code
            for _ in range(4)
        ]
        assert answers == [401, 401, 401, 429]
        limited = await _login(client, "whoever@example.com", "a guess at it")
        assert int(limited.headers["Retry-After"]) >= 1
        # Another account from the same address is its own bucket.
        assert (await _login(client, *ADMIN)).status_code == 200


@pytest.mark.skipif(not REDIS_URL, reason="set JFAST_TEST_REDIS_URL to run against Redis")
async def test_sign_in_is_rate_limited_per_address(dsn: str) -> None:
    app = _app(dsn, cache=True, login_limit_per_ip=3, max_failed_logins=50)
    async with _Running(app) as client:
        answers = [
            (await _login(client, f"guess{i}@example.com", "a guess at it")).status_code
            for i in range(4)
        ]
    assert answers == [401, 401, 401, 429]


@pytest.mark.skipif(not REDIS_URL, reason="set JFAST_TEST_REDIS_URL to run against Redis")
async def test_email_requests_are_rate_limited(dsn: str) -> None:
    app = _app(dsn, cache=True, password_reset=True, email_limit_per_ip=2)
    async with _Running(app) as client:
        answers = [
            (await client.post("/auth/password/forgot", json={"email": ADMIN[0]})).status_code
            for _ in range(3)
        ]
    assert answers == [202, 202, 429]


async def test_without_cache_the_lockout_still_applies(dsn: str) -> None:
    app = _app(dsn, max_failed_logins=2)
    async with _Running(app) as client:
        assert app.state.jfast.require("accounts")._limiter is None
        for _ in range(2):
            await _login(client, ADMIN[0], "not the password")
        assert (await _login(client, *ADMIN)).status_code == 401


async def test_a_provider_sign_in_still_owes_the_second_factor(dsn: str, frozen_step: int) -> None:
    from types import SimpleNamespace

    app = _app(dsn, mfa=True)
    async with _Running(app) as client:
        await _create(client, "v@example.com")
        await _enrol(client, (await _login(client, "v@example.com", PASSWORD)).json(), frozen_step)
        plugin = app.state.jfast.require("accounts")
        identity = SimpleNamespace(
            federated_id="google:123", email="v@example.com", email_verified=True, name="V"
        )
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=None))
        result = await plugin._sign_in_identity(identity, request)
    assert result.mfa_required is True and not hasattr(result, "access_token")


async def test_a_column_that_cannot_be_added_in_place_is_refused(tmp_path: Path) -> None:
    from sqlalchemy import Column, Integer, String, Table

    from jfastframework.db.framework import ensure_columns, ensure_tables, framework_metadata

    table = Table(
        "jfast_test_evolving",
        framework_metadata,
        Column("id", String(8), primary_key=True),
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    try:
        await ensure_tables(engine, "jfast_test_evolving")
        table.append_column(Column("note", String(20)))
        assert await ensure_columns(engine, "jfast_test_evolving") == ["note"]
        assert await ensure_columns(engine, "jfast_test_evolving") == []
        table.append_column(Column("count", Integer, nullable=False))
        with pytest.raises(ValueError, match="nullable"):
            await ensure_columns(engine, "jfast_test_evolving")
    finally:
        framework_metadata.remove(table)
        await engine.dispose()
