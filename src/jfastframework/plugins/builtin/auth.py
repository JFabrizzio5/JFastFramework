"""JWT authentication.

    [plugins]
    enabled = ["observability", "auth"]

    [plugin.auth]
    mode = "jwks"                                   # jwks | public_key | secret
    jwks_url = "https://id.example.com/.well-known/jwks.json"
    issuer = "https://id.example.com/"
    audience = "billing"
    algorithms = ["RS256"]

Then guard a route:

    from jfastframework.auth import Principal, require_scopes

    @router.post("/invoices")
    async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
        ...

**What this plugin does and does not do.** It verifies tokens, and it can mint
them. It does not know who your users are: the ``accounts`` plugin does, and
mounts the login route. A service with a user store of its own calls
`auth.issuer` from its own login route instead.

The security decisions are documented where they are made -- see
``jfastframework.auth.tokens`` for algorithm pinning and claim verification,
``jfastframework.auth.jwks`` for key rotation, and
``jfastframework.auth.store`` for revocation.

Requires: ``pip install jfastframework[auth]``
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from jfastframework.auth.jwks import JWKSClient, JWKSError
from jfastframework.auth.principal import Grant, Principal, principal_var
from jfastframework.auth.store import MemoryTokenStore, RedisTokenStore, TokenStore
from jfastframework.auth.tokens import (
    SYMMETRIC_ALGORITHMS,
    TokenClaims,
    TokenError,
    issue,
    verify,
)
from jfastframework.errors import (
    ForbiddenError,
    NotFoundError,
    PluginError,
    ServiceUnavailableError,
    UnauthorizedError,
    problem_response,
)
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.auth.oidc import OIDCIdentity, OIDCProvider
    from jfastframework.context import AppContext

# What the application does once a provider has vouched for someone. It
# returns whatever the browser should get back: a TokenPair, a dict, or a
# RedirectResponse to the frontend.
IdentityHandler = Callable[["OIDCIdentity", Request], Awaitable[Any]]

# What the application says a session may still do, asked at every refresh.
# Returning None revokes the session family, access token included.
RefreshResolver = Callable[[Principal], Awaitable[Grant | None]]

logger = logging.getLogger("jfast.auth")

MODES = ("jwks", "public_key", "secret")

# Claim names this issuer writes into its own tokens. Deliberately not the
# configurable scope/roles claims: these are rotation bookkeeping and must
# never come back through verify() as authorization.
FAMILY_CLAIM = "fam"
GRANT_CLAIM = "grt"


class RevocationUnavailableError(ServiceUnavailableError):
    """The revocation store did not answer and ``revocation_fail_open`` is off."""

    title = "Revocation Store Unavailable"


# The shortest HMAC key this plugin accepts in production: RFC 7518 3.2 asks
# for a key at least as long as the hash, 256 bits for HS256.
MIN_SECRET_BYTES = 32


def _as_response(result: Any) -> Response:
    """Whatever the on_identity handler returned, as a response."""
    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse

    if isinstance(result, Response):
        return result
    return JSONResponse(jsonable_encoder(result))


class AuthSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_AUTH_", env_file=".env", extra="ignore")

    # jwks       : fetch public keys from an issuer. The right default for
    #              more than one service -- no shared secret to leak.
    # public_key : a pinned PEM. Same trust model, no network dependency.
    # secret     : HMAC. Simple, and every holder can also *mint* tokens, so
    #              it does not belong between services that do not trust
    #              each other equally.
    mode: str = "jwks"
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])

    jwks_url: str = ""
    jwks_cache_seconds: int = 3600
    # Deadline, retry and breaker for the key fetch. The reasoning for each
    # number is on `JWKSClient`; they are here so an issuer known to be slow
    # can be given more room without a code change.
    jwks_timeout: float = 5.0
    jwks_attempts: int = 2
    jwks_breaker_failures: int = 3
    jwks_breaker_cool_down: float = 30.0
    public_key: str = ""
    secret: SecretStr | None = None

    issuer: str = ""
    audience: str = ""
    # Clock skew allowance. Seconds, not minutes: a generous leeway is extra
    # life for a stolen token.
    leeway: int = 30

    scope_claim: str = "scope"
    roles_claim: str = "roles"
    tenant_claim: str = "tenant_id"

    # Issuance, for a service that owns its own login.
    issue_tokens: bool = False
    access_lifetime_minutes: int = 15
    refresh_lifetime_days: int = 30
    # How long after a rotation the token it replaced is still told apart from
    # a replay. Two tabs of one browser refresh at the same instant and one of
    # them loses; without this window the loser's attempt reads as a theft and
    # revokes the session both tabs were sharing.
    #
    # Set 0 for strict reuse detection, and accept that a normal browser can
    # end its own session. Longer than a request round trip buys nothing and
    # is that much longer a stolen token can be replayed unnoticed.
    refresh_grace_seconds: int = 10
    # Mounts /auth/refresh and /auth/logout. Not /auth/login: this plugin has
    # no user store. The accounts plugin mounts that one.
    mount_router: bool = True
    prefix: str = "/auth"

    # Reject a token whose jti has been revoked. Costs one store lookup per
    # authenticated request.
    check_revocation: bool = True
    # What a revocation lookup does when the shared store (Redis) does not
    # answer. Open: the token is accepted without the check, a warning is
    # logged and /ready reports auth degraded -- the same trade the rate
    # limiter makes, and the one the health check already promised by calling
    # the store non-critical. The window is bounded: access tokens live
    # `access_lifetime_minutes`. Closed: every authenticated request answers
    # 503 until the store is back. Choose closed where a logout that does not
    # take effect for a few minutes is worse than an outage.
    revocation_fail_open: bool = True

    # Social login. One entry per provider:
    #
    #   [plugin.auth.providers.google]
    #   client_id = "...apps.googleusercontent.com"
    #   client_secret = "${GOOGLE_CLIENT_SECRET}"
    #   redirect_uri = "https://app.example.com/auth/google/callback"
    #
    # `google`, `microsoft` and `github` need nothing else; any other name
    # must also give issuer, jwks_uri, authorization_endpoint, token_endpoint.
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # The state and nonce survive the round trip to the provider in a cookie.
    # Not a session: there is no session yet at that point in the flow.
    oidc_cookie_name: str = "jfast_oidc"
    oidc_cookie_seconds: int = 600


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class TokenIssuer:
    """Mints tokens. Call it from your own login route.

    Refresh tokens are rotated: every refresh returns a new one and invalidates
    the one presented. Presenting a used refresh token means either a client
    retry or a replayed stolen token -- indistinguishable from here, so the
    whole family is revoked and the user has to log in again. Losing a session
    is a much smaller cost than not noticing a theft.

    The exception is ``refresh_grace``, and it exists because one case *is*
    distinguishable: a second request arriving within seconds of the rotation
    it lost is the same client, not a thief who happened to strike inside that
    window. It is refused, and the family survives.

    A family is one session, not one person: it is random per login, and both
    tokens of the pair carry it. Keying it on the subject would make every
    revocation reach every device that person has, and their next login too.
    """

    def __init__(
        self,
        *,
        key: Any,
        algorithm: str,
        issuer: str,
        audience: str,
        access_lifetime: timedelta,
        refresh_lifetime: timedelta,
        store: TokenStore,
        claims: TokenClaims,
        resolve_grant: RefreshResolver | None = None,
        refresh_grace: timedelta = timedelta(seconds=10),
    ) -> None:
        self._key = key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_lifetime = access_lifetime
        self._refresh_lifetime = refresh_lifetime
        self._store = store
        self._claims = claims
        self._resolve_grant = resolve_grant
        self._refresh_grace = max(int(refresh_grace.total_seconds()), 0)

    def on_refresh(self, handler: RefreshResolver) -> RefreshResolver:
        """Register what a session may still do, re-read at every refresh.

            @issuer.on_refresh
            async def rights(principal):
                user = await users.get(principal.subject)
                return Grant(scopes=tuple(user.scopes)) if user.active else None

        Without a hook the refresh token's own grant is carried forward, which
        means a permission taken away today survives until the session ends.
        With one, the application decides -- and returning ``None`` revokes the
        session family, so the access token already in the client's hands stops
        working too instead of running out its lifetime.
        """
        self._resolve_grant = handler
        return handler

    async def issue_pair(
        self,
        subject: str,
        *,
        scopes: list[str] | None = None,
        roles: list[str] | None = None,
        tenant_id: str | None = None,
        family: str | None = None,
    ) -> TokenPair:
        # The family ties every refresh in a session together, so detecting one
        # replay can end all of them -- and only them. It is random, never the
        # subject: a family keyed on the person is revoked for the person, so
        # one logout would end every session they have and every session they
        # open next, for the whole refresh lifetime.
        refresh_family = family or secrets.token_urlsafe(16)

        # The access token carries the family because /auth/logout has nothing
        # else to end the session with.
        access, _, expires_at = issue(
            subject,
            key=self._key,
            algorithm=self._algorithm,
            lifetime=self._access_lifetime,
            audience=self._audience or None,
            issuer=self._issuer or None,
            scopes=scopes,
            roles=roles,
            tenant_id=tenant_id,
            token_type="access",
            extra={FAMILY_CLAIM: refresh_family},
            claims=self._claims,
        )

        # The grant rides along so that a rotation mints an access token with
        # the same rights instead of an empty one. It is written under a claim
        # of this issuer's own, never the configured scope claim: verify() must
        # not read it back as authorization. Always written, even empty: its
        # presence is what tells a rotation that this token is new enough.
        refresh, refresh_id, _ = issue(
            subject,
            key=self._key,
            algorithm=self._algorithm,
            lifetime=self._refresh_lifetime,
            audience=self._audience or None,
            issuer=self._issuer or None,
            tenant_id=tenant_id,
            token_type="refresh",
            extra={
                FAMILY_CLAIM: refresh_family,
                GRANT_CLAIM: {"scopes": list(scopes or ()), "roles": list(roles or ())},
            },
            claims=self._claims,
        )
        await self._store.remember_refresh(
            refresh_id,
            family=refresh_family,
            ttl=int(self._refresh_lifetime.total_seconds()),
        )

        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=int((expires_at - datetime.now(UTC)).total_seconds()),
        )

    async def rotate(self, principal: Principal) -> TokenPair:
        # Pure-token checks first: a request that can never succeed must not
        # burn a still-usable refresh token on the way to failing.
        token_id = principal.token_id

        if principal.claims.get("typ") != "refresh":
            raise UnauthorizedError("an access token cannot be used to refresh")
        if token_id is None:
            raise UnauthorizedError("refresh token has no id")

        family = principal.claims.get(FAMILY_CLAIM)
        if not family:
            # No fallback to the subject: that fallback would let a session
            # revocation outlive the session.
            raise UnauthorizedError("refresh token carries no session family")
        family = str(family)

        raw_grant = principal.claims.get(GRANT_CLAIM)
        carried: dict[str, Any] | None = raw_grant if isinstance(raw_grant, dict) else None
        if self._resolve_grant is None and carried is None:
            # No grant claim and no hook to resolve one. Rotating it would hand
            # back an access token with no scopes at all, and the 403 that
            # follows would land nowhere near the cause.
            raise UnauthorizedError("this refresh token predates scope-preserving rotation")

        if await self._store.is_family_revoked(family):
            raise UnauthorizedError("this session has been revoked")

        # Before the consume, deliberately. The hook reads the application's
        # own database, and a database that blinks must not cost the session:
        # a token consumed before the hook failed makes the client's natural
        # retry a replay, and a replay costs the family for the whole refresh
        # lifetime. Nothing is consumed here, so that retry is just a retry.
        #
        # The price is that a replayed token runs the hook once before the
        # consume refuses it. Once only: that consume revokes the family, and
        # the check above then turns every later attempt away first.
        grant = await self._grant_for(principal, family=family, carried=carried)

        outcome = await self._store.rotate_refresh(
            token_id,
            family=family,
            ttl=int(self._refresh_lifetime.total_seconds()),
            grace=self._refresh_grace,
        )
        if outcome == "raced":
            # The same client, seconds behind its own winning request. Refused
            # -- there is one live refresh token and the winner has it -- but
            # not treated as a theft, because treating it as one would let a
            # browser with two tabs end the session it was sharing.
            logger.info(
                "refresh token already rotated within the grace window",
                extra={"subject": principal.subject, "family": family},
            )
            raise UnauthorizedError("this refresh token has already been rotated")
        if outcome == "replayed":
            # Kill the family rather than guess which holder is real.
            await self._store.revoke_family(family, ttl=int(self._refresh_lifetime.total_seconds()))
            logger.warning(
                "refresh token replay; family revoked",
                extra={"subject": principal.subject, "family": family},
            )
            raise UnauthorizedError("this session has been revoked")

        return await self.issue_pair(
            principal.subject,
            scopes=list(grant.scopes),
            roles=list(grant.roles),
            tenant_id=principal.tenant_id,
            family=family,
        )

    async def _grant_for(
        self, principal: Principal, *, family: str, carried: dict[str, Any] | None
    ) -> Grant:
        """What this session may do now, from the hook or from the token."""
        if self._resolve_grant is None:
            # Never None here: without a hook, rotate() rejected a token that
            # carries no grant before reaching this.
            assert carried is not None
            return Grant(
                scopes=tuple(str(s) for s in carried.get("scopes", ())),
                roles=tuple(str(r) for r in carried.get("roles", ())),
            )

        try:
            resolved = await self._resolve_grant(principal)
        except Exception:
            # Logged here rather than left to the 500 handler, which cannot say
            # which half of a refresh failed or that the session is intact.
            logger.warning(
                "on_refresh hook failed; nothing consumed, the refresh token is still usable",
                extra={"subject": principal.subject, "family": family},
            )
            raise

        if resolved is None:
            # The application says this session is over, so end it rather than
            # only refusing this one request: the access token the client is
            # already holding is otherwise good for its full lifetime, and a
            # banned user goes on working for every minute of it.
            await self._store.revoke_family(family, ttl=int(self._refresh_lifetime.total_seconds()))
            logger.info(
                "on_refresh declined; family revoked",
                extra={"subject": principal.subject, "family": family},
            )
            raise UnauthorizedError("this session is no longer valid")
        return resolved


class AuthMiddleware:
    """Verify a bearer token when one is present, and never reject here.

    Rejection is the dependency's job: a public endpoint must keep working
    with a bad token in the header, and an authenticated one must fail with
    the right status. Doing it here would make every route private.

    Verifying anyway means the access log and every log line inside the
    request carry the caller's identity, including on public routes.

    Plain ASGI rather than ``BaseHTTPMiddleware``: same work, without a task
    group and a memory stream per request.
    """

    def __init__(self, app: ASGIApp, *, plugin: AuthPlugin) -> None:
        self.app = app
        self._plugin = plugin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = ""
        for key, value in scope["headers"]:
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        principal: Principal | None = None

        if header.lower().startswith("bearer "):
            candidate: Principal | None = None
            try:
                candidate = await self._plugin.verify_token(header[7:].strip())
            except RevocationUnavailableError as exc:
                # Middleware runs outside the exception handlers, so the 503
                # is written here rather than raised.
                response = problem_response(exc, Request(scope, receive))
                await response(scope, receive, send)
                return
            except (TokenError, JWKSError) as exc:
                # The reason belongs in the log, never in the response: an
                # attacker learning *why* a token failed gets free
                # reconnaissance.
                logger.info("token rejected", extra={"reason": str(exc)})
            if candidate is not None and candidate.claims.get("typ") == "refresh":
                # A refresh token verifies like any other -- same key, same
                # issuer, same audience -- so without this check a 30-day
                # token opens a session anywhere an access token would.
                # Only an explicit "refresh" is refused: a token from an
                # external issuer carries no typ at all.
                logger.info("refresh token used as bearer", extra={"subject": candidate.subject})
            else:
                principal = candidate

        state = scope.setdefault("state", {})
        state["principal"] = principal
        token = principal_var.set(principal)
        tenant_token = None
        if principal is not None and principal.tenant_id:
            # A signed claim beats the X-Tenant-ID header the observability
            # plugin would otherwise trust. Anyone can send that header.
            state["tenant_id"] = principal.tenant_id
            from jfastframework.plugins.builtin.observability import tenant_id_var

            tenant_token = tenant_id_var.set(principal.tenant_id)

        try:
            await self.app(scope, receive, send)
        finally:
            principal_var.reset(token)
            if tenant_token is not None:
                from jfastframework.plugins.builtin.observability import tenant_id_var

                tenant_id_var.reset(tenant_token)


# -- dependencies -------------------------------------------------------


# Every dependency below is ``async def`` on purpose. FastAPI runs a plain
# ``def`` dependency in the threadpool, and that hop cost 75-85 us per request
# in the 0.1.0a10 benchmark -- more than every middleware in the stack put
# together -- for a function that only reads request.state. Call them through
# Depends; in plain code read ``request.state.principal`` or use
# ``principal_of``.


def principal_of(request: Request) -> Principal:
    """The verified caller, or UnauthorizedError. Synchronous, for plain code."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise UnauthorizedError("authentication required")
    return principal


