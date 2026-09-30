"""One command that gets a workspace from cold to running.

``jfast serve`` starts the backend and nothing else, which is right for what it
is and wrong for the thing people actually do in the morning: bring up the
database and cache, apply whatever migrations landed on the branch they just
pulled, then start the API and the frontend.

Doing that by hand is four terminals and one forgotten step, and the forgotten
step is always the migration -- so the failure arrives later, as a column that
does not exist, in a request that has nothing to do with it.

Design decisions worth keeping:

* **Every stage is skippable and every skip is announced.** No Docker on the
  machine, no compose file, no Alembic, no frontend: each one degrades to a
  printed line and the rest still runs. A dev command that refuses to start
  because the optional half is missing is a dev command people stop using.
* **A failing migration stops before the server starts.** Booting against a
  schema that is behind produces errors that point at the wrong place.
* **Ctrl-C kills the children.** Terminating the parent and leaving uvicorn
  holding the port is how the next start fails with "address already in use"
  and a confusing hunt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class DevError(RuntimeError):
    """A stage failed in a way that should stop the run, with the reason."""


@dataclass
class Process:
    name: str
    popen: subprocess.Popen[bytes]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def compose_services(compose_file: Path, suffixes: tuple[str, ...]) -> list[str]:
    """Infrastructure service names in a compose file, without parsing YAML.

    The generated file names them ``<workspace>-database`` and
    ``<workspace>-cache``, so matching on the suffix finds them without a YAML
    dependency and without hardcoding the workspace name. A hand-edited file
    that renamed them simply yields nothing, and the caller degrades to
    bringing the whole file up.
    """
    if not compose_file.is_file():
        return []
    found: list[str] = []
    for raw in compose_file.read_text(encoding="utf-8").splitlines():
        # Service keys sit at exactly two spaces of indentation under
        # `services:`; anything deeper is that service's own configuration.
        if not raw.startswith("  ") or raw.startswith("   ") or ":" not in raw:
            continue
        name = raw.strip().rstrip(":").strip()
        if name.endswith(suffixes):
            found.append(name)
    return found


def published_ports(compose_file: Path) -> dict[str, int]:
    """Map each compose service to the host port it publishes, if any.

    Line-based rather than YAML-parsed, for the same reason as
    :func:`compose_services`: this file is generated, its shape is known, and a
    YAML dependency in every install to read two fields is a poor trade. A
    hand-edited file in an unexpected shape yields fewer entries, and the
    caller degrades to leaving the URL alone.
    """
    ports: dict[str, int] = {}
    if not compose_file.is_file():
        return ports

    service: str | None = None
    in_ports = False
    for raw in compose_file.read_text(encoding="utf-8").splitlines():
        if raw.startswith("  ") and not raw.startswith("   ") and raw.rstrip().endswith(":"):
            service = raw.strip().rstrip(":").strip()
            in_ports = False
            continue
        if service is None:
            continue
        stripped = raw.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports and stripped.startswith("- "):
            mapping = stripped[2:].strip().strip('"').strip("'")
            host, _, _container = mapping.partition(":")
            if host.isdigit():
                ports.setdefault(service, int(host))
            in_ports = False
        elif stripped and not stripped.startswith("-"):
            in_ports = False
    return ports


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value pairs, ignoring comments and blanks."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def host_environment(service_env: Path, root_env: Path, compose_file: Path) -> dict[str, str]:
    """The service's environment, rewritten for a process running on the host.

    Two things are true of the generated ``.env`` and false for a host process:

    * It contains ``${WORKSPACE_DATABASE_PASSWORD}``. Compose interpolates that
      from the workspace ``.env``; nothing does it for a plain ``uvicorn``, so
      the DSN would reach asyncpg with the braces still in it.
    * It addresses containers by service name -- ``shop-database:5432`` -- which
      resolves on the compose network and nowhere else. From the host the same
      database is at ``localhost:<published port>``.

    So both are translated. Anything this cannot resolve is left exactly as it
    was: a wrong guess would be harder to debug than the original value.
    """
    secrets = read_env_file(root_env)
    ports = published_ports(compose_file)
    resolved: dict[str, str] = {}

    for key, value in read_env_file(service_env).items():
        for secret_key, secret_value in secrets.items():
            value = value.replace(f"${{{secret_key}}}", secret_value)
        for service, host_port in ports.items():
            # The container port is whatever follows the hostname; it is
            # replaced wholesale by the published one.
            value = re.sub(
                rf"(?<=[@/]){re.escape(service)}:\d+",
                f"localhost:{host_port}",
                value,
            )
        resolved[key] = value
    return resolved


#: Vite's own default, used when neither the dev script nor vite.config names one.
VITE_DEFAULT_PORT = 5173

_PORT_FLAG = re.compile(r"(?:^|\s)--port(?:=|\s+)(\d+)(?=\s|$)")
_CONFIG_PORT = re.compile(r"\bserver\s*:\s*\{[^}]*?\bport\s*:\s*(\d+)", re.DOTALL)


def frontend_port(front_dir: Path) -> tuple[int, bool]:
    """The port ``npm run dev`` starts Vite on, and whether the script pins it.

    The generated ``package.json`` runs ``vite --port <workspace port>`` and the
    generated ``vite.config`` repeats it under ``server.port``; Vite takes the
    command line over the config and the config over its own 5173. Announcing
    5173 because ``jfast dev`` did not pass a port printed a URL where nothing
    was listening. A hand-edited script or config that hides the number (a
    variable, another tool in front of vite) falls through to the next source.
    """
    script = ""
    package = front_dir / "package.json"
    try:
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        script = str(scripts.get("dev") or "")
    except (OSError, ValueError, AttributeError):
        script = ""
    found = _PORT_FLAG.findall(script)
    if found:
        # Vite keeps the last --port it is given.
        return int(found[-1]), True
    for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts"):
        config = front_dir / name
        if not config.is_file():
            continue
        match = _CONFIG_PORT.search(config.read_text(encoding="utf-8"))
        if match:
            return int(match.group(1)), False
    return VITE_DEFAULT_PORT, False


def frontend_command(front_dir: Path, web_port: int | None) -> tuple[list[str], int]:
    """``npm run dev``, with ``--port`` only when it changes something, and the
    port the frontend will actually be on.

    Appending ``--port`` to a script that already pins the same one produced
    ``vite --port 8610 --port 8610``. When the requested port differs from the
    pinned one it is still appended: Vite honours the last ``--port``.
    """
    pinned, in_script = frontend_port(front_dir)
    command = ["npm", "run", "dev"]
    if web_port is None:
        return command, pinned
    if not (in_script and pinned == web_port):
        # The bare `--` is npm's, not vite's: without it npm eats the flag
        # instead of forwarding it to the script.
        command += ["--", "--port", str(web_port)]
    return command, web_port


def run(command: list[str], *, cwd: Path, what: str, env: dict[str, str] | None = None) -> None:
    """Run a command to completion, raising with its output when it fails."""
    result = subprocess.run(  # nosec B603
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DevError(f"{what} failed:\n{detail}")


def wait_for_healthy(compose_file: Path, cwd: Path, timeout: float = 60.0) -> bool:
    """Poll until no container is still starting, or the timeout runs out.

    Returns whether everything settled. Starting Alembic against a PostgreSQL
    that is still initialising fails with a connection error that reads like a
    configuration problem, so the wait is worth the wall-clock.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(  # nosec B603
            ["docker", "compose", "-f", str(compose_file), "ps", "--format", "{{.Health}}"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        states = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not states or all(state in ("healthy", "") for state in states):
            return True
        if any(state == "unhealthy" for state in states):
            return False
        time.sleep(1.0)
    return False


def spawn(
    command: list[str], *, cwd: Path, name: str, env: dict[str, str] | None = None
) -> Process:
    """Start a long-running child in its own process group.

    The group is what makes Ctrl-C work: signalling only the parent leaves
    uvicorn's reloader and vite's esbuild holding their ports, and the next
    start fails with an address already in use.
    """
    merged = {**os.environ, **(env or {})}
    # `sys.platform`, not `os.name`: mypy narrows on the first and does not on
    # the second, so with `os.name` the POSIX-only calls below are checked
    # against a Windows stdlib and `mypy src` fails on any Windows machine, on
    # code that never runs there.
    if sys.platform != "win32":
        popen = subprocess.Popen(  # nosec B603
            command, cwd=str(cwd), env=merged, start_new_session=True
        )
    else:  # pragma: no cover - exercised on Windows only
        popen = subprocess.Popen(  # nosec B603
            command,
            cwd=str(cwd),
            env=merged,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return Process(name, popen)


def terminate(processes: list[Process], grace: float = 5.0) -> None:
    """Stop every child, escalating only for the ones that ignore the first ask.

    SIGTERM to the group, a short grace period, then SIGKILL to whatever is
    left. Killing immediately would cost vite its cache and uvicorn its chance
    to close connections; waiting forever would hang the terminal.
    """
    for process in processes:
        if process.popen.poll() is not None:
            continue
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.popen.pid), signal.SIGTERM)
            else:  # pragma: no cover - Windows only
                process.popen.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            continue

    deadline = time.monotonic() + grace
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.popen.pid), signal.SIGKILL)
                else:  # pragma: no cover - Windows only
                    process.popen.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass


class _Stopped(BaseException):
    """SIGTERM arrived. Not an Exception, so no `except Exception` swallows it."""


def _raise_stopped(signum: int, frame: object) -> None:
    raise _Stopped


def supervise(processes: list[Process]) -> int:
    """Block until the first child exits or a signal arrives, then stop the rest.

    One dead process means the pair is no longer what was asked for: a frontend
    talking to a backend that fell over produces network errors that look like
    frontend bugs. Better to stop cleanly and say which one went.

    SIGTERM needs its own handler. Ctrl-C works for free because Python turns
    SIGINT into KeyboardInterrupt and ``finally`` runs, but the default SIGTERM
    disposition kills the interpreter outright -- no ``finally``, no cleanup.
    Since the children were deliberately put in their own process groups so a
    Ctrl-C at the terminal does not reach them directly, they would then survive
    as orphans still holding the ports. Which is the exact failure this module
    claims to prevent, arriving through the other signal.
    """
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, _raise_stopped)
    except ValueError:  # pragma: no cover - not on the main thread
        previous = None

    try:
        while True:
            for process in processes:
                code = process.popen.poll()
                if code is not None:
                    return code
            time.sleep(0.3)
    except (KeyboardInterrupt, _Stopped):
        return 0
    finally:
        terminate(processes)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def python_executable() -> str:
    """The interpreter running the CLI, so a child lands in the same venv."""
    return sys.executable or "python"


__all__ = [
    "VITE_DEFAULT_PORT",
    "DevError",
    "Process",
    "compose_services",
    "docker_available",
    "frontend_command",
    "frontend_port",
    "python_executable",
    "run",
    "spawn",
    "supervise",
    "terminate",
    "wait_for_healthy",
]
