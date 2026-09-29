"""Datastores as named instances, and the bindings that connect services to them.

A list of datastore *types* on a service (the legacy
``datastores = ["database", "cache"]``) cannot express two things:

* the DSN that points at a generated container. Deriving the container while
  the connection string stays hand-maintained in a ``.env`` is where drift
  lives;
* a second PostgreSQL, because a type gives the second instance no name to
  hang on.

A resource has a name. A service binds to it under an environment variable.
From those two facts the compose file, the per-service ``.env``, the
``depends_on`` edges and the workspace graph are all derived, and none of them
can disagree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ports for standalone resources start here, above the base port and below the
# first service block, so a resource can never collide with a service's
# ten-port block. Implicit resources -- the ones synthesised from the old
# per-service `datastores` list -- keep their historical offsets instead.
RESOURCE_PORT_BAND = 900

# /dev/shm for PostgreSQL, as compose spells it. Docker's default is 64 MB and
# that is where a parallel query keeps its working memory: past the table size
# at which the planner starts parallelising, the query fails with "could not
# resize shared memory segment". A 500 on exactly the queries that matter, on
# nothing else, so it reads as random until somebody correlates it with size.
POSTGRES_SHM_SIZE = "1gb"


@dataclass(frozen=True)
class ResourceType:
    """What a kind of datastore is, once, so nothing has to restate it.

    ``env_var`` is the variable a service reads by default when it binds to
    one of these. It matches the plugin that consumes it -- ``JFAST_DB_DSN``
    is what ``DatabaseSettings`` reads -- so the default binding needs no
    configuration to be correct.
    """

    name: str
    image: str
    internal_port: int
    env_var: str
    plugin: str
    # The name this type had in the per-service `datastores` list.
    legacy: str
    # Historical offset inside a service's ten-port block, for compatibility.
    legacy_offset: int
    needs_credentials: bool = False
    default_database: str = ""


RESOURCE_TYPES: dict[str, ResourceType] = {
    "postgres": ResourceType(
        name="postgres",
        image="pgvector/pgvector:pg16",
        internal_port=5432,
        env_var="JFAST_DB_DSN",
        plugin="database",
        legacy="database",
        legacy_offset=1,
        needs_credentials=True,
        default_database="app",
    ),
    "redis": ResourceType(
        name="redis",
        image="redis:7-alpine",
        internal_port=6379,
        env_var="JFAST_CACHE_URL",
        plugin="cache",
        legacy="cache",
        legacy_offset=3,
    ),
    "mongo": ResourceType(
        name="mongo",
        image="mongo:7",
        internal_port=27017,
        env_var="JFAST_MONGO_DSN",
        plugin="mongo",
        legacy="mongo",
        legacy_offset=4,
        needs_credentials=True,
    ),
    "qdrant": ResourceType(
        name="qdrant",
        image="qdrant/qdrant:v1.12.4",
        internal_port=6333,
        env_var="JFAST_QDRANT_URL",
        plugin="qdrant",
        legacy="qdrant",
        legacy_offset=7,
    ),
}

# The reverse map, for reading the old per-service `datastores` list.
BY_LEGACY_NAME: dict[str, ResourceType] = {spec.legacy: spec for spec in RESOURCE_TYPES.values()}


def secret_var(resource_name: str) -> str:
    """Environment variable holding one resource's password.

    Per resource, not per workspace. A single shared ``POSTGRES_PASSWORD``
    means every database in the workspace has the same credentials, so a leak
    anywhere is a leak everywhere.
    """
    return resource_name.upper().replace("-", "_").replace(".", "_") + "_PASSWORD"


@dataclass
class Resource:
    """One datastore instance the workspace owns."""

    name: str
    type: str
    port: int
    image: str = ""
    database: str = ""
    user: str = "app"
    # True for resources synthesised from a service's legacy `datastores` list.
    # They render exactly as the legacy list does and are not written to the
    # file.
    implicit: bool = False

    def __post_init__(self) -> None:
        if self.type not in RESOURCE_TYPES:
            known = ", ".join(sorted(RESOURCE_TYPES))
            raise ValueError(f"Unknown resource type {self.type!r}. Known types: {known}.")
        if not self.image:
            self.image = self.spec.image
        if not self.database:
            self.database = self.spec.default_database

    @property
    def spec(self) -> ResourceType:
        return RESOURCE_TYPES[self.type]

    @property
    def container(self) -> str:
        """Its compose service name, which is its hostname on that network.

        Not a ``container_name``: that one is global to the daemon, so a second
        copy of the same workspace could not start beside the first. Compose
        derives the running container's name from the project instead.
        """
        return self.name

    @property
    def volume(self) -> str:
        return f"{self.name.replace('-', '_')}_data"

    @property
    def secret_var(self) -> str:
        return secret_var(self.name)

    def dsn(self, *, internal: bool = True) -> str:
        """How a service reaches this resource.

        ``internal`` is the compose network, where the hostname is the
        container. Otherwise it is localhost and the published port, which is
        what a developer running one service outside compose needs.
        """
        host = self.container if internal else "localhost"
        port = self.spec.internal_port if internal else self.port
        password = "${" + self.secret_var + "}"

        if self.type == "postgres":
            return f"postgresql+asyncpg://{self.user}:{password}@{host}:{port}/{self.database}"
        if self.type == "redis":
            return f"redis://{host}:{port}/0"
        if self.type == "mongo":
            return f"mongodb://{self.user}:{password}@{host}:{port}"
        if self.type == "qdrant":
            return f"http://{host}:{port}"
        raise ValueError(f"No DSN template for resource type {self.type!r}")

    def compose_service(self) -> dict[str, Any]:
        """The container this resource is, for the generated compose file."""
        entry: dict[str, Any] = {
            "image": self.image,
            "restart": "unless-stopped",
            "ports": [f"{self.port}:{self.spec.internal_port}"],
        }
        password = "${" + self.secret_var + ":?set " + self.secret_var + "}"

        if self.type == "postgres":
            entry["environment"] = {
                "POSTGRES_DB": self.database,
                "POSTGRES_USER": self.user,
                "POSTGRES_PASSWORD": password,
            }
            entry["volumes"] = [f"{self.volume}:/var/lib/postgresql/data"]
            entry["shm_size"] = POSTGRES_SHM_SIZE
            entry["healthcheck"] = {
                "test": ["CMD-SHELL", f"pg_isready -U {self.user}"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 10,
            }
        elif self.type == "mongo":
            entry["environment"] = {
                "MONGO_INITDB_ROOT_USERNAME": self.user,
                "MONGO_INITDB_ROOT_PASSWORD": password,
            }
            entry["volumes"] = [f"{self.volume}:/data/db"]
        elif self.type == "redis":
            entry["command"] = "redis-server --appendonly yes"
            entry["volumes"] = [f"{self.volume}:/data"]
        else:
            entry["volumes"] = [f"{self.volume}:/data"]

        return entry

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "port": self.port,
            "image": self.image,
            "database": self.database,
            "implicit": self.implicit,
        }


@dataclass
class Binding:
    """One service reaching one resource, under one environment variable."""

    resource: str
    env: str = ""
    # Set when the binding was derived from the legacy `datastores` list rather
    # than declared. Rendering skips those, so an unmigrated file round-trips.
    implicit: bool = False

    def resolved_env(self, resource: Resource) -> str:
        return self.env or resource.spec.env_var


@dataclass
class ResourceError(Exception):
    """A workspace whose resources and bindings do not agree."""

    problems: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "\n".join(self.problems)
