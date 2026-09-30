"""Security response headers, and a CSP checked against what jfast generates.

A grep for ``content-security-policy``, ``strict-transport-security``,
``x-frame-options``, ``referrer-policy`` or ``permissions-policy`` across
``src/`` returned nothing, so every generated app was frameable and an XSS in
one had no ceiling.

The half of this file below the divider is the part that matters. A policy is
only worth shipping enabled if the framework's own output survives it, so the
sources the shipped templates and FastAPI's ``/docs`` actually load are
extracted and checked against the policy rather than asserted by hand.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.responses import Response

import jfastframework
from jfastframework.middleware import build_default_csp
from jfastframework.testing import build_test_app, client_for

router = APIRouter()


@router.get("/quick")
async def quick() -> dict[str, str]:
    return {"ok": "yes"}


def _app(**overrides: object):  # type: ignore[no-untyped-def]
    return build_test_app(plugins=[], routers=[router], **overrides)


# -- the headers exist at all ------------------------------------------


async def test_a_default_install_sends_the_baseline_headers() -> None:
    async with client_for(_app()) as client:
        headers = (await client.get("/quick")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "permissions-policy" in headers
    assert "content-security-policy" in headers


async def test_clickjacking_is_closed_by_both_mechanisms() -> None:
    """``frame-ancestors`` is the modern one; the old header is not dead yet."""
    async with client_for(_app()) as client:
        headers = (await client.get("/quick")).headers
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


async def test_the_headers_reach_a_response_the_edge_produced() -> None:
    """A 413 is still a response a browser renders."""
    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/quick", content=b"x" * 4096)
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_policy_the_application_set_itself_wins() -> None:
    """One route needing a looser policy must not have to disable the rest."""
    own = APIRouter()

    @own.get("/own-policy")
    async def own_policy() -> Response:
        return JSONResponse({"ok": "yes"}, headers={"content-security-policy": "default-src *"})

    app = build_test_app(plugins=[], routers=[router, own])
    async with client_for(app) as client:
        response = await client.get("/own-policy")
    assert response.headers["content-security-policy"] == "default-src *"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_report_only_moves_the_policy_to_the_other_header() -> None:
    async with client_for(_app(csp_report_only=True)) as client:
        headers = (await client.get("/quick")).headers
    assert "content-security-policy-report-only" in headers
    assert "content-security-policy" not in headers


async def test_the_headers_can_be_turned_off_wholesale() -> None:
    async with client_for(_app(security_headers=False)) as client:
        headers = (await client.get("/quick")).headers
    assert "content-security-policy" not in headers
    assert "x-frame-options" not in headers


# -- HSTS: sticky, so it must not fire in development ------------------


async def test_hsts_is_absent_in_local_development() -> None:
    """Months of poisoned ``http://localhost`` is not an acceptable default."""
    async with client_for(_app()) as client:
        headers = (await client.get("/quick")).headers
    assert "strict-transport-security" not in headers


async def test_hsts_is_absent_in_production_over_plain_http() -> None:
    async with client_for(_app(env="prod")) as client:
        headers = (await client.get("/quick")).headers
    assert "strict-transport-security" not in headers


async def test_hsts_is_sent_in_production_over_https() -> None:
    async with client_for(_app(env="prod")) as client:
        headers = (await client.get("https://test/quick")).headers
    assert headers["strict-transport-security"].startswith("max-age=")
    assert "includeSubDomains" in headers["strict-transport-security"]


async def test_hsts_can_be_asked_for_explicitly_outside_production() -> None:
    async with client_for(_app(hsts_seconds=600)) as client:
        headers = (await client.get("https://test/quick")).headers
    assert headers["strict-transport-security"].startswith("max-age=600")


# -- the scheme is the gate, so the scheme is an attack surface ---------
#
# ``X-Forwarded-Proto: https`` is one header away from making a cleartext
# request look like a secured one, and the gate above releases HSTS on exactly
# that signal. TEST-NET-3 is outside the default ``trusted_proxies``; the
# compose bridge address is inside it.
UNTRUSTED_PEER = "203.0.113.9"
TRUSTED_PEER = "172.18.0.5"


@asynccontextmanager
async def _client_from(peer: str, **overrides: object) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose requests arrive from ``peer``, the way a socket would."""
    app = _app(**overrides)
    transport = httpx.ASGITransport(app=app, client=(peer, 51234))
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def test_a_forged_proto_from_an_untrusted_peer_does_not_release_hsts() -> None:
    """The cleartext request that would otherwise claim a guarantee."""
    async with _client_from(UNTRUSTED_PEER, hsts_seconds=600) as client:
        response = await client.get("/quick", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in response.headers


async def test_a_trusted_proxy_still_releases_hsts() -> None:
    """The case the header exists for: TLS terminated one hop in front."""
    async with _client_from(TRUSTED_PEER, hsts_seconds=600) as client:
        response = await client.get("/quick", headers={"X-Forwarded-Proto": "https"})
    assert response.headers["strict-transport-security"].startswith("max-age=600")


async def test_a_forged_proto_cannot_downgrade_a_request_that_really_was_tls() -> None:
    """A scheme the header could not have produced is the transport's own."""
    async with _client_from(UNTRUSTED_PEER, hsts_seconds=600) as client:
        response = await client.get("https://test/quick", headers={"X-Forwarded-Proto": "http"})
    assert response.headers["strict-transport-security"].startswith("max-age=600")


# ======================================================================
# The policy against the framework's own output.
# ======================================================================

TEMPLATE_ROOT = Path(jfastframework.__file__).parent / "templates"

