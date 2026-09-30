"""System endpoints every JFast service exposes.

``/health``  liveness  -- the process is up. Cheap, no dependency probing.
``/ready``   readiness -- every critical plugin reports healthy. This is the
             one your load balancer and orchestrator should poll.
``/info``    build and plugin inventory. Disabled in production by default.

Readiness runs every plugin's check **concurrently and under a timeout**. Both
halves matter. Run serially, a service with five dependencies pays the sum of
five round-trips on every probe, and an orchestrator polls this every few
seconds. Without a timeout, a dependency that hangs at the TCP level -- not
refused, hung -- holds the probe open until the socket gives up, which reads to
Kubernetes as a slow service rather than a broken dependency.

Criticality has one rule: a failure fails readiness (503, ``unavailable``)
only when the plugin is declared ``health_critical`` *and* the report says
critical. Everything else that is unhealthy is ``degraded`` and answers 200, so
an orchestrator keeps the replica in rotation while a cache, a mail server or a
rate-limit backend is down. docs/resilience.md lists which plugin is which.

A check that times out is reported as ``timeout``, distinct from ``fail``: one
means the dependency answered "no", the other means it did not answer at all,
and whoever is reading this at three in the morning needs to tell them apart.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from jfastframework.context import AppContext
    from jfastframework.plugins.base import Plugin


async def _probe(plugin: Plugin, ctx: AppContext, timeout: float) -> dict[str, Any]:
    """One plugin's readiness, and never an exception.

    A probe that can 500 is a probe that reports the whole service as down
    whenever one health check has a bug in it.
    """
    try:
        async with asyncio.timeout(timeout):
            report = await plugin.health(ctx)
    except TimeoutError:
        return {
            "healthy": False,
            "status": "timeout",
            "detail": f"health check did not answer within {timeout}s",
            # The plugin never got to say how bad this is, so its declared
            # criticality decides. A hung cache still must not fail readiness.
            "critical": plugin.meta.health_critical,
        }
    except Exception as exc:  # noqa: BLE001 - a probe must never 500
        return {
            "healthy": False,
            "status": "error",
            "detail": f"health check raised: {exc}",
            # A bug in a cache's health check is still the cache's problem:
            # it used to be reported critical and took every replica out of
            # rotation over a dependency the service promises to live without.
            "critical": plugin.meta.health_critical,
        }

    entry: dict[str, Any] = {
        "healthy": report.healthy,
        "status": "ok" if report.healthy else "fail",
        "detail": report.detail,
        # A plugin declared non-critical cannot fail readiness, whatever one
        # of its reports says. `HealthReport.fail()` defaults to critical, so
        # a forgotten `critical=False` on one branch of a cache's check was
        # enough to turn "degraded" into "unavailable". A critical plugin may
        # still report a failure as non-critical (auth's token store).
        "critical": report.critical and plugin.meta.health_critical,
    }
    if report.meta:
        entry["meta"] = report.meta
    return entry


def build_system_router(ctx: AppContext, plugins: list[Plugin]) -> APIRouter:
    router = APIRouter(tags=["system"])
    settings = ctx.settings

    @router.get("/health", summary="Liveness probe")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.version,
            "env": settings.env,
        }

    @router.get("/ready", summary="Readiness probe")
    async def ready() -> JSONResponse:
        timeout = settings.readiness_timeout
        results = await asyncio.gather(*(_probe(plugin, ctx, timeout) for plugin in plugins))
        checks = {plugin.meta.name: result for plugin, result in zip(plugins, results, strict=True)}

        failed = any(not c["healthy"] and c["critical"] for c in checks.values())
        degraded = any(not c["healthy"] and not c["critical"] for c in checks.values())

        status = "unavailable" if failed else ("degraded" if degraded else "ok")
        return JSONResponse(
            status_code=503 if failed else 200,
            content={"status": status, "service": settings.app_name, "checks": checks},
        )

    if not settings.is_production:

        @router.get("/info", summary="Build and plugin inventory")
        async def info() -> dict[str, Any]:
            return {
                "service": settings.app_name,
                "version": settings.version,
                "env": settings.env,
                "providers": list(ctx.providers),
                "plugins": [plugin.describe() for plugin in plugins],
            }

    return router
