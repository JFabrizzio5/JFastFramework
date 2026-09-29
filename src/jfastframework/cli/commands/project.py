"""`jfast inspect`, `jfast analyze` and `jfast graph`: reading a project back.

`describe` answers "what is this service" by building the app. That answer is
the true one and it is unavailable in the two moments you most want it: when
a dependency is not installed, and when the code does not import. It also
says nothing whatsoever about modules -- generate two and neither name
appears in its output.

These three read the filesystem instead. Slightly less authoritative, always
available, and they know what a module is.
"""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework import project as project_model
from jfastframework.cli import insight
from jfastframework.cli.common import _declared_paths, _echo, _load
from jfastframework.cli.exits import Code
from jfastframework.settings import DEFAULT_CONFIG_FILE


def _project(path: Path) -> project_model.Project:
    root = path.resolve()
    if not (root / DEFAULT_CONFIG_FILE).is_file():
        typer.echo(
            f"no {DEFAULT_CONFIG_FILE} in {root}\n"
            "Run this inside a service, or point at one with --path."
        )
        raise typer.Exit(Code.CONFIG)
    return project_model.load(root)


def _known_plugins(root: Path) -> frozenset[str]:
    """Every plugin name this installation can resolve, importable or not.

    A name that is installed but broken is `doctor`'s finding, not `analyze`'s.
    Only a name nothing provides at all is reported here -- which is why the
    project's own ``[plugins.paths]`` have to be discovered too: a plugin
    declared three lines above the allow-list that reads it is not missing.
    """
    from jfastframework.plugins import registry

    config_path = root / DEFAULT_CONFIG_FILE
    extra = _declared_paths(_load(str(config_path))) if config_path.is_file() else {}
    available = registry.discover(extra_paths=extra, search_path=root)
    broken: dict[str, str] = getattr(registry.discover, "broken", {})
    # A dotted path that does not import is a name nothing provides, however
    # confidently jfast.toml names it. Only an installed distribution earns the
    # "broken, not missing" reading, so the declarations are dropped here.
    installed_but_broken = frozenset(name for name in broken if name not in extra)
    return frozenset(available) | installed_but_broken


def inspect_project(
    resource: str | None = typer.Argument(
        None, help="`module <name>` for one module. Omit for the whole project."
    ),
    name: str | None = typer.Argument(None, help="Which module."),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """What is in this project, read from disk without importing it.

    The one command to run first in an unfamiliar service, and the one an agent
    should run before it edits anything: modules, how each is shaped, what it
    serves, whether it is actually wired into the app.
    """
    project = _project(path)

    if resource is None:
        findings = project_model.analyze(project)
        payload = {**project.describe(), "findings": [f.describe() for f in findings]}
        _echo(payload, json_out, insight.render_project(project, findings))
        return

    if resource != "module":
        typer.echo("inspect takes `module <name>`, or no argument at all.")
        raise typer.Exit(Code.USAGE)
    if not name:
        typer.echo(f"which module? {', '.join(project.module_names) or 'there are none yet'}")
        raise typer.Exit(Code.USAGE)

    module = project.module(name)
    if module is None:
        typer.echo(f"no module {name!r}. Found: {', '.join(project.module_names) or 'none'}")
        raise typer.Exit(Code.USAGE)
    _echo(module.describe(), json_out, insight.render_module(module))


def analyze_project(
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    fail_on: str = typer.Option(
        "high",
        "--fail-on",
        help="Exit non-zero at this severity or worse: critical, high, medium, low, never.",
    ),
) -> None:
    """What is structurally wrong with this project.

    Complementary to `contracts check`, not a replacement: the contract enforces
    the rules you declared *inside* a file, this reports on the shape of the
    project *between* files -- import cycles, a module main.py never registers,
    two routers claiming one prefix, shared/ importing a module.

    Every check is decidable from the source text. Nothing here guesses, on
    purpose: a checker that is right nine times in ten gets muted after the
    second false positive, and the true findings go with it.
    """
    levels = (*project_model.SEVERITY_ORDER, "never")
    if fail_on not in levels:
        raise typer.BadParameter(f"choose from: {', '.join(levels)}", param_hint="--fail-on")

    project = _project(path)
    findings = project_model.analyze(project, known_plugins=_known_plugins(project.root))
    payload = {
        "schema_version": "1",
        "project": project.name,
        "ok": not findings,
        "counts": insight.severity_counts(findings),
        "findings": [finding.describe() for finding in findings],
    }
    _echo(payload, json_out, insight.render_analysis(findings))

    if fail_on == "never":
        return
    threshold = project_model.SEVERITY_ORDER.index(fail_on)
    if any(project_model.SEVERITY_ORDER.index(f.severity) <= threshold for f in findings):
        raise typer.Exit(Code.VALIDATION)


def module_graph(
    module: str | None = typer.Option(None, "--module", "-m", help="Only this module's edges."),
    output_format: str = typer.Option(
        "ascii", "--format", "-f", help=", ".join(insight.GRAPH_FORMATS)
    ),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
) -> None:
    """The module dependency graph.

    `jfast workspace graph` draws services. This draws the modules inside one,
    which is the graph that decides whether a module can ever be extracted:
    a module nothing imports is a service waiting to happen, and a cycle is two
    modules that will never be either.
    """
    if output_format not in insight.GRAPH_FORMATS:
        raise typer.BadParameter(
            f"choose from: {', '.join(insight.GRAPH_FORMATS)}", param_hint="--format"
        )
    project = _project(path)
    if module and project.module(module) is None:
        typer.echo(f"no module {module!r}. Found: {', '.join(project.module_names) or 'none'}")
        raise typer.Exit(Code.USAGE)
    typer.echo(insight.render_graph(project, output_format=output_format, root=module))


def register(app: typer.Typer) -> None:
    """Attach `inspect`, `analyze` and `graph` to *app*."""
    app.command("inspect")(inspect_project)
    app.command("analyze")(analyze_project)
    app.command("graph")(module_graph)
