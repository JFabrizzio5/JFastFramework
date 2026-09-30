"""`jfast bench` against a real server: uvicorn on a free port, in a thread.

The service under load has one route whose capacity is known exactly -- two
slots of 20 ms, so 100 req/s and a queue that grows with every client past
two -- which is what lets the tests say where it must break and where it
must saturate instead of hoping.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from jfastframework.cli.commands.bench import (
    StepResult,
    Target,
    _broken,
    _saturation,
    bench,
    k6_script,
    targets_from_openapi,
)
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta
from jfastframework.testing import build_test_app

uvicorn = pytest.importorskip("uvicorn")


class Item(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"name": "widget", "price": 3}})

    name: str
    price: int


class StrainedDependency(Plugin):
    """A dependency that reports itself degraded, as a pool under load would."""

    meta = PluginMeta(name="strained", description="test", health_critical=False)

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.fail("pool exhausted", critical=False)


def _service() -> Any:
    router = APIRouter()
    slots = asyncio.Semaphore(2)

    @router.get("/items")
    async def items() -> list[int]:
        return [1, 2, 3]

    @router.get("/items/{item_id}")
    async def item(item_id: int = Path(examples=[7])) -> dict[str, int]:
        return {"id": item_id}

    @router.get("/needs/{thing}")
    async def needs(thing: str) -> dict[str, str]:
        return {"thing": thing}

    @router.post("/items", status_code=201)
    async def create(body: Item) -> Item:
        return body

    @router.get("/slow")
    async def slow() -> dict[str, bool]:
        async with slots:
            await asyncio.sleep(0.02)
        return {"ok": True}

    @router.get("/broken")
    async def broken() -> dict[str, bool]:
        raise RuntimeError("boom")

    return build_test_app(
        routers=[router],
        plugins=["strained"],
        extra_plugins=[StrainedDependency],
        raw={"plugin": {"observability": {"level": "WARNING", "access_log": False}}},
    )


@pytest.fixture(scope="module")
def service() -> Iterator[str]:
    """The app behind uvicorn in a thread of its own, so the CLI's
    ``asyncio.run`` and the tests' own loop are both free to run."""
    config = uvicorn.Config(_service(), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


# -- the scenario ---------------------------------------------------------


def test_the_scenario_comes_from_the_schema() -> None:
    spec = _service().openapi()
    targets, skipped = targets_from_openapi(spec)
    labels = {t.label for t in targets}

    assert {"GET /items", "GET /items/7", "GET /slow"} <= labels
    # Probes and docs are not the service's work.
    assert not any(t.path in ("/health", "/ready", "/metrics", "/info") for t in targets)
    # A path parameter with no example is skipped, and says how to fix it.
    assert any(s.startswith("GET /needs/{thing}") and "--route" in s for s in skipped)
    # Writes only when asked for.
    assert "POST /items" not in labels


def test_a_write_is_included_with_its_example_body() -> None:
    spec = _service().openapi()
    targets, _ = targets_from_openapi(spec, methods=["POST"])
    assert targets == [Target("POST", "/items", {"name": "widget", "price": 3})]


def test_picked_routes_replace_the_schema_and_accept_literal_paths() -> None:
    spec = _service().openapi()
    targets, _ = targets_from_openapi(spec, routes=["GET /items/{item_id}", "/needs/lamp"])
    assert [t.label for t in targets] == ["GET /items/7", "GET /needs/lamp"]


def test_the_k6_export_carries_the_steps_and_never_the_token() -> None:
    script = k6_script(
        [Target("GET", "/items"), Target("POST", "/items", {"name": "w"})],
        base_url="http://svc:8000",
        steps=[1, 8],
        duration=5,
        max_p99_ms=250,
        max_error_rate=0.02,
    )
    assert '"executor": "ramping-vus"' in script
    assert '"target": 8' in script and '"duration": "5s"' in script
    assert "p(99)<250" in script and "rate<0.02" in script
    assert '"path": "/items"' in script
    assert "__ENV.TOKEN" in script and "Bearer ey" not in script


# -- the load -------------------------------------------------------------


async def test_it_finds_where_the_service_breaks(service: str) -> None:
    report = await bench(
        service,
        [Target("GET", "/slow")],
        steps=[1, 4, 32],
        duration=1.5,
        max_p99_ms=300,
    )
    one, four, many = report.steps
    assert one.errors == four.errors == many.errors == 0
    # Two slots of 20 ms: ~45 req/s with one client (each also waits for its
    # own round trip), ~95 with four, when both slots are always busy, and
    # never more than the slots allow with thirty-two -- they queue sixteen
    # deep, ~320 ms, and the queue is the p99. The bounds only go the way a
    # stall on a busy CI machine cannot push them past.
    assert one.p50_ms < 40, one
    assert many.rps < 115, many
    assert many.p50_ms > 250 and many.p99_ms > 300, many
    assert report.breaks_at == 32 and "p99" in (report.break_reason or "")
    # /ready was read after each step, and the strained dependency named.
    assert many.degraded == {"strained": "fail"}


def _step(concurrency: int, rps: float, *, p99: float = 10.0, errors: int = 0) -> StepResult:
    return StepResult(
        concurrency=concurrency,
        seconds=1.0,
        requests=1000,
        errors=errors,
        non_2xx=errors,
        rps=rps,
        p50_ms=p99 / 2,
        p95_ms=p99,
        p99_ms=p99,
        max_ms=p99,
    )


def test_saturation_is_where_more_clients_stop_buying_throughput() -> None:
    steps = [_step(1, 45), _step(4, 95), _step(16, 99), _step(64, 100)]
    assert _saturation(steps) == 4
    assert _saturation([_step(1, 45), _step(4, 170), _step(16, 600)]) is None


def test_breaking_is_the_first_threshold_crossed() -> None:
    assert _broken(_step(8, 90, p99=40), 500, 0.01) is None
    assert "p99" in (_broken(_step(8, 90, p99=600), 500, 0.01) or "")
    assert "error rate" in (_broken(_step(8, 90, errors=20), 500, 0.01) or "")


async def test_server_errors_break_the_first_step(service: str) -> None:
    report = await bench(service, [Target("GET", "/broken")], steps=[1, 2], duration=0.5)
    assert report.breaks_at == 1
    assert "error rate" in (report.break_reason or "")
    assert report.steps[0].statuses.get("500", 0) > 0


def test_the_command_reports_json_and_can_fail_a_build(service: str, tmp_path: Any) -> None:
    from jfastframework.cli.main import app

    k6 = tmp_path / "load.js"
    result = CliRunner().invoke(
        app,
        [
            "bench",
            service,
            "--concurrency",
            "1,2",
            "--duration",
            "0.5",
            "--json",
            "--k6",
            str(k6),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert "GET /items/7" in report["targets"]
    assert any(s.startswith("GET /needs/{thing}") for s in report["skipped"])
    assert [s["concurrency"] for s in report["steps"]] == [1, 2]
    assert k6.read_text().startswith("// Generated by `jfast bench --k6`")

    failing = CliRunner().invoke(
        app,
        ["bench", service, "-r", "GET /broken", "-c", "1", "-d", "0.3", "--fail-on-break"],
    )
    assert failing.exit_code == 1, failing.output
    assert "Breaks at 1 clients" in failing.output


def test_an_unreachable_service_is_an_environment_error() -> None:
    from jfastframework.cli.main import app

    result = CliRunner().invoke(app, ["bench", "http://127.0.0.1:9", "-d", "0.1"])
    assert result.exit_code == 3, result.output
