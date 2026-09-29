"""`jfast contracts ...`: declare the rules, enforce them, publish them.

`explain` and `diff` join this group from :mod:`jfastframework.cli.explain`,
which looks for it by name -- so it has to be registered first.
"""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework.cli.common import _echo, _report
from jfastframework.cli.exits import Code
from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    DEFAULT_LAYOUT,
    MODULE_LAYOUTS,
    Scaffolder,
    to_snake,
)
from jfastframework.contracts import CONTRACTS_FILE, Contract, check, render, waivers

contracts_app = typer.Typer(
    help="Per-project contracts: declare the rules, enforce them, publish them.",
    no_args_is_help=True,
)


def _coverage_table(coverage: dict[str, int]) -> str:
    """How many files each layer governs, in the order the contract declares.

    Sorted by name would put the answer somewhere different in every project.
    Declaration order is the order the contract was written to be read in, and
    for the layered and hexagonal templates it is also outermost-inward.
    """
    if not coverage:
        return ""
    width = max(len(name) for name in coverage)
    rows = [
        f"  {name:<{width}}  {count:>4} file{'' if count == 1 else 's'}"
        + ("   governs nothing" if count == 0 else "")
        for name, count in coverage.items()
    ]
    return "layers\n" + "\n".join(rows) + "\n\n"


def _require_contract(path: Path | None) -> tuple[Contract, Path]:
    source = path or Contract.find()
    if source is None or not source.is_file():
        typer.echo(
            f"No {CONTRACTS_FILE} found here or above.\nCreate one with:\n    jfast contracts init",
            err=True,
        )
        raise typer.Exit(1)
    return Contract.load(source), source.parent


@contracts_app.command("init")
def contracts_init(
    name: str | None = typer.Argument(None, help="Project name. Defaults to the directory."),
    layout: str = typer.Option(
        DEFAULT_LAYOUT,
        "--layout",
        "-l",
        help=f"Defaults matching your modules: {', '.join(MODULE_LAYOUTS)}.",
    ),
    target: Path = typer.Option(Path("."), "--target", "-t"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing contracts.toml."),
) -> None:
    """Write a contracts.toml with defaults for your layout.

    The defaults are a floor, not the answer. The value is in the lines you
    add: what this service does *not* own, which interfaces are stable, which
    invariants a checker cannot see.
    """
    if layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")

    if (target / CONTRACTS_FILE).exists() and not force:
        typer.echo(
            f"{target / CONTRACTS_FILE} already exists. Re-run with --force to replace it.",
            err=True,
        )
        raise typer.Exit(1)

    project = to_snake(name or target.resolve().name)
    written = Scaffolder().render_tree(
        CONTRACT_TEMPLATE_FOR[layout],
        target,
        {"project": project, "layout": layout, "Project": project.replace("_", " ").title()},
        force=force,
    )
    _report(written)
    typer.echo(
        f"\nContract for '{project}' written ({layout} layout).\n"
        f"\nEdit it -- the defaults are a floor, not the point. Then:\n"
        f"    jfast contracts check\n"
        f"    jfast contracts render      # CONTRACTS.md, for review and for agents"
    )


@contracts_app.command("check")
def contracts_check(
    path: Path | None = typer.Option(None, "--file", "-f", help="Path to contracts.toml."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify the code against its contract. Non-zero exit on a violation.

    The per-layer file counts print on every run, pass or fail. A layer at 0 is
    the one finding this command reports late -- `check_coverage` only raises it
    once files it should have claimed are also unclaimed -- and a layer that
    drops from 40 files to 3 after a refactor never raises it at all.
    """
    from jfastframework.contracts.checker import layer_matches

    contract, root = _require_contract(path)
    violations = check(contract, root)
    coverage = layer_matches(contract, root)

    payload = {
        "project": contract.project,
        "ok": not violations,
        "coverage": coverage,
        "violations": [
            {"path": v.path, "line": v.line, "rule": v.rule, "message": v.message, "why": v.why}
            for v in violations
        ],
    }
    human = _coverage_table(coverage)
    human += "\n".join(str(v) for v in violations) or f"OK  {contract.project}: no violations"
    if violations:
        human += f"\n\n{len(violations)} violation(s). Fix them, or waive one inline with"
        human += "\n    # contracts: allow <reason>"
    _echo(payload, json_out, human)

    if violations:
        raise typer.Exit(Code.CONTRACT)


@contracts_app.command("show")
def contracts_show(
    path: Path | None = typer.Option(None, "--file", "-f"),
    json_out: bool = typer.Option(True, "--json/--text"),
) -> None:
    """The contract itself.

    `--json` is what an agent should read before writing a line here: scope,
    layer boundaries, forbidden calls, interfaces and invariants.
    """
    contract, _ = _require_contract(path)
    human = "\n".join(
        [
            f"project      : {contract.project}",
            f"owns         : {contract.owns or '-'}",
            f"does not own : {contract.does_not_own or '-'}",
            f"layers       : {', '.join(contract.layers) or '-'}",
            f"provides     : {', '.join(i.name for i in contract.provides) or '-'}",
            f"consumes     : {', '.join(i.name for i in contract.consumes) or '-'}",
            "depends_on   : "
            + (
                "; ".join(
                    f"{name} -> {', '.join(deps) or 'nothing'}"
                    for name, deps in sorted(contract.module_deps.items())
                )
                or "-"
            ),
            f"invariants   : {len(contract.invariants)}",
        ]
    )
    _echo(contract.describe(), json_out, human)


@contracts_app.command("render")
def contracts_render(
    path: Path | None = typer.Option(None, "--file", "-f"),
    output: Path = typer.Option(Path("CONTRACTS.md"), "--output", "-o"),
    stdout: bool = typer.Option(False, "--stdout"),
) -> None:
    """Write CONTRACTS.md from contracts.toml."""
    contract, _ = _require_contract(path)
    rendered = render(contract)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@contracts_app.command("waivers")
def contracts_waivers(
    path: Path | None = typer.Option(None, "--file", "-f"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List every inline waiver.

    A waiver is a decision. Decisions nobody revisits are how a contract stops
    meaning anything, so they are listed rather than hidden.
    """
    _, root = _require_contract(path)
    found = waivers(root)
    payload = [{"path": w.path, "line": w.line, "reason": w.message} for w in found]
    human = "\n".join(f"{w.path}:{w.line}: {w.message}" for w in found) or "no waivers"
    _echo(payload, json_out, human)


def register(app: typer.Typer) -> None:
    """Add the `contracts` group to *app*."""
    app.add_typer(contracts_app, name="contracts")
