"""What modules say to each other without importing each other.

``placement`` sees the edges a module *imports*. Two kinds of coupling have no
import to see:

* **Events.** ``Event(type="comprobante.registrado")`` in one module and
  ``@subscribe("comprobante.registrado")`` in another. That is the decoupled
  way to react, and it still has to be a declared, reviewable fact: a
  subscription to an event nobody publishes never runs, and a typo in the
  name fails silently. ``[modules.<name>] publishes`` in ``contracts.toml`` is
  the declaration; subscriptions are read from the code.
* **Task names.** ``Job(task="alerta.revisar_presupuesto")`` queued from
  ``comprobante`` runs code ``alerta`` owns -- a call into another module,
  spelt as a string so nothing notices. A task belongs to the module whose
  ``@task`` declares it, and queuing one from elsewhere is a dependency like
  an import of its facade: declared, or reported.

Everything here is read from string literals in the source. A type or task
name built at run time cannot be checked and is not guessed at: a checker that
is right nine times in ten gets muted after the second false positive.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from jfastframework.contracts._scan import (
    call_name,
    import_aliases,
    python_files,
    resolve,
    waived,
)

__all__ = [
    "ORPHAN_RULE",
    "UNDECLARED_EVENT_RULE",
    "UNUSED_RULE",
    "Site",
    "Wiring",
    "facade_functions",
    "graph",
    "scan",
    "scan_tree",
    "toml_line",
]

ORPHAN_RULE = "orphan-subscription"
UNDECLARED_EVENT_RULE = "undeclared-event"
UNUSED_RULE = "unused-dependency"

_MODULE_PATH = re.compile(r"^modules/([^/]+)/")

_EVENT_CLASSES = frozenset(
    {"jfastframework.events.Event", "jfastframework.plugins.builtin.events.Event"}
)


@dataclass(frozen=True)
class Site:
    """One literal in the source: a task, an event type, a job's task name."""

    module: str
    name: str
    file: str
    line: int
    waived: bool = False
    #: The function a decorator is on, for tasks and subscriptions.
    handler: str = ""

    def describe(self) -> dict[str, object]:
        found: dict[str, object] = {"name": self.name, "file": self.file, "line": self.line}
        if self.handler:
            found["handler"] = self.handler
        return found


@dataclass
class Wiring:
    tasks: list[Site] = field(default_factory=list)
    subscriptions: list[Site] = field(default_factory=list)
    publications: list[Site] = field(default_factory=list)
    job_refs: list[Site] = field(default_factory=list)
    #: Every folder under modules/, so a task name can be traced to its owner
    #: even when the handler is registered outside the module.
    modules: set[str] = field(default_factory=set)

    def task_owners(self) -> dict[str, str]:
        """``task name -> module`` for every task a module owns.

        Declared with ``@task`` inside a module first. A name nobody declares
        there -- a handler registered in a root ``worker.py``, the pre-0.1.0a11
        shape -- still belongs to the module its ``<module>.`` prefix names:
        that is the convention every generated task follows, and without it
        the coupling the rule exists for stays invisible in exactly the
        projects that most need it.
        """
        owners: dict[str, str] = {}
        for site in self.tasks:
            owners.setdefault(site.name, site.module)
        for ref in self.job_refs:
            prefix = ref.name.partition(".")[0]
            if ref.name not in owners and "." in ref.name and prefix in self.modules:
                owners[ref.name] = prefix
        return owners

    def of(self, module: str) -> dict[str, list[str]]:
        """One module's side of the graph, names only, sorted."""

        def names(sites: Iterable[Site]) -> list[str]:
            return sorted({s.name for s in sites if s.module == module})

        return {
            "tasks": names(self.tasks),
            "subscribes": names(self.subscriptions),
            "publishes_in_code": names(self.publications),
        }


def _module_of(relative: str) -> str | None:
    match = _MODULE_PATH.match(relative)
    return match.group(1) if match else None


def _is_test(relative: str) -> bool:
    """A module's own tests build events and jobs to test with; they publish nothing."""
    parts = relative.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name == "conftest.py"


def _literal(node: ast.Call, keyword: str | None) -> str | None:
    """The first positional argument, or ``keyword=``, when it is a string literal."""
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    for item in node.keywords:
        if item.arg == keyword and isinstance(item.value, ast.Constant):
            value = item.value.value
            return value if isinstance(value, str) else None
    return None


def _origin(node: ast.Call, aliases: dict[str, str]) -> tuple[str | None, str | None]:
    """``(resolved origin, written name)`` of a call target."""
    written = call_name(node)
    if written is None:
        return None, None
    return resolve(written, aliases), written


