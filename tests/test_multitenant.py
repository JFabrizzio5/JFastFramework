"""Single-tenant today, multitenant tomorrow: the static half.

Three things, each with a fixture that trips it and one that does not:

* the ``tenancy`` check of ``jfast check`` -- settings that contradict each
  other or the code;
* ``jfast check --multitenant-ready`` -- what a switch would break, one fixture
  per rule, plus a clean one and a waived one;
* ``jfast tenancy enable`` without a database -- the revision it writes, the
  jfast.toml it edits, and every refusal naming its fix.

The database half, where the switch is applied and a second tenant is shown to
be locked out by PostgreSQL itself, is tests/test_tenancy_enable_pg.py.
"""

from __future__ import annotations

import json as jsonlib
import textwrap
import tomllib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli import check as check_cli
from jfastframework.cli import tenancy as tenancy_cli
from jfastframework.cli.exits import Code
from jfastframework.cli.scaffold import (
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)
from jfastframework.multitenant import consistency
from jfastframework.multitenant.consistency import consistency_findings
from jfastframework.multitenant.readiness import RULES, readiness
from jfastframework.multitenant.switch import (
    MARKER,
    SwitchError,
    edit_config,
    plan_switch,
    render_migration,
)

runner = CliRunner()


def _cli() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None: ...

    check_cli.register(app)
    tenancy_cli.register(app)
    return app


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return root


def _toml(plugins: list[str], extra: str = "") -> str:
    enabled = ", ".join(f'"{p}"' for p in plugins)
    return f'[app]\nname = "svc"\n\n[plugins]\nenabled = [{enabled}]\n\n{textwrap.dedent(extra)}'


MODEL = """
from sqlalchemy.orm import Mapped, mapped_column
from jfastframework.db import Base, TenantMixin, TimestampMixin

class Invoice(Base, TimestampMixin, TenantMixin):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
"""


def _codes(root: Path, **kwargs: object) -> list[str]:
    return [f.code for f in consistency_findings(root, **kwargs)]  # type: ignore[arg-type]


def _ready(root: Path) -> list[str]:
    return [f.code for f in readiness(root).open]


# ---------------------------------------------------------------------------
# The contradictions (`jfast check`, section `tenancy`)
# ---------------------------------------------------------------------------


def test_the_defaults_here_are_the_plugins_defaults() -> None:
    """DEFAULTS mirrors each plugin's Settings; a changed default must fail here."""
    from jfastframework.plugins.builtin.auth import AuthSettings
    from jfastframework.plugins.builtin.database import DatabaseSettings
    from jfastframework.plugins.builtin.llm import LLMSettings
    from jfastframework.plugins.builtin.rag import RagSettings
    from jfastframework.plugins.builtin.tenancy import TenancySettings

    classes = {
        "tenancy": TenancySettings,
        "rag": RagSettings,
        "llm": LLMSettings,
        "database": DatabaseSettings,
        "auth": AuthSettings,
    }
    for plugin, keys in consistency.DEFAULTS.items():
        fields = classes[plugin].model_fields
        for key, value in keys.items():
            assert fields[key].get_default(call_default_factory=True) == value, (plugin, key)


def test_rag_scoped_with_no_tenancy_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path, {"jfast.toml": _toml(["database", "rag"], "[plugin.rag]\nstore = 'pgvector'\n")}
    )
    findings = consistency_findings(tmp_path)
    assert [f.code for f in findings] == ["tenancy-rag-scoped-without-tenancy"]
    assert "tenant_scoped = false" in findings[0].why  # the fix is named
    assert findings[0].line is not None


def test_rag_unscoped_or_tenancy_on_is_consistent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"jfast.toml": _toml(["database", "rag"], "[plugin.rag]\ntenant_scoped = false\n")},
    )
    assert _codes(tmp_path) == []
    _write(
        tmp_path,
        {
            "jfast.toml": _toml(
                ["database", "auth", "tenancy", "rag"],
                '[plugin.tenancy]\nsources = ["token", "user"]\n',
            )
        },
    )
    assert _codes(tmp_path) == []


