# Contributing

## Setting up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

`[dev]` pulls every plugin extra. The suite tests the plugins, so it imports
what the plugins import; a narrower install collects ImportErrors rather than
running.

## What CI checks

Four gates, all of them runnable locally, and none of them advisory:

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

Plus `mypy --platform win32 src` if you touched code that branches on the
operating system — `sys.platform` is what mypy narrows on, `os.name` is not.

Beyond that, CI builds the generated image and runs it against a real
PostgreSQL (`scripts/smoke_docker.sh`), runs the scaffolder end to end
(`scripts/smoke*.sh`), and runs the suite on Windows. If you are changing the
generator or the Dockerfile, run the relevant smoke script before pushing:
they catch the class of defect the unit tests structurally cannot, which is
"the thing we generate does not work".

## Tests

A test that skips is a test nobody is running. The suites needing a real
server skip without one, and CI fails if they skip there — so if you add one,
add it to the "Prove the tests needing a real server actually ran" step in
`.github/workflows/ci.yml` too.

Locally:

```bash
JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5432 \
JFAST_TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
```

Two habits worth keeping, because both have already cost this project a
shipped defect:

- **Write the assertion against the real shape.** A test upstream that reads
  `dict(request.query_params)` agrees with a proxy that drops repeated
  parameters, and neither notices.
- **Prove the fix fails without the fix.** Break it back, watch the test go
  red, restore it. A test that passes either way is documentation.

### Running the suite with a Windows clock

On Windows (before Python 3.13) `time.monotonic()` moves in 15.625 ms steps,
and asyncio fires every timer due within that resolution of now -- early, and
in batches. Code that compares two clock readings to decide whether something
happened in between passes on macOS and Linux and fails there: the JWKS
single-flight bug fixed in 0.1.0a10 did exactly that for months.

`tests/windows_clock.py` reproduces that clock on any OS. It floors
`time.monotonic`/`monotonic_ns` to 1/64 s and reports that resolution through
`time.get_clock_info("monotonic")`, so every event loop created afterwards
behaves like one on Windows. Against the pre-fix JWKS client it fails the
concurrency tests in about half the runs; with a normal clock, never.

```bash
JFAST_TEST_WINDOWS_CLOCK=1 python -m pytest -q \
  tests/test_auth.py tests/test_cache.py tests/test_ratelimit.py \
  tests/test_queue.py tests/test_redis_queue.py tests/test_scheduler.py \
  tests/test_http_client.py tests/test_outbox.py tests/test_idempotency.py
```

The report header says `windows clock: ...` when it is on. Unset, the plugin
does nothing. `JFAST_TEST_WINDOWS_CLOCK_STEP=0.05` makes the step coarser, which
makes the same races easier to hit while you chase one. Wall time
(`time.time`, `datetime.now`) and `perf_counter` are left alone; the module
docstring says why. A test that fails only under the plugin is either
clock-fragile (fix the test, and say why in a comment) or a real Windows bug
(fix the code) -- never loosen the assertion to make it pass.

## Security

`pip-audit` and `bandit` are hard gates. When a finding lands, upgrade the
package first; only if it cannot be upgraded, waive it explicitly
(`--ignore-vuln PYSEC-...`, or `# nosec BXXX`) with a comment saying which
code path makes it inapplicable. A check that goes yellow and is ignored has
stopped meaning anything.
