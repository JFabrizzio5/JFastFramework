"""What more than one command writes.

`jfast new service`, `jfast init` and `jfast start` all produce a service;
`jfast new module` and `jfast start` both mount a module; `jfast workspace env`
and `jfast start` both write the workspace secrets. Each of those paths lives
here once, because two copies of a generator are two trees that drift apart --
and so that no command module imports another to reuse one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from jfastframework import languages
from jfastframework.cli import ui
from jfastframework.cli import ui as cli_ui
from jfastframework.cli.common import _report
from jfastframework.cli.patcher import PatchError, insert_at_marker
from jfastframework.cli.scaffold import (
    DEFAULT_FRONTEND_TEMPLATE,
    Scaffolder,
    WrittenFile,
    service_context,
    service_trees,
    to_snake,
)
from jfastframework.workspace import ServiceEntry, Workspace


def _register_module(root: Path, modules_dir: str, module: str, *, htmx: bool) -> None:
    """Splice a new module's router into main.py.

    The frontend registers its own routes and menu entries, and the backend
    does the same here rather than printing two lines to be pasted: a generated
    module is inert until it is mounted, and a module that is not mounted looks
    exactly like a module that does not work.

    Non-fatal by design. A hand-edited main.py that lost its markers, or a
    module generated outside a service, should still leave the files on disk --
    so a failure here prints what to paste instead of unwinding the scaffold.
    """
    entry = root / "main.py"
    if not entry.is_file():
        cli_ui.note(f"No main.py in {root}; mount {module}_router yourself.")
        return

    imports = [f"from {modules_dir}.{module} import router as {module}_router"]
    routers = [f"{module}_router,"]
    if htmx:
        imports.append(f"from {modules_dir}.{module}.web import router as {module}_web_router")
        routers.append(f"{module}_web_router,")

    changed = False
    try:
        for statement in imports:
            result = insert_at_marker(entry, "jfast:imports", statement, guard=statement, indent="")
            changed = changed or result.changed
        for router in routers:
            result = insert_at_marker(
                entry, "jfast:routers", router, guard=f"    {router}", indent="    "
            )
            changed = changed or result.changed
    except PatchError as exc:
        cli_ui.warn(str(exc))
        cli_ui.note("Add these by hand:")
        for line in imports + routers:
            cli_ui.note(f"    {line}")
        return

    if changed:
        _sort_mounted_imports(entry, first_party={modules_dir, "web", "shared"})

    # Say which of the two happened. Reporting a mount that did not occur is
    # the same lie as reporting a file written that was already there.
    if changed:
        cli_ui.created("main.py", f"{module}_router mounted")
    else:
        cli_ui.note(f"main.py already mounts {module}_router")


_IMPORTS_MARKER = "# [jfast:imports]"
_FROM_IMPORT = re.compile(r"^from ([\w.]+) import ")


def _sort_mounted_imports(entry: Path, *, first_party: set[str]) -> None:
    """Keep the router imports above the marker sorted, and apart from it.

    The marker splices each import in where the marker sits, so imports land in
    the order modules were created: `jfast new module alerta` after `item`
    wrote them unsorted, and the marker right under the last import is one
    blank line short of what isort wants. Either one fails `ruff check .` in a
    project the generator itself just wrote.

    Only the run of this project's own imports directly above the marker is
    touched -- a line that is anything else ends the run -- so a hand-edited
    main.py keeps whatever else it has.
    """
    lines = entry.read_text(encoding="utf-8").split("\n")
    try:
        marker = next(i for i, line in enumerate(lines) if line.strip() == _IMPORTS_MARKER)
    except StopIteration:
        return

    start = marker
    while start > 0:
        previous = lines[start - 1]
        found = _FROM_IMPORT.match(previous)
        if previous.strip() == "" or (found and found.group(1).split(".")[0] in first_party):
            start -= 1
            continue
        break

    run = [line for line in lines[start:marker] if line.strip()]
    if not run:
        return

    def module_of(line: str) -> str:
        found = _FROM_IMPORT.match(line)
        return found.group(1).lower() if found else line

    ordered = sorted(dict.fromkeys(run), key=module_of)
    rewritten = [*lines[:start], *ordered, "", *lines[marker:]]
    if rewritten != lines:
        entry.write_text("\n".join(rewritten), encoding="utf-8")


def workspace_has_accounts(workspace: Workspace | None) -> bool:
    """Whether any backend in the workspace enables `accounts`.

    A generated frontend draws sign-in, registration and a Security page, and
    makes its routes private by default, only when something can sign people
    in; otherwise it stays public with no account pages. Read from each
    backend's jfast.toml, which is where that is decided.
    """
    import tomllib

    if workspace is None:
        return False
    for service in workspace.services:
        if service.is_frontend:
            continue
        try:
            data = tomllib.loads((Path(service.path) / "jfast.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        plugins = data.get("plugins", {})
        if isinstance(plugins, dict) and "accounts" in plugins.get("enabled", []):
            return True
    return False


def generate_service(
    name: str,
    *,
    kind: str,
    port: int | None,
    plugins: Sequence[str],
    frontend: str | None,
    target: Path | None,
    workspace: Workspace | None,
    frontend_template: str | None = None,
    language: str = "python",
    grpc: bool = False,
    agent_docs: bool = False,
    layout: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    multitenant: bool = False,
    frontend_accounts: bool | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Render a service and register it in the workspace, if there is one.

    Shared by `jfast new service`, `jfast init` and `jfast start` so every path
    produces exactly the same tree -- a wizard that generates something slightly
    different from the flag-driven command is a wizard nobody trusts.
    """
    scaffolder = Scaffolder()
    slug = to_snake(name)
    resolved_port = port if port is not None else (workspace.next_port() if workspace else 8000)

    spec = languages.get(language)
    if not spec.installed():
        typer.echo(
            f"Warning: {spec.toolchain} is not on PATH. The files will be written, "
            f"but you cannot build or run this service until it is installed.",
            err=True,
        )

    context = service_context(
        name,
        kind=kind,
        port=resolved_port,
        plugins=plugins,
        frontend=frontend,
        frontend_template=frontend_template,
        language=language,
        grpc=grpc,
        agent_docs=agent_docs,
        workspace_name=workspace.name if workspace else slug,
        api_base_url=workspace.api_base_url() if workspace else f"http://localhost:{resolved_port}",
        multitenant=multitenant,
        frontend_accounts=(
            workspace_has_accounts(workspace) if frontend_accounts is None else frontend_accounts
        ),
    )
    destination = target or Path(slug)

    trees = service_trees(
        kind,
        frontend,
        destination,
        language=language,
        grpc=grpc,
        agent_docs=agent_docs,
        layout=layout,
        frontend_template=frontend_template or DEFAULT_FRONTEND_TEMPLATE,
    )
    with ui.working("scaffolding"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
        written += _write_dockerignore(destination, dry_run=dry_run)
        written += _write_dockerfile(destination, kind=kind, language=language, dry_run=dry_run)
    _report(written)

    if workspace is not None and not dry_run:
        workspace.add(
            ServiceEntry(
                name=slug,
                kind=kind,
                port=resolved_port,
                path=str(destination),
                frontend=frontend,
                language=language,
                grpc=grpc,
                datastores=list(context["datastores"]),
            ),
            replace=force,
        )
        # New services get named resources rather than the legacy type list, so
        # the file a project starts with is the one the documentation teaches.
        # Idempotent, and it preserves every port.
        workspace.migrate_resources()
        workspace.save()
        ui.note(f"registered in {workspace.file}")

    return destination, context


def _write_dockerignore(destination: Path, *, dry_run: bool) -> list[WrittenFile]:
    """The exclude list, written with the service rather than with the image.

    `jfast deploy dockerfile` writes one too, but that command is run later --
    often after the first `cp .env.example .env`. A build that happens in
    between copies the filled-in .env into a layer, and by then the leak has
    already been made. It costs nothing to have the file from the start.
    """
    from jfastframework.deploy import render_dockerignore

    path = destination / ".dockerignore"
    if path.exists():
        return [WrittenFile(path, created=False)]
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_dockerignore(), encoding="utf-8")
    return [WrittenFile(path, created=True)]