def test_a_built_plugin_setting_wins_over_the_file(tmp_path: Path) -> None:
    """An environment override is what the service runs with, so it is what is judged."""
    _write(tmp_path, {"jfast.toml": _toml(["database", "rag"])})

    class Built:
        tenant_scoped = False

    assert _codes(tmp_path, built={"rag": Built()}) == []


def test_a_tenant_budget_with_no_tenancy_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, {"jfast.toml": _toml(["llm"], "[plugin.llm]\ntenant_budget_usd = 5\n")})
    assert _codes(tmp_path) == ["tenancy-budget-without-tenancy"]
    _write(tmp_path, {"jfast.toml": _toml(["llm"], "[plugin.llm]\ntenant_budget_usd = 0\n")})
    assert _codes(tmp_path) == []


def test_rls_with_nothing_to_set_the_tenant_is_high(tmp_path: Path) -> None:
    _write(tmp_path, {"jfast.toml": _toml(["database"], "[plugin.database]\nrls = true\n")})
    findings = consistency_findings(tmp_path)
    assert [(f.code, f.severity) for f in findings] == [("tenancy-rls-without-tenancy", "high")]


def test_rls_with_only_the_auth_claim_is_softer(tmp_path: Path) -> None:
    _write(tmp_path, {"jfast.toml": _toml(["database", "auth"], "[plugin.database]\nrls = true\n")})
    findings = consistency_findings(tmp_path)
    assert [(f.code, f.severity) for f in findings] == [("tenancy-rls-without-tenancy", "medium")]


def test_policies_in_revisions_with_rls_off_are_reported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "jfast.toml": _toml(["database", "auth", "tenancy"]),
            "migrations/versions/abc_rls.py": """
                revision = "abc"
                down_revision = None
                from jfastframework.db.rls import enable_tenant_rls
                def upgrade():
                    enable_tenant_rls(op, "invoices")
            """,
        },
    )
    findings = consistency_findings(tmp_path)
    assert "tenancy-policies-without-rls" in [f.code for f in findings]
    policy = next(f for f in findings if f.code == "tenancy-policies-without-rls")
    assert policy.path == "migrations/versions/abc_rls.py" and policy.line == 5


def test_current_tenant_with_no_source_points_at_the_line(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "jfast.toml": _toml(["database"]),
            "modules/invoice/routes.py": """
                from fastapi import Depends
                from jfastframework.plugins.builtin.tenancy import current_tenant

                async def invoices(tenant: str = Depends(current_tenant)) -> None: ...
            """,
        },
    )
    findings = consistency_findings(tmp_path)
    assert [f.code for f in findings] == ["tenancy-current-tenant-without-source"]
    assert findings[0].path == "modules/invoice/routes.py" and findings[0].line == 4
    # The auth claim is a source: the same code is fine with auth on.
    _write(tmp_path, {"jfast.toml": _toml(["database", "auth"])})
    assert _codes(tmp_path) == []


def test_sources_that_can_never_resolve(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"jfast.toml": _toml(["tenancy"], '[plugin.tenancy]\nsources = ["token", "subdomain"]\n')},
    )
    findings = consistency_findings(tmp_path)
    messages = " | ".join(f.message for f in findings)
    assert [f.code for f in findings] == ["tenancy-source-unresolvable"] * 2
    assert "base_domain" in messages and "auth plugin" in messages


def test_the_tenancy_check_is_in_the_battery(tmp_path: Path) -> None:
    root = tmp_path / "shop"
    scaffolder = Scaffolder()
    scaffolder.render_trees(service_trees("api", None, root), service_context("shop"))
    result = runner.invoke(_cli(), ["check", "--path", str(root), "--json", "--only", "tenancy"])
    payload = jsonlib.loads(result.stdout)
    assert payload["checks"][0]["name"] == "tenancy"
    assert payload["checks"][0]["status"] == "pass", payload  # a generated service is consistent

    (root / "jfast.toml").write_text(
        _toml(["observability", "database"], "[plugin.database]\nrls = true\n"), encoding="utf-8"
    )
    result = runner.invoke(_cli(), ["check", "--path", str(root), "--json", "--only", "tenancy"])
    payload = jsonlib.loads(result.stdout)
    assert result.exit_code == Code.VALIDATION, result.stdout
    codes = [f["code"] for f in payload["checks"][0]["findings"]]
    assert codes == ["tenancy-rls-without-tenancy"]