# Only the templates this middleware's headers actually cover: pages the
# service itself serves. A built SPA is served by Caddy from <frontend>/dist.
SERVED_TEMPLATES = (
    TEMPLATE_ROOT / "ui_htmx" / "templates" / "base.html.j2",
    TEMPLATE_ROOT / "service_web" / "templates" / "base.html.j2",
)

_URL_ATTR = re.compile(r"""(?:src|href)\s*=\s*["'](https?://[^"']+)["']""", re.IGNORECASE)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
_INLINE_STYLE = re.compile(r"<style[^>]*>", re.IGNORECASE)

# Which directive governs which tag, for the checks below.
_SCRIPT, _STYLE, _IMG = "script-src", "style-src", "img-src"


def _sources(policy: str, directive: str) -> list[str]:
    """Sources for a directive, following the ``default-src`` fallback."""
    parsed = {part.split()[0]: part.split()[1:] for part in policy.split(";") if part.strip()}
    if directive in parsed:
        return parsed[directive]
    return parsed.get("default-src", [])


def _permits(policy: str, directive: str, url: str) -> bool:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    sources = _sources(policy, directive)
    return origin in sources or f"{urlsplit(url).scheme}:" in sources


def _permits_inline(policy: str, directive: str) -> bool:
    return "'unsafe-inline'" in _sources(policy, directive)


def _directive_for(url: str, html: str) -> str:
    """Whether this URL is loaded as a script, a stylesheet or an image."""
    if re.search(rf'<script[^>]*src\s*=\s*["\']{re.escape(url)}', html, re.IGNORECASE):
        return _SCRIPT
    if re.search(rf'rel\s*=\s*["\']stylesheet["\'][^>]*{re.escape(url)}', html, re.IGNORECASE):
        return _STYLE
    if re.search(rf'{re.escape(url)}[^>]*rel\s*=\s*["\']stylesheet', html, re.IGNORECASE):
        return _STYLE
    return _IMG


def test_the_default_policy_permits_the_htmx_the_web_plugin_points_at() -> None:
    """``[plugin.web] htmx_cdn`` is on by default, so the CDN is jfast's own."""
    from jfastframework.plugins.builtin.web import WebSettings

    settings = WebSettings(_env_file=None)  # type: ignore[call-arg]
    src = f"https://unpkg.com/htmx.org@{settings.htmx_version}"
    assert settings.htmx_cdn is True
    assert _permits(build_default_csp(docs_enabled=False), _SCRIPT, src)


def test_the_default_policy_permits_the_inline_scripts_the_templates_ship() -> None:
    """Neither base template can be edited into shape from here.

    Both carry an inline ``htmx:responseError`` handler, and the scaffolder
    leaves an existing copy alone, so any policy that forbids inline scripts
    breaks the first page of a ``--kind web`` service.
    """
    policy = build_default_csp(docs_enabled=False)
    for template in SERVED_TEMPLATES:
        html = template.read_text(encoding="utf-8")
        if _INLINE_SCRIPT.search(html):
            assert _permits_inline(policy, _SCRIPT), template
        if _INLINE_STYLE.search(html):
            assert _permits_inline(policy, _STYLE), template


def test_the_default_policy_permits_every_literal_url_in_a_served_template() -> None:
    policy = build_default_csp(docs_enabled=False)
    for template in SERVED_TEMPLATES:
        html = template.read_text(encoding="utf-8")
        for url in _URL_ATTR.findall(html):
            assert _permits(policy, _directive_for(url, html), url), (template, url)


async def test_the_swagger_page_loads_nothing_the_default_policy_forbids() -> None:
    """/docs is open outside production, and it is jfast's own output too."""
    async with client_for(_app()) as client:
        response = await client.get("/docs")
    assert response.status_code == 200
    html = response.text
    policy = response.headers["content-security-policy"]

    for url in _URL_ATTR.findall(html):
        assert _permits(policy, _directive_for(url, html), url), url
    assert _INLINE_SCRIPT.search(html), "swagger stopped using an inline init script"
    assert _permits_inline(policy, _SCRIPT)
    # Swagger UI injects its own <style> blocks once the bundle runs.
    assert _permits_inline(policy, _STYLE)


async def test_production_drops_the_docs_cdns_from_the_policy() -> None:
    """No ``/docs`` in production, so nothing has to be allowed for it."""
    async with client_for(_app(env="prod")) as client:
        policy = (await client.get("/quick")).headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in policy
    assert "fonts.googleapis.com" not in policy


# -- the policy follows what the service actually renders ---------------


def test_a_json_api_is_granted_neither_inline_script_nor_the_htmx_cdn() -> None:
    """A JSON API renders no markup, so it needs no permission to run any.

    Both allowances are here for pages: the HTMX templates ship an inline
    handler and `/docs` is an inline SwaggerUIBundle call. A deployed API with
    the schema already closed kept both -- permission granted to output that
    does not exist, which is the definition of a policy nobody tightened.
    """
    policy = build_default_csp(docs_enabled=False, html_enabled=False)

    assert "'unsafe-inline'" not in policy
    assert "unpkg.com" not in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


def test_the_web_plugin_asks_for_what_its_templates_need() -> None:
    """And gets it. The generated HTMX base loads htmx from unpkg and carries
    an inline error handler, so a policy without these 500s the first page."""
    policy = build_default_csp(docs_enabled=False, html_enabled=True)

    assert "unpkg.com" in policy
    assert "'unsafe-inline'" in policy


def test_open_docs_still_get_their_inline_allowance_on_an_api() -> None:
    """`/docs` is FastAPI's own inline call, so closing the API's markup
    permissions must not close the documentation a developer is reading."""
    policy = build_default_csp(docs_enabled=True, html_enabled=False)

    assert "'unsafe-inline'" in policy
    assert "cdn.jsdelivr.net" in policy
