"""`jfast new module`, `service`, `enum` and `view`: the scaffolding commands.

What a new service shares with `jfast init` and `jfast start` is in
:mod:`jfastframework.cli.generate`; this module owns the flags and the prompts.
"""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework.cli import modules as module_registry
from jfastframework.cli import ui
from jfastframework.cli import ui as cli_ui
from jfastframework.cli.common import _report
from jfastframework.cli.generate import (
    _generate_gateway,
    _print_next_steps,
    _register_module,
    generate_service,
)
from jfastframework.cli.patcher import (
    PatchError,
    ensure_import,
    ensure_named_import,
    insert_at_marker,
)
from jfastframework.cli.scaffold import (
    BASE_PLUGINS,
    DEFAULT_FRONTEND_TEMPLATE,
    FRONTEND_TEMPLATES,
    FRONTENDS,
    MODULE_LAYOUTS,
    MODULE_UIS,
    PLUGIN_CATALOG,
    SERVICE_KINDS,
    Scaffolder,
    check_frontend_template,
    detect_frontend,
    detect_frontend_template,
    module_context,
    module_trees,
    to_pascal,
    to_snake,
    view_context,
    view_trees,
)
from jfastframework.workspace import Workspace

new_app = typer.Typer(help="Generate services and modules.", no_args_is_help=True)


#: What each layout is for, in the order somebody should consider them. The
#: hint is the deciding question, not a description -- a list of four
#: architectures with no way to choose between them is not a choice.
LAYOUT_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("layered", "Layered", "router / service / repository. Start here."),
    ("modular", "Modular", "the same, in folders. For a module that outgrows four files."),
    ("screaming", "Screaming", "one file per use case. When the verbs matter more than the nouns."),
    (
        "hexagonal",
        "Hexagonal",
        "ports and adapters. When the domain must be testable with no database.",
    ),
)


def _ask_layout(module: str) -> str:
    """Ask which shape this module should have, when the flag did not say.

    Falls back to the default without asking when there is no terminal, so a
    script, a CI job and a piped install do not hang on a prompt nobody can
    see. A wizard that blocks a pipeline is worse than a flag nobody set.
    """
    if not ui.console.is_terminal:
        return "layered"
    return ui.select(
        f"Architecture for {module!r}",
        [ui.Choice(key, label, hint) for key, label, hint in LAYOUT_CHOICES],
        default="layered",
    )


