"""What a request costs on top of FastAPI, measured in-process.

``ab`` against uvicorn (the method behind docs/deploy.md#performance) measures
the whole stack -- sockets, HTTP parsing, the event loop's I/O -- and on a
shared CI runner that part swings by tens of percent between two runs of the
same commit. A budget that fails on noise gets switched off, so this drives
each app's ASGI callable directly: no socket, no server, only the code that
the framework adds. What is left is deterministic enough to hold a line.

Every scenario serves the same ``GET /ping`` returning ``{"ok": true}``:

``fastapi``
    FastAPI alone. The denominator of every ratio.
``fastapi_jwt_by_hand``
    FastAPI with an ``async def`` dependency that verifies an HS256 JWT with
    PyJWT and reads the tenant claim -- what a careful team writes without a
    framework. Not budgeted; it is the fair comparison for the next one.
``jfast_defaults``
    ``create_app`` with the default plugins (observability, metrics).
``jfast_auth_tenancy_metrics``
    The default plugins plus ``auth`` (secret mode) and ``tenancy`` (token
    source); the route depends on ``current_tenant``.

Logs are at ``WARNING`` in every JFast scenario: at ``INFO`` the access log
writes one JSON line per request, and this would measure the terminal.

Rounds interleave the scenarios, so a background process slowing the machine
for a second slows all of them in the same round, and the median over rounds
is what is reported. The ratio against bare FastAPI on the same machine is
what the budget in ``tests/test_performance_budget.py`` compares; absolute
microseconds are printed for people and never compared across machines.

    python scripts/bench_overhead.py                 # print a table
    python scripts/bench_overhead.py --json          # machine-readable
    python scripts/bench_overhead.py --write-baseline tests/performance_baseline.json
    python scripts/bench_overhead.py --check tests/performance_baseline.json
"""

import argparse
import asyncio
import gc
import json
import logging
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

SECRET = "bench-secret-long-enough-for-hs256-at-least-32-bytes"
ISSUER = "https://id.bench.local/"
AUDIENCE = "bench"

#: Scenarios the budget holds. The by-hand one is context, not a product.
BUDGETED = ("jfast_defaults", "jfast_auth_tenancy_metrics")
#: How much a ratio may grow over the baseline before the budget fails.
DEFAULT_TOLERANCE = 0.20

ASGIApp = Callable[..., Awaitable[None]]


@dataclass
class Scenario:
    name: str
    app: Any
    headers: list[tuple[bytes, bytes]]


def _token() -> str:
    from jfastframework.auth import issue

    token, _, _ = issue(
        "user-1",
        key=SECRET,
        algorithm="HS256",
        # Long enough that a slow CI run cannot outlive it.
        lifetime=timedelta(hours=6),
        audience=AUDIENCE,
        issuer=ISSUER,
        tenant_id="acme",
    )
    return token


def _bare_fastapi() -> Any:
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def _fastapi_jwt_by_hand() -> Any:
    import jwt
    from fastapi import Depends, FastAPI, HTTPException, Request

    app = FastAPI()

    async def tenant(request: Request) -> str:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401)
        try:
            claims = jwt.decode(
                header[7:], SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
            )
        except jwt.InvalidTokenError as exc:
            raise HTTPException(401) from exc
        return str(claims["tenant_id"])

    @app.get("/ping")
    async def ping(tenant_id: str = Depends(tenant)) -> dict[str, bool]:
        return {"ok": True}

    return app


def _jfast(*, full: bool) -> Any:
    from fastapi import APIRouter, Depends

    from jfastframework.plugins.builtin.tenancy import current_tenant
    from jfastframework.testing import build_test_app

    router = APIRouter()
    raw: dict[str, Any] = {"plugin": {"observability": {"level": "WARNING"}}}
    plugins: list[str] = []

    if full:

        @router.get("/ping")
        async def ping_tenant(tenant: str = Depends(current_tenant)) -> dict[str, bool]:
            return {"ok": True}

        plugins = ["auth", "tenancy", "metrics"]
        raw["plugin"]["auth"] = {
            "mode": "secret",
            "secret": SECRET,
            "algorithms": ["HS256"],
            "issuer": ISSUER,
            "audience": AUDIENCE,
        }
        raw["plugin"]["tenancy"] = {"sources": ["token"]}
    else:

        @router.get("/ping")
        async def ping() -> dict[str, bool]:
            return {"ok": True}

    return build_test_app(plugins=plugins, routers=[router], raw=raw, app_name="bench")


def build_scenarios() -> list[Scenario]:
    auth = [(b"authorization", f"Bearer {_token()}".encode())]
    return [
        Scenario("fastapi", _bare_fastapi(), []),
        Scenario("fastapi_jwt_by_hand", _fastapi_jwt_by_hand(), auth),
        Scenario("jfast_defaults", _jfast(full=False), []),
        Scenario("jfast_auth_tenancy_metrics", _jfast(full=True), auth),
    ]


def _scope(path: str, headers: list[tuple[bytes, bytes]], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"bench.local"), (b"accept", b"*/*"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("bench.local", 80),
        # A copy per request: Starlette hands the lifespan state to each
        # request as a shallow copy, and the server does the same.
        "state": dict(state),
    }