# ---------------------------------------------------------------------------
# The readiness report (`jfast check --multitenant-ready`)
# ---------------------------------------------------------------------------

CLEAN = {
    "jfast.toml": _toml(["database", "auth", "tenancy"]),
    "modules/invoice/models.py": MODEL,
    "modules/invoice/routes.py": """
        from fastapi import APIRouter, Depends
        from jfastframework.plugins.builtin.database import DbSession
        from jfastframework.plugins.builtin.tenancy import current_tenant

        router = APIRouter(prefix="/invoices")

        async def get_service(session: DbSession, tenant: str = Depends(current_tenant)):
            return (session, tenant)

        @router.get("")
        async def list_invoices(service=Depends(get_service)):
            return []

        @router.get("/raw")
        async def raw(session: DbSession, tenant: str = Depends(current_tenant)):
            await session.execute("SELECT * FROM invoices WHERE tenant_id = :t", {"t": tenant})

        @router.get("/health-ish")
        async def no_data():
            return {"ok": True}
    """,
    "modules/invoice/files.py": """
        async def save(storage, cache, tenant: str, invoice_id: int, pdf: bytes) -> None:
            key = f"{tenant}/invoices/{invoice_id}.pdf"
            await storage.put(key, pdf)
            await cache.set(f"{tenant}:invoice:{invoice_id}", 1)

        async def forward(storage, key: str) -> None:
            # A parameter: who built it is not in view, so nothing is said.
            await storage.get(key)
    """,
    "modules/invoice/tests/test_invoice.py": """
        async def test_single_tenant(repo_cls, session):
            repo_cls(session, tenant_id=None)
    """,
}


def test_a_clean_service_has_nothing_to_report(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN)
    report = readiness(tmp_path)
    assert report.open == [], [str(f) for f in report.open]
    assert [t.table for t in report.tables] == ["invoices"]


@pytest.mark.parametrize(
    ("code", "files"),
    [
        (
            "tenant-none-literal",
            {
                "modules/invoice/public.py": """
                    async def get(session, invoice_id):
                        return await InvoiceRepository(session, tenant_id=None).get(invoice_id)
                """
            },
        ),
        (
            "route-without-tenant",
            {
                "modules/invoice/routes.py": """
                    from fastapi import APIRouter, Depends
                    from jfastframework.plugins.builtin.auth import require_auth
                    from jfastframework.plugins.builtin.database import DbSession

                    router = APIRouter()

                    @router.get("/invoices", dependencies=[Depends(require_auth)])
                    async def list_invoices(session: DbSession):
                        return []
                """
            },
        ),
        (
            "factory-without-tenant",
            {
                "modules/invoice/api/deps.py": """
                    from fastapi import Request
                    from jfastframework.plugins.builtin.database import DbSession

                    async def get_service(request: Request, session: DbSession):
                        return getattr(request.state, "tenant_id", None)
                """,
                "modules/invoice/api/routes.py": """
                    from fastapi import APIRouter, Depends
                    from .deps import get_service

                    router = APIRouter()

                    @router.get("/invoices")
                    async def list_invoices(service=Depends(get_service)):
                        return []
                """,
            },
        ),
        (
            "raw-sql-without-tenant",
            {
                "modules/invoice/report.py": """
                    from sqlalchemy import text

                    async def totals(session):
                        return await session.execute(
                            text("SELECT count(*) FROM invoices WHERE is_active")
                        )
                """
            },
        ),
        (
            "storage-key-without-tenant",
            {
                "modules/invoice/files.py": """
                    async def save(storage, invoice_id, pdf):
                        key = f"invoices/{invoice_id}.pdf"
                        await storage.put(key, pdf)
                """
            },
        ),
        (
            "cache-key-without-tenant",
            {
                "modules/invoice/files.py": """
                    async def monthly(cache, month):
                        return await cache.get(f"report:{month}")
                """
            },
        ),
        (
            "rag-unscoped",
            {
                "jfast.toml": _toml(
                    ["database", "auth", "tenancy", "rag"], "[plugin.rag]\ntenant_scoped = false\n"
                )
            },
        ),
        (
            "scheduled-job-without-tenant",
            {
                "modules/invoice/tasks.py": """
                    from datetime import timedelta
                    from jfastframework.queues.base import Job

                    @tasks.task("nightly", every=timedelta(hours=24))
                    async def nightly(payload):
                        await queue.enqueue(Job(task="invoice.remind", payload={}))
                """
            },
        ),
        (
            "llm-call-without-tenant",
            {
                "modules/invoice/advisor.py": """
                    async def summarise(llm, text):
                        return await llm.chat([{"role": "user", "content": text}])
                """
            },
        ),
    ],
)
def test_each_rule_fires_on_its_fixture(tmp_path: Path, code: str, files: dict[str, str]) -> None:
    assert code in RULES
    _write(tmp_path, {**CLEAN, **files})
    report = readiness(tmp_path)
    found = [f for f in report.open if f.code == code]
    assert found, [str(f) for f in report.open]
    assert all(f.path and (f.line or f.path == "jfast.toml") for f in found)
    # Nothing else fires: each fixture trips exactly its own rule.
    assert {f.code for f in report.open} == {code}, [str(f) for f in report.open]


