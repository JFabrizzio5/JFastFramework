"""How modules talk to each other, checked instead of agreed.

One sentence covers it: **queries through a facade, effects through events,
nothing through shared.**

* **A module's only importable surface is ``modules/<name>/public.py``.**
  Everything else inside a module is private. Two modules that reach into each
  other's services, repositories or entities are one module with a folder
  between them: neither can be extracted into a service, and a change to one
  breaks the other in a way no test covers. The facade is a handful of
  functions that take the caller's session and an explicit tenant, and return
  DTOs -- never an ORM entity, never a FastAPI type.
* **The dependency is declared.** ``[modules.<name>] depends_on`` in
  ``contracts.toml`` lists the modules whose facade this one may call, so the
  graph is a reviewed decision rather than whatever the imports add up to --
  and a cycle in it is reported.
* **No SQL against another module's tables.** A raw ``SELECT ... FROM
  <their table>`` is the same coupling as an import, with the difference that
  nothing sees it: rename a column and the other module fails at runtime.
  Ownership is read from ``__tablename__``.
* **``shared/`` is vocabulary.** Enums, types and pure functions two modules
  both speak. Behaviour does not go there: a repository in ``shared/`` is two
  modules sharing a table. The direction stays one-way -- modules import
  ``shared/``, ``shared/`` imports no module.

Reported separately from a layer violation because the fix is different. A
layer violation means the call is in the wrong place inside a module; these
mean the module boundary was crossed, and each message names the way across
that is allowed.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from jfastframework.contracts._scan import Violation, python_files, resolve_relative, waived
from jfastframework.contracts.model import Contract
from jfastframework.contracts.wiring import (
    ORPHAN_RULE,
    UNDECLARED_EVENT_RULE,
    UNUSED_RULE,
    Wiring,
    scan_tree,
    toml_line,
)

RULE = "cross-module"
SHARED_RULE = "shared-direction"
UNDECLARED_RULE = "undeclared-dependency"
CYCLE_RULE = "module-cycle"
LEAK_RULE = "public-leak"
SQL_RULE = "cross-module-sql"
UNKNOWN_RULE = "unknown-dependency"

#: Every rule this file reports. `[rules.placement] enabled` turns them all off.
RULES = (
    RULE,
    SHARED_RULE,
    UNDECLARED_RULE,
    CYCLE_RULE,
    LEAK_RULE,
    SQL_RULE,
    UNKNOWN_RULE,
    ORPHAN_RULE,
    UNDECLARED_EVENT_RULE,
    UNUSED_RULE,
)

#: The facade's file name. Fixed rather than configurable: every generator,
#: template and doc names it, and a knob that only the checker honoured would
#: make the two disagree.
PUBLIC_FILE = "public.py"
PUBLIC_NAME = "public"

#: Packages a facade may not touch. A facade that needs a request object can
#: only be called from HTTP, and a worker or another module is exactly who
#: calls it.
_HTTP_PACKAGES = frozenset({"fastapi", "starlette"})

# `modules/<name>/...` -- the layout every generated layout shares.
_MODULE_PATH = re.compile(r"^modules/([^/]+)/")
_SHARED_PATH = re.compile(r"^shared/")

# A table name after the SQL keyword that introduces one. Schema-qualified and
# quoted names are accepted and reduced to the bare table: `public."orders"`
# is still `orders`.
_SQL_TABLE = re.compile(
    r"\b(?:from|join|into|update|table)\s+((?:\"?[A-Za-z_][\w$]*\"?\.)?\"?[A-Za-z_][\w$]*\"?)",
    re.IGNORECASE,
)


def _module_of(relative: str) -> str | None:
    match = _MODULE_PATH.match(relative)
    return match.group(1) if match else None


def _imported_module(dotted: str) -> str | None:
    """The module name in ``modules.invoice.enums``, if that is what this is."""
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "modules":
        return parts[1]
    return None


def is_facade(dotted: str, names: Iterable[str] = ()) -> bool:
    """Is this import the other module's ``public.py`` and nothing else?

    Both spellings count: ``from modules.invoice.public import x`` and
    ``from modules.invoice import public``. The second only when *every* name
    imported is ``public`` -- ``from modules.invoice import public, service``
    is still a reach past the facade.
    """
    parts = dotted.split(".")
    if len(parts) == 3 and parts[0] == "modules" and parts[2] == PUBLIC_NAME:
        return True
    names = list(names)
    return len(parts) == 2 and parts[0] == "modules" and bool(names) and set(names) == {PUBLIC_NAME}


def is_vocabulary(dotted: str) -> bool:
    """Does the import name an enum or types module -- the one thing shared/ is for?"""
    tail = dotted.split(".")[2:]
    return any("enum" in part or part in ("types", "typing") for part in tail)


def _suggest_shared_target(dotted: str) -> str:
    """Where an imported enum or type should live instead."""
    parts = dotted.split(".")[2:]
    # An enum is the common case and has an obvious home. It is looked for in
    # every segment, so a hexagonal `domain.enums` lands in shared/enums.py
    # rather than in a shared/domain.py nobody asked for.
    if any("enum" in part for part in parts):
        return "shared/enums.py"
    if any(part in ("types", "typing") for part in parts):
        return "shared/types.py"
    tail = parts[0] if parts else "types"
    return f"shared/{tail}.py"


def facade_path(module: str) -> str:
    return f"modules/{module}/{PUBLIC_FILE}"


def crosses_to_facade(relative: str, dotted: str, names: Iterable[str] = ()) -> bool:
    """Is the file at *relative* calling a *different* module's facade?

    Such an import is governed here -- declared in ``depends_on``, acyclic --
    and not by the layer rules. Layers describe the inside of one module; the
    facade is the edge of another, and which of its own layers a module calls
    it from is that module's business.
    """
    here = _module_of(relative)
    other = _imported_module(dotted)
    return here is not None and other is not None and other != here and is_facade(dotted, names)


# ---------------------------------------------------------------------------
# One pass over the tree
# ---------------------------------------------------------------------------


@dataclass
class _Import:
    file: str
    line: int
    dotted: str
    names: tuple[str, ...]
    waived: bool


@dataclass
class _File:
    relative: str
    module: str | None
    in_shared: bool
    tree: ast.Module
    lines: list[str]
    imports: list[_Import] = field(default_factory=list)


def _imports(
    tree: ast.Module, path: Path, root: Path, relative: str, lines: list[str]
) -> Iterator[_Import]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            dotted = resolve_relative(node.module, node.level, path, root)
            if not dotted:
                continue
            yield _Import(
                relative,
                node.lineno,
                dotted,
                tuple(alias.name for alias in node.names),
                waived(lines, node.lineno) is not None,
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield _Import(
                    relative, node.lineno, alias.name, (), waived(lines, node.lineno) is not None
                )


def _tablename(node: ast.ClassDef) -> str | None:
    """The table a class maps to, if its body assigns ``__tablename__``.

    Both spellings: SQLAlchemy 2.0 style annotates it
    (``__tablename__: str = "users"``), which is an ``AnnAssign``.
    """
    for statement in node.body:
        targets: list[ast.expr]
        if isinstance(statement, ast.Assign):
            targets, value = list(statement.targets), statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in targets):
            return value.value
    return None


def _entities(tree: ast.Module) -> Iterator[tuple[ast.ClassDef, str]]:
    """Every ORM entity class in the file, with its table."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            table = _tablename(node)
            if table is not None:
                yield node, table


