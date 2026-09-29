"""Workspaces: several services that know about each other.

A single service needs no workspace. The moment there are two, three questions
appear that a per-service config cannot answer:

* which ports are already taken;
* what the frontend should call;
* whether anything sits in front of them.

``jfast.workspace.toml`` answers all three. It is the only file that knows the
whole system, and it is what makes the gateway and the frontend `.env` generate
themselves instead of being hand-maintained.

    [workspace]
    name = "cometax"
    base_port = 8000

    [[workspace.services]]
    name = "billing"
    kind = "api"
    port = 8010
    path = "billing"
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jfastframework.resources import (
    BY_LEGACY_NAME,
    RESOURCE_PORT_BAND,
    Binding,
    Resource,
    ResourceError,
)

WORKSPACE_FILE = "jfast.workspace.toml"
PORT_BLOCK_SIZE = 10
DEFAULT_BASE_PORT = 8000

# Kinds that expose an HTTP API a gateway can route to.
BACKEND_KINDS = frozenset({"api", "web"})
FRONTEND_KINDS = frozenset({"spa"})
GATEWAY_KIND = "gateway"


@dataclass
class ServiceEntry:
    name: str
    kind: str
    port: int
    path: str
    # Only for kind == "spa": vue | react.
    frontend: str | None = None
    # python | go. A service's language changes how it is built and tested,
    # and nothing else -- the contract it satisfies is identical.
    language: str = "python"
    grpc: bool = False
    # Datastores the service needs, by *type*. The original model, kept so
    # a 0.1 workspace file still loads and still renders the same compose.
    # `uses` is the replacement: it names instances.
    datastores: list[str] = field(default_factory=list)
    # Resources this service connects to, and the variable each lands in.
    uses: list[Binding] = field(default_factory=list)

    @property
    def grpc_port(self) -> int:
        """Offset +9, at the end of the block so it never meets a datastore."""
        return self.port + 9

    @property
    def is_backend(self) -> bool:
        return self.kind in BACKEND_KINDS

    @property
    def is_frontend(self) -> bool:
        return self.kind in FRONTEND_KINDS

    @property
    def is_gateway(self) -> bool:
        return self.kind == GATEWAY_KIND

    @property
    def prefix(self) -> str:
        """URL prefix the gateway routes to this service under."""
        return f"/{self.name.replace('_', '-')}"

    @property
    def internal_url(self) -> str:
        """How sibling containers reach it on the compose network."""
        return f"http://{self.name}:{self.port}"

    @property
    def local_url(self) -> str:
        """How a developer reaches it from the host."""
        return f"http://localhost:{self.port}"


@dataclass
class Workspace:
    name: str
    base_port: int = DEFAULT_BASE_PORT
    services: list[ServiceEntry] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    file: Path | None = None

    # -- discovery -----------------------------------------------------

    @staticmethod
    def find(start: Path | None = None) -> Path | None:
        """Nearest ``jfast.workspace.toml`` at or above ``start``, bounded.

        A walk to the root of the filesystem would let a workspace file
        left in the home directory by one ``jfast start`` capture *every*
        project underneath: one compose file, one port space, services from
        unrelated work registering against each other, and nothing failing.

        So the search stops at two boundaries. It never considers the home
        directory itself -- a workspace file there is an accident, not a
        project -- and it stops after a directory containing ``.git``,
        because a repository root is where a project ends.
        """
        current = (start or Path.cwd()).resolve()
        try:
            home = Path.home().resolve()
        except (OSError, RuntimeError):  # no home on this platform
            home = None

        for candidate in (current, *current.parents):
            if home is not None and candidate == home:
                break
            path = candidate / WORKSPACE_FILE
            if path.is_file():
                return path
            if (candidate / ".git").exists():
                break
        return None

    @classmethod
    def load(cls, path: Path) -> Workspace:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        section: dict[str, Any] = raw.get("workspace", {})
        services = [
            ServiceEntry(
                name=entry["name"],
                kind=entry.get("kind", "api"),
                port=int(entry["port"]),
                path=entry.get("path", entry["name"]),
                frontend=entry.get("frontend"),
                language=entry.get("language", "python"),
                grpc=bool(entry.get("grpc", False)),
                datastores=list(entry.get("datastores", [])),
                uses=[
                    Binding(
                        resource=use["resource"],
                        env=use.get("as", ""),
                    )
                    for use in entry.get("uses", [])
                ],
            )
            for entry in section.get("services", [])
        ]
        resources = [
            Resource(
                name=entry["name"],
                type=entry["type"],
                port=int(entry["port"]),
                image=entry.get("image", ""),
                database=entry.get("database", ""),
                user=entry.get("user", "app"),
            )
            for entry in section.get("resources", [])
        ]
        return cls(
            name=section.get("name", path.parent.name),
            base_port=int(section.get("base_port", DEFAULT_BASE_PORT)),
            services=services,
            resources=resources,
            file=path,
        )

    @classmethod
    def load_or_none(cls, start: Path | None = None) -> Workspace | None:
        path = cls.find(start)
        return cls.load(path) if path else None

    # -- mutation ------------------------------------------------------

    def get(self, name: str) -> ServiceEntry | None:
        return next((s for s in self.services if s.name == name), None)

    def next_port(self) -> int:
        """First free port block.

        Blocks are ten wide because each service's plugins claim offsets
        inside them (PostgreSQL at +1, Redis at +3, Qdrant at +7...). Handing
        out consecutive ports would make two services fight over the same
        database container port.
        """
        if not self.services:
            return self.base_port + PORT_BLOCK_SIZE
        return max(s.port for s in self.services) + PORT_BLOCK_SIZE

    def add(self, entry: ServiceEntry, *, replace: bool = False) -> ServiceEntry:
        existing = self.get(entry.name)
        if existing is not None and not replace:
            raise ValueError(
                f"Service {entry.name!r} is already in the workspace on port {existing.port}."
            )
        if existing is not None:
            self.services.remove(existing)

        clash = next((s for s in self.services if s.port == entry.port), None)
        if clash is not None:
            raise ValueError(
                f"Port {entry.port} is already taken by {clash.name!r}. "
                f"Next free block starts at {self.next_port()}."
            )
        self.services.append(entry)
        self.services.sort(key=lambda s: s.port)
        return entry

    # -- queries -------------------------------------------------------

    @property
    def backends(self) -> list[ServiceEntry]:
        return [s for s in self.services if s.is_backend]

    @property
    def frontends(self) -> list[ServiceEntry]:
        return [s for s in self.services if s.is_frontend]

    @property
    def gateway(self) -> ServiceEntry | None:
        return next((s for s in self.services if s.is_gateway), None)

    def needs_gateway(self) -> bool:
        """True once more than one backend exists and nothing fronts them.

        One service does not need a gateway -- adding one buys a hop and an
        outage surface for nothing. Two is where clients start needing to know
        too many hostnames.
        """
        return len(self.backends) > 1 and self.gateway is None

    def api_base_url(self, *, internal: bool = False) -> str:
        """What a frontend should call.

        The gateway when there is one, the single backend when there is not,
        and the workspace's own base port as a last resort.
        """
        target = self.gateway or (self.backends[0] if self.backends else None)
        if target is None:
            return f"http://localhost:{self.base_port}"
        return target.internal_url if internal else target.local_url

    # -- resources -----------------------------------------------------

    def resource(self, name: str) -> Resource | None:
        return next((r for r in self.resources if r.name == name), None)

    def next_resource_port(self) -> int:
        """First free port in the resource band.

        Resources live above the base port and below the first service block,
        so a resource can never land inside a service's ten wide block. The
        band starts at ``base_port + 900``; with blocks of ten that leaves room
        for ninety services before the two ranges could meet, and
        ``validate()`` catches it if they ever do.
        """
        floor = self.base_port + RESOURCE_PORT_BAND
        taken = {r.port for r in self.resources}
        port = floor
        while port in taken:
            port += 1
        return port

    def add_resource(self, resource: Resource, *, replace: bool = False) -> Resource:
        existing = self.resource(resource.name)
        if existing is not None and not replace:
            raise ValueError(f"Resource {resource.name!r} already exists on port {existing.port}.")
        if existing is not None:
            self.resources.remove(existing)

        clash = next((r for r in self.resources if r.port == resource.port), None)
        if clash is not None:
            raise ValueError(
                f"Port {resource.port} is already taken by resource {clash.name!r}. "
                f"Next free resource port is {self.next_resource_port()}."
            )
        self.resources.append(resource)
        self.resources.sort(key=lambda r: r.port)
        return resource

    def link(self, service_name: str, resource_name: str, *, env: str = "") -> Binding:
        """Connect a service to a resource, and say which variable carries it."""
        service = self.get(service_name)
        if service is None:
            known = ", ".join(sorted(s.name for s in self.services)) or "none"
            raise ValueError(f"No service named {service_name!r}. Known: {known}.")
        resource = self.resource(resource_name)
        if resource is None:
            known = ", ".join(sorted(r.name for r in self.resources)) or "none"
            raise ValueError(f"No resource named {resource_name!r}. Known: {known}.")

        variable = env or resource.spec.env_var
        # Re-linking the same pair renames the variable; it is not an error.
        for existing in service.uses:
            if existing.resource == resource_name:
                existing.env = variable
                return existing

        clash = next(
            (b for b in service.uses if self._binding_env(b) == variable),
            None,
        )
        if clash is not None:
            raise ValueError(
                f"{service_name!r} already binds {clash.resource!r} to {variable}. "
                f"Two resources cannot share one variable -- pass --as to choose "
                f"another."
            )

        binding = Binding(resource=resource_name, env=variable)
        service.uses.append(binding)
        # The legacy list would otherwise keep generating a second, implicit
        # container for the same type.
        legacy = resource.spec.legacy
        if legacy in service.datastores:
            service.datastores.remove(legacy)
        return binding

    def unlink(self, service_name: str, resource_name: str) -> bool:
        service = self.get(service_name)
        if service is None:
            return False
        before = len(service.uses)
        service.uses = [b for b in service.uses if b.resource != resource_name]
        return len(service.uses) != before

    def _binding_env(self, binding: Binding) -> str:
        resource = self.resource(binding.resource)
        return binding.env or (resource.spec.env_var if resource else "")

    def implicit_resources(self, service: ServiceEntry) -> list[Resource]:
        """Resources a service's legacy ``datastores`` list still implies.

        Named and ported exactly as the 0.1 generator did, so an unmigrated
        workspace produces a byte-identical compose file.
        """
        found: list[Resource] = []
        for legacy in service.datastores:
            spec = BY_LEGACY_NAME.get(legacy)
            if spec is None:
                continue
            found.append(
                Resource(
                    name=f"{service.name}-{legacy}",
                    type=spec.name,
                    port=service.port + spec.legacy_offset,
                    implicit=True,
                )
            )
        return found

    def bindings_for(self, service: ServiceEntry) -> list[tuple[Binding, Resource]]:
        """Every resource this service connects to, declared or implied."""
        pairs: list[tuple[Binding, Resource]] = []
        for binding in service.uses:
            resource = self.resource(binding.resource)
            if resource is not None:
                pairs.append((binding, resource))
        for resource in self.implicit_resources(service):
            pairs.append((Binding(resource=resource.name, implicit=True), resource))
        return pairs

    def all_resources(self) -> list[Resource]:
        """Declared resources plus the ones the legacy lists still imply."""
        seen: dict[str, Resource] = {r.name: r for r in self.resources}
        for service in self.services:
            for resource in self.implicit_resources(service):
                seen.setdefault(resource.name, resource)
        return sorted(seen.values(), key=lambda r: r.port)

    def environment_for(self, service: ServiceEntry, *, internal: bool = True) -> dict[str, str]:
        """The variables this service needs to reach what it is bound to.

        It comes from the same declaration as the containers it points at,
        so the two cannot drift apart.
        """
        env: dict[str, str] = {}
        for binding, resource in self.bindings_for(service):
            env[binding.resolved_env(resource)] = resource.dsn(internal=internal)
        return env

    def migrate_resources(self) -> list[Resource]:
        """Rewrite legacy ``datastores`` lists as explicit resources and bindings.

        Idempotent, and it preserves every port, so the compose file it
        produces afterwards is the one it produced before.
        """
        promoted: list[Resource] = []
        for service in self.services:
            for resource in self.implicit_resources(service):
                if self.resource(resource.name) is None:
                    resource.implicit = False
                    self.add_resource(resource)
                    promoted.append(resource)
                self.link(service.name, resource.name)
            service.datastores = []
        return promoted

    def validate(self) -> list[str]:
        """Everything that would make the generated output wrong or ambiguous."""
        problems: list[str] = []

        by_port: dict[int, list[str]] = {}
        for resource in self.all_resources():
            by_port.setdefault(resource.port, []).append(f"resource {resource.name!r}")
        for service in self.services:
            by_port.setdefault(service.port, []).append(f"service {service.name!r}")
        for port, owners in sorted(by_port.items()):
            if len(owners) > 1:
                problems.append(f"port {port} is claimed by {' and '.join(sorted(owners))}")

        names = [r.name for r in self.all_resources()]
        for name in sorted(set(names)):
            if names.count(name) > 1:
                problems.append(f"resource {name!r} is declared more than once")

        for service in self.services:
            variables: dict[str, str] = {}
            for binding in service.uses:
                if self.resource(binding.resource) is None:
                    problems.append(
                        f"service {service.name!r} binds {binding.resource!r}, "
                        f"which is not a resource in this workspace"
                    )
                    continue
                variable = self._binding_env(binding)
                if variable in variables:
                    problems.append(
                        f"service {service.name!r} binds both {variables[variable]!r} "
                        f"and {binding.resource!r} to {variable}"
                    )
                variables[variable] = binding.resource

        used = {b.resource for s in self.services for b in s.uses}
        for resource in self.resources:
            if resource.name not in used:
                problems.append(f"resource {resource.name!r} is declared but nothing uses it")

        return problems

    def require_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise ResourceError(problems=problems)

    # -- persistence ---------------------------------------------------

    def render(self) -> str:
        lines = [
            "# JFast workspace. Generated and updated by the `jfast` CLI.",
            "# Ports are allocated in blocks of ten; see docs/deploy.md.",
            "",
            "[workspace]",
            f'name = "{self.name}"',
            f"base_port = {self.base_port}",
        ]
        for resource in sorted(self.resources, key=lambda r: r.port):
            lines += [
                "",
                "[[workspace.resources]]",
                f'name = "{resource.name}"',
                f'type = "{resource.type}"',
                f"port = {resource.port}",
            ]
            if resource.image != resource.spec.image:
                lines.append(f'image = "{resource.image}"')
            if resource.database and resource.database != resource.spec.default_database:
                lines.append(f'database = "{resource.database}"')
            if resource.user != "app":
                lines.append(f'user = "{resource.user}"')

        for service in sorted(self.services, key=lambda s: s.port):
            lines += [
                "",
                "[[workspace.services]]",
                f'name = "{service.name}"',
                f'kind = "{service.kind}"',
                f"port = {service.port}",
                f'path = "{service.path}"',
            ]
            if service.language != "python":
                lines.append(f'language = "{service.language}"')
            if service.frontend:
                lines.append(f'frontend = "{service.frontend}"')
            if service.grpc:
                lines.append("grpc = true")
            if service.datastores:
                stores = ", ".join(f'"{name}"' for name in service.datastores)
                lines.append(f"datastores = [{stores}]")
            if service.uses:
                lines.append("uses = [")
                for binding in service.uses:
                    # Not `resource`: that name is already a Resource in this
                    # function, and shadowing it hides a type error.
                    bound = self.resource(binding.resource)
                    variable = binding.resolved_env(bound) if bound else binding.env
                    lines.append(f'  {{ resource = "{binding.resource}", as = "{variable}" }},')
                lines.append("]")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | None = None) -> Path:
        destination = path or self.file
        if destination is None:
            raise ValueError("Workspace has no file to save to")
        destination.write_text(self.render(), encoding="utf-8")
        self.file = destination
        self._ignore_env(destination.parent)
        return destination

    @staticmethod
    def _ignore_env(root: Path) -> None:
        """Ignore the `.env` this workspace's own commands generate beside it.

        Here rather than in the body of `jfast workspace init`, because that is
        not the only command that creates a workspace: `jfast start` and
        `jfast init` build one and call `save()` directly, then generate a
        resource password into `.env` -- into an unignored file, while
        announcing it as gitignored. Anything that writes a workspace has to
        pass through here, so this is the one place that cannot be missed.
        """
        ignore = root / ".gitignore"
        rules = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        if ".env" in rules.split():
            return
        ignore.write_text(
            rules + ("" if rules.endswith("\n") or not rules else "\n") + ".env\n",
            encoding="utf-8",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_port": self.base_port,
            "services": [
                {**asdict(s), "environment": self.environment_for(s)} for s in self.services
            ],
            "resources": [r.describe() for r in self.all_resources()],
            "api_base_url": self.api_base_url(),
            "needs_gateway": self.needs_gateway(),
        }
