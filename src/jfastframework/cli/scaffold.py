"""File generation from Jinja2 templates.

Templates live as real files under ``jfastframework/templates/``, not as string
literals inside Python functions: a template in a string cannot be linted,
diffed or tested.

Two Jinja environments, because HTML templates are themselves Jinja::

    *.py.j2     scaffold-time vars use {{ }}
    *.html.j2   scaffold-time vars use [[ ]], so the runtime {{ }} the browser
                template needs passes through untouched

Every generated tree carries a ``.jfast-template`` stamp recording which
template produced it, so ``jfast upgrade`` can later re-apply a newer template
and show a diff instead of a rewrite. For a frontend the stamp is also where
its look is recorded, so ``jfast new view`` can match it later.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from jfastframework import languages
from jfastframework.contracts.model import CONTRACTS_FILE

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"
STAMP_FILE = ".jfast-template"

#: In the order somebody should consider them, which is also increasing cost.
#: A module is free to differ from its neighbours -- that is the point of a
#: modular monolith, and `jfast.toml` remembers which is which.
MODULE_LAYOUTS = ("layered", "modular", "screaming", "hexagonal")
#: A package per layer: the shape a module grows into without being moved.
#: Five files is where ``layered`` starts to hurt, and most modules get there.
DEFAULT_LAYOUT = "modular"

#: The contract whose layer globs match the files a layout generates. Every
#: entry is load-bearing: a contract written for another layout matches none of
#: the files on disk, so every layer rule -- `forbid_packages` included --
#: enforces nothing while `contracts check` still reports a pass.
CONTRACT_TEMPLATE_FOR: dict[str, str] = {
    "layered": "contracts_layered",
    "modular": "contracts_modular",
    "screaming": "contracts_screaming",
    "hexagonal": "contracts_hexagonal",
}
MODULE_UIS = ("api", "htmx")
SERVICE_KINDS = ("api", "web", "spa", "gateway")
FRONTENDS = ("vue", "react")

#: The look a generated frontend is drawn in, first entry the default.
#:
#: A look is only the files that draw: stylesheet, components, layout, the two
#: shipped screens and the page `jfast new view` writes. Router, stores, the
#: axios instance and the generator markers are one set shared by every look,
#: so a view generator or a 401 fix never has to be written twice. The look's
#: trees are rendered first and win any file both sides carry -- see
#: `Scaffolder.render_trees`.
FRONTEND_TEMPLATES = ("nexora", "classic")
DEFAULT_FRONTEND_TEMPLATE = FRONTEND_TEMPLATES[0]


@dataclass(frozen=True)
class PluginSpec:
    """What the installer needs to know to offer a plugin as a choice."""

    extra: str
    label: str
    # Offered by `jfast init`'s datastore prompt.
    is_datastore: bool = False
    # Pre-checked in `jfast init` and on in `jfast start`. See RECOMMENDED.
    recommended: bool = False
    # Pre-checked when the service serves several customers: a tenant has to
    # come from somebody signed in.
    multitenant: bool = False


# The menu the installer shows, and the source of the extras a generated
# service pins. `test_every_shipped_plugin_is_in_the_menu_a_generated_service_shows`
# compares it with the entry points in pyproject.toml, both ways.
#
# Recommended, and why each one earns being on by default:
#
#   telemetry  free until OTEL_EXPORTER_OTLP_ENDPOINT is set (it exports
#              nothing and its overhead is measured inside the performance
#              budget), and traces are what is missed first when something is
#              slow in production -- after the fact, when turning it on no
#              longer helps.
#   queue      anything slower than a request (a model call, a PDF, an email)
#              belongs off the request path. The default backend is the
#              PostgreSQL the service already has, so it costs no new server,
#              and `jfast worker` / the generated worker service consume it.
#
# For a multitenant service, also auth and accounts: the tenant is read from
# a signed token (`token`) or is the signed-in user (`user`), so something has
# to sign people in. A service with a separate identity provider unchecks
# accounts and points auth at the provider's JWKS.
PLUGIN_CATALOG: dict[str, PluginSpec] = {
    "observability": PluginSpec("", "Structured JSON logging with request ids"),
    "metrics": PluginSpec("metrics", "Prometheus RED metrics at /metrics"),
    "telemetry": PluginSpec(
        "telemetry",
        "Traces (OpenTelemetry), exported once an OTLP endpoint is set",
        recommended=True,
    ),
    "database": PluginSpec("db", "PostgreSQL + pgvector (SQLAlchemy, Alembic)", True),
    "cache": PluginSpec("cache", "Redis cache, pub/sub and queue", True),
    "mongo": PluginSpec("mongo", "MongoDB for document-shaped data", True),
    "qdrant": PluginSpec("qdrant", "Qdrant vector database", True),
    "rag": PluginSpec("rag", "Tenant-scoped semantic and hybrid search over pgvector or Qdrant"),
    "llm": PluginSpec("llm", "Chat, vision and embeddings with a spending cap (OpenAI-compatible)"),
    "queue": PluginSpec(
        "queue", "Background jobs on PostgreSQL, Redis or RabbitMQ", recommended=True
    ),
    "outbox": PluginSpec("db", "Jobs and events that commit with the request's rows"),
    "idempotency": PluginSpec("db", "Idempotency-Key: a retried POST gets the first answer"),
    "auth": PluginSpec("auth", "JWT verification, scopes, rotation, revocation", multitenant=True),
    "accounts": PluginSpec(
        "accounts", "Users, password login, roles and permissions", multitenant=True
    ),
    "ratelimit": PluginSpec("cache", "Per-tenant and per-subject rate limits (Redis-backed)"),
    "channels": PluginSpec("", "Declared pub/sub channels over memory, Redis or Kafka"),
    "websocket": PluginSpec("server", "Authenticated WebSocket connections, Redis fan-out"),
    "events": PluginSpec("kafka", "Kafka event streaming between services"),
    "web": PluginSpec("web", "Jinja2 templates + HTMX (server-rendered pages)"),
    "sentry": PluginSpec("sentry", "Sentry error and performance reporting"),
    "gateway": PluginSpec("gateway", "Prefix-based reverse proxy"),
    "http": PluginSpec("http", "Calls to sibling services: deadlines, retries, breakers"),
    "storage": PluginSpec("storage", "File storage on local disks, S3 or MinIO"),
    "tenancy": PluginSpec("", "Multi-tenancy by token claim, signed-in user, subdomain or path"),
    "notifications": PluginSpec("fcm", "Push notifications via Firebase (FCM)"),
    "mail": PluginSpec("mail", "Email with templates, queued by default"),
}

RECOMMENDED = tuple(n for n, s in PLUGIN_CATALOG.items() if s.recommended)
MULTITENANT_RECOMMENDED = tuple(n for n, s in PLUGIN_CATALOG.items() if s.multitenant)

#: Where a multitenant service reads the tenant from, in order of trust: the
#: token's tenant claim when an organisation owns the data, the signed-in user
#: when every account is its own tenant. Both come from a signed token, never
#: from a header a client can set.
MULTITENANT_SOURCES = ("token", "user")


def plugin_importable(name: str) -> bool:
    """Whether a catalogued plugin's module is importable in this install.

    Read from the entry point rather than imported: finding the module is
    enough to know it ships, and importing it would pull in its optional
    dependencies. A plugin catalogued ahead of its code -- one being written on
    another branch -- is then skipped by the installer with a note instead of
    written into a jfast.toml that cannot boot.
    """
    import importlib.util
    from importlib.metadata import entry_points

    modules = [
        entry.value.partition(":")[0]
        for entry in entry_points(group="jfastframework.plugins")
        if entry.name == name
    ]
    # An editable install keeps the entry points it was installed with, so a
    # builtin added since is found by its module path instead.
    modules.append(f"jfastframework.plugins.builtin.{name}")
    for module in modules:
        try:
            if importlib.util.find_spec(module) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


DATASTORE_PLUGINS = tuple(n for n, s in PLUGIN_CATALOG.items() if s.is_datastore)

# Always on, in this order, regardless of what else was chosen.
BASE_PLUGINS = ("observability", "metrics")

# File kinds whose own syntax uses ``{{ }}`` at runtime: Vue interpolation,
# JSX inline style objects, and server-rendered HTML. They render through the
# square-bracket environment so their braces survive scaffolding.
_RUNTIME_BRACE_SUFFIXES = (".html", ".htm.", ".vue", ".jsx", ".tsx")

_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    cleaned = re.sub(r"[\s\-]+", "_", name.strip())
    return _SNAKE_RE.sub("_", cleaned).lower().replace("__", "_")


def to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in to_snake(name).split("_") if part)


def to_kebab(name: str) -> str:
    return to_snake(name).replace("_", "-")


#: Languages ``pluralize`` knows, set per project with ``[scaffold] language``.
PLURAL_LANGUAGES = ("en", "es")


def pluralize(word: str, language: str = "en") -> str:
    """Pluralise a module name into a table name, in English or Spanish.

    Table names are plural because singular nouns collide with SQL reserved
    words far more often than plurals do (``order``, ``user``, ``group``). The
    rules are the regular ones, good enough for identifiers; ``--table``
    overrides a guess that is wrong.
    """
    if language == "es":
        return _pluralize_es(word)
    if word.endswith("y") and not word.endswith(("ay", "ey", "iy", "oy", "uy")):
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def _pluralize_es(word: str) -> str:
    """Spanish: the head noun -- the first word -- takes the plural.

    ``orden_compra`` is ``ordenes_compra``, where English pluralises the last
    word (``line_items``). Identifiers are ASCII, so the stress an accent would
    show is guessed: a word ending in an unstressed ``-es``/``-is`` of more
    than one syllable (``lunes``, ``tesis``) stays as it is.
    """
    head, sep, rest = word.partition("_")
    if not head:
        return word
    if head.endswith(("a", "e", "i", "o", "u")):
        plural = head + "s"
    elif head.endswith("z"):
        plural = head[:-1] + "ces"
    elif head.endswith(("es", "is")) and len(head) > 4:
        plural = head
    else:
        plural = head + "es"
    return plural + sep + rest


def resolve_plugins(kind: str, chosen: Sequence[str], *, multitenant: bool = False) -> list[str]:
    """Full, ordered plugin list for a generated service.

    Unknown names are rejected here rather than at the service's first boot,
    and `web` is forced on for a `web` service because the kind is meaningless
    without it. ``multitenant`` adds tenancy and the auth it reads the tenant
    through -- the one answer sets both, so they cannot disagree.
    """
    if multitenant:
        chosen = [*chosen, *(n for n in ("auth", "tenancy") if n not in chosen)]
    unknown = [name for name in chosen if name not in PLUGIN_CATALOG]
    if unknown:
        raise ValueError(
            f"Unknown plugin(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(PLUGIN_CATALOG))}"
        )

    selected = list(BASE_PLUGINS)
    if kind == "web" and "web" not in chosen:
        selected.append("web")
    if kind == "gateway":
        selected.append("gateway")

    for name in chosen:
        if name not in selected:
            selected.append(name)

    # rag without a store is a service that boots and then fails on first use.
    if "rag" in selected and not ({"database", "qdrant"} & set(selected)):
        selected.insert(selected.index("rag"), "database")
    # Same for the queue: its default backend is PostgreSQL.
    if "queue" in selected and not ({"database", "cache"} & set(selected)):
        selected.insert(selected.index("queue"), "database")
    return selected


def extras_for(plugins: Sequence[str]) -> str:
    """The pip extras string a generated service pins."""
    extras = {"server"}
    for name in plugins:
        spec = PLUGIN_CATALOG.get(name)
        if spec and spec.extra:
            extras.add(spec.extra)
    return ",".join(sorted(extras))


def framework_pin() -> str:
    """The version specifier a generated service should pin, derived not typed.

    Two things this gets right that a hardcoded string cannot:

    * It follows the framework. A literal pin in the template survives a
      renumbering of the framework, and every generated project then ships a
      requirements file pip cannot satisfy.
    * A pre-release is pinned **exactly**. ``~=0.1`` does not match
      ``0.1.0a1``: a compatible-release clause normalises to ``>= 0.1, == 0.*``
      and ``0.1.0a1`` sorts below ``0.1.0``, so it is out of range even with
      ``--pre``. While the API is unstable, exact is also the honest pin.
    """
    from jfastframework import __version__

    if any(marker in __version__ for marker in ("a", "b", "rc", ".dev")):
        return f"=={__version__}"
    major, _, rest = __version__.partition(".")
    minor = rest.partition(".")[0]
    return f"~={major}.{minor}"


@dataclass
class WrittenFile:
    path: Path
    created: bool


class Tree(NamedTuple):
    """One template tree, where it lands, and what only it needs to render.

    ``extra`` exists because the trees of one command do not always share a
    vocabulary. `jfast new module` renders under a module context, which has no
    project name, while the contract template it carries needs one.
    """

    template: str
    target: Path
    extra: dict[str, Any] | None = None


class Scaffolder:
    def __init__(self, template_root: Path = TEMPLATE_ROOT) -> None:
        self.template_root = template_root
        loader = FileSystemLoader(str(template_root))
        common: dict[str, Any] = {
            "loader": loader,
            "undefined": StrictUndefined,
            "keep_trailing_newline": True,
            "trim_blocks": True,
            "lstrip_blocks": True,
        }
        self.env = Environment(**common)  # nosec B701
        # Alternate delimiters so browser-side Jinja survives scaffolding.
        self.html_env = Environment(  # nosec B701
            **common,
            variable_start_string="[[",
            variable_end_string="]]",
            block_start_string="[%",
            block_end_string="%]",
            comment_start_string="[#",
            comment_end_string="#]",
        )

    def _env_for(self, relative: Path) -> Environment:
        """Pick the delimiters that will not collide with the file's own syntax.

        Vue interpolates with ``{{ }}``, JSX inlines style objects as ``{{ }}``,
        and Jinja-rendered HTML has runtime ``{{ }}`` of its own. Rendering
        those with the default delimiters eats the very syntax the file exists
        to emit, so they get the square-bracket environment instead.
        """
        name = relative.name
        return (
            self.html_env if any(marker in name for marker in _RUNTIME_BRACE_SUFFIXES) else self.env
        )

    def available_templates(self) -> list[str]:
        return sorted(p.name for p in self.template_root.iterdir() if p.is_dir())

    def render_tree(
        self,
        template: str,
        target: Path,
        context: dict[str, Any],
        *,
        force: bool = False,
        dry_run: bool = False,
        skip: Collection[Path] = (),
    ) -> list[WrittenFile]:
        source = self.template_root / template
        if not source.is_dir():
            raise FileNotFoundError(
                f"Unknown template {template!r}. "
                f"Available: {', '.join(self.available_templates()) or '<none>'}"
            )

        written: list[WrittenFile] = []
        for path in sorted(source.rglob("*")):
            if path.is_dir() or path.name == STAMP_FILE:
                continue
            relative = path.relative_to(source)
            # Directory and file names are themselves templated, so a module
            # named "orders" lands in modules/orders/, not modules/{{module}}/.
            rendered_name = self.env.from_string(str(relative).replace(".j2", "")).render(**context)
            destination = target / rendered_name
            if destination in skip:
                continue

            if destination.exists() and not force:
                written.append(WrittenFile(destination, created=False))
                continue

            if not path.name.endswith(".j2"):
                # Not a template: an image, a font -- copied byte for byte,
                # because rendering one as text would corrupt it.
                if not dry_run:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(path.read_bytes())
                written.append(WrittenFile(destination, created=True))
                continue

            env = self._env_for(relative)
            content = env.get_template(f"{template}/{relative.as_posix()}").render(**context)
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            written.append(WrittenFile(destination, created=True))

        if not dry_run:
            self._write_stamp(target, template, context)
        return written

    def render_trees(
        self,
        trees: Sequence[Tree],
        context: dict[str, Any],
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> list[WrittenFile]:
        """Compose several template trees into (possibly different) targets.

        Composition instead of multiplication: a layout tree plus an optional
        UI overlay covers layered/screaming x api/htmx with three trees rather
        than four copies that drift apart.

        The first tree to claim a path owns it. That is what lets a frontend
        look replace a stylesheet or a component of the shared frontend tree
        without a copy of the rest -- and it holds under ``--force`` too, where
        "the last write wins" would hand every overridden file back to the
        tree underneath.
        """
        written: list[WrittenFile] = []
        claimed: set[Path] = set()
        for tree in trees:
            merged = {**context, **tree.extra} if tree.extra else context
            files = self.render_tree(
                tree.template, tree.target, merged, force=force, dry_run=dry_run, skip=claimed
            )
            claimed.update(file.path for file in files)
            written.extend(files)
        return written

    def _write_stamp(self, target: Path, template: str, context: dict[str, Any]) -> None:
        stamp_path = target / STAMP_FILE
        existing: dict[str, Any] = {}
        if stamp_path.is_file():
            try:
                existing = json.loads(stamp_path.read_text(encoding="utf-8"))
            except ValueError:
                existing = {}
        from jfastframework import __version__

        existing.setdefault("templates", {})
        existing["templates"][template] = {
            "framework_version": __version__,
            "context": {k: v for k, v in context.items() if isinstance(v, str | int | bool)},
        }
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def format_generated(paths: Sequence[Path], root: Path) -> bool:
    """Sort imports and format the Python files a command just wrote, with ruff.

    The templates are written in ruff's format, but a name is only known at
    generation time, and `PresupuestoHexagonalUseCases(SqlPresupuestoHexagonal
    Repository(session, ...))` is a line no template can wrap in advance. The
    project's own ruff.toml is used (``cwd=root``), so the result is exactly
    what `ruff format --check .` expects. Only the files passed in are touched:
    a file the user already edited is never reformatted behind their back.

    Returns False when ruff is not installed where `jfast` runs -- it is in
    ``jfastframework[dev]``, which a generated requirements-dev.txt installs --
    and the files are then left as rendered.
    """
    import importlib.util
    import shutil
    import subprocess
    import sys

    files = [str(path) for path in paths if path.suffix == ".py" and path.is_file()]
    if not files:
        return True
    if importlib.util.find_spec("ruff") is not None:
        command = [sys.executable, "-m", "ruff"]
    elif (found := shutil.which("ruff")) is not None:
        command = [found]
    else:
        return False
    for arguments in (["check", "--fix", "--select", "I", "--quiet"], ["format", "--quiet"]):
        subprocess.run([*command, *arguments, *files], cwd=root, check=False, capture_output=True)
    return True


def module_context(
    name: str,
    *,
    layout: str = DEFAULT_LAYOUT,
    ui: str = "api",
    table: str | None = None,
    modules_dir: str = "modules",
    language: str = "en",
    fields: str | None = None,
    unique: Sequence[str] = (),
    bare: bool = False,
    access: str = "open",
) -> dict[str, Any]:
    """The vocabulary a module template renders with.

    ``fields``/``unique``/``bare`` are `jfast new module`'s flags, parsed by
    :mod:`jfastframework.cli.fields`; with none of them the module carries the
    example fields it always has. ``access`` is how the generated routes find
    out who is asking -- see :data:`ROUTE_ACCESS`.
    """
    from jfastframework.cli.fields import module_fields

    if access not in ROUTE_ACCESS:
        raise ValueError(f"Unknown access {access!r}. Choose from: {', '.join(ROUTE_ACCESS)}")
    snake = to_snake(name)
    plural = pluralize(snake, language)
    resolved_table = table or plural
    declared = module_fields(fields, unique, bare=bare, table=resolved_table)
    return {
        "module": snake,
        "Module": to_pascal(name),
        "module_title": snake.replace("_", " ").title(),
        "module_plural": plural.replace("_", " "),
        "table": resolved_table,
        "layout": layout,
        "ui": ui,
        "modules_dir": modules_dir,
        "access": access,
        **declared.as_context(),
    }


#: How a generated module's routes learn who is asking, in the order a service
#: grows into them:
#:
#: ``open``    no auth plugin: the routes are public, and the tenant is whatever
#:             the request resolved to -- nothing, in a service without tenancy.
#: ``auth``    auth on, one customer: every route needs a signed-in caller
#:             (`require_auth`), and rows are written with no tenant.
#: ``tenant``  tenancy on: every route runs as the caller's tenant
#:             (`current_tenant`), 401 without a session and 403 without a
#:             tenant, so a request can never fall back to "all tenants".
ROUTE_ACCESS = ("open", "auth", "tenant")


def route_access_for(enabled_plugins: Collection[str]) -> str:
    """The access a service's plugins imply for the modules generated in it."""
    if "tenancy" in enabled_plugins:
        return "tenant"
    if "auth" in enabled_plugins or "accounts" in enabled_plugins:
        return "auth"
    return "open"


