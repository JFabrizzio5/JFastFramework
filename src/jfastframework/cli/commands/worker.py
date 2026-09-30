"""`jfast worker` and `jfast jobs ...`: consume the queue, and see what died in it.

The ``queue`` plugin is on in every generated service, and before this command
nothing consumed it: jobs accumulated in ``jfast_jobs`` while each project
wrote its own ``worker.py``. The worker here boots the *same* app ``jfast
serve`` does -- the same ``main:app``, the same lifespan -- so the tasks and
subscribers it runs are exactly the ones the API queues for, with the same
plugins, the same database and the same tenant handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json as jsonlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import typer

from jfastframework.cli.exits import Code
from jfastframework.queues.worker import DEFAULT_DRAIN_SECONDS, Worker
from jfastframework.settings import DEFAULT_CONFIG_FILE

logger = logging.getLogger("jfast.queue")

jobs_app = typer.Typer(
    help="The job queue's dead letters: list them, and put them back.",
    no_args_is_help=True,
)


def _enter_service(path: Path) -> Path:
    """Change into the service and make ``main`` importable, as `jfast serve` does.

    ``create_app()`` resolves ``jfast.toml`` against the working directory, so a
    worker started anywhere else would boot on framework defaults -- no
    database, no queue -- and wait forever on nothing.
    """
    service_dir = path.resolve()
    if not (service_dir / DEFAULT_CONFIG_FILE).is_file():
        typer.echo(
            f"No {DEFAULT_CONFIG_FILE} in {service_dir}. Run this from a service directory, "
            f"or point at one with --path.",
            err=True,
        )
        raise typer.Exit(Code.CONFIG)
    os.chdir(service_dir)
    if str(service_dir) not in sys.path:
        sys.path.insert(0, str(service_dir))
    return service_dir


def load_app(app_path: str) -> Any:
    """``module:attribute`` -> the ASGI app, with the failure named."""
    module_name, _, attribute = app_path.partition(":")
    if not module_name or not attribute:
        typer.echo(f"--app {app_path!r} is not module:attribute (e.g. main:app).", err=True)
        raise typer.Exit(Code.USAGE)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        typer.echo(f"{module_name} has no attribute {attribute!r}.", err=True)
        raise typer.Exit(Code.USAGE) from None


@contextlib.asynccontextmanager
async def running_app(app: Any) -> AsyncIterator[Any]:
    """The app's lifespan -- every plugin started -- and its context."""
    from jfastframework.app import get_context

    async with app.router.lifespan_context(app):
        yield get_context(app)


def _queue_of(ctx: Any) -> tuple[Any, Any]:
    if not ctx.has("queue"):
        typer.echo(
            'This service has no queue: add "queue" to [plugins].enabled in jfast.toml.',
            err=True,
        )
        raise typer.Exit(Code.CONFIG)
    return ctx.require("queue"), ctx.require("tasks")


async def run_worker(
    app: Any,
    *,
    concurrency: int = 4,
    drain_timeout: float = DEFAULT_DRAIN_SECONDS,
    poll_timeout: float = 1.0,
    install_signals: bool = True,
) -> None:
    """Boot ``app``'s lifespan and consume its queue until SIGTERM or SIGINT.

    The first signal stops claiming and drains; a second one stops waiting and
    releases whatever is still running, for an operator who means it.
    """
    async with running_app(app) as ctx:
        backend, registry = _queue_of(ctx)
        worker = Worker(
            backend,
            registry,
            concurrency=concurrency,
            poll_timeout=poll_timeout,
            drain_timeout=drain_timeout,
            # The queue plugin owns the backend and closes it at shutdown.
            close_backend=False,
        )
        if install_signals:
            loop = asyncio.get_running_loop()

            def stop() -> None:
                if worker.stopping:
                    worker.drain_timeout = 0
                    logger.warning("second signal: releasing in-flight jobs now")
                else:
                    logger.info("signal received: finishing in-flight jobs, claiming no more")
                worker.stop()

            for sig in (signal.SIGTERM, signal.SIGINT):
                # Windows' event loop has no signal handlers; Ctrl-C there
                # raises KeyboardInterrupt instead, and the lifespan still exits.
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop)
        tasks = ", ".join(registry.names) or "none"
        logger.info("worker consuming %s with concurrency %d", tasks, concurrency)
        await worker.run()


