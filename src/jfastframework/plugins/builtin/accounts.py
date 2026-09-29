"""Accounts: the user store and login that the ``auth`` plugin leaves to you.

    [plugins]
    enabled = ["database", "cache", "auth", "accounts"]

    [plugin.auth]
    mode = "secret"
    issue_tokens = true

    [plugin.accounts]
    allow_registration = false
    bootstrap_admin_email = "admin@example.com"   # password from the environment

Mounts, under ``[plugin.accounts] prefix`` (``/auth``):

* ``POST /auth/login`` -- email and password in, a token pair out;
* ``POST /auth/register`` -- only when ``allow_registration`` is on;
* ``GET /auth/account`` and ``POST /auth/password`` -- the signed-in user's own;

and under ``admin_prefix`` (``/accounts``), for holders of ``accounts:admin``:
users and roles, within the administrator's own tenant.

It also registers two ``auth`` hooks: ``on_refresh`` re-reads a user's roles
at every refresh, so a permission taken away or an account deactivated ends
the session at its next refresh instead of at its end; and, when ``auth`` has
social providers, ``on_identity`` turns a verified identity into a user.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict

# At module level, not inside the router builders: FastAPI resolves a route's
# annotations against the module's globals, so a type imported locally is a
# name it cannot find.
from jfastframework.auth.principal import Principal
from jfastframework.errors import PluginError, UnauthorizedError, problem_response
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings
from jfastframework.plugins.builtin.database import DbSession

if TYPE_CHECKING:
    from jfastframework.accounts.service import AccountsService
    from jfastframework.auth.principal import Grant
    from jfastframework.context import AppContext


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


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegisterRequest(LoginRequest):
    display_name: str | None = Field(default=None, max_length=200)


class PasswordChange(BaseModel):
    current_password: str | None = Field(default=None, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str | None = Field(default=None, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(default_factory=list)


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


class AccountsPlugin(Plugin):
    meta = PluginMeta(
        name="accounts",
        version="0.1.0",
        description="Users, password login, roles and permissions for the auth plugin.",
        requires=("database", "auth"),
        after=("database", "cache", "auth", "tenancy"),
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

    def register(self, ctx: AppContext) -> None:
        import argon2  # noqa: F401 - fail here, naming the extra, not at the first login

        if not ctx.has("db.sessionmaker"):
            raise PluginError("the accounts plugin needs the 'database' plugin enabled first")
        if not ctx.has("auth.issuer"):
            raise PluginError(
                "the accounts plugin signs users in, so auth has to mint tokens: set "
                '[plugin.auth] issue_tokens = true (with mode = "secret" or a private key)'
            )
        self._engine = ctx.require("db.engine")
        self._sessionmaker = ctx.require("db.sessionmaker")
        self._issuer = ctx.require("auth.issuer")
        auth: Any = ctx.require("auth")
        auth.on_refresh(self._grant_for)
        if self.settings.social_login and ctx.has("auth.providers"):
            auth.on_identity(self._sign_in_identity)

        ctx.provide("accounts", self)
        settings: AccountsSettings = self.settings
        ctx.app.include_router(self._account_router(), prefix=settings.prefix, tags=["accounts"])
        ctx.app.include_router(
            self._admin_router(), prefix=settings.admin_prefix, tags=["accounts admin"]
        )

    async def startup(self, ctx: AppContext) -> None:
        from jfastframework.accounts.models import ACCOUNT_TABLES
        from jfastframework.db.framework import ensure_tables

        await ensure_tables(self._engine, *ACCOUNT_TABLES)
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
            )
        ctx.logger.info("accounts: created the bootstrap administrator", extra={"email": email})

    # -- auth hooks --------------------------------------------------------

    async def _grant_for(self, principal: Principal) -> Grant | None:
        from jfastframework.auth.principal import Grant

        async with self._sessionmaker() as session:
            user = await self.service(session).find(
                principal.subject, tenant_id=principal.tenant_id
            )
        if user is None or not user.is_active:
            return None
        return Grant(scopes=user.permissions, roles=user.roles)

    async def _sign_in_identity(self, identity: Any, request: Request) -> Any:
        settings: AccountsSettings = self.settings
        tenant = getattr(request.state, "tenant_id", None)
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
        return await self._tokens(user)

    async def _tokens(self, user: Any) -> Any:
        return await self._issuer.issue_pair(
            user.id,
            scopes=list(user.permissions),
            roles=list(user.roles),
            tenant_id=user.tenant_id,
        )

    # -- routes ------------------------------------------------------------

    def _account_router(self) -> APIRouter:
        from jfastframework.plugins.builtin.auth import TokenPair, require_auth

        settings: AccountsSettings = self.settings
        router = APIRouter()
        plugin = self

        @router.post("/login", response_model=TokenPair, summary="Sign in with email and password")
        async def login(body: LoginRequest, request: Request, session: DbSession) -> Any:
            tenant = getattr(request.state, "tenant_id", None)
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
            return await plugin._tokens(result.user)

        if settings.allow_registration:

            @router.post(
                "/register",
                response_model=TokenPair,
                status_code=status.HTTP_201_CREATED,
                summary="Create an account and sign in",
            )
            async def register(body: RegisterRequest, request: Request, session: DbSession) -> Any:
                user = await plugin.service(session).create_user(
                    body.email,
                    password=body.password,
                    tenant_id=getattr(request.state, "tenant_id", None),
                    display_name=body.display_name,
                    role_names=list(settings.default_roles),
                )
                return await plugin._tokens(user)

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
            body: UserCreate, session: DbSession, principal: Principal = Depends(admin)
        ) -> dict[str, Any]:
            user = await plugin.service(session).create_user(
                body.email,
                password=body.password,
                tenant_id=principal.tenant_id,
                display_name=body.display_name,
                role_names=body.roles,
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
            return user.public()

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
        return HealthReport.ok("accounts ready", registration=self.settings.allow_registration)