def _write_dockerfile(
    destination: Path, *, kind: str, language: str, dry_run: bool
) -> list[WrittenFile]:
    """The image, written with the service for the same reason as the excludes.

    Every generated compose file gives the application service ``build: .`` --
    both generators, both commands. Without the Dockerfile that entry needs,
    `docker compose up`, printed as the next step by `jfast start` and by
    `jfast new service`, fails on a fresh project with

        failed to solve: failed to read dockerfile: open Dockerfile: no such
        file or directory

    A generated compose file that cannot build is not a deployment artefact.

    Go ships its own Dockerfile in its template, and an SPA is static files
    behind Caddy rather than an image, so neither is written here.
    """
    if language != "python" or kind == "spa":
        return []

    from jfastframework.deploy import render_dockerfile

    path = destination / "Dockerfile"
    if path.exists():
        return [WrittenFile(path, created=False)]
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_dockerfile(), encoding="utf-8")
    return [WrittenFile(path, created=True)]


def _print_next_steps(destination: Path, context: dict[str, Any], kind: str) -> None:
    """The commands to run now, each with what it does.

    A list of commands with no explanation is a list somebody pastes without
    reading. The description column is the difference between following
    instructions and understanding them.
    """
    slug = context["service_slug"]
    port = context["port"]

    if context.get("language") == "go":
        steps = [
            (f"cd {destination}", ""),
            ("cp .env.example .env", "the generated defaults"),
            ("go test ./...", "the contract tests that ship with it"),
            ("go run .", f"serves on :{port}"),
        ]
        ui.next_steps(f"{slug}  go {kind}", steps)
        if context.get("grpc"):
            ui.note("The gRPC contract is in proto/. Generating stubs is a build step you own.")
        return

    if kind == "spa":
        look = context["frontend_template"]
        ui.next_steps(
            f"{slug} {ui.G.dash} {context['frontend']} {ui.G.bullet} {look}",
            [
                (f"cd {destination}", ""),
                ("npm install", ""),
                ("npm run dev", f"http://localhost:{port}"),
                ("jfast new view Facturas", "a page, wired into the router and sidebar"),
            ],
        )
        ui.note(f"VITE_API_URL already points at {context['api_base_url']}.")
        return

    if kind == "gateway":
        ui.next_steps(
            f"{slug} {ui.G.dash} gateway",
            [
                (f"cd {destination}", ""),
                (f'pip install "jfastframework[{context["extras"]}]"', ""),
                ("jfast serve", f"http://127.0.0.1:{port}"),
            ],
        )
        return

    steps = [
        (f"cd {destination}", ""),
        # The dev list, not the deploy one: the next step this command and
        # `jfast new module` both print is `pytest`, and requirements.txt has no
        # test runner in it -- deliberately, it is what the image installs. So
        # the deploy list would answer that step with `No module named pytest`.
        ("pip install -r requirements-dev.txt", "requirements.txt to deploy"),
        ("cp .env.example .env", "then fill in the secrets"),
    ]
    if context["has_database"]:
        # `jfast deploy compose` first, because there is no docker-compose.yml
        # in the tree yet, and `docker compose up -d` against a file that does
        # not exist fails.
        steps.append(("jfast deploy compose -o docker-compose.yml", "writes it from the plugins"))
        steps.append(("docker compose up -d", "the datastores it needs"))
        steps.append(("alembic upgrade head", "creates the schema"))
    steps.append(
        ("jfast serve", f"http://127.0.0.1:{port}  {ui.G.bullet}  /docs  {ui.G.bullet}  /ready")
    )
    steps.append(
        (
            "jfast new module invoice" + (" --ui htmx" if kind == "web" else ""),
            "your first module",
        )
    )

    ui.next_steps(f"{slug}  {kind}", steps)
    ui.note(f"plugins: {', '.join(context['enabled_plugins'])}")


