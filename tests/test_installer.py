"""The installer and the plugin commands: `jfast init`'s menu, the multitenant
answer, `jfast start`'s flags, and `jfast add`/`jfast remove` for plugins.

The menu used to be a hand-written list that stopped at 0.1.0a8 and
pre-selected nothing, so pressing Enter through it produced a service with
none of the plugins added since. These tests hold it to the catalog.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.commands import add as add_command
from jfastframework.cli.commands import install
from jfastframework.cli.generate import _write_service_envs
from jfastframework.cli.main import app
from jfastframework.cli.scaffold import (
    BASE_PLUGINS,
    DATASTORE_PLUGINS,
    PLUGIN_CATALOG,
    RECOMMENDED,
    service_context,
)
from jfastframework.workspace import Workspace

runner = CliRunner()


def _toml(path: Path) -> dict:  # type: ignore[type-arg]
    return tomllib.loads(path.read_text(encoding="utf-8"))


# -- the menu -----------------------------------------------------------------


def test_every_catalogued_plugin_is_offered_or_decided_by_another_question() -> None:
    choices, _, skipped = install.capability_choices("api", ["database"], multitenant=False)
    offered = {choice.key for choice in choices} | set(skipped)
    decided = {*DATASTORE_PLUGINS, *BASE_PLUGINS, "tenancy", "gateway"}
    assert offered | decided == set(PLUGIN_CATALOG)
    for late in ("llm", "accounts", "outbox", "idempotency", "ratelimit", "http"):
        assert late in offered, f"{late} shipped after 0.1.0a8 and must be in the menu"
    for late in ("websocket", "channels", "telemetry"):
        assert late in offered


def test_recommended_plugins_are_pre_checked_labelled_and_first() -> None:
    choices, defaults, _ = install.capability_choices("api", ["database"], multitenant=False)
    assert {"telemetry", "queue"} <= set(RECOMMENDED)
    assert defaults == set(RECOMMENDED)
    for choice in choices[: len(defaults)]:
        assert choice.key in defaults
        assert "recommended" in choice.hint
    assert not {"auth", "accounts"} & defaults, "one customer needs no sign-in by default"


def test_a_multitenant_answer_also_recommends_auth_and_accounts() -> None:
    _, defaults, _ = install.capability_choices("api", ["database"], multitenant=True)
    assert {"auth", "accounts", "telemetry", "queue"} <= defaults


def test_a_plugin_this_install_cannot_import_is_skipped_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "plugin_importable", lambda name: name != "telemetry")
    choices, defaults, skipped = install.capability_choices("api", [], multitenant=False)
    assert skipped == ["telemetry"]
    assert "telemetry" not in {c.key for c in choices} | defaults


def test_rag_says_it_brings_a_store_when_none_was_chosen() -> None:
    choices, _, _ = install.capability_choices("api", ["cache"], multitenant=False)
    rag = next(c for c in choices if c.key == "rag")
    assert "PostgreSQL" in rag.hint


def test_the_catalog_requires_what_each_plugin_requires() -> None:
    """`jfast add` enables requirements from the catalog, without importing plugins."""
    from importlib.metadata import entry_points

    for entry in entry_points(group="jfastframework.plugins"):
        if entry.name not in PLUGIN_CATALOG:
            continue
        try:
            plugin = entry.load()
        except ImportError:  # pragma: no cover - an optional dependency missing
            continue
        assert tuple(PLUGIN_CATALOG[entry.name].requires) == tuple(plugin.meta.requires), entry.name


# -- one answer, every piece ---------------------------------------------------


def test_multitenant_sets_every_piece_consistently() -> None:
    context = service_context("shop", plugins=["database", "rag", "llm"], multitenant=True)
    assert {"tenancy", "auth"} <= set(context["enabled_plugins"])
    assert context["tenancy_sources"] == ["token", "user"]
    assert context["route_access"] == "tenant"


def test_single_tenant_keeps_tenancy_off_and_routes_open() -> None:
    context = service_context("shop", plugins=["database", "rag", "llm"])
    assert "tenancy" not in context["enabled_plugins"]
    assert context["route_access"] == "open"


@pytest.mark.parametrize("multitenant", [True, False])
def test_rag_scope_and_llm_budget_follow_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multitenant: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    flags = ["--multitenant"] if multitenant else []
    result = runner.invoke(
        app, ["new", "service", "shop", "--with", "database,rag,llm,accounts", *flags]
    )
    assert result.exit_code == 0, result.output
    config = _toml(tmp_path / "shop" / "jfast.toml")
    assert config["plugin"]["rag"]["tenant_scoped"] is multitenant
    assert ("tenant_budget_usd" in config["plugin"]["llm"]) is multitenant
    assert config["plugin"]["auth"]["mode"] == "secret"
    assert config["plugin"]["auth"]["issue_tokens"] is True
    if multitenant:
        assert config["plugin"]["tenancy"]["sources"] == ["token", "user"]
        assert "base_domain" not in config["plugin"]["tenancy"]


# -- jfast start ----------------------------------------------------------------


def _start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str) -> Path:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["start", "shop", *flags])
    assert result.exit_code == 0, result.output
    return tmp_path / "shop"


def test_start_turns_telemetry_on_and_no_telemetry_leaves_it_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on = _start(tmp_path / "on", monkeypatch)
    off = _start(tmp_path / "off", monkeypatch, "--no-telemetry")
    assert "telemetry" in _toml(on / "jfast.toml")["plugins"]["enabled"]
    assert "telemetry" not in _toml(off / "jfast.toml")["plugins"]["enabled"]


def test_start_is_single_tenant_by_default_and_its_frontend_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _start(tmp_path, monkeypatch)
    enabled = _toml(api / "jfast.toml")["plugins"]["enabled"]
    assert not {"tenancy", "accounts"} & set(enabled)
    routes = (api / "modules" / "item" / "api" / "routes.py").read_text(encoding="utf-8")
    assert "current_tenant" not in routes
    entity = (api / "modules" / "item" / "models" / "item_entity.py").read_text(encoding="utf-8")
    assert "TenantMixin" in entity, "the tenant_id column stays either way"
    router = (tmp_path / "shop-web" / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "SecurityView" not in router


def test_start_multitenant_scopes_routes_mints_secrets_and_draws_account_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _start(tmp_path, monkeypatch, "--multitenant")
    config = _toml(api / "jfast.toml")
    assert {"tenancy", "auth", "accounts"} <= set(config["plugins"]["enabled"])
    routes = (api / "modules" / "item" / "api" / "routes.py").read_text(encoding="utf-8")
    assert "Depends(current_tenant)" in routes
    env = (api / ".env").read_text(encoding="utf-8")
    assert "JFAST_AUTH_SECRET=" in env and "CHANGEME" not in env
    router = (tmp_path / "shop-web" / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "SecurityView" in router


def test_a_fresh_multitenant_project_is_ready_for_its_own_readiness_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _start(tmp_path, monkeypatch, "--multitenant")
    monkeypatch.chdir(api)
    for layout in ("layered", "screaming", "hexagonal"):
        result = runner.invoke(app, ["new", "module", f"m_{layout}", "--layout", layout])
        assert result.exit_code == 0, result.output
    done = subprocess.run(
        [sys.executable, "-m", "jfastframework", "check", "--multitenant-ready", "--json"],
        cwd=api,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(done.stdout)
    assert report["findings"] == [], report["findings"]
    assert len(report["tenant_tables"]) == 4, "every generated entity is a tenant table"


def test_a_spa_added_later_draws_account_pages_only_if_a_backend_has_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["workspace", "init", "shop"]).exit_code == 0
    assert runner.invoke(app, ["new", "service", "api", "--with", "database"]).exit_code == 0
    first = runner.invoke(app, ["new", "service", "web", "--kind", "spa", "--frontend", "vue"])
    assert first.exit_code == 0, first.output
    public = (tmp_path / "web" / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "SecurityView" not in public

    with_accounts = ["new", "service", "id", "--with", "database,accounts"]
    assert runner.invoke(app, with_accounts).exit_code == 0
    second = runner.invoke(app, ["new", "service", "admin", "--kind", "spa", "--frontend", "vue"])
    assert second.exit_code == 0, second.output
    private = (tmp_path / "admin" / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "SecurityView" in private


def test_rewriting_env_files_keeps_what_the_graph_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _start(tmp_path, monkeypatch, "--multitenant")
    secret = next(
        line
        for line in (api / ".env").read_text(encoding="utf-8").splitlines()
        if line.startswith("JFAST_AUTH_SECRET=")
    )
    workspace = Workspace.load_or_none()
    assert workspace is not None
    _write_service_envs(workspace)
    assert secret in (api / ".env").read_text(encoding="utf-8")


# -- jfast add / jfast remove ---------------------------------------------------


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["new", "service", "svc", "--with", "database"]).exit_code == 0
    monkeypatch.chdir(tmp_path / "svc")
    return tmp_path / "svc"


def test_add_enables_a_plugin_with_what_it_requires_and_its_settings(service: Path) -> None:
    before = (service / "jfast.toml").read_text(encoding="utf-8")
    result = runner.invoke(app, ["add", "accounts", "--no-install"])
    assert result.exit_code == 0, result.output
    config = _toml(service / "jfast.toml")
    assert config["plugins"]["enabled"][-2:] == ["auth", "accounts"]
    assert config["plugin"]["auth"]["mode"] == "secret", "the block the generator writes"
    assert "accounts" in config["plugin"]
    requirements = (service / "requirements.txt").read_text(encoding="utf-8")
    assert "accounts" in requirements and "auth" in requirements
    assert "JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD" in result.output
    # Every comment the file had is still there.
    after = (service / "jfast.toml").read_text(encoding="utf-8")
    for line in before.splitlines():
        if line.startswith("#"):
            assert line in after


def test_adding_twice_changes_nothing(service: Path) -> None:
    assert runner.invoke(app, ["add", "telemetry", "--no-install"]).exit_code == 0
    once = (service / "jfast.toml").read_text(encoding="utf-8")
    result = runner.invoke(app, ["add", "telemetry", "--no-install"])
    assert result.exit_code == 0
    assert "already enables" in result.output
    assert (service / "jfast.toml").read_text(encoding="utf-8") == once


def test_add_refuses_a_plugin_this_install_lacks(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(add_command, "plugin_importable", lambda name: False)
    result = runner.invoke(app, ["add", "telemetry", "--no-install"])
    assert result.exit_code == 1
    assert "telemetry" not in _toml(service / "jfast.toml")["plugins"]["enabled"]


def _record_pip(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_add_never_installs_an_older_pin_over_the_running_framework(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Cuadra migration: requirements.txt still pinned 0.1.0a10, and
    # `jfast add telemetry` reinstalled a10 over the a11 that was running.
    monkeypatch.setattr(add_command, "_editable_location", lambda: None)
    requirements = service / "requirements.txt"
    body = requirements.read_text(encoding="utf-8")
    requirements.write_text(
        re.sub(r"(jfastframework\[[^\]]*\])==\S+", r"\1==0.0.1", body), encoding="utf-8"
    )
    calls = _record_pip(monkeypatch)
    result = runner.invoke(app, ["add", "telemetry"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "pins jfastframework==0.0.1" in " ".join(result.output.split())
    assert "telemetry" in requirements.read_text(encoding="utf-8"), "the pin is still edited"


def test_add_never_replaces_an_editable_checkout(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(add_command, "_editable_location", lambda: "/src/jfast")
    calls = _record_pip(monkeypatch)
    result = runner.invoke(app, ["add", "telemetry"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert 'pip install -e "/src/jfast[' in " ".join(result.output.split())


def test_add_installs_when_the_pin_is_this_version(
    service: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jfastframework import __version__

    monkeypatch.setattr(add_command, "_editable_location", lambda: None)
    assert f"=={__version__}" in (service / "requirements.txt").read_text(encoding="utf-8")
    calls = _record_pip(monkeypatch)
    result = runner.invoke(app, ["add", "telemetry"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][-2:] == ["-r", str(service / "requirements.txt")]


def test_remove_refuses_while_another_plugin_needs_it(service: Path) -> None:
    assert runner.invoke(app, ["add", "accounts", "--no-install"]).exit_code == 0
    refused = runner.invoke(app, ["remove", "auth"])
    assert refused.exit_code == 1
    assert "accounts" in refused.output
    assert "auth" in _toml(service / "jfast.toml")["plugins"]["enabled"]


def test_remove_takes_the_plugin_and_its_unshared_extra_out(service: Path) -> None:
    assert runner.invoke(app, ["add", "accounts", "--no-install"]).exit_code == 0
    assert runner.invoke(app, ["remove", "accounts"]).exit_code == 0
    assert runner.invoke(app, ["remove", "auth"]).exit_code == 0
    config = _toml(service / "jfast.toml")
    assert not {"auth", "accounts"} & set(config["plugins"]["enabled"])
    assert "auth" in config["plugin"], "settings stay for the day it comes back"
    requirements = (service / "requirements.txt").read_text(encoding="utf-8")
    assert "accounts" not in requirements and "auth," not in requirements


def test_remove_keeps_an_extra_another_plugin_still_uses(service: Path) -> None:
    assert runner.invoke(app, ["add", "outbox", "--no-install"]).exit_code == 0
    assert runner.invoke(app, ["add", "idempotency", "--no-install"]).exit_code == 0
    assert runner.invoke(app, ["remove", "outbox"]).exit_code == 0
    assert "db" in (service / "requirements.txt").read_text(encoding="utf-8")


def test_remove_names_the_plugins_that_exist(service: Path) -> None:
    result = runner.invoke(app, ["remove", "telemetri"])
    assert result.exit_code != 0
    assert "telemetry" in result.output


# -- the frontend's dev server may call the API --------------------------------


def test_start_lets_the_frontend_dev_server_call_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Found building a receipts SaaS from scratch: the generated frontend on
    # :8610 called the API on :8600 and the browser blocked everything (the
    # preflight answered 405) -- nothing wrote cors_origins.
    from fastapi.testclient import TestClient

    from jfastframework import create_app

    api = _start(tmp_path, monkeypatch)
    workspace = Workspace.load(tmp_path / "jfast.workspace.toml")
    port = workspace.frontends[0].port
    origins = _toml(api / "jfast.toml")["app"]["cors_origins"]
    assert origins == [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]

    monkeypatch.chdir(api)
    client = TestClient(create_app(config_path="jfast.toml"))
    allowed = client.options(
        "/health",
        headers={"Origin": origins[0], "Access-Control-Request-Method": "GET"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == origins[0]


def test_dev_cors_keeps_configured_origins_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jfastframework.cli.generate import write_dev_cors

    api = _start(tmp_path, monkeypatch)
    config = api / "jfast.toml"
    text = config.read_text(encoding="utf-8").replace(
        "cors_origins = [", 'cors_origins = ["https://app.example.com", ', 1
    )
    config.write_text(text, encoding="utf-8")
    workspace = Workspace.load(tmp_path / "jfast.workspace.toml")
    assert write_dev_cors(workspace) == []
    origins = _toml(config)["app"]["cors_origins"]
    assert origins[0] == "https://app.example.com" and len(origins) == 3