def worker(
    path: Path = typer.Option(
        Path("."), "--path", "-p", help="Service directory. Defaults to the current one."
    ),
    app_path: str = typer.Option("main:app", "--app", help="Import path of the ASGI app."),
    concurrency: int = typer.Option(4, "--concurrency", "-c", min=1, help="Jobs at once."),
    grace: float = typer.Option(
        DEFAULT_DRAIN_SECONDS,
        "--grace",
        min=0,
        help="Seconds in-flight jobs get to finish on SIGTERM before they are released.",
    ),
) -> None:
    """Run this service's background jobs and event subscribers.

    Boots the same app `jfast serve` does, registers every module's `@task`
    and `@subscribe` (from `modules/<name>/tasks.py`), and consumes the queue.
    On SIGTERM it stops claiming, gives running jobs `--grace` seconds, and
    returns the rest to the queue without spending an attempt. Keep `--grace`
    below the orchestrator's kill deadline (Kubernetes: 30 s by default).
    """
    _enter_service(path)
    app = load_app(app_path)
    asyncio.run(run_worker(app, concurrency=concurrency, drain_timeout=grace))


# ---------------------------------------------------------------------------
# jfast jobs
# ---------------------------------------------------------------------------


def _dead_letter_backend(backend: Any, settings_backend: str) -> Any:
    from jfastframework.queues.base import DeadLetters

    if isinstance(backend, DeadLetters):
        return backend
    if settings_backend == "rabbitmq":
        hint = (
            "RabbitMQ keeps them in the '<name>.dead' queue: inspect and move them from the "
            "management UI (port 15672), or with rabbitmqadmin."
        )
    else:
        hint = "the backend does not implement dead() and retry_dead()."
    typer.echo(f"Dead letters cannot be listed for the {settings_backend!r} queue: {hint}")
    raise typer.Exit(Code.USAGE)


def _load_quietly(app_path: str) -> Any:
    """Build the app with its log handler on stderr: stdout carries the answer.

    The observability plugin logs to whatever ``sys.stdout`` is when the app is
    built -- right for a server, wrong for `jfast jobs dead --json | jq`, where
    the boot's own log lines would be parsed as the payload.
    """
    with contextlib.redirect_stdout(sys.stderr):
        return load_app(app_path)


def _backend_name(ctx: Any) -> str:
    raw = ctx.config.plugin_config("queue").get("backend", "postgres")
    return str(raw)


async def _dead(app: Any, limit: int) -> list[dict[str, Any]]:
    async with running_app(app) as ctx:
        backend, _ = _queue_of(ctx)
        letters = _dead_letter_backend(backend, _backend_name(ctx))
        return [job.describe() for job in await letters.dead(limit=limit)]


async def _retry(app: Any, ids: list[str] | None) -> int:
    async with running_app(app) as ctx:
        backend, _ = _queue_of(ctx)
        letters = _dead_letter_backend(backend, _backend_name(ctx))
        count: int = await letters.retry_dead(ids)
        return count


@jobs_app.command("dead")
def jobs_dead(
    path: Path = typer.Option(Path("."), "--path", "-p", help="Service directory."),
    app_path: str = typer.Option("main:app", "--app", help="Import path of the ASGI app."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="At most this many."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List jobs that ran out of attempts, newest first, with why they failed."""
    _enter_service(path)
    app = _load_quietly(app_path)
    found = asyncio.run(_dead(app, limit))
    if json_out:
        typer.echo(jsonlib.dumps(found, indent=2, default=str))
        return
    if not found:
        typer.echo("no dead jobs")
        return
    for job in found:
        tenant = f"  tenant {job['tenant_id']}" if job["tenant_id"] else ""
        typer.echo(f"{job['id']}  {job['task']}  {job['attempts']}/{job['max_attempts']}{tenant}")
        typer.echo(f"    {job['error'] or '(no error recorded)'}")
    typer.echo(f"\n{len(found)} dead. Replay one with `jfast jobs retry <id>`, all with --all.")


@jobs_app.command("retry")
def jobs_retry(
    ids: list[str] = typer.Argument(None, help="Dead job ids, from `jfast jobs dead`."),
    all_jobs: bool = typer.Option(False, "--all", help="Retry every dead job."),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Service directory."),
    app_path: str = typer.Option("main:app", "--app", help="Import path of the ASGI app."),
) -> None:
    """Put dead jobs back in the queue with their attempts reset.

    Fix the cause first: a job that died for a reason still true dies again.
    """
    if not ids and not all_jobs:
        typer.echo("Name the job ids to retry, or pass --all.", err=True)
        raise typer.Exit(Code.USAGE)
    if ids and all_jobs:
        typer.echo("Pass job ids or --all, not both.", err=True)
        raise typer.Exit(Code.USAGE)
    _enter_service(path)
    app = _load_quietly(app_path)
    count = asyncio.run(_retry(app, None if all_jobs else list(ids)))
    if ids and count < len(ids):
        typer.echo(f"returned {count} of {len(ids)}: the others are not dead jobs (any more).")
    else:
        typer.echo(f"returned {count} job(s) to the queue")


def register(app: typer.Typer) -> None:
    """Attach `worker` and the `jobs` group to *app*."""
    app.command()(worker)
    app.add_typer(jobs_app, name="jobs")
