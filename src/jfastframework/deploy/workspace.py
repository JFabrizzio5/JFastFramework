"""One compose file and one Caddyfile for a whole workspace.

Per-service compose files are right while you are working on one service. The
moment there are three plus a gateway, you want a single `docker compose up`
and one hostname — that is what these two generators produce.

Both derive from ``jfast.workspace.toml``, and the compose file also reads each
service's own ``jfast.toml`` -- the workspace owns ports, paths and the resource
graph, but which plugins a service loads is recorded next to the service, and a
plugin can own a container. Regenerate after adding a service rather than
editing the output; a hand-edited generated file is a merge conflict waiting to
happen.

The other generator is ``deploy.compose``, for one service on its own. It names
a datastore after the plugin that wants it (``postgres``, ``POSTGRES_PASSWORD``);
here a datastore is a named resource with its own password, because a workspace
can hold several and one shared password makes a leak anywhere a leak
everywhere. Neither naming fits the other case, so the difference stays -- see
docs/deploy.md, `Two generators`. Everything that is *not* topology comes from
``deploy.compose`` so the two cannot drift.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jfastframework.deploy.compose import (
    _dump_yaml,
    generation_context,
    infra_compose_service,
    named_volumes,
    storage_mounts,
)
from jfastframework.resources import RESOURCE_TYPES

if TYPE_CHECKING:
    from pathlib import Path

    from jfastframework.plugins.base import Plugin
    from jfastframework.settings import JFastConfig
    from jfastframework.workspace import ServiceEntry, Workspace

# Datastore images and offsets live in `jfastframework.resources`, so the
# compose generator and the workspace model cannot disagree about what a
# `postgres` resource is.

CADDY_HTTP_PORT = 80
CADDY_HTTPS_PORT = 443

# Where a service records which plugins it loads. The workspace file does not
# know: it owns ports, paths and the resource graph, and which plugins a
# service loads is the service's own business.
SERVICE_CONFIG_FILE = "jfast.toml"

# The plugins whose containers the resource graph already owns. Their
# `infra()` is the per-service version of what the workspace declares by
# name, so emitting it as well would stand a second, anonymous PostgreSQL
# beside the one every generated DSN points at.
RESOURCE_OWNED_PLUGINS = frozenset(spec.plugin for spec in RESOURCE_TYPES.values())


def _warn(message: str) -> None:
    """Warn whenever a container is left out: dropping one silently hides the gap.

    ``stacklevel=3`` points the warning at whoever asked for the compose file
    rather than at this module, which is not where anything can be done.
    """
    warnings.warn(message, UserWarning, stacklevel=3)


@dataclass
class _PluginGraph:
    """What the services' enabled plugins contribute to the compose file."""

    # Container name -> compose service, keyed by the name the plugin chose:
    # that name is also the container's hostname on the compose network.
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Service name -> the containers its plugins declared, for `depends_on`.
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # Service name -> mounts to add to the service's own container.
    mounts: dict[str, list[str]] = field(default_factory=dict)
    volumes: dict[str, Any] = field(default_factory=dict)
    # Service name -> how it reaches those containers on the compose network
    # (``InfraService.client_env``), as the single-service generator writes it.
    environment: dict[str, dict[str, str]] = field(default_factory=dict)


