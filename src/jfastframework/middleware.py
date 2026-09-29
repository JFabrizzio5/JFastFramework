"""Edge protections the kernel applies before a request reaches your code.

Caddy or an ingress covers some of this when one is in front. Not every
deployment has one: ``jfast deploy function`` puts a service on Lambda or Cloud
Run with nothing between it and the internet, and a developer running
``uvicorn`` locally has nothing either. A framework that only behaves when
something else is correctly configured is a framework with a footgun.

Every middleware here is plain ASGI rather than ``BaseHTTPMiddleware``. That
is deliberate: ``BaseHTTPMiddleware`` buffers the response through an anyio
stream, which breaks streaming responses and makes a timeout land in the wrong
place.

The body limit, the request timeout and the security headers ship enabled with
defaults chosen to be survivable rather than tight -- see ``settings.py`` for
the numbers and why they are those numbers. Setting a limit to ``0`` turns it
back off; that is the only way to say "unlimited" in a TOML file, which has no
null.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections.abc import Iterable
from typing import Any

from jfastframework.errors import PROBLEM_CONTENT_TYPE

logger = logging.getLogger("jfast")

Scope = dict[str, Any]
Receive = Any
Send = Any

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _problem(status: int, title: str, detail: str) -> tuple[dict[str, Any], bytes]:
    body = json.dumps(
        {"type": "about:blank", "title": title, "status": status, "detail": detail}
    ).encode()
    start = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    return start, body


async def _send_problem(send: Send, status: int, title: str, detail: str) -> None:
    start, body = _problem(status, title, detail)
    await send(start)
    await send({"type": "http.response.body", "body": body})


class BodyTooLarge(Exception):
    """The body outgrew the limit after the response had already started.

    Raised into the ASGI server rather than returned to the client, because by
    then there is no status code left to change. The server closes the
    connection, which is the only remaining way to say "this response is not
    the whole story".
    """


class BodySizeLimitMiddleware:
    """Reject a request body larger than ``max_bytes`` with 413.

    Checks ``Content-Length`` first, which rejects the common case before a
    single byte is read. A chunked upload has no length to check, so the bytes
    are counted as they arrive and the request is refused the moment it goes
    over -- not after the whole thing has been buffered into memory, which is
    the outcome this exists to prevent.

    A handler that starts streaming its response before it has read the whole
    request is the awkward case: the 413 cannot be sent any more. See
    ``guarded_send`` for what happens instead.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await _send_problem(
                        send,
                        413,
                        "Payload Too Large",
                        f"Request body of {declared} bytes exceeds the "
                        f"{self.max_bytes} byte limit.",
                    )
                    return
                break

        seen = 0
        refused = False

        async def counting_receive() -> dict[str, Any]:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    refused = True
                    # Tell the application the body ended. It will see a short
                    # or empty body; the response it produces is discarded.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message  # type: ignore[no-any-return]

        # Two different things, and conflating them makes an oversized body
        # look like a successful request: `started` is the application's own
        # status line going out, `answered` is this middleware having replaced
        # it with a 413.
        started = False
        answered = False

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal started, answered
            if refused:
                if answered:
                    return
                if not started:
                    # Nothing is on the wire yet, so the application's verdict
                    # on a truncated body can still be replaced with the right
                    # one.
                    answered = True
                    await _send_problem(
                        send,
                        413,
                        "Payload Too Large",
                        f"Request body exceeds the {self.max_bytes} byte limit.",
                    )
                    return
                # The status line is already gone. 413 is no longer available,
                # and quietly dropping the rest hands the client a 200 that
                # looks complete and was computed from half a request --
                # exactly the outcome a limit exists to prevent. Failing the
                # connection is what is left: the client gets a short read,
                # which is what actually happened.
                logger.error(
                    "request body exceeded %d bytes after the response had started; "
                    "failing the connection, because the status line cannot be recalled",
                    self.max_bytes,
                )
                raise BodyTooLarge(f"request body exceeds the {self.max_bytes} byte limit")
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    def __repr__(self) -> str:
        return f"<BodySizeLimitMiddleware max_bytes={self.max_bytes}>"