def test_schedule_by_name_reaches_the_handler(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            **CLEAN,
            "modules/invoice/tasks.py": """
                from jfastframework.queues.base import Job

                @tasks.task("purge")
                async def purge(payload):
                    await queue.enqueue(Job(task="invoice.purge_one", payload={}))

                @tasks.task("per_request")
                async def per_request(payload):
                    await queue.enqueue(Job(task="x", payload={}))

                tasks.schedule("purge", cron="@hourly")
            """,
        },
    )
    found = [f for f in readiness(tmp_path).open if f.code == "scheduled-job-without-tenant"]
    assert [f.line for f in found] == [5]  # purge, not per_request


def test_a_waiver_sets_a_finding_aside_and_keeps_it_visible(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            **CLEAN,
            "modules/invoice/fx.py": """
                async def rate(cache):
                    return await cache.get("fx:usd")  # contracts: allow exchange rates are global

                async def other(cache):
                    # contracts: allow warmed for everyone at boot
                    return await cache.get("warm:all")
            """,
        },
    )
    report = readiness(tmp_path)
    assert report.open == []
    assert sorted(item.waived for item in report.waived) == [
        "exchange rates are global",
        "warmed for everyone at boot",
    ]


def test_a_generated_service_reports_its_factories(tmp_path: Path) -> None:
    """The real templates: every module's get_service reads an optional tenant."""
    root = tmp_path / "shop"
    scaffolder = Scaffolder()
    scaffolder.render_trees(service_trees("api", None, root), service_context("shop"))
    for name in ("invoice", "customer"):
        scaffolder.render_trees(
            module_trees("modular", "api", root / "modules", root), module_context(name)
        )
    report = readiness(root)
    assert sorted({t.table for t in report.tables}) == ["customers", "invoices"]
    assert sorted((f.code, f.path) for f in report.open) == [
        ("factory-without-tenant", "modules/customer/api/routes.py"),
        ("factory-without-tenant", "modules/invoice/api/routes.py"),
    ]


