"""`jfast bench`: load a running service in steps and say where it breaks.

    jfast bench http://localhost:8000
    jfast bench http://localhost:8000 --route "GET /invoices" --route "GET /invoices/1" \\
        --concurrency 1,8,32,128 --duration 15 --token "$TOKEN"
    jfast bench http://localhost:8000 --k6 load.js     # the same scenario, for k6

The scenario comes from the service itself: its ``/openapi.json`` lists the
routes, and every ``GET`` whose parameters can be filled -- from an example,
a default or an enum in the schema -- is in it. ``--route`` picks routes
instead, as a template from the schema or as a literal path. Writes
(``--method POST``) are included only when the schema carries an example
body: a load test that invents bodies measures the validation errors.

Each step holds a fixed number of concurrent clients for ``--duration``
seconds and reports req/s, p50/p95/p99 and the error rate. The first step
whose p99 or error rate crosses its threshold is **where it breaks**; the
step where throughput stops growing as concurrency rises is where it
**saturates**, which is usually a little earlier and is the number to size
replicas by. After each step ``/ready`` is read, so a dependency that
degraded under the load -- the database pool, the cache -- is named next to
the step that did it.

No external tool is needed: the load comes from this process, with httpx.
That is also its limit: one Python process drives 3,000-3,600 requests a
second (measured on an Apple M5 against a bare FastAPI ``/ping``; ``ab`` did
21,000 against the same server), so against a service faster than that the
generator is what saturates, and the report says so when its own CPU is the
bottleneck. For
more, export the scenario to k6 with ``--k6``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import typer

from jfastframework.cli.exits import Code

#: Framework routes: probes and docs, not the service's work. Left out of
#: a scenario built from the schema unless ``--include-framework``.
FRAMEWORK_PATHS = frozenset(
    {"/health", "/ready", "/info", "/metrics", "/openapi.json", "/docs", "/redoc"}
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PARAM = re.compile(r"{([^}:]+)(?::[^}]*)?}")


@dataclass(frozen=True)
class Target:
    """One request of the scenario."""

    method: str
    path: str
    body: Any = None

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class StepResult:
    concurrency: int
    seconds: float
    requests: int
    errors: int
    non_2xx: int
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    statuses: dict[str, int] = field(default_factory=dict)
    #: ``/ready`` checks that were not ok right after the step, name -> status.
    degraded: dict[str, str] = field(default_factory=dict)
    #: This process's CPU share during the step; near 1.0 means the numbers
    #: describe the load generator, not the service.
    generator_cpu: float = 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass
class BenchReport:
    base_url: str
    targets: list[str]
    thresholds: dict[str, float]
    steps: list[StepResult]
    breaks_at: int | None = None
    break_reason: str | None = None
    saturates_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for step, raw in zip(self.steps, data["steps"], strict=True):
            raw["error_rate"] = round(step.error_rate, 4)
        return data


# -- the scenario ---------------------------------------------------------


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    """Follow one local ``$ref``; a schema is rarely more than one hop deep."""
    if isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"])
        if ref.startswith("#/"):
            target: Any = spec
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return target
    return node


def _example(spec: dict[str, Any], holder: dict[str, Any]) -> tuple[bool, Any]:
    """An example value from a parameter, media type or schema, if it has one."""
    if "example" in holder:
        return True, holder["example"]
    examples = holder.get("examples")
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return True, first["value"]
    if isinstance(examples, list) and examples:
        return True, examples[0]
    schema = _resolve(spec, holder.get("schema", {}))
    if isinstance(schema, dict) and schema is not holder:
        if "example" in schema:
            return True, schema["example"]
        if isinstance(schema.get("examples"), list) and schema["examples"]:
            return True, schema["examples"][0]
        if "default" in schema:
            return True, schema["default"]
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            return True, schema["enum"][0]
    return False, None


def targets_from_openapi(
    spec: dict[str, Any],
    *,
    routes: Sequence[str] = (),
    methods: Iterable[str] = ("GET",),
    include_framework: bool = False,
) -> tuple[list[Target], list[str]]:
    """The requests to send, and why each route left out was left out.

    ``routes`` are ``"METHOD /path"`` or ``"/path"`` (meaning GET). A route
    that names a template in the schema is filled from it; any other path is
    sent as written, so ``GET /invoices/42`` works for a route whose id has
    no example.
    """
    wanted_methods = {m.upper() for m in methods}
    picked: dict[tuple[str, str], bool] = {}
    for route in routes:
        method, _, path = route.strip().partition(" ")
        if not path:
            method, path = "GET", method
        picked[(method.upper(), path.strip())] = False

    targets: list[Target] = []
    skipped: list[str] = []
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            method = method.upper()
            if method not in {"GET", *WRITE_METHODS} or not isinstance(operation, dict):
                continue
            if picked:
                if (method, path) not in picked:
                    continue
                picked[(method, path)] = True
            elif method not in wanted_methods or (
                path in FRAMEWORK_PATHS and not include_framework
            ):
                continue
            target, reason = _fill(spec, method, path, item, operation)
            if target is None:
                skipped.append(f"{method} {path}: {reason}")
            else:
                targets.append(target)

    # Picked routes that are not templates in the schema: literal paths.
    for (method, path), matched in picked.items():
        if not matched:
            targets.append(Target(method, path))
    return targets, skipped


def _fill(
    spec: dict[str, Any], method: str, path: str, item: dict[str, Any], operation: dict[str, Any]
) -> tuple[Target | None, str]:
    parameters = [
        _resolve(spec, p) for p in [*item.get("parameters", []), *operation.get("parameters", [])]
    ]
    values: dict[str, Any] = {}
    query: list[str] = []
    for parameter in parameters:
        location, name = parameter.get("in"), parameter.get("name")
        if location not in ("path", "query"):
            continue
        found, value = _example(spec, parameter)
        if location == "path":
            if not found:
                return None, f"no example for path parameter {name!r}; pass it with --route"
            values[str(name)] = value
        elif found:
            query.append(f"{name}={value}")
        elif parameter.get("required"):
            return None, f"no example for required query parameter {name!r}"

    body = None
    if method in WRITE_METHODS and method != "DELETE":
        content = (operation.get("requestBody") or {}).get("content", {})
        media = content.get("application/json")
        if operation.get("requestBody"):
            if media is None:
                return None, "request body is not JSON"
            found, body = _example(spec, media)
            if not found:
                return None, "no example body in the schema; a guessed body measures a 422"

    concrete = _PARAM.sub(lambda m: str(values.get(m.group(1), m.group(0))), path)
    if query:
        concrete += "?" + "&".join(query)
    return Target(method, concrete, body), ""


# -- the load -------------------------------------------------------------


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


async def run_step(
    clients: Sequence[Any], targets: Sequence[Target], duration: float
) -> StepResult:
    """One simulated user per client, each sending back to back for ``duration`` seconds.

    One ``httpx.AsyncClient`` per user, each holding one connection, rather
    than one client shared by all: httpx's pool is contended by every
    request, and shared by 32 users it drove fewer requests (430/s) than one
    user alone (1,900/s) against the same server. Separate clients held
    ~3,600/s from 8 users up, which is this process's CPU.
    """
    import httpx

    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors = 0
    non_2xx = 0

    concurrency = len(clients)

    async def user(offset: int) -> None:
        client = clients[offset]
        nonlocal errors, non_2xx
        n = offset
        while loop.time() < deadline:
            target = targets[n % len(targets)]
            n += 1
            started = time.perf_counter()
            try:
                response = await client.request(target.method, target.path, json=target.body)
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
                errors += 1
            else:
                statuses[str(response.status_code)] += 1
                if response.status_code >= 300:
                    non_2xx += 1
                # 5xx is the service failing; 429 is it refusing load. Both
                # are where it breaks. A 404 or 401 is the scenario's fault,
                # counted in non_2xx and reported, not blamed on the load.
                if response.status_code >= 500 or response.status_code == 429:
                    errors += 1
            latencies.append((time.perf_counter() - started) * 1000)

    cpu_started, wall_started = time.process_time(), time.perf_counter()
    await asyncio.gather(*(user(i) for i in range(concurrency)))
    wall = time.perf_counter() - wall_started
    cpu = time.process_time() - cpu_started

    ordered = sorted(latencies)
    return StepResult(
        concurrency=concurrency,
        seconds=round(wall, 3),
        requests=len(latencies),
        errors=errors,
        non_2xx=non_2xx,
        rps=round(len(latencies) / wall, 1) if wall else 0.0,
        p50_ms=round(statistics.median(ordered), 2) if ordered else 0.0,
        p95_ms=round(_percentile(ordered, 0.95), 2),
        p99_ms=round(_percentile(ordered, 0.99), 2),
        max_ms=round(ordered[-1], 2) if ordered else 0.0,
        statuses=dict(statuses),
        generator_cpu=round(cpu / wall, 2) if wall else 0.0,
    )


async def _degraded(client: Any) -> dict[str, str]:
    """Which ``/ready`` checks are not ok. Empty when all are, or no /ready."""
    import httpx

    try:
        response = await client.get("/ready", timeout=10)
        checks = response.json().get("checks", {})
    except (httpx.HTTPError, ValueError, AttributeError):
        return {}
    return {
        name: str(check.get("status"))
        for name, check in checks.items()
        if isinstance(check, dict) and check.get("status") != "ok"
    }


async def bench(
    base_url: str,
    targets: Sequence[Target],
    *,
    steps: Sequence[int],
    duration: float,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_p99_ms: float = 500.0,
    max_error_rate: float = 0.01,
    transport: Any = None,
) -> BenchReport:
    """Run every step in order and find where the service breaks."""
    import httpx

    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    report = BenchReport(
        base_url=base_url,
        targets=[t.label for t in targets],
        thresholds={"p99_ms": max_p99_ms, "error_rate": max_error_rate},
        steps=[],
    )

    def client(connections: int = 1) -> Any:
        return httpx.AsyncClient(
            base_url=base_url,
            headers=request_headers,
            timeout=timeout,
            limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
            transport=transport,
        )

    async with client() as probe:
        for concurrency in steps:
            async with contextlib.AsyncExitStack() as stack:
                users = [await stack.enter_async_context(client()) for _ in range(concurrency)]
                step = await run_step(users, targets, duration)
            step.degraded = await _degraded(probe)
            report.steps.append(step)
            if report.breaks_at is None:
                reason = _broken(step, max_p99_ms, max_error_rate)
                if reason:
                    report.breaks_at, report.break_reason = concurrency, reason
    report.saturates_at = _saturation(report.steps)
    return report


def _broken(step: StepResult, max_p99_ms: float, max_error_rate: float) -> str | None:
    if step.requests == 0:
        return "no request completed"
    if step.error_rate > max_error_rate:
        return f"error rate {step.error_rate:.1%} > {max_error_rate:.1%}"
    if step.p99_ms > max_p99_ms:
        return f"p99 {step.p99_ms:.0f} ms > {max_p99_ms:.0f} ms"
    return None


def _saturation(steps: Sequence[StepResult]) -> int | None:
    """The first step whose extra concurrency bought under 10 % more req/s."""
    for previous, current in itertools.pairwise(steps):
        more_clients = current.concurrency > previous.concurrency
        if more_clients and current.rps < previous.rps * 1.10:
            return previous.concurrency
    return None


# -- output ---------------------------------------------------------------


def render(report: BenchReport) -> str:
    lines = [
        f"{report.base_url}  --  {len(report.targets)} route(s): {', '.join(report.targets)}",
        "",
        f"{'clients':>7} {'req/s':>9} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} "
        f"{'errors':>7} {'non-2xx':>8}  ready",
    ]
    for step in report.steps:
        ready = ", ".join(f"{k}={v}" for k, v in step.degraded.items()) or "ok"
        lines.append(
            f"{step.concurrency:>7} {step.rps:>9,.0f} {step.p50_ms:>8.1f} {step.p95_ms:>8.1f} "
            f"{step.p99_ms:>8.1f} {step.error_rate:>7.1%} {step.non_2xx:>8}  {ready}"
        )
    lines.append("")
    if report.breaks_at is not None:
        lines.append(f"Breaks at {report.breaks_at} clients: {report.break_reason}.")
    else:
        widest = report.steps[-1].concurrency if report.steps else 0
        lines.append(
            f"Did not break up to {widest} clients "
            f"(p99 <= {report.thresholds['p99_ms']:.0f} ms, "
            f"errors <= {report.thresholds['error_rate']:.1%})."
        )
    if report.saturates_at is not None:
        lines.append(
            f"Throughput stops growing past {report.saturates_at} clients: "
            f"size replicas from there."
        )
    if any(step.generator_cpu >= 0.8 for step in report.steps):
        lines.append(
            "The load generator used a whole core in some steps: those numbers measure it, "
            "not the service. Export with --k6 for more load."
        )
    return "\n".join(lines)


def k6_script(
    report_targets: Sequence[Target],
    *,
    base_url: str,
    steps: Sequence[int],
    duration: float,
    max_p99_ms: float,
    max_error_rate: float,
) -> str:
    """The same scenario for k6: one ``ramping-vus`` stage per step.

    The token is read from ``TOKEN`` in k6's environment, never written into
    the script: scripts end up in repositories.
    """
    stages = [{"duration": f"{duration:g}s", "target": vus} for vus in steps]
    targets = [{"method": t.method, "path": t.path, "body": t.body} for t in report_targets]
    options = {
        "scenarios": {"steps": {"executor": "ramping-vus", "startVUs": steps[0], "stages": stages}},
        "thresholds": {
            "http_req_failed": [f"rate<{max_error_rate}"],
            "http_req_duration": [f"p(99)<{max_p99_ms:g}"],
        },
    }
    return f"""// Generated by `jfast bench --k6`. Run: k6 run -e BASE_URL=... -e TOKEN=... this.js