class RequestTimeoutMiddleware:
    """Fail a request that outlives ``seconds`` with 504.

    Once the response has started streaming there is nothing honest left to do
    -- the status line is already on the wire -- so the timeout only applies
    before the first byte is sent. That is the window where a stuck dependency
    holds a connection open forever.
    """

    def __init__(self, app: Any, *, seconds: float) -> None:
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.seconds):
                await self.app(scope, receive, tracking_send)
        except TimeoutError:
            if started:
                raise
            await _send_problem(
                send,
                504,
                "Gateway Timeout",
                f"The request did not complete within {self.seconds}s.",
            )

    def __repr__(self) -> str:
        return f"<RequestTimeoutMiddleware seconds={self.seconds}>"


def _header(scope: Scope, name: bytes) -> str | None:
    """One request header, with repeats joined the way a proxy chain builds them."""
    values = [value.decode("latin-1") for key, value in scope.get("headers", []) if key == name]
    return ", ".join(values) if values else None


# -- security headers --------------------------------------------------

#: Hosts the framework's own generated output loads from.
CSP_HTMX_CDN = "https://unpkg.com"
CSP_DOCS_CDN = "https://cdn.jsdelivr.net"
CSP_DOCS_FONT_CSS = "https://fonts.googleapis.com"
CSP_DOCS_FONT_FILES = "https://fonts.gstatic.com"
CSP_DOCS_FAVICON = "https://fastapi.tiangolo.com"

#: Every browser permission a generated service has no use for. Anything a
#: service does want -- a camera upload, geolocation -- is added back by
#: setting ``permissions_policy``, which is the direction that fails loudly.
DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(self), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)

DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"


def build_default_csp(*, docs_enabled: bool, html_enabled: bool = True) -> str:
    """The policy the framework's own pages are known to survive.

    ``'unsafe-inline'`` is in here because the output this has to not break
    requires it: both HTMX base templates ship an inline ``htmx:responseError``
    handler and the scaffolder leaves an existing copy alone, and FastAPI's
    ``/docs`` is an inline ``SwaggerUIBundle`` call. A policy that 500s the
    first page of a new service gets switched off within the hour, so the
    default buys the directives that cost nothing -- no framing, no injected
    ``<base>``, no off-origin form post, no plugin embed, and no off-origin
    ``fetch`` or ``<img>`` to exfiltrate to -- and ``docs/deploy.md`` carries
    the path to a policy without it.

    The docs CDNs drop out when the OpenAPI schema is closed, which is what
    production does by default: the tighter policy arrives with the
    environment rather than with an edit somebody has to remember.

    ``html_enabled`` is the same rule applied to the other half. A JSON API
    serves no page, so the HTMX CDN and the inline allowance would be
    permissions granted to a service that renders nothing, kept even with
    `/docs` closed. The permission follows the plugin that needs it.
    """
    inline = ["'unsafe-inline'"] if html_enabled or docs_enabled else []
    script = ["'self'", *inline, *([CSP_HTMX_CDN] if html_enabled else [])]
    style = ["'self'", *inline]
    img = ["'self'", "data:"]
    font = ["'self'", "data:"]
    extra: dict[str, list[str]] = {}

    if docs_enabled:
        script.append(CSP_DOCS_CDN)
        style += [CSP_DOCS_CDN, CSP_DOCS_FONT_CSS]
        img.append(CSP_DOCS_FAVICON)
        font.append(CSP_DOCS_FONT_FILES)
        # ReDoc parses the schema in a worker it builds from a blob URL.
        extra["worker-src"] = ["'self'", "blob:"]

    directives: dict[str, list[str]] = {
        "default-src": ["'self'"],
        "script-src": script,
        "style-src": style,
        "img-src": img,
        "font-src": font,
        "connect-src": ["'self'"],
        **extra,
        # No fallback to default-src for these three, so they have to be said.
        "frame-ancestors": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "object-src": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(sources)}" for name, sources in directives.items())


