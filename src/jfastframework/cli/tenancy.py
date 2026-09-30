"""`jfast tenancy enable` and `jfast check --multitenant-ready`.

Two halves of one move -- from a service that serves one customer to one that
serves several -- and they share their report on purpose: the readiness check
is what `enable` prints as its list of remaining manual steps, so the thing a
person reads before the switch and the thing they work through after it are
the same list.

Why the report lives on `jfast check` and not here
    It is static and read-only, like every check: it parses files and answers
    in milliseconds. But it replaces the battery rather than joining it,
    because its findings are about a hypothetical. A single-tenant service is
    right to build a repository with ``tenant_id=None``; putting that in the
    default battery would fail a correct service forever. So the flag switches
    `check` into the one report, with its own exit code, and ``jfast check``
    without it is unchanged. ``tenancy`` -- the contradictions that are wrong
    *today* -- is in the battery.
"""

from __future__ import annotations

import difflib
import json as jsonlib
import textwrap
from pathlib import Path
from typing import Annotated, Any

import typer

from jfastframework.cli import insight, ui
from jfastframework.cli.exits import MEANING, Code
from jfastframework.multitenant.readiness import RULES, Readiness, readiness
from jfastframework.multitenant.switch import SwitchError, SwitchPlan, plan_switch

__all__ = ["readiness_payload", "register", "render_readiness", "run_readiness"]

SCHEMA_VERSION = "1"

#: The role the policies bind, spelled out, because the generated compose file
#: connects as the database superuser and a superuser ignores every policy.
ROLE_SQL = """\
CREATE ROLE app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app;"""


# ---------------------------------------------------------------------------
# The readiness report
# ---------------------------------------------------------------------------


def readiness_payload(report: Readiness, *, project: str) -> dict[str, Any]:
    """The report as data. ``open`` decides the exit code; ``waived`` is for review."""
    open_findings = report.open
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "ok": not open_findings,
        "exit_code": int(Code.VALIDATION if open_findings else Code.OK),
        "files_read": report.files_read,
        "tenant_tables": [
            {"table": t.table, "model": t.model, "path": t.path, "line": t.line}
            for t in report.tables
        ],
        "counts": insight.severity_counts(open_findings),
        "findings": [item.describe() for item in report.findings if item.waived is None],
        "waived": [item.describe() for item in report.waived],
        "rules": [
            {"code": code, "severity": severity, "looks_for": what}
            for code, (severity, what) in RULES.items()
        ],
    }


def _wrap(text: str, indent: int) -> list[str]:
    pad = " " * indent
    return textwrap.wrap(text, width=78 - indent, initial_indent=pad, subsequent_indent=pad)


def render_readiness(report: Readiness, *, project: str) -> str:
    """One screen, the shape of `jfast upgrade --check`: where, what, and the fix."""
    G = ui.G
    tables = ", ".join(t.table for t in report.tables) or "none found"
    lines = [
        f"  {project}: what a switch to multitenant would break",
        f"  tenant tables: {tables}",
        "",
    ]
    open_findings = report.open
    if not open_findings:
        lines.append(f"  {G.tick} nothing found in {report.files_read} files")
    for finding in open_findings:
        where = finding.path or ""
        if where and finding.line:
            where = f"{where}:{finding.line}"
        lines.append(f"  {G.cross} {where}  {finding.code}  [{finding.severity}]")
        lines.extend(_wrap(finding.message, 6))
        lines.append(f"      {G.arrow} fix")
        lines.extend(_wrap(finding.why, 8))
        lines.append("")
    if report.waived:
        lines.append(f"  waived ({len(report.waived)}):")
        for item in report.waived:
            finding = item.finding
            lines.append(
                f"    {G.bullet} {finding.path}:{finding.line}  {finding.code}  -- {item.waived}"
            )
        lines.append("")
    counts = insight.severity_counts(open_findings)
    tally = "  ".join(f"{severity} {count}" for severity, count in counts.items())
    lines.append(f"  {len(open_findings)} to fix{'  ' + tally if tally else ''}")
    lines.append(
        "  heuristics: a rule that cannot decide stays quiet. Waive a deliberate one with "
        "`# contracts: allow <reason>`."
    )
    return "\n".join(lines)