@new_app.command("module")
def new_module(
    name: str = typer.Argument(..., help="Module name, e.g. 'order' or 'BillingAccount'."),
    layout: str | None = typer.Option(
        None,
        "--layout",
        "-l",
        help=(
            "layered, modular, screaming or hexagonal. "
            "Asked interactively when omitted; defaults to layered when piped."
        ),
    ),
    ui: str = typer.Option(
        "api",
        "--ui",
        "-u",
        help="api = JSON only. htmx = JSON plus server-rendered pages.",
    ),
    table: str | None = typer.Option(
        None, "--table", help="Table name. Defaults to the pluralised module name."
    ),
    target: Path = typer.Option(Path("modules"), "--target", "-t", help="Modules directory."),
    root: Path = typer.Option(
        Path("."), "--root", help="Project root, where the htmx overlay writes templates."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a domain module.

    Two layouts and two UI options, composed rather than duplicated:

        jfast new module order
        jfast new module order --layout screaming
        jfast new module order --ui htmx
        jfast new module order --layout screaming --ui htmx
    """
    if layout is None:
        layout = _ask_layout(name)
    if layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")
    if ui not in MODULE_UIS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_UIS)}", param_hint="--ui")

    scaffolder = Scaffolder()
    context = module_context(name, layout=layout, ui=ui, table=table, modules_dir=target.name)
    trees = module_trees(layout, ui, target, root)
    # `ui` here is the --ui option, which shadows the ui module inside this one
    # function. The spinner is reached through the package to say which is meant.
    with cli_ui.working(f"scaffolding {name}"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    module = context["module"]
    if not dry_run:
        _register_module(root, target.name, module, htmx=ui == "htmx")
        # Remembered so `jfast new use-case` and friends know which folder this
        # module keeps that kind of file in, rather than asking again.
        if module_registry.record(root, module, layout=layout, ui=ui):
            cli_ui.created(module_registry.CONFIG_FILE, f"{module} is {layout}")

    steps = [
        (f"pytest {target}/{module}/tests", "the generated test"),
        (f"alembic revision --autogenerate -m 'add {context['table']}'", "the table"),
    ]
    if ui == "htmx":
        steps.insert(0, ('[plugins] enabled = [..., "web"]', "HTMX pages need it"))
    cli_ui.next_steps(f"{module} ({layout}, {ui})", steps)


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


#: Every plugin `--with` accepts, read off the catalog the installer uses. A
#: hand-written list falls behind the plugins that ship, and then the help
#: denies options `--with` accepts.
_WITH_CHOICES = ",".join(n for n in PLUGIN_CATALOG if n not in BASE_PLUGINS)


@new_app.command("service")
def new_service(
    name: str = typer.Argument(..., help="Service name, e.g. 'billing'."),
    kind: str = typer.Option(
        "api",
        "--kind",
        "-k",
        help="api = JSON. web = server-rendered (Jinja+HTMX). spa = Vue/React. gateway = proxy.",
    ),
    with_: str | None = typer.Option(
        None,
        "--with",
        "-w",
        help=(
            f"Comma-separated plugins: {_WITH_CHOICES}. Defaults to database for backend services."
        ),
    ),
    frontend: str | None = typer.Option(
        None, "--frontend", "-f", help=f"For --kind spa: {', '.join(FRONTENDS)}."
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-T",
        help=(
            f"For --kind spa, the look: {', '.join(FRONTEND_TEMPLATES)}. "
            f"Defaults to {DEFAULT_FRONTEND_TEMPLATE}; `jfast new view` follows it."
        ),
    ),
    language: str = typer.Option(
        "python",
        "--language",
        "-L",
        help="python (full plugin system) or go (stdlib net/http, zero deps).",
    ),
    grpc: bool = typer.Option(
        False, "--grpc", help="Also generate the .proto contract for internal calls."
    ),
    agent_docs: bool = typer.Option(
        False,
        "--agent-docs",
        help="Also write AGENTS.md and .jfast/skills/, for AI agents working here.",
    ),
    layout: str | None = typer.Option(
        None,
        "--layout",
        "-l",
        help=(
            f"Write the contract for this layout now: {', '.join(MODULE_LAYOUTS)}. "
            "Only if you already know how every module here will be shaped -- "
            "otherwise leave it out and the first `jfast new module --layout X` "
            "writes the contract that matches what it generated."
        ),
    ),
    port: int | None = typer.Option(
        None, "--port", "-p", help="Base port. Defaults to the next free block in the workspace."
    ),
    target: Path | None = typer.Option(
        None, "--target", "-t", help="Destination directory. Defaults to ./<name>."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a whole service.

    A frontend is a service like any other -- it deploys, logs and reports
    health identically:

        jfast new service billing --with database,cache
        jfast new service storefront --kind web
        jfast new service admin --kind spa --frontend vue
        jfast new service admin --kind spa --frontend react --template classic
    """
    if kind not in SERVICE_KINDS:
        raise typer.BadParameter(f"choose from: {', '.join(SERVICE_KINDS)}", param_hint="--kind")
    _check_template(template, kind=kind)
    if layout is not None and layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")

    chosen = _split_csv(with_)
    if not chosen and kind in ("api", "web"):
        chosen = ["database"]

    workspace = Workspace.load_or_none()

    try:
        destination, context = generate_service(
            name,
            kind=kind,
            port=port,
            plugins=chosen,
            frontend=frontend,
            frontend_template=template,
            target=target,
            workspace=workspace,
            language=language,
            grpc=grpc,
            agent_docs=agent_docs,
            layout=layout,
            force=force,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    _print_next_steps(destination, context, kind)

    if workspace is not None and workspace.needs_gateway() and not dry_run:
        typer.echo(
            f"\nThe workspace now has {len(workspace.backends)} backends and no gateway.\n"
            f"Generating one so clients need a single hostname:"
        )
        _generate_gateway(workspace, force=False)


ENUM_HEADER = (
    '"""Enumerated values.\n'
    "\n"
    "The value is the wire format: it is stored in a column, serialised into JSON\n"
    "and read by a frontend. Renaming a member is free; changing its value is a\n"
    "data migration.\n"
    "\n"
    "`str, Enum` rather than `Enum`, so a member is a string everywhere -- in\n"
    "JSON, in SQL, and in a log line -- instead of `Status.DRAFT` in some paths\n"
    'and `"DRAFT"` in others.\n'
    '"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from enum import Enum\n"
    "\n"
    "\n"
)


def _render_enum(class_name: str, members: list[str]) -> str:
    lines = [f"class {class_name}(str, Enum):", f'    """{class_name}."""', ""]
    for member in members:
        lines.append(f'    {to_snake(member).upper()} = "{to_snake(member)}"')
    return "\n".join(lines) + "\n"


@new_app.command("enum")
def new_enum(
    name: str = typer.Argument(..., help="Enum name in PascalCase, e.g. DocumentStatus."),
    module: str | None = typer.Option(None, "--module", "-m", help="Put it in this module."),
    shared: bool = typer.Option(False, "--shared", help="Put it in shared/ instead."),
    values: str = typer.Option("", "--values", help="Comma-separated members."),
) -> None:
    """Add an enum, in the module that needs it or in shared/.

    The placement is the decision, not the file. An enum only one module speaks
    belongs to that module; one that two modules speak belongs in `shared/`,
    because the alternative is a cross-module import -- and that is the
    coupling that stops either module from ever being extracted.

    You do not have to predict which is which. Start it in the module, and
    `jfast contracts check` tells you the day a second module imports it.
    """
    if shared and module:
        raise typer.BadParameter("--shared and --module are the same decision, made twice")

    if not shared and module is None:
        shared = ui.confirm(
            "Will more than one module use it?",
            default=False,
            hint="yes puts it in shared/, no puts it in one module",
        )
        if not shared:
            candidates = sorted(
                p.name for p in Path("modules").glob("*") if p.is_dir() and p.name[0] != "_"
            )
            if not candidates:
                raise typer.BadParameter(
                    "no modules here. Run this inside a service, or pass --shared"
                )
            module = (
                candidates[0]
                if len(candidates) == 1
                else ui.select(
                    "Which module?",
                    [ui.Choice(c, "", "") for c in candidates],
                    default=candidates[0],
                )
            )

    members = [v.strip() for v in values.split(",") if v.strip()] or ["DRAFT", "ACTIVE"]
    class_name = to_pascal(name)

    if shared:
        target = Path("shared") / "enums.py"
        where = "shared/"
        why = "every module can import it, and none has to import another"
    else:
        # Hexagonal keeps its vocabulary in the domain layer, and the generated
        # contract scopes that layer to `modules/*/domain/*.py`. An enum at the
        # module root matches no layer glob, so the domain's `may_import` and
        # `forbid_packages` never apply to it -- it imports fine and sits
        # outside the architecture without the checker saying so. The other
        # three layouts do keep enums.py at the module root.
        parent = Path("modules") / str(module)
        recorded = module_registry.layout_of(Path("."), str(module))
        # Falling back to the tree on disk covers modules with no recorded
        # layout; there is no other signal left for those.
        if recorded == "hexagonal" or (recorded is None and (parent / "domain").is_dir()):
            parent = parent / "domain"
        target = parent / "enums.py"
        where = f"{parent.as_posix()}/"
        why = "move it to shared/ the day a second module needs it"

    if not target.parent.is_dir():
        raise typer.BadParameter(f"{target.parent} does not exist. Run this inside a service.")

    body = _render_enum(class_name, members)
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if f"class {class_name}(" in existing:
            typer.echo(f"{class_name} is already in {target}.")
            raise typer.Exit(1)
        target.write_text(existing.rstrip("\n") + "\n\n\n" + body, encoding="utf-8")
    else:
        target.write_text(ENUM_HEADER + body, encoding="utf-8")

    ui.created(str(target), class_name)
    dotted = str(target.with_suffix("")).replace("/", ".").replace("\\", ".")
    ui.summary(
        f"{class_name} in {where}",
        [
            ("members", ", ".join(members)),
            ("why here", why),
            ("import", f"from {dotted} import {class_name}"),
        ],
    )


ICON = "mdiViewDashboardOutline"


def _check_template(template: str | None, *, kind: str = "spa") -> None:
    """Reject a look nobody ships, before anything is written.

    Also a look given to a service that draws nothing: `--kind api --template
    classic` would otherwise be accepted and ignored, and the person who typed
    it would reasonably believe it did something.
    """
    if template is None:
        return
    try:
        check_frontend_template(template)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--template") from exc
    if kind != "spa":
        raise typer.BadParameter(
            f"only a frontend has a look, and --kind {kind} is not one. Use --kind spa.",
            param_hint="--template",
        )


@new_app.command("view")
def new_view(
    name: str = typer.Argument(..., help="View name in PascalCase, e.g. 'Facturas'."),
    frontend: str | None = typer.Option(
        None,
        "--frontend",
        "-f",
        help=f"Choose from: {', '.join(FRONTENDS)}. Detected from the project when omitted.",
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-T",
        help=(
            f"Choose from: {', '.join(FRONTEND_TEMPLATES)}. Read from the project's "
            ".jfast-template stamp when omitted, so the page matches the rest."
        ),
    ),
    root: Path = typer.Option(
        Path("."), "--root", "-r", help="Frontend project root (the folder holding src/)."
    ),
    sidebar: bool = typer.Option(
        True, "--sidebar/--no-sidebar", help="Also add the entry to src/menuAside.js."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a frontend module and register it.

    Creates ``src/Modulo<Name>/`` with Pages, Routes, Services and Components,
    then splices the route into the router and, unless ``--no-sidebar``, the
    entry into the sidebar at their marker comments.

    ``--no-sidebar`` is for the pages a logged-out visitor reaches: login,
    password reset, a public invoice. They are routed like any other view and
    listed in no menu, and removing the entry afterwards means hand-editing
    generated code.

    Running it twice is safe: an already-registered module is detected and
    skipped rather than duplicated.

    The page is drawn in the project's look -- nexora or classic, whichever it
    was generated with -- so a new screen does not arrive in a different
    design from every screen around it.
    """
    _check_template(template)
    resolved = frontend or detect_frontend(root)
    if resolved is None:
        typer.echo(
            f"Cannot tell which framework {root} uses, and --frontend was not given.\n"
            f"Run this from a frontend project root, or pass "
            f"--frontend {'|'.join(FRONTENDS)}.",
            err=True,
        )
        raise typer.Exit(1)

    look = template or detect_frontend_template(root)
    if look is None:
        # No stamp: a project generated before stamps existed, or not by jfast
        # at all. Either way classic was the only look there was, and its page
        # needs nothing but Tailwind -- a nexora page would name classes this
        # project does not have.
        look = "classic"
        ui.note("No .jfast-template here; drawing the classic page. Pass --template to choose.")

    scaffolder = Scaffolder()
    try:
        context = view_context(name, frontend=resolved, frontend_template=look)
        trees = view_trees(resolved, root, frontend_template=look)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    frontend = resolved

    with ui.working("scaffolding"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    if dry_run:
        typer.echo("\n(dry run: router and menu not patched)")
        return

    view = context["View"]
    ext = "js" if frontend == "vue" else "jsx"
    router_file = root / "src" / "router" / f"index.{ext}"
    menu_file = root / "src" / "menuAside.js"

    try:
        results = [
            ensure_import(
                router_file,
                f"import {{ Modulo{view} }} from '@/Modulo{view}/Routes/router.{ext}'",
                guard=f"Modulo{view}/Routes/router",
            ),
            insert_at_marker(
                router_file,
                "nuevaRuta",
                f"...Modulo{view},",
                guard=f"...Modulo{view},",
            ),
        ]
        if sidebar:
            results += [
                ensure_named_import(menu_file, "@mdi/js", ICON),
                insert_at_marker(
                    menu_file,
                    "nuevoModulo",
                    "{\n"
                    f"  to: '{context['view_path']}',\n"
                    f"  icon: {ICON},\n"
                    f"  label: '{context['view_title']}',\n"
                    "},",
                    guard=f"to: '{context['view_path']}'",
                ),
            ]
    except PatchError as exc:
        typer.echo(f"\nFiles were written, but registration failed:\n  {exc}", err=True)
        raise typer.Exit(1) from exc

    for result in results:
        typer.echo(str(result))

    typer.echo(
        f"\nModule 'Modulo{view}' scaffolded and registered.\n"
        f"  route:   {context['view_path']}\n"
        f"  page:    src/Modulo{view}/Pages/{view}View."
        + ("vue" if frontend == "vue" else "jsx")
        + f"\n  service: src/Modulo{view}/Services/{context['view_slug']}.service.js\n"
        + ("" if sidebar else "  sidebar: not listed (--no-sidebar)\n")
        + f"\nThe service calls {context['view_path']} on VITE_API_URL. Point it at a real\n"
        f"backend module with: jfast new module {context['view_snake']}"
    )


def register(app: typer.Typer) -> None:
    """Add the `new` group to *app*."""
    app.add_typer(new_app, name="new")
