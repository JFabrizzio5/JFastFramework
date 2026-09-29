"""Derive deployment artifacts from the enabled plugin graph.

This is the payoff of ``Plugin.infra()``: docker-compose is generated, not
hand-maintained. Disable the ``cache`` plugin and the Redis container is gone
from the next generated compose file -- no drift between what the app loads and
what the infrastructure runs.

Port allocation follows the CometaX convention: a service owns a block of ten
ports starting at its base port, and each plugin declares its offset inside
that block.

This generator writes **one service's** compose file. A workspace of several is
``deploy.workspace``, and the two are not interchangeable: there, datastores are
named resources with a password each, because a workspace can hold any number of
them. What both must do identically -- the shape of a container, the volumes
that keep a service's uploads -- lives here and is imported there.

No YAML dependency: the emitted structure is small and fully known, so a tiny
deterministic serialiser is cheaper than pulling in PyYAML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jfastframework.context import AppContext
    from jfastframework.plugins.base import InfraService, Plugin
    from jfastframework.settings import JFastConfig

PORT_BLOCK_SIZE = 10

# WORKDIR in the generated Dockerfile. Local storage disks are configured
# relative to it, so a volume that keeps them has to be mounted under it.
IMAGE_WORKDIR = "/app"


def _dump_yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return " {}"
        lines = [f"{pad}{key}:{_dump_yaml(item, indent + 1)}" for key, item in value.items()]
        return "\n" + "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return " []"
        lines = []
        for item in value:
            rendered = _dump_yaml(item, indent + 1)
            if rendered.startswith("\n"):
                inner = rendered.lstrip("\n")
                first, _, rest = inner.partition("\n")
                lines.append(f"{pad}- {first.strip()}")
                if rest:
                    lines.append(rest)
            else:
                lines.append(f"{pad}-{rendered}")
        return "\n" + "\n".join(lines)
    if isinstance(value, bool):
        return f" {str(value).lower()}"
    if value is None:
        return " null"
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[]&*?|<>=!%@`") or text != text.strip():
        escaped = text.replace('"', '\\"')
        return f' "{escaped}"'
    return f" {text}"


def generation_context(config: JFastConfig, base_port: int) -> AppContext:
    """A context with no application behind it, for ``Plugin.infra()``.

    ``infra()`` runs at generation time: nothing is started, nothing is
    connected, and no provider has been published. What a plugin legitimately
    needs from the context here is the settings, above all the base port --
    without it a plugin cannot know which host port its container will be
    published on. Kafka has to advertise exactly that address to clients
    outside the compose network, and advertising the wrong one makes a client
    connect and then hang.

    ``base_port`` overrides ``settings.port`` because ``--base-port`` does.
    """
    from fastapi import FastAPI

    from jfastframework.context import AppContext as _AppContext
    from jfastframework.settings import JFastConfig as _JFastConfig

    settings = config.settings.model_copy(update={"port": base_port})
    return _AppContext(app=FastAPI(), config=_JFastConfig(settings=settings, raw=config.raw))


def collect_infra(plugins: list[Plugin], ctx: AppContext | None = None) -> list[Any]:
    services = []
    for plugin in plugins:
        services.extend(plugin.infra(ctx))
    return services


def infra_compose_service(infra: InfraService, *, base_port: int) -> dict[str, Any]:
    """One ``InfraService`` rendered as a compose service.

    Shared with the workspace generator: a field added to ``InfraService`` and
    emitted here reaches both generated files, or neither. Built separately,
    the two entries drift: a field such as volumes reaches only one of them.
    """
    entry: dict[str, Any] = {
        "image": infra.image,
        "restart": "unless-stopped",
    }
    offsets: list[int] = []
    if infra.port_offset is not None and infra.internal_port is not None:
        offsets.append(infra.port_offset)
    offsets += [offset for offset, _ in infra.extra_ports]
    for offset in offsets:
        if offset >= PORT_BLOCK_SIZE:
            raise ValueError(
                f"Plugin infra {infra.name!r} declares port_offset "
                f"{offset}, outside the {PORT_BLOCK_SIZE}-port block."
            )
    mappings = infra.port_mappings(base_port)
    if mappings:
        entry["ports"] = [f"{host}:{internal}" for host, internal in mappings]
    if infra.environment:
        entry["environment"] = dict(infra.environment)
    if infra.command:
        entry["command"] = infra.command
    if infra.volumes:
        entry["volumes"] = list(infra.volumes)
    if infra.shm_size:
        entry["shm_size"] = infra.shm_size
    if infra.healthcheck:
        entry["healthcheck"] = dict(infra.healthcheck)
    if infra.depends_on:
        entry["depends_on"] = list(infra.depends_on)
    return entry


def named_volumes(mounts: list[str]) -> list[str]:
    """The named volumes among a container's mounts.

    A bind mount (``./Caddyfile:/etc/caddy/Caddyfile``) is a host path and must
    not be declared at the top level; a named volume must, or compose refuses
    the file.
    """
    return [
        name
        for name in (mount.split(":", 1)[0] for mount in mounts)
        if not name.startswith((".", "/"))
    ]


def storage_mounts(plugin: Plugin, *, prefix: str) -> list[str]:
    """Volumes for one service's local storage disks.

    Without them the uploads live in the container's own filesystem, and the
    next `docker build` throws them away while the rows referencing them stay.

    Keyed on the plugin name because the plugin contract has no way to say "I
    need this directory to survive" -- only ``infra()``, which is about *other*
    containers. Shared by both generators: a service must not lose its uploads
    by being deployed through the other command.
    """
    if plugin.meta.name != "storage":
        return []

    from jfastframework.plugins.builtin.storage import DEFAULT_DISKS

    disks: dict[str, dict[str, Any]] = getattr(plugin.settings, "disks", None) or DEFAULT_DISKS
    mounts: list[str] = []
    for disk, spec in sorted(disks.items()):
        # S3 and MinIO hold the bytes themselves; there is nothing local to keep.
        if spec.get("driver") != "local":
            continue
        root = str(spec.get("root", "")).lstrip("./")
        if not root:
            continue
        volume = f"{prefix}_{disk}_data".replace("-", "_").replace(".", "_")
        mounts.append(f"{volume}:{IMAGE_WORKDIR}/{root}")
    return mounts


def build_compose(
    config: JFastConfig,
    plugins: list[Plugin],
    *,
    base_port: int | None = None,
    include_app: bool = True,
) -> dict[str, Any]:
    settings = config.settings
    base = base_port if base_port is not None else settings.port
    app_name = settings.app_name

    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    infra_services = collect_infra(plugins, generation_context(config, base))

    for infra in infra_services:
        entry = infra_compose_service(infra, base_port=base)
        for name in named_volumes(entry.get("volumes", [])):
            volumes[name] = None
        services[infra.name] = entry

    if include_app:
        # `environment` beats `env_file` in compose, which is the whole reason
        # these belong here: the generated .env holds the addresses a developer
        # running outside compose needs (localhost and the published port), and
        # inside the network those resolve to the container itself. Deriving the
        # internal address from the same plugin that declared the container is
        # what keeps the two from drifting.
        client_env: dict[str, str] = {}
        for infra in infra_services:
            client_env.update(infra.client_env)
        app_entry: dict[str, Any] = {
            "build": ".",
            "restart": "unless-stopped",
            "ports": [f"{base}:{base}"],
            "env_file": [".env"],
            "environment": {
                "JFAST_PORT": str(base),
                "JFAST_APP_NAME": app_name,
                **client_env,
            },
        }
        mounts: list[str] = []
        for plugin in plugins:
            mounts.extend(storage_mounts(plugin, prefix=app_name))
        if mounts:
            app_entry["volumes"] = mounts
            for name in named_volumes(mounts):
                volumes[name] = None
        # Wait for a healthcheck when the container declares one; otherwise
        # "started" is the strongest guarantee compose can give.
        dependants = {
            name: {
                "condition": ("service_healthy" if entry.get("healthcheck") else "service_started")
            }
            for name, entry in services.items()
        }
        if dependants:
            app_entry["depends_on"] = dependants
        services = {"api": app_entry, **services}

    compose: dict[str, Any] = {"services": services}
    if volumes:
        compose["volumes"] = volumes
    return compose


def render_compose(compose: dict[str, Any]) -> str:
    header = (
        "# Generated by `jfast deploy compose`. Do not edit by hand --\n"
        "# regenerate after changing the plugin list in jfast.toml.\n"
        "#\n"
        "# One service. A workspace of several is generated by\n"
        "# `jfast workspace compose`, which names its datastores after the\n"
        "# resources in jfast.workspace.toml rather than after the plugin that\n"
        "# wants them -- see docs/deploy.md, `Two generators`.\n"
        "#\n"
        "# Containers are named by compose, from the project (the directory, or\n"
        "# `docker compose -p <name>`) -- never pinned here, so two copies of\n"
        "# this service can run at once.\n"
    )
    return header + _dump_yaml(compose).lstrip("\n") + "\n"


# The image, as a named template rather than an f-string inside the function.
# Hoisted so a security scanner reads it as one string constant instead of as
# formatted output that happens to resemble SQL, and so the Dockerfile can be
# read without the surrounding Python.
DOCKERFILE_TEMPLATE = """\
# Generated by `jfast deploy dockerfile`.
#
# Two stages. The compiler is what builds the wheels for asyncpg, Pillow and
# anything else without one for this platform, and it has no business being
# in the image that faces the internet: it is ~200 MB and a toolchain for
# whoever gets a shell. Only the finished virtualenv crosses over.
FROM python:{python_version}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \\
 && apt-get install -y --no-install-recommends build-essential \\
 && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the system site-packages, so the runtime stage
