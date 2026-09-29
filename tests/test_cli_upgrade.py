"""What `jfast upgrade --check` promises, and the one promise that matters.

Every test builds a real project on disk. The command reads files and reports
on what it found there, so a mocked filesystem would test the mock.

The test that keeps this command worth reading is
`test_a_project_without_auth_is_never_told_about_refresh_tokens`. A report that
lists changes the project cannot be affected by is release notes with extra
steps, and after the second irrelevant warning nobody reads the first one
either.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework import project as project_scan
from jfastframework import upgrades
from jfastframework.cli.exits import Code
from jfastframework.cli.main import app
from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    Scaffolder,
    module_context,
    module_trees,
)
from jfastframework.cli.upgrade import register
from jfastframework.contracts import Contract, check
from jfastframework.project import load

runner = CliRunner()

#: Rich decides where the escape codes go, and that decision moves between
#: versions. Anything asserting on what a reader sees has to read through it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(rendered: str) -> str:
    """The help as text: no styling, no soft-wrap artefacts."""
    return re.sub(r"\s+", " ", _ANSI.sub("", rendered))


def build() -> typer.Typer:
    """A Typer app carrying nothing but this command.

    `main.py` is wired by hand at integration time; a test that imported it
    would fail for reasons belonging to a sibling command.
    """
    app = typer.Typer()
    register(app)

    # Typer collapses a one-command app into a bare callback, and `upgrade`
    # stops being a name you can type. The real app has dozens of commands;
    # this stands in for them so the invocation under test is the real one.
    @app.command("filler")
    def _filler() -> None:  # pragma: no cover
        pass

    return app


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


CONFIG = """\
[app]
name = "billing"
version = "0.1.0"
env = "local"

[plugins]
enabled = ["observability", "database"]
disabled = []
"""

AUTH_CONFIG = """\
[app]
name = "billing"
version = "0.1.0"
env = "local"

[plugins]
enabled = ["observability", "database", "auth"]
disabled = []

[plugin.auth]
mode = "jwks"
issue_tokens = true
"""

# `may_import` without "shared" is exactly what 0.1.0a3 generated.
CONTRACTS_A3 = """\
[project]
name = "billing"

[layers.http]
paths = ["modules/*/router.py"]
may_import = ["service", "schemas"]

[layers.service]
paths = ["modules/*/service.py"]
may_import = ["storage", "schemas"]

[layers.schemas]
paths = ["modules/*/schemas.py"]
may_import = []

