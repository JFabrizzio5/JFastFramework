"""`jfast worker` and `jfast jobs`, run the way an operator runs them.

The graceful-shutdown test is a real process and a real signal: a worker is
started on a generated project against PostgreSQL, given one short and one
long job, and sent SIGTERM while both run. The short one must finish, the long
one must come back to the queue without spending an attempt, and the process
must exit inside the grace period -- the three things an orchestrator's
rolling deploy depends on.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.queues.base import Job
from jfastframework.queues.postgres import PostgresQueue

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
TABLE = "jfast_jobs_cli"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
async def dsn() -> str:
    dsn = f"{PG_BASE}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await engine.dispose()
    return dsn


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    (root / "modules" / "demo").mkdir(parents=True)
    (root / "jfast.toml").write_text(
        textwrap.dedent(
            f"""
            [app]
            name = "svc"

            [plugins]
            enabled = ["observability", "database", "queue"]

            [plugin.queue]
            name = "{TABLE}"
            """
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from jfastframework import create_app\n\napp = create_app()\n", encoding="utf-8"
    )
    (root / "modules" / "__init__.py").write_text("", encoding="utf-8")
    (root / "modules" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (root / "modules" / "demo" / "tasks.py").write_text(
        textwrap.dedent(
            """
            import asyncio
            from pathlib import Path

            from jfastframework.tasks import task


            @task("demo.sleep")
            async def sleep(payload: dict) -> None:
                Path(payload["started"]).write_text("started")
                await asyncio.sleep(payload["seconds"])
                Path(payload["finished"]).write_text("finished")
            """
        ),
        encoding="utf-8",
    )
    return root


def _env(dsn: str) -> dict[str, str]:
    return {**os.environ, "JFAST_DB_DSN": dsn, "PYTHONUNBUFFERED": "1"}


def _jfast(project: Path, dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jfastframework", *args],
        cwd=project,
        env=_env(dsn),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def _queue(dsn: str) -> tuple[Any, PostgresQueue]:
    engine = create_async_engine(dsn)
    queue = PostgresQueue(engine, table=TABLE)
    await queue.setup()
    return engine, queue


async def _rows(engine: Any) -> dict[str, dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(text(f"SELECT id, status, attempts FROM {TABLE}"))
        return {row["id"]: dict(row) for row in result.mappings()}


async def _wait_for(paths: list[Path], process: subprocess.Popen[str], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not all(p.exists() for p in paths):
        if process.poll() is not None:
            raise AssertionError(f"worker exited early:\n{process.stdout.read()}")  # type: ignore[union-attr]
        if time.monotonic() > deadline:
            raise AssertionError("the jobs never started")
        await asyncio.sleep(0.1)


async def test_sigterm_finishes_short_jobs_and_releases_long_ones(
    dsn: str, project: Path, tmp_path: Path
) -> None:
    engine, queue = await _queue(dsn)
    marks = {name: tmp_path / name for name in ("s1", "s2", "l1", "l2")}
    short = Job(
        task="demo.sleep",
        payload={"seconds": 1.0, "started": str(marks["s1"]), "finished": str(marks["s2"])},
    )
    long = Job(
        task="demo.sleep",
        payload={"seconds": 120, "started": str(marks["l1"]), "finished": str(marks["l2"])},
    )
    await queue.enqueue(short)
    await queue.enqueue(long)

    # Blocking on purpose: the process is the thing under test, not this loop.
    process = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "jfastframework", "worker", "--concurrency", "2", "--grace", "3"],
        cwd=project,
        env=_env(dsn),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await _wait_for([marks["s1"], marks["l1"]], process, 30)
        signalled = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=20)
        took = time.monotonic() - signalled
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, output
    # Inside the grace period, with room to spare -- not the 120 s job.
    assert took < 10, output
    assert marks["s2"].exists(), output
    assert not marks["l2"].exists()
    rows = await _rows(engine)
    assert short.id not in rows  # acknowledged
    # Handed back at once, and the claim's attempt refunded: a deploy is not
    # a failure and must not walk the job towards the dead-letter queue.
    assert rows[long.id]["status"] == "pending"
    assert rows[long.id]["attempts"] == 0
    await engine.dispose()


async def test_jobs_dead_lists_the_reason_and_retry_puts_them_back(dsn: str, project: Path) -> None:
    engine, queue = await _queue(dsn)
    first = Job(task="demo.sleep", max_attempts=1)
    second = Job(task="demo.sleep", max_attempts=1)
    for job in (first, second):
        await queue.enqueue(job)
        claimed = await queue.dequeue()
        assert claimed is not None
        claimed.error = f"ValueError: broken {claimed.id}"
        await queue.nack(claimed)

    listed = _jfast(project, dsn, "jobs", "dead", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    dead = {entry["id"]: entry for entry in json.loads(listed.stdout)}
    assert set(dead) == {first.id, second.id}
    assert dead[first.id]["error"] == f"ValueError: broken {first.id}"

    retried = _jfast(project, dsn, "jobs", "retry", first.id)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    rows = await _rows(engine)
    assert (rows[first.id]["status"], rows[first.id]["attempts"]) == ("pending", 0)
    assert rows[second.id]["status"] == "dead"

    everything = _jfast(project, dsn, "jobs", "retry", "--all")
    assert "returned 1 job(s)" in everything.stdout
    assert {row["status"] for row in (await _rows(engine)).values()} == {"pending"}

    refused = _jfast(project, dsn, "jobs", "retry")
    assert refused.returncode != 0
    await engine.dispose()


def test_the_worker_needs_a_service_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jfastframework", "worker", "--path", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "jfast.toml" in result.stderr


def _dev_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugins: str, *extra: str):  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from jfastframework.cli import main as cli_main

    (tmp_path / "jfast.toml").write_text(
        f'[app]\nname = "svc"\n\n[plugins]\nenabled = [{plugins}]\n', encoding="utf-8"
    )
    spawned: list[tuple[str, list[str]]] = []

    def fake_spawn(command: list[str], *, name: str, **kwargs: Any) -> object:
        spawned.append((name, command))
        return object()

    monkeypatch.setattr(cli_main.devtools, "spawn", fake_spawn)
    monkeypatch.setattr(cli_main.devtools, "supervise", lambda processes: 0)
    result = CliRunner().invoke(
        cli_main.app,
        ["dev", "--path", str(tmp_path), "--no-infra", "--no-migrate", "--no-web", *extra],
    )
    assert result.exit_code == 0, result.output
    return dict(spawned)


def test_dev_starts_the_worker_when_the_queue_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _dev_commands(tmp_path, monkeypatch, '"observability", "database", "queue"')
    assert spawned["worker"][-2:] == ["jfastframework", "worker"]
    assert "api" in spawned


def test_dev_leaves_the_worker_out_without_a_queue_or_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "worker" not in _dev_commands(tmp_path, monkeypatch, '"observability"')
    assert "worker" not in _dev_commands(
        tmp_path, monkeypatch, '"observability", "queue"', "--no-worker"
    )
