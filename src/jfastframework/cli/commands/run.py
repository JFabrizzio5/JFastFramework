"""`jfast serve`, `jfast dev` and `jfast doctor`: running this service locally,
and checking that it can run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import typer

from jfastframework.cli import check as check_cli
from jfastframework.cli import dev as devtools
from jfastframework.cli import ui
from jfastframework.cli.common import _echo, _load
from jfastframework.cli.exits import Code
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig
from jfastframework.workspace import Workspace

# Vite's own default. Only what `jfast dev` prints depends on it: the port is
# left to vite unless --web-port asks for another one.
WEB_PORT = 5173


def serve(
    path: Path = typer.Option(
        Path("."), "--path", "-p", help="Service directory. Defaults to the current one."
    ),
    port: int | None = typer.Option(None, "--port", help="Overrides the port in jfast.toml."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    reload: bool = typer.Option(True, "--reload/--no-reload", help="Restart on a file change."),
    app_path: str = typer.Option("main:app", "--app", help="Import path of the ASGI app."),
) -> None:
    """Run this service locally.

    ``create_app()`` resolves ``jfast.toml`` against the working directory, so a
    service started from anywhere else boots with framework defaults -- the name
    ``jfast-service``, two plugins, no database, no cache -- and says nothing
    about it. The symptom is a service that runs and is missing everything.

    This changes into the service directory before importing, and refuses to
    start when there is no ``jfast.toml`` there, which is the case that used to
    boot silently wrong.

    The default host is loopback rather than ``0.0.0.0``: a development server
    should not be reachable from the rest of the network unless you say so.
    """
    service_dir = path.resolve()
    if not service_dir.is_dir():
        typer.echo(f"{service_dir} is not a directory.", err=True)
        raise typer.Exit(1)

    config_file = service_dir / DEFAULT_CONFIG_FILE
    if not config_file.is_file():
        typer.echo(
            f"No {DEFAULT_CONFIG_FILE} in {service_dir}.\n"
            f"\nRun this from a service directory, or point at one:\n"
            f"    jfast serve --path ./billing\n"
            f"\nStarting anyway would boot with framework defaults and no database,"
            f"\nwhich looks like it worked.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on the install
        typer.echo(
            "uvicorn is not installed. Add the server extra:\n"
            '    pip install "jfastframework[server]"',
            err=True,
        )
        raise typer.Exit(1) from exc

    # chdir alone is not enough. Python fixes sys.path[0] to wherever the CLI
    # lives when the interpreter starts, so `main` would not be importable;
    # and with --reload uvicorn spawns a child that builds its own sys.path, so
    # the directory has to travel in PYTHONPATH to survive the reload.
    os.chdir(service_dir)
    sys.path.insert(0, str(service_dir))
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        f"{service_dir}{os.pathsep}{existing}" if existing else str(service_dir)
    )

    settings = JFastConfig.load(config_path=DEFAULT_CONFIG_FILE).settings
    resolved_port = port if port is not None else settings.port

    typer.echo(
        f"  {settings.app_name}  ({settings.env})\n"
        f"  http://{host}:{resolved_port}\n"
        f"  docs      {settings.effective_docs_url or 'closed in this environment'}\n"
        f"  probes    /health  /ready\n"
    )
    uvicorn.run(
        app_path,
        host=host,
        port=resolved_port,
        reload=reload,
        reload_dirs=[str(service_dir)] if reload else None,
        # uvicorn ships this on, with loopback trusted, and it rewrites the
        # client address from X-Forwarded-For before any application middleware
        # runs -- so trusted_proxies would never see a real peer. This host is
        # loopback by default, which is exactly the address uvicorn believes:
        # a rate limit validated here would pass for the wrong reason and fail
        # in production. There is no flag to put it back; trusted_proxies in
        # jfast.toml is the one place that policy is written.
        proxy_headers=False,
    )


def dev(
    path: Path = typer.Option(
        Path("."), "--path", "-p", help="Service directory. Defaults to the current one."
    ),
    frontend: Path | None = typer.Option(
        None, "--frontend", help="Frontend directory. Found from the workspace when omitted."
    ),
    port: int | None = typer.Option(None, "--port", help="Overrides the port in jfast.toml."),
    web_port: int | None = typer.Option(
        None, "--web-port", help=f"Frontend dev server port. Vite's {WEB_PORT} when omitted."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    infra: bool = typer.Option(True, "--infra/--no-infra", help="Bring up database and cache."),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="Run alembic upgrade head."),
    web: bool = typer.Option(True, "--web/--no-web", help="Also start the frontend dev server."),
) -> None:
    """Everything needed to develop: infrastructure, migrations, API and frontend.

    `jfast serve` starts the backend and nothing else. This is the other thing:
    the four steps somebody does every morning, in the order that makes the
    failures land where they belong.

    Every stage degrades rather than blocking. No Docker, no compose file, no
    Alembic, no frontend -- each is announced and skipped, and what remains
    still runs. The one stage that does stop the run is a failing migration:
    booting against a schema that is behind produces errors in requests that
    have nothing to do with it.

    Both servers can be moved: `--port` for the API, `--web-port` for the
    frontend. Without the second one, a machine already using 5173 left
    `--no-web` as the only way through, which gives up half the command.
    """
    service_dir = path.resolve()
    config_file = service_dir / DEFAULT_CONFIG_FILE
    if not config_file.is_file():
        ui.warn(f"No {DEFAULT_CONFIG_FILE} in {service_dir}.")
        ui.note("Run this from a service directory, or point at one:")
        ui.note("    jfast dev --path ./billing")
        raise typer.Exit(1)

    ui.banner("everything needed to develop, in order")

    settings = JFastConfig.load(config_path=str(config_file)).settings
    resolved_port = port if port is not None else settings.port
    workspace = Workspace.load_or_none()

    processes: list[devtools.Process] = []

    # -- infrastructure --------------------------------------------------
    compose_file = _find_compose(service_dir)
    if not infra:
        ui.note("infra    skipped (--no-infra)")
    elif compose_file is None:
        ui.note("infra    skipped: no docker-compose.yml found")
    elif not devtools.docker_available():
        ui.note("infra    skipped: docker is not on PATH")
    else:
        services = devtools.compose_services(compose_file, ("-database", "-cache"))
        target = services or []
        ui.step(f"starting {', '.join(target) if target else 'every compose service'}")
        try:
            with ui.working("waiting for containers"):
                devtools.run(
                    ["docker", "compose", "-f", str(compose_file), "up", "-d", *target],
                    cwd=compose_file.parent,
                    what="docker compose up",
                )
                healthy = devtools.wait_for_healthy(compose_file, compose_file.parent)
        except devtools.DevError as exc:
            ui.warn(str(exc))
            raise typer.Exit(1) from exc
        if healthy:
            ui.created("infra", "up and healthy")
        else:
            ui.warn("containers did not report healthy; continuing anyway")

    # Everything from here runs on the host, where the generated .env is wrong
    # twice over: it addresses containers by service name, and leaves the
    # password as a ${...} only compose interpolates. Computed once, because
    # Alembic needs the same translation the server does -- and it runs first,
    # so getting this only onto the server means the migration fails with a DNS
    # error naming a host that was never meant to resolve here.
    env = {"PYTHONPATH": str(service_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if compose_file is not None:
        env.update(
            devtools.host_environment(
                service_dir / ".env", compose_file.parent / ".env", compose_file
            )
        )

    # -- migrations ------------------------------------------------------
    if not migrate:
        ui.note("migrate  skipped (--no-migrate)")
    elif not (service_dir / "alembic.ini").is_file():
        ui.note("migrate  skipped: no alembic.ini")
    else:
        try:
            with ui.working("alembic upgrade head"):
                devtools.run(
                    [devtools.python_executable(), "-m", "alembic", "upgrade", "head"],
                    cwd=service_dir,
                    what="alembic upgrade head",
                    env=env,
                )
        except devtools.DevError as exc:
            # The one hard stop. A server on a stale schema fails later, in a
            # request that has nothing to do with the missing column.
            ui.warn(str(exc))
            raise typer.Exit(1) from exc
        ui.created("schema", "at head")

    # -- the servers -----------------------------------------------------
    processes.append(
        devtools.spawn(
            [
                devtools.python_executable(),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                host,
                "--port",
                str(resolved_port),
                "--reload",
                # Same reason as `jfast serve`: uvicorn's own X-Forwarded-For
                # handling replaces the client address before trusted_proxies
                # can decide, and it trusts the loopback address this binds.
                "--no-proxy-headers",
            ],
            cwd=service_dir,
            name="api",
            env=env,
        )
    )

    front_dir = frontend or _find_frontend(service_dir, workspace)
    resolved_web_port = web_port if web_port is not None else WEB_PORT
    if not web:
        ui.note("web      skipped (--no-web)")
    elif front_dir is None:
        ui.note("web      skipped: no frontend project found")
    elif not (front_dir / "node_modules").is_dir():
        ui.warn(f"{front_dir}/node_modules is missing. Run npm install there first.")
    else:
        # The bare `--` is npm's, not vite's: without it npm eats the flag
        # instead of forwarding it to the script.
        command = ["npm", "run", "dev"]
        if web_port is not None:
            command += ["--", "--port", str(web_port)]
        processes.append(devtools.spawn(command, cwd=front_dir, name="web"))

    ui.next_steps(
        "Running",
        [
            (f"http://{host}:{resolved_port}", "the API"),
            (f"http://{host}:{resolved_port}/docs", "its docs"),
            *(
                [(f"http://localhost:{resolved_web_port}", "the frontend")]
                if len(processes) > 1
                else []
            ),
            ("Ctrl-C", "stops everything it started" if len(processes) > 1 else "stops it"),
        ],
    )

    code = devtools.supervise(processes)
    ui.note("stopped")
    raise typer.Exit(code)


def _find_compose(service_dir: Path) -> Path | None:
    """The compose file for this service, which usually lives one level up.

    A workspace writes one compose file at its root covering every service, so
    looking only in the service directory finds nothing in the normal case.
    """
    for candidate in (service_dir, service_dir.parent):
        found = candidate / "docker-compose.yml"
        if found.is_file():
            return found
    return None


def _find_frontend(service_dir: Path, workspace: Workspace | None) -> Path | None:
    """The frontend project belonging to this service.

    The workspace knows, when there is one. Without it, fall back to the
    convention `jfast start` uses: a sibling directory named `<service>-web`.
    """
    if workspace is not None and workspace.file is not None:
        # Service paths in the workspace are relative to the workspace file, not
        # to the service being started.
        base = workspace.file.parent
        for entry in workspace.services:
            if entry.frontend:
                candidate = (base / entry.path).resolve()
                if (candidate / "package.json").is_file():
                    return candidate
    sibling = service_dir.parent / f"{service_dir.name}-web"
    if (sibling / "package.json").is_file():
        return sibling
    return None


def doctor(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Check that the configuration resolves and the service can be built."""
    from jfastframework.plugins import registry

    problems: list[str] = []
    checks: dict[str, Any] = {}

    config_path = Path(config)
    checks["config_file"] = (
        str(config_path) if config_path.is_file() else "missing (using env only)"
    )

    try:
        cfg = _load(config)
        checks["app_name"] = cfg.settings.app_name
        checks["env"] = cfg.settings.env
    except Exception as exc:
        problems.append(f"config failed to load: {exc}")
        _echo({"ok": False, "problems": problems}, json_out, f"FAIL  {problems[-1]}")
        raise typer.Exit(Code.CONFIG) from exc

    try:
        instances = registry.build(cfg)
        checks["plugins"] = [p.meta.name for p in instances]
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plugin graph failed to resolve: {exc}")
        instances = []

    broken = getattr(registry.discover, "broken", {})
    for name in cfg.settings.plugins:
        if name in broken:
            problems.append(f"plugin {name!r} is enabled but cannot import: {broken[name]}")

    # Resolving the graph instantiates the plugins; it does not register them,
    # and registration is where every plugin checks its own settings. A fresh
    # `jfast new service --with auth` resolves cleanly and then raises
    # `auth mode "jwks" needs jwks_url` on the first import of main.py -- so
    # the command whose job is to say "this is configured" would be answering
    # from the half of the boot that cannot fail on configuration. Building the app
    # runs the same code path `create_app` does, minus the lifespan: no
    # connection is opened and nothing is started.
    if not problems:
        build_failure = check_cli.build_error(cfg)
        if build_failure is not None:
            problems.append(f"the service cannot be built: {build_failure}")

    if "database" in checks.get("plugins", []):
        # The service pins its own sessions to UTC, so its answers are
        # right. Everything else touching that database -- psql, a BI tool,
        # a migration run by hand -- computes date_trunc, CURRENT_DATE and
        # now()::date in the server's zone, and reports a different day.
        # Not a failure of this service; worth saying once, out loud.
        import asyncio

        from jfastframework.plugins.builtin.database import (
            DatabaseSettings,
            server_timezone,
        )

        try:
            db_settings = DatabaseSettings(**cfg.plugin_config("database"))
            server, session = asyncio.run(
                server_timezone(
                    db_settings.dsn_for(db_settings.default_name()),
                    session_timezone=db_settings.session_timezone,
                )
            )
            checks["db_timezone"] = f"server {server}, session {session}"
            if server != "UTC":
                problems.append(
                    f"the database's TimeZone is {server!r}, not UTC. This service pins "
                    f"its own sessions, but every other client of that database computes "
                    f"date_trunc, CURRENT_DATE and now()::date in {server!r} and will "
                    f"report a different day."
                )
        except Exception as exc:  # noqa: BLE001 -- every failure reads the same here
            checks["db_timezone"] = f"unreachable ({exc})"

    ok = not problems
    payload = {"ok": ok, "checks": checks, "problems": problems}
    human_lines = [f"{k:<14} {v}" for k, v in checks.items()]
    human_lines += [f"PROBLEM        {p}" for p in problems]
    human_lines.append("OK" if ok else f"{len(problems)} problem(s)")
    _echo(payload, json_out, "\n".join(human_lines))
    if not ok:
        raise typer.Exit(Code.ENVIRONMENT)


def register(app: typer.Typer) -> None:
    """Attach `serve`, `dev` and `doctor` to *app*.

    `dev` reaches every process helper through ``devtools``, the module, not
    names copied out of it: that attribute is what a test replaces to run the
    command without starting a server.
    """
    app.command()(serve)
    app.command()(dev)
    app.command()(doctor)
