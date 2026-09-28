"""`jfast check`: one battery, one exit code, and no silent green.

The tests that matter here are not the ones proving a broken project fails.
They are the ones proving a check that could not run says so. A battery that
reports a check it skipped as a pass is worse than no battery: it converts "I
did not look" into "I looked and it was fine", and the first person to trust it
ships the thing it never examined.

Every fixture is a real generated service -- the same templates `jfast new
service` renders -- because a checker tuned against a hand-written fixture only
proves it agrees with the fixture.
"""

from __future__ import annotations

import inspect
import json as jsonlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli import check as check_cli
from jfastframework.cli.exits import Code
from jfastframework.cli.patcher import insert_at_marker
from jfastframework.cli.scaffold import (
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)
from jfastframework.project import Finding

runner = CliRunner()


def _cli() -> typer.Typer:
    """A Typer app carrying nothing but `check`.

    The callback is load-bearing: a Typer app with exactly one command
    collapses into that command, and `["check", ...]` would then be parsed as
    arguments rather than as the command name.
    """
    app = typer.Typer()

    @app.callback()
    def _root() -> None: ...

    check_cli.register(app)
    return app


def _mount(root: Path, module: str) -> None:
    insert_at_marker(
        root / "main.py",
        "jfast:imports",
        f"from modules.{module} import router as {module}_router",
        guard=f"from modules.{module} import router as {module}_router",
        indent="",
    )
    insert_at_marker(
        root / "main.py",
        "jfast:routers",
        f"{module}_router,",
        guard=f"    {module}_router,",
        indent="    ",
    )


def _service(
    tmp_path: Path, *, mounted: Sequence[str] = ("invoice",), loose: Sequence[str] = ()
) -> Path:
    """A generated service, optionally with modules main.py never hears about."""
    root = tmp_path / "shop"
    scaffolder = Scaffolder()
    scaffolder.render_trees(service_trees("api", None, root), service_context("shop"))
    for name in (*mounted, *loose):
        scaffolder.render_trees(
            module_trees("layered", "api", root / "modules", root),
            module_context(name),
        )
    for name in mounted:
        _mount(root, name)
    return root


def _failed(name: str) -> check_cli.CheckResult:
    """A check with one finding, for pinning the exit-code precedence."""
    return check_cli.CheckResult(
        name=name,
        findings=(Finding(severity="critical", code="x", message="m", why="w"),),
    )


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(_cli(), ["check", "--path", str(root), *args])
    return result.exit_code, result.stdout


def _json(root: Path, *args: str) -> tuple[int, dict]:
    code, out = _run(root, "--json", *args)
    return code, jsonlib.loads(out)


# ---------------------------------------------------------------------------
# The shape of the thing
# ---------------------------------------------------------------------------


def test_register_takes_a_typer_app_and_returns_nothing() -> None:
    signature = inspect.signature(check_cli.register)
    assert list(signature.parameters) == ["app"]
    assert signature.parameters["app"].annotation in (typer.Typer, "typer.Typer")
    assert signature.return_annotation in (None, "None", type(None))


def test_a_healthy_generated_project_passes(tmp_path: Path) -> None:
    root = _service(tmp_path)
    code, payload = _json(root)
    assert code == Code.OK, payload
    assert payload["ok"] is True
    assert payload["failed"] == []


# ---------------------------------------------------------------------------
# A skip is not a pass. The point of the whole command.
# ---------------------------------------------------------------------------


def test_a_check_that_cannot_run_is_reported_as_skipped_not_as_a_pass(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root)
    contracts = next(c for c in payload["checks"] if c["name"] == "contracts")

    assert contracts["status"] == "skip"
    assert contracts["reason"], "a skip with no reason is indistinguishable from a pass"
    assert "contracts" in payload["skipped"]
    assert "contracts" not in payload["passed"]
    assert payload["complete"] is False
    assert payload["summary"]["skip"] >= 1
    assert code == Code.OK


