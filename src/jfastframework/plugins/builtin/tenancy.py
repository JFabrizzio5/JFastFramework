"""Which tenant is this request for.

    [plugin.tenancy]
    sources = ["token", "subdomain"]
    base_domain = "app.example.com"

The list is an **order of trust**, and it is the whole design:

| Source | Who controls it | Signed |
| --- | --- | --- |
| `token` | your identity provider, cryptographically | yes |
| `user` | the same signed token: the user *is* the tenant | yes |
| `subdomain` | whoever sends the `Host` header | no |
| `path` | whoever writes the URL | no |
| `header` | whoever sent the request | no |

`user` is for the SaaS where every account owns its own data and there is no
organisation above it: the tenant is the signed-in user's id (the token's
`sub`). Put it after `token` -- `sources = ["token", "user"]` -- and a user who
later joins an organisation carrying a `tenant_id` claim moves to it without a
code change.

`header` is in the code because it is genuinely useful in development and in
tests. It is not in the default list, and enabling it in production logs a
warning, because `X-Tenant-ID: acme` is one curl away from another tenant's
data.

**With `auth` on, an unsigned source names a tenant; it never grants one.**
`subdomain`, `path` and `header` are chosen by whoever sends the request --
`curl -H "Host: acme.example.com"` needs no DNS -- so on their own they are
only a hint: what the sign-in routes read to know which tenant's accounts to
look in. The tenant a request may *act in* comes from the principal:

| Request | Tenant granted | `current_tenant` |
| --- | --- | --- |
| no session, any source | none | 401 |
| token claim `a`, unsigned source says nothing or `a` | `a` | `a` |
| token claim `a`, unsigned source says `b` | none | 403 |
| signed in, no claim, unsigned source says `b` | none | 403 |
| the same, with `trust_unscoped_principals = true` | `b` | `b` |

A signed source outranks an unsigned one wherever it sits in `sources`; the
order only ranks sources of the same kind. The denied request still reaches
routes that do not ask for a tenant -- `/health`, `/auth/refresh` -- but
`request.state.tenant_id` is None, the RLS session sets no tenant, and
`current_tenant` and `TenantSession` answer 401 or 403. The tenant the request
named is on `request.state.tenant_requested` (and `requested_tenant`) for a
page that only needs to know which tenant's sign-in form to draw.

Without `auth` there is no principal to check against: a public site with a
tenant per subdomain keeps the resolved tenant, as before.

The resolved tenant lands on `request.state.tenant_id`, in the logging context,
and in `BaseRepository` — so a query that forgets to filter is at least
filtered by the repository. That is still a convention, not isolation: row-level
security is what makes it a guarantee. See PLAN.md phase 2.

A tenant may also carry its own time zone:

    [plugin.tenancy.timezones]
    acme = "America/Santiago"
    globex = "America/Mexico_City"

That is the multi-region case one deployment actually has: the same rows, the
same UTC instants, and a different answer to "what were yesterday's orders" per
tenant. It is optional. A tenant with no entry gets `[app] timezone`, which
defaults to UTC — never the server's zone, because that is the thing that makes
an answer depend on where the container runs. Every name is validated at boot,
so a typo in this table stops the service instead of shifting one tenant's
reports by a day.

The resolved zone lands on `request.state.tenant_timezone`; read it with the
`tenant_zone` dependency and hand it to `jfastframework.time.day_bounds`.
"""

from __future__ import annotations

import logging
import re
from datetime import tzinfo
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings
from jfastframework.time import default_zone
from jfastframework.time import zone as resolve_zone

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.tenancy")

SOURCES = ("token", "user", "subdomain", "path", "header")

# Sources a client cannot choose: both read the verified token.
SIGNED_SOURCES = frozenset({"token", "user"})

# A tenant slug ends up in hostnames, log fields and SQL parameters. Keep it
# to what is safe in all three.
TENANT_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# A user id is chosen by the identity provider, not by us: UUIDs, hex ids,
# "auth0|abc". Looser than a slug, still nothing that could be read as SQL or
# a path, and bound as a parameter everywhere it goes.
SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@|-]{0,127}$")

# Subdomains that are never a tenant, whatever the DNS says.
RESERVED_SUBDOMAINS = frozenset(
    {"www", "api", "app", "admin", "static", "assets", "cdn", "mail", "ftp", "localhost"}
)


class TenancySettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_TENANCY_", env_file=".env", extra="ignore")

    # Ordered by trust: the first source that yields a tenant wins.
    sources: list[str] = Field(default_factory=lambda: ["token", "subdomain"])
    base_domain: str = ""
    header_name: str = "X-Tenant-ID"
    token_claim: str = "tenant_id"
    path_prefix: str = "/t"
    # Reject a request that resolves to no tenant. Off by default: health
    # checks, metrics and the docs are not tenant-scoped.
    require_tenant: bool = False
    exempt_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/ready", "/info", "/metrics", "/docs", "/openapi.json"]
    )
    reserved: list[str] = Field(default_factory=lambda: sorted(RESERVED_SUBDOMAINS))
    # With `auth` on: let a signed-in principal whose token carries no tenant
    # (no claim, and no `user` source) act in the tenant a subdomain, path or
    # header names. Off, that is a 403 -- nothing signed says the account
    # belongs there. Turn it on only when the service checks membership itself
    # (a staff account that works across tenants, a membership table read on
    # every request); the framework cannot tell a member from a stranger.
    trust_unscoped_principals: bool = False

    # Tenant slug to IANA zone. Empty is the common case: one country, one
    # zone, and `[app] timezone` already answers it. Filled in, it is what
    # makes one deployment serve tenants whose days start at different
    # instants without storing anything in local time.
    timezones: dict[str, str] = Field(default_factory=dict)


def tenant_from_host(host: str, base_domain: str, reserved: set[str]) -> str | None:
    """`acme.app.example.com` with base `app.example.com` -> `acme`."""
    if not base_domain:
        return None
    hostname = host.split(":", 1)[0].lower().rstrip(".")
    suffix = "." + base_domain.lower().lstrip(".")
    if not hostname.endswith(suffix):
        return None

    label = hostname[: -len(suffix)]
    # Only the leftmost label, and only one: `a.b.app.example.com` is not a
    # tenant called "a.b", it is a mistake.
    if not label or "." in label or label in reserved:
        return None
    return label if TENANT_SLUG.match(label) else None


def tenant_from_path(path: str, prefix: str) -> str | None:
    """`/t/acme/invoices` -> `acme`."""
    marker = prefix.rstrip("/") + "/"
    if not path.startswith(marker):
        return None
    candidate = path[len(marker) :].split("/", 1)[0].lower()
    return candidate if TENANT_SLUG.match(candidate) else None


def raise_if_denied(request: Request) -> None:
    """Raise the 401 or 403 tenancy decided for this request, if it decided one.

    With `auth` on, a tenant named by an unsigned source that the principal is
    not entitled to is refused here rather than in the middleware: the request
    still reaches routes that need no tenant (a health check, a token refresh),
    and only what asks for a tenant hears the refusal.
    """
    from jfastframework.errors import ForbiddenError, UnauthorizedError

    denied = getattr(request.state, "tenant_denied", None)
    if not denied:
        return
    status, message = denied
    if status == 401:
        raise UnauthorizedError(message)
    raise ForbiddenError(message)


async def current_tenant(request: Request) -> str:
    """The tenant of this request; 401 without a session, 403 without a tenant::

        @router.get("/invoices")
        async def invoices(tenant: str = Depends(current_tenant)): ...

    It reads what the tenancy plugin granted and nothing else -- not a header,
    not a body field. Without the plugin it falls back to the token's
    ``tenant_id`` claim, so a service that only uses `auth` still works. With
    `auth` on, a subdomain, path or header never grants a tenant by itself:
    see the module docstring for the whole table.
    """
    from jfastframework.errors import ForbiddenError, UnauthorizedError

    raise_if_denied(request)
    tenant = getattr(request.state, "tenant_id", None)
    principal = getattr(request.state, "principal", None)
    if not tenant:
        tenant = getattr(principal, "tenant_id", None)
    if not tenant and principal is None:
        # 401, not 403: nobody is signed in -- or their token just expired.
        # A client refreshes its session on a 401 and gives up on a 403, so
        # answering 403 here strands every session at its first expiry.
        raise UnauthorizedError("authentication required")
    if not tenant:
        raise ForbiddenError(
            "this request is not scoped to a tenant. Sign in, or check [plugin.tenancy] sources."
        )
    return str(tenant)


def tenant_hint(request: Request) -> str | None:
    """The tenant this request *names*, granted or not; None when it names none.

    What the sign-in routes read: before a session exists, the subdomain or
    path is the only way to know which tenant's accounts to look in, and the
    password is what proves the caller belongs there. Never a tenant to read
    or write data as -- that is `current_tenant`.
    """
    requested = getattr(request.state, "tenant_requested", None)
    if requested:
        return str(requested)
    tenant = getattr(request.state, "tenant_id", None)
    return str(tenant) if tenant else None