def run_readiness(path: Path, *, json_out: bool) -> None:
    """`jfast check --multitenant-ready`. Exits 1 when anything is left to fix."""
    root = path.resolve()
    if not (root / "jfast.toml").is_file():
        typer.echo(
            f"no jfast.toml in {root}\nRun this inside a service, or point at one with --path.",
            err=True,
        )
        raise typer.Exit(Code.CONFIG)
    report = readiness(root)
    if json_out:
        typer.echo(jsonlib.dumps(readiness_payload(report, project=root.name), indent=2))
    else:
        typer.echo(render_readiness(report, project=root.name))
    if report.open:
        raise typer.Exit(Code.VALIDATION)


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


def _diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="jfast.toml",
            tofile="jfast.toml",
        )
    )


def _manual_steps(plan: SwitchPlan, report: Readiness) -> list[str]:
    """What the command cannot do, in the order it has to be done."""
    steps = [
        f"Review {plan.migration_path.name}, then apply it as the tables' owner: "
        "`alembic upgrade head`.",
        "Run the service as a role the policies bind -- not a superuser, not BYPASSRLS. "
        "The generated compose file connects as the superuser, so create one:",
    ]
    steps.append("ROLE_SQL")
    steps.append(
        f"Existing rows now belong to tenant {plan.tenant!r}. A request sees them only when "
        f"it resolves to {plan.tenant!r}: a token whose tenant_id claim says so, or -- with "
        f"the `user` source -- the user whose id it is. If this app was one person's, their "
        "user id is the right --tenant."
    )
    if plan.not_null:
        steps.append(
            "Declare `tenant_id: Mapped[str] = mapped_column(index=True)` on each model, or "
            "the next autogenerate makes the column nullable again."
        )
    if plan.rag_table:
        steps.append(
            f"Pass `tenant_id=` to every rag call: [plugin.rag] tenant_scoped is now true. If "
            f"{plan.rag_table} did not exist yet, the rag plugin creates it at startup without "
            "a policy -- run `enable_tenant_rls(op, ...)` for it in a later revision."
        )
    if report.open:
        steps.append(
            f"Fix what the readiness report found ({len(report.open)} below): routes to "
            "`current_tenant`, keys and SQL to carry the tenant. `jfast check "
            "--multitenant-ready` re-runs it."
        )
    return steps


