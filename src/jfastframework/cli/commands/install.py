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
    DATASTORE_PLUGINS,
    DEFAULT_FRONTEND_TEMPLATE,
    FRONTEND_TEMPLATES,
    FRONTENDS,
    PLUGIN_CATALOG,
    Scaffolder,
    check_frontend_template,
    module_context,
    module_trees,
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
    api_dir, _api_context = generate_service(
        slug,
        kind="api",
        port=port,
        plugins=plugins,
        frontend=None,
        target=Path(slug),
        workspace=workspace,
        force=force,
    )

    # A monolith with one module is a monolith with nothing in it. Generate a
    # real one so the first `pytest` and the first migration have a subject.
    scaffolder = Scaffolder()
    module = module_context("item", modules_dir="modules")
    scaffolder.render_trees(
        module_trees("layered", "api", api_dir / "modules", api_dir),
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
    if module_registry.record(api_dir, "item", layout="layered", ui="api"):
        ui.created(f"{api_dir}/{module_registry.CONFIG_FILE}", "item is layered")

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
    if kind in ("api", "web"):
        ui.rule("Datastores")
        chosen += ui.multiselect(
            "What does it store?",
            [ui.Choice(name, PLUGIN_CATALOG[name].label, "") for name in DATASTORE_PLUGINS],
            defaults={"database"},
        )

        ui.rule("Capabilities")
        optional: list[ui.Choice] = []
        if {"database", "qdrant"} & set(chosen):
            optional.append(ui.Choice("rag", "Semantic search", "Retrieval over the store above"))
        optional += [
            ui.Choice("queue", "Background jobs", "Retries, backoff, dead-lettering"),
            ui.Choice("mail", "Email", "Templates, queued by default"),
            ui.Choice("auth", "Authentication", "JWT, scopes, rotation, revocation"),
            ui.Choice("storage", "File storage", "Local disks, S3 or MinIO"),
            ui.Choice("tenancy", "Multi-tenancy", "One deployment, many customers"),
            ui.Choice("notifications", "Push", "Firebase Cloud Messaging"),
            ui.Choice("sentry", "Error reporting", "Off unless a DSN is set"),
        ]
        if kind == "api":
            optional.append(
                ui.Choice("web", "Server-rendered pages", "Jinja2 and HTMX alongside the API")
            )
        chosen += ui.multiselect("Anything else?", optional, defaults=set())

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

    if "tenancy" in chosen:
        ui.warn("Multi-tenancy needs two things set before it isolates anything.")
        typer.echo(
            "Set [plugin.tenancy] base_domain in jfast.toml,\n"
            "then, for a certificate per tenant subdomain:\n"
            "    jfast workspace caddy --hostname <your-domain> --production --wildcard-tenants\n"
            "\nThat needs a wildcard DNS record and an /internal/tenant-exists endpoint --\n"
            "docs/multitenancy.md explains why the second one is not optional."
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