import http from 'k6/http';
import {{ check }} from 'k6';

export const options = {json.dumps(options, indent=2)};

const BASE_URL = __ENV.BASE_URL || {json.dumps(base_url)};
const TOKEN = __ENV.TOKEN || '';
const targets = {json.dumps(targets, indent=2)};

export default function () {{
  const target = targets[(__ITER + __VU) % targets.length];
  const headers = {{ 'Content-Type': 'application/json' }};
  if (TOKEN) headers['Authorization'] = `Bearer ${{TOKEN}}`;
  const body = target.body === null ? null : JSON.stringify(target.body);
  const res = http.request(target.method, BASE_URL + target.path, body, {{ headers }});
  check(res, {{ 'not a server error': (r) => r.status < 500 && r.status !== 429 }});
}}
"""


# -- the command ----------------------------------------------------------


def _steps(raw: str) -> list[int]:
    try:
        steps = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        steps = []
    if not steps or any(step < 1 for step in steps):
        typer.echo(f"--concurrency must be positive integers, e.g. 1,8,32: {raw!r}", err=True)
        raise typer.Exit(Code.USAGE)
    return steps


def bench_command(
    url: str = typer.Argument(..., help="Base URL of the running service."),
    route: list[str] = typer.Option(
        [], "--route", "-r", help='"GET /path" or "/path"; repeat. Default: from /openapi.json.'
    ),
    method: list[str] = typer.Option(
        ["GET"], "--method", "-m", help="Methods taken from the schema (writes need examples)."
    ),
    token: str | None = typer.Option(
        None, "--token", envvar="JFAST_BENCH_TOKEN", help="Bearer token (or JFAST_BENCH_TOKEN)."
    ),
    header: list[str] = typer.Option([], "--header", "-H", help='"Name: value"; repeat.'),
    concurrency: str = typer.Option("1,4,16,64", "--concurrency", "-c", help="Steps, e.g. 1,8,32."),
    duration: float = typer.Option(10.0, "--duration", "-d", help="Seconds per step."),
    timeout: float = typer.Option(10.0, "--timeout", help="Per-request timeout, seconds."),
    max_p99_ms: float = typer.Option(500.0, "--max-p99-ms", help="p99 above this breaks."),
    max_error_rate: float = typer.Option(
        0.01, "--max-error-rate", help="5xx/429/transport errors above this fraction break."
    ),
    include_framework: bool = typer.Option(
        False, "--include-framework", help="Also load /health, /ready, /metrics..."
    ),
    k6: Path | None = typer.Option(None, "--k6", help="Also write the scenario as a k6 script."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    fail_on_break: bool = typer.Option(
        False, "--fail-on-break", help="Exit 1 when a step breaks (for CI)."
    ),
) -> None:
    """Load a running service in steps of concurrency; report where it breaks."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with most extras
        typer.echo('jfast bench needs httpx: pip install "jfastframework[http]"', err=True)
        raise typer.Exit(Code.ENVIRONMENT) from None

    steps = _steps(concurrency)
    headers: dict[str, str] = {}
    for raw in header:
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    spec: dict[str, Any] = {}
    try:
        response = httpx.get(url.rstrip("/") + "/openapi.json", headers=headers, timeout=timeout)
        if response.status_code == 200:
            spec = response.json()
    except httpx.HTTPError as exc:
        typer.echo(f"{url} is not answering: {exc}", err=True)
        raise typer.Exit(Code.ENVIRONMENT) from None
    except ValueError:
        spec = {}
    if not spec and not route:
        typer.echo(
            f"{url}/openapi.json is not available (closed in production); name the routes "
            f'with --route "GET /path".',
            err=True,
        )
        raise typer.Exit(Code.USAGE)

    targets, skipped = targets_from_openapi(
        spec, routes=route, methods=method, include_framework=include_framework
    )
    if not targets:
        typer.echo("no route to load. " + "; ".join(skipped), err=True)
        raise typer.Exit(Code.USAGE)

    if k6 is not None:
        k6.write_text(
            k6_script(
                targets,
                base_url=url.rstrip("/"),
                steps=steps,
                duration=duration,
                max_p99_ms=max_p99_ms,
                max_error_rate=max_error_rate,
            ),
            encoding="utf-8",
        )

    report = asyncio.run(
        bench(
            url.rstrip("/"),
            targets,
            steps=steps,
            duration=duration,
            headers=headers,
            timeout=timeout,
            max_p99_ms=max_p99_ms,
            max_error_rate=max_error_rate,
        )
    )
    if json_out:
        typer.echo(json.dumps({**report.to_dict(), "skipped": skipped}, indent=2))
    else:
        typer.echo(render(report))
        for line in skipped:
            typer.echo(f"  skipped {line}")
        if k6 is not None:
            typer.echo(f"k6 script written to {k6}")
    if fail_on_break and report.breaks_at is not None:
        raise typer.Exit(Code.VALIDATION)


def register(app: typer.Typer) -> None:
    """Attach `bench` to *app*."""
    app.command("bench")(bench_command)