def _enable(
    tenant: Annotated[
        str,
        typer.Option(
            "--tenant",
            "-t",
            help="Who owns every existing row: the customer served until now.",
        ),
    ],
    path: Annotated[Path, typer.Option("--path", "-p", help="Service root.")] = Path("."),
    not_null: Annotated[
        bool,
        typer.Option("--not-null", help="Also make tenant_id NOT NULL on every tenant table."),
    ] = False,
    sources: Annotated[
        str | None,
        typer.Option(
            "--sources",
            help="Comma-separated tenancy sources, most trusted first. Default: token,user.",
        ),
    ] = None,
    base_domain: Annotated[
        str,
        typer.Option("--base-domain", help='For the "subdomain" source: app.example.com.'),
    ] = "",
    table: Annotated[
        list[str] | None,
        typer.Option("--table", help="A tenant table no model declares. Repeatable."),
    ] = None,
    rag_table: Annotated[
        str | None,
        typer.Option("--rag-table", help="The RAG chunks table, if not [plugin.rag] collection."),
    ] = None,
    no_rag: Annotated[
        bool, typer.Option("--no-rag", help="Leave the RAG chunks table alone.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print everything; write nothing.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Switch this service to multitenant: a migration, the config, and what is left.

    Writes one Alembic revision that backfills every NULL tenant_id with
    --tenant, turns row-level security on for every table whose model carries
    TenantMixin, and re-keys the RAG chunks if the table exists. Updates
    jfast.toml: the tenancy plugin on (sources token,user unless --sources),
    [plugin.database] rls = true, [plugin.rag] tenant_scoped = true. Applies
    nothing: `alembic upgrade head` is still the step that changes data.

    Then prints what it cannot do -- the database role the policies need, and
    the readiness report of routes, SQL and keys still assuming one customer.

        jfast tenancy enable --tenant acme --dry-run
        jfast tenancy enable --tenant acme --not-null
        jfast tenancy enable --tenant acme --sources subdomain --base-domain app.example.com
    """
    root = path.resolve()
    chosen = [s.strip() for s in sources.split(",") if s.strip()] if sources is not None else None
    try:
        plan = plan_switch(
            root,
            tenant=tenant,
            not_null=not_null,
            sources=chosen,
            base_domain=base_domain,
            rag_table=rag_table,
            skip_rag=no_rag,
            extra_tables=list(table or []),
        )
    except SwitchError as exc:
        typer.echo(f"jfast tenancy enable: {exc}", err=True)
        code = Code.CONFIG if "jfast.toml" in str(exc) else Code.USAGE
        raise typer.Exit(code) from exc
    except ValueError as exc:  # an identifier safe_identifier refused
        typer.echo(f"jfast tenancy enable: {exc}", err=True)
        raise typer.Exit(Code.USAGE) from exc

    # Read after the plan and against the *new* configuration: the report is
    # about what is still wrong once the switch is made, and `rag-unscoped` is
    # one of the things the switch fixes.
    import tomllib

    report = readiness(root, config=tomllib.loads(plan.config_after))
    steps = _manual_steps(plan, report)
    relative = plan.migration_path.relative_to(root).as_posix()

    if not dry_run:
        plan.migration_path.write_text(plan.migration, encoding="utf-8")
        (root / "jfast.toml").write_text(plan.config_after, encoding="utf-8")

    if json_out:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "dry_run": dry_run,
            "tenant": plan.tenant,
            "tables": list(plan.table_names),
            "rag_table": plan.rag_table,
            "not_null": plan.not_null,
            "sources": list(plan.sources),
            "revision": plan.revision,
            "down_revision": plan.down_revision,
            "migration_path": relative,
            "migration": plan.migration,
            "config_diff": _diff(plan.config_before, plan.config_after),
            "manual_steps": [ROLE_SQL if step == "ROLE_SQL" else step for step in steps],
            "readiness": readiness_payload(report, project=root.name),
        }
        typer.echo(jsonlib.dumps(payload, indent=2))
        return

    G = ui.G
    verb = "would write" if dry_run else "wrote"
    lines = [
        f"  {root.name}: switch to multitenant, existing rows to {plan.tenant!r}",
        "",
        f"  tables    {', '.join(plan.table_names) or 'none'}",
        f"  rag       {plan.rag_table or 'none'}",
        f"  sources   {', '.join(plan.sources)}",
        "",
        f"  {G.tick} {verb} {relative}",
        f"  {G.tick} {verb} jfast.toml",
    ]
    if dry_run:
        lines.append("")
        lines.append(f"  --- {relative}")
        lines.extend(f"  {line}" for line in plan.migration.splitlines())
        lines.append("")
        diff = _diff(plan.config_before, plan.config_after)
        lines.extend(f"  {line}" for line in diff.split("\n"))
    lines.append("")
    lines.append("  what is left, in order:")
    for number, step in enumerate(steps, start=1):
        if step == "ROLE_SQL":
            lines.extend(f"        {line}" for line in ROLE_SQL.splitlines())
            continue
        wrapped = _wrap(step, 7)
        wrapped[0] = f"  {number:>2}. " + wrapped[0].lstrip()
        lines.extend(wrapped)
    typer.echo("\n".join(lines))
    typer.echo("")
    typer.echo(render_readiness(report, project=root.name))
    if dry_run:
        typer.echo(f"\n  dry run: nothing written ({MEANING[int(Code.OK)]}).")


def register(app: typer.Typer) -> None:
    """Attach the `tenancy` group to *app*."""
    group = typer.Typer(
        name="tenancy",
        help="Move a service from one customer to several.",
        no_args_is_help=True,
    )
    group.command("enable")(_enable)
    app.add_typer(group, name="tenancy")
