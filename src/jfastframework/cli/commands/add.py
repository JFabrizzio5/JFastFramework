"""`jfast add`: a capability, with the packages, the extra and the advice."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from jfastframework import capabilities
from jfastframework.cli import ui
from jfastframework.cli.generate import _add_extra
from jfastframework.workspace import Workspace


def add_capability(
    capability: str | None = typer.Argument(None, help="What to add. Omit to see the catalogue."),
    service: str | None = typer.Option(
        None, "--service", "-s", help="Which service. Asked when a workspace has several."
    ),
    pandas: bool = typer.Option(
        False, "--pandas", help="dataframes only: install pandas instead of polars."
    ),
    install: bool = typer.Option(
        True, "--install/--no-install", help="Run pip after editing requirements."
    ),
) -> None:
    """Add a capability to a service: the packages, the extra, and the advice.

    Not a nicer `pip install`. Each entry carries the decision somebody would
    otherwise make badly -- which of two libraries and why, the packaging trap
    that makes the obvious wheel fail inside a container, and what has to
    happen besides installing.
    """
    if capability is None:
        _list_capabilities()
        return

    try:
        spec = capabilities.get(capability)
    except KeyError as exc:
        ui.console.print(f"  [{ui.ACCENT}]{exc}[/]")
        raise typer.Exit(1) from exc

    target = _resolve_service_dir(service)
    requirements = target / "requirements.txt"
    if not requirements.is_file():
        ui.console.print(
            f"  [{ui.ACCENT}]No requirements.txt in {target}.[/]\n"
            f"  Run this inside a generated service, or pass --service."
        )
        raise typer.Exit(1)

    extra = "pandas" if (spec.name == "dataframes" and pandas) else spec.extra
    packages = ("pandas>=2.2",) if extra == "pandas" else spec.packages

    body = requirements.read_text(encoding="utf-8")
    if f"[{extra}" in body or f",{extra}]" in body or f",{extra}," in body:
        ui.note(f"{target.name} already has {extra}.")
        raise typer.Exit(0)

    updated = _add_extra(body, extra)
    requirements.write_text(updated, encoding="utf-8")

    ui.summary(
        f"{spec.name} {ui.G.arrow} {target.name}",
        [
            ("packages", ", ".join(packages)),
            ("extra", extra),
            ("why", spec.rationale or ui.G.dash),
        ],
    )
    ui.created(str(requirements), f"now pins [{extra}]")

    if spec.system_packages:
        ui.warn("This needs system packages in the image:")
        ui.note("apt-get install -y " + " ".join(spec.system_packages))

    if spec.plugin:
        ui.note(f'Enable the plugin: add "{spec.plugin}" to [plugins].enabled in jfast.toml')

    if spec.after:
        ui.note(spec.after)

    if install:
        import subprocess

        ui.step(f"pip install -r {requirements}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
            check=False,
        )
        if result.returncode != 0:
            ui.warn("pip failed. requirements.txt is updated; install it yourself.")
            raise typer.Exit(1)
        ui.console.print(f"  [{ui.OK}]installed[/]")


def _list_capabilities() -> None:
    from rich.table import Table

    table = Table(box=None, padding=(0, 2))
    table.add_column("", style=ui.ACCENT, no_wrap=True)
    table.add_column("", style="white")
    table.add_column("", style=ui.DIM)

    for name in capabilities.names():
        spec = capabilities.CATALOG[name]
        note = "heavy" if spec.heavy else ""
        table.add_row(name, spec.summary, note)

    ui.console.print()
    ui.console.print(table)
    ui.console.print()
    ui.note("jfast add <name>          adds it to this service")
    ui.note("jfast add <name> -s api   adds it to one service in a workspace")


def _resolve_service_dir(service: str | None) -> Path:
    """Which service this applies to.

    In a single service, the current directory. In a workspace with several,
    ask -- because adding a heavy dependency to the wrong one is invisible
    until the image is built.
    """
    if service is not None:
        workspace = Workspace.load_or_none()
        if workspace is not None:
            entry = workspace.get(service)
            if entry is None:
                known = ", ".join(s.name for s in workspace.services) or "none"
                raise typer.BadParameter(f"no service {service!r}. Known: {known}")
            return Path(entry.path).resolve()
        return Path(service).resolve()

    if (Path.cwd() / "requirements.txt").is_file():
        return Path.cwd()

    workspace = Workspace.load_or_none()
    if workspace is None:
        return Path.cwd()

    backends = [s for s in workspace.services if not s.is_frontend]
    if not backends:
        return Path.cwd()
    if len(backends) == 1:
        return Path(backends[0].path).resolve()

    chosen = ui.select(
        "Which service?",
        [ui.Choice(s.name, "", f"{s.kind} :{s.port}") for s in backends],
        default=backends[0].name,
    )
    entry = workspace.get(chosen)
    assert entry is not None
    return Path(entry.path).resolve()


def register(app: typer.Typer) -> None:
    """Attach `add` to *app*."""
    app.command("add")(add_capability)