class SecurityHeadersMiddleware:
    """Response headers that tell a browser what this service will not do.

    A header the application already set is left alone: one route that needs a
    looser policy sets its own and the rest of the service stays strict.

    ``X-Frame-Options`` goes out alongside CSP ``frame-ancestors`` rather than
    instead of it. The CSP directive is the one that supersedes it, but it is
    ignored in a report-only policy and in the embedded WebViews and IE-mode
    frames that are still the reason clickjacking gets reported at all -- and
    two headers saying the same thing costs 24 bytes.
    """

    def __init__(
        self,
        app: Any,
        *,
        csp: str | None = None,
        csp_report_only: bool = False,
        frame_options: str | None = "DENY",
        referrer_policy: str | None = DEFAULT_REFERRER_POLICY,
        permissions_policy: str | None = DEFAULT_PERMISSIONS_POLICY,
        hsts_seconds: int = 0,
        hsts_include_subdomains: bool = True,
        hsts_preload: bool = False,
    ) -> None:
        self.app = app
        headers: list[tuple[bytes, bytes]] = [(b"x-content-type-options", b"nosniff")]
        if csp:
            name = (
                b"content-security-policy-report-only"
                if csp_report_only
                else (b"content-security-policy")
            )
            headers.append((name, csp.encode("latin-1")))
        if frame_options:
            headers.append((b"x-frame-options", frame_options.encode("latin-1")))
        if referrer_policy:
            headers.append((b"referrer-policy", referrer_policy.encode("latin-1")))
        if permissions_policy:
            headers.append((b"permissions-policy", permissions_policy.encode("latin-1")))
        self.headers = tuple(headers)

        hsts = ""
        if hsts_seconds > 0:
            hsts = f"max-age={hsts_seconds}"
            if hsts_include_subdomains:
                hsts += "; includeSubDomains"
            if hsts_preload:
                hsts += "; preload"
        self.hsts = hsts.encode("latin-1") if hsts else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # RFC 6797: a browser ignores HSTS arriving over cleartext, and a
        # developer reading `curl -I http://localhost` should not see the
        # service claim a guarantee nothing is enforcing.
        hsts = self.hsts if scope.get("scheme") == "https" else None

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                headers += [(name, value) for name, value in self.headers if name not in present]
                if hsts is not None and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", hsts))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def __repr__(self) -> str:
        return f"<SecurityHeadersMiddleware headers={len(self.headers)}>"


# -- trusted proxies ---------------------------------------------------