def service_context(
    name: str,
    *,
    kind: str = "api",
    port: int = 8000,
    plugins: Sequence[str] = (),
    rag_store: str | None = None,
    queue_backend: str | None = None,
    workspace_name: str = "workspace",
    api_base_url: str = "",
    frontend: str | None = None,
    frontend_template: str | None = None,
    routes: Sequence[dict[str, str]] = (),
    language: str = "python",
    sample_module: str = "item",
    agent_docs: bool = False,
    grpc: bool = False,
    multitenant: bool = False,
    frontend_accounts: bool = True,
) -> dict[str, Any]:
    snake = to_snake(name)
    enabled = resolve_plugins(kind, plugins, multitenant=multitenant)
    # Pick the vector store the service can actually reach. Enabling `rag` and
    # `qdrant` but writing `store = "pgvector"` produces a service that boots
    # and then fails on the first search -- the exact trap the plugin's own
    # startup check exists to catch, and one the generator should never set.
    if rag_store is None:
        rag_store = "pgvector" if "database" in enabled else "qdrant"
    # Same reasoning for the queue: point it at a backend the service has.
    if queue_backend is None:
        queue_backend = "postgres" if "database" in enabled else "redis"

    # Datastores a non-Python service declares for itself: it has no plugin
    # graph, so the workspace reads this list to build its compose entry.
    datastores = [name for name in DATASTORE_PLUGINS if name in enabled]

    context: dict[str, Any] = {
        "project": snake,
        "Project": snake.replace("_", " ").title(),
        "service": snake,
        "Service": to_pascal(name),
        "service_slug": to_kebab(name),
        "service_title": snake.replace("_", " ").title(),
        "kind": kind,
        "agent_docs": agent_docs,
        "language": language,
        "port": port,
        "is_web": kind == "web",
        "grpc": grpc,
        "grpc_port": port + 9,
        "frontend": frontend,
        # Stamped into `.jfast-template` with the rest of the context, which is
        # the one place `jfast new view` reads the look back from.
        "frontend_template": (frontend_template or DEFAULT_FRONTEND_TEMPLATE)
        if kind == "spa"
        else None,
        "workspace_name": workspace_name,
        "api_base_url": api_base_url or f"http://localhost:{port}",
        "routes": list(routes),
        "rag_store": rag_store,
        "queue_backend": queue_backend,
        "datastores": datastores,
        "enabled_plugins": enabled,
        # One answer, every piece: tenancy, how routes are guarded, whether RAG
        # and the LLM budget are per tenant. See docs/local-setup.md.
        "multitenant": "tenancy" in enabled,
        "tenancy_sources": list(MULTITENANT_SOURCES)
        if multitenant
        else (["token"] if "auth" in enabled else []) + ["subdomain"],
        "route_access": route_access_for(enabled),
        # Read by the frontend templates: account pages and a private-by-default
        # router only when some backend in the workspace has `accounts`.
        "frontend_accounts": frontend_accounts,
        "extras": extras_for(enabled),
        "framework_pin": framework_pin(),
        "available_plugins": [
            (n, spec.extra)
            for n, spec in PLUGIN_CATALOG.items()
            if n not in enabled and n not in BASE_PLUGINS
        ],
        **{f"has_{n}": n in enabled for n in PLUGIN_CATALOG},
    }
    if language != "python":
        # Non-Python templates ship one sample domain module and need the same
        # naming vocabulary the Python module templates use.
        context.update(module_context(sample_module))
    return context


