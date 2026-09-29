"""API gateway: prefix-based reverse proxy.

One service needs no gateway. Two or more, and every client has to know every
hostname -- so `jfast` generates this one the moment a workspace grows past a
single backend.

    [plugin.gateway]
    timeout = 30.0

    [[plugin.gateway.routes]]
    prefix = "/billing"
    target = "http://billing:8010"

    [[plugin.gateway.routes]]
    prefix = "/catalog"
    target = "http://catalog:8020"

Deliberately *not* a catch-all. Each configured prefix registers its own route,
so ``/health``, ``/ready`` and ``/metrics`` still belong to the gateway itself
rather than being swallowed and proxied somewhere.

Requires: ``pip install jfastframework[gateway]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request, Response
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import JFastError
from jfastframework.middleware import client_ip
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Headers that describe one hop and must not be forwarded to the next.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: Headers this proxy writes itself, dropped from whatever the client sent so
#: the upstream sees one value rather than the client's and ours concatenated.
#: ``x-forwarded-for`` is here because a client that names its own address
#: through a gateway that forwards the claim verbatim has defeated every
#: allow-list behind it: the value the upstream gets is the one
#: ``trusted_proxies`` already resolved, and nothing the request carried.
CLIENT_SUPPLIED = frozenset({"x-forwarded-host", "x-forwarded-proto", "x-forwarded-for"})

#: Dropped from the upstream's response on top of the hop-by-hop set.
#: ``httpx`` decompresses the body before this code ever sees it, so relaying
#: the header that describes the compression hands the client a gzip label on
#: plain bytes -- which every HTTP client in existence then fails to decode.
RESPONSE_DROP = HOP_BY_HOP | {"content-encoding"}


class BadGatewayError(JFastError):
    status_code = 502
    title = "Bad Gateway"


class GatewayTimeoutError(JFastError):
    status_code = 504
    title = "Gateway Timeout"


class GatewayRoute(BaseModel):
    prefix: str
    target: str
    # Forward /billing/invoices as /invoices. Turn off when the upstream
    # already mounts its routers under the same prefix.
    strip_prefix: bool = True

    @field_validator("prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"prefix must start with '/': {value!r}")
        value = value.rstrip("/")
        if not value:
            # "/" would shadow /health, /ready and /metrics on the gateway.
            raise ValueError("prefix '/' would swallow the gateway's own endpoints")
        return value

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"target must be an absolute http(s) URL: {value!r}")
        return value.rstrip("/")


class GatewaySettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_GATEWAY_", env_file=".env", extra="ignore")

    routes: list[GatewayRoute] = Field(default_factory=list)
    timeout: float = 30.0
    # Expose GET /gateway/routes so an operator (or an agent) can see the
    # routing table without reading the config file.
    expose_routes: bool = True


class GatewayPlugin(Plugin):
    meta = PluginMeta(
        name="gateway",
        version="0.1.0",
        description="Prefix-based reverse proxy across the workspace's services.",
        after=("observability", "metrics"),
        provides=("gateway.client",),
        default_enabled=False,
        extra="jfastframework[gateway]",
    )
    Settings = GatewaySettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None

    def register(self, ctx: AppContext) -> None:
        import httpx

        settings: GatewaySettings = self.settings
        if not settings.routes:
            ctx.logger.warning("gateway plugin enabled with no routes configured")

        client = httpx.AsyncClient(timeout=settings.timeout, follow_redirects=False)
        self._client = client
        ctx.provide("gateway.client", client)

        for route in settings.routes:
            self._mount(ctx, route)

        if settings.expose_routes:

            @ctx.app.get("/gateway/routes", tags=["gateway"], summary="Routing table")
            async def routes() -> dict[str, Any]:
                return {
                    "routes": [
                        {
                            "prefix": r.prefix,
                            "target": r.target,
                            "strip_prefix": r.strip_prefix,
                        }
                        for r in settings.routes
                    ]
                }

    def _mount(self, ctx: AppContext, route: GatewayRoute) -> None:
        """Register one prefix. Closure captures the route, not loop state."""

        async def proxy(request: Request, path: str = "") -> Response:
            return await self._forward(ctx, route, request, path)

        # Two patterns: the prefix itself, and everything below it.
        ctx.app.add_api_route(
            route.prefix,
            proxy,
            methods=PROXY_METHODS,
            include_in_schema=False,
            name=f"gateway_{route.prefix.strip('/')}_root",
        )
        ctx.app.add_api_route(
            f"{route.prefix}/{{path:path}}",
            proxy,
            methods=PROXY_METHODS,
            include_in_schema=False,
            name=f"gateway_{route.prefix.strip('/')}",
        )

    async def _forward(
        self,
        ctx: AppContext,
        route: GatewayRoute,
        request: Request,
        path: str,
    ) -> Response:
        import httpx

        if self._client is None:
            raise BadGatewayError("gateway client not initialised")

        suffix = f"/{path}" if path else ""
        if not route.strip_prefix:
            suffix = f"{route.prefix}{suffix}"
        url = f"{route.target}{suffix}"

        # Built as a list of pairs rather than a dict: `Accept`, `Cookie` and
        # `Via` are all legally repeatable, and a dict keeps the last one.
        request_id = getattr(request.state, "request_id", None)
        dropped = CLIENT_SUPPLIED | HOP_BY_HOP
        if request_id:
            # Ours replaces the client's. Without a correlation id of our own
            # there is nothing better than what arrived, so it is relayed.
            dropped = dropped | {"x-request-id"}
        headers: list[tuple[str, str]] = [
            (key, value) for key, value in request.headers.items() if key.lower() not in dropped
        ]
        # Preserve the correlation id across the hop; the observability plugin
        # put it on request.state, and the upstream reads the same header.
        if request_id:
            headers.append(("X-Request-ID", request_id))
        headers.append(("X-Forwarded-Host", request.headers.get("host", "")))
        headers.append(("X-Forwarded-Proto", request.url.scheme))
        # The address `trusted_proxies` resolved, not the one the request
        # claimed. `client_ip` answers "unknown" when ASGI reported no client
        # at all, which is not an address and must not be written as one.
        peer = client_ip(request)
        if peer and peer != "unknown":
            headers.append(("X-Forwarded-For", peer))

        body = await request.body()

        try:
            upstream = await self._client.request(
                request.method,
                url,
                # multi_items(), not dict(): `?tag=a&tag=b` is two values and a
                # dict silently forwards one.
                params=request.query_params.multi_items(),
                headers=headers,
                content=body or None,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Before the request was ever handed over there is no upstream to
            # have been slow, so this is 502 whichever way the platform
            # reports it. It matters because they do not agree: a refused
            # connection to a dead port raises ConnectError on Linux and
            # ConnectTimeout on Windows, and catching TimeoutException first
            # would turn the same dead upstream into a 504 on one of them.
            ctx.logger.warning("gateway cannot reach %s: %s", url, exc)
            raise BadGatewayError(f"{route.prefix} is unreachable") from exc
        except httpx.TimeoutException as exc:
            # Connected, and then ran out of time: the upstream is up and slow.
            ctx.logger.warning("gateway timeout for %s: %s", url, exc)
            raise GatewayTimeoutError(f"{route.prefix} did not respond in time") from exc
        except httpx.HTTPError as exc:
            ctx.logger.warning("gateway cannot reach %s: %s", url, exc)
            raise BadGatewayError(f"{route.prefix} is unreachable") from exc

        response = Response(content=upstream.content, status_code=upstream.status_code)
        # `raw_headers` rather than the `headers=` argument, which takes a
        # Mapping and so cannot express two `Set-Cookie` lines. A session that
        # arrives as two cookies has to leave as two: joining them produces one
        # malformed header, and the browser keeps the first cookie with the
        # rest of the line folded into its attributes.
        relayed: list[tuple[bytes, bytes]] = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in upstream.headers.multi_items()
            if key.lower() not in RESPONSE_DROP
        ]
        # Recomputed, because the body this hands on is the decompressed one
        # and the upstream's length described the compressed bytes.
        relayed.append((b"content-length", str(len(upstream.content)).encode("latin-1")))
        response.raw_headers = relayed
        return response

    async def shutdown(self, ctx: AppContext) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def health(self, ctx: AppContext) -> HealthReport:
        settings: GatewaySettings = self.settings
        if not settings.routes:
            return HealthReport.fail("no routes configured", critical=False)
        # Do not probe upstreams here: a gateway whose upstream is restarting
        # is still doing its job, and cascading readiness failures take the
        # whole system out together.
        return HealthReport.ok(
            f"{len(settings.routes)} route(s)",
            prefixes=[r.prefix for r in settings.routes],
        )
