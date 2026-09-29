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
and show a diff instead of a rewrite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
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


@dataclass(frozen=True)
class PluginSpec:
    """What the installer needs to know to offer a plugin as a choice."""

    extra: str
    label: str
    # Offered by `jfast init`'s datastore prompt.
    is_datastore: bool = False


# The menu the installer shows, and the source of the extras a generated
# service pins. Keep it in step with the entry points in pyproject.toml.
PLUGIN_CATALOG: dict[str, PluginSpec] = {
    "observability": PluginSpec("", "Structured JSON logging with request ids"),
    "metrics": PluginSpec("metrics", "Prometheus RED metrics at /metrics"),
    "database": PluginSpec("db", "PostgreSQL + pgvector (SQLAlchemy, Alembic)", True),
    "cache": PluginSpec("cache", "Redis cache, pub/sub and queue", True),
    "mongo": PluginSpec("mongo", "MongoDB for document-shaped data", True),
    "qdrant": PluginSpec("qdrant", "Qdrant vector database", True),
    "rag": PluginSpec("rag", "Semantic search over pgvector or Qdrant"),
    "queue": PluginSpec("queue", "Background jobs on PostgreSQL, Redis or RabbitMQ"),
    "outbox": PluginSpec("db", "Jobs and events that commit with the request's rows"),
    "idempotency": PluginSpec("db", "Idempotency-Key: a retried POST gets the first answer"),
    "auth": PluginSpec("auth", "JWT verification, scopes, rotation, revocation"),
    "accounts": PluginSpec("accounts", "Users, password login, roles and permissions"),
    "ratelimit": PluginSpec("cache", "Per-tenant and per-subject rate limits (Redis-backed)"),
    "channels": PluginSpec("", "Declared pub/sub channels over memory, Redis or Kafka"),
    "websocket": PluginSpec("server", "Authenticated WebSocket connections, Redis fan-out"),
    "events": PluginSpec("kafka", "Kafka event streaming between services"),
    "web": PluginSpec("web", "Jinja2 templates + HTMX (server-rendered pages)"),
    "sentry": PluginSpec("sentry", "Sentry error and performance reporting"),
    "gateway": PluginSpec("gateway", "Prefix-based reverse proxy"),
    "storage": PluginSpec("storage", "File storage on local disks, S3 or MinIO"),
    "tenancy": PluginSpec("", "Multi-tenancy by subdomain, token claim or path"),
    "notifications": PluginSpec("fcm", "Push notifications via Firebase (FCM)"),
    "mail": PluginSpec("mail", "Email with templates, queued by default"),
}

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


def resolve_plugins(kind: str, chosen: Sequence[str]) -> list[str]:
    """Full, ordered plugin list for a generated service.

    Unknown names are rejected here rather than at the service's first boot,
    and `web` is forced on for a `web` service because the kind is meaningless
    without it.
    """
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

            if destination.exists() and not force:
                written.append(WrittenFile(destination, created=False))
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
        """
        written: list[WrittenFile] = []
        for tree in trees:
            merged = {**context, **tree.extra} if tree.extra else context
            written.extend(
                self.render_tree(tree.template, tree.target, merged, force=force, dry_run=dry_run)
            )
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


def module_context(
    name: str,
    *,
    layout: str = DEFAULT_LAYOUT,
    ui: str = "api",
    table: str | None = None,
    modules_dir: str = "modules",
    language: str = "en",
) -> dict[str, Any]:
    snake = to_snake(name)
    plural = pluralize(snake, language)
    return {
        "module": snake,
        "Module": to_pascal(name),
        "module_title": snake.replace("_", " ").title(),
        "module_plural": plural.replace("_", " "),
        "table": table or plural,
        "layout": layout,
        "ui": ui,
        "modules_dir": modules_dir,
    }


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
    routes: Sequence[dict[str, str]] = (),
    language: str = "python",
    sample_module: str = "item",
    agent_docs: bool = False,
    grpc: bool = False,
) -> dict[str, Any]:
    snake = to_snake(name)
    enabled = resolve_plugins(kind, plugins)
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
        "workspace_name": workspace_name,
        "api_base_url": api_base_url or f"http://localhost:{port}",
        "routes": list(routes),
        "rag_store": rag_store,
        "queue_backend": queue_backend,
        "datastores": datastores,
        "enabled_plugins": enabled,
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


def view_context(name: str, *, frontend: str = "vue") -> dict[str, Any]:
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
        spa: list[Tree] = [Tree(f"frontend_{frontend}", target)]
        if agent_docs:
            # The design skill lives with the thing it describes, which for a
            # frontend project is the frontend project.
            spa.append(Tree("agent_design", target))
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


def view_trees(frontend: str, target: Path) -> list[Tree]:
    if frontend not in FRONTENDS:
        raise ValueError(
            f"Frontend {frontend!r} is not supported for view generation. "
            f"Choose from: {', '.join(FRONTENDS)}."
        )
    return [Tree(f"view_{frontend}", target)]
