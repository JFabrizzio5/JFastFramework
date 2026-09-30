"""Flags that had to exist for the command to be usable at all.

Most of these were reachable only by hand-editing what the CLI generated, or
by giving up half the command: a logged-out page meant undoing the sidebar
splice, and a taken port 5173 meant `--no-web` and a second terminal.

`new service --layout` is the odd one out: the contract is deliberately
deferred to the first `jfast new module`, which is the first moment anything
knows the layout. The flag exists for the caller who knew before that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jfastframework.cli import dev as devtools
from jfastframework.cli.main import app
from jfastframework.contracts import CONTRACTS_FILE

runner = CliRunner()

MINIMAL_CONFIG = """\
[app]
name = "shop"
version = "0.1.0"
env = "local"
port = 8000

[plugins]
enabled = []
disabled = []
"""


@pytest.fixture
def vue_project(tmp_path: Path) -> Path:
    """The two files `jfast new view` patches, with their marker comments."""
    root = tmp_path / "web"
    (root / "src" / "router").mkdir(parents=True)
    (root / "src" / "router" / "index.js").write_text(
        "const routes = [\n  /*nuevaRuta*/\n]\nexport default routes\n", encoding="utf-8"
    )
    (root / "src" / "menuAside.js").write_text(
        "import { mdiHome } from '@mdi/js'\n\nexport default [\n  /*nuevoModulo*/\n]\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text('{"dependencies": {"vue": "^3"}}', encoding="utf-8")
    return root


def test_a_view_can_be_scaffolded_without_a_sidebar_entry(vue_project: Path) -> None:
    # A login page is routed but never listed: the entry has to be opt-out,
    # because removing it afterwards means editing generated code by hand.
    menu_before = (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["new", "view", "Login", "--frontend", "vue", "--root", str(vue_project), "--no-sidebar"],
    )

    assert result.exit_code == 0, result.output
    assert (vue_project / "src" / "ModuloLogin").is_dir()
    router = (vue_project / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "ModuloLogin" in router, "the route still has to be registered"
    assert (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8") == menu_before


def test_the_sidebar_entry_is_still_the_default(vue_project: Path) -> None:
    result = runner.invoke(
        app, ["new", "view", "Facturas", "--frontend", "vue", "--root", str(vue_project)]
    )

    assert result.exit_code == 0, result.output
    menu = (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8")
    assert "/facturas" in menu


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A service directory `jfast dev` accepts, with every stage but the
    servers disabled, and the servers themselves captured rather than run."""
    root = tmp_path / "shop"
    root.mkdir()
    (root / "jfast.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    front = tmp_path / "shop-web"
    (front / "node_modules").mkdir(parents=True)
    (front / "package.json").write_text('{"dependencies": {"vue": "^3"}}', encoding="utf-8")

    monkeypatch.setattr(devtools, "supervise", lambda processes: 0)
    monkeypatch.setattr(devtools, "terminate", lambda processes, grace=5.0: None)
    yield root


def _spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], *, cwd: Path, name: str, env: dict[str, str] | None = None):
        commands.append(command)
        return devtools.Process(name, None)  # type: ignore[arg-type]

    monkeypatch.setattr(devtools, "spawn", fake_spawn)
    return commands