def view_context(
    name: str, *, frontend: str = "vue", frontend_template: str = "classic"
) -> dict[str, Any]:
    """Context for a frontend module (his `Modulo<Name>` structure)."""
    pascal = to_pascal(name)
    slug = to_kebab(name)
    return {
        "View": pascal,
        "view_slug": slug,
        "view_snake": to_snake(name),
        # "BillingAccount" -> "Billing Account", which is what a sidebar label
        # and a page heading should read.
        "view_title": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", pascal),
        "view_path": f"/{slug}",
        "frontend": frontend,
        "frontend_template": frontend_template,
    }


def contract_tree(layout: str, project_root: Path) -> Tree | None:
    """The contract this layout needs, unless the project already wrote one.

    The first module is where the layout stops being hypothetical, so it is
    where the contract can first be chosen honestly -- a service has no module
    yet, and it may end up holding modules of several layouts.

    Nothing is ever replaced. A contract on disk is a document its owner has
    had the chance to edit, and the layer paths are the least of what it
    carries. That also keeps `.jfast-template` truthful: adding the tree
    unconditionally would stamp a template that never wrote a byte.
    """
    if (project_root / CONTRACTS_FILE).is_file():
        return None
    project = to_snake(project_root.resolve().name)
    return Tree(
        CONTRACT_TEMPLATE_FOR[layout],
        project_root,
        {"project": project, "Project": project.replace("_", " ").title(), "layout": layout},
    )