def test_a_skip_is_visible_in_the_human_output_too(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    _, out = _run(root)
    assert "skipped" in out
    assert "contracts" in out


def test_ci_fails_on_a_skip(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root, "--ci")
    assert code == Code.ENVIRONMENT
    assert payload["ok"] is False


def test_allow_skips_is_the_only_way_past_it(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root, "--ci", "--allow-skips")
    assert code == Code.OK
    assert payload["skipped"] == ["contracts"]


def test_the_migrations_check_reports_itself_when_its_command_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sibling command may or may not be installed in this build. Either way
    # `check` must not pretend it ran.
    monkeypatch.setattr(check_cli, "_migration_findings", check_cli._unavailable)
    root = _service(tmp_path)
    _, payload = _json(root)
    migrations = next(c for c in payload["checks"] if c["name"] == "migrations")
    assert migrations["status"] == "skip"
    assert migrations["reason"]


# ---------------------------------------------------------------------------
# Findings and exit codes
# ---------------------------------------------------------------------------


def test_a_contract_violation_exits_with_the_contract_code(tmp_path: Path) -> None:
    root = _service(tmp_path)
    service = root / "modules" / "invoice" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + '\n\ndef shout() -> None:\n    print("x")\n',
        encoding="utf-8",
    )

    code, payload = _json(root)
    assert code == Code.CONTRACT
    assert "contracts" in payload["failed"]


def test_an_unregistered_module_exits_with_the_validation_code(tmp_path: Path) -> None:
    root = _service(tmp_path, loose=("order",))
    code, payload = _json(root)
    assert code == Code.VALIDATION
    assert "analyze" in payload["failed"]
    codes = [f["code"] for c in payload["checks"] for f in c["findings"]]
    assert "module-unregistered" in codes


LOCAL_PLUGIN = """\
from __future__ import annotations

from jfastframework.plugins.base import Plugin, PluginMeta


class ShopAuditPlugin(Plugin):
    meta = PluginMeta(name="shop_audit", version="1.0.0")
"""


@pytest.fixture
def restore_import_state() -> Iterator[None]:
    """`discover` prepends to `sys.path` and imports; neither may leak."""
    before = list(sys.path)
    modules = set(sys.modules)
    yield
    sys.path[:] = before
    for name in set(sys.modules) - modules:
        del sys.modules[name]


DECLARATION = '[plugins.paths]\nshop_audit = "shop_plugins.audit:ShopAuditPlugin"\n\n'


def _with_local_plugin(root: Path) -> None:
    """A plugin that lives in the repo, enabled and declared, as a user does it."""
    package = root / "shop_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "audit.py").write_text(LOCAL_PLUGIN, encoding="utf-8")

    config = root / "jfast.toml"
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("enabled = ["):
            lines[index] = line.replace("]", ', "shop_audit"]')
            break
    else:  # pragma: no cover -- the generated config always has one
        raise AssertionError("the generated jfast.toml has no [plugins].enabled")
    config.write_text(DECLARATION + "".join(lines), encoding="utf-8")


def test_a_plugin_declared_in_the_project_is_not_reported_as_missing(
    tmp_path: Path, restore_import_state: None
) -> None:
    """`[plugins.paths]` is a plugin that ships in the repo, not in a wheel.

    `check` runs `plugins` and `analyze` against one discovered set, so a
    discovery that ignores the declarations makes the second one call a plugin
    "enabled but not installed" three lines below the table that declares it --
    on a project where nothing is wrong.
    """
    root = _service(tmp_path)
    _with_local_plugin(root)

    code, payload = _json(root)
    codes = [f["code"] for c in payload["checks"] for f in c["findings"]]
    assert "plugin-unknown" not in codes, payload
    assert "plugin-unimportable" not in codes, payload
    assert code == Code.OK, payload


def test_the_same_plugin_without_the_declaration_is_reported(
    tmp_path: Path, restore_import_state: None
) -> None:
    """The case that must fail, and does.

    Identical tree, identical `[plugins].enabled`, one table removed. Without
    it the name really is unresolvable and `plugin-unknown` is the right
    answer -- which is what makes its absence in the test above evidence of
    anything.
    """
    root = _service(tmp_path)
    _with_local_plugin(root)
    config = root / "jfast.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(DECLARATION, ""),
        encoding="utf-8",
    )

    code, payload = _json(root)
    codes = [f["code"] for c in payload["checks"] for f in c["findings"]]
    assert "plugin-unknown" in codes, payload
    assert code != Code.OK


