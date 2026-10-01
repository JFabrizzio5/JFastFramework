"""`jfast deploy ...`: deployment artifacts for one service."""

from __future__ import annotations

from pathlib import Path

import typer

from jfastframework.cli.common import _load
from jfastframework.settings import DEFAULT_CONFIG_FILE

deploy_app = typer.Typer(help="Generate deployment artifacts.", no_args_is_help=True)


@deploy_app.command("compose")
def deploy_compose(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    output: Path = typer.Option(Path("docker-compose.generated.yml"), "--output", "-o"),
    base_port: int | None = typer.Option(None, "--base-port"),
    stdout: bool = typer.Option(False, "--stdout", help="Print instead of writing."),
) -> None:
    """Generate docker-compose.yml from the enabled plugin graph."""
    from jfastframework.deploy import build_compose, render_compose
    from jfastframework.plugins import registry

    cfg = _load(config)
    instances = registry.build(cfg)
    rendered = render_compose(build_compose(cfg, instances, base_port=base_port))
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")

    # The api service is `build: .`, so a compose file without a Dockerfile
    # beside it cannot come up -- `docker compose up` stops at "failed to read
    # dockerfile" before a single container starts. A project whose scaffold
    # did not write one finds out here rather than at the first build.
    dockerfile = output.parent / "Dockerfile"
    if not dockerfile.exists():
        typer.echo(f"no {dockerfile}; `docker compose up` cannot build the api service")
        typer.echo("  jfast deploy dockerfile")


@deploy_app.command("dockerfile")
def deploy_dockerfile(
    output: Path = typer.Option(Path("Dockerfile"), "--output", "-o"),
    python: str = typer.Option("3.12", "--python"),
    stdout: bool = typer.Option(False, "--stdout"),
    config: str = typer.Option(
        DEFAULT_CONFIG_FILE,
        "--config",
        "-c",
        help="Its local storage disks are created in the image, owned by the app user.",
    ),
) -> None:
    """Generate a production Dockerfile and the .dockerignore it needs."""
    from jfastframework.deploy import render_dockerfile, render_dockerignore
    from jfastframework.deploy.compose import read_storage_disks, storage_dirs

    # Every local disk root in jfast.toml, not only the defaults: the image
    # runs as appuser, and a volume mounted where the image has no directory
    # is created as root -- /ready 503, and the first upload a 500.
    disks = read_storage_disks(Path(config))
    rendered = render_dockerfile(python, disks=disks)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}  (storage: {' '.join(storage_dirs(disks))})")

    # Written next to the Dockerfile, never over an existing one: the exclude
    # list is a thing people edit, and silently replacing an edited copy is
    # how a build starts shipping a directory somebody had excluded. The
    # Dockerfile is generated and says so; this is generated once and owned.
    ignore = output.parent / ".dockerignore"
    if ignore.exists():
        typer.echo(f"kept {ignore} (already present)")
    else:
        ignore.write_text(render_dockerignore(), encoding="utf-8")
        typer.echo(f"wrote {ignore}")


@deploy_app.command("function")
def deploy_function(
    name: str = typer.Argument(..., help="Function / Cloud Run service name."),
    target: str = typer.Option("aws", "--target", "-t", help="aws | gcp"),
    region: str = typer.Option("us-east-1", "--region"),
    account_id: str = typer.Option("", "--account-id", help="AWS account id (12 digits)."),
    project: str = typer.Option("", "--project", help="GCP project id."),
    memory: int = typer.Option(512, "--memory", help="Memory in MB."),
    timeout: int = typer.Option(30, "--timeout", help="Request timeout in seconds."),
    public: bool = typer.Option(
        False, "--public", help="Expose without authentication. Off by default."
    ),
    output: Path = typer.Option(Path("."), "--output", "-o"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Generate the files that deploy this service as a serverless function.

    It writes scripts; it does not run them. Read them before you do -- they
    are the only generated artifacts that spend money.
    """
    from jfastframework.deploy.serverless import FunctionConfig, render

    try:
        files = render(
            FunctionConfig(
                name=name,
                target=target,
                region=region,
                account_id=account_id,
                project=project,
                memory_mb=memory,
                timeout_seconds=timeout,
                public=public,
            )
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    output.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        destination = output / relative
        if destination.exists() and not force:
            typer.secho(f"skipped {destination} (exists; --force to overwrite)", fg="yellow")
            continue
        destination.write_text(content, encoding="utf-8")
        if destination.suffix == ".sh":
            destination.chmod(0o755)
        typer.echo(f"wrote {destination}")

    if public:
        typer.secho(
            "This function will be reachable by anyone with the URL. "
            "Enable the auth plugin, or drop --public.",
            fg=typer.colors.YELLOW,
        )
    if target == "aws":
        typer.echo("\nAdd 'mangum' to requirements.txt, then: ./deploy-lambda.sh")
    else:
        typer.echo("\nThen: ./deploy-cloudrun.sh")


def register(app: typer.Typer) -> None:
    """Add the `deploy` group to *app*."""
    app.add_typer(deploy_app, name="deploy")
