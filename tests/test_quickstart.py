"""The printed next steps have to work on the tree that just got generated.

Every command here ends in a panel telling somebody what to run next. Those
lines are the first contact anyone has with this framework, and three of them
were wrong at the same time on 0.1.0a6: `jfast start` produced a service whose
only module was never mounted, `docker compose up` could not build because no
generator wrote a Dockerfile, and `pytest` was printed by a scaffold that did
not install one.

None of it showed up in a unit test, because each piece worked. What failed was
the sequence, so this file asserts the sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app

runner = CliRunner()


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project from `jfast start`, in a directory of its own."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["start", "demo"])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_the_module_start_generates_is_actually_mounted(started: Path) -> None:
    """`jfast start` rendered the module and stopped, so its routes 404ed.

    Nothing failed on the way: the generated tests passed, the server booted and
    the endpoints simply were not there. `jfast new module` had mounted what it
    generated since it existed; the flagship command did not use that path.
    """
    main = (started / "demo" / "main.py").read_text(encoding="utf-8")

    assert "from modules.item import router as item_router" in main
    assert "item_router," in main


def test_the_generated_project_passes_the_frameworks_own_check(started: Path) -> None:
    """A tree this framework writes must satisfy the rules it enforces.

    `jfast check` reported the unmounted module as HIGH and exited 1 on a
    project nobody had touched yet, which is the framework failing its own
    contract on its own output.
    """
    result = runner.invoke(app, ["check", "--path", "demo"])

    assert result.exit_code == 0, result.output


def test_the_compose_file_start_writes_has_something_to_build(started: Path) -> None:
    """`docker compose up --build` is the first line of the panel.

    The workspace compose file gives the backend `build: ./demo`, and until the
    Dockerfile was written with the service that build stopped at "failed to
    read dockerfile" before any container started.
    """
    compose = (started / "docker-compose.yml").read_text(encoding="utf-8")

    assert "context: ./demo" in compose
    assert (started / "demo" / "Dockerfile").is_file()
    assert (started / "demo" / ".dockerignore").is_file()


def test_caddy_serves_the_directory_the_frontend_builds_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compose file mounted ./dist at the workspace root; `npm run build`
    writes demo-web/dist. Nothing ever reached /srv, and production Caddy
    served an empty site while the panel said "nothing else to install"."""
    import json

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["start", "demo"])
    assert result.exit_code == 0, result.output

    compose = (tmp_path / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"./demo-web/dist:/srv:ro"' in compose
    assert "./dist:/srv" not in compose
    front = tmp_path / "demo-web"
    # Vite's outDir is dist unless a config says otherwise, and none does.
    package = json.loads((front / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["build"] == "vite build"
    for config in front.glob("vite.config.*"):
        assert "outDir" not in config.read_text(encoding="utf-8")
    # The panel builds the SPA before it brings the stack up.
    output = " ".join(result.output.split())
    assert "npm run build" in output
    assert output.index("npm run build") < output.index("docker compose up --build")


def test_the_env_start_wrote_is_not_thrown_away_by_the_next_step(started: Path) -> None:
    """The panel used to say `cp .env.example .env`, "defaults already match".

    Both halves were false. The generated .env is derived from the resource
    graph; the example is a static file pointing at localhost:8001 with a
    password nobody set, so the copy replaced a working file with a broken one.
    """
    env = (started / "demo" / ".env").read_text(encoding="utf-8")

    assert "JFAST_DB_DSN" in env
    assert "demo-database" in env


def test_a_new_service_can_install_what_it_tells_you_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pytest modules/<name>/tests` is the next step after `jfast new module`.

    requirements.txt is the deploy list and has no test runner in it, on
    purpose. Without a second list the printed step answered `No module named
    pytest` on a fresh project.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "service", "billing"])
    assert result.exit_code == 0, result.output

    dev = (tmp_path / "billing" / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "-r requirements.txt" in dev
    assert "jfastframework[dev]" in dev


def test_a_generated_service_ships_the_image_its_compose_file_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-service path has the same `build: .` and had the same gap."""
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["new", "service", "billing"]).exit_code == 0

    dockerfile = (tmp_path / "billing" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:" in dockerfile


def test_a_frontend_project_gets_no_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SPA is static files behind Caddy, so no compose service builds it.

    Writing one anyway would be a file nobody uses and a build nobody runs.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "service", "admin", "--kind", "spa", "--frontend", "vue"])
    assert result.exit_code == 0, result.output

    assert not (tmp_path / "admin" / "Dockerfile").exists()