def _plugins_of(root: Path, service: ServiceEntry) -> tuple[JFastConfig | None, list[Plugin]]:
    """One service's plugin graph, instantiated far enough to interrogate.

    Deliberately not ``registry.build``: that resolves dependencies and raises
    on the first plugin it cannot import, so one service whose extra is missing
    from *this* environment would take the whole workspace's compose file with
    it. Here that is a warning and the rest of the file still generates.
    """
    config_path = root / service.path / SERVICE_CONFIG_FILE
    if not config_path.is_file():
        # A Go service, or a directory nothing has generated yet.
        return None, []

    from jfastframework.plugins import registry
    from jfastframework.settings import JFastConfig as _JFastConfig

    config = _JFastConfig.load(config_path)
    paths: dict[str, str] = config.raw.get("plugins", {}).get("paths", {})
    available = registry.discover(extra_paths=paths)
    broken: dict[str, str] = getattr(registry.discover, "broken", {})

    disabled = set(config.settings.disabled_plugins)
    # An empty allow-list means the defaults, which is how the kernel reads it.
    enabled = list(config.settings.plugins) or [
        name for name, cls in available.items() if cls.meta.default_enabled
    ]

    instances: list[Plugin] = []
    for name in enabled:
        if name in disabled or name in RESOURCE_OWNED_PLUGINS:
            continue
        cls = available.get(name)
        if cls is None:
            _warn(
                f"{service.name}: cannot inspect plugin {name!r} "
                f"({broken.get(name, 'not installed')}), so any container it declares is "
                f"missing from the generated compose file."
            )
            continue
        try:
            instances.append(cls(config.plugin_config(name)))
        except Exception as exc:  # noqa: BLE001 - any settings error, same answer
            _warn(
                f"{service.name}: plugin {name!r} could not be configured ({exc}), so any "
                f"container it declares is missing from the generated compose file."
            )
    return config, instances


def _scan_plugins(workspace: Workspace) -> _PluginGraph:
    """Everything the plugin graph contributes, service by service.

    This is what the resource graph alone cannot express: `events` declares a
    broker, `storage` declares MinIO, `queue` on RabbitMQ declares a broker of
    its own. None of them is a datastore the resource graph can name, so
    without this scan all of them would be dropped without a word.
    """
    graph = _PluginGraph()
    if workspace.file is None:
        # In memory, with no directory to read the per-service plugin lists from.
        return graph

    root = workspace.file.parent
    taken = {r.container for r in workspace.all_resources()} | {s.name for s in workspace.services}

    for service in workspace.services:
        if service.is_frontend:
            continue
        config, plugins = _plugins_of(root, service)
        if config is None:
            continue
        # The base port the plugin's container will be published on. Without
        # it a plugin cannot advertise an address that resolves from the host.
        ctx = generation_context(config, service.port)

        for plugin in plugins:
            graph.mounts.setdefault(service.name, []).extend(
                storage_mounts(plugin, prefix=service.name)
            )
            try:
                declared = plugin.infra(ctx)
            except Exception as exc:  # noqa: BLE001 - one bad plugin is not fatal here
                _warn(f"{service.name}: plugin {plugin.meta.name!r} failed to declare infra: {exc}")
                continue

            for infra in declared:
                if infra.name in taken:
                    _warn(
                        f"{service.name}: plugin {plugin.meta.name!r} wants a container named "
                        f"{infra.name!r}, which is already a service or a resource in this "
                        f"workspace. Skipped -- rename one of the two."
                    )
                    continue
                graph.dependencies.setdefault(service.name, []).append(infra.name)
                # Without it the container falls back to the .env, which holds
                # the host's address -- `localhost` is the service itself.
                graph.environment.setdefault(service.name, {}).update(infra.client_env)
                if infra.name in graph.services:
                    # One container, shared. These advertise their own name as
                    # their hostname -- Kafka tells clients to reconnect to
                    # `kafka:9092` -- so a second copy under a different name
                    # would advertise an address that does not reach it.
                    if graph.services[infra.name]["image"] != infra.image:
                        _warn(
                            f"two services declare a container named {infra.name!r} with "
                            f"different images; keeping "
                            f"{graph.services[infra.name]['image']}, ignoring {infra.image}."
                        )
                    continue
                entry = infra_compose_service(infra, base_port=service.port)
                graph.services[infra.name] = entry
                for volume in named_volumes(entry.get("volumes", [])):
                    graph.volumes[volume] = None

    for mounts in graph.mounts.values():
        for volume in named_volumes(mounts):
            graph.volumes[volume] = None
    return graph


def _resource_services(workspace: Workspace) -> dict[str, Any]:
    """One container per *resource*, not per service.

    The difference is the point of the resource graph: two services that bind
    the same resource share one database, and a service that binds two
    databases gets two. Neither is expressible when a service owns its
    datastores by type.
    """
    return {r.container: r.compose_service() for r in workspace.all_resources()}