def _add_extra(requirements: str, extra: str) -> str:
    """Put an extra into the existing jfastframework pin, in sorted order."""
    import re

    def rewrite(match: re.Match[str]) -> str:
        current = [e for e in match.group(1).split(",") if e]
        if extra not in current:
            current.append(extra)
        return "jfastframework[" + ",".join(sorted(current)) + "]"

    updated, count = re.subn(r"jfastframework\[([^\]]*)\]", rewrite, requirements, count=1)
    if count:
        return updated
    # No extras yet, or no framework line at all: append rather than guess.
    return requirements.rstrip("\n") + f"\njfastframework[{extra}]\n"


def _generate_gateway(workspace: Workspace, *, force: bool) -> Path:
    """Render the gateway from the workspace's current backends."""
    existing = workspace.gateway
    port = existing.port if existing else workspace.next_port()
    destination = Path(existing.path) if existing else Path("gateway")

    routes = [
        {"prefix": service.prefix, "target": service.internal_url} for service in workspace.backends
    ]

    scaffolder = Scaffolder()
    context = service_context(
        "gateway",
        kind="gateway",
        port=port,
        workspace_name=workspace.name,
        routes=routes,
    )
    with ui.working("scaffolding the gateway"):
        written = scaffolder.render_trees(
            service_trees("gateway", None, destination), context, force=force
        )
    _report(written)

    if existing is None:
        workspace.add(
            ServiceEntry(name="gateway", kind="gateway", port=port, path=str(destination))
        )
    workspace.save()

    typer.echo(
        f"\nGateway on port {port}, routing {len(routes)} backend(s):\n"
        + "\n".join(f"  {r['prefix']:<16} -> {r['target']}" for r in routes)
    )
    return destination