async def optional_auth(request: Request) -> Principal | None:
    """The caller, or None. For routes that behave differently when signed in."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal


async def require_auth(request: Request) -> Principal:
    """A verified caller, or 401."""
    return principal_of(request)


def require_scopes(*scopes: str) -> Callable[[Request], Awaitable[Principal]]:
    """Require every listed scope, or 403.

    401 means "I do not know who you are"; 403 means "I do, and you may not".
    Collapsing them into one status makes debugging a permissions problem
    guesswork.
    """

    async def dependency(request: Request) -> Principal:
        principal = principal_of(request)
        if not principal.has_scope(*scopes):
            missing = sorted(set(scopes) - principal.scopes)
            raise ForbiddenError(f"missing scope(s): {', '.join(missing)}")
        return principal

    return dependency


def require_roles(*roles: str) -> Callable[[Request], Awaitable[Principal]]:
    """Require any one of these roles, or 403."""

    async def dependency(request: Request) -> Principal:
        principal = principal_of(request)
        if not principal.has_any_role(*roles):
            raise ForbiddenError(f"requires one of: {', '.join(sorted(roles))}")
        return principal

    return dependency


# -- plugin -------------------------------------------------------------


class AuthPlugin(Plugin):
    meta = PluginMeta(
        name="auth",
        version="0.1.0",
        description="JWT verification with JWKS rotation, scopes, and revocation.",
        after=("observability", "cache"),
        provides=("auth", "auth.issuer", "auth.store", "auth.providers"),
        default_enabled=False,
        extra="jfastframework[auth]",
    )
    Settings = AuthSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._jwks: JWKSClient | None = None
        self._key: Any = None
        self._store: TokenStore | None = None
        self._issuer: TokenIssuer | None = None
        self._claims = TokenClaims()
        self._providers: dict[str, OIDCProvider] = {}
        self._on_identity: IdentityHandler | None = None
        self._is_dev = True

    # -- configuration -------------------------------------------------

    def _validate(self, *, production: bool = False) -> None:
        settings: AuthSettings = self.settings
        if settings.mode not in MODES:
            raise PluginError(f"auth mode must be one of {', '.join(MODES)}, not {settings.mode!r}")

        import jwt

        if not settings.algorithms:
            raise PluginError(
                '[plugin.auth] algorithms is empty; list at least one, e.g. ["RS256"].'
            )
        known = set(jwt.algorithms.get_default_algorithms())
        unknown = sorted(set(settings.algorithms) - known)
        if unknown:
            # PyJWT would reject every token with "algorithm not supported" --
            # at the first request, as a 401 nobody can explain.
            raise PluginError(
                f"[plugin.auth] algorithms has {', '.join(unknown)}, which PyJWT does not "
                f"know. Known: {', '.join(sorted(known))}."
            )
        for field, value, floor in (
            ("leeway", settings.leeway, 0),
            ("access_lifetime_minutes", settings.access_lifetime_minutes, 1),
            ("refresh_lifetime_days", settings.refresh_lifetime_days, 1),
            ("refresh_grace_seconds", settings.refresh_grace_seconds, 0),
            ("jwks_cache_seconds", settings.jwks_cache_seconds, 1),
            ("jwks_attempts", settings.jwks_attempts, 1),
            ("jwks_breaker_failures", settings.jwks_breaker_failures, 0),
        ):
            if value < floor:
                raise PluginError(f"[plugin.auth] {field} must be at least {floor}, not {value}.")
        if settings.jwks_timeout <= 0 or settings.jwks_breaker_cool_down <= 0:
            raise PluginError(
                "[plugin.auth] jwks_timeout and jwks_breaker_cool_down must be positive."
            )

        symmetric = set(settings.algorithms) & SYMMETRIC_ALGORITHMS
        asymmetric = set(settings.algorithms) - SYMMETRIC_ALGORITHMS

        if symmetric and asymmetric:
            # Allowing both is the algorithm-confusion attack, configured in.
            raise PluginError(
                "auth.algorithms mixes symmetric and asymmetric algorithms "
                f"({', '.join(sorted(settings.algorithms))}). Allowing both lets a token "
                "signed with the public key as an HMAC secret verify. Pick one family."
            )

        if settings.mode == "jwks":
            if not settings.jwks_url:
                raise PluginError('auth mode "jwks" needs [plugin.auth] jwks_url.')
            if symmetric:
                raise PluginError('auth mode "jwks" cannot verify HMAC algorithms.')
            scheme = settings.jwks_url.partition("://")[0].lower()
            if scheme not in ("http", "https"):
                raise PluginError(
                    f"[plugin.auth] jwks_url must be an http(s) URL, not {settings.jwks_url!r}."
                )
            if production and scheme != "https":
                # Whoever can answer that plain-HTTP request chooses which
                # keys this service trusts, and so mints any token they like.
                raise PluginError(
                    "[plugin.auth] jwks_url is plain http in production: anyone on the "
                    "path can serve their own keys and sign their own tokens. Use https."
                )
        elif settings.mode == "public_key":
            if not settings.public_key:
                raise PluginError('auth mode "public_key" needs [plugin.auth] public_key.')
            if symmetric:
                raise PluginError('auth mode "public_key" cannot verify HMAC algorithms.')
        elif settings.mode == "secret":
            if settings.secret is None:
                raise PluginError('auth mode "secret" needs JFAST_AUTH_SECRET.')
            if asymmetric:
                raise PluginError('auth mode "secret" cannot verify asymmetric algorithms.')
            secret_bytes = len(settings.secret.get_secret_value().encode("utf-8"))
            if production and secret_bytes < MIN_SECRET_BYTES:
                raise PluginError(
                    f"JFAST_AUTH_SECRET is {secret_bytes} bytes; production needs at least "
                    f"{MIN_SECRET_BYTES} (RFC 7518 3.2), or the HMAC can be brute-forced "
                    f"offline from any one token. Generate one with "
                    f'`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
                )

        if settings.issue_tokens and settings.mode == "jwks":
            # Minting requires a private key; JWKS publishes public ones.
            raise PluginError(
                'auth cannot issue tokens in "jwks" mode: minting needs a private key. '
                'Use mode = "secret" for a single service, or issue from your identity service.'
            )
        if settings.issue_tokens and settings.mode == "public_key":
            # The PEM here verifies; signing needs the private half, and
            # jwt.encode would fail at the first login instead of at boot.
            raise PluginError(
                'auth cannot issue tokens in "public_key" mode: public_key holds the key '
                "that verifies, not the one that signs. Issue from the service that holds "
                'the private key, or use mode = "secret" for a single service.'
            )

        if not settings.audience:
            logger.warning(
                "auth has no audience configured: a token minted for any other service "
                "of the same issuer will be accepted here"
            )

    # -- lifecycle -----------------------------------------------------

    def register(self, ctx: AppContext) -> None:
        settings: AuthSettings = self.settings
        self._validate(production=ctx.settings.is_production)
        self._is_dev = not ctx.settings.is_production
        self._claims = TokenClaims(
            scopes=settings.scope_claim,
            roles=settings.roles_claim,
            tenant=settings.tenant_claim,
        )

        if settings.mode == "jwks":
            self._jwks = JWKSClient(
                url=settings.jwks_url,
                cache_seconds=settings.jwks_cache_seconds,
                timeout=settings.jwks_timeout,
                attempts=settings.jwks_attempts,
                breaker_failures=settings.jwks_breaker_failures,
                breaker_cool_down=settings.jwks_breaker_cool_down,
            )
        elif settings.mode == "public_key":
            self._key = settings.public_key
        else:
            assert settings.secret is not None
            self._key = settings.secret.get_secret_value()

        if ctx.has("cache.client"):
            self._store = RedisTokenStore(ctx.require("cache.client"))
        elif ctx.settings.is_production and settings.issue_tokens:
            # The generated image runs `uvicorn --workers`, defaulting to one
            # per CPU, so more than one process is the shape production
            # actually has. Every worker then holds its own copy of this store,
            # and both halves of session security become per-process:
            #
            #   * a logout revokes on the worker that served it and nowhere
            #     else, so the token keeps working on the others;
            #   * a refresh reaching any worker but the issuing one finds no
            #     family and is answered 401 "this session has been revoked" --
            #     a revocation that never happened, and on four cores it is
            #     three refreshes in four.
            #
            # A warning in a JSON log at boot is not read, so this refuses
            # instead. This is not a degraded mode, it is a broken one, and the
            # symptom -- users logged out at random weeks later -- costs more
            # to diagnose than a refused boot costs to fix.
            raise PluginError(
                "auth issues tokens in production with no shared token store. "
                "Revocation and refresh-reuse detection would be per worker, and "
                "the generated image runs one worker per CPU: a logout would "
                "apply to one process and a refresh sent to any other worker "
                "would be refused as revoked. Enable the 'cache' plugin "
                '(`pip install "jfastframework[cache]"`, then add "cache" to '
                "[plugins].enabled), or set [plugin.auth] issue_tokens = false "
                "if this service only verifies tokens minted elsewhere."
            )
        else:
            self._store = MemoryTokenStore()
            # A warning only where it can bite: in development `jfast start`
            # runs one process, and a line on every start that asks for Redis
            # teaches people to skip the startup log.
            (ctx.logger.warning if ctx.settings.is_production else ctx.logger.info)(
                "auth is using an in-memory token store: a logout applies to this "
                "process only. Enable the 'cache' plugin for shared revocation."
            )

        ctx.provide("auth", self)
        ctx.provide("auth.store", self._store)

        if settings.issue_tokens:
            self._issuer = TokenIssuer(
                key=self._key,
                algorithm=settings.algorithms[0],
                issuer=settings.issuer,
                audience=settings.audience,
                access_lifetime=timedelta(minutes=settings.access_lifetime_minutes),
                refresh_lifetime=timedelta(days=settings.refresh_lifetime_days),
                store=self._store,
                claims=self._claims,
                refresh_grace=timedelta(seconds=settings.refresh_grace_seconds),
            )
            ctx.provide("auth.issuer", self._issuer)

        if settings.providers:
            from jfastframework.auth.oidc import provider as build_provider

            for name, raw in settings.providers.items():
                self._providers[name] = build_provider(name, **raw)
            ctx.provide("auth.providers", self._providers)

        ctx.app.add_middleware(AuthMiddleware, plugin=self)

        if settings.mount_router:
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["auth"])

    async def verify_token(self, token: str) -> Principal:
        """Verify a bearer token. Raises TokenError or JWKSError."""
        import jwt

        settings: AuthSettings = self.settings
        key: Any = self._key

        if self._jwks is not None:
            try:
                header = jwt.get_unverified_header(token)
            except jwt.InvalidTokenError as exc:
                raise TokenError(f"malformed token header: {exc}") from exc
            # The header's `kid` only selects which *public* key to try. The
            # algorithm still comes from configuration, never from the token.
            jwk = await self._jwks.key_for(header.get("kid"))
            key = jwt.PyJWK(jwk).key

        principal = verify(
            token,
            key=key,
            algorithms=list(settings.algorithms),
            audience=settings.audience or None,
            issuer=settings.issuer or None,
            leeway=settings.leeway,
            claims=self._claims,
        )

        if settings.check_revocation and principal.token_id and self._store is not None:
            family = principal.claims.get("fam")
            try:
                revoked = await self._store.is_revoked(principal.token_id)
                family_revoked = bool(family) and await self._store.is_family_revoked(str(family))
            except Exception as exc:  # any store failure is an outage
                if not settings.revocation_fail_open:
                    raise RevocationUnavailableError(
                        "the revocation store is not answering; retry shortly"
                    ) from exc
                self._store_outage(exc)
                return principal
            if revoked:
                raise TokenError("token has been revoked")
            if family_revoked:
                raise TokenError("session has been revoked")

        return principal

    _last_store_warning: float = 0.0

    def _store_outage(self, exc: Exception) -> None:
        """Log that revocation is not being checked, at most every ten seconds."""
        now = time.monotonic()
        if now - self._last_store_warning >= 10.0:
            self._last_store_warning = now
            logger.warning(
                "revocation store unreachable (%s: %s); accepting tokens without the "
                "revocation check (revocation_fail_open = true)",
                type(exc).__name__,
                exc,
            )

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/me", summary="The verified caller")
        async def me(caller: Principal = Depends(require_auth)) -> dict[str, Any]:
            # Never the token, never the raw claims: just identity and rights.
            return caller.describe()

        @router.post("/logout", status_code=204, summary="Revoke this token")
        async def logout(caller: Principal = Depends(require_auth)) -> None:
            if self._store is None or caller.token_id is None:
                return
            remaining = 0
            if caller.expires_at is not None:
                remaining = int((caller.expires_at - datetime.now(UTC)).total_seconds())
            await self._store.revoke(caller.token_id, ttl=max(remaining, 1))
            # Revoke the family too, or the refresh token issued alongside this
            # one quietly mints a new session. No fallback to the subject: a
            # token without a family was not minted here (an external IdP in
            # "jwks" mode), and revoking `subject` as if it were a family would
            # end every other session this person has -- including the next one
            # they open.
            family = caller.claims.get(FAMILY_CLAIM)
            if family:
                await self._store.revoke_family(
                    str(family),
                    ttl=int(timedelta(days=self.settings.refresh_lifetime_days).total_seconds()),
                )

        if self.settings.issue_tokens:

            @router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
            async def refresh(payload: RefreshRequest) -> TokenPair:
                if self._issuer is None:
                    raise UnauthorizedError("token issuance is disabled")
                try:
                    principal = await self.verify_token(payload.refresh_token)
                except (TokenError, JWKSError) as exc:
                    logger.info("refresh rejected", extra={"reason": str(exc)})
                    raise UnauthorizedError("invalid refresh token") from exc
                return await self._issuer.rotate(principal)

        if self._providers:
            self._mount_oidc(router)

        return router

    # -- social login ---------------------------------------------------

    def on_identity(self, handler: IdentityHandler) -> IdentityHandler:
        """Register what happens once a provider has vouched for someone.

            @auth.on_identity
            async def sign_in(identity, request):
                user = await users.upsert(identity)
                return auth.issuer.issue(subject=str(user.id), scopes=user.scopes)

        This hook is yours because only you know what a user is here. The
        plugin does the part that is the same everywhere and easy to get
        wrong -- state, nonce, audience, issuer -- and stops at the point
        where the answer is application-specific.
        """
        self._on_identity = handler
        return handler

    def on_refresh(self, handler: RefreshResolver) -> RefreshResolver:
        """Register what a session may still do, re-read at every refresh.

            @auth.on_refresh
            async def rights(principal):
                user = await users.get(principal.subject)
                return Grant(scopes=tuple(user.scopes)) if user.active else None

        Without it the refresh token carries its own grant forward, so a
        permission revoked today survives until the session ends. Returning
        ``None`` revokes the session family: the outstanding access token stops
        working immediately rather than running out its lifetime.
        """
        if self._issuer is None:
            # Registering this on a plugin that cannot mint tokens is a
            # configuration mistake, not a no-op to discover in production.
            raise PluginError(
                "auth cannot register an on_refresh handler: token issuance is off. "
                "Set [plugin.auth] issue_tokens = true."
            )
        self._issuer.on_refresh(handler)
        return handler

    def _mount_oidc(self, router: APIRouter) -> None:
        import json

        from starlette.responses import RedirectResponse

        settings: AuthSettings = self.settings

        @router.get("/{provider}/start", summary="Begin a social login")
        async def start(provider: str) -> RedirectResponse:
            client = self._providers.get(provider)
            if client is None:
                raise NotFoundError(f"no provider named {provider!r}")
            url, state, nonce = client.authorization_url()
            response = RedirectResponse(url, status_code=307)
            # httponly so script cannot read it, samesite=lax so it survives
            # the provider's top-level redirect back but not a cross-site
            # POST, secure outside development.
            response.set_cookie(
                settings.oidc_cookie_name,
                json.dumps({"state": state, "nonce": nonce, "provider": provider}),
                max_age=settings.oidc_cookie_seconds,
                httponly=True,
                samesite="lax",
                secure=not self._is_dev,
                path=settings.prefix,
            )
            return response

        @router.get("/{provider}/callback", summary="Finish a social login")
        async def callback(provider: str, request: Request) -> Any:
            client = self._providers.get(provider)
            if client is None:
                raise NotFoundError(f"no provider named {provider!r}")

            raw = request.cookies.get(settings.oidc_cookie_name)
            if not raw:
                raise UnauthorizedError("no login is in progress")
            try:
                pending = json.loads(raw)
            except ValueError as exc:
                raise UnauthorizedError("malformed login cookie") from exc

            # Without this comparison the callback accepts a code obtained in
            # someone else's browser: that is the login CSRF this parameter
            # exists to stop.
            if not secrets.compare_digest(
                str(pending.get("state", "")), request.query_params.get("state", "")
            ):
                raise UnauthorizedError("login state does not match")
            if pending.get("provider") != provider:
                raise UnauthorizedError("login state is for a different provider")

            code = request.query_params.get("code", "")
            if not code:
                error = request.query_params.get("error", "no authorization code")
                raise UnauthorizedError(f"login failed: {error}")

            try:
                tokens = await client.exchange(code)
                identity = await client.verify_id_token(
                    tokens.get("id_token", ""), nonce=pending.get("nonce")
                )
            except TokenError as exc:
                logger.info("social login rejected", extra={"provider": provider})
                raise UnauthorizedError("could not verify this login") from exc

            if self._on_identity is None:
                # Deliberately not a silent success: without a handler there
                # is no user and no session, and returning 200 here would
                # look like a working login.
                raise PluginError(
                    "A provider verified this user, but no on_identity handler is "
                    "registered, so there is nothing to log them in to. Register one "
                    "with @auth.on_identity."
                )

            result = await self._on_identity(identity, request)
            response = _as_response(result)
            response.delete_cookie(settings.oidc_cookie_name, path=settings.prefix)
            return response

    async def health(self, ctx: AppContext) -> HealthReport:
        settings: AuthSettings = self.settings
        meta: dict[str, Any] = {
            "mode": settings.mode,
            "algorithms": list(settings.algorithms),
            "audience": settings.audience or None,
            "issues_tokens": settings.issue_tokens,
        }

        if self._jwks is not None:
            healthy, detail = await self._jwks.health()
            if not healthy:
                return HealthReport.fail(detail, **meta)
            meta["key_ids"] = list(self._jwks.key_ids)

        if self._store is not None:
            store_ok, store_detail = await self._store.health()
            if not store_ok:
                # A degraded store means revocation is weaker than intended.
                # The service still authenticates, so this is not critical.
                return HealthReport.fail(store_detail, critical=False, **meta)

        return HealthReport.ok("auth configured", **meta)
