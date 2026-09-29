"""`jfast workspace ...`, and `jfast link` / `jfast unlink`, which edit the
same file.
"""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework.cli.common import _echo
from jfastframework.cli.generate import _generate_gateway, _write_workspace_secrets
from jfastframework.cli.scaffold import to_snake
from jfastframework.graph import render_graph
from jfastframework.resources import RESOURCE_TYPES, Resource
from jfastframework.workspace import WORKSPACE_FILE, Workspace

workspace_app = typer.Typer(help="Manage a multi-service workspace.", no_args_is_help=True)


def _require_workspace() -> Workspace:
    workspace = Workspace.load_or_none()
    if workspace is None:
        typer.echo(
            f"No {WORKSPACE_FILE} found here or above. Create one with:\n"
            f"    jfast workspace init <name>",
            err=True,
        )
        raise typer.Exit(1)
    return workspace


@workspace_app.command("init")
def workspace_init(
    name: str = typer.Argument(..., help="Workspace name."),
    base_port: int = typer.Option(8000, "--base-port", help="First port block starts above this."),
) -> None:
    """Create jfast.workspace.toml in the current directory."""
    path = Path(WORKSPACE_FILE)
    if path.exists():
        typer.echo(f"{path} already exists.", err=True)
        raise typer.Exit(1)
    # save() writes the .gitignore rule for the .env `workspace env` generates.
    workspace = Workspace(name=to_snake(name), base_port=base_port, file=path)
    workspace.save()

    typer.echo(
        f"created           {path}\n"
        f"\nServices created from here register themselves, get a free port block,\n"
        f"and a gateway is generated once there is more than one backend.\n"
        f"\n    jfast new service billing --with database\n"
        f"    jfast new service admin --kind spa --frontend vue"
    )