def build_workspace_compose(workspace: Workspace, *, with_caddy: bool = True) -> dict[str, Any]:
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    plugins = _scan_plugins(workspace)

    for container, spec in _resource_services(workspace).items():
        services[container] = spec
        for volume in named_volumes(spec.get("volumes", [])):
            volumes[volume] = None

    services.update(plugins.services)
    volumes.update(plugins.volumes)

    for service in workspace.services:
        if service.is_frontend:
            # A built SPA is static files. Caddy serves them; there is no
            # container to run in production. `npm run dev` is the dev story.
            continue

        entry: dict[str, Any] = {
            "build": {"context": f"./{service.path}"},
            "restart": "unless-stopped",
            "env_file": [f"./{service.path}/.env"],
            "environment": {
                "JFAST_APP_NAME": service.name,
                "JFAST_PORT": str(service.port),
            },
            "ports": [f"{service.port}:{service.port}"],
        }

        entry["environment"].update(plugins.environment.get(service.name, {}))

        bound = workspace.bindings_for(service)
        depends_on: dict[str, Any] = {}
        if bound:
            # The connection strings, written here rather than left to a
            # hand-maintained .env beside a generated container.
            entry["environment"].update(workspace.environment_for(service))
            depends_on = {
                resource.container: {
                    "condition": (
                        "service_healthy"
                        if resource.compose_service().get("healthcheck")
                        else "service_started"
                    )
                }
                for _, resource in bound
            }
        for container in plugins.dependencies.get(service.name, []):
            depends_on[container] = {
                "condition": (
                    "service_healthy"
                    if plugins.services[container].get("healthcheck")
                    else "service_started"
                )
            }
        if depends_on:
            entry["depends_on"] = depends_on

        mounts = plugins.mounts.get(service.name) or []
        if mounts:
            entry["volumes"] = mounts

        if service.grpc:
            entry["ports"].append(f"{service.grpc_port}:{service.grpc_port}")

        services[service.name] = entry

    if with_caddy:
        # Caddy last so it depends on everything already collected.
        services["caddy"] = {
            "image": "caddy:2-alpine",
            "restart": "unless-stopped",
            "ports": [f"{CADDY_HTTP_PORT}:80", f"{CADDY_HTTPS_PORT}:443"],
            "volumes": [
                "./Caddyfile:/etc/caddy/Caddyfile:ro",
                "./dist:/srv:ro",
                "caddy_data:/data",
                "caddy_config:/config",
            ],
            "depends_on": [s.name for s in workspace.services if not s.is_frontend],
        }
        volumes["caddy_data"] = None
        volumes["caddy_config"] = None

    compose: dict[str, Any] = {"services": services}
    if volumes:
        compose["volumes"] = volumes
    return compose


def render_workspace_compose(workspace: Workspace, *, with_caddy: bool = True) -> str:
    header = (
        f"# {workspace.name} — generated by `jfast workspace compose`.\n"
        "# Do not edit by hand: regenerate after adding a service.\n"
        "#\n"
        "# Frontends are absent on purpose. A built SPA is static files, served\n"
        "# by Caddy from ./dist — there is no container to run.\n"
        "#\n"
        "# One service on its own is generated by `jfast deploy compose`, which\n"
        "# names its datastores after the plugin that wants them (`postgres`,\n"
        "# `POSTGRES_PASSWORD`) rather than after a resource — see\n"
        "# docs/deploy.md, `Two generators`.\n"
        "#\n"
        "# Containers are named by compose, from the project (the directory, or\n"
        "# `docker compose -p <name>`) -- never pinned here, so an old copy of\n"
        "# this workspace and a new one can run side by side.\n"
    )
    compose = build_workspace_compose(workspace, with_caddy=with_caddy)
    return header + _dump_yaml(compose).lstrip("\n") + "\n"