# copies one directory and inherits nothing else from this one.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Both optional, both globbed. A generated service ships
# requirements.txt and no pyproject.toml, and COPY fails the build when
# its source does not exist -- so the image could never be built from
# what the generator actually produced.
#
# Copied before the source so a code change does not reinstall the world.
COPY pyproject.toml* requirements.txt* ./
RUN pip install --upgrade pip \\
 && if [ -f requirements.txt ]; then pip install -r requirements.txt; fi

COPY . .

# The other branch, and it has to run after the source is here: `pip install .`
# builds *this* project, and with only the manifest copied there is nothing to
# build.
RUN if [ ! -f requirements.txt ]; then pip install .; fi


FROM python:{python_version}-slim AS runtime

# JFAST_WORKERS empty means "decide at start from the CPUs this container
# actually got". Set it here to pin a number into the image, or at run time to
# override it per deployment.
ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PATH="/opt/venv/bin:$PATH" \\
    JFAST_WORKERS={workers}

WORKDIR /app

# curl is here for the HEALTHCHECK below and nothing else. No compiler.
RUN apt-get update \\
 && apt-get install -y --no-install-recommends curl \\
 && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. Containers that run as root are a finding in every
# security review, and fixing it later means rebuilding image layers.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=appuser:appuser /app /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
  CMD curl -fsS http://localhost:${{JFAST_PORT:-8000}}/health || exit 1

