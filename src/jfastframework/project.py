"""A project as it exists on disk, read without importing it.

`jfast describe` answers "what is this service" by building the app: it
resolves the plugin graph, imports every plugin, and reads the live settings.
That answer is the true one, and it is unavailable in the two moments you most
want it -- when a dependency is not installed, and when the code does not
import.

It also answers a question nobody asked. `describe` knows the plugin graph and
the settings schema and says **nothing about modules**: generate `invoice` and
`order`, run `jfast describe --json`, and neither name appears anywhere in the
output. Without this module the CLI cannot tell you what is in your own
project, and every agent that touches one reaches for `grep`.

This module answers that from the filesystem alone: what modules exist, what
they import, whether they are wired into the app, and what is inconsistent
about the result. It never imports project code, so it works on a project that
is broken, half-migrated, or missing its dependencies entirely.

What it deliberately does not do is guess. Every finding below is decidable
from the source text. A checker that is right nine times in ten gets muted
after the second false positive, and then the true findings are lost with it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jfastframework.contracts.checker import check_coverage
from jfastframework.contracts.model import CONTRACTS_FILE, Contract
from jfastframework.contracts.placement import is_facade
from jfastframework.contracts.wiring import facade_functions
from jfastframework.contracts.wiring import scan as scan_wiring

__all__ = [
    "SEVERITY_ORDER",
    "Finding",
    "Module",
    "Project",
    "analyze",
    "load",
    "module_edges",
]

SCHEMA_VERSION = "1"

#: Ordered worst-first. `analyze` sorts by this, and the CLI's exit code is
#: decided by the worst one present.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "site",
    }
)

#: Files that legitimately sit at the root of a service. Anything else there is
#: code that belongs to no module, which is where a codebase starts to sprawl.
ROOT_FILES = frozenset(
    {
        "main.py",
        "conftest.py",
        "settings.py",
        "config.py",
        "wsgi.py",
        "asgi.py",
        "manage.py",
        "worker.py",
        "tasks.py",
    }
)


def _python_files(directory: Path) -> list[Path]:
    """Every `.py` under *directory*, skipping the noise directories.

    The parts below *directory*, not the absolute path: a project checked out
    at `~/build/billing` is still a project, and testing the absolute parts
    skipped every file in it.
    """
    found: list[Path] = []
    for path in sorted(directory.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(directory).parts):
            continue
        found.append(path)
    return found


def _parse(path: Path) -> ast.Module | None:
    """The AST, or `None` when the file does not parse.

    A syntax error is somebody else's report to make -- ruff's, or the
    interpreter's. Refusing to describe the other twenty files because one of
    them is mid-edit would make this command useless exactly when it is needed.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None


def _dotted(node: ast.ImportFrom, path: Path, root: Path) -> str | None:
    """The absolute dotted name a `from ... import` resolves to.

    Relative imports are resolved against the file's own position, so
    `from ..order import x` inside `modules/invoice/service.py` becomes
    `modules.order` and is recognised as a cross-module import rather than
    silently ignored.
    """
    if not node.level:
        return node.module
    try:
        parts = list(path.relative_to(root).parts[:-1])
    except ValueError:
        return None
    up = node.level - 1
    if up:
        if up > len(parts):
            return None
        parts = parts[: len(parts) - up]
    if node.module:
        parts.append(node.module)
    return ".".join(parts) or None


def _top_level(name: str) -> str:
    return name.split(".", 1)[0]


def _tablenames(tree: ast.Module) -> set[str]:
    """Every `__tablename__` in the file, in both spellings.

    SQLAlchemy 2.0 style annotates it -- `__tablename__: str = "users"` -- which
    is an `AnnAssign` and not an `Assign`. Reading only the plain form would
    make a modern model invisible here, and `module-no-migration` would stay
    quiet about a table no revision creates.

    `cli/migrations.py` and `upgrades.py` each carry their own copy of this
    walk. Three parsers of one construct is the real defect; consolidating them
    is worth doing once nobody is mid-edit in those files.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value  # `__tablename__: str` with no value is legal
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "__tablename__":
                found.add(value.value)
    return found


def _local_names(root: Path) -> frozenset[str]:
    """Top-level importable names that belong to this project, not to PyPI."""
    names: set[str] = set()
    for entry in root.iterdir():
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if entry.is_dir():
            names.add(entry.name)
        elif entry.suffix == ".py":
            names.add(entry.stem)
    return frozenset(names)


def _route_prefix(tree: ast.Module) -> str | None:
    """The `prefix=` of the first `APIRouter(...)` in the file."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "APIRouter":
            continue
        for keyword in node.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                value = keyword.value.value
                if isinstance(value, str):
                    return value
    return None