def render_caddyfile(
    workspace: Workspace,
    *,
    hostname: str = "localhost",
    local_dev: bool = True,
    wildcard_tenants: bool = False,
) -> str:
    """Caddyfile routing the whole workspace behind one hostname.

    Caddy is the *edge*: TLS, HTTP/3, compression, static assets. The JFast
    gateway, when there is one, is the *application* proxy: auth, per-tenant
    limits, request-id minting.

    Running both is only worth it when you need that application layer. If you
    do not, this file routes straight to the services and the gateway is one
    hop you can delete.
    """
    lines: list[str] = [
        f"# {workspace.name} — generated by `jfast workspace caddy`.",
        "# Regenerate after adding a service; do not edit by hand.",
        "",
    ]

    if local_dev:
        lines += [
            "{",
            "\t# Local development: no ACME, no certificate on first boot.",
            "\tauto_https off",
            "}",
            "",
        ]

    # A wildcard site block serves every tenant subdomain from one config.
    # Caddy cannot get a certificate per tenant from a wildcard match, so
    # on-demand TLS issues one the first time each hostname is seen -- which
    # is why the `ask` endpoint below is not optional: without it, anyone
    # pointing a DNS record at you can make you request certificates for it.
    if wildcard_tenants and not local_dev:
        lines += [
            "{",
            "\ton_demand_tls {",
            f"\t\task http://{workspace.backends[0].name if workspace.backends else 'api'}"
            f":{workspace.backends[0].port if workspace.backends else 8000}/internal/tenant-exists",
            "\t\tinterval 2m",
            "\t\tburst 5",
            "\t}",
            "}",
            "",
        ]

    site = f"{'http://' if local_dev else ''}{hostname}"
    if wildcard_tenants:
        site = f"{site}, {'http://' if local_dev else ''}*.{hostname}"

    lines += [f"{site} {{", "\tencode zstd gzip", ""]

    if wildcard_tenants and not local_dev:
        lines += ["\ttls {", "\t\ton_demand", "\t}", ""]

    if wildcard_tenants:
        lines += [
            "\t# The tenant is the subdomain. The application reads it from the",
            "\t# Host header; this header is a convenience for logs and must not",
            "\t# be trusted on its own -- see [plugin.tenancy] sources.",
            "\theader_up X-Forwarded-Host {host}",
            "",
        ]

    gateway = workspace.gateway
    backends = [s for s in workspace.backends]
    frontends = workspace.frontends

    # Everything server-side lives under /api, with or without a gateway.
    # Keeping the public shape identical is what lets the frontend ship one
    # production build (VITE_API_URL=/api) that keeps working the day a
    # gateway appears in front of the backends.
    if gateway is not None:
        lines += [
            "\t# One application proxy in front of every backend. It owns auth",
            "\t# and request-id minting; Caddy owns TLS, HTTP/3 and compression.",
            "\thandle /api/* {",
            "\t\turi strip_prefix /api",
            f"\t\treverse_proxy {gateway.name}:{gateway.port}",
            "\t}",
            "",
        ]
    elif len(backends) == 1:
        service = backends[0]
        lines += [
            "\t# One backend, so no gateway: a second proxy would buy a hop and",
            "\t# an outage surface for nothing. The path stays /api either way.",
            "\thandle /api/* {",
            "\t\turi strip_prefix /api",
            f"\t\treverse_proxy {service.name}:{service.port}",
            "\t}",
            "",
        ]
    else:
        for service in backends:
            lines += [
                f"\thandle /api{service.prefix}/* {{",
                f"\t\turi strip_prefix /api{service.prefix}",
                f"\t\treverse_proxy {service.name}:{service.port}",
                "\t}",
                "",
            ]

    if frontends:
        primary = frontends[0]
        lines += [
            f"\t# {primary.name}: the built SPA, served as static files.",
            "\t# try_files sends unknown paths to index.html so client-side",
            "\t# routing survives a hard refresh.",
            "\thandle {",
            "\t\troot * /srv",
            "\t\ttry_files {path} /index.html",
            "\t\tfile_server",
            "\t}",
            "",
        ]
    elif not backends:
        lines += ['\trespond "no services in this workspace" 503', ""]

    lines += [
        "\tlog {",
        "\t\toutput stdout",
        "\t\tformat json",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"
