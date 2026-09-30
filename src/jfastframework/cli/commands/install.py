"""`jfast start` and `jfast init`: the interactive installer and the
opinionated default stack.
"""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework import capabilities
from jfastframework.cli import modules as module_registry
from jfastframework.cli import ui
from jfastframework.cli.generate import (
    _add_extra,
    _print_next_steps,
    _register_module,
    _write_service_envs,
    _write_workspace_secrets,
    generate_service,
)
from jfastframework.cli.scaffold import (
    BASE_PLUGINS,
    DATASTORE_PLUGINS,
    DEFAULT_FRONTEND_TEMPLATE,
    DEFAULT_LAYOUT,
    FRONTEND_TEMPLATES,
    FRONTENDS,
    MULTITENANT_RECOMMENDED,
    PLUGIN_CATALOG,
    RECOMMENDED,
    Scaffolder,
    check_frontend_template,
    module_context,
    module_trees,
    plugin_importable,
    to_snake,
)
from jfastframework.workspace import PORT_BLOCK_SIZE, WORKSPACE_FILE, Workspace


def start(
    name: str = typer.Argument("app", help="Project name."),
    port: int = typer.Option(8000, "--port", "-p", help="Base port for the first block."),
    frontend: str = typer.Option("vue", "--frontend", "-f", help=f"{', '.join(FRONTENDS)}."),
    template: str = typer.Option(
        DEFAULT_FRONTEND_TEMPLATE,
        "--template",
        "-T",
        help=f"The frontend's look: {', '.join(FRONTEND_TEMPLATES)}.",
    ),
    queue_backend: str = typer.Option("postgres", "--queue", help="postgres (default) or redis."),
    telemetry: bool = typer.Option(
        True,
        "--telemetry/--no-telemetry",
        help="OpenTelemetry traces: on by default, exporting nothing until an endpoint is set.",
    ),
    multitenant: bool = typer.Option(
        False,
        "--multitenant/--single-tenant",
        help=(
            "Several customers in one deployment: tenancy, auth and accounts on, routes "
            "scoped with current_tenant. Default single-tenant; tenant_id columns stay."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """The opinionated default stack, in one command.

    A modular monolith in Python with PostgreSQL + pgvector, Redis, background
    jobs and a Vue frontend, behind Caddy. No questions asked.

    Why a monolith and not three services: you do not know the seams yet.
    Splitting later is a move; un-splitting is a rewrite. Modules keep the
    boundaries visible until the seams are obvious, and then
    `jfast new service` stands up its deployment and rewires the workspace;
    moving the code across is still yours.

    Single-tenant by default. Multitenant means every route needs a signed-in
    caller with a tenant and a user system to sign them in -- the right shape
    for a SaaS and too much for the first `curl` of anything else. Every
    generated table keeps its `tenant_id` column either way, so switching later
    is a data backfill, not a schema rewrite.
    """
    if frontend not in FRONTENDS:
        raise typer.BadParameter(f"choose from: {', '.join(FRONTENDS)}", param_hint="--frontend")
    try:
        check_frontend_template(template)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--template") from exc
    if queue_backend not in ("postgres", "redis"):
        raise typer.BadParameter("choose from: postgres, redis", param_hint="--queue")

    slug = to_snake(name)
    ui.banner("the opinionated default stack")

    workspace = Workspace.load_or_none()
    if workspace is None:
        workspace = Workspace(
            name=slug, base_port=port - PORT_BLOCK_SIZE, file=Path(WORKSPACE_FILE)
        )
        workspace.save()
        ui.created(str(workspace.file), "workspace")

    plugins = ["database", "cache", "queue"]
    if telemetry:
        plugins.append(_telemetry_or_note())
    if multitenant:
        plugins += list(MULTITENANT_RECOMMENDED)
    plugins = [name for name in plugins if name]
    api_dir, api_context = generate_service(
        slug,
        kind="api",
        port=port,
        plugins=plugins,
        frontend=None,
        target=Path(slug),
        workspace=workspace,
        force=force,
        multitenant=multitenant,
    )

    # A monolith with one module is a monolith with nothing in it. Generate a
    # real one so the first `pytest` and the first migration have a subject.
    scaffolder = Scaffolder()
    module = module_context("item", modules_dir="modules", access=api_context["route_access"])
    scaffolder.render_trees(
        module_trees(DEFAULT_LAYOUT, "api", api_dir / "modules", api_dir),
        module,
        force=force,
    )
    ui.created(f"{api_dir}/modules/item/", "a real module, so the first test has a subject")

    # `jfast new module` mounts what it generates, and so does this path. A
    # module that is rendered but not mounted is inert and nothing fails: the
    # tests pass, the server starts and /items 404s, while `jfast check` reports
    # HIGH, exit 1, on a tree the framework just wrote itself. Same two calls,
    # so both paths agree.
    _register_module(api_dir, "modules", "item", htmx=False)
    if module_registry.record(api_dir, "item", layout=DEFAULT_LAYOUT, ui="api"):
        ui.created(f"{api_dir}/{module_registry.CONFIG_FILE}", f"item is {DEFAULT_LAYOUT}")

    front_dir, _ = generate_service(
        f"{slug}_web",
        kind="spa",
        port=None,
        plugins=[],
        frontend=frontend,
        frontend_template=template,
        target=Path(f"{slug}-web"),
        workspace=workspace,
        force=force,
        frontend_accounts="accounts" in api_context["enabled_plugins"],
    )

    from jfastframework.deploy.workspace import render_caddyfile, render_workspace_compose

    Path("docker-compose.yml").write_text(render_workspace_compose(workspace), encoding="utf-8")
    Path("Caddyfile").write_text(render_caddyfile(workspace), encoding="utf-8")
    ui.created("docker-compose.yml", "every service, one network")
    ui.created("Caddyfile", "one origin, so the browser sees no CORS")

    # Without this the compose file this command just wrote cannot start: it
    # interpolates ${SHOP_DATABASE_PASSWORD} and friends, and compose refuses
    # rather than defaulting. So `docker compose up --build`, which is the very
    # next thing this command tells you to run, would fail on a fresh project.
    secrets_written = _write_workspace_secrets(workspace)
    if secrets_written:
        ui.created(".env", f"{secrets_written} generated, gitignored")
    for env_path in _write_service_envs(workspace):
        ui.created(str(env_path), "from the resource graph")

    ui.summary(
        f"{slug} is ready",
        [
            ("stack", "modular monolith"),
            ("data", "PostgreSQL + pgvector, Redis"),
            ("jobs", f"background jobs on {queue_backend}"),
            ("customers", "several (tenancy, accounts)" if multitenant else "one (tenant_id kept)"),
            ("traces", "OpenTelemetry, off until an endpoint is set" if telemetry else "off"),
            ("web", f"{frontend} frontend, {template} look, behind Caddy"),
        ],
    )

    # Docker first: it is the one path that needs nothing installed, and the
    # one that matches what runs in production.
    #
    # The local path is `jfast dev`, not `cp .env.example .env`: the .env this
    # command just wrote from the resource graph is the correct one, and the
    # copy would overwrite it with a DSN pointing at localhost:8001 and a
    # password nobody has set. `jfast dev` rewrites the container hostnames to the
    # published ports and resolves the workspace secret, which is exactly what a
    # process on the host needs and what no static file can hold for both.
    ui.next_steps(
        "Run it",
        [
            ("docker compose up --build", "all of it, nothing else to install"),
            ("", ""),
            (f"cd {api_dir}", "or run it on the host"),
            ("pip install -r requirements-dev.txt", "in a virtualenv"),
            ("jfast dev", "datastores, migrations, API and frontend"),
            ("", ""),
            (f"cd {front_dir} && npm install && npm run dev", "the frontend alone"),
        ],
    )

    ui.note("When a module outgrows the monolith:")
    ui.note("    jfast new service billing --with database")


def capability_choices(
    kind: str, datastores: list[str], *, multitenant: bool
) -> tuple[list[ui.Choice], set[str], list[str]]:
    """The capabilities `jfast init` offers, which are pre-checked, and which are skipped.

    Generated from PLUGIN_CATALOG rather than written out, because the written
    list stopped at 0.1.0a8 and nine plugins shipped after it that the
    installer never offered. Left out: what the other questions decide --
    datastores, the always-on base, tenancy (the multitenant question), the
    gateway (a kind of its own) -- and a plugin this install cannot import.
    """
    decided = {*DATASTORE_PLUGINS, *BASE_PLUGINS, "tenancy", "gateway"}
    if kind != "api":
        decided.add("web")  # a web service has it; nothing else can host pages
    recommended = set(RECOMMENDED) | (set(MULTITENANT_RECOMMENDED) if multitenant else set())

    choices: list[ui.Choice] = []
    skipped: list[str] = []
    for name, spec in PLUGIN_CATALOG.items():
        if name in decided:
            continue
        if not plugin_importable(name):
            skipped.append(name)
            continue
        hint = "recommended" if name in recommended else ""
        if name == "rag" and not {"database", "qdrant"} & set(datastores):
            hint = (hint + "; " if hint else "") + "adds PostgreSQL for its vectors"
        choices.append(ui.Choice(name, spec.label, hint))
    # Recommended first: pressing Enter through the list keeps what it should.
    choices.sort(key=lambda choice: choice.key not in recommended)
    return choices, {c.key for c in choices if c.key in recommended}, skipped


def _telemetry_or_note() -> str:
    """``"telemetry"``, or nothing with a note when this install lacks the plugin.

    The catalog can list a plugin before its code ships in the installed
    framework; writing it into jfast.toml then would produce a service that
    refuses to boot on an unknown plugin.
    """
    if plugin_importable("telemetry"):
        return "telemetry"
    ui.note("telemetry is not in this jfastframework install; left out. Upgrade, then")
    ui.note("    jfast add telemetry")
    return ""


def init(
    name: str | None = typer.Argument(None, help="Service name. Prompted if omitted."),
) -> None:
    """Interactive installer: pick a kind, a frontend and its look, and your datastores.

    The flag-driven `jfast new service` does the same thing without questions.
    This is the front door for the first service in a project.
    """
    ui.banner("The interactive installer. Every answer is also a flag.")

    service_name = name or ui.ask("service name", default="app")

    kind = ui.select(
        "What are you building?",
        [
            ui.Choice("api", "", "A JSON API"),
            ui.Choice("web", "", "Server-rendered pages: Jinja2 and HTMX, no build step"),
            ui.Choice("spa", "", "A frontend project: Vue or React, with Tailwind and a look"),
            ui.Choice("gateway", "", "A reverse proxy in front of other services"),
        ],
        default="api",
    )

    frontend: str | None = None
    template: str | None = None
    if kind == "spa":
        frontend = ui.select(
            "Which frontend?",
            [
                ui.Choice("vue", "Vue 3", "Vite, Tailwind v4, built in CI"),
                ui.Choice("react", "React", "Vite, Tailwind v4, built in CI"),
            ],
            default="vue",
        )
        ui.note("Angular is not generated: no CI job builds it, so it would be untested.")
        template = ui.select(
            "Which look?",
            [
                ui.Choice("nexora", "Nexora", "Liquid glass: glass panels, island top bar, WebGL"),
                ui.Choice("classic", "Classic", "Plain Tailwind panels, one crimson accent"),
            ],
            default=DEFAULT_FRONTEND_TEMPLATE,
        )

    chosen: list[str] = []
    multitenant = False
    if kind in ("api", "web"):
        ui.rule("Datastores")
        chosen += ui.multiselect(
            "What does it store?",
            [ui.Choice(name, PLUGIN_CATALOG[name].label, "") for name in DATASTORE_PLUGINS],
            defaults={"database"},
        )

        ui.rule("Customers")
        ui.note(
            "Yes turns on tenancy, auth and accounts, scopes every generated route to\n"
            "  the caller's tenant, and makes RAG and the LLM budget per tenant. No keeps\n"
            "  one customer. Either way every table keeps its tenant_id column, so the\n"
            "  switch later is a data backfill, not a schema rewrite."
        )
        multitenant = ui.confirm(
            "Does this app serve several customers (multitenant)?",
            default=False,
            hint="a SaaS: yes. An internal tool or one client's system: no",
        )

        ui.rule("Capabilities")
        choices, defaults, skipped = capability_choices(kind, chosen, multitenant=multitenant)
        for name in skipped:
            ui.note(f"{name} is catalogued but not in this install; left out (jfast add {name}).")
        chosen += ui.multiselect("Anything else?", choices, defaults=defaults)

    extras_chosen: list[str] = []
    if kind in ("api", "web"):
        ui.rule("Packages")
        ui.note(
            "None of these are installed by default: a service that serves "
            "JSON should not carry numpy. Each one can be added later with "
            "`jfast add`."
        )
        extras_chosen = ui.multiselect(
            "Anything from the catalogue?",
            [
                ui.Choice(
                    name,
                    capabilities.CATALOG[name].summary,
                    "heavy" if capabilities.CATALOG[name].heavy else "",
                )
                for name in capabilities.names()
            ],
            defaults=set(),
        )

    ui.rule("Agents")
    ui.note(
        "An AGENTS.md and a skill under .jfast/skills/, so an AI agent reads the\n"
        "  rules of this project before it writes in it rather than guessing them."
    )
    agent_docs = ui.confirm(
        "Write the agent surface?",
        default=True,
        hint="a design skill too, if there is a frontend",
    )

    workspace = Workspace.load_or_none()
    if workspace is None:
        ui.rule("Workspace")
        ui.note(
            "A workspace gives every service a free port block, writes one compose "
            "file,\n  and generates a gateway once there is more than one backend."
        )
        if ui.confirm(f"Create {WORKSPACE_FILE} here?", default=True):
            workspace = Workspace(name=to_snake(service_name), file=Path(WORKSPACE_FILE))
            workspace.save()
            ui.created(str(workspace.file))

    default_port = workspace.next_port() if workspace else 8000
    port = ui.ask_int("base port, a block of ten", default=default_port)

    ui.summary(
        "About to generate",
        [
            ("service", service_name),
            ("kind", kind + (f" ({frontend}, {template})" if frontend else "")),
            ("ports", f"{port}-{port + 9}"),
            ("plugins", ", ".join(chosen) if chosen else "observability, metrics"),
            ("customers", "several (tenancy)" if multitenant else "one"),
            ("packages", ", ".join(extras_chosen) if extras_chosen else "none"),
            ("workspace", str(workspace.file) if workspace else "none"),
            ("agents", "AGENTS.md + skills" if agent_docs else "none"),
        ],
    )

    try:
        destination, context = generate_service(
            service_name,
            kind=kind,
            port=port,
            plugins=chosen,
            frontend=frontend,
            frontend_template=template,
            target=None,
            workspace=workspace,
            agent_docs=agent_docs,
            multitenant=multitenant,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    for name in extras_chosen:
        spec = capabilities.get(name)
        requirements = destination / "requirements.txt"
        if requirements.is_file():
            requirements.write_text(
                _add_extra(requirements.read_text(encoding="utf-8"), spec.extra),
                encoding="utf-8",
            )
            ui.created(str(requirements), f"+ {spec.extra}")
        if spec.system_packages:
            ui.warn(f"{name} needs system packages: {' '.join(spec.system_packages)}")

    _print_next_steps(destination, context, kind)

    if multitenant:
        ui.warn("Multitenant: the tenant comes from the signed-in caller.")
        typer.echo(
            'Tenancy reads sources = ["token", "user"]: the token\'s tenant claim when an\n'
            "organisation owns the data, the signed-in user otherwise. Generated routes use\n"
            "current_tenant (401 without a session, 403 without a tenant). Set\n"
            "JFAST_AUTH_SECRET and JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD in .env before\n"
            "the first start; docs/multitenancy.md covers row-level security."
        )

    if "storage" in chosen:
        ui.warn("Signed storage links do not work until a key is set.")
        typer.echo(
            "A public and a private local disk are configured. Private links are\n"
            "signed, so set a key or they will not work:\n"
            "    JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)"
        )

    if workspace is not None and ui.confirm(
        "Deploying to Kubernetes?", default=False, hint="writes a kustomize tree under k8s/"
    ):
        from jfastframework.deploy.kubernetes import build as build_k8s

        host = typer.prompt("  Ingress hostname", default="example.com")
        files = build_k8s(workspace, host=host)
        for relative, contents in sorted(files.items()):
            destination_path = Path("k8s") / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(contents, encoding="utf-8")
        typer.echo(
            f"\n  created           k8s/ ({len(files)} files)\n"
            f"\n    kubectl apply -k k8s/overlays/dev\n"
            f"\nRegenerate after adding a service:\n"
            f"    jfast workspace k8s --force\n"
            f"\nk8s/README.md explains what is not generated -- the database, on\n"
            f"purpose -- and what to change before production."
        )
    elif workspace is not None:
        typer.echo("\nIf you need Kubernetes later:  jfast workspace k8s")


def register(app: typer.Typer) -> None:
    """Attach `start` and `init` to *app*."""
    app.command()(start)
    app.command()(init)