def scan_tree(tree: ast.Module, relative: str, lines: list[str], wiring: Wiring) -> None:
    """Add one file's tasks, subscriptions, publications and job references."""
    module = _module_of(relative)
    if module is None or _is_test(relative):
        return
    aliases = import_aliases(tree)

    def site(name: str, line: int, handler: str = "") -> Site:
        return Site(module, name, relative, line, waived(lines, line) is not None, handler)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                origin, written = _origin(decorator, aliases)
                if written is None:
                    continue
                tail = written.rsplit(".", 1)[-1]
                is_framework = origin is not None and origin.startswith("jfastframework.")
                if tail == "subscribe" and is_framework:
                    name = _literal(decorator, None)
                    if name:
                        wiring.subscriptions.append(site(name, decorator.lineno, node.name))
                elif tail == "task" and (is_framework or (origin is None and "." in written)):
                    # `@task(...)` from jfastframework.tasks, or the registry
                    # spelling `@tasks.task(...)`: either way this module owns it.
                    name = _literal(decorator, "name")
                    if name:
                        wiring.tasks.append(site(name, decorator.lineno, node.name))
        elif isinstance(node, ast.Call):
            origin, _ = _origin(node, aliases)
            if origin is None:
                continue
            if origin in _EVENT_CLASSES:
                name = _literal(node, "type")
                if name:
                    wiring.publications.append(site(name, node.lineno))
            elif origin.startswith("jfastframework.") and origin.endswith(".Job"):
                name = _literal(node, "task")
                if name:
                    wiring.job_refs.append(site(name, node.lineno))


def scan(root: Path) -> Wiring:
    """Every module's wiring under ``root/modules``. Never imports project code."""
    wiring = Wiring()
    modules = root / "modules"
    if not modules.is_dir():
        return wiring
    wiring.modules = {
        p.name for p in modules.iterdir() if p.is_dir() and not p.name.startswith("_")
    }
    for path in python_files(modules):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue
        scan_tree(tree, relative, source.splitlines(), wiring)
    return wiring


def graph(module_publishes: dict[str, list[str]], wiring: Wiring) -> dict[str, object]:
    """Who publishes what, who listens, and which tasks each module owns.

    Publishers come from the contract (declared) and the code (built), so a
    reader sees both and any disagreement between them; subscribers and tasks
    come from the code alone, because that is where they are declared.
    """
    types = {e for events in module_publishes.values() for e in events}
    types |= {s.name for s in wiring.subscriptions} | {p.name for p in wiring.publications}
    events: dict[str, object] = {}
    for event_type in sorted(types):
        events[event_type] = {
            "published_by": sorted(m for m, ev in module_publishes.items() if event_type in ev),
            "built_in": sorted({p.module for p in wiring.publications if p.name == event_type}),
            "subscribers": [
                {"module": s.module, **s.describe()}
                for s in wiring.subscriptions
                if s.name == event_type
            ],
        }
    tasks: dict[str, object] = {}
    for site in sorted(wiring.tasks, key=lambda s: s.name):
        tasks[site.name] = {
            "module": site.module,
            **site.describe(),
            "queued_by": sorted({r.module for r in wiring.job_refs if r.name == site.name}),
        }
    return {"events": events, "tasks": tasks}


def facade_functions(root: Path, module: str) -> list[str]:
    """The public functions of ``modules/<module>/public.py``.

    ``__all__`` when it is a literal list, otherwise every top-level function
    not starting with an underscore. What an agent may call, without opening
    the file.
    """
    path = root / "modules" / module / "public.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            return sorted(
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    )


def toml_line(lines: list[str], table: str, key: str, value: str | None = None) -> int:
    """The line of ``key`` (optionally the one naming ``"value"``) in ``[table]``.

    0 when it cannot be found. Text, not a TOML parse: the parser drops line
    numbers, and a finding that cannot point at a line is one nobody fixes.
    """
    header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    inside = False
    key_line = 0
    for number, text in enumerate(lines, start=1):
        stripped = text.strip()
        if stripped.startswith("["):
            if inside:
                break
            inside = bool(header.match(text))
            continue
        if not inside:
            continue
        if not key_line and re.match(rf"^\s*{re.escape(key)}\s*=", text):
            key_line = number
            if value is None or f'"{value}"' in text:
                return number
            continue
        if key_line and value is not None and f'"{value}"' in text:
            return number
    return key_line
