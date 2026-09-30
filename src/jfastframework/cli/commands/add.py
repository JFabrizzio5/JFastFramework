"""`jfast add` and `jfast remove`: plugins and capabilities, with the advice.

A plugin (`jfast add telemetry`) is a name in ``[plugins].enabled`` plus the
extra that installs its dependencies. A capability (`jfast add dataframes`) is
packages only. One command for both, because which one a name is is the
framework's business, not the user's.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from jfastframework import capabilities
from jfastframework.cli import ui
from jfastframework.cli.generate import _add_extra
from jfastframework.cli.scaffold import BASE_PLUGINS, PLUGIN_CATALOG, plugin_importable
from jfastframework.workspace import Workspace


def add_capability(
    capability: str | None = typer.Argument(
        None, help="A plugin (telemetry, queue, ...) or a capability. Omit to see the catalogue."
    ),
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

    if capability in PLUGIN_CATALOG:
        add_plugin(capability, _resolve_service_dir(service), install=install)
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
        _pip_install(requirements)


def _list_capabilities() -> None:
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
    ui.console.print(f"  [{ui.DIM}]plugins[/]")
    plugins = Table(box=None, padding=(0, 2))
    plugins.add_column("", style=ui.ACCENT, no_wrap=True)
    plugins.add_column("", style="white")
    plugins.add_column("", style=ui.DIM)
    for name, plugin in PLUGIN_CATALOG.items():
        if name not in BASE_PLUGINS:
            plugins.add_row(name, plugin.label, "recommended" if plugin.recommended else "")
    ui.console.print(plugins)
    ui.console.print()
    ui.note("jfast add <name>          adds it to this service")
    ui.note("jfast remove <plugin>     takes a plugin out again")
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


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

# The [plugins] table: its header and every line up to the next header. Not
# `[^\[]*` -- the enabled list itself opens with a bracket.
_PLUGINS_TABLE = re.compile(r"^\[plugins\][ \t]*\n(?:(?!\[)[^\n]*\n?)*", re.MULTILINE)
_ENABLED = re.compile(r"^(?P<indent>[ \t]*)enabled\s*=\s*\[(?P<names>[^\]]*)\]", re.MULTILINE)

#: Only the ones whose next step is not "set the variables it reads".
_PLUGIN_NOTES: dict[str, str] = {
    "database": "Then `alembic upgrade head`; module tables come from your migrations.",
    "accounts": "Its tables (users, roles, sessions) are created at startup, not by a migration.",
    "outbox": "Its table is created at startup. Queue work with outbox.enqueue(session, Job(...)).",
    "queue": "Run the worker next to the API: `jfast worker` (`jfast dev` starts it).",
    "tenancy": (
        "Every generated table already has tenant_id. `jfast check --multitenant-ready` lists "
        "what still assumes one customer; new modules get current_tenant routes."
    ),
    "rag": "Set [plugin.rag] dimensions to your embedding model's; tenant_scoped follows tenancy.",
    "telemetry": "Free until OTEL_EXPORTER_OTLP_ENDPOINT is set; then every request is traced.",
    "web": "Pages go in templates/ and static/; `jfast new module X --ui htmx` draws some.",
}


def read_enabled(config: Path) -> list[str]:
    """``[plugins].enabled`` as written, or an error naming the fix."""
    import tomllib

    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise typer.BadParameter(f"cannot read {config}: {exc}") from exc
    plugins = data.get("plugins", {})
    enabled = plugins.get("enabled", []) if isinstance(plugins, dict) else []
    return [str(name) for name in enabled]


def write_enabled(config: Path, enabled: list[str]) -> None:
    """Rewrite ``[plugins].enabled`` in place, keeping every other line and comment.

    Text, not a TOML round-trip: jfast.toml is mostly comments explaining each
    setting, and a serialiser would drop every one of them.
    """
    text = config.read_text(encoding="utf-8")
    table = _PLUGINS_TABLE.search(text)
    rendered = "[" + ", ".join(f'"{name}"' for name in enabled) + "]"
    if table is None:
        text = text.rstrip("\n") + f"\n\n[plugins]\nenabled = {rendered}\n"
    else:
        body = table.group(0)
        line = _ENABLED.search(body)
        if line is None:
            new_body = body.replace("[plugins]", f"[plugins]\nenabled = {rendered}", 1)
        else:
            new_body = body[: line.start()] + f"{line.group('indent')}enabled = {rendered}"
            new_body += body[line.end() :]
        text = text[: table.start()] + new_body + text[table.end() :]
    config.write_text(text, encoding="utf-8")


def _extras_in(requirements: str) -> set[str]:
    found = re.search(r"jfastframework\[([^\]]*)\]", requirements)
    return {e.strip() for e in found.group(1).split(",") if e.strip()} if found else set()


def _remove_extra(requirements: str, extra: str) -> str:
    """Take one extra out of the jfastframework pin, leaving the rest sorted."""

    def rewrite(match: re.Match[str]) -> str:
        kept = sorted(e for e in match.group(1).split(",") if e and e != extra)
        return "jfastframework[" + ",".join(kept) + "]" if kept else "jfastframework"

    return re.sub(r"jfastframework\[([^\]]*)\]", rewrite, requirements, count=1)


def _with_requirements(name: str, enabled: list[str]) -> list[str]:
    """``name`` and whatever it cannot start without, in the order to enable them."""
    order: list[str] = []

    def visit(plugin: str) -> None:
        for required in PLUGIN_CATALOG[plugin].requires:
            visit(required)
        if plugin not in enabled and plugin not in order:
            order.append(plugin)

    visit(name)
    # Same rule the generator applies: rag and the queue need somewhere to put
    # things, and PostgreSQL is the one that serves both.
    if name == "rag" and not {"database", "qdrant"} & {*enabled, *order}:
        order.insert(0, "database")
    if name == "queue" and not {"database", "cache"} & {*enabled, *order}:
        order.insert(0, "database")
    return order


def settings_block(rendered: str, plugin: str) -> str | None:
    """The ``[plugin.<name>]`` block (and its sub-tables) out of a rendered jfast.toml."""
    lines = rendered.splitlines()
    header = f"[plugin.{plugin}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        return None
    end = start + 1
    while end < len(lines):
        line = lines[end].strip()
        own = line.startswith(f"[plugin.{plugin}.")
        if (line.startswith("[") and not own) or line.startswith("# Not enabled"):
            break
        end += 1
    return "\n".join(lines[start:end]).rstrip() + "\n"


def append_settings_blocks(
    config: Path, target: Path, enabled: list[str], added: list[str]
) -> list[str]:
    """Append the settings a newly enabled plugin starts with, as the generator writes them.

    Rendered from the same jfast.toml template a new service gets, with the
    service's resulting plugin list -- so `jfast add accounts` writes auth in
    "secret" mode that issues tokens, exactly as `jfast start --multitenant`
    would, and a block that already exists is left alone.
    """
    from jfastframework.cli.scaffold import TEMPLATE_ROOT, Scaffolder, service_context

    text = config.read_text(encoding="utf-8")
    missing = [p for p in added if f"[plugin.{p}]" not in text]
    if not missing:
        return []
    context = service_context(
        target.resolve().name, plugins=enabled, multitenant="tenancy" in enabled
    )
    template = Scaffolder(TEMPLATE_ROOT).env.get_template("service_base/jfast.toml.j2")
    rendered = template.render(**context)
    written: list[str] = []
    for plugin in missing:
        block = settings_block(rendered, plugin)
        if block:
            text = text.rstrip("\n") + "\n\n" + block
            written.append(plugin)
    if written:
        config.write_text(text, encoding="utf-8")
    return written


def add_plugin(name: str, target: Path, *, install: bool) -> list[str]:
    """Enable ``name`` (and what it requires) in a service. Returns what was added."""
    config = target / "jfast.toml"
    if not config.is_file():
        raise typer.BadParameter(f"no jfast.toml in {target}. Run this inside a service.")
    if not plugin_importable(name):
        ui.warn(f"{name} is catalogued but not in this jfastframework install.")
        ui.note("Upgrade jfastframework first; enabling it now would stop the service at boot.")
        raise typer.Exit(1)

    enabled = read_enabled(config)
    added = _with_requirements(name, enabled)
    if not added:
        ui.note(f"{target.name} already enables {name}.")
        return []
    write_enabled(config, [*enabled, *added])
    ui.created(str(config), f"[plugins].enabled + {', '.join(added)}")
    blocks = append_settings_blocks(config, target, [*enabled, *added], added)
    if blocks:
        ui.created(str(config), f"+ {', '.join(f'[plugin.{b}]' for b in blocks)}")

    requirements = target / "requirements.txt"
    extras = sorted({PLUGIN_CATALOG[p].extra for p in added if PLUGIN_CATALOG[p].extra})
    if requirements.is_file() and extras:
        body = requirements.read_text(encoding="utf-8")
        missing = [extra for extra in extras if extra not in _extras_in(body)]
        for extra in missing:
            body = _add_extra(body, extra)
        if missing:
            requirements.write_text(body, encoding="utf-8")
            ui.created(str(requirements), f"now pins [{', '.join(missing)}]")

    for plugin in added:
        spec = PLUGIN_CATALOG[plugin]
        lines = [f"reads {variable}" for variable in spec.env]
        if plugin in _PLUGIN_NOTES:
            lines.append(_PLUGIN_NOTES[plugin])
        lines.append(
            escape(f"settings: [plugin.{plugin}] in jfast.toml, JFAST_{plugin.upper()}_* in .env")
        )
        ui.summary(plugin, [("next", line) for line in lines])

    if install and requirements.is_file() and extras:
        _pip_install(requirements)
    return added


def remove_plugin(
    plugin: str = typer.Argument(..., help="The plugin to take out, e.g. telemetry."),
    service: str | None = typer.Option(
        None, "--service", "-s", help="Which service. Asked when a workspace has several."
    ),
    force: bool = typer.Option(
        False, "--force", help="Remove it even though an enabled plugin requires it."
    ),
) -> None:
    """Take a plugin out of a service: [plugins].enabled, and its extra if nothing else uses it.

    Its [plugin.<name>] settings stay in jfast.toml, so enabling it again later
    finds them where they were; delete the block if it is gone for good.
    """
    if plugin not in PLUGIN_CATALOG:
        known = ", ".join(n for n in PLUGIN_CATALOG if n not in BASE_PLUGINS)
        raise typer.BadParameter(f"no plugin {plugin!r}. Plugins: {known}")
    if plugin in BASE_PLUGINS:
        raise typer.BadParameter(
            f"{plugin} is part of every service: disable it with [plugins].disabled instead"
        )
    target = _resolve_service_dir(service)
    config = target / "jfast.toml"
    if not config.is_file():
        raise typer.BadParameter(f"no jfast.toml in {target}. Run this inside a service.")

    enabled = read_enabled(config)
    if plugin not in enabled:
        ui.note(f"{target.name} does not enable {plugin}.")
        return
    dependants = [
        p for p in enabled if p in PLUGIN_CATALOG and plugin in PLUGIN_CATALOG[p].requires
    ]
    if dependants and not force:
        ui.warn(f"{', '.join(dependants)} cannot start without {plugin}.")
        ui.note(f"Remove {' and '.join(dependants)} first, or pass --force.")
        raise typer.Exit(1)

    remaining = [p for p in enabled if p != plugin]
    write_enabled(config, remaining)
    ui.created(str(config), f"[plugins].enabled - {plugin}")

    requirements = target / "requirements.txt"
    extra = PLUGIN_CATALOG[plugin].extra
    still_used = {PLUGIN_CATALOG[p].extra for p in remaining if p in PLUGIN_CATALOG}
    if requirements.is_file() and extra and extra not in still_used and extra != "server":
        body = requirements.read_text(encoding="utf-8")
        if extra in _extras_in(body):
            requirements.write_text(_remove_extra(body, extra), encoding="utf-8")
            ui.created(str(requirements), f"no longer pins [{extra}]")
    elif extra in still_used:
        ui.note(f"[{extra}] stays in requirements.txt: another enabled plugin uses it.")

    if plugin == "tenancy":
        ui.warn("Routes that use current_tenant now answer 403: there is no tenant to give.")
    ui.note(f"[plugin.{plugin}] stays in jfast.toml; delete it if {plugin} is gone for good.")


def _pinned_version(requirements: str) -> str | None:
    found = re.search(r"^\s*jfastframework(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)", requirements, re.M)
    return found.group(1) if found else None


def _editable_location() -> str | None:
    """Where the running jfastframework is checked out, if it is an editable install."""
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        raw = distribution("jfastframework").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not info.get("dir_info", {}).get("editable"):
        return None
    url = str(info.get("url", ""))
    return url.removeprefix("file://") or None


def _install_would_replace_framework(requirements: Path) -> bool:
    """Say so, and return True, when `pip install -r` would swap the running framework.

    Found migrating a real project: its requirements.txt still pinned the
    previous release, so `jfast add telemetry` reinstalled that release over
    the one running -- which does not even ship the extra it had just pinned.
    """
    from jfastframework import __version__

    extras = sorted(_extras_in(requirements.read_text(encoding="utf-8")))
    spec = f"[{','.join(extras)}]" if extras else ""
    editable = _editable_location()
    if editable is not None:
        ui.warn(
            f"jfastframework is an editable install ({editable}); pip install -r would "
            f"replace it with the published package. Not running pip."
        )
        ui.note(f'Install the extras into your checkout: pip install -e "{editable}{spec}"')
        return True
    pinned = _pinned_version(requirements.read_text(encoding="utf-8"))
    if pinned is not None and pinned != __version__:
        ui.warn(
            f"requirements.txt pins jfastframework=={pinned}, but this is {__version__}; "
            f"pip install -r would install {pinned} over it. Not running pip."
        )
        ui.note(
            f"Run `jfast upgrade --check`, move the pin to {__version__}, then "
            f"pip install -r {requirements}"
        )
        return True
    return False


def _pip_install(requirements: Path) -> None:
    import subprocess  # nosec B404 - fixed argv: this interpreter's pip

    if _install_would_replace_framework(requirements):
        return
    ui.step(f"pip install -r {requirements}")
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)], check=False
    )
    if result.returncode != 0:
        ui.warn("pip failed. requirements.txt is updated; install it yourself.")
        raise typer.Exit(1)
    ui.console.print(f"  [{ui.OK}]installed[/]")


def register(app: typer.Typer) -> None:
    """Attach `add` and `remove` to *app*."""
    app.command("add")(add_capability)
    app.command("remove")(remove_plugin)
