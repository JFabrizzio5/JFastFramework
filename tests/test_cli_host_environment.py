"""The compose-oriented `.env`, translated for whatever runs on the host.

A workspace's service `.env` is written for compose: the database is
`<workspace>-database:5432`, a name only the compose network resolves, and the
password is `${<WORKSPACE>_DATABASE_PASSWORD}`, which only compose fills in.
Before 0.1.0a12 only `jfast dev` translated it, so `alembic revision
--autogenerate` -- the step `jfast new module` prints -- failed on the host with
a DNS error, and so did `jfast serve`, `jfast worker` and `pytest`.

Here: the translation itself, `jfast exec` running real commands with it,
`jfast serve` and `jfast dev` handing it on, and the two places it must not
apply -- inside a container, and wherever there is no compose file (which is
the production image). The real `jfast worker` path is in test_worker_cli.py.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - this interpreter, fixed arguments
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jfastframework.cli import dev as devtools
from jfastframework.cli.main import app

runner = CliRunner()

COMPOSE = textwrap.dedent(
    """\
    services:
      shop-database:
        image: postgres:16
        ports:
          - "15432:5432"
      shop-cache:
        image: redis:7
        ports:
          - "16379:6379"
      shop:
        build: ./shop
        env_file: ./shop/.env
    """
)

SERVICE_ENV = textwrap.dedent(
    """\
    # Written by jfast from jfast.workspace.toml.
    JFAST_DB_DSN=postgresql+asyncpg://app:${SHOP_DATABASE_PASSWORD}@shop-database:5432/app
    JFAST_CACHE_URL=redis://shop-cache:6379/0
    JFAST_AUTH_SECRET=not-a-real-secret
    """
)

HOST_DSN = "postgresql+asyncpg://app:s3cret@localhost:15432/app"
HOST_CACHE = "redis://localhost:16379/0"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace as `jfast start` lays it out; returns the service directory."""
    root = tmp_path / "ws"
    service = root / "shop"
    service.mkdir(parents=True)
    (root / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (root / ".env").write_text("SHOP_DATABASE_PASSWORD=s3cret\n", encoding="utf-8")
    (service / ".env").write_text(SERVICE_ENV, encoding="utf-8")
    (service / "jfast.toml").write_text('[app]\nname = "shop"\n', encoding="utf-8")
    for key in ("JFAST_DB_DSN", "JFAST_CACHE_URL", "JFAST_AUTH_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(devtools, "in_container", lambda: False)
    return service


# -- the translation ---------------------------------------------------------


def test_names_and_passwords_are_translated_for_the_host(workspace: Path) -> None:
    found = devtools.service_host_environment(workspace)
    assert found.compose_file == workspace.parent / "docker-compose.yml"
    assert found.skipped is None
    assert found.values["JFAST_DB_DSN"] == HOST_DSN
    assert found.values["JFAST_CACHE_URL"] == HOST_CACHE
    assert found.values["JFAST_AUTH_SECRET"] == "not-a-real-secret"


def test_a_variable_set_in_the_shell_wins(workspace: Path) -> None:
    """As pydantic-settings reads the shell over .env: the translation takes
    the file's place, not the shell's."""
    found = devtools.service_host_environment(
        workspace, environ={"JFAST_DB_DSN": "postgresql+asyncpg://mine@db:1/x"}
    )
    assert "JFAST_DB_DSN" not in found.values
    assert found.values["JFAST_CACHE_URL"] == HOST_CACHE


def test_nothing_is_translated_inside_a_container(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There the compose names resolve and localhost is the container itself."""
    monkeypatch.setattr(devtools, "in_container", lambda: True)
    found = devtools.service_host_environment(workspace)
    assert found.values == {}
    assert found.skipped == "inside a container"


def test_nothing_is_translated_without_a_compose_file(tmp_path: Path) -> None:
    """The production image: WORKDIR /app, the workspace's compose file is
    outside the build context, and .dockerignore drops a service-level one."""
    from jfastframework.deploy import render_dockerignore
    from jfastframework.deploy.compose import IMAGE_WORKDIR

    service = tmp_path / "app"
    service.mkdir()
    (service / ".env").write_text(SERVICE_ENV, encoding="utf-8")
    found = devtools.service_host_environment(service)
    assert found.values == {}
    assert found.compose_file is None
    assert found.skipped is not None
    assert "docker-compose*.yml" in render_dockerignore().splitlines()
    assert IMAGE_WORKDIR == "/app"  # whose parent, "/", holds no compose file


# -- jfast exec --------------------------------------------------------------


def _exec(service: Path, *command: str, env: dict[str, str] | None = None) -> Any:
    base = {k: v for k, v in os.environ.items() if not k.startswith("JFAST_")}
    return subprocess.run(  # nosec B603
        [sys.executable, "-m", "jfastframework", "exec", "--", *command],
        cwd=service,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


PRINT_ENV = "import os; print(os.environ['JFAST_DB_DSN']); print(os.environ['JFAST_CACHE_URL'])"


@pytest.mark.skipif(sys.platform == "win32", reason="exec replaces the process on POSIX")
def test_exec_runs_a_command_with_the_host_environment(workspace: Path) -> None:
    if Path("/.dockerenv").exists():  # pragma: no cover - the suite inside a container
        pytest.skip("a subprocess cannot be told it is on the host")
    result = _exec(workspace, sys.executable, "-c", PRINT_ENV)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [HOST_DSN, HOST_CACHE]


@pytest.mark.skipif(sys.platform == "win32", reason="exec replaces the process on POSIX")
def test_exec_keeps_the_shell_passes_flags_through_and_returns_the_status(
    workspace: Path,
) -> None:
    if Path("/.dockerenv").exists():  # pragma: no cover
        pytest.skip("a subprocess cannot be told it is on the host")
    mine = "postgresql+asyncpg://mine@elsewhere:1/x"
    code = f"{PRINT_ENV}; import sys; sys.exit(int(sys.argv[1]))"
    result = _exec(workspace, sys.executable, "-c", code, "3", env={"JFAST_DB_DSN": mine})
    assert result.returncode == 3
    assert result.stdout.split() == [mine, HOST_CACHE]


def test_exec_without_a_compose_file_runs_the_command_unchanged(tmp_path: Path) -> None:
    (tmp_path / "jfast.toml").write_text('[app]\nname = "x"\n', encoding="utf-8")
    result = _exec(tmp_path, sys.executable, "-c", "print('ran')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran"
    assert ".env used as is" in result.stderr


def test_exec_needs_a_service_directory_and_names_a_missing_command(
    tmp_path: Path, workspace: Path
) -> None:
    result = _exec(tmp_path, sys.executable, "-c", "print('ran')")
    assert result.returncode != 0
    assert "jfast.toml" in result.stderr
    missing = _exec(workspace, "no-such-command-jfast")
    assert missing.returncode == 127
    assert "command not found" in missing.stderr


# -- serve and dev -----------------------------------------------------------


def test_serve_translates_before_the_settings_load(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, str | None] = {}

    def fake_run(app_path: str, **kwargs: Any) -> None:
        # What uvicorn -- and the child it starts for --reload -- inherits.
        seen["dsn"] = os.environ.get("JFAST_DB_DSN")

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.chdir(workspace)
    # serve writes into os.environ; monkeypatch puts every key back.
    for key in ("JFAST_DB_DSN", "JFAST_CACHE_URL", "JFAST_AUTH_SECRET", "PYTHONPATH"):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)

    result = runner.invoke(app, ["serve", "--path", str(workspace), "--no-reload"])
    assert result.exit_code == 0, result.output
    assert seen["dsn"] == HOST_DSN
    assert ".env translated for the host" in result.output


def test_dev_hands_the_same_translation_to_every_child(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: dict[str, dict[str, str]] = {}

    def fake_spawn(command: list[str], *, name: str, env: Any = None, **kwargs: Any) -> object:
        spawned[name] = dict(env or {})
        return object()

    monkeypatch.setattr(devtools, "spawn", fake_spawn)
    monkeypatch.setattr(devtools, "supervise", lambda processes: 0)
    monkeypatch.setenv("JFAST_CACHE_URL", "redis://mine:1/0")
    result = runner.invoke(
        app, ["dev", "--path", str(workspace), "--no-infra", "--no-migrate", "--no-web"]
    )
    assert result.exit_code == 0, result.output
    assert spawned["api"]["JFAST_DB_DSN"] == HOST_DSN
    # Set in the shell: left for the shell's value to reach the child.
    assert "JFAST_CACHE_URL" not in spawned["api"]


# -- the printed next steps --------------------------------------------------


def test_new_module_prints_alembic_through_jfast_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / "shop"
    created = runner.invoke(
        app, ["new", "service", "shop", "--target", str(service), "--with", "database"]
    )
    assert created.exit_code == 0, created.output
    assert "jfast exec -- alembic upgrade head" in created.output
    monkeypatch.chdir(service)
    result = runner.invoke(app, ["new", "module", "invoice", "--fields", "number:str"])
    assert result.exit_code == 0, result.output
    assert "jfast exec -- alembic revision --autogenerate" in result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert not any(line.startswith("alembic ") for line in lines), result.output