def _docstrings(tree: ast.Module) -> set[int]:
    """``id()`` of every docstring node. A docstring cannot run a query."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _string_literals(tree: ast.Module) -> Iterator[tuple[ast.expr, str]]:
    """Every string constant, with f-strings reduced to their constant parts.

    The interpolated pieces of an f-string are replaced by a placeholder, so
    ``f"SELECT {cols} FROM orders"`` is read as ``SELECT ? FROM orders``: the
    table is in the constant part, which is the part that can be read without
    running anything.
    """
    skip = _docstrings(tree)
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = []
            for value in node.values:
                inside_fstring.add(id(value))
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    text.append(value.value)
                else:
                    text.append(" ? ")
            yield node, "".join(text)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and id(node) not in inside_fstring
        ):
            yield node, node.value


def _sql_tables(text: str) -> list[str]:
    tables: list[str] = []
    for match in _SQL_TABLE.finditer(text):
        name = match.group(1).rsplit(".", 1)[-1].strip('"').lower()
        if name not in tables:
            tables.append(name)
    return tables


def _span_waived(lines: list[str], node: ast.expr) -> bool:
    """A waiver anywhere on the lines the string spans.

    A triple-quoted query runs over several lines, and the one that reads
    naturally for the comment is rarely the first.
    """
    end = getattr(node, "end_lineno", None) or node.lineno
    return any(waived(lines, line) is not None for line in range(node.lineno, end + 1))


def _scan(root: Path) -> list[_File]:
    files: list[_File] = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        module = _module_of(relative)
        in_shared = bool(_SHARED_PATH.match(relative))
        if module is None and not in_shared:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()
        scanned = _File(relative, module, in_shared, tree, lines)
        scanned.imports = list(_imports(tree, path, root, relative, lines))
        files.append(scanned)
    return files


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _cross_module(here: str, other: str, dotted: str, root: Path) -> tuple[str, str]:
    """``(message, why)`` for a reach past another module's facade."""
    if is_vocabulary(dotted):
        # The one case shared/ is for: a value two modules both speak.
        target = _suggest_shared_target(dotted)
        return (
            f"module {here!r} imports {dotted}, private to module {other!r}",
            f"an enum or type two modules both speak is vocabulary: move it to {target} "
            f"and import it from both",
        )
    facade = facade_path(other)
    if (root / facade).is_file():
        how = f"call a function in {facade} that returns DTOs (add one if it is missing)"
    else:
        how = (
            f"{facade} does not exist yet: create it and expose a function that takes the "
            f"session and tenant_id and returns DTOs"
        )
    return (
        f"module {here!r} imports {dotted}; import modules.{other}.public instead",
        f'only public.py is another module\'s API -- {how}, and add "{other}" to '
        f"depends_on under [modules.{here}] in contracts.toml",
    )


