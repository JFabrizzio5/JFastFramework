"""The plugin contract.

Everything above the kernel is a plugin: monitoring, database, cache, RAG,
auth. The kernel itself only knows how to resolve, order, start and stop them.

A plugin declares four things:

1. ``meta``            -- identity, dependencies, whether it is on by default.
2. ``Settings``        -- its own typed config block, read from ``[plugin.<name>]``.
3. lifecycle hooks     -- ``register`` (build time), ``startup`` / ``shutdown`` (runtime).
4. ``infra()``         -- the containers it needs.

Point 4 is what makes deploy generation honest: docker-compose is derived from
the enabled plugin graph rather than hand-maintained, so removing the cache
plugin actually removes the Redis container.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class PluginSettings(BaseSettings):
    """Base for per-plugin settings. Subclasses set their own env prefix."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@dataclass(frozen=True)
class PluginMeta:
    name: str
    version: str = "0.1.0"
    description: str = ""
    # Plugin names that must be loaded and started before this one.
    requires: tuple[str, ...] = ()
    # Optional dependencies: ordered after them if present, no error if absent.
    after: tuple[str, ...] = ()
    # Context keys this plugin publishes. Used by `jfast describe` and to
    # detect two plugins claiming the same key before either one runs.
    provides: tuple[str, ...] = ()
    # Loaded when the service does not pin an explicit plugin allow-list.
    default_enabled: bool = False
    # Extra to install for this plugin to import cleanly, e.g. "jfastframework[db]".
    extra: str | None = None
    # Whether this plugin failing to answer a readiness probe should take
    # the service out of rotation. A health() implementation says so per
    # report; this is the answer for the case where it never returns at all.
    health_critical: bool = True


@dataclass
class InfraService:
    """One container a plugin needs in order to work.

    Consumed by ``jfastframework.deploy`` to emit compose / k8s manifests.
    ``port_offset`` is relative to the service's assigned base port, which
    keeps port allocation declarative instead of hardcoded.
    """

    name: str
    image: str
    port_offset: int | None = None
    internal_port: int | None = None
    # Additional (offset, internal_port) pairs for services that expose more
    # than one port -- Qdrant's HTTP and gRPC endpoints, for example.
    extra_ports: list[tuple[int, int]] = field(default_factory=list)
    # The host port the main mapping is published on, when it is not
    # ``base_port + port_offset``. A plugin that advertises its own address to
    # clients outside the compose network -- Kafka does -- must derive that
    # address from ``port_mappings`` rather than from a second setting of its
    # own: an advertised port the compose file does not publish is a client
    # that bootstraps, reconnects to the advertised address and hangs.
    host_port: int | None = None
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[str] = field(default_factory=list)
    command: str | None = None
    healthcheck: dict[str, Any] | None = None
    depends_on: list[str] = field(default_factory=list)
    # Size of /dev/shm, as compose spells it ("1gb"). Docker gives a container
    # 64 MB, which is where a database puts the working memory of a parallel
    # query -- so it fails with "could not resize shared memory segment" on
    # exactly the queries big enough for the planner to parallelise, and on no
    # others. Only set this for a container that needs it; the default is right
    # for everything else.
    shm_size: str | None = None
    # How a service on the same compose network reaches this container:
    # ``{"JFAST_DB_DSN": "postgresql+asyncpg://app:${POSTGRES_PASSWORD}@postgres:5432/app"}``.
    #
    # The container is derived from the plugin graph, so the connection string
    # has to be too: otherwise `jfast deploy compose` writes a file whose api
    # service loads a `.env` still pointing at `localhost` -- which inside a
    # container is that container. A crash loop on a fresh project, on the
    # first command the scaffold tells you to run. The workspace generator
    # derives this through `resources.Resource.dsn`; this is the same fact,
    # declared where the single-service generator can see it.
    #
    # Hostname and internal port, never the published one: the published port is
    # for a client on the host, and this value is only ever read by a container
    # on the network. A plugin whose address is per-disk or per-connection
    # configuration rather than one variable -- storage -- declares nothing.
    client_env: dict[str, str] = field(default_factory=dict)

    def port_mappings(self, base_port: int) -> list[tuple[int, int]]:
        """``(host port, container port)`` pairs, offsets already resolved.

        The one place a published host port is decided, so the compose ``ports``
        key and anything a plugin advertises about itself read the same number.
        """
        mappings: list[tuple[int, int]] = []
        if self.port_offset is not None and self.internal_port is not None:
            host = self.host_port if self.host_port is not None else base_port + self.port_offset
            mappings.append((host, self.internal_port))
        mappings += [(base_port + offset, internal) for offset, internal in self.extra_ports]
        return mappings


@dataclass
class HealthReport:
    """Result of one plugin's health probe, aggregated into ``/health``."""

    healthy: bool
    detail: str = "ok"
    # False means a failure here does not make the whole service unhealthy.
    critical: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, detail: str = "ok", **meta: Any) -> HealthReport:
        return cls(healthy=True, detail=detail, meta=meta)

    @classmethod
    def fail(cls, detail: str, *, critical: bool = True, **meta: Any) -> HealthReport:
        return cls(healthy=False, detail=detail, critical=critical, meta=meta)


class Plugin:
    """Base class for all plugins.

    Every hook is optional. A plugin that only adds a router overrides
    ``register`` and nothing else.
    """

    meta: ClassVar[PluginMeta]
    Settings: ClassVar[type[PluginSettings] | None] = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        raw = config or {}
        self.settings: Any = self.Settings(**raw) if self.Settings is not None else None
        self.raw_config = raw

    # -- lifecycle -----------------------------------------------------

    def register(self, ctx: AppContext) -> None:
        """Build time: mount routers, add middleware, publish providers.

        Runs before the app starts serving and before any ``startup`` hook.
        Must not open network connections -- that belongs in ``startup``.
        """

    async def startup(self, ctx: AppContext) -> None:
        """Runtime: open pools, connect clients, warm caches."""

    async def shutdown(self, ctx: AppContext) -> None:
        """Runtime: close everything ``startup`` opened. Runs in reverse order."""

    async def health(self, ctx: AppContext) -> HealthReport:
        """Probe backing resources. Default: healthy with nothing to check."""
        return HealthReport.ok("no health check implemented")

    # -- deployment ----------------------------------------------------

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        """Containers this plugin needs. Empty for pure in-process plugins."""
        return []

    # -- introspection -------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Machine-readable summary, surfaced by ``jfast describe --json``.

        This is the contract an AI agent reads instead of grepping source.
        """
        return {
            "name": self.meta.name,
            "version": self.meta.version,
            "description": self.meta.description,
            "requires": list(self.meta.requires),
            "after": list(self.meta.after),
            "provides": list(self.meta.provides),
            "default_enabled": self.meta.default_enabled,
            "extra": self.meta.extra,
            "settings": (
                self.settings.model_dump(mode="json") if self.settings is not None else None
            ),
            "infra": [service.name for service in self.infra()],
        }

    def __repr__(self) -> str:
        return f"<Plugin {self.meta.name} v{self.meta.version}>"