def test_the_frontend_port_can_be_chosen(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Port 5173 taken is the ordinary case on a machine running two projects.
    # Without this the only answer was --no-web, which is not an answer.
    commands = _spawned(monkeypatch)

    result = runner.invoke(
        app,
        [
            "dev",
            "--path",
            str(service),
            "--frontend",
            str(tmp_path / "shop-web"),
            "--no-infra",
            "--no-migrate",
            "--web-port",
            "5199",
        ],
    )

    assert result.exit_code == 0, result.output
    web = next((c for c in commands if c[:1] == ["npm"]), None)
    assert web is not None, f"the frontend was never started: {commands}"
    assert "--port" in web and "5199" in web, web


def test_the_frontend_port_is_reported(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawned(monkeypatch)

    result = runner.invoke(
        app,
        [
            "dev",
            "--path",
            str(service),
            "--frontend",
            str(tmp_path / "shop-web"),
            "--no-infra",
            "--no-migrate",
            "--web-port",
            "5199",
        ],
    )

    assert "5199" in result.output, result.output


def _dev_web(service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str):  # type: ignore[no-untyped-def]
    commands = _spawned(monkeypatch)
    result = runner.invoke(
        app,
        [
            "dev",
            "--path",
            str(service),
            "--frontend",
            str(tmp_path / "shop-web"),
            "--no-infra",
            "--no-migrate",
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    web = next(c for c in commands if c[:1] == ["npm"])
    return web, result.output


def _pin_the_generated_port(tmp_path: Path, port: int = 8610) -> None:
    """What `jfast start` writes: the workspace port in the dev script and in
    vite.config."""
    front = tmp_path / "shop-web"
    scripts = {"dev": f"vite --port {port}", "preview": f"vite preview --port {port}"}
    (front / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    (front / "vite.config.js").write_text(
        f"export default defineConfig({{\n  server: {{\n    port: {port},\n  }},\n}})\n",
        encoding="utf-8",
    )


def test_dev_announces_the_port_the_generated_frontend_pins(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: Vite ran on the workspace's 8610 and `jfast dev` printed 5173."""
    _pin_the_generated_port(tmp_path)
    web, output = _dev_web(service, tmp_path, monkeypatch)
    assert web == ["npm", "run", "dev"]
    assert "http://localhost:8610" in output
    assert "5173" not in output


def test_dev_does_not_repeat_the_port_the_script_already_pins(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--web-port 8610` on a script with `--port 8610` was `vite --port 8610 --port 8610`."""
    _pin_the_generated_port(tmp_path)
    web, output = _dev_web(service, tmp_path, monkeypatch, "--web-port", "8610")
    assert web == ["npm", "run", "dev"]
    assert "http://localhost:8610" in output


def test_dev_moves_a_pinned_frontend_and_announces_where_it_went(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Vite keeps the last --port it is given (checked on 6.4), so appending one
    # moves a script that pins another.
    _pin_the_generated_port(tmp_path)
    web, output = _dev_web(service, tmp_path, monkeypatch, "--web-port", "8620")
    assert web == ["npm", "run", "dev", "--", "--port", "8620"]
    assert "http://localhost:8620" in output
    assert "8610" not in output


def test_dev_reads_the_port_from_vite_config_or_falls_back_to_vites_default(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front = tmp_path / "shop-web"
    (front / "package.json").write_text('{"scripts": {"dev": "vite"}}', encoding="utf-8")
    assert devtools.frontend_port(front) == (5173, False)
    (front / "vite.config.ts").write_text(
        "export default { server: { host: true, port: 8630 } }", encoding="utf-8"
    )
    assert devtools.frontend_port(front) == (8630, False)
    web, output = _dev_web(service, tmp_path, monkeypatch)
    assert web == ["npm", "run", "dev"]
    assert "http://localhost:8630" in output
    (front / "package.json").write_text(
        '{"scripts": {"dev": "vite --host --port=8640"}}', encoding="utf-8"
    )
    assert devtools.frontend_port(front) == (8640, True)


def _new_service(target: Path, *extra: str) -> Any:
    return runner.invoke(
        app, ["new", "service", "shop", "--target", str(target), "--with", "database", *extra]
    )


def test_a_service_writes_no_contract_by_default(tmp_path: Path) -> None:
    """A service has no module yet, so any layout it wrote would be a guess."""
    result = _new_service(tmp_path / "shop")

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "shop" / CONTRACTS_FILE).exists()


def test_the_layout_flag_writes_the_matching_contract(tmp_path: Path) -> None:
    result = _new_service(tmp_path / "shop", "--layout", "hexagonal")

    assert result.exit_code == 0, result.output
    contract = (tmp_path / "shop" / CONTRACTS_FILE).read_text(encoding="utf-8")
    # The layer globs are the whole point: a contract written for another
    # layout matches no file and every rule in it enforces nothing.
    assert "adapters" in contract, contract


def test_an_unknown_layout_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    result = _new_service(tmp_path / "shop", "--layout", "sideways")

    assert result.exit_code != 0
    assert not (tmp_path / "shop").exists()


def test_a_plugin_needing_no_extra_still_reaches_the_generated_jfast_toml(tmp_path):
    """The menu filter used to end in `and spec.extra`, so a plugin that needs
    no extra was dropped from it -- `tenancy` was in the catalog and invisible
    in every generated jfast.toml anyway. Reading the catalog would not catch
    that: the assertion has to be on the file a user opens.
    """
    import tomllib

    from jfastframework.cli.scaffold import BASE_PLUGINS, PLUGIN_CATALOG

    target = tmp_path / "shop"
    result = runner.invoke(app, ["new", "service", "shop", "--target", str(target)])
    assert result.exit_code == 0, result.output

    rendered = (target / "jfast.toml").read_text(encoding="utf-8")
    menu = rendered.split("# Not enabled.", 1)[1]
    enabled = set(tomllib.loads(rendered)["plugins"]["enabled"])

    no_extra = [n for n, s in PLUGIN_CATALOG.items() if not s.extra]
    assert no_extra, "no plugin needs zero extras any more -- this test guards nothing"

    for name, spec in PLUGIN_CATALOG.items():
        if name in enabled or name in BASE_PLUGINS:
            continue
        row = f"#   {name:<13}"
        assert row in menu, f"{name} is offered nowhere in the generated jfast.toml"
        line = next(ln for ln in menu.splitlines() if ln.startswith(row))
        if spec.extra:
            assert f'pip install "jfastframework[{spec.extra}]"' in line, line
        else:
            # The half that broke: no extra, so no `pip install` line to carry
            # the row, and the old filter dropped it entirely.
            assert "no extra needed" in line, line