async def requested_tenant(request: Request) -> str:
    """The tenant this request names, for pages that are public on purpose::

        @router.get("/branding")
        async def branding(tenant: str = Depends(requested_tenant)): ...

    A sign-in page's logo, a tenant's public landing page. It proves nothing
    about the caller, so never use it to choose whose rows to read or write:
    that is `current_tenant`. 404 when the request names no tenant.
    """
    from jfastframework.errors import NotFoundError

    tenant = tenant_hint(request)
    if not tenant:
        raise NotFoundError("this request does not name a tenant")
    return tenant


async def tenant_zone(request: Request) -> tzinfo:
    """The zone this request's days are measured in.

    Falls back to the business zone -- ``[app] timezone`` -- for a tenant with
    no entry, and for a request that resolved to no tenant at all. Never to the
    server's zone: that is the failure this exists to remove.
    """
    resolved = getattr(request.state, "tenant_timezone", None)
    return resolved if isinstance(resolved, tzinfo) else default_zone()


class TenancyMiddleware:
    """Resolve the tenant once per request. Plain ASGI: no task group, no stream.

    ``auth`` says whether the auth plugin is on. With it, the tenant a request
    may act in has to be backed by the principal (see the module docstring);
    without it there is no principal to check, and the first source that
    yields a tenant wins, as it always has.
    """

    def __init__(self, app: ASGIApp, *, settings: TenancySettings, auth: bool = False) -> None:
        self.app = app
        self._settings = settings
        self._auth = auth
        self._reserved = set(settings.reserved)
        # Resolved once at construction, not per request: `zone()` caches, but
        # the point is that an unknown name has already failed at boot.
        self._zones: dict[str, tzinfo] = {
            tenant: resolve_zone(name) for tenant, name in settings.timezones.items()
        }

    def _from(self, source: str, request: Request) -> str | None:
        """What one source says, or None."""
        if source == "token":
            principal = getattr(request.state, "principal", None)
            if principal is not None and principal.tenant_id:
                return str(principal.tenant_id)
        elif source == "user":
            principal = getattr(request.state, "principal", None)
            subject = getattr(principal, "subject", None)
            if subject and SUBJECT.match(str(subject)):
                return str(subject)
        elif source == "subdomain":
            return tenant_from_host(
                request.headers.get("host", ""), self._settings.base_domain, self._reserved
            )
        elif source == "path":
            return tenant_from_path(request.url.path, self._settings.path_prefix)
        elif source == "header":
            raw = request.headers.get(self._settings.header_name, "").lower()
            if raw and TENANT_SLUG.match(raw):
                return raw
        return None

    def _resolve(self, request: Request) -> tuple[str | None, str | None]:
        """Returns ``(tenant, source)`` from the first source that yields one."""
        for source in self._settings.sources:
            tenant = self._from(source, request)
            if tenant:
                return tenant, source
        return None, None

    def _decide(
        self, request: Request
    ) -> tuple[str | None, str | None, str | None, tuple[int, str] | None]:
        """``(granted, source, requested, denied)`` for this request.

        ``requested`` is what an unsigned source named; ``denied`` is the
        ``(status, message)`` that `current_tenant` answers instead of a tenant.
        """
        if not self._auth:
            tenant, source = self._resolve(request)
            requested = tenant if source not in SIGNED_SOURCES else None
            return tenant, source, requested, None

        signed: tuple[str, str] | None = None
        unsigned: tuple[str, str] | None = None
        for source in self._settings.sources:
            tenant = self._from(source, request)
            if not tenant:
                continue
            if source in SIGNED_SOURCES:
                signed = signed or (tenant, source)
            else:
                unsigned = unsigned or (tenant, source)

        principal = getattr(request.state, "principal", None)
        if signed is None and principal is not None and principal.tenant_id:
            # A claim is what the account is entitled to even when `token` is
            # not a listed source: `current_tenant` falls back to it anyway.
            signed = (str(principal.tenant_id), "token")
        requested = unsigned[0] if unsigned else None

        if principal is None:
            if unsigned is None:
                return None, None, None, None
            # Anyone can type a Host header. A tenant named that way is where
            # the sign-in routes look for the account, and nothing more.
            return None, None, requested, (401, "authentication required")

        if signed is not None:
            if unsigned is None or unsigned[0] == signed[0]:
                return signed[0], signed[1], requested, None
            return (
                None,
                None,
                requested,
                (
                    403,
                    f"this session is scoped to tenant {signed[0]!r}, and the request "
                    f"names {unsigned[0]!r}. Sign in to {unsigned[0]!r} to act there.",
                ),
            )

        if unsigned is None:
            return None, None, None, None
        if self._settings.trust_unscoped_principals:
            return unsigned[0], unsigned[1], requested, None
        return (
            None,
            None,
            requested,
            (
                403,
                f"this account is not scoped to tenant {unsigned[0]!r}: its token "
                "carries no tenant. Sign in to that tenant, or set [plugin.tenancy] "
                "trust_unscoped_principals = true if the service checks membership itself.",
            ),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from jfastframework.errors import ForbiddenError, problem_response
        from jfastframework.plugins.builtin.observability import tenant_id_var

        # A Request is only a view over the scope: building one costs nothing
        # and gives _decide the same headers and state the old code read.
        request = Request(scope, receive)
        tenant, source, requested, denied = self._decide(request)
        exempt = any(scope["path"].startswith(p) for p in self._settings.exempt_paths)

        if tenant is None and requested is None and self._settings.require_tenant and not exempt:
            # Answered here, not raised: this runs outside FastAPI's exception
            # handlers, so raising would surface as a 500 rather than the
            # documented problem+json 403. A request that *names* a tenant it
            # was not granted passes: the sign-in routes need it, and
            # `current_tenant` answers the 401 or 403 for everything else.
            response = problem_response(
                ForbiddenError("this request is not scoped to a tenant"), request
            )
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        # Only a granted tenant goes here and into the context variable the
        # RLS session, jobs and events read: a denied request acts in none.
        state["tenant_id"] = tenant
        state["tenant_source"] = source
        state["tenant_requested"] = requested
        state["tenant_denied"] = denied
        # `default_zone()` is read per request rather than captured at
        # construction so that a service which sets the business zone after
        # wiring its middleware is not pinned to whatever UTC it started with.
        state["tenant_timezone"] = self._zones.get(tenant or requested or "", default_zone())
        token = tenant_id_var.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            tenant_id_var.reset(token)


class TenancyPlugin(Plugin):
    meta = PluginMeta(
        name="tenancy",
        version="0.1.0",
        description="Resolve the tenant from the token, the user, the subdomain or the path.",
        # After auth, so a signed claim is available to prefer over the host.
        after=("observability", "auth"),
        provides=("tenancy",),
        default_enabled=False,
    )
    Settings = TenancySettings

    def register(self, ctx: AppContext) -> None:
        settings: TenancySettings = self.settings

        unknown = set(settings.sources) - set(SOURCES)
        if unknown:
            raise PluginError(
                f"tenancy sources {', '.join(sorted(unknown))} are unknown; "
                f"choose from {', '.join(SOURCES)}"
            )
        if not settings.sources:
            raise PluginError("tenancy has no sources; it would resolve nothing")

        if "subdomain" in settings.sources and not settings.base_domain:
            raise PluginError(
                'tenancy source "subdomain" needs [plugin.tenancy] base_domain, '
                "or every host looks like a tenant."
            )

        if "header" in settings.sources and ctx.settings.is_production:
            # Not an error -- some deployments terminate at a trusted proxy
            # that sets it. But it must never be silent.
            ctx.logger.warning(
                "tenancy trusts the %s header in production. Anyone who can reach "
                "this service can now choose a tenant. Remove 'header' from "
                "[plugin.tenancy] sources unless a trusted proxy sets it.",
                settings.header_name,
            )

        for tenant, name in settings.timezones.items():
            try:
                resolve_zone(name)
            except ValueError as exc:
                raise PluginError(
                    f"[plugin.tenancy.timezones] {tenant} = {name!r} is not a "
                    f"usable time zone. {exc}"
                ) from exc

        if not {"token", "user"} & set(settings.sources):
            ctx.logger.info(
                "tenancy is not using the token claim; the tenant will come from "
                "the request rather than from something signed"
            )

        ctx.provide("tenancy", settings)

        # Appended rather than added: `add_middleware` puts a middleware
        # *outermost*, which would run tenancy before auth and leave the
        # signed `token` source unreadable -- the principal does not exist
        # that early. Appending makes this the innermost middleware, so every
        # source, including the token claim, is available when it resolves.
        from starlette.middleware import Middleware

        # `auth` registers first (see `after`), so this is decided once, here.
        ctx.app.user_middleware.append(
            Middleware(TenancyMiddleware, settings=settings, auth=ctx.has("auth"))
        )

    async def health(self, ctx: AppContext) -> HealthReport:
        settings: TenancySettings = self.settings
        return HealthReport.ok(
            "tenancy configured",
            sources=list(settings.sources),
            base_domain=settings.base_domain or None,
            require_tenant=settings.require_tenant,
            trust_unscoped_principals=settings.trust_unscoped_principals,
            timezones=dict(settings.timezones) or None,
            default_timezone=str(default_zone()),
        )