def _check_imports(
    contract: Contract, files: list[_File], root: Path, entities: dict[str, dict[str, str]]
) -> tuple[list[Violation], dict[str, dict[str, _Import]]]:
    """Import rules, and the facade edges the code actually has.

    The edges are returned rather than checked here because a cycle is a
    property of the whole graph, not of any one import.
    """
    violations: list[Violation] = []
    edges: dict[str, dict[str, _Import]] = {}

    for scanned in files:
        here = scanned.module
        is_public = here is not None and scanned.relative == facade_path(here)
        for imported in scanned.imports:
            other = _imported_module(imported.dotted)

            if is_public and here is not None and not imported.waived:
                violations.extend(_leaks(scanned, imported, here, entities))

            if other is None or imported.waived:
                continue

            if scanned.in_shared:
                violations.append(
                    Violation(
                        imported.file,
                        imported.line,
                        SHARED_RULE,
                        f"shared/ imports modules.{other}",
                        "the direction is one-way: modules use shared/, never the "
                        "reverse, or the graph becomes a circle",
                    )
                )
                continue
            if here is None or other == here:
                continue

            if not is_facade(imported.dotted, imported.names):
                message, why = _cross_module(here, other, imported.dotted, root)
                violations.append(Violation(imported.file, imported.line, RULE, message, why))
                continue

            edges.setdefault(here, {}).setdefault(other, imported)
            if other not in contract.module_deps.get(here, []):
                violations.append(
                    Violation(
                        imported.file,
                        imported.line,
                        UNDECLARED_RULE,
                        f"module {here!r} calls modules.{other}.public but does not declare "
                        f"{other!r} in depends_on",
                        f'add "{other}" to depends_on under [modules.{here}] in contracts.toml: '
                        f"the module graph is a reviewed decision, and the one place a cycle "
                        f"can be seen before it is built",
                    )
                )
    return violations, edges