# Migrate, then serve. A container starting against an empty database
# answers 500 to everything until somebody remembers to run Alembic by
# hand, and `create_all` is not the fix: it builds a schema Alembic does
# not know about, so the first real migration diverges silently. One
# source of truth, applied before the first request.
#
# `set -e` stops the container on a failed migration rather than serving a
# half-migrated schema; `exec` leaves uvicorn as PID 1 so it gets SIGTERM.
#
# `--no-proxy-headers` because uvicorn's own X-Forwarded-For handling is on by
# default and rewrites the client address before the application sees it, from
# any peer in its own loopback allow-list. In a pod that is every sidecar, and
# the framework's trusted_proxies would be deciding about an address the
# request supplied. One resolver, and it is the one that reads jfast.toml.
#
# One uvicorn worker is one Python process on one core, and a request spends
# most of its life outside the database -- serialising, validating, rendering.
# Measured: 6.5 ms in PostgreSQL against 79 ms end to end at concurrency 16.
# The number is derived at start rather than baked in because the image does
# not know how much CPU it will be given, and `nproc` alone is the wrong
# answer: it reports the host's cores, so a container limited to half a core
# would start as many workers as the machine has. cgroup v2 publishes the real
# quota, so read that first and fall back to `nproc`. Capped, because past a
# point the workers only compete for the same core and each one costs a full
# copy of the application's memory.
RUN echo '#!/bin/sh' > /entrypoint.sh \\
 && echo 'set -e' >> /entrypoint.sh \\
 && echo '[ -f alembic.ini ] && alembic upgrade head' >> /entrypoint.sh \\
 && echo 'if [ -z "$JFAST_WORKERS" ]; then' >> /entrypoint.sh \\
 && echo '  cpus=$(nproc 2>/dev/null || echo 1)' >> /entrypoint.sh \\
 && echo '  if [ -r /sys/fs/cgroup/cpu.max ]; then' >> /entrypoint.sh \\
 && echo '    read -r quota period < /sys/fs/cgroup/cpu.max' >> /entrypoint.sh \\
 && echo '    if [ "$quota" != max ]; then cpus=$(( (quota + period - 1) / period )); fi' \\
      >> /entrypoint.sh \\
 && echo '  fi' >> /entrypoint.sh \\
 && echo '  if [ "$cpus" -lt 1 ]; then cpus=1; fi' >> /entrypoint.sh \\
 && echo '  if [ "$cpus" -gt {max_workers} ]; then cpus={max_workers}; fi' >> /entrypoint.sh \\
 && echo '  JFAST_WORKERS=$cpus' >> /entrypoint.sh \\
 && echo 'fi' >> /entrypoint.sh \\
 && echo 'exec uvicorn main:app --host 0.0.0.0 --port ${{JFAST_PORT:-8000}}' \\
      '--no-proxy-headers --workers $JFAST_WORKERS' >> /entrypoint.sh \\
 && chmod +x /entrypoint.sh