def test_a_contract_violation_outranks_a_structural_one(tmp_path: Path) -> None:
    root = _service(tmp_path, loose=("order",))
    service = root / "modules" / "invoice" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + '\n\ndef shout() -> None:\n    print("x")\n',
        encoding="utf-8",
    )

    code, payload = _json(root)
    assert code == Code.CONTRACT
    assert set(payload["failed"]) >= {"analyze", "contracts"}
    # The single number is one of many. The payload has to carry the rest or a
    # script has to guess what else broke.
    assert sorted(payload["codes"]) == [int(Code.VALIDATION), int(Code.CONTRACT)]


@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        (["config", "contracts"], Code.CONFIG),
        (["plugins", "migrations"], Code.ENVIRONMENT),
        (["migrations", "contracts"], Code.MIGRATION),
        (["contracts", "analyze"], Code.CONTRACT),
        (["analyze", "deploy"], Code.VALIDATION),
    ],
)
def test_the_precedence_is_cause_before_effect(failing: list[str], expected: Code) -> None:
    results = [_failed(name) for name in failing]
    assert check_cli.worst_code(results, fail_on="low", strict=False) == expected


def test_a_skip_under_ci_loses_to_a_real_failure() -> None:
    results = [_failed("contracts"), check_cli.skipped("migrations", "not installed")]
    assert check_cli.worst_code(results, fail_on="low", strict=True) == Code.CONTRACT


# ---------------------------------------------------------------------------
# What it does not check. The whole reason the name was a problem.
# ---------------------------------------------------------------------------

#: An unused import, a formatting violation, a `str` assigned to an `int`, and
#: -- next to it -- a failing test. Every one of the four tools `check` does not
#: run rejects this file; `check` itself has nothing to say about any of them.
BOMB = """\
import os,sys
def add(a: int, b: int) -> int:
    return a+b
BAD: int = "not an int"
"""

FAILING_TEST = "def test_this_fails() -> None:\n    assert 1 == 2\n"


def _armed(tmp_path: Path) -> Path:
    root = _service(tmp_path)
    (root / "modules" / "invoice" / "helpers.py").write_text(BOMB, encoding="utf-8")
    (root / "modules" / "invoice" / "tests" / "test_bomb.py").write_text(
        FAILING_TEST, encoding="utf-8"
    )
    return root


def test_the_injected_failures_are_real(tmp_path: Path) -> None:
    """Rule one: the case that must fail, and does -- before anything is claimed.

    `ruff` stands in for the four here because it is the one that runs in
    milliseconds. `ruff format --check`, `mypy` and `pytest` were each measured
    rejecting this same tree; the module docstring records the result.
    """
    if shutil.which("ruff") is None:  # pragma: no cover -- ruff is a dev dependency
        pytest.skip("ruff is not on PATH")
    root = _armed(tmp_path)
    finished = subprocess.run(
        ["ruff", "check", "--no-cache", str(root / "modules" / "invoice" / "helpers.py")],
        capture_output=True,
        text=True,
    )
    assert finished.returncode != 0, finished.stdout


def test_a_project_that_fails_all_four_still_passes_check(tmp_path: Path) -> None:
    """Measured, not assumed: this is the state the disclosure exists for.

    Clean and armed produce the same verdict, because none of the four tools is
    this command's question. That is defensible only while the command says so.
    """
    code, payload = _json(_armed(tmp_path))
    assert code == Code.OK
    assert payload["ok"] is True
    assert payload["failed"] == []


def test_the_run_names_the_four_it_did_not_check(tmp_path: Path) -> None:
    """The path a user takes: read the screen, decide what else the pipeline needs."""
    _, out = _run(_armed(tmp_path))
    assert "not checked here" in out
    for _, _, command in check_cli.NOT_COVERED:
        assert command in out, f"{command!r} missing from:\n{out}"