def _leaks(
    scanned: _File, imported: _Import, here: str, entities: dict[str, dict[str, str]]
) -> list[Violation]:
    """What a facade must not hand across: an ORM entity or an HTTP type."""
    found: list[Violation] = []
    package = imported.dotted.split(".", 1)[0]
    if package in _HTTP_PACKAGES:
        found.append(
            Violation(
                imported.file,
                imported.line,
                LEAK_RULE,
                f"{facade_path(here)} imports {package}",
                "a facade is called by other modules and by workers, where there is no "
                "request: take plain arguments and return DTOs",
            )
        )
    own = f"modules.{here}"
    if imported.dotted == own or imported.dotted.startswith(own + "."):
        leaked = sorted(name for name in imported.names if name in entities.get(here, {}))
        if leaked:
            found.append(
                Violation(
                    imported.file,
                    imported.line,
                    LEAK_RULE,
                    f"{facade_path(here)} imports the ORM entity {', '.join(leaked)}",
                    "return a DTO (a dataclass or Pydantic model) built from it: an entity "
                    "carries its session and lazy relations across the boundary, and every "
                    "column becomes part of the API",
                )
            )
    return found


def _check_defined_entities(files: list[_File]) -> list[Violation]:
    """An entity declared inside public.py leaks by definition."""
    found: list[Violation] = []
    for scanned in files:
        here = scanned.module
        if here is None or scanned.relative != facade_path(here):
            continue
        for node, _ in _entities(scanned.tree):
            if waived(scanned.lines, node.lineno) is not None:
                continue
            found.append(
                Violation(
                    scanned.relative,
                    node.lineno,
                    LEAK_RULE,
                    f"{facade_path(here)} declares the ORM entity {node.name}",
                    "the table belongs in the module's storage layer; the facade returns DTOs",
                )
            )
    return found


def _check_sql(files: list[_File], owners: dict[str, str]) -> list[Violation]:
    found: list[Violation] = []
    for scanned in files:
        here = scanned.module
        if here is None:
            continue
        for node, text in _string_literals(scanned.tree):
            foreign = [
                (table, owners[table])
                for table in _sql_tables(text)
                if table in owners and owners[table] != here
            ]
            if not foreign or _span_waived(scanned.lines, node):
                continue
            named = ", ".join(f"{table!r} (module {owner!r})" for table, owner in foreign)
            modules = sorted({owner for _, owner in foreign})
            found.append(
                Violation(
                    scanned.relative,
                    node.lineno,
                    SQL_RULE,
                    f"module {here!r} queries {named} with raw SQL",
                    f"a query against another module's table is an import nobody can see: "
                    f"read it through {', '.join(facade_path(m) for m in modules)} instead",
                )
            )
    return found


def _cycles(graph: dict[str, list[str]]) -> list[tuple[str, ...]]:
    """Every cycle, each reported once, rotated to start at its smallest name."""
    found: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    colour: dict[str, int] = dict.fromkeys(graph, 0)

    def walk(node: str, path: list[str]) -> None:
        colour[node] = 1
        path.append(node)
        for neighbour in sorted(graph.get(node, [])):
            state = colour.get(neighbour, 0)
            if state == 1:
                cycle = path[path.index(neighbour) :]
                start = cycle.index(min(cycle))
                key = tuple(cycle[start:] + cycle[:start])
                if key not in seen:
                    seen.add(key)
                    found.append(key)
            elif state == 0 and neighbour in graph:
                walk(neighbour, path)
        path.pop()
        colour[node] = 2

    for name in sorted(graph):
        if colour[name] == 0:
            walk(name, [])
    return found


def _check_cycles(
    contract: Contract, edges: dict[str, dict[str, _Import]], source: str
) -> list[Violation]:
    graph: dict[str, set[str]] = {}
    for module, deps in contract.module_deps.items():
        graph.setdefault(module, set()).update(deps)
    for module, imported in edges.items():
        graph.setdefault(module, set()).update(imported)
    for reached in list(graph.values()):
        for target in reached:
            graph.setdefault(target, set())

    found: list[Violation] = []
    for cycle in _cycles({name: sorted(deps) for name, deps in graph.items()}):
        chain = " -> ".join([*cycle, cycle[0]])
        # Point at an import that closes the cycle when there is one, so the
        # report lands on a line someone can change; a cycle made only of
        # declarations is reported against contracts.toml.
        evidence = next(
            (
                edges[a][b]
                for a, b in zip(cycle, [*cycle[1:], cycle[0]], strict=True)
                if b in edges.get(a, {})
            ),
            None,
        )
        found.append(
            Violation(
                evidence.file if evidence else source,
                evidence.line if evidence else 0,
                CYCLE_RULE,
                f"module dependency cycle: {chain}",
                "modules in a cycle cannot be extracted or reasoned about apart. Break it: "
                "one side reads through the other's public.py, the other reacts to an event "
                "it publishes (outbox) instead of calling back",
            )
        )
    return found


