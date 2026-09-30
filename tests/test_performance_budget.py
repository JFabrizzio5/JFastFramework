"""The performance budget: framework overhead may not grow more than 20 %.

Off by default -- it takes about a minute and measures, so it has no place
in the quick loop. Switched on with ``JFAST_PERF_BUDGET=1``:

    JFAST_PERF_BUDGET=1 pytest tests/test_performance_budget.py

What it compares is the ratio of each budgeted scenario to bare FastAPI on
the same machine in the same run (see ``scripts/bench_overhead.py``), never
absolute microseconds. The a10 fix took the auth + tenancy stack from 2,411
to 8,581 req/s; this is what keeps that from sliding back one innocent
middleware at a time.

The baseline is ``tests/performance_baseline.json``, one entry per platform
(``darwin-arm64``, ``linux-x86_64``...), because ratios differ between CPU
families. ``JFAST_PERF_BASELINE=<file>`` points at another file -- CI runs
the script on the base branch first and compares against that, on the same
runner. To accept a change that costs more on purpose, re-record and commit
the file with the reason in the commit message:

    python scripts/bench_overhead.py --write-baseline tests/performance_baseline.json
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("JFAST_PERF_BUDGET", "") not in ("1", "true", "yes"),
    reason="performance budget runs only with JFAST_PERF_BUDGET=1",
)


def _bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bench_overhead", ROOT / "scripts" / "bench_overhead.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_framework_overhead_stays_within_budget() -> None:
    bench = _bench()
    path = Path(
        os.environ.get("JFAST_PERF_BASELINE") or ROOT / "tests" / "performance_baseline.json"
    )
    baseline = bench.load_baseline(path)
    if baseline is None:
        pytest.skip(
            f"no baseline for {bench.platform_key()} in {path}; record one with "
            f"`python scripts/bench_overhead.py --write-baseline {path}`"
        )
    tolerance = float(os.environ.get("JFAST_PERF_TOLERANCE", bench.DEFAULT_TOLERANCE))
    result = await bench.measure(requests=3000, rounds=7)
    failures = bench.check(result, baseline, tolerance)
    assert not failures, "\n".join([*failures, "", bench.render(result)])


async def test_the_budget_catches_the_a10_regression() -> None:
    """One ``BaseHTTPMiddleware`` back in the stack must fail the budget.

    That is the regression 0.1.0a10 removed (about 75 us per request). A
    budget that let it through would be decoration.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    bench = _bench()
    baseline = await bench.measure(requests=1000, rounds=3)
    original = bench._jfast

    def regressed(*, full: bool) -> object:
        app = original(full=full)

        async def passthrough(request, call_next):  # type: ignore[no-untyped-def]
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
        return app

    bench._jfast = regressed
    result = await bench.measure(requests=1000, rounds=3)
    assert bench.check(result, baseline, bench.DEFAULT_TOLERANCE), bench.render(result)