async def drive(app: Any, headers: list[tuple[bytes, bytes]], n: int, state: dict[str, Any]) -> int:
    """Send ``n`` requests through ``app``; returns the last status seen.

    The receive/send pair is what uvicorn would hand the app, minus the
    socket. The template scope is rebuilt per request because middleware is
    allowed to mutate it, and a mutation leaking into the next request would
    measure something no server does.
    """
    request_message = {"type": "http.request", "body": b"", "more_body": False}
    disconnect = {"type": "http.disconnect"}
    status = 0

    for _ in range(n):
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                # Only a streaming response listens past the body, and it
                # would be waiting for the client to go away.
                return disconnect
            delivered = True
            return request_message

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]

        await app(_scope("/ping", headers, state), receive, send)
    return status


async def _lifespan_state(stack: AsyncExitStack, app: Any) -> dict[str, Any]:
    state = await stack.enter_async_context(app.router.lifespan_context(app))
    return dict(state or {})


async def measure(requests: int = 5000, rounds: int = 7, warmup: int = 1000) -> dict[str, Any]:
    """Median CPU microseconds per request of every scenario, and ratios to FastAPI."""
    # The access log and plugin chatter would land in the timing otherwise.
    logging.disable(logging.WARNING)
    try:
        scenarios = build_scenarios()
        samples: dict[str, list[float]] = {s.name: [] for s in scenarios}
        wall: dict[str, list[float]] = {s.name: [] for s in scenarios}
        async with AsyncExitStack() as stack:
            states = {s.name: await _lifespan_state(stack, s.app) for s in scenarios}
            for scenario in scenarios:
                status = await drive(scenario.app, scenario.headers, warmup, states[scenario.name])
                if status != 200:
                    raise RuntimeError(
                        f"{scenario.name} answered {status}; the benchmark would time an error"
                    )
            for round_number in range(rounds):
                # Rotate the order, so no scenario is always the one that
                # runs right after the garbage collector.
                shift = round_number % len(scenarios)
                for scenario in scenarios[shift:] + scenarios[:shift]:
                    gc.collect()
                    gc.disable()
                    try:
                        cpu_started = time.process_time_ns()
                        wall_started = time.perf_counter_ns()
                        await drive(scenario.app, scenario.headers, requests, states[scenario.name])
                        wall_elapsed = time.perf_counter_ns() - wall_started
                        cpu_elapsed = time.process_time_ns() - cpu_started
                    finally:
                        gc.enable()
                    samples[scenario.name].append(cpu_elapsed / requests / 1000)
                    wall[scenario.name].append(wall_elapsed / requests / 1000)
    finally:
        logging.disable(logging.NOTSET)

    medians = {name: statistics.median(values) for name, values in samples.items()}
    base = medians["fastapi"]
    return {
        "machine": _machine(),
        "method": {
            "driver": "in-process ASGI, no server",
            "requests_per_round": requests,
            "rounds": rounds,
            "warmup": warmup,
            # Process CPU time, not wall time: on a shared runner the wall
            # clock also counts the time other processes held the core. CPU
            # time of the whole process still counts a threadpool hop, which
            # is one of the regressions this exists to catch.
            "clock": "process CPU time",
            "statistic": "median of per-round mean",
        },
        "scenarios": {
            name: {
                "us_per_request": round(value, 2),
                "req_per_s_one_core": round(1_000_000 / value),
                "ratio_to_fastapi": round(value / base, 3),
                "overhead_us": round(value - base, 2),
                "spread_us": round(max(samples[name]) - min(samples[name]), 2),
                "wall_us_per_request": round(statistics.median(wall[name]), 2),
            }
            for name, value in medians.items()
        },
    }


def _machine() -> dict[str, str]:
    import fastapi
    import starlette

    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "starlette": starlette.__version__,
        "cpus": str(os.cpu_count()),
    }


def check(result: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> list[str]:
    """The budgeted scenarios whose ratio grew more than ``tolerance``."""
    failures = []
    for name in BUDGETED:
        allowed = baseline["scenarios"][name]["ratio_to_fastapi"] * (1 + tolerance)
        measured = result["scenarios"][name]["ratio_to_fastapi"]
        if measured > allowed:
            failures.append(
                f"{name}: {measured:.2f}x FastAPI, budget {allowed:.2f}x "
                f"(baseline {baseline['scenarios'][name]['ratio_to_fastapi']:.2f}x "
                f"+ {tolerance:.0%})"
            )
    return failures


def render(result: dict[str, Any]) -> str:
    lines = [
        f"{'scenario':<28} {'cpu us/req':>11} {'req/s/core':>11} {'x FastAPI':>10} {'+us':>7}",
    ]
    for name, row in result["scenarios"].items():
        lines.append(
            f"{name:<28} {row['us_per_request']:>11.1f} {row['req_per_s_one_core']:>11,} "
            f"{row['ratio_to_fastapi']:>10.2f} {row['overhead_us']:>7.1f}"
        )
    method = result["method"]
    lines.append(
        f"({method['rounds']} rounds x {method['requests_per_round']} requests, "
        f"{method['statistic']}; {result['machine']['processor']}, "
        f"Python {result['machine']['python']}, FastAPI {result['machine']['fastapi']})"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--requests", type=int, default=5000, help="requests per round")
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--write-baseline", type=Path, help="save the result as the baseline")
    parser.add_argument("--check", type=Path, help="compare against a baseline; exit 1 over it")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    args = parser.parse_args(argv)

    result = asyncio.run(measure(requests=args.requests, rounds=args.rounds))
    print(json.dumps(result, indent=2) if args.json else render(result))

    if args.write_baseline:
        args.write_baseline.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.write_baseline}", file=sys.stderr)
    if args.check:
        failures = check(result, json.loads(args.check.read_text(encoding="utf-8")), args.tolerance)
        for failure in failures:
            print(f"OVER BUDGET  {failure}", file=sys.stderr)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