@dataclass(frozen=True)
class Module:
    """One module directory, described from its source."""

    name: str
    path: str
    layout: str | None
    ui: str | None
    files: tuple[str, ...]
    imports: tuple[str, ...]
    """Other modules in this project that this one imports."""
    external: tuple[str, ...]
    """Third-party top-level packages it imports."""
    route_prefixes: tuple[str, ...]
    registered: bool
    """Whether `main.py` imports it. An unregistered module serves nothing."""
    has_tests: bool
    has_readme: bool
    tables: tuple[str, ...]
    """Table names declared by its models."""
    private_imports: tuple[str, ...] = ()
    """The subset of `imports` reached past their `public.py` facade.

    An import of another module's `public.py` is how modules are meant to talk;
    anything else of theirs is private, and only these are findings.
    """
    facade: tuple[str, ...] = ()
    """The functions its `public.py` offers other modules."""
    tasks: tuple[str, ...] = ()
    """Task names declared with `@task` inside it -- the tasks it owns."""
    subscribes: tuple[str, ...] = ()
    """Event types it reacts to with `@subscribe`."""
    publishes: tuple[str, ...] = ()
    """Event types its `[modules.<name>]` block declares under `publishes`."""
    depends_on: tuple[str, ...] = ()
    """What its `[modules.<name>]` block declares under `depends_on`."""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "layout": self.layout,
            "ui": self.ui,
            "files": list(self.files),
            "imports": list(self.imports),
            "external": list(self.external),
            "route_prefixes": list(self.route_prefixes),
            "registered": self.registered,
            "has_tests": self.has_tests,
            "has_readme": self.has_readme,
            "tables": list(self.tables),
            "private_imports": list(self.private_imports),
            "facade": list(self.facade),
            "tasks": list(self.tasks),
            "subscribes": list(self.subscribes),
            "publishes": list(self.publishes),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class Project:
    """Everything the filesystem says about a service."""

    root: Path
    name: str
    version: str
    env: str
    plugins: tuple[str, ...]
    disabled: tuple[str, ...]
    modules: tuple[Module, ...]
    shared_files: tuple[str, ...]
    shared_module_imports: tuple[tuple[str, str], ...]
    """`(file, module)` for every module that `shared/` imports. Should be empty."""
    loose_files: tuple[str, ...]
    has_contract: bool
    migrations: int
    migrated_tables: frozenset[str]
    """Tables named by a `create_table(...)` in some revision."""
    frontend: str | None

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(module.name for module in self.modules)

    def module(self, name: str) -> Module | None:
        for module in self.modules:
            if module.name == name:
                return module
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {
                "name": self.name,
                "version": self.version,
                "env": self.env,
                "root": str(self.root),
            },
            "plugins": {"enabled": list(self.plugins), "disabled": list(self.disabled)},
            "modules": [module.describe() for module in self.modules],
            "shared": {
                "files": list(self.shared_files),
                "imports_modules": [
                    {"file": file, "module": module} for file, module in self.shared_module_imports
                ],
            },
            "loose_files": list(self.loose_files),
            "contract": self.has_contract,
            "migrations": self.migrations,
            "migrated_tables": sorted(self.migrated_tables),
            "frontend": self.frontend,
        }


