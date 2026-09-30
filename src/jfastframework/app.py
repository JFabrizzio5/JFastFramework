"""Application factory.

A JFast service's ``main.py`` is this::

    from jfastframework import create_app

    app = create_app()

Everything else -- middleware, observability, database, routers -- arrives
through the plugin graph resolved from ``jfast.toml``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import BaseRoute, Match, Route

from jfastframework.context import AppContext
from jfastframework.errors import PluginError, install_error_handlers
from jfastframework.health import add_system_routes
from jfastframework.middleware import (
    BodySizeLimitMiddleware,
    ProxyHeadersMiddleware,
    RequestTimeoutMiddleware,
    SecurityHeadersMiddleware,
    TrustedProxies,
)
from jfastframework.plugins import registry
from jfastframework.plugins.base import Plugin
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig, JFastSettings

logger = logging.getLogger("jfast")


def _native_telemetry_off() -> dict[str, Any]:
    """Turn off FastAPI's own OpenTelemetry (0.14x+), where it exists.

    Found tracing a real service to Jaeger: on seeing
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` -- the variable the ``telemetry`` plugin
    reads -- FastAPI configured a second, global provider with no service
    name, exported a duplicate server span for every request, and would have
    exported logs carrying exception messages and the rejected input values.
    Traces here come from the ``telemetry`` plugin alone, which never exports
    content. FastAPI's documented switch when another component owns export.
    """
    import inspect

    if "telemetry" not in inspect.signature(FastAPI.__init__).parameters:
        return {}
    return {
        "telemetry": {
            "auto_configure": False,
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
        }
    }


def create_app(
    *,
    config: JFastConfig | None = None,
    config_path: str | Path | None = DEFAULT_CONFIG_FILE,
    overrides: dict[str, Any] | None = None,
    plugins: Sequence[type[Plugin]] | None = None,
    routers: Sequence[APIRouter] | None = None,
) -> FastAPI:
    """Build a configured FastAPI application.

    Args:
        config: Pre-built config. Skips loading ``jfast.toml``.
        config_path: Path to ``jfast.toml``. ``None`` to use env vars only.
        overrides: Kernel settings overrides, highest precedence.
        plugins: Extra plugin classes to register without installing them as
            distributions. Useful in tests and for private in-repo plugins.
        routers: Application routers to mount after plugins have registered.
    """
    cfg = config or JFastConfig.load(config_path=config_path, overrides=overrides)
    settings = cfg.settings

    resolved = registry.build(cfg, extra_plugins=list(plugins or []))

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        root_path=settings.root_path,
        docs_url=settings.effective_docs_url,
        openapi_url=settings.effective_openapi_url,
        lifespan=_build_lifespan(resolved),
        **_native_telemetry_off(),
    )

    # Before plugins, so their middleware runs inside these. A body that is too
    # large should be refused before observability logs it as a request served.
    _install_edge_middleware(app, settings)

    ctx = AppContext(app=app, config=cfg)
    # Plugins and the lifespan reach the context through app.state; nothing
    # else in the framework uses module-level globals.
    app.state.jfast = ctx
    app.state.plugins = resolved

    install_error_handlers(app, debug=settings.debug)

    for plugin in resolved:
        logger.debug("registering plugin %s", plugin.meta.name)
        try:
            plugin.register(ctx)
        except ModuleNotFoundError as exc:
            # A plugin imports its client library inside `register`, so an
            # uninstalled extra surfaces here as `No module named 'motor'` --
            # the distribution's name, which is not what anyone has to type.
            # `PluginMeta.extra` holds the exact command, so the failure that
            # needs it names it.
            if plugin.meta.extra:
                raise PluginError(
                    f"plugin {plugin.meta.name!r} needs a dependency that is not "
                    f'installed ({exc.name}): pip install "{plugin.meta.extra}"'
                ) from exc
            raise

    add_system_routes(app, ctx, resolved)
    # Everything so far is the framework's: FastAPI's docs, what the plugins
    # registered, the system endpoints. Remembered by identity; the lifespan
    # moves them behind the application's routes once startup is over.
    app.state.jfast_framework_routes = tuple(app.router.routes)

    for router in routers or []:
        app.include_router(router)

    logger.info(
        "%s built with plugins: %s",
        settings.app_name,
        ", ".join(p.meta.name for p in resolved) or "<none>",
    )
    return app


def _install_edge_middleware(app: FastAPI, settings: JFastSettings) -> None:
    """Proxy headers, security headers, host validation, CORS, body, timeout.

    Starlette applies middleware in reverse registration order, so the last one
    added is the outermost. Registration here therefore reads inside-out:
    timeout and body limit closest to the application, then CORS, then the host
    check -- a request for a host this service does not serve is rejected
    before anything else looks at it, and an error response still carries its
    CORS headers, which is the only way the browser will show it.

    The two outermost are the two that have to be. Security headers sit
    outside everything so a 400, a 413 and a 504 carry them too; a browser
    renders those. Proxy headers sit outside that, because ``scope["client"]``
    and ``scope["scheme"]`` have to be the real ones before anything -- the
    HSTS check included -- reads them.
    """
    timeout = settings.effective_request_timeout
    if timeout is not None:
        app.add_middleware(RequestTimeoutMiddleware, seconds=timeout)

    max_body = settings.effective_max_body_bytes
    if max_body is not None:
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

    if settings.cors_origins or settings.cors_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_origin_regex=settings.cors_origin_regex,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=settings.cors_allow_methods,
            allow_headers=settings.cors_allow_headers,
        )

    if settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    if settings.security_headers:
        app.add_middleware(
            SecurityHeadersMiddleware,
            csp=settings.effective_csp,
            csp_report_only=settings.csp_report_only,
            frame_options=settings.frame_options,
            referrer_policy=settings.referrer_policy,
            permissions_policy=settings.permissions_policy,
            hsts_seconds=settings.effective_hsts_seconds,
            hsts_include_subdomains=settings.hsts_include_subdomains,
            hsts_preload=settings.hsts_preload,
        )

    # Always, even with an empty list: `client_ip()` has to answer the same
    # way in every deployment, and with nothing trusted that answer is the
    # peer address.
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted=TrustedProxies(settings.trusted_proxies),
    )


def order_framework_routes_last(app: FastAPI) -> None:
    """Move the framework's fixed-path routes behind the application's.

    Starlette tries routes in order, and the first to match wins. Registered
    first, ``/health``, ``/ready``, ``/info``, ``/metrics`` and the docs are
    tried -- and fail -- on every request to every application route: about
    8 us per request on 0.1.0a10, for probes that arrive once every few
    seconds. Behind the application's routes they cost those requests nothing
    and cost themselves one pass over the route table.

    What must not change is who answers. Today a framework route wins over an
    application route that would also match its path, a ``/{slug}`` catch-all
    for instance; moved blindly, the catch-all would start answering
    ``/health``. So each moved route is probed with its own path and method,
    and when an application route in front would claim it -- a full match, or
    a partial one that would turn it into a 405 -- the framework route goes
    back directly in front of that route. The winner for every path is the
    same as before; only requests that were never going to match it stop
    paying for it.

    Only fixed paths move. A route with parameters matches paths nobody can
    enumerate, so there is no probe that proves moving it is harmless; those
    stay where they were.

    It runs at the end of startup, never earlier. Probing an included router
    makes FastAPI (0.121+) build and cache that router's resolved routes, and
    a plugin that edits routes at startup -- ``ratelimit`` puts its default
    limit on every route there -- would then edit copies nobody serves.
    After startup nothing edits routes, and routers included after
    ``create_app`` returned are in the table too. Idempotent.
    """
    framework: tuple[BaseRoute, ...] = getattr(app.state, "jfast_framework_routes", ())
    routes = app.router.routes
    present = {id(route) for route in routes}
    movable = [route for route in framework if id(route) in present and _fixed_path(route)]
    if not movable:
        return
    moved = {id(route) for route in movable}
    for route in movable:
        routes.remove(route)
    routes.extend(movable)

    for route in movable:
        assert isinstance(route, Route)
        probe = _probe_scope(route)
        position = routes.index(route)
        for index, candidate in enumerate(routes[:position]):
            if id(candidate) in moved:
                continue
            match, _ = candidate.matches(dict(probe))
            if match is not Match.NONE:
                routes.pop(position)
                routes.insert(index, route)
                logger.debug(
                    "%s stays in front of %r, which would otherwise answer it",
                    route.path,
                    candidate,
                )
                break


def _fixed_path(route: BaseRoute) -> bool:
    return isinstance(route, Route) and not route.param_convertors


def _probe_scope(route: Route) -> dict[str, Any]:
    method = sorted(route.methods)[0] if route.methods else "GET"
    return {
        "type": "http",
        "method": method,
        "path": route.path,
        "raw_path": route.path.encode(),
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "headers": [(b"host", b"localhost")],
        "server": ("localhost", 80),
        "client": None,
    }


def _build_lifespan(plugins: list[Plugin]):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx: AppContext = app.state.jfast
        started: list[Plugin] = []
        try:
            for plugin in plugins:
                await plugin.startup(ctx)
                started.append(plugin)
                logger.debug("started plugin %s", plugin.meta.name)
            order_framework_routes_last(app)
            yield
        finally:
            # Reverse order, and one plugin failing to shut down must not
            # prevent the rest from releasing their resources.
            for plugin in reversed(started):
                try:
                    await plugin.shutdown(ctx)
                except Exception:
                    logger.exception("plugin %s failed to shut down", plugin.meta.name)

    return lifespan


def get_context(app: FastAPI) -> AppContext:
    """Retrieve the JFast context from a running app."""
    ctx = getattr(app.state, "jfast", None)
    if ctx is None:
        raise RuntimeError("This app was not built by jfastframework.create_app")
    return ctx  # type: ignore[no-any-return]