def _entity_map(files: list[_File]) -> dict[str, dict[str, str]]:
    """``module -> {entity class name: table}``."""
    found: dict[str, dict[str, str]] = {}
    for scanned in files:
        if scanned.module is None:
            continue
        for node, table in _entities(scanned.tree):
            found.setdefault(scanned.module, {})[node.name] = table
    return found


def _check_declared(contract: Contract, root: Path, source: str) -> list[Violation]:
    """A depends_on entry, or a [modules.x] block, naming no module on disk.

    Almost always a typo -- `comprobantes` for `comprobante` -- and silent
    otherwise: the real dependency then reads as undeclared, or an old block
    outlives the module it described.
    """
    modules_dir = root / "modules"
    if not modules_dir.is_dir():
        return []
    present = {
        p.name for p in modules_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
    }
    found: list[Violation] = []
    for module, deps in sorted(contract.module_deps.items()):
        for name in [module, *deps]:
            if name not in present:
                where = (
                    f"[modules.{module}]" if name == module else f"[modules.{module}] depends_on"
                )
                found.append(
                    Violation(
                        source,
                        0,
                        UNKNOWN_RULE,
                        f"{where} names {name!r}, which is not a module under modules/",
                        "fix the name, or remove the entry if the module is gone",
                    )
                )
    return found


def _check_task_refs(
    contract: Contract, wiring: Wiring, edges: dict[str, dict[str, _Import]]
) -> list[Violation]:
    """A job queued by name for a task another module owns is a call into it.

    Added to ``edges`` so the cycle check sees it: the string spelling is how
    a cycle between two modules hides from every import-based check.
    """
    owners = wiring.task_owners()
    found: list[Violation] = []
    for ref in wiring.job_refs:
        owner = owners.get(ref.name)
        if owner is None or owner == ref.module or ref.waived:
            continue
        edges.setdefault(ref.module, {}).setdefault(
            owner, _Import(ref.file, ref.line, f"task {ref.name}", (), False)
        )
        if owner in contract.module_deps.get(ref.module, []):
            continue
        found.append(
            Violation(
                ref.file,
                ref.line,
                UNDECLARED_RULE,
                f"module {ref.module!r} queues task {ref.name!r}, which module {owner!r} "
                f"owns, but does not declare {owner!r} in depends_on",
                f"a task name is a call into the module that declares it, spelt so nothing "
                f"sees it. To react to something {ref.module!r} did, publish an event "
                f"(declare it under [modules.{ref.module}] publishes) and @subscribe to it in "
                f'{owner!r} -- no dependency either way. Otherwise add "{owner}" to '
                f"depends_on under [modules.{ref.module}]",
            )
        )
    return found


def _check_events(contract: Contract, wiring: Wiring, source: str) -> list[Violation]:
    declared = {event for events in contract.module_publishes.values() for event in events}
    found: list[Violation] = []
    for sub in wiring.subscriptions:
        if sub.waived or sub.name in declared:
            continue
        found.append(
            Violation(
                sub.file,
                sub.line,
                ORPHAN_RULE,
                f"module {sub.module!r} subscribes to {sub.name!r}, which no module declares "
                f"under publishes",
                f'declare it -- publishes = ["{sub.name}"] under the publishing module\'s '
                f"[modules.<name>] in {source} -- or fix the name: a subscription to an event "
                f"nobody publishes never runs, and says nothing. An event from another service "
                f"arrives over Kafka: use @on(topic) for it, not @subscribe",
            )
        )
    for pub in wiring.publications:
        if pub.waived or pub.name in contract.module_publishes.get(pub.module, []):
            continue
        found.append(
            Violation(
                pub.file,
                pub.line,
                UNDECLARED_EVENT_RULE,
                f"module {pub.module!r} publishes {pub.name!r} but does not declare it",
                f'add "{pub.name}" to publishes under [modules.{pub.module}] in {source}: an '
                f"event is part of a module's API, and its subscribers are only checked "
                f"against events someone declared",
            )
        )
    return found