def _parse_address(raw: str | None) -> IPAddress | None:
    """One hop of a forwarded chain as an address, or ``None`` if it is not one.

    Chains carry ports (``198.51.100.7:41234``), bracketed IPv6
    (``[2001:db8::1]:443``) and RFC 7239's ``unknown`` and ``_obfuscated``
    placeholders. Only the first two identify anybody. IPv4-mapped IPv6 is
    folded back to IPv4 so ``::ffff:10.0.0.5`` matches a ``10.0.0.0/8`` entry
    -- a dual-stack listener produces that form and nobody writes CIDRs for it.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.startswith("["):
        value = value[1:].partition("]")[0]
    elif value.count(":") == 1:
        value = value.partition(":")[0]
    try:
        parsed: IPAddress = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


class TrustedProxies:
    """Which peers are allowed to speak for somebody else.

    ``"*"`` trusts every peer. That is the only workable answer on a platform
    whose front end has no stable address, and it is wrong anywhere the
    service can also be reached directly, so it is never the default.
    """

    def __init__(self, entries: Iterable[str]) -> None:
        self.trust_all = False
        networks: list[IPNetwork] = []
        for entry in entries:
            value = entry.strip()
            if not value:
                continue
            if value == "*":
                self.trust_all = True
                continue
            networks.append(ipaddress.ip_network(value, strict=False))
        self.networks: tuple[IPNetwork, ...] = tuple(networks)

    def is_trusted(self, address: str | None) -> bool:
        if self.trust_all:
            return True
        parsed = _parse_address(address)
        if parsed is None:
            return False
        return any(parsed in network for network in self.networks)

    def resolve(self, peer: str | None, forwarded_for: str | None) -> str | None:
        """The client address, given who we are talking to and what they claim.

        Walks the chain from the right and stops at the first hop that is not
        one of ours. Everything further left was appended by somebody with no
        claim on our trust -- the client included, which is the whole reason
        this is not a one-line ``headers["x-forwarded-for"].split(",")[0]``.
        """
        if peer is None or not self.is_trusted(peer):
            return peer
        hops = [hop.strip() for hop in (forwarded_for or "").split(",") if hop.strip()]
        if not hops:
            return peer
        for hop in reversed(hops):
            parsed = _parse_address(hop)
            # A hop that is not an address means the chain is forged or a
            # proxy obfuscated it. Believing the rest of it is worse than
            # believing none of it.
            if parsed is None:
                return peer
            if not self.is_trusted(hop):
                return str(parsed)
        # Every hop is one of our own proxies, so the chain never reached a
        # client. The leftmost is the closest thing to one it carries.
        leftmost = _parse_address(hops[0])
        return str(leftmost) if leftmost is not None else peer

    def resolve_proto(self, peer: str | None, forwarded_proto: str | None) -> str | None:
        """The scheme the outermost proxy saw, which is the leftmost value."""
        if not forwarded_proto or peer is None or not self.is_trusted(peer):
            return None
        first = forwarded_proto.partition(",")[0].strip().lower()
        return first if first in {"http", "https"} else None

    def __repr__(self) -> str:
        inner = "*" if self.trust_all else ", ".join(str(n) for n in self.networks)
        return f"<TrustedProxies {inner}>"


SUBSTITUTED_PEER_WARNING = (
    "The peer address on this request also appears in its X-Forwarded-For "
    "chain, so something in front of the application already replaced it and "
    "the real transport peer is gone. trusted_proxies cannot be applied to an "
    "address that was never a peer, so this connection is being treated as "
    "having no client at all. The usual cause is uvicorn's own proxy handling, "
    "which is on by default: start it with --no-proxy-headers (jfast serve, "
    "jfast dev and the generated Dockerfile already do)."
)


def _peer_came_from_the_chain(peer: str | None, forwarded_for: str | None) -> bool:
    """Whether ``peer`` is an address the request itself supplied.

    A proxy appends the address it received the connection *from*, never its
    own, so a genuine transport peer does not appear in the chain it is
    relaying. An address that does appear there was either written into
    ``scope["client"]`` by an outer layer that read the same header -- uvicorn's
    ``ProxyHeadersMiddleware``, which leaves no other trace -- or put there by a
    direct client naming itself.

    Those two are byte-identical in the scope and cannot be told apart, which
    is the whole reason the rewrite is invisible. Both are handled as the
    dangerous one.

    The one legitimate topology this costs is two proxies sharing an address --
    both bound to loopback, the inner one appending the outer's -- which lands
    here as a peer in its own chain and loses per-client identity. Narrowing
    the rule to untrusted peers would spare it and reopen the hole: uvicorn
    substitutes whatever the header names, so an attacker would name an address
    inside ``trusted_proxies`` and rotate through the range.
    """
    if peer is None or not forwarded_for:
        return False
    parsed_peer = _parse_address(peer)
    if parsed_peer is None:
        return False
    return any(_parse_address(hop) == parsed_peer for hop in forwarded_for.split(","))


def _cleartext_scheme(scope: Scope) -> str:
    return "ws" if scope["type"] == "websocket" else "http"


def _scheme_may_be_the_header(scope: Scope, forwarded_proto: str | None) -> bool:
    """Whether ``scope["scheme"]`` could have been written from the header.

    Only the transport knows whether TLS was really used, and an outer layer
    that overwrote the scheme did not record what it replaced. A scheme that
    disagrees with every value the header carries proves nothing rewrote it, so
    it is the transport's and is kept; one that matches is indistinguishable
    from a forged one and is not.
    """
    if not forwarded_proto:
        return False
    current = str(scope.get("scheme", "")).lower()
    websocket = scope["type"] == "websocket"
    for value in forwarded_proto.split(","):
        claim = value.strip().lower()
        # A websocket scope carries ws/wss, and every layer that writes one
        # from this header maps http/https across.
        if claim == current or (websocket and claim.replace("http", "ws") == current):
            return True
    return False


class ProxyHeadersMiddleware:
    """Client address and scheme, taken from headers only a trusted peer set.

    ``scope["client"]`` is rewritten rather than a new key added, so
    ``request.client.host`` -- which every log line, audit record and rate
    limiter already reads -- becomes the real client without any of them
    knowing this exists. ``request.state.client_ip`` carries the same value
    for code that would rather be explicit, and ``client_ip()`` reads either.

    Installed even when nothing is trusted, so that answer exists in every
    deployment: with an empty list it is the peer address, unconditionally.

    This has to be the only thing in the process resolving a client address.
    Two resolvers is not a redundancy, it is a bypass: uvicorn ships its own
    with loopback trusted and runs it before any application middleware, so on
    a service reachable from its own host -- a sidecar, anything in the same
    network namespace -- every request could pick its own address before this
    ever saw one. Every launcher the framework owns turns that off, and a peer
    that arrives already substituted is caught here rather than believed.
    """

    def __init__(self, app: Any, *, trusted: TrustedProxies) -> None:
        self.app = app
        self.trusted = trusted
        self._warned = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if state.get("client_ip"):
            # An ASGI layer outside this app already resolved the client -- a
            # server adapter, a serverless shim. It sat closer to the transport
            # than this does, so it wins; two resolvers disagreeing is worse
            # than either answer. Nothing a request carries can reach here.
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer = client[0] if client else None
        forwarded_for = _header(scope, b"x-forwarded-for")
        forwarded_proto = _header(scope, b"x-forwarded-proto")

        if _peer_came_from_the_chain(peer, forwarded_for):
            if not self._warned:
                self._warned = True
                logger.error(SUBSTITUTED_PEER_WARNING)
            # No peer means no trust and no chain to walk. ASGI allows a null
            # client, `client_ip()` already reports it as "unknown", and one
            # shared bucket is the answer an attacker cannot rotate out of.
            scope["client"] = None
            state["client_ip"] = ""
            if _scheme_may_be_the_header(scope, forwarded_proto):
                scope["scheme"] = _cleartext_scheme(scope)
            await self.app(scope, receive, send)
            return

        resolved = self.trusted.resolve(peer, forwarded_for)
        if resolved is not None and resolved != peer:
            scope["client"] = (resolved, client[1] if client else 0)

        proto = self.trusted.resolve_proto(peer, forwarded_proto)
        if proto is not None:
            scope["scheme"] = proto if scope["type"] != "websocket" else proto.replace("http", "ws")
        elif _scheme_may_be_the_header(scope, forwarded_proto):
            # The peer is not trusted to speak for the scheme, and the scheme
            # in the scope is one the header could have produced. Downgrading
            # costs an HSTS header on a request that sent X-Forwarded-Proto;
            # believing it releases HSTS on a claim nothing verified.
            scope["scheme"] = _cleartext_scheme(scope)

        # ASGI servers hand every request its own copy of the lifespan state,
        # so this does not leak into the next one.
        state["client_ip"] = resolved or ""
        await self.app(scope, receive, send)

    def __repr__(self) -> str:
        return f"<ProxyHeadersMiddleware trusted={self.trusted!r}>"


def client_ip(request: Any) -> str:
    """The client address, resolved through the trusted proxy chain.

    Takes a Starlette ``Request`` or a raw ASGI scope. Falls back to the peer
    address so it still answers in a unit test that never built an app, and to
    ``"unknown"`` only when ASGI reported no client at all -- which anything
    keyed on this must treat as one shared bucket, not as an identity.
    """
    scope: Scope = getattr(request, "scope", request)
    state = scope.get("state") or {}
    resolved = state.get("client_ip")
    if resolved:
        return str(resolved)
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"