@dataclass(frozen=True)
class Finding:
    """One thing that is wrong, and what to do about it."""

    severity: str
    code: str
    message: str
    why: str
    path: str | None = None
    line: int | None = None

    def __str__(self) -> str:
        where = self.path or ""
        if where and self.line:
            where = f"{where}:{self.line}"
        head = f"{where}: " if where else ""
        return f"{head}{self.code}: {self.message}\n  ({self.why})"

    def describe(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "why": self.why,
            "path": self.path,
            "line": self.line,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _config(root: Path) -> dict[str, Any]:
    path = root / "jfast.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            loaded: dict[str, Any] = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded


def _registered_modules(root: Path) -> frozenset[str]:
    """Module names that `main.py` imports.

    Only the import is checked, not the `ROUTERS` list. A module whose import
    is present but whose router is not in the list is a narrower mistake that
    the type checker catches (an unused import); a module main.py has never
    heard of is invisible at runtime and nothing else reports it.
    """
    main = root / "main.py"
    tree = _parse(main) if main.is_file() else None
    if tree is None:
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(tree):
        dotted: str | None = None
        if isinstance(node, ast.ImportFrom):
            dotted = _dotted(node, main, root)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("modules."):
                    names.add(alias.name.split(".")[1])
            continue
        if dotted and dotted.startswith("modules."):
            names.add(dotted.split(".")[1])
    return frozenset(names)


def _scan_module(
    directory: Path,
    root: Path,
    *,
    local: frozenset[str],
    module_names: frozenset[str],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """`(files, module imports, external packages, route prefixes, tables, private imports)`."""
    files: list[str] = []
    imported: set[str] = set()
    private: set[str] = set()
    external: set[str] = set()
    prefixes: list[str] = []
    tables: set[str] = set()
    own = directory.name

    for path in _python_files(directory):
        files.append(path.relative_to(root).as_posix())
        tree = _parse(path)
        if tree is None:
            continue
        tables |= _tablenames(tree)
        prefix = _route_prefix(tree)
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
        for node in ast.walk(tree):
            dotted: str | None = None
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                dotted = _dotted(node, path, root)
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = _top_level(alias.name)
                    if top not in local and top not in sys.stdlib_module_names:
                        external.add(top)
                continue
            if not dotted:
                continue
            if dotted.startswith("modules."):
                other = dotted.split(".")[1]
                if other != own and other in module_names:
                    imported.add(other)
                    if not is_facade(dotted, names):
                        private.add(other)
                continue
            top = _top_level(dotted)
            if top not in local and top not in sys.stdlib_module_names:
                external.add(top)

    return (
        tuple(files),
        tuple(sorted(imported)),
        tuple(sorted(external)),
        tuple(prefixes),
        tuple(sorted(tables)),
        tuple(sorted(private)),
    )


def _declared_modules(root: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """``(depends_on, publishes)`` per module from contracts.toml, or empty.

    A contract that does not parse is `contracts check`'s error to report; here
    it only means nothing is declared.
    """
    source = root / CONTRACTS_FILE
    if not source.is_file():
        return {}, {}
    try:
        contract = Contract.load(source)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        return {}, {}
    return contract.module_deps, contract.module_publishes


def load(root: Path) -> Project:
    """Read *root* into a `Project`. Never imports project code."""
    root = root.resolve()
    config = _config(root)
    app = config.get("app", {}) if isinstance(config.get("app"), dict) else {}
    plugins_table = config.get("plugins", {}) if isinstance(config.get("plugins"), dict) else {}
    module_config = config.get("modules", {}) if isinstance(config.get("modules"), dict) else {}

    local = _local_names(root)
    modules_dir = root / "modules"
    directories = (
        sorted(
            entry
            for entry in modules_dir.iterdir()
            if entry.is_dir() and entry.name not in SKIP_DIRS and not entry.name.startswith(".")
        )
        if modules_dir.is_dir()
        else []
    )
    module_names = frozenset(entry.name for entry in directories)
    registered = _registered_modules(root)
    declared_deps, declared_publishes = _declared_modules(root)
    wiring = scan_wiring(root)

    modules: list[Module] = []
    for directory in directories:
        files, imports, external, prefixes, tables, private = _scan_module(
            directory, root, local=local, module_names=module_names
        )
        recorded = module_config.get(directory.name, {})
        recorded = recorded if isinstance(recorded, dict) else {}
        modules.append(
            Module(
                name=directory.name,
                path=directory.relative_to(root).as_posix(),
                layout=recorded.get("layout"),
                ui=recorded.get("ui"),
                files=files,
                imports=imports,
                external=external,
                route_prefixes=prefixes,
                registered=directory.name in registered,
                has_tests=(directory / "tests").is_dir()
                or any("test" in Path(name).name for name in files),
                has_readme=(directory / "README.md").is_file(),
                tables=tables,
                private_imports=private,
                facade=tuple(facade_functions(root, directory.name)),
                tasks=tuple(wiring.of(directory.name)["tasks"]),
                subscribes=tuple(wiring.of(directory.name)["subscribes"]),
                publishes=tuple(declared_publishes.get(directory.name, [])),
                depends_on=tuple(declared_deps.get(directory.name, [])),
            )
        )

    shared_dir = root / "shared"
    shared_files: list[str] = []
    shared_imports: list[tuple[str, str]] = []
    if shared_dir.is_dir():
        for path in _python_files(shared_dir):
            relative = path.relative_to(root).as_posix()
            shared_files.append(relative)
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                dotted = _dotted(node, path, root)
                if dotted and dotted.startswith("modules."):
                    shared_imports.append((relative, dotted.split(".")[1]))

    loose = [
        entry.name
        for entry in sorted(root.glob("*.py"))
        if entry.name not in ROOT_FILES and not entry.name.startswith("test_")
    ]

    versions = root / "migrations" / "versions"
    revisions = sorted(versions.glob("*.py")) if versions.is_dir() else []
    migrated: set[str] = set()
    for revision in revisions:
        tree = _parse(revision)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called not in ("create_table", "drop_table"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                first = node.args[0].value
                if isinstance(first, str):
                    migrated.add(first)

    frontend: str | None = None
    for candidate in ("frontend", "web", "ui"):
        if (root / candidate / "package.json").is_file():
            frontend = candidate
            break

    return Project(
        root=root,
        name=str(app.get("name", root.name)),
        version=str(app.get("version", "0.0.0")),
        env=str(app.get("env", "local")),
        plugins=tuple(plugins_table.get("enabled", []) or []),
        disabled=tuple(plugins_table.get("disabled", []) or []),
        modules=tuple(modules),
        shared_files=tuple(shared_files),
        shared_module_imports=tuple(shared_imports),
        loose_files=tuple(loose),
        has_contract=(root / CONTRACTS_FILE).is_file(),
        migrations=len(revisions),
        migrated_tables=frozenset(migrated),
        frontend=frontend,
    )


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


def module_edges(project: Project) -> list[tuple[str, str]]:
    """`(importer, imported)` for every module-to-module import."""
    return [(module.name, other) for module in project.modules for other in module.imports]


def _cycles(project: Project) -> list[tuple[str, ...]]:
    """Every cycle between modules, each reported once.

    The graph is the one `contracts check` judges: what the modules import
    *plus* what contracts.toml declares under depends_on. Two commands that
    disagree about whether a project has a cycle is one command too many.

    Depth-first with an explicit stack rather than recursion: a project with a
    thousand modules is unlikely, a recursion limit blowing up inside a
    diagnostic command is embarrassing either way.
    """
    graph = {
        module.name: sorted(set(module.imports) | set(module.depends_on))
        for module in project.modules
    }
    found: list[tuple[str, ...]] = []
    seen: set[frozenset[str]] = set()
    colour: dict[str, int] = dict.fromkeys(graph, 0)

    def walk(node: str, path: list[str]) -> None:
        colour[node] = 1
        path.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in colour:
                continue
            if colour[neighbour] == 1:
                cycle = tuple(path[path.index(neighbour) :])
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
            elif colour[neighbour] == 0:
                walk(neighbour, path)
        path.pop()
        colour[node] = 2

    for name in graph:
        if colour[name] == 0:
            walk(name, [])
    return found


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _ungoverned_contract(project: Project) -> list[Finding]:
    """A contract whose layers match no file, as a finding about the shape.

    `check_coverage` is called rather than reimplemented, and that is the whole
    point of the function. Two commands answering one question from two copies
    of the reasoning is how they end up disagreeing: a project cannot be
    simultaneously `no findings` and `3 violations` when both numbers come from
    here.

    A contract that does not parse is deliberately not reported. It is already
    `contracts check`'s error and `jfast next`'s step, and a third voice on it
    would be the duplication this function exists to end.
    """
    source = project.root / CONTRACTS_FILE
    if not source.is_file():
        return []
    try:
        contract = Contract.load(source)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        return []

    return [
        Finding(
            severity="high",
            code="contract-governs-nothing",
            message=violation.message,
            why=(
                "The layer's rules -- its may_import, its forbid_packages -- apply to no file, "
                "so `jfast contracts check` passes while enforcing nothing, and every layer "
                "boundary this project believes it has is unguarded. Point the layer's paths "
                "at the layout the modules actually use, or regenerate the contract for that "
                "layout: `jfast inspect` names each module's layout."
            ),
            path=CONTRACTS_FILE,
        )
        for violation in check_coverage(contract, project.root)
    ]


def analyze(project: Project, *, known_plugins: frozenset[str] | None = None) -> list[Finding]:
    """Static findings about *project*, worst first.

    Every check here is decidable from the source, and nothing guesses at
    intent.

    One of them reads `contracts.toml`, and the line that is *not* crossed is
    worth stating. `analyze` does not run `contracts check`. That command
    reports what happens inside a file -- which layer an import crossed, which
    call a function made -- and re-emitting its findings here would give one
    report two owners, two severities and two remedies, which is how two
    commands drift into contradicting each other. What `analyze` does ask is
    whether the contract's layers match any file at all, and that is a claim
    about how files are arranged rather than about what is in them: this
    command's own question, answered without parsing a single one of them.

    It is asked by calling `check_coverage`, so the answer is `contracts
    check`'s rather than a second opinion on it. The alternative -- staying
    quiet, on the grounds that anything touching the contract belongs to
    `contracts check` -- would let `analyze` print `no findings` on a project
    whose contract governs nothing, while `jfast next` says the check failed.
    """
    findings: list[Finding] = []

    for cycle in _cycles(project):
        chain = " -> ".join([*cycle, cycle[0]])
        findings.append(
            Finding(
                severity="critical",
                code="module-cycle",
                message=f"module dependency cycle (imports and depends_on): {chain}",
                why=(
                    "Modules in a cycle are one module with folders between them: neither can "
                    "be extracted into a service, and a change to one breaks the other in a "
                    "way no test covers. Keep the query in one direction, through the other "
                    "module's public.py, and turn the other direction into an event."
                ),
            )
        )

    findings.extend(_ungoverned_contract(project))

    for module in project.modules:
        if not module.registered and module.route_prefixes:
            findings.append(
                Finding(
                    severity="high",
                    code="module-unregistered",
                    message=f"module '{module.name}' declares routes but main.py never imports it",
                    why=(
                        "The endpoints do not exist at runtime. Nothing fails: the tests in the "
                        "module pass, the server starts, and the route 404s. Add it to main.py "
                        "between the [jfast:imports] and [jfast:routers] markers."
                    ),
                    path=f"{module.path}/",
                )
            )

    by_prefix: dict[str, list[str]] = {}
    for module in project.modules:
        for prefix in module.route_prefixes:
            by_prefix.setdefault(prefix, []).append(module.name)
    for prefix, owners in sorted(by_prefix.items()):
        if len(owners) > 1:
            findings.append(
                Finding(
                    severity="high",
                    code="route-conflict",
                    message=f"prefix '{prefix}' is claimed by {', '.join(sorted(owners))}",
                    why=(
                        "Whichever router registers first wins the overlapping paths and the "
                        "other's are unreachable. FastAPI does not warn. Give each router its "
                        "own prefix."
                    ),
                )
            )

    for file, module_name in project.shared_module_imports:
        findings.append(
            Finding(
                severity="high",
                code="shared-imports-module",
                message=f"shared/ imports module '{module_name}'",
                why=(
                    "The direction is one way: modules import shared/, shared/ imports no "
                    "module. Without that, shared/ becomes where everything ends up and the "
                    "dependency graph is a circle."
                ),
                path=file,
            )
        )

    # Only once there ARE revisions. A scaffold with no migrations at all has
    # not reached that step yet -- the generator's own next-steps panel says
    # so -- and reporting it would greet every new project with a finding.
    if project.migrations:
        for module in project.modules:
            missing = [t for t in module.tables if t not in project.migrated_tables]
            if missing:
                findings.append(
                    Finding(
                        severity="medium",
                        code="module-no-migration",
                        message=(
                            f"module '{module.name}' declares "
                            f"{', '.join(missing)} and no revision creates it"
                        ),
                        why=(
                            "The table is never created. The service starts and the first query "
                            "fails on a relation that does not exist. Run "
                            "`jfast exec -- alembic revision --autogenerate`."
                        ),
                        path=f"{module.path}/",
                    )
                )

    for module in project.modules:
        for other in module.private_imports:
            findings.append(
                Finding(
                    severity="medium",
                    code="cross-module-import",
                    message=f"module '{module.name}' imports module '{other}' past its public.py",
                    why=(
                        f"Only modules/{other}/public.py is another module's API: read through a "
                        "function there that returns DTOs, or move an enum both speak to "
                        "shared/. `jfast contracts check` reports the exact line."
                    ),
                    path=f"{module.path}/",
                )
            )

    if known_plugins is not None:
        for name in project.plugins:
            if name not in known_plugins:
                findings.append(
                    Finding(
                        severity="high",
                        code="plugin-unknown",
                        message=f"plugin '{name}' is enabled but not installed",
                        why=(
                            "The app will refuse to start. Either install the extra that "
                            "provides it (`jfast add --list`) or drop the name from "
                            "[plugins].enabled in jfast.toml."
                        ),
                        path="jfast.toml",
                    )
                )

    for name in project.loose_files:
        findings.append(
            Finding(
                severity="low",
                code="code-outside-module",
                message=f"'{name}' belongs to no module",
                why=(
                    "Code at the root has no owner and no boundary. It is the first file of "
                    "the utils/ directory that eventually imports everything."
                ),
                path=name,
            )
        )

    findings.sort(key=lambda f: (SEVERITY_ORDER.index(f.severity), f.code, f.path or ""))
    return findings


def worst(findings: list[Finding]) -> str | None:
    """The highest severity present, or `None` for a clean project."""
    for severity in SEVERITY_ORDER:
        if any(finding.severity == severity for finding in findings):
            return severity
    return None