[layers.shared]
paths = ["shared/*.py"]
may_import = []
"""

#: Where each layout's model lives inside its module template. `_python_files`
#: walks the whole project, so only the contents matter -- but rendering the
#: real path keeps this honest about which file the layout actually writes.
MODEL_TEMPLATES = {
    "layered": "module_layered/{{module}}/models.py.j2",
    "screaming": "module_screaming/{{module}}/storage.py.j2",
    "hexagonal": "module_hexagonal/{{module}}/infrastructure/orm.py.j2",
    "modular": "module_modular/{{module}}/models/{{module}}_entity.py.j2",
}


def generated_model(root: Path, layout: str = "layered", name: str = "invoice") -> Path:
    """The model `jfast module --layout <layout>` writes, rendered from its template.

    Hand-written stand-ins are what let `_tablenames` and its fixture agree
    with each other about a spelling and disagree with the product. This is the
    file the product produces.
    """
    context = module_context(name, layout=layout)
    destination = root / "modules" / str(context["module"]) / "models.py"
    write(
        destination,
        Scaffolder().env.get_template(MODEL_TEMPLATES[layout]).render(**context),
    )
    return destination


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A service pinned to 0.1.0a3, with timestamps and a stale contract."""
    write(tmp_path / "jfast.toml", CONFIG)
    write(tmp_path / "requirements.txt", "jfastframework[db,server]==0.1.0a3\n")
    write(tmp_path / "contracts.toml", CONTRACTS_A3)
    write(tmp_path / "main.py", "from modules.invoice import router\n")
    write(tmp_path / "modules" / "invoice" / "__init__.py", "")
    generated_model(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def test_prerelease_numbers_compare_numerically() -> None:
    # The string comparison this replaces reads "0.1.0a10" < "0.1.0a9".
    assert upgrades.parse_version("0.1.0a10") > upgrades.parse_version("0.1.0a9")


def test_ordering_matches_packaging() -> None:
    """The vendored key has to agree with the reference implementation.

    `packaging` is not a runtime dependency of this framework, so the shipped
    code cannot import it -- but it is installed for development, which makes
    it usable as an oracle here.
    """
    packaging_version = pytest.importorskip("packaging.version")
    corpus = [
        "0.1.0a1",
        "0.1.0a2",
        "0.1.0a9",
        "0.1.0a10",
        "0.1.0b1",
        "0.1.0rc1",
        "0.1.0",
        "0.1.0.post1",
        "0.1.1",
        "0.2.0.dev1",
        "0.2.0",
        "1.0",
        "1.0.0",
    ]
    ours = sorted(corpus, key=upgrades.parse_version)
    theirs = sorted(corpus, key=packaging_version.Version)
    assert ours == theirs


def test_an_unparseable_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        upgrades.parse_version("not-a-version")


# ---------------------------------------------------------------------------
# Finding the version the project pins
# ---------------------------------------------------------------------------


def test_the_pin_comes_from_requirements(project: Path) -> None:
    found = upgrades.pinned_version(project)
    assert found is not None
    assert found.version == "0.1.0a3"
    assert found.source == "requirements.txt"


def test_the_stamp_is_the_fallback(tmp_path: Path) -> None:
    """A project with no requirements file still stamps what scaffolded it."""
    write(tmp_path / "jfast.toml", CONFIG)
    write(
        tmp_path / ".jfast-template",
        '{"templates": {"service_base": {"framework_version": "0.1.0a3"}}}',
    )
    found = upgrades.pinned_version(tmp_path)
    assert found is not None
    assert found.version == "0.1.0a3"
    assert found.source == ".jfast-template"


def test_no_pin_at_all_is_a_config_error(tmp_path: Path) -> None:
    write(tmp_path / "jfast.toml", CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(tmp_path)])
    assert result.exit_code == Code.CONFIG, result.output


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_a_project_on_the_installed_version_has_nothing_to_report(project: Path) -> None:
    from jfastframework import __version__

    write(project / "requirements.txt", f"jfastframework[db,server]=={__version__}\n")
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert result.exit_code == Code.OK, result.output