def _check_unused(
    contract: Contract, files: list[_File], wiring: Wiring, root: Path
) -> list[Violation]:
    """A depends_on entry nothing uses: the graph has started to lie.

    Any import of the other module counts as use, waived or not, and so does
    queuing one of its tasks: the finding is about the declaration, not about
    whether the use is legal -- the other rules decide that.
    """
    used: dict[str, set[str]] = {}
    for scanned in files:
        if scanned.module is None:
            continue
        for imported in scanned.imports:
            other = _imported_module(imported.dotted)
            if other is not None and other != scanned.module:
                used.setdefault(scanned.module, set()).add(other)
    owners = wiring.task_owners()
    for ref in wiring.job_refs:
        owner = owners.get(ref.name)
        if owner is not None and owner != ref.module:
            used.setdefault(ref.module, set()).add(owner)

    modules_dir = root / "modules"
    present = (
        {p.name for p in modules_dir.iterdir() if p.is_dir()} if modules_dir.is_dir() else set()
    )
    lines: list[str] = []
    if contract.source is not None and contract.source.is_file():
        lines = contract.source.read_text(encoding="utf-8").splitlines()
    source = contract.source.name if contract.source else "contracts.toml"

    found: list[Violation] = []
    for module, deps in sorted(contract.module_deps.items()):
        if module not in present:
            continue  # unknown-dependency already says so
        for dep in deps:
            if dep == module or dep not in present or dep in used.get(module, set()):
                continue
            line = toml_line(lines, f"modules.{module}", "depends_on", dep)
            if waived(lines, line) is not None:
                continue
            found.append(
                Violation(
                    source,
                    line,
                    UNUSED_RULE,
                    f"[modules.{module}] depends_on lists {dep!r}, but module {module!r} never "
                    f"calls modules.{dep}.public or queues one of its tasks",
                    "remove it: a stale edge is how the declared graph stops matching the "
                    "code, and it can report a cycle that does not exist or hide one that does",
                )
            )
    return found


def check_placement(contract: Contract, root: Path) -> list[Violation]:
    """Every module-boundary rule, in one pass over the tree."""
    if not contract.enforce_placement:
        return []

    files = _scan(root)
    entities = _entity_map(files)
    owners: dict[str, str] = {}
    for module, found in sorted(entities.items()):
        for table in found.values():
            owners.setdefault(table.lower(), module)
    wiring = Wiring()
    modules_dir = root / "modules"
    if modules_dir.is_dir():
        wiring.modules = {
            p.name for p in modules_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
        }
    for scanned in files:
        scan_tree(scanned.tree, scanned.relative, scanned.lines, wiring)

    source = contract.source.name if contract.source else "contracts.toml"
    violations, edges = _check_imports(contract, files, root, entities)
    violations += _check_task_refs(contract, wiring, edges)
    violations += _check_events(contract, wiring, source)
    violations += _check_defined_entities(files)
    violations += _check_sql(files, owners)
    violations += _check_cycles(contract, edges, source)
    violations += _check_declared(contract, root, source)
    violations += _check_unused(contract, files, wiring, root)
    return violations


__all__ = [
    "CYCLE_RULE",
    "LEAK_RULE",
    "ORPHAN_RULE",
    "PUBLIC_FILE",
    "RULE",
    "RULES",
    "SHARED_RULE",
    "SQL_RULE",
    "UNDECLARED_EVENT_RULE",
    "UNDECLARED_RULE",
    "UNKNOWN_RULE",
    "UNUSED_RULE",
    "check_placement",
    "crosses_to_facade",
    "facade_path",
    "is_facade",
    "is_vocabulary",
]