def module_trees(layout: str, ui: str, target: Path, project_root: Path) -> list[Tree]:
    """Which template trees to render for a module, and where."""
    if layout not in MODULE_LAYOUTS:
        raise ValueError(f"Unknown layout {layout!r}. Choose from: {', '.join(MODULE_LAYOUTS)}")
    if ui not in MODULE_UIS:
        raise ValueError(f"Unknown ui {ui!r}. Choose from: {', '.join(MODULE_UIS)}")

    trees: list[Tree] = [Tree(f"module_{layout}", target)]
    if ui == "htmx":
        # The overlay writes into the project root: the HTML router goes next
        # to the module, the templates go in the service-wide templates dir.
        trees.append(Tree("ui_htmx", project_root))
    contract = contract_tree(layout, project_root)
    if contract is not None:
        trees.append(contract)
    return trees


def service_trees(
    kind: str,
    frontend: str | None,
    target: Path,
    *,
    language: str = "python",
    grpc: bool = False,
    agent_docs: bool = False,
    layout: str | None = None,
    frontend_template: str = DEFAULT_FRONTEND_TEMPLATE,
) -> list[Tree]:
    """Which template trees make up a service of this kind.

    ``layout`` is the contract's, and ``None`` means nobody has said yet. A
    service is generated before any module exists, so at this point the only
    thing a layout could be is a guess -- and a guessed layered contract in a
    hexagonal, modular or screaming service matches no file and enforces
    nothing. The contract is deferred to the first
    `jfast new module`, which knows. Pass ``layout`` when the caller does.

    ``agent_docs`` adds the surface an AI agent reads before it writes: an
    ``AGENTS.md`` and a skill under ``.jfast/skills/``. Off by default, because
    a project generated for a human who will never point an agent at it does
    not need two extra files it has to keep true.

    It is worth turning on for more than tidiness. The generated stylesheets
    tell a reader to consult ``.jfast/skills/design-system/SKILL.md``, and
    without this that path is written into every frontend and points at
    nothing.

    ``frontend_template`` is the look of an SPA and means nothing for any other
    kind. A non-default look is its own trees in front of the shared one.
    """
    if kind not in SERVICE_KINDS:
        raise ValueError(f"Unknown kind {kind!r}. Choose from: {', '.join(SERVICE_KINDS)}")
    if layout is not None and layout not in MODULE_LAYOUTS:
        raise ValueError(f"Unknown layout {layout!r}. Choose from: {', '.join(MODULE_LAYOUTS)}")

    if language != "python":
        spec = languages.get(language)
        if kind not in spec.kinds:
            raise ValueError(
                f"{spec.label} does not support --kind {kind}. Supported: {', '.join(spec.kinds)}."
            )
        polyglot: list[Tree] = [Tree(spec.template, target)]
        if grpc:
            polyglot.append(Tree("proto", target))
        return polyglot

    if kind == "spa":
        if frontend not in FRONTENDS:
            raise ValueError(
                f"Frontend {frontend!r} is not implemented. "
                f"Choose from: {', '.join(FRONTENDS)}. "
                f"Angular is not generated -- see PLAN.md phase 3."
            )
        check_frontend_template(frontend_template)
        spa: list[Tree] = []
        if frontend_template != "classic":
            # Framework-specific files first, then what one look shares between
            # Vue and React, then the base: the first tree to claim a path wins.
            spa.append(Tree(f"frontend_{frontend}_{frontend_template}", target))
            spa.append(Tree(f"frontend_{frontend_template}", target))
        spa.append(Tree(f"frontend_{frontend}", target))
        if agent_docs:
            # The design skill lives with the thing it describes, which for a
            # frontend project is the frontend project.
            spa.append(Tree("agent_design", target))
            if frontend_template == "nexora":
                # JFast Suite, the pages the look was drawn from, as a
                # catalogue an agent or a person can open before building a
                # screen the project does not have yet.
                spa.append(Tree("agent_design_nexora", target))
        return spa

    if kind == "gateway":
        # The gateway shares nothing with an application service: no modules,
        # no migrations, no database. Its own tree keeps it that way.
        return [Tree("service_gateway", target)]

    trees: list[Tree] = [Tree("service_base", target)]
    if layout is not None:
        trees.append(Tree(CONTRACT_TEMPLATE_FOR[layout], target, {"layout": layout}))
    if kind == "web":
        trees.append(Tree("service_web", target))
    if agent_docs:
        trees.append(Tree("agent_docs", target))
        if kind == "web":
            # Server-rendered pages are still pages: the same design rules
            # apply, and app.css already points at the skill.
            trees.append(Tree("agent_design", target))
    return trees