def test_changes_that_apply_exit_with_the_compatibility_code(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert result.exit_code == Code.COMPATIBILITY, result.output


def test_the_timestamp_migration_names_the_real_table(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE invoices" in result.output
    # Without USING, PostgreSQL converts through the implicit cast and shifts
    # the whole table on any server whose TimeZone is not UTC.
    assert "USING created_at AT TIME ZONE 'UTC'" in result.output


def test_a_project_with_no_timestamps_is_not_told_to_migrate(project: Path) -> None:
    (project / "modules" / "invoice" / "models.py").write_text(
        'class Invoice:\n    __tablename__ = "invoices"\n', encoding="utf-8"
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE" not in result.output


@pytest.mark.parametrize("layout", sorted(MODEL_TEMPLATES))
def test_every_layout_template_declares_a_table_this_report_can_find(
    project: Path, layout: str
) -> None:
    # Each layout puts the model in a different file under a different name.
    # A report that only works for the layout the fixture happened to copy is
    # a report three quarters of projects get nothing out of.
    generated_model(project, layout=layout)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE invoices" in result.output


def test_an_annotated_tablename_is_not_invisible(project: Path) -> None:
    """`__tablename__: str = "invoices"` is the same declaration.

    SQLAlchemy 2.0 style annotates every other attribute in the class body, so
    the annotated form turns up in real projects. Read as `ast.Assign` only, the
    model declares no table and the report says so instead of naming one --
    which reads as "nothing to migrate" for a table that does need migrating.
    """
    models = project / "modules" / "invoice" / "models.py"
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            '__tablename__ = "invoices"', '__tablename__: str = "invoices"'
        ),
        encoding="utf-8",
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE invoices" in result.output
    assert "declares no __tablename__" not in result.output


#: A table rebuilt from another one. It is in the same file as the model that
#: does carry the mixin, which is where the per-file resolution went wrong.
PROJECTION = '''

class InvoiceMonthlyTotal(Base):
    """Rebuilt from invoices on a schedule. No mixin, no timestamp columns."""

    __tablename__ = "invoice_monthly_totals"
'''


def test_a_projection_table_in_the_same_file_gets_no_alter(project: Path) -> None:
    """The half of this report that was wrong.

    The mixin was resolved per file, so every `__tablename__` in a models file
    holding one mixin user got an `ALTER`. Against a table with no
    `created_at` PostgreSQL answers `ERROR: column "created_at" does not
    exist`, and the revision stops there -- after the statements before it
    have already taken ACCESS EXCLUSIVE and rewritten their own tables.
    """
    models = project / "modules" / "invoice" / "models.py"
    models.write_text(models.read_text(encoding="utf-8") + PROJECTION, encoding="utf-8")
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE invoices" in result.output
    assert "invoice_monthly_totals" not in result.output


def test_a_model_reaching_the_mixin_through_a_base_is_still_found(project: Path) -> None:
    """A shared base carrying the mixin is how a project stops repeating it."""
    write(
        project / "shared" / "models.py",
        "from jfastframework.db import Base, TimestampMixin\n\n\n"
        "class AuditedBase(Base, TimestampMixin):\n"
        "    __abstract__ = True\n",
    )
    write(
        project / "modules" / "invoice" / "models.py",
        "from shared.models import AuditedBase\n\n\n"
        "class Receipt(AuditedBase):\n"
        '    __tablename__ = "receipts"\n',
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE receipts" in result.output
    # `__abstract__` owns no table, so there is nothing to alter and nothing
    # missing either.
    assert "declares no __tablename__" not in result.output


def test_layers_missing_shared_are_named(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    for layer in ("http", "service", "schemas"):
        assert layer in result.output
    # `shared` itself must never appear as an offender: an empty may_import is
    # the entry that makes every other one safe.
    assert "layers.shared" not in result.output


def test_fixing_the_contract_removes_the_finding(project: Path) -> None:
    before = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "contracts-shared-import" in before.output

    (project / "contracts.toml").write_text(
        CONTRACTS_A3.replace('may_import = ["service", "schemas"]', 'may_import = ["shared"]')
        .replace('may_import = ["storage", "schemas"]', 'may_import = ["shared"]')
        .replace('[layers.schemas]\npaths = ["modules/*/schemas.py"]\nmay_import = []', ""),
        encoding="utf-8",
    )
    after = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "contracts-shared-import" not in after.output


# ---------------------------------------------------------------------------
# Not reporting what cannot apply. The point of the whole command.
# ---------------------------------------------------------------------------


def test_a_project_without_auth_is_never_told_about_refresh_tokens(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" not in result.output.lower()
    assert "logout" not in result.output.lower()


def test_auth_without_issue_tokens_is_still_not_told(project: Path) -> None:
    """Enabling the plugin is not enough: it has to be the thing minting tokens."""
    write(
        project / "jfast.toml",
        AUTH_CONFIG.replace("issue_tokens = true", "issue_tokens = false"),
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" not in result.output.lower()


def test_a_token_issuing_project_is_told(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" in result.output.lower()
    assert "/auth/logout" in result.output


def test_explicit_limits_suppress_the_defaults_notice(project: Path) -> None:
    write(
        project / "jfast.toml",
        CONFIG.replace(
            'env = "local"', 'env = "local"\nmax_body_bytes = 1024\nrequest_timeout = 5.0'
        ),
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "request-limit-defaults" not in result.output


def test_the_storage_defaults_are_the_ones_reported(project: Path) -> None:
    write(project / "jfast.toml", CONFIG.replace('"database"', '"database", "storage"'))
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "26214400" in result.output
    assert "120" in result.output


def test_the_limits_reported_are_the_ones_the_service_will_resolve(project: Path) -> None:
    """The half that was wrong: the report quoted a rule nobody implemented.

    The raised pair existed only where the scaffold had written it into
    jfast.toml, so a project that enabled `storage` afterwards was promised
    25 MiB here and answered a 3 MB upload with 413 at 2 MiB. Asserted against
    the kernel rather than against a literal, because a literal is what let
    the two drift apart.
    """
    import json

    from jfastframework.settings import JFastSettings

    write(project / "jfast.toml", CONFIG.replace('"database"', '"database", "storage"'))
    result = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    reported = next(
        change
        for change in json.loads(result.output)["changes"]
        if change["code"] == "request-limit-defaults"
    )
    settings = JFastSettings(  # type: ignore[call-arg]
        plugins=["observability", "database", "storage"], _env_file=None
    )
    assert reported["affected"][0].startswith(
        f"max_body_bytes = {settings.effective_max_body_bytes}"
    )
    assert reported["affected"][1].startswith(
        f"request_timeout = {settings.effective_request_timeout}"
    )


# ---------------------------------------------------------------------------
# Pagination, token stores, contract layout
# ---------------------------------------------------------------------------

REPOSITORY = """\
from jfastframework.db import BaseRepository

from .models import Invoice


class InvoiceRepository(BaseRepository[Invoice]):
    model = Invoice

    async def recent(self):
        return await self.paginate(limit=50, offset=0)
"""

TOKEN_STORE = """\
class DynamoTokenStore:
    async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
        return True
"""


def test_a_project_that_paginates_is_told_total_can_be_none(project: Path) -> None:
    write(project / "modules" / "invoice" / "repository.py", REPOSITORY)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "pagination-total-optional" in result.output
    assert "modules/invoice/repository.py" in result.output


def test_a_project_that_never_paginates_is_not_told(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "pagination-total-optional" not in result.output


def test_a_project_with_its_own_token_store_is_told_about_rotate_refresh(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG)
    write(project / "shared" / "store.py", TOKEN_STORE)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "token-store-rotate-refresh" in result.output
    assert "DynamoTokenStore.rotate_refresh" in result.output


def test_a_project_using_the_shipped_stores_is_not_told(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "token-store-rotate-refresh" not in result.output


def test_a_token_issuer_is_told_the_grace_window_is_on(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh-grace-seconds" in result.output


def test_an_issuer_that_chose_a_grace_window_is_not_told(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG + "refresh_grace_seconds = 0\n")
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh-grace-seconds" not in result.output


def test_a_layered_contract_over_hexagonal_modules_is_reported(project: Path) -> None:
    write(project / "jfast.toml", CONFIG + '\n[modules.invoice]\nlayout = "hexagonal"\nui = ""\n')
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "contracts-layout-mismatch" in result.output
    for layer in ("layers.http", "layers.service", "layers.schemas"):
        assert layer in result.output
    # `shared` governs shared/, not a module, so it says nothing about layout.
    assert "layers.shared" not in result.output


def test_a_contract_that_matches_the_recorded_layouts_is_not_reported(project: Path) -> None:
    write(project / "jfast.toml", CONFIG + '\n[modules.invoice]\nlayout = "layered"\nui = ""\n')
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "contracts-layout-mismatch" not in result.output


def test_a_project_that_recorded_no_layout_is_not_guessed_at(project: Path) -> None:
    """Nothing on disk says which layout an unrecorded module is in."""
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "contracts-layout-mismatch" not in result.output


def test_the_layers_named_are_the_ones_contracts_check_will_reject(tmp_path: Path) -> None:
    """The report and the checker have to name the same layers.

    Both generators run for real: a hand-written module tree and a
    hand-composed contract would agree with each other and disagree with the
    product. Naming one layer the checker does not reject is the failure mode
    this whole command exists to avoid -- and the first version of this detect
    had it, because `modules/*/repository.py` does match
    `modules/orders/infrastructure/repository.py`: fnmatch's `*` crosses a
    slash.
    """
    write(
        tmp_path / "jfast.toml",
        CONFIG + '\n[modules.orders]\nlayout = "hexagonal"\nui = "api"\n',
    )
    write(tmp_path / "requirements.txt", "jfastframework[db,server]==0.1.0a3\n")
    write(tmp_path / "shared" / "enums.py", "STATUS = 1\n")

    scaffolder = Scaffolder()
    scaffolder.render_trees(
        module_trees("hexagonal", "api", tmp_path / "modules", tmp_path),
        module_context("orders", layout="hexagonal"),
    )
    # The contract an 0.1.0a3 `jfast new service` wrote, whatever the modules
    # turned out to be.
    scaffolder.render_tree(
        CONTRACT_TEMPLATE_FOR["layered"],
        tmp_path,
        {"project": "billing", "layout": "layered", "Project": "Billing"},
        force=True,
    )

    reported = {
        line.split("]")[0].removeprefix("[layers.")
        for line in upgrades._contracts_layout_mismatch(load(tmp_path))
    }
    rejected = {
        violation.message.split("'")[1]
        for violation in check(Contract.load(tmp_path / "contracts.toml"), tmp_path)
        if violation.rule == "layer-unmatched"
    }
    assert reported == rejected
    assert reported


def test_the_exit_code_change_is_unconditional(project: Path) -> None:
    """Nothing on disk says whether CI branches on an exit code, so it is stated."""
    change = next(c for c in upgrades.CHANGES if c.code == "cli-exit-codes")
    assert change.detect is None


# ---------------------------------------------------------------------------
# Shape of the output
# ---------------------------------------------------------------------------


def test_json_is_machine_readable(project: Path) -> None:
    import json

    result = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    payload = json.loads(result.output)
    assert payload["from"] == "0.1.0a3"
    assert payload["ok"] is False
    codes = {change["code"] for change in payload["changes"]}
    assert "timestamps-timezone-aware" in codes
    assert "refresh-tokens-rejected" not in codes
    for change in payload["changes"]:
        assert set(change) >= {"version", "kind", "code", "summary", "detail", "remedy", "affected"}


def test_apply_is_out_of_scope_and_says_so() -> None:
    """The help says the flag exists and is not implemented.

    Read through `_plain`: `result.output` is rich's rendering, and rich is
    free to put an escape code between the dashes and the word. Searching the
    styled string for a literal `--apply` tests the colour scheme, and it fails
    on some interpreters and not others for that reason alone. What this cares
    about is the text a reader sees.
    """
    # Both axes pinned. COLUMNS stops rich soft-wrapping the flag across a
    # line -- which no amount of escape-stripping can put back together --
    # and _plain reads through the styling.
    result = runner.invoke(build(), ["upgrade", "--help"], env={"COLUMNS": "200"})
    help_text = _plain(result.output)
    assert "--apply" in help_text, help_text
    assert "not implemented" in help_text.lower(), help_text


def test_apply_is_refused(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--apply", "--path", str(project)])
    assert result.exit_code == Code.USAGE, result.output


# --- 0.1.0a5 -------------------------------------------------------------


def _project_on_a4(tmp_path, runner_, app_):
    """A generated service, pinned back to the version it would be upgrading
    from. Generated at the installed version, it pins that, and `upgrade` has
    nothing between the two to report -- which is a true answer to a different
    question than the one these tests ask.
    """
    target = tmp_path / "shop"
    assert runner_.invoke(app_, ["new", "service", "shop", "--target", str(target)]).exit_code == 0
    assert (
        runner_.invoke(
            app_,
            [
                "new",
                "module",
                "billing",
                "--layout",
                "modular",
                "--root",
                str(target),
                "--target",
                str(target / "modules"),
            ],
        ).exit_code
        == 0
    )
    pin = target / "requirements.txt"
    # The line carries extras: jfastframework[db,metrics,server]==0.1.0a5
    pin.write_text(
        pin.read_text(encoding="utf-8").replace(
            f"=={__import__('jfastframework').__version__}", "==0.1.0a4"
        ),
        encoding="utf-8",
    )
    return target


def test_a_narrowed_glob_names_the_file_that_changed_hands(tmp_path):
    """`*` used to cross `/`, so a layer claimed files arbitrarily deep. The
    note has to name them: "your globs narrowed" is not actionable, and which
    files moved is a fact about this project's tree, not about the patterns.
    """
    root = _project_on_a4(tmp_path, runner, app)

    contract = root / "contracts.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            'paths = ["modules/*/api/*.py"]',
            'paths = ["modules/*/api/*.py", "modules/*/handlers.py"]',
            1,
        ),
        encoding="utf-8",
    )
    deep = root / "modules" / "billing" / "deep" / "nested"
    deep.mkdir(parents=True)
    (deep / "handlers.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["upgrade", "--path", str(root)])
    assert "layer-globs-narrowed" in result.output, result.output
    assert "modules/billing/deep/nested/handlers.py" in result.output, result.output

    # And the half that must stay quiet: a contract whose globs mean the same
    # thing under both matchers gets no note, or the note is noise on upgrade.
    clean = _project_on_a4(tmp_path / "clean", runner, app)
    assert (
        "layer-globs-narrowed" not in runner.invoke(app, ["upgrade", "--path", str(clean)]).output
    )


def test_the_naive_datetime_note_names_the_line_and_its_remedy_works(tmp_path):
    """The detector read `getattr(violation, "code", "")` when the field is
    `rule`, so it returned an empty list for every project on earth and the
    note never fired. Nothing about the note's wording would have shown that,
    which is why this asserts the line number out of a real scan.
    """
    root = _project_on_a4(tmp_path, runner, app)
    stamp = root / "modules" / "billing" / "api" / "stamp.py"
    stamp.write_text(
        "from datetime import datetime\n\n\ndef stamped() -> str:\n"
        "    return datetime.now().isoformat()\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["upgrade", "--path", str(root)])
    assert "naive-datetime-rule" in result.output, result.output
    assert "modules/billing/api/stamp.py:5" in result.output, result.output

    # The remedy the note prints. If this does not silence the rule, the note
    # is telling people to edit a key that does nothing.
    contract = root / "contracts.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "naive_datetime = true", "naive_datetime = false", 1
        ),
        encoding="utf-8",
    )
    silenced = runner.invoke(app, ["contracts", "check", "--file", str(root / "contracts.toml")])
    assert "naive-datetime" not in silenced.output, silenced.output


def test_silencing_naive_datetime_leaves_the_async_check_running(tmp_path):
    """Both rules ride [rules.async_safety]. The switch is worth having only if
    it separates them, so both have to be in the same run: one violation of
    each, one flag flipped, and the two answers must differ. Asserting only
    that async-blocking survives passes under a shared switch as well -- it
    was never the half at risk.
    """
    root = _project_on_a4(tmp_path, runner, app)
    api = root / "modules" / "billing" / "api"
    (api / "slow.py").write_text(
        "import time\n\n\nasync def wait() -> None:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    (api / "stamp.py").write_text(
        "from datetime import datetime\n\n\ndef stamped() -> str:\n"
        "    return datetime.now().isoformat()\n",
        encoding="utf-8",
    )
    check = ["contracts", "check", "--file", str(root / "contracts.toml")]

    both = runner.invoke(app, check)
    assert "naive-datetime" in both.output, both.output
    assert "async-blocking" in both.output, both.output

    contract = root / "contracts.toml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "naive_datetime = true", "naive_datetime = false", 1
        ),
        encoding="utf-8",
    )

    one = runner.invoke(app, check)
    assert "naive-datetime" not in one.output, one.output
    assert "async-blocking" in one.output, one.output


def test_a_note_is_invisible_to_a_project_pinned_at_its_own_version(tmp_path):
    """The boundary that makes a mistagged note silent rather than wrong.

    `applicable` keeps `(current, installed]`, so a note tagged with the
    version a project already pins is skipped -- and the published version is
    the one every project pins. Three notes shipped tagged 0.1.0a4 describing
    code that does not exist in 0.1.0a4, and `jfast upgrade` answered "nothing
    between those versions affects this project" to all three. The rule the
    version field follows is: the release the code LANDED in, never the release
    being prepared.
    """
    # A real scanned project: `applicable` takes one, and the boundary
    # under test is the version arithmetic, which runs before any detector.
    target = tmp_path / "shop"
    assert runner.invoke(app, ["new", "service", "shop", "--target", str(target)]).exit_code == 0
    project = project_scan.load(target)
    change = upgrades.Change(
        version="0.1.0a4",
        kind="breaking",
        code="test-only",
        summary="s",
        detail="d",
        remedy="r",
        detect=None,
    )
    original = upgrades.CHANGES
    upgrades.CHANGES = (change,)
    try:
        from_a4 = upgrades.applicable(project, current="0.1.0a4", installed="0.1.0a5")
        from_a3 = upgrades.applicable(project, current="0.1.0a3", installed="0.1.0a5")
    finally:
        upgrades.CHANGES = original

    assert from_a4 == [], "a note tagged with the pinned version must not fire"
    assert [c.code for c, _ in from_a3] == ["test-only"]


def test_no_note_claims_a_version_newer_than_this_release():
    """A note tagged ahead of `__version__` can never fire: `applicable` caps
    at the installed version, so it is dead text that reads like coverage.
    """
    from jfastframework import __version__

    ceiling = upgrades.parse_version(__version__)
    ahead = [c.code for c in upgrades.CHANGES if upgrades.parse_version(c.version) > ceiling]
    assert ahead == [], f"notes tagged after {__version__}: {ahead}"


def test_a_stale_compose_file_is_named_along_with_what_is_missing(tmp_path):
    """The upgrade a project cannot get by reinstalling.

    Both files belong to the project once generated, so 0.1.0a7 does not
    rewrite them: a service scaffolded earlier keeps a compose file that builds
    an image nothing wrote and starts a container pointed at the host's DSN.
    """
    target = tmp_path / "billing"
    assert runner.invoke(app, ["new", "service", "billing", "--target", str(target)]).exit_code == 0

    # What a project generated before 0.1.0a7 has on disk.
    compose = target / "docker-compose.yml"
    rendered = runner.invoke(
        app,
        ["deploy", "compose", "--config", str(target / "jfast.toml"), "-o", str(compose)],
    )
    assert rendered.exit_code == 0, rendered.output
    compose.write_text(
        "\n".join(
            line
            for line in compose.read_text(encoding="utf-8").splitlines()
            if "JFAST_DB_DSN" not in line
        ),
        encoding="utf-8",
    )
    (target / "Dockerfile").unlink()

    found = upgrades._stale_deploy_artifacts(project_scan.load(target))

    assert any("no Dockerfile to build" in line for line in found)
    assert any("JFAST_DB_DSN" in line for line in found)


def test_a_freshly_generated_project_is_told_nothing(tmp_path):
    """The half that keeps the report worth reading.

    A detector that fires on the output of the generator it ships with turns
    every upgrade into a warning nobody can act on.
    """
    target = tmp_path / "billing"
    assert runner.invoke(app, ["new", "service", "billing", "--target", str(target)]).exit_code == 0
    rendered = runner.invoke(
        app,
        [
            "deploy",
            "compose",
            "--config",
            str(target / "jfast.toml"),
            "-o",
            str(target / "docker-compose.yml"),
        ],
    )
    assert rendered.exit_code == 0, rendered.output

    assert upgrades._stale_deploy_artifacts(project_scan.load(target)) == []


def test_a_project_with_no_compose_file_has_nothing_stale(tmp_path):
    """Nothing to regenerate: the next `jfast deploy compose` writes the current
    shape, so reporting here would be noise on a project that is already fine."""
    target = tmp_path / "billing"
    assert runner.invoke(app, ["new", "service", "billing", "--target", str(target)]).exit_code == 0

    assert upgrades._stale_deploy_artifacts(project_scan.load(target)) == []


# ---------------------------------------------------------------------------
# 0.1.0a8: the three that stop a boot
# ---------------------------------------------------------------------------


def _service(tmp_path: Path, config: str, *, name: str = "svc") -> Path:
    root = tmp_path / name
    write(root / "jfast.toml", config)
    write(root / "requirements.txt", "jfastframework[db,server]==0.1.0a7\n")
    return root


def test_a_service_minting_tokens_without_a_shared_store_is_told_before_the_deploy(
    tmp_path: Path,
) -> None:
    """The refusal lands at boot, and a boot is the worst place to learn it."""
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "auth"]\ndisabled = []\n\n'
        "[plugin.auth]\nissue_tokens = true\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    codes = [change.code for change, _ in found]
    assert "session-store-per-process" in codes
    remedy = next(c.remedy for c, _ in found if c.code == "session-store-per-process")
    assert "issue_tokens = false" in remedy


def test_the_cache_plugin_clears_it(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "cache", "auth"]\ndisabled = []\n\n'
        "[plugin.auth]\nissue_tokens = true\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    assert "session-store-per-process" not in [c.code for c, _ in found]


def test_a_default_mail_backend_is_reported_because_the_default_is_the_silent_one(
    tmp_path: Path,
) -> None:
    """`console` is what a project has unless it said otherwise, which is why
    this is reported on a configuration that names no backend at all."""
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "mail"]\ndisabled = []\n',
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    reported = [affected for change, affected in found if change.code == "mail-backend-silent"]
    assert reported and 'backend = "console"' in reported[0][0]


def test_an_smtp_backend_is_left_alone(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "mail"]\ndisabled = []\n\n'
        '[plugin.mail]\nbackend = "smtp"\n',
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    assert "mail-backend-silent" not in [c.code for c, _ in found]


def test_a_job_timeout_past_the_window_names_the_line(tmp_path: Path) -> None:
    """The two numbers live in different files, which is why nobody compared
    them. This is the comparison, done statically."""
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "queue"]\ndisabled = []\n\n'
        "[plugin.queue]\nvisibility_timeout = 300\n",
    )
    write(
        root / "worker.py",
        "from jfastframework.queues.worker import Worker\n"
        "worker = Worker(backend, registry, job_timeout=600)\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    reported = [a for change, a in found if change.code == "job-timeout-past-visibility"]
    assert reported
    assert "worker.py" in reported[0][0]
    assert "600" in reported[0][0]


def test_a_job_timeout_inside_the_window_is_not_reported(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
        '[plugins]\nenabled = ["observability", "queue"]\ndisabled = []\n\n'
        "[plugin.queue]\nvisibility_timeout = 1800\n",
    )
    write(
        root / "worker.py",
        "from jfastframework.queues.worker import Worker\n"
        "worker = Worker(backend, registry, job_timeout=600)\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a7", installed="0.1.0a8")

    assert "job-timeout-past-visibility" not in [c.code for c, _ in found]


_DB_CONFIG = (
    '[app]\nname = "s"\nversion = "0.1.0"\nenv = "local"\n\n'
    '[plugins]\nenabled = ["observability", "database"]\ndisabled = []\n'
)


def test_a_session_that_commits_after_the_response_names_the_line(tmp_path: Path) -> None:
    """The database plugin refuses these at boot; this finds them on a laptop."""
    root = _service(tmp_path, _DB_CONFIG)
    write(
        root / "routes.py",
        "from fastapi import Depends\n"
        "from jfastframework.plugins.builtin.database import (\n"
        "    read_session_dependency, session_dependency)\n"
        "def get_service(session=Depends(session_dependency)): ...\n"
        "def fine(session=Depends(session_dependency, scope='function')): ...\n"
        "def reads(session=Depends(read_session_dependency)): ...\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a8", installed="0.1.0a9")

    reported = [a for change, a in found if change.code == "session-commits-after-response"]
    assert reported
    assert reported[0] == [
        "routes.py:4  ->  Depends(session_dependency)",
        "routes.py:6  ->  Depends(read_session_dependency)",
    ]


def test_the_scoped_aliases_are_not_reported(tmp_path: Path) -> None:
    root = _service(tmp_path, _DB_CONFIG)
    write(
        root / "routes.py",
        "from jfastframework.plugins.builtin.database import DbSession\n"
        "def get_service(session: DbSession): ...\n",
    )

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a8", installed="0.1.0a9")

    assert "session-commits-after-response" not in [c.code for c, _ in found]


def test_an_env_py_that_would_drop_framework_tables_is_named(tmp_path: Path) -> None:
    root = _service(tmp_path, _DB_CONFIG)
    write(root / "migrations" / "env.py", "target_metadata = Base.metadata\n")

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a8", installed="0.1.0a9")

    reported = [a for change, a in found if change.code == "autogenerate-drops-framework-tables"]
    assert reported == [["migrations/env.py"]]


def test_an_env_py_with_the_filter_is_not(tmp_path: Path) -> None:
    root = _service(tmp_path, _DB_CONFIG)
    write(root / "migrations" / "env.py", "context.configure(include_name=include_name)\n")

    found = upgrades.applicable(project_scan.load(root), current="0.1.0a8", installed="0.1.0a9")

    assert "autogenerate-drops-framework-tables" not in [c.code for c, _ in found]