def test_the_disclosure_is_printed_on_a_failing_run_too(tmp_path: Path) -> None:
    """A green screen is the dangerous one, but a red screen must not drop it either."""
    root = _armed(tmp_path)
    service = root / "modules" / "invoice" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + '\n\ndef shout() -> None:\n    print("x")\n',
        encoding="utf-8",
    )
    code, out = _run(root)
    assert code == Code.CONTRACT
    assert "not checked here" in out


def test_the_json_payload_carries_what_it_did_not_check(tmp_path: Path) -> None:
    """`ok: true` reaches a script, and a script cannot read a footer."""
    _, payload = _json(_armed(tmp_path))
    commands = {entry["command"] for entry in payload["not_covered"]}
    assert commands == {command for _, _, command in check_cli.NOT_COVERED}
    assert all(entry["catches"] for entry in payload["not_covered"])


def test_the_help_does_not_claim_to_check_everything(tmp_path: Path) -> None:
    """The name is `check`; the help is the only place that can qualify it."""
    text = runner.invoke(_cli(), ["check", "--help"]).stdout
    for tool in ("ruff", "mypy", "pytest"):
        assert tool in text, f"`check --help` does not mention {tool}:\n{text}"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_only_runs_just_those_checks(tmp_path: Path) -> None:
    root = _service(tmp_path)
    _, payload = _json(root, "--only", "contracts,analyze")
    assert [c["name"] for c in payload["checks"]] == ["analyze", "contracts"]


def test_only_rejects_a_name_that_is_not_a_check(tmp_path: Path) -> None:
    root = _service(tmp_path)
    code, _ = _run(root, "--only", "vibes")
    assert code == Code.USAGE


def test_a_directory_that_is_not_a_service_exits_config(tmp_path: Path) -> None:
    code, _ = _run(tmp_path)
    assert code == Code.CONFIG


# ---------------------------------------------------------------------------
# Building, not just resolving
# ---------------------------------------------------------------------------


def _auth_service(tmp_path: Path) -> Path:
    """What `jfast new service <name> --with auth` actually generates.

    Which is a service that resolves and does not build: the template sets
    `mode = "jwks"` and leaves `jwks_url` to the environment, so the plugin
    raises the moment it registers.
    """
    root = tmp_path / "gated"
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        service_trees("api", None, root),
        service_context("gated", plugins=["auth"]),
    )
    return root


def test_a_service_that_cannot_be_built_is_not_a_pass(tmp_path: Path) -> None:
    # The plugin graph resolves here. It is registration that raises, and
    # registration is what `import main` reaches -- so a green run on this
    # tree means the battery reported on the half of the boot that cannot
    # fail on configuration.
    root = _auth_service(tmp_path)

    code, payload = _json(root, "--only", "plugins")

    assert code == Code.ENVIRONMENT
    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "service-unbuildable" in codes


def test_the_unbuildable_finding_names_what_is_missing(tmp_path: Path) -> None:
    root = _auth_service(tmp_path)

    _, payload = _json(root, "--only", "plugins")

    messages = [f["message"] for check in payload["checks"] for f in check["findings"]]
    assert any("jwks_url" in message for message in messages)


def test_a_service_that_builds_still_passes(tmp_path: Path) -> None:
    # The guard against a check that fails everything: the default template
    # has no such gap and must stay green.
    root = _service(tmp_path)

    code, payload = _json(root, "--only", "plugins")

    assert code == Code.OK
    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "service-unbuildable" not in codes


def _token_issuing_service(tmp_path: Path, *, cache: bool) -> Path:
    """A service that mints its own sessions, with and without a shared store."""
    root = tmp_path / ("cached" if cache else "alone")
    plugins = ["auth", "cache"] if cache else ["auth"]
    Scaffolder().render_trees(
        service_trees("api", None, root),
        service_context("sessions", plugins=plugins),
    )
    # Written, not appended: the template already carries a [plugin.auth]
    # table, and a second one is a TOML parse error -- which would skip the
    # check under test rather than run it.
    enabled = ", ".join(f'"{name}"' for name in ["observability", *plugins])
    (root / "jfast.toml").write_text(
        '[app]\nname = "sessions"\nversion = "0.1.0"\nenv = "local"\n\n'
        f"[plugins]\nenabled = [{enabled}]\ndisabled = []\n\n"
        '[plugin.auth]\nmode = "secret"\nissue_tokens = true\n'
        'issuer = "https://id.example"\naudience = "sessions"\n',
        encoding="utf-8",
    )
    return root