def _write_workspace_secrets(workspace: Workspace, *, dry_run: bool = False) -> int:
    """Generate a password per resource into the workspace `.env`, once.

    Existing values are never overwritten: rotating a password is a decision,
    and silently changing one would lock a running container out of its own
    volume. The file is gitignored -- the DSNs that reference these live in
    each service's generated .env as `${NAME_PASSWORD}`, so the secret itself
    appears in exactly one place.
    """
    import secrets as _secrets

    needed = [r for r in workspace.all_resources() if r.spec.needs_credentials]
    if not needed:
        return 0

    path = Path(".env")
    existing: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value

    generated = 0
    for resource in needed:
        if resource.secret_var not in existing:
            existing[resource.secret_var] = _secrets.token_urlsafe(24)
            generated += 1

    if generated and not dry_run:
        body = "# Secrets for this workspace. Generated by `jfast workspace env`.\n"
        body += "# Never commit this file.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(existing.items()))
        path.write_text(body, encoding="utf-8")
    return generated


def _write_service_envs(workspace: Workspace) -> list[Path]:
    """Write each service's and frontend's .env from the resource graph.

    The compose file lists ``./<service>/.env`` as an ``env_file``, and compose
    treats a missing one as an error rather than an empty set -- so a project
    that has never run ``jfast workspace env`` cannot ``docker compose up`` at
    all. Generating them alongside the compose file keeps the two consistent by
    construction instead of by instruction.
    """
    written: list[Path] = []
    url = workspace.api_base_url()

    for backend in workspace.services:
        # Written even when the service binds nothing -- the gateway is the
        # ordinary case. Compose names every service's env_file unconditionally
        # and treats a missing one as an error rather than an empty set, so
        # skipping the empty ones makes `docker compose config` fail.
        variables = workspace.environment_for(backend)
        env_path = Path(backend.path) / ".env"
        body = "# Written by jfast from jfast.workspace.toml.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(variables.items()))
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        written.append(env_path)

    for frontend in workspace.frontends:
        env_path = Path(frontend.path) / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "# Written by jfast from jfast.workspace.toml.\n"
            f"VITE_API_URL={url}\n"
            f"VITE_APP_NAME={frontend.name.replace('_', ' ').title()}\n",
            encoding="utf-8",
        )
        written.append(env_path)

    return written