def detect_frontend(root: Path) -> str | None:
    """Work out which framework a frontend project uses.

    Asking the user to repeat ``--frontend react`` inside a React project is
    how you end up with Vue files in it. The router filename is the cheapest
    reliable signal; package.json is the fallback for a project whose router
    has been moved.
    """
    router_dir = root / "src" / "router"
    if (router_dir / "index.jsx").is_file():
        return "react"
    if (router_dir / "index.js").is_file():
        return "vue"

    package = root / "package.json"
    if package.is_file():
        try:
            deps = json.loads(package.read_text(encoding="utf-8")).get("dependencies", {})
        except ValueError:
            return None
        if "react" in deps:
            return "react"
        if "vue" in deps:
            return "vue"
    return None


def check_frontend_template(template: str) -> None:
    """Raise on a look nobody ships, naming the ones that exist."""
    if template not in FRONTEND_TEMPLATES:
        raise ValueError(
            f"Unknown frontend template {template!r}. Choose from: {', '.join(FRONTEND_TEMPLATES)}."
        )


def detect_frontend_template(root: Path) -> str | None:
    """The look a frontend project was generated with, read from its stamp.

    The stamp is the single record of it: the choice is made once, at
    generation, and every later `jfast new view` has to draw its page in the
    same look or the new screen is the odd one out.

    A project stamped before looks existed has a frontend entry and no
    template in it, and that project can only be classic -- the one look there
    was. ``None`` means there is no frontend stamp here at all.
    """
    stamp = root / STAMP_FILE
    if not stamp.is_file():
        return None
    try:
        templates = json.loads(stamp.read_text(encoding="utf-8")).get("templates", {})
    except (ValueError, AttributeError):
        return None
    if not isinstance(templates, dict):
        return None

    stamped_frontend = False
    for name, entry in templates.items():
        if not name.startswith("frontend_") or not isinstance(entry, dict):
            continue
        stamped_frontend = True
        chosen = (entry.get("context") or {}).get("frontend_template")
        if chosen in FRONTEND_TEMPLATES:
            return str(chosen)
    return "classic" if stamped_frontend else None


def view_trees(frontend: str, target: Path, *, frontend_template: str = "classic") -> list[Tree]:
    if frontend not in FRONTENDS:
        raise ValueError(
            f"Frontend {frontend!r} is not supported for view generation. "
            f"Choose from: {', '.join(FRONTENDS)}."
        )
    check_frontend_template(frontend_template)
    trees: list[Tree] = []
    if frontend_template != "classic":
        # Only the page is drawn differently; routes, service and folders are
        # the shared tree's.
        trees.append(Tree(f"view_{frontend}_{frontend_template}", target))
    trees.append(Tree(f"view_{frontend}", target))
    return trees