USER appuser

CMD ["/entrypoint.sh"]
"""

# Above this, more workers stop buying throughput and start buying memory: each
# one is a full copy of the application. Deployments that really want more say
# so with JFAST_WORKERS.
MAX_DERIVED_WORKERS = 8


def render_dockerfile(python_version: str = "3.12", workers: int | None = None) -> str:
    """The image. ``workers`` pins a worker count instead of deriving one.

    Left as ``None``, the entrypoint reads the container's CPU quota at start,
    which is the only place that number is actually known.
    """
    return DOCKERFILE_TEMPLATE.format(
        python_version=python_version,
        workers="" if workers is None else workers,
        max_workers=MAX_DERIVED_WORKERS,
    )


# The Dockerfile ends in `COPY . .`, which is the only spelling that survives a
# service growing a directory nobody updated a COPY line for. What that costs
# is everything else in the working tree, and the path a new service is walked
# down is `cp .env.example .env` and then a build -- so without this file the
# filled-in .env, with the database password and the signing secret in it,
# lands in an image layer and travels wherever that image goes. Deleting the
# file later does not remove the layer.
DOCKERIGNORE_TEMPLATE = """\
# Generated by `jfast deploy dockerfile`.
#
# The Dockerfile copies the whole directory. This is the list of what that
# must not mean.

# Secrets. .env.example stays: it is a template with no values in it.
.env
.env.*
!.env.example

# History, and the credentials a remote URL can carry.
.git
.gitignore
.github

# Rebuilt inside the image, and a host virtualenv holds binaries linked
# against the host's libraries.
.venv
venv
__pycache__
*.py[cod]
*.egg-info
build
dist

# Caches with nothing the image needs.
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
htmlcov
node_modules

# Local state. Uploads written by a development run are not part of the build.
storage
*.sqlite3
*.db
*.log

# Deployment inputs, not application code.
Dockerfile
.dockerignore
docker-compose*.yml
k8s
"""


def render_dockerignore() -> str:
    """What `COPY . .` must not pick up."""
    return DOCKERIGNORE_TEMPLATE