def test_a_service_minting_sessions_with_no_shared_store_is_reported(tmp_path: Path) -> None:
    """The environment in the file is not the environment it ships with.

    The plugin refuses to register in production, which is correct and also
    late: a service is developed at `env = "local"`, so the boot that fails is
    the deployment. Reported here at any environment.
    """
    root = _token_issuing_service(tmp_path, cache=False)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "session-store-per-process" in codes


def test_adding_the_cache_plugin_clears_it(tmp_path: Path) -> None:
    root = _token_issuing_service(tmp_path, cache=True)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "session-store-per-process" not in codes


def test_a_service_that_only_verifies_tokens_is_not_reported(tmp_path: Path) -> None:
    """It keeps no session, so it has none to lose. A finding here would be the
    irrelevant warning that teaches people to skip the output."""
    root = tmp_path / "verifier"
    Scaffolder().render_trees(
        service_trees("api", None, root), service_context("verifier", plugins=["auth"])
    )
    (root / "jfast.toml").write_text(
        '[app]\nname = "verifier"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "auth"]\ndisabled = []\n\n'
        '[plugin.auth]\nmode = "jwks"\njwks_url = "https://id.example/jwks"\n'
        "issue_tokens = false\n",
        encoding="utf-8",
    )

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "session-store-per-process" not in codes


def _database_service(tmp_path: Path, *, pool: int, overflow: int, ceiling: int) -> Path:
    root = tmp_path / f"db-{pool}-{overflow}-{ceiling}"
    Scaffolder().render_trees(
        service_trees("api", None, root), service_context("db", plugins=["database"])
    )
    (root / "jfast.toml").write_text(
        '[app]\nname = "db"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "database"]\ndisabled = []\n\n'
        f"[plugin.database]\npool_size = {pool}\nmax_overflow = {overflow}\n"
        f"server_max_connections = {ceiling}\n",
        encoding="utf-8",
    )
    return root


def test_the_default_pool_times_the_default_workers_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arithmetic nobody was doing.

    30 connections per process is the shipped default and the entrypoint starts
    one worker per CPU, so an eight-core host holds 240 against a PostgreSQL
    that accepts 100 -- and the failure lands on whichever service connects
    after this one.
    """
    monkeypatch.delenv("JFAST_WORKERS", raising=False)
    root = _database_service(tmp_path, pool=10, overflow=20, ceiling=100)

    _, payload = _json(root, "--only", "plugins")

    findings = [f for check in payload["checks"] for f in check["findings"]]
    reported = [f for f in findings if f["code"] == "pool-exceeds-server"]
    assert reported, [f["code"] for f in findings]
    assert "240" in reported[0]["message"]


def test_pinning_the_worker_count_is_believed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that sets JFAST_WORKERS has already answered this."""
    monkeypatch.setenv("JFAST_WORKERS", "2")
    root = _database_service(tmp_path, pool=10, overflow=20, ceiling=100)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "pool-exceeds-server" not in codes