def test_the_cli_report_and_its_exit_code(tmp_path: Path) -> None:
    _write(tmp_path, CLEAN)
    result = runner.invoke(_cli(), ["check", "--path", str(tmp_path), "--multitenant-ready"])
    assert result.exit_code == Code.OK, result.stdout
    assert "nothing found" in result.stdout

    _write(
        tmp_path,
        {"modules/invoice/files.py": "async def f(cache):\n    await cache.get('all')\n"},
    )
    result = runner.invoke(
        _cli(), ["check", "--path", str(tmp_path), "--multitenant-ready", "--json"]
    )
    assert result.exit_code == Code.VALIDATION
    payload = jsonlib.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "cache-key-without-tenant"
    assert payload["findings"][0]["path"] == "modules/invoice/files.py"
    assert payload["findings"][0]["line"] == 2
    assert {rule["code"] for rule in payload["rules"]} == set(RULES)


# ---------------------------------------------------------------------------
# The switch, without a database
# ---------------------------------------------------------------------------

REVISION = """
    \"\"\"init\"\"\"
    revision = "base01"
    down_revision = None

    def upgrade():
        pass
"""


def _switchable(tmp_path: Path, plugins: list[str] | None = None, extra: str = "") -> Path:
    return _write(
        tmp_path,
        {
            "jfast.toml": _toml(plugins or ["database", "auth", "rag"], extra),
            "modules/invoice/models.py": MODEL,
            "modules/invoice/other.py": MODEL.replace("Invoice", "Customer").replace(
                "invoices", "customers"
            ),
            "migrations/versions/base01_init.py": REVISION,
        },
    )


def test_the_revision_backfills_before_the_policy(tmp_path: Path) -> None:
    plan = plan_switch(_switchable(tmp_path), tenant="acme", revision="r1")
    assert plan.table_names == ("customers", "invoices")
    assert plan.rag_table == "rag_chunks"
    assert plan.down_revision == "base01"
    source = plan.migration
    assert MARKER in source
    for table in ("customers", "invoices"):
        backfill = source.index(f"UPDATE {table} SET tenant_id")
        policy = source.index(f'enable_tenant_rls(op, "{table}")')
        assert backfill < policy
    rag = source.index("UPDATE rag_chunks")
    assert rag < source.index("rag_chunks ENABLE ROW LEVEL SECURITY")
    assert "to_regclass('rag_chunks')" in source  # guarded: the table may not exist
    assert "alter_column" not in source
    compile(source, "revision.py", "exec")


def test_not_null_is_optional_and_reversible(tmp_path: Path) -> None:
    source = plan_switch(_switchable(tmp_path), tenant="acme", not_null=True).migration
    assert source.count("nullable=False") == 2
    assert source.count("nullable=True") == 2
    assert "Mapped[str]" in source  # the model change it needs is named


def test_the_config_is_edited_in_place(tmp_path: Path) -> None:
    root = _switchable(
        tmp_path,
        extra=(
            "[plugin.database]\n# keep me\npool_size = 5\n\n[plugin.rag]\ntenant_scoped = false\n"
        ),
    )
    plan = plan_switch(root, tenant="acme")
    after = tomllib.loads(plan.config_after)
    assert after["plugins"]["enabled"] == ["database", "auth", "tenancy", "rag"]
    assert after["plugin"]["tenancy"]["sources"] == ["token", "user"]
    assert after["plugin"]["database"] == {"pool_size": 5, "rls": True}
    assert after["plugin"]["rag"]["tenant_scoped"] is True
    assert "# keep me" in plan.config_after