@workspace_app.command("list")
def workspace_list(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show every service, its kind and its port block."""
    workspace = _require_workspace()
    rows = [
        f"{s.name:<16} {s.kind:<8} :{s.port:<6} {s.path}"
        + (f"  ({s.frontend})" if s.frontend else "")
        for s in workspace.services
    ]
    human = "\n".join(
        [
            f"workspace : {workspace.name}",
            f"api url   : {workspace.api_base_url()}",
            "",
            *(rows or ["no services yet"]),
        ]
    )
    _echo(workspace.describe(), json_out, human)


@workspace_app.command("gateway")
def workspace_gateway(
    force: bool = typer.Option(False, "--force", help="Rewrite an existing gateway's routes."),
) -> None:
    """Generate or refresh the API gateway from the workspace's backends."""
    workspace = _require_workspace()

    if len(workspace.backends) < 2 and workspace.gateway is None:
        typer.echo(
            f"Only {len(workspace.backends)} backend service. A gateway would add a hop\n"
            f"and an outage surface for nothing -- skipping. It is generated\n"
            f"automatically once a second backend exists."
        )
        raise typer.Exit(0)

    if workspace.gateway is not None and not force:
        typer.echo(
            "A gateway already exists. Re-run with --force to rewrite its routes\n"
            "from the current workspace."
        )
        raise typer.Exit(0)

    _generate_gateway(workspace, force=force)


@workspace_app.command("compose")
def workspace_compose(
    output: Path = typer.Option(Path("docker-compose.yml"), "--output", "-o"),
    caddy: bool = typer.Option(True, "--caddy/--no-caddy", help="Include Caddy at the edge."),
    stdout: bool = typer.Option(False, "--stdout", help="Print instead of writing."),
) -> None:
    """One compose file for every service in the workspace."""
    from jfastframework.deploy.workspace import render_workspace_compose

    workspace = _require_workspace()
    rendered = render_workspace_compose(workspace, with_caddy=caddy)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@workspace_app.command("caddy")
def workspace_caddy(
    output: Path = typer.Option(Path("Caddyfile"), "--output", "-o"),
    hostname: str = typer.Option("localhost", "--hostname", "-H"),
    production: bool = typer.Option(
        False, "--production", help="Enable automatic HTTPS (needs a real hostname and DNS)."
    ),
    wildcard_tenants: bool = typer.Option(
        False,
        "--wildcard-tenants",
        help="Serve *.HOST as tenant subdomains, with on-demand TLS.",
    ),
    stdout: bool = typer.Option(False, "--stdout"),
) -> None:
    """Caddyfile putting the whole workspace behind one hostname.

    Caddy is the edge: TLS, HTTP/3, compression, the built SPA. The JFast
    gateway, when there is one, is the application proxy behind it.
    """
    from jfastframework.deploy.workspace import render_caddyfile

    workspace = _require_workspace()
    rendered = render_caddyfile(
        workspace,
        hostname=hostname,
        local_dev=not production,
        wildcard_tenants=wildcard_tenants,
    )
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@workspace_app.command("k8s")
def workspace_k8s(
    output: Path = typer.Option(Path("k8s"), "--output", "-o", help="Directory to write into."),
    namespace: str | None = typer.Option(None, "--namespace", "-n"),
    host: str = typer.Option("example.com", "--host", "-H", help="Ingress hostname."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing manifests."),
) -> None:
    """Kubernetes manifests for the whole workspace, as a kustomize tree.

    Databases are deliberately not generated -- the README it writes says why.
    """
    from jfastframework.deploy.kubernetes import build as build_k8s

    workspace = _require_workspace()
    files = build_k8s(workspace, namespace=namespace, host=host)

    for relative, contents in sorted(files.items()):
        destination = output / relative
        if destination.exists() and not force:
            typer.echo(f"  skipped (exists) {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
        typer.echo(f"  created          {destination}")

    typer.echo(
        f"\n{len(files)} file(s) in {output}/.\n"
        f"\n    kubectl apply -k {output}/overlays/dev\n"
        f"\nBefore production: set real images (not :latest), wire your secret\n"
        f"manager, and point the DSNs at a managed database. {output}/README.md\n"
        f"explains why the database is not generated."
    )


@workspace_app.command("validate")
def workspace_validate() -> None:
    """Check the resource graph before anything is generated from it.

    A port claimed twice, a binding to a resource that does not exist, two
    resources landing in the same variable, a resource nobody uses. Each one
    produces output that is wrong in a way nobody notices until a container
    fails to start.
    """
    workspace = _require_workspace()
    problems = workspace.validate()
    if not problems:
        count = len(workspace.all_resources())
        typer.echo(f"OK  {workspace.name}: {len(workspace.services)} services, {count} resources")
        raise typer.Exit(0)
    for problem in problems:
        typer.echo(f"  {problem}")
    typer.echo(f"\n{len(problems)} problem(s).")
    raise typer.Exit(1)


@workspace_app.command("migrate-resources")
def workspace_migrate_resources(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the result, write nothing."),
) -> None:
    """Rewrite legacy per-service `datastores` lists as named resources.

    Ports are preserved, so the compose file this produces afterwards is the
    one it produced before. Idempotent: running it twice changes nothing.
    """
    workspace = _require_workspace()
    if not any(service.datastores for service in workspace.services):
        typer.echo("Nothing to migrate: no service declares a legacy `datastores` list.")
        raise typer.Exit(0)

    promoted = workspace.migrate_resources()
    if dry_run:
        typer.echo(workspace.render())
        raise typer.Exit(0)

    workspace.save()
    for resource in promoted:
        typer.echo(f"  resource          {resource.name}  ({resource.type}, port {resource.port})")
    typer.echo(f"\nwrote {workspace.file}")
    typer.echo(
        "Regenerate what derives from it:\n    jfast workspace compose && jfast workspace env"
    )


@workspace_app.command("resource")
def workspace_resource(
    name: str = typer.Argument(..., help="Name for the instance, e.g. core-db."),
    type_: str = typer.Option("postgres", "--type", "-t", help="postgres | redis | mongo | qdrant"),
    port: int | None = typer.Option(None, "--port", help="Published port. Allocated if omitted."),
    image: str = typer.Option("", "--image", help="Override the default image."),
    database: str = typer.Option("", "--database", help="Database name, for postgres and mongo."),
    remove: bool = typer.Option(False, "--remove", help="Delete the resource instead."),
) -> None:
    """Add a datastore instance the workspace owns.

    It is attached to nothing until something is linked to it, which is the
    point: a second database is now a thing you can name.
    """
    workspace = _require_workspace()

    if remove:
        existing = workspace.resource(name)
        if existing is None:
            typer.echo(f"No resource named {name!r}.")
            raise typer.Exit(1)
        holders = [s.name for s in workspace.services if any(b.resource == name for b in s.uses)]
        if holders:
            typer.echo(
                f"{name!r} is still used by {', '.join(sorted(holders))}. "
                f"Unlink it first:\n    jfast unlink {holders[0]} {name}"
            )
            raise typer.Exit(1)
        workspace.resources.remove(existing)
        workspace.save()
        typer.echo(f"  removed           {name}")
        typer.echo(
            "The container and its volume are not deleted. `docker compose down -v` does that."
        )
        raise typer.Exit(0)

    if type_ not in RESOURCE_TYPES:
        known = ", ".join(sorted(RESOURCE_TYPES))
        typer.echo(f"Unknown type {type_!r}. Known: {known}.")
        raise typer.Exit(1)

    resource = Resource(
        name=name,
        type=type_,
        port=port if port is not None else workspace.next_resource_port(),
        image=image,
        database=database,
    )
    try:
        workspace.add_resource(resource)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace.save()
    typer.echo(f"  resource          {resource.name}  ({resource.type}, port {resource.port})")
    if resource.spec.needs_credentials:
        typer.echo(f"  secret            {resource.secret_var}  (set it in the workspace .env)")
    typer.echo(f"\nConnect a service to it:\n    jfast link <service> {resource.name}")


def link_resource(
    service: str = typer.Argument(..., help="Service that needs the resource."),
    resource: str = typer.Argument(..., help="Resource it should reach."),
    as_: str = typer.Option("", "--as", help="Variable to bind it to. Defaults per type."),
) -> None:
    """Connect a service to a resource, and regenerate what depends on that.

    This is the answer to "I added a second database -- which service talks to
    it?". The binding is the only place that question has an answer, and the
    .env, the compose file and the graph all read it.
    """
    workspace = _require_workspace()
    try:
        binding = workspace.link(service, resource, env=as_)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace.save()
    entry = workspace.get(service)
    assert entry is not None
    instance = workspace.resource(resource)
    assert instance is not None
    typer.echo(f"  linked            {service} -> {resource}")
    typer.echo(f"  variable          {binding.resolved_env(instance)}")
    typer.echo("\nRegenerate:\n    jfast workspace compose && jfast workspace env")


def unlink_resource(
    service: str = typer.Argument(...),
    resource: str = typer.Argument(...),
) -> None:
    """Disconnect a service from a resource."""
    workspace = _require_workspace()
    if not workspace.unlink(service, resource):
        typer.echo(f"{service!r} was not bound to {resource!r}.")
        raise typer.Exit(1)
    workspace.save()
    typer.echo(f"  unlinked          {service} -> {resource}")


@workspace_app.command("graph")
def workspace_graph(
    output_format: str = typer.Option("mermaid", "--format", "-f", help="mermaid | dot"),
) -> None:
    """Draw the workspace: services, resources, and the variable between them.

    Text, not an image. A committed PNG is a blob nobody can review and that
    goes stale in silence; mermaid renders on GitHub and diffs line by line.
    """
    workspace = _require_workspace()
    typer.echo(render_graph(workspace, output_format=output_format))


@workspace_app.command("env")
def workspace_env(
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Rewrite every frontend's .env from the workspace.

    The API base URL is the gateway when there is one and the single backend
    when there is not -- which is exactly the value that goes stale by hand the
    day a gateway appears.
    """
    workspace = _require_workspace()
    url = workspace.api_base_url()

    written = _write_workspace_secrets(workspace, dry_run=dry_run)
    if written:
        typer.echo(f"  secrets           .env  ({written} generated, existing values kept)")

    # Backends first: their connection strings are derived from the resource
    # bindings, and used to be the one generated thing left to a human.
    for backend in workspace.services:
        # A service that binds nothing still gets a file: the compose generator
        # names ./<service>/.env for every one of them, and compose fails on a
        # missing env_file rather than treating it as empty.
        variables = workspace.environment_for(backend)
        env_path = Path(backend.path) / ".env"
        body = "# Written by `jfast workspace env` from jfast.workspace.toml.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(variables.items()))
        if dry_run:
            typer.echo(f"would write {env_path}:\n{body}")
            continue
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        typer.echo(f"  wrote             {env_path}  ({len(variables)} variable(s))")

    if not workspace.frontends:
        raise typer.Exit(0)

    for frontend in workspace.frontends:
        env_path = Path(frontend.path) / ".env"
        body = (
            "# Written by `jfast workspace env` from jfast.workspace.toml.\n"
            f"VITE_API_URL={url}\n"
            f"VITE_APP_NAME={frontend.name.replace('_', ' ').title()}\n"
        )
        if dry_run:
            typer.echo(f"would write {env_path}:\n{body}")
            continue
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        typer.echo(f"  wrote             {env_path}  (VITE_API_URL={url})")


def register(app: typer.Typer) -> None:
    """Add the `workspace` group, `link` and `unlink` to *app*."""
    app.add_typer(workspace_app, name="workspace")
    app.command("link")(link_resource)
    app.command("unlink")(unlink_resource)