def test_a_server_whose_size_nobody_here_knows_is_not_guessed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 turns it off, for a managed instance sized from RAM."""
    monkeypatch.delenv("JFAST_WORKERS", raising=False)
    root = _database_service(tmp_path, pool=10, overflow=20, ceiling=0)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "pool-exceeds-server" not in codes


def _tenant_database_service(tmp_path: Path, *, routed: bool, alias: bool = False) -> Path:
    root = tmp_path / ("routed" if routed else "unrouted")
    Scaffolder().render_trees(
        service_trees("api", None, root), service_context("t", plugins=["database"])
    )
    (root / "jfast.toml").write_text(
        '[app]\nname = "t"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "database"]\ndisabled = []\n\n'
        "[plugin.database]\nserver_max_connections = 0\n"
        'tenant_dsn_template = "postgresql+asyncpg://app:x@db:5432/{tenant}"\n',
        encoding="utf-8",
    )
    if routed:
        name = "TenantSession" if alias else "tenant_session_dependency"
        (root / "routes.py").write_text(
            f"from jfastframework.plugins.builtin.database import {name}\n",
            encoding="utf-8",
        )
    return root


def test_a_database_per_tenant_that_no_route_opens_is_reported(tmp_path: Path) -> None:
    """The configuration says one database per tenant and the generated module
    depends on `session_dependency`, which is the shared primary. Nothing
    fails; the per-tenant databases just stay empty."""
    root = _tenant_database_service(tmp_path, routed=False)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "tenant-databases-unused" in codes


def test_a_route_that_opens_one_clears_it(tmp_path: Path) -> None:
    root = _tenant_database_service(tmp_path, routed=True)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "tenant-databases-unused" not in codes


def test_the_tenant_session_alias_clears_it_too(tmp_path: Path) -> None:
    """`TenantSession` is what the docs now show; it must count as opening one."""
    root = _tenant_database_service(tmp_path, routed=True, alias=True)

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "tenant-databases-unused" not in codes


def test_one_shared_database_is_not_asked_about_routing(tmp_path: Path) -> None:
    """The ordinary case: no template, so there is nothing to route to."""
    root = tmp_path / "shared"
    Scaffolder().render_trees(
        service_trees("api", None, root), service_context("shared", plugins=["database"])
    )
    (root / "jfast.toml").write_text(
        '[app]\nname = "shared"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "database"]\ndisabled = []\n\n'
        "[plugin.database]\nserver_max_connections = 0\n",
        encoding="utf-8",
    )

    _, payload = _json(root, "--only", "plugins")

    codes = [f["code"] for check in payload["checks"] for f in check["findings"]]
    assert "tenant-databases-unused" not in codes


# ---------------------------------------------------------------------------
# A skip that is a blocked prerequisite, not an absence
# ---------------------------------------------------------------------------


def _unparseable(tmp_path: Path) -> Path:
    root = tmp_path / "broken"
    root.mkdir()
    (root / "jfast.toml").write_text('[app]\nname = "x"\nthis is not toml\n', encoding="utf-8")
    return root


def test_selecting_only_a_blocked_check_does_not_exit_zero(tmp_path: Path) -> None:
    """The green run over a service that cannot start.

    The full battery exits 2 because the config check reports the parse
    failure. `--only plugins` deselects that check, so nothing reported it and
    the skip left behind was read as success -- by a pipeline, silently.
    """
    root = _unparseable(tmp_path)

    code, _ = _json(root, "--only", "plugins")

    assert code == Code.CONFIG


def test_the_same_holds_for_a_check_two_steps_downstream(tmp_path: Path) -> None:
    """`--only deploy` needs both the config and the graph, and reports neither."""
    root = _unparseable(tmp_path)

    code, _ = _json(root, "--only", "deploy")

    assert code == Code.CONFIG


def test_a_skip_for_an_absence_is_still_a_pass(tmp_path: Path) -> None:
    """The other half, and the reason this is not just "skips fail now".

    No contracts.toml means the check had nothing to look at. Exiting non-zero
    there is the false failure that gets a battery removed from CI.
    """
    root = tmp_path / "fine"
    Scaffolder().render_trees(service_trees("api", None, root), service_context("fine", plugins=[]))

    code, payload = _json(root, "--only", "contracts")

    assert code == Code.OK
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses["contracts"] == "skip"


def test_the_json_says_what_blocked_it(tmp_path: Path) -> None:
    """A machine reading the report gets the same answer the exit code gives."""
    root = _unparseable(tmp_path)

    _, payload = _json(root, "--only", "plugins")

    plugins = next(check for check in payload["checks"] if check["name"] == "plugins")
    assert plugins["status"] == "skip"
    assert plugins["blocked_by"] == int(Code.CONFIG)


def test_fail_on_never_still_means_never(tmp_path: Path) -> None:
    """An escape hatch with an exception is not an escape hatch."""
    root = _unparseable(tmp_path)

    code, _ = _json(root, "--only", "plugins", "--fail-on", "never")

    assert code == Code.OK