def test_edit_config_keeps_existing_sources_and_multiline_lists() -> None:
    text = (
        '[plugins]\nenabled = [\n  "database",\n  "auth",\n]  # comment\n\n'
        '[plugin.tenancy]\nsources = ["token"]\n'
    )
    after = tomllib.loads(edit_config(text, sources=None, rag=False))
    assert after["plugins"]["enabled"] == ["database", "auth", "tenancy"]
    assert after["plugin"]["tenancy"]["sources"] == ["token"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _switchable(tmp_path)
    before = (root / "jfast.toml").read_text(encoding="utf-8")
    result = runner.invoke(
        _cli(), ["tenancy", "enable", "--tenant", "acme", "--path", str(root), "--dry-run"]
    )
    assert result.exit_code == 0, result.stdout
    assert "UPDATE invoices SET tenant_id" in result.stdout
    assert "+rls = true" in result.stdout
    assert "CREATE ROLE app" in result.stdout
    assert (root / "jfast.toml").read_text(encoding="utf-8") == before
    assert sorted(p.name for p in (root / "migrations/versions").glob("*.py")) == ["base01_init.py"]


def test_enable_writes_both_and_refuses_a_second_time(tmp_path: Path) -> None:
    root = _switchable(tmp_path)
    result = runner.invoke(
        _cli(), ["tenancy", "enable", "--tenant", "acme", "--path", str(root), "--json"]
    )
    assert result.exit_code == 0, result.stdout
    payload = jsonlib.loads(result.stdout)
    written = root / payload["migration_path"]
    assert written.read_text(encoding="utf-8") == payload["migration"]
    assert "tenancy" in tomllib.loads((root / "jfast.toml").read_text())["plugins"]["enabled"]

    again = runner.invoke(_cli(), ["tenancy", "enable", "--tenant", "acme", "--path", str(root)])
    assert again.exit_code == Code.USAGE
    assert "switched before" in again.output


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda root: (root / "migrations/versions/base01_init.py").unlink(), "no revision"),
        (
            lambda root: (root / "migrations/versions/base02.py").write_text(
                'revision = "base02"\ndown_revision = None\n'
            ),
            "2 heads",
        ),
        (
            lambda root: (root / "jfast.toml").write_text(_toml(["database", "rag"])),
            "auth plugin",
        ),
        (lambda root: (root / "jfast.toml").write_text(_toml(["auth"])), "no database plugin"),
    ],
)
def test_refusals_name_the_fix(tmp_path: Path, setup: object, message: str) -> None:
    root = _switchable(tmp_path)
    setup(root)  # type: ignore[operator]
    with pytest.raises(SwitchError, match=message):
        plan_switch(root, tenant="acme")


def test_a_merge_revision_is_one_head(tmp_path: Path) -> None:
    root = _switchable(tmp_path)
    _write(
        root,
        {
            "migrations/versions/b2.py": 'revision = "b2"\ndown_revision = "base01"\n',
            "migrations/versions/b3.py": 'revision = "b3"\ndown_revision = "base01"\n',
            "migrations/versions/m4.py": 'revision = "m4"\ndown_revision = ("b2", "b3")\n',
        },
    )
    assert plan_switch(root, tenant="acme").down_revision == "m4"


@pytest.mark.parametrize("tenant", ["", "a'b", "has space", "-lead", "x" * 200])
def test_the_tenant_cannot_carry_sql(tenant: str) -> None:
    with pytest.raises(SwitchError):
        render_migration(
            tenant=tenant,
            tables=["t"],
            rag_table=None,
            not_null=False,
            revision="r",
            down_revision=None,
        )


def test_subdomain_needs_a_base_domain(tmp_path: Path) -> None:
    root = _switchable(tmp_path)
    with pytest.raises(SwitchError, match="base-domain"):
        plan_switch(root, tenant="acme", sources=["subdomain"])
    plan = plan_switch(root, tenant="acme", sources=["subdomain"], base_domain="app.example.com")
    assert tomllib.loads(plan.config_after)["plugin"]["tenancy"]["base_domain"] == "app.example.com"


def test_a_scheduled_module_task_with_a_task_session(tmp_path: Path) -> None:
    """`@task(..., every=...)` from jfastframework.tasks: its TaskSession has no tenant."""
    _write(
        tmp_path,
        {
            **CLEAN,
            "modules/invoice/tasks.py": """
                from datetime import timedelta
                from jfastframework.events import Event
                from jfastframework.tasks import TaskSession, task

                @task("invoice.remind", every=timedelta(hours=1))
                async def remind(payload: dict, session: TaskSession) -> None:
                    await publish(session, Event(type="invoice.reminded"))

                @task("invoice.one")
                async def one(payload: dict, session: TaskSession) -> None: ...
            """,
        },
    )
    found = [f for f in readiness(tmp_path).open if f.code == "scheduled-job-without-tenant"]
    assert sorted(f.line for f in found) == [6, 7]  # the session, and the Event; not `one`
