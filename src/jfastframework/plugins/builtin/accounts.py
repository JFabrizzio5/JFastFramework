"""Accounts: the user store and login that the ``auth`` plugin leaves to you.

    [plugins]
    enabled = ["database", "cache", "auth", "mail", "accounts"]

    [plugin.auth]
    mode = "secret"
    issue_tokens = true

    [plugin.accounts]
    allow_registration = true
    email_verification = "required"      # off | optional | required
    password_reset = true
    frontend_url = "https://app.example.com"
    mfa = true                           # needs JFAST_ENCRYPTION_KEYS
    mfa_required_roles = ["admin"]
    bootstrap_admin_email = "admin@example.com"   # password from the environment

Mounts, under ``[plugin.accounts] prefix`` (``/auth``):

* ``POST /auth/login`` -- email and password in, a token pair out, or the
  second step when the account has two-factor authentication;
* ``POST /auth/login/mfa`` -- that second step;
* ``POST /auth/register`` -- only when ``allow_registration`` is on;
* ``POST /auth/verify`` and ``/auth/verify/resend`` -- email verification;
* ``POST /auth/password/forgot`` and ``/auth/password/reset`` -- reset by email;
* ``/auth/mfa/*`` -- enrolment, recovery codes, turning it off;
* ``GET /auth/account``, ``POST /auth/password``, ``POST /auth/logout/all``;
* ``GET /auth/features`` -- what of the above this service has turned on;

and under ``admin_prefix`` (``/accounts``), for holders of ``accounts:admin``:
users and roles, within the administrator's own tenant.

It also registers two ``auth`` hooks: ``on_refresh`` re-reads a user's roles
at every refresh, so a permission taken away or an account deactivated ends
the session at its next refresh instead of at its end; and, when ``auth`` has
social providers, ``on_identity`` turns a verified identity into a user.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict

# At module level, not inside the router builders: FastAPI resolves a route's
# annotations against the module's globals, so a type imported locally is a
# name it cannot find.
from jfastframework.auth.principal import Principal
from jfastframework.errors import (
    ForbiddenError,
    JFastError,
    PluginError,
    UnauthorizedError,
    ValidationError,
    problem_response,
)
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings
from jfastframework.plugins.builtin.database import DbSession

if TYPE_CHECKING:
    from jfastframework.accounts.mfa import MfaService
    from jfastframework.accounts.service import AccountsService, User
    from jfastframework.auth.principal import Grant
    from jfastframework.context import AppContext

EMAIL_VERIFICATION_MODES = ("off", "optional", "required")


def _signin_tenant(request: Request) -> str | None:
    """Which tenant's accounts a sign-in, sign-up or reset looks in.

    The tenant the request *names* -- subdomain or path -- because before a
    session exists that is the only way to say which tenant the form is for,
    and the password (or the emailed token, or the provider) is what proves
    the caller belongs there. Not ``request.state.tenant_id``: with `auth` on,
    tenancy grants no tenant to a request without a session.
    """
    from jfastframework.plugins.builtin.tenancy import tenant_hint

    return tenant_hint(request)


class AccountsSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_ACCOUNTS_", env_file=".env", extra="ignore")

    prefix: str = "/auth"
    admin_prefix: str = "/accounts"
    # Off by default: a service that lets anybody create an account says so.
    allow_registration: bool = False
    # Roles a self-registered or social-signup user starts with, by name.
    default_roles: list[str] = Field(default_factory=list)
    min_password_length: int = 10
    # Failed passwords before the account is locked, and for how long. Counted
    # per account, so a guesser cannot get past it by changing IP address.
    # Wrong second-factor codes count here too.
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    admin_permission: str = "accounts:admin"
    # Created at startup when no user has this email yet, with a role holding
    # admin_permission. The password comes from JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD
    # and never from jfast.toml, which is committed.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: SecretStr | None = None
    # With auth providers configured, sign provider-verified users in here.
    social_login: bool = True

    # -- email verification and password reset: both send mail ----------
    # off: nothing is sent. optional: a link is sent at sign-up and the
    # account works meanwhile. required: no session until the link is used.
    email_verification: Literal["off", "optional", "required"] = "off"
    verification_token_minutes: int = 24 * 60
    password_reset: bool = False
    reset_token_minutes: int = 30
    # Where the links in those emails point. The frontend reads the token from
    # the address and posts it back; the API never serves the page.
    frontend_url: str = ""
    verify_email_path: str = "/verify-email"
    reset_password_path: str = "/reset-password"
    login_path: str = "/login"
    # At most one email of each kind per address per this many seconds,
    # however often it is asked for -- a form is not a way to flood an inbox.
    email_cooldown_seconds: int = 60

    # -- two-factor authentication (TOTP) --------------------------------
    mfa: bool = False
    # Users holding any of these roles cannot get a session without MFA: at
    # sign-in they are sent to enrol, and a session they already had ends at
    # its next refresh.
    mfa_required_roles: list[str] = Field(default_factory=list)
    # The name an authenticator app shows next to the code. Empty: the app name.
    mfa_issuer: str = ""
    mfa_token_minutes: int = 5
    # Wrong codes one sign-in may try before it has to start again.
    mfa_max_attempts: int = 5
    recovery_codes: int = 10

    # -- rate limits, when the cache plugin is on ------------------------
    rate_limit: bool = True
    login_limit_per_ip: int = 20
    login_limit_per_account: int = 10
    login_window_seconds: float = 300.0
    email_limit_per_ip: int = 5
    email_window_seconds: float = 900.0


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(LoginRequest):
    display_name: str | None = Field(default=None, max_length=200)


class PasswordChange(BaseModel):
    current_password: str | None = Field(default=None, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class EmailRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)


class TokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class PasswordReset(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=1024)


class MfaChallenge(BaseModel):
    """What a correct password gets when a second step is still missing."""

    # The account has MFA: send the code to /login/mfa with this token.
    mfa_required: bool = False
    # A role of this account requires MFA and it has none: enrol with this token.
    mfa_enrollment_required: bool = False
    mfa_token: str
    expires_in: int


class MfaLogin(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=200)
    # Six digits from the app, or a recovery code.
    code: str = Field(min_length=1, max_length=64)


class MfaSetup(BaseModel):
    # Signed in: the current password (unless the account has none).
    password: str | None = Field(default=None, max_length=1024)
    # Or, at sign-in for a role that requires MFA, the enrolment token.
    mfa_token: str | None = Field(default=None, max_length=200)


class MfaConfirm(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    mfa_token: str | None = Field(default=None, max_length=200)


class MfaProof(BaseModel):
    password: str | None = Field(default=None, max_length=1024)
    code: str = Field(min_length=1, max_length=64)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str | None = Field(default=None, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(default_factory=list)
    # The administrator vouches for the address: no verification email.
    email_verified: bool = False


class UserUpdate(BaseModel):
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)
    roles: list[str] | None = None


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None


#: The one answer a request for an email gets, whether an email went out or not.
ACCEPTED = {"status": "accepted"}


def _accepted() -> JSONResponse:
    return JSONResponse(ACCEPTED, status_code=status.HTTP_202_ACCEPTED)


class AccountsPlugin(Plugin):
    meta = PluginMeta(
        name="accounts",
        version="0.2.0",
        description="Users, password login, roles and permissions for the auth plugin.",
        requires=("database", "auth"),
        after=("database", "cache", "auth", "tenancy", "mail"),
        provides=("accounts",),
        default_enabled=False,
        extra="jfastframework[accounts]",
    )
    Settings = AccountsSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._engine: Any = None
        self._sessionmaker: Any = None
        self._issuer: Any = None
        self._store: Any = None
        self._mailer: Any = None
        self._emails: Any = None
        self._box: Any = None
        self._limiter: Any = None
        self._policies: dict[str, Any] = {}
        self._refresh_lifetime = timedelta(days=30)
        self._app_name = "jfast-service"
        self._logger: Any = None

    # -- wiring ------------------------------------------------------------

    def service(self, session: Any) -> AccountsService:
        from jfastframework.accounts.service import AccountsService

        settings: AccountsSettings = self.settings
        return AccountsService(
            session,
            min_password_length=settings.min_password_length,
            max_failed_logins=settings.max_failed_logins,
            lockout=timedelta(minutes=settings.lockout_minutes),
        )

    def mfa_service(self, session: Any) -> MfaService:
        from jfastframework.accounts.mfa import MfaService

        settings: AccountsSettings = self.settings
        if self._box is None:
            raise PluginError("two-factor authentication is off: set [plugin.accounts] mfa = true")
        return MfaService(
            session,
            box=self._box,
            issuer=settings.mfa_issuer or self._app_name,
            recovery_count=settings.recovery_codes,
        )

    @property
    def sends_mail(self) -> bool:
        settings: AccountsSettings = self.settings
        return settings.email_verification != "off" or settings.password_reset

    def _validate(self, ctx: AppContext) -> None:
        """Every setting that cannot work, refused at boot rather than at first use."""
        settings: AccountsSettings = self.settings
        if settings.email_verification not in EMAIL_VERIFICATION_MODES:  # pragma: no cover
            raise PluginError(
                f"[plugin.accounts] email_verification must be one of "
                f"{', '.join(EMAIL_VERIFICATION_MODES)}"
            )
        if self.sends_mail:
            wanted = []
            if settings.email_verification != "off":
                wanted.append(f'email_verification = "{settings.email_verification}"')
            if settings.password_reset:
                wanted.append("password_reset = true")
            if not ctx.has("mail"):
                raise PluginError(
                    f"[plugin.accounts] {' and '.join(wanted)} send email, and the 'mail' "
                    'plugin is not enabled: add "mail" to [plugins].enabled '
                    '(`pip install "jfastframework[mail]"`), or turn them off'
                )
            if not settings.frontend_url:
                raise PluginError(
                    f"[plugin.accounts] {' and '.join(wanted)} email a link to the frontend, "
                    f"and frontend_url is empty: set [plugin.accounts] frontend_url (or "
                    f"JFAST_ACCOUNTS_FRONTEND_URL) to where the frontend is served, such as "
                    f'"https://app.example.com"'
                )
        if settings.mfa_required_roles and not settings.mfa:
            raise PluginError(
                "[plugin.accounts] mfa_required_roles is set and mfa is off: nobody could "
                "enrol, so those roles could never sign in. Set mfa = true"
            )
        for name, value in (
            ("verification_token_minutes", settings.verification_token_minutes),
            ("reset_token_minutes", settings.reset_token_minutes),
            ("mfa_token_minutes", settings.mfa_token_minutes),
            ("mfa_max_attempts", settings.mfa_max_attempts),
            ("recovery_codes", settings.recovery_codes),
        ):
            if value <= 0:
                raise PluginError(f"[plugin.accounts] {name} must be positive")

    def _load_box(self) -> Any:
        """The key the TOTP secrets are encrypted with, or a refusal to start."""
        from jfastframework import encryption

        try:
            # The box encryption.configure() set, else JFAST_ENCRYPTION_KEYS.
            return encryption._box()
        except encryption.EncryptionConfigError as exc:
            raise PluginError(
                "[plugin.accounts] mfa = true stores each TOTP secret encrypted, and there "
                f"is no key to encrypt with: {exc}"
            ) from exc

    def _build_limiter(self, ctx: AppContext) -> None:
        from jfastframework.plugins.builtin.ratelimit import (
            RateLimitedError,
            RateLimiter,
            RateLimitPolicy,
            _rate_limited_handler,
        )

        settings: AccountsSettings = self.settings
        if not settings.rate_limit:
            return
        if not ctx.has("cache.client"):
            ctx.logger.info(
                "accounts: sign-in is not rate limited -- the cache plugin is off. The "
                "per-account lockout still applies."
            )
            return
        try:
            self._policies = {
                "login-ip": RateLimitPolicy(
                    limit=settings.login_limit_per_ip,
                    window=settings.login_window_seconds,
                    scope="accounts-login-ip",
                ),
                "login-account": RateLimitPolicy(
                    limit=settings.login_limit_per_account,
                    window=settings.login_window_seconds,
                    scope="accounts-login-account",
                ),
                "email-ip": RateLimitPolicy(
                    limit=settings.email_limit_per_ip,
                    window=settings.email_window_seconds,
                    scope="accounts-email-ip",
                ),
            }
        except ValueError as exc:
            raise PluginError(f"[plugin.accounts] invalid rate limit: {exc}") from exc
        # Its own limiter on the cache's connection: this works without the
        # ratelimit plugin, which is a service-wide policy with its own opinion.
        self._limiter = RateLimiter(
            ctx.require("cache.client"),
            prefix=f"{ctx.settings.app_name}:",
            fail_open=True,
        )
        # So a 429 carries Retry-After whether or not the ratelimit plugin is on.
        ctx.app.add_exception_handler(RateLimitedError, _rate_limited_handler)

    def register(self, ctx: AppContext) -> None:
        import argon2  # noqa: F401 - fail here, naming the extra, not at the first login

        if not ctx.has("db.sessionmaker"):
            raise PluginError("the accounts plugin needs the 'database' plugin enabled first")
        if not ctx.has("auth.issuer"):
            raise PluginError(
                "the accounts plugin signs users in, so auth has to mint tokens: set "
                '[plugin.auth] issue_tokens = true (with mode = "secret" or a private key)'
            )
        self._validate(ctx)
        settings: AccountsSettings = self.settings
        self._app_name = ctx.settings.app_name
        self._logger = ctx.logger
        self._engine = ctx.require("db.engine")
        self._sessionmaker = ctx.require("db.sessionmaker")
        self._issuer = ctx.require("auth.issuer")
        self._store = ctx.optional("auth.store")
        auth: Any = ctx.require("auth")
        self._refresh_lifetime = timedelta(days=auth.settings.refresh_lifetime_days)
        auth.on_refresh(self._grant_for)
        if settings.social_login and ctx.has("auth.providers"):
            auth.on_identity(self._sign_in_identity)

        if self.sends_mail:
            from jfastframework.accounts.emails import AccountEmails

            self._mailer = ctx.require("mail")
            self._emails = AccountEmails(
                self._mailer,
                app_name=self._app_name,
                frontend_url=settings.frontend_url,
                verify_path=settings.verify_email_path,
                reset_path=settings.reset_password_path,
                login_path=settings.login_path,
            )
        if settings.mfa:
            self._box = self._load_box()
        self._build_limiter(ctx)

        ctx.provide("accounts", self)
        ctx.app.include_router(self._account_router(), prefix=settings.prefix, tags=["accounts"])
        if self.sends_mail:
            ctx.app.include_router(self._email_router(), prefix=settings.prefix, tags=["accounts"])
        if settings.mfa:
            ctx.app.include_router(self._mfa_router(), prefix=settings.prefix, tags=["accounts"])
        ctx.app.include_router(
            self._admin_router(), prefix=settings.admin_prefix, tags=["accounts admin"]
        )

    async def startup(self, ctx: AppContext) -> None:
        from jfastframework.accounts.models import ACCOUNT_TABLES
        from jfastframework.db.framework import ensure_columns, ensure_tables

        await ensure_tables(self._engine, *ACCOUNT_TABLES)
        added = await ensure_columns(self._engine, "jfast_users")
        if added:
            ctx.logger.info("accounts: added columns to jfast_users", extra={"columns": added})
        await self._bootstrap_admin(ctx)

    async def _bootstrap_admin(self, ctx: AppContext) -> None:
        settings: AccountsSettings = self.settings
        if not settings.bootstrap_admin_email:
            return
        from jfastframework.accounts.service import normalize_email

        async with self._sessionmaker() as session, session.begin():
            service = self.service(session)
            email = normalize_email(settings.bootstrap_admin_email)
            if await service._row_by_email(email, None) is not None:
                return
            if settings.bootstrap_admin_password is None:
                raise PluginError(
                    "[plugin.accounts] bootstrap_admin_email is set and no such user exists, "
                    "but JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD is not: set it for the first "
                    "start, then remove it"
                )
            await service.ensure_role(
                "admin",
                tenant_id=None,
                permissions=[settings.admin_permission],
                description="Manages users and roles.",
            )
            await service.create_user(
                email,
                password=settings.bootstrap_admin_password.get_secret_value(),
                role_names=["admin"],
                # The operator wrote this address into the configuration.
                email_verified=True,
            )
        ctx.logger.info("accounts: created the bootstrap administrator", extra={"email": email})

    # -- policy ------------------------------------------------------------

    def requires_mfa(self, roles: tuple[str, ...] | list[str]) -> bool:
        settings: AccountsSettings = self.settings
        return bool(settings.mfa and set(roles) & set(settings.mfa_required_roles))

    def features(self) -> dict[str, Any]:
        """What this service has turned on: what a frontend should offer."""
        settings: AccountsSettings = self.settings
        return {
            "registration": settings.allow_registration,
            "email_verification": settings.email_verification,
            "password_reset": settings.password_reset,
            "mfa": settings.mfa,
            "min_password_length": settings.min_password_length,
        }

    async def _limit(self, request: Request, bucket: str, identity: str | None = None) -> None:
        """Spend one token from a bucket, or answer 429. Nothing to spend without cache."""
        if self._limiter is None:
            return
        from jfastframework.plugins.builtin.ratelimit import RateLimitedError, client_ip

        policy = self._policies[bucket]
        decision = await self._limiter.check(policy, identity or f"ip:{client_ip(request)}")
        if not decision.allowed:
            raise RateLimitedError(
                "too many attempts; wait a moment and try again",
                headers=decision.headers(),
                retry_after=decision.retry_after,
            )

    @staticmethod
    def _account_key(email: str, tenant: str | None) -> str:
        # Hashed: the bucket key lands in Redis, and an address is personal data.
        # Unnormalised on purpose when it is malformed -- it still names a bucket.
        address = email.strip().lower()
        return "acct:" + hashlib.sha256(f"{tenant or ''}|{address}".encode()).hexdigest()[:32]

    # -- auth hooks --------------------------------------------------------

    async def _grant_for(self, principal: Principal) -> Grant | None:
        from jfastframework.auth.principal import Grant

        async with self._sessionmaker() as session:
            service = self.service(session)
            row = await service._row(principal.subject, principal.tenant_id)
            if row is None or not row["is_active"]:
                return None
            # Signed out everywhere after this refresh token was minted.
            valid_after = row["sessions_valid_after"]
            issued_at = principal.claims.get("iat")
            if valid_after is not None and issued_at is not None:
                if valid_after.tzinfo is None:
                    valid_after = valid_after.replace(tzinfo=UTC)
                if int(issued_at) < int(valid_after.timestamp()):
                    return None
            user = await service.user_of_row(row)
        # A role that now requires MFA ends a session without it: the next
        # sign-in sends the user to enrol.
        if self.requires_mfa(user.roles) and not user.mfa_enabled:
            return None
        return Grant(scopes=user.permissions, roles=user.roles)

    async def _sign_in_identity(self, identity: Any, request: Request) -> Any:
        settings: AccountsSettings = self.settings
        tenant = _signin_tenant(request)
        async with self._sessionmaker() as session, session.begin():
            user = await self.service(session).sign_in_federated(
                federated_id=identity.federated_id,
                email=identity.email,
                email_verified=bool(identity.email_verified),
                display_name=identity.name,
                tenant_id=tenant,
                allow_signup=settings.allow_registration,
                default_roles=settings.default_roles,
            )
            if user is None:
                raise UnauthorizedError("there is no account for this sign-in")
            # A provider is the first factor. An account with a second one
            # still owes it, whichever way the first was given.
            return await self._after_first_factor(session, user)

    async def _issue(self, session: Any, user: User) -> Any:
        """A session for this user, recorded so it can be ended with all the others."""
        from jfastframework.accounts.sessions import record_session

        family = secrets.token_urlsafe(16)
        pair = await self._issuer.issue_pair(
            user.id,
            scopes=list(user.permissions),
            roles=list(user.roles),
            tenant_id=user.tenant_id,
            family=family,
        )
        await record_session(
            session, user_id=user.id, family=family, lifetime=self._refresh_lifetime
        )
        await self.service(session).touch_login(user.id)
        return pair

    async def _challenge(self, session: Any, user: User, *, enrol: bool) -> MfaChallenge:
        from jfastframework.accounts.onetime import MFA_ENROLL, MFA_LOGIN, OneTimeTokens

        settings: AccountsSettings = self.settings
        lifetime = timedelta(minutes=settings.mfa_token_minutes)
        token = await OneTimeTokens(session).issue(
            user.id, MFA_ENROLL if enrol else MFA_LOGIN, lifetime
        )
        return MfaChallenge(
            mfa_required=not enrol,
            mfa_enrollment_required=enrol,
            mfa_token=token,
            expires_in=int(lifetime.total_seconds()),
        )

    async def _after_first_factor(self, session: Any, user: User) -> Any:
        """A user whose password (or provider) checked out: a session, or the next step."""
        settings: AccountsSettings = self.settings
        if settings.email_verification == "required" and not user.email_verified:
            # Only now, after the password: saying "not verified" to someone
            # who does not know the password would tell them the account exists.
            raise ForbiddenError(
                "confirm your email address before signing in: follow the link we sent",
                code="email_not_verified",
            )
        if settings.mfa:
            if user.mfa_enabled:
                return await self._challenge(session, user, enrol=False)
            if self.requires_mfa(user.roles):
                return await self._challenge(session, user, enrol=True)
        return await self._issue(session, user)

    # -- mail, sent after the response ------------------------------------

    def _schedule_email(
        self,
        background: BackgroundTasks,
        kind: str,
        *,
        email: str,
        tenant: str | None,
    ) -> None:
        """Look the address up and email it -- after the response has gone.

        The response to "send me a link" is the same, and leaves at the same
        moment, whether or not the address has an account: the lookup, the
        token and the mail all happen after it. Anything else lets the
        response time say which addresses exist.
        """
        background.add_task(self._email_later, kind, email=email, tenant=tenant)

    async def _email_later(self, kind: str, *, email: str, tenant: str | None) -> None:
        from jfastframework.accounts.onetime import RESET_PASSWORD, VERIFY_EMAIL, OneTimeTokens

        settings: AccountsSettings = self.settings
        try:
            message = None
            async with self._sessionmaker() as session, session.begin():
                service = self.service(session)
                row = await service.row_for_email(email, tenant_id=tenant)
                if row is None or not row["is_active"]:
                    return
                if kind == "verify_email" and row["email_verified_at"] is not None:
                    return
                tokens = OneTimeTokens(session)
                if kind == "already_registered" and not settings.password_reset:
                    purpose = None
                    kind = "already_registered_no_reset"
                    minutes = 0
                elif kind == "verify_email":
                    purpose, minutes = VERIFY_EMAIL, settings.verification_token_minutes
                else:
                    purpose, minutes = RESET_PASSWORD, settings.reset_token_minutes
                token = None
                if purpose is not None:
                    last = await tokens.last_issued(row["id"], purpose)
                    cooldown = timedelta(seconds=settings.email_cooldown_seconds)
                    if last is not None and datetime.now(UTC) - last < cooldown:
                        return
                    await tokens.prune(row["id"])
                    token = await tokens.issue(row["id"], purpose, timedelta(minutes=minutes))
                message = self._emails.compose(
                    kind,
                    to=row["email"],
                    token=token,
                    minutes=minutes,
                    display_name=row["display_name"],
                )
            # After the commit: an email whose token was rolled back is a dead link.
            await self._mailer.send(message)
        except Exception as exc:  # noqa: BLE001 - after the response, nobody else will say
            # The type only: an exception from a mail backend can quote the message.
            self._logger.error(
                "accounts: could not send an email",
                extra={"kind": kind, "error": type(exc).__name__},
            )

    # -- routes ------------------------------------------------------------

    def _account_router(self) -> APIRouter:
        from jfastframework.plugins.builtin.auth import TokenPair, require_auth

        settings: AccountsSettings = self.settings
        router = APIRouter()
        plugin = self

        @router.get("/features", summary="Which account features this service has on")
        async def features() -> dict[str, Any]:
            return plugin.features()

        @router.post(
            "/login",
            response_model=TokenPair | MfaChallenge,
            summary="Sign in with email and password",
        )
        async def login(body: LoginRequest, request: Request, session: DbSession) -> Any:
            tenant = _signin_tenant(request)
            # Before the password is checked, and keyed on what was typed: the
            # answer is the same for an address with no account.
            await plugin._limit(request, "login-ip")
            await plugin._limit(request, "login-account", plugin._account_key(body.email, tenant))
            result = await plugin.service(session).authenticate(
                body.email, body.password, tenant_id=tenant
            )
            if not result.ok:
                request.app.state.jfast.logger.info(
                    "login refused", extra={"reason": result.reason, "tenant": tenant}
                )
                # Returned, not raised: raising would roll back the failure
                # count, and a lockout that is never saved is not a lockout.
                return problem_response(
                    UnauthorizedError("the email or password is not correct"), request
                )
            assert result.user is not None
            try:
                return await plugin._after_first_factor(session, result.user)
            except JFastError as exc:
                # Returned for the same reason: the cleared failure count stays.
                return problem_response(exc, request)

        if settings.allow_registration:

            @router.post(
                "/register",
                response_model=TokenPair | MfaChallenge,
                status_code=status.HTTP_201_CREATED,
                summary="Create an account",
                responses={202: {"description": "Check your email (verification required)"}},
            )
            async def register(
                body: RegisterRequest,
                request: Request,
                session: DbSession,
                background: BackgroundTasks,
            ) -> Any:
                from jfastframework.accounts.passwords import check_password
                from jfastframework.accounts.service import normalize_email

                tenant = _signin_tenant(request)
                await plugin._limit(request, "email-ip")
                service = plugin.service(session)
                address = normalize_email(body.email)
                service._check_password_policy(body.password)

                if settings.email_verification == "required":
                    # Nobody gets a session here, so the form need not say
                    # whether the address was taken -- and does not. The owner
                    # of a taken one hears about it by email instead.
                    if await service.row_for_email(address, tenant_id=tenant) is not None:
                        await check_password(body.password, None)  # the cost of a new hash
                        plugin._schedule_email(
                            background, "already_registered", email=address, tenant=tenant
                        )
                    else:
                        await service.create_user(
                            address,
                            password=body.password,
                            tenant_id=tenant,
                            display_name=body.display_name,
                            role_names=list(settings.default_roles),
                        )
                        plugin._schedule_email(
                            background, "verify_email", email=address, tenant=tenant
                        )
                    return _accepted()

                user = await service.create_user(
                    address,
                    password=body.password,
                    tenant_id=tenant,
                    display_name=body.display_name,
                    role_names=list(settings.default_roles),
                )
                if settings.email_verification == "optional":
                    plugin._schedule_email(background, "verify_email", email=address, tenant=tenant)
                return await plugin._after_first_factor(session, user)

        @router.get("/account", summary="The signed-in user, with roles and permissions")
        async def account(
            session: DbSession, principal: Principal = Depends(require_auth)
        ) -> dict[str, Any]:
            user = await plugin.service(session).get(
                principal.subject, tenant_id=principal.tenant_id
            )
            return user.public()

        @router.post(
            "/password", status_code=status.HTTP_204_NO_CONTENT, summary="Change your password"
        )
        async def change_password(
            body: PasswordChange,
            session: DbSession,
            principal: Principal = Depends(require_auth),
        ) -> Response:
            await plugin.service(session).change_password(
                principal.subject,
                tenant_id=principal.tenant_id,
                current=body.current_password,
                new=body.new_password,
            )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @router.post(
            "/logout/all",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Sign out of every session, this one included",
        )
        async def logout_everywhere(
            session: DbSession, principal: Principal = Depends(require_auth)
        ) -> Response:
            from jfastframework.accounts.sessions import revoke_all_sessions

            await plugin.service(session).get(principal.subject, tenant_id=principal.tenant_id)
            await revoke_all_sessions(session, plugin._store, user_id=principal.subject)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return router

    def _email_router(self) -> APIRouter:
        settings: AccountsSettings = self.settings
        router = APIRouter()
        plugin = self

        if settings.email_verification != "off":

            @router.post("/verify", summary="Confirm an email address with the emailed token")
            async def verify(
                body: TokenRequest, request: Request, session: DbSession
            ) -> dict[str, str]:
                from jfastframework.accounts.onetime import VERIFY_EMAIL, OneTimeTokens

                await plugin._limit(request, "login-ip")
                tokens = OneTimeTokens(session)
                row = await tokens.consume(body.token, VERIFY_EMAIL)
                if row is None:
                    raise ValidationError(
                        "this link is not valid: it was used, it expired, or it was mistyped",
                        code="token_invalid",
                    )
                await plugin.service(session).mark_email_verified(row["user_id"])
                await tokens.spend_all(row["user_id"], VERIFY_EMAIL)
                return {"status": "verified"}

            @router.post(
                "/verify/resend",
                status_code=status.HTTP_202_ACCEPTED,
                summary="Send the verification link again",
            )
            async def resend(
                body: EmailRequest, request: Request, background: BackgroundTasks
            ) -> Any:
                await plugin._limit(request, "email-ip")
                plugin._schedule_email(
                    background,
                    "verify_email",
                    email=body.email,
                    tenant=_signin_tenant(request),
                )
                return _accepted()

        if settings.password_reset:

            @router.post(
                "/password/forgot",
                status_code=status.HTTP_202_ACCEPTED,
                summary="Email a password-reset link",
            )
            async def forgot(
                body: EmailRequest, request: Request, background: BackgroundTasks
            ) -> Any:
                await plugin._limit(request, "email-ip")
                plugin._schedule_email(
                    background,
                    "reset_password",
                    email=body.email,
                    tenant=_signin_tenant(request),
                )
                return _accepted()

            @router.post(
                "/password/reset",
                status_code=status.HTTP_204_NO_CONTENT,
                summary="Set a new password with the emailed token; ends every session",
            )
            async def reset(body: PasswordReset, request: Request, session: DbSession) -> Response:
                from jfastframework.accounts.onetime import RESET_PASSWORD, OneTimeTokens
                from jfastframework.accounts.sessions import revoke_all_sessions

                await plugin._limit(request, "login-ip")
                service = plugin.service(session)
                tokens = OneTimeTokens(session)
                # The policy before the token is spent: a password that is
                # too short must not cost the user their link.
                service._check_password_policy(body.new_password)
                row = await tokens.consume(body.token, RESET_PASSWORD)
                if row is None:
                    raise ValidationError(
                        "this link is not valid: it was used, it expired, or it was mistyped",
                        code="token_invalid",
                    )
                user_id = row["user_id"]
                await service.set_password(
                    user_id, body.new_password, failed_logins=0, locked_until=None
                )
                # The link reached the inbox, which is what verification proves.
                await service.mark_email_verified(user_id)
                await tokens.spend_all(user_id, RESET_PASSWORD)
                # Whoever knew the old password -- or held a session made with
                # it -- is out, the access tokens they hold included.
                await revoke_all_sessions(session, plugin._store, user_id=user_id)
                return Response(status_code=status.HTTP_204_NO_CONTENT)

        return router

    def _mfa_router(self) -> APIRouter:
        from jfastframework.plugins.builtin.auth import TokenPair, optional_auth, require_auth

        settings: AccountsSettings = self.settings
        router = APIRouter()
        plugin = self

        async def signed_in_row(
            session: Any, principal: Principal | None, password: str | None
        ) -> Any:
            """The caller's row, with the password checked when the account has one."""
            from jfastframework.accounts.passwords import check_password

            if principal is None:
                raise UnauthorizedError("sign in first")
            service = plugin.service(session)
            row = await service._row(principal.subject, principal.tenant_id)
            if row is None or not row["is_active"]:
                raise UnauthorizedError("sign in first")
            if row["password_hash"] is not None:
                ok, _ = await check_password(password or "", row["password_hash"])
                if not ok:
                    await service.record_failure(row)
                    return None
            return row

        async def enrolment_row(session: Any, token: str) -> Any:
            from jfastframework.accounts.onetime import MFA_ENROLL, OneTimeTokens

            found = await OneTimeTokens(session).find(token, MFA_ENROLL)
            if found is None:
                raise UnauthorizedError(
                    "this sign-in has expired; sign in again", code="mfa_token_invalid"
                )
            row = await plugin.service(session).row_by_id(found["user_id"])
            if row is None or not row["is_active"]:
                raise UnauthorizedError(
                    "this sign-in has expired; sign in again", code="mfa_token_invalid"
                )
            return row

        wrong_password = ValidationError("the password is not correct", code="password_invalid")
        wrong_code = ValidationError("the code is not correct", code="mfa_code_invalid")

        @router.post(
            "/login/mfa", response_model=TokenPair, summary="Second step: the code from the app"
        )
        async def login_mfa(body: MfaLogin, request: Request, session: DbSession) -> Any:
            from jfastframework.accounts.onetime import MFA_LOGIN, OneTimeTokens

            await plugin._limit(request, "login-ip")
            service = plugin.service(session)
            tokens = OneTimeTokens(session)
            expired = UnauthorizedError(
                "this sign-in has expired; sign in again", code="mfa_token_invalid"
            )
            found = await tokens.find(body.mfa_token, MFA_LOGIN)
            if found is None:
                return problem_response(expired, request)
            row = await service.row_by_id(found["user_id"])
            tenant = _signin_tenant(request)
            if row is None or not row["is_active"] or row["tenant_id"] != tenant:
                return problem_response(expired, request)
            await plugin._limit(request, "login-account", f"user:{row['id']}")
            locked = row["locked_until"]
            if locked is not None and locked.tzinfo is None:
                locked = locked.replace(tzinfo=UTC)
            kind = None
            if locked is None or locked <= datetime.now(UTC):
                kind = await plugin.mfa_service(session).check(row, body.code)
            if kind is None:
                # Both counted, and returned rather than raised so both stick:
                # the token dies after mfa_max_attempts, the account locks
                # after max_failed_logins across every sign-in.
                await tokens.fail(found, max_attempts=settings.mfa_max_attempts)
                await service.record_failure(row)
                return problem_response(
                    UnauthorizedError("the code is not correct", code="mfa_code_invalid"),
                    request,
                )
            if await tokens.consume(body.mfa_token, MFA_LOGIN) is None:
                return problem_response(expired, request)
            if kind == "recovery":
                request.app.state.jfast.logger.info(
                    "signed in with a recovery code", extra={"user": row["id"]}
                )
            return await plugin._issue(session, await service.user_of_row(row))

        @router.post("/mfa/setup", summary="Start enrolling an authenticator app")
        async def setup(
            body: MfaSetup,
            request: Request,
            session: DbSession,
            principal: Principal | None = Depends(optional_auth),
        ) -> Any:
            if body.mfa_token:
                row = await enrolment_row(session, body.mfa_token)
            else:
                row = await signed_in_row(session, principal, body.password)
                if row is None:
                    return problem_response(wrong_password, request)
            return await plugin.mfa_service(session).begin(row)

        @router.post("/mfa/confirm", summary="Turn MFA on with a code; returns recovery codes")
        async def confirm(
            body: MfaConfirm,
            session: DbSession,
            principal: Principal | None = Depends(optional_auth),
        ) -> dict[str, Any]:
            from jfastframework.accounts.onetime import MFA_ENROLL, OneTimeTokens

            service = plugin.service(session)
            if body.mfa_token:
                row = await enrolment_row(session, body.mfa_token)
            else:
                if principal is None:
                    raise UnauthorizedError("sign in first")
                row = await service._row(principal.subject, principal.tenant_id)
                if row is None:
                    raise UnauthorizedError("sign in first")
            codes = await plugin.mfa_service(session).confirm(row, body.code)
            result: dict[str, Any] = {"recovery_codes": codes}
            if body.mfa_token:
                if await OneTimeTokens(session).consume(body.mfa_token, MFA_ENROLL) is None:
                    raise UnauthorizedError(
                        "this sign-in has expired; sign in again", code="mfa_token_invalid"
                    )
                fresh = await service.row_by_id(row["id"])
                result["session"] = await plugin._issue(session, await service.user_of_row(fresh))
            return result

        @router.post(
            "/mfa/disable",
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Turn MFA off (password and a code)",
        )
        async def disable(
            body: MfaProof,
            request: Request,
            session: DbSession,
            principal: Principal = Depends(require_auth),
        ) -> Response:
            row = await signed_in_row(session, principal, body.password)
            if row is None:
                return problem_response(wrong_password, request)
            service = plugin.service(session)
            user = await service.user_of_row(row)
            if plugin.requires_mfa(user.roles):
                raise ForbiddenError(
                    "a role you hold requires two-factor authentication",
                    code="mfa_required_by_role",
                )
            mfa = plugin.mfa_service(session)
            if await mfa.check(row, body.code) is None:
                await service.record_failure(row)
                return problem_response(wrong_code, request)
            await mfa.disable(row["id"])
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @router.post("/mfa/recovery-codes", summary="Replace the recovery codes")
        async def new_codes(
            body: MfaProof,
            request: Request,
            session: DbSession,
            principal: Principal = Depends(require_auth),
        ) -> Any:
            row = await signed_in_row(session, principal, body.password)
            if row is None:
                return problem_response(wrong_password, request)
            mfa = plugin.mfa_service(session)
            if await mfa.check(row, body.code) is None:
                await plugin.service(session).record_failure(row)
                return problem_response(wrong_code, request)
            return {"recovery_codes": await mfa.regenerate_codes(row["id"])}

        return router

    def _admin_router(self) -> APIRouter:
        from jfastframework.plugins.builtin.auth import require_scopes

        settings: AccountsSettings = self.settings
        admin = require_scopes(settings.admin_permission)
        router = APIRouter()
        plugin = self

        @router.get("/users", summary="Users in your tenant")
        async def list_users(
            session: DbSession,
            principal: Principal = Depends(admin),
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0),
        ) -> dict[str, Any]:
            found, total = await plugin.service(session).list_users(
                tenant_id=principal.tenant_id, limit=limit, offset=offset
            )
            return {"items": [u.public() for u in found], "total": total}

        @router.post("/users", status_code=status.HTTP_201_CREATED, summary="Create a user")
        async def create_user(
            body: UserCreate,
            session: DbSession,
            background: BackgroundTasks,
            principal: Principal = Depends(admin),
        ) -> dict[str, Any]:
            user = await plugin.service(session).create_user(
                body.email,
                password=body.password,
                tenant_id=principal.tenant_id,
                display_name=body.display_name,
                role_names=body.roles,
                email_verified=body.email_verified,
            )
            if settings.email_verification != "off" and not body.email_verified:
                plugin._schedule_email(
                    background, "verify_email", email=user.email, tenant=user.tenant_id
                )
            return user.public()

        @router.patch("/users/{user_id}", summary="Activate, deactivate or re-role a user")
        async def update_user(
            user_id: str,
            body: UserUpdate,
            session: DbSession,
            principal: Principal = Depends(admin),
        ) -> dict[str, Any]:
            user = await plugin.service(session).update_user(
                user_id,
                tenant_id=principal.tenant_id,
                is_active=body.is_active,
                display_name=body.display_name,
                role_names=body.roles,
            )
            if body.is_active is False:
                from jfastframework.accounts.sessions import revoke_all_sessions

                # Now, not at the next refresh: the access tokens stop too.
                await revoke_all_sessions(session, plugin._store, user_id=user.id)
            return user.public()

        if settings.mfa:

            @router.delete(
                "/users/{user_id}/mfa",
                status_code=status.HTTP_204_NO_CONTENT,
                summary="Turn a user's MFA off: lost phone and lost recovery codes",
            )
            async def reset_mfa(
                user_id: str, session: DbSession, principal: Principal = Depends(admin)
            ) -> Response:
                user = await plugin.service(session).get(user_id, tenant_id=principal.tenant_id)
                await plugin.mfa_service(session).disable(user.id)
                return Response(status_code=status.HTTP_204_NO_CONTENT)

        @router.get("/roles", summary="Roles in your tenant, with their permissions")
        async def list_roles(
            session: DbSession, principal: Principal = Depends(admin)
        ) -> list[dict[str, Any]]:
            return await plugin.service(session).list_roles(tenant_id=principal.tenant_id)

        @router.post("/roles", status_code=status.HTTP_201_CREATED, summary="Create a role")
        async def create_role(
            body: RoleCreate, session: DbSession, principal: Principal = Depends(admin)
        ) -> dict[str, Any]:
            return await plugin.service(session).create_role(
                body.name,
                tenant_id=principal.tenant_id,
                permissions=body.permissions,
                description=body.description,
            )

        @router.patch("/roles/{role_id}", summary="Change a role's permissions")
        async def update_role(
            role_id: str,
            body: RoleUpdate,
            session: DbSession,
            principal: Principal = Depends(admin),
        ) -> dict[str, Any]:
            return await plugin.service(session).update_role(
                role_id,
                tenant_id=principal.tenant_id,
                permissions=body.permissions,
                description=body.description,
            )

        @router.delete(
            "/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a role"
        )
        async def delete_role(
            role_id: str, session: DbSession, principal: Principal = Depends(admin)
        ) -> Response:
            await plugin.service(session).delete_role(role_id, tenant_id=principal.tenant_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return router

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._sessionmaker is None:
            return HealthReport.fail("accounts not initialised")
        return HealthReport.ok(
            "accounts ready",
            **self.features(),
            login_rate_limited=self._limiter is not None,
        )
