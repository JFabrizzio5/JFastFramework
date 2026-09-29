"""Contracts: the rules a project declares about itself.

`AGENTS.md` says what to do. A contract says what is *allowed*, and something
checks it. That difference is the whole point: a rule nobody verifies is a
suggestion, and an agent generating code at speed will drift past suggestions
without noticing.

A contract lives in ``contracts.toml`` at the root of a service. It is written
by the person who owns the service, not generated and forgotten:

    [project]
    name = "billing"
    owns = "Invoices and payments."
    does_not_own = "Customers. Ask the catalog service."

    [layers.domain]
    paths = ["modules/*/domain.py", "modules/*/{module}.py"]
    may_import = []

    [layers.http]
    paths = ["modules/*/router.py", "modules/*/http.py"]
    may_import = ["use_cases", "domain"]

    [[rules.forbid_call]]
    pattern = "os.getenv"
    except_in = ["settings.py"]
    why = "Configuration is typed. Add a field to a settings model."

    [modules.asesor]
    depends_on = ["comprobante"]   # may call modules/comprobante/public.py

Three consumers, one file:

* ``jfast contracts check`` — fails the build on a violation;
* ``jfast contracts show --json`` — what an agent reads before writing code;
* ``CONTRACTS.md`` — what a human reads in review.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACTS_FILE = "contracts.toml"


# ---------------------------------------------------------------------------
# Matching a path against a layer glob
# ---------------------------------------------------------------------------


def _translate(pattern: str) -> str:
    """A layer glob as a regular expression, with ``/`` as a real boundary.

    The one rule worth stating: ``*`` matches within a path segment and stops
    at the separator, ``**`` crosses it. Everything else follows from that.
    ``**/`` also matches *zero* segments, so ``modules/**/http.py`` covers
    ``modules/http.py`` -- the alternative makes every author write the
    pattern twice.
    """
    out: list[str] = []
    index = 0
    end = len(pattern)
    while index < end:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:[^/]+/)*")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            # An unterminated `[` is a literal bracket, as it is in fnmatch.
            if close < 0:
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : close]
            negated = body[:1] in ("!", "^")
            if negated:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("]", "\\]")
            # A negated class must not become a way back across the separator:
            # `[!_]` is "not an underscore", never "anything, / included".
            out.append(f"[^/{body}]" if negated else f"[{body}]")
            index = close + 1
            continue
        out.append(re.escape(char))
        index += 1
    return "".join(out)


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(_translate(pattern) + r"\Z")


def match_path(relative: str, pattern: str) -> bool:
    """Does *relative* -- a ``/``-separated path -- match this layer glob?

    Not :func:`fnmatch.fnmatch`, and the difference is the whole reason this
    exists. ``fnmatch`` translates ``*`` to ``.*``, which happily crosses a
    directory separator: ``modules/*/repository.py`` matches
    ``modules/invoice/infrastructure/repository.py``, so the layered contract's
    ``storage`` layer silently claims a hexagonal project's adapters and
    ``layer-unmatched`` -- the finding that exists to catch a contract
    governing nothing -- never fires, and the symptom shows up far from its
    cause.

    Matching is case-sensitive on every platform. ``fnmatch`` normalises case
    on Windows, which would let a contract pass on a laptop and fail in CI; a
    rule that answers differently by operating system is not a rule.
    """
    return _compiled(pattern).match(relative) is not None


@dataclass
class Layer:
    """One architectural layer and what it is allowed to reach.

    ``may_import`` lists *other layers*, not packages. Layers are the thing a
    reviewer argues about; package names are the thing they forget.
    """

    name: str
    paths: list[str] = field(default_factory=list)
    may_import: list[str] = field(default_factory=list)
    forbid_packages: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ForbiddenCall:
    """A call that must not appear, and why."""

    pattern: str
    why: str = ""
    # Globs, matched against the path relative to the project root.
    except_in: list[str] = field(default_factory=list)


@dataclass
class ForbiddenImport:
    """A package that must not be imported from certain paths."""

    packages: list[str]
    in_paths: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class Requirement:
    """A file or directory every module must have."""

    path: str
    why: str = ""
    applies_to: str = "modules/*"


@dataclass
class AsyncSafety:
    """Rules for the check that nothing stalls the event loop.

    ``allow_in`` replaces the built-in default rather than adding to it, so a
    project that wants its tests checked can say so. The default is listed in
    the generated ``contracts.toml``, where it is visible instead of implied.
    """

    enabled: bool = True
    # The naive-datetime half of this table. It rides here because both
    # rules are 'calls whose damage does not show up where they are
    # written', but it is not about async at all -- so it gets its own
    # switch. Sharing `enabled` would mean a project silencing one had to
    # silence the other, and the one it did not mean to silence is the one
    # that was working.
    naive_datetime: bool = True
    # Dotted call patterns this project knows to be blocking, mapped to the
    # replacement to suggest. ``mylib.fetch = "mylib.afetch"``.
    extra_blocking: dict[str, str] = field(default_factory=dict)
    allow_in: list[str] = field(default_factory=list)
    follow_local_helpers: bool = True


@dataclass
class Interface:
    """Something this service promises to others, or consumes from them.

    Written down so a change to it is a visible decision rather than a
    surprise for whoever depended on it.
    """

    name: str
    kind: str = "http"
    path: str = ""
    stability: str = "experimental"
    description: str = ""
    via_env: str = ""


@dataclass
class Contract:
    project: str
    owns: str = ""
    does_not_own: str = ""
    language: str = "python"
    layers: dict[str, Layer] = field(default_factory=dict)
    forbid_calls: list[ForbiddenCall] = field(default_factory=list)
    forbid_imports: list[ForbiddenImport] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    provides: list[Interface] = field(default_factory=list)
    consumes: list[Interface] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    async_safety: AsyncSafety = field(default_factory=AsyncSafety)
    # Modules talk through each other's public.py and nothing else, and
    # shared/ must not import a module. On by default: both are the kind of
    # coupling that is easy to add and expensive to remove once a second team
    # has done it too. One switch for every rule in `placement.py`.
    enforce_placement: bool = True
    # `[modules.<name>] depends_on`: whose public.py each module may call. A
    # module with no block depends on nothing, so a new edge in the graph is
    # always a line someone added to this file and a reviewer saw.
    module_deps: dict[str, list[str]] = field(default_factory=dict)
    source: Path | None = None

    # -- loading -------------------------------------------------------

    @staticmethod
    def find(start: Path | None = None) -> Path | None:
        """Nearest ``contracts.toml`` at or above ``start``."""
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            path = candidate / CONTRACTS_FILE
            if path.is_file():
                return path
        return None

    @classmethod
    def load(cls, path: Path) -> Contract:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        project = raw.get("project", {})

        layers = {
            name: Layer(
                name=name,
                paths=list(block.get("paths", [])),
                may_import=list(block.get("may_import", [])),
                forbid_packages=list(block.get("forbid_packages", [])),
                description=block.get("description", ""),
            )
            for name, block in raw.get("layers", {}).items()
        }

        rules = raw.get("rules", {})
        return cls(
            project=project.get("name", path.parent.name),
            owns=project.get("owns", ""),
            does_not_own=project.get("does_not_own", ""),
            language=project.get("language", "python"),
            layers=layers,
            forbid_calls=[
                ForbiddenCall(
                    pattern=entry["pattern"],
                    why=entry.get("why", ""),
                    except_in=list(entry.get("except_in", [])),
                )
                for entry in rules.get("forbid_call", [])
            ],
            forbid_imports=[
                ForbiddenImport(
                    packages=list(entry.get("packages", [])),
                    in_paths=list(entry.get("in_paths", [])),
                    why=entry.get("why", ""),
                )
                for entry in rules.get("forbid_import", [])
            ],
            requirements=[
                Requirement(
                    path=entry["path"],
                    why=entry.get("why", ""),
                    applies_to=entry.get("applies_to", "modules/*"),
                )
                for entry in rules.get("require", [])
            ],
            provides=[_interface(entry) for entry in raw.get("provides", [])],
            consumes=[_interface(entry) for entry in raw.get("consumes", [])],
            invariants=list(raw.get("invariants", {}).get("rules", [])),
            async_safety=_async_safety(rules.get("async_safety", {})),
            enforce_placement=bool(rules.get("placement", {}).get("enabled", True)),
            module_deps=_module_deps(raw.get("modules", {})),
            source=path,
        )

    @classmethod
    def load_or_none(cls, start: Path | None = None) -> Contract | None:
        path = cls.find(start)
        return cls.load(path) if path else None

    # -- queries -------------------------------------------------------

    def layer_for(self, relative: str) -> Layer | None:
        """Which layer a file belongs to: the most *specific* matching pattern.

        Specificity is the count of wildcards, not the length of the string.
        Length is the obvious heuristic and it is wrong: a catch-all like
        ``modules/*/[!_]*.py`` is longer than ``modules/*/http.py`` and would
        win, quietly classifying every HTTP router as domain code and then
        rejecting its imports for a reason that makes no sense.

        Ties break on the longer pattern, and then on the layer name, so the
        answer never depends on dictionary insertion order.

        Matched with :func:`match_path`, not ``fnmatch``: a layer glob is a
        statement about *where in the tree* a file sits, so ``*`` has to stop
        at ``/`` for ``modules/*/repository.py`` to mean what its author meant.
        """
        best: tuple[int, int, str, Layer] | None = None
        for layer in self.layers.values():
            for pattern in layer.paths:
                if not match_path(relative, pattern):
                    continue
                wildcards = sum(pattern.count(char) for char in "*?[")
                candidate = (-wildcards, len(pattern), layer.name, layer)
                if best is None or candidate[:3] > best[:3]:
                    best = candidate
        return best[3] if best else None

    def describe(self) -> dict[str, Any]:
        """Everything an agent needs, before it writes a line."""
        return {
            "project": self.project,
            "language": self.language,
            "owns": self.owns,
            "does_not_own": self.does_not_own,
            "layers": {
                name: {
                    "paths": layer.paths,
                    "may_import": layer.may_import,
                    "forbid_packages": layer.forbid_packages,
                    "description": layer.description,
                }
                for name, layer in self.layers.items()
            },
            "rules": {
                "forbid_call": [
                    {"pattern": c.pattern, "why": c.why, "except_in": c.except_in}
                    for c in self.forbid_calls
                ],
                "forbid_import": [
                    {"packages": i.packages, "in_paths": i.in_paths, "why": i.why}
                    for i in self.forbid_imports
                ],
                "require": [
                    {"path": r.path, "applies_to": r.applies_to, "why": r.why}
                    for r in self.requirements
                ],
                "placement": {
                    "enabled": self.enforce_placement,
                    # Where another module's API lives. Stated here so an agent
                    # reading only this payload knows the one legal import.
                    "facade": "modules/<name>/public.py",
                },
                "async_safety": {
                    "enabled": self.async_safety.enabled,
                    "naive_datetime": self.async_safety.naive_datetime,
                    "extra_blocking": self.async_safety.extra_blocking,
                    "allow_in": self.async_safety.allow_in,
                    "follow_local_helpers": self.async_safety.follow_local_helpers,
                },
            },
            "modules": {
                name: {"depends_on": list(deps)} for name, deps in sorted(self.module_deps.items())
            },
            "provides": [_interface_dict(i) for i in self.provides],
            "consumes": [_interface_dict(i) for i in self.consumes],
            "invariants": self.invariants,
        }


def _interface(entry: dict[str, Any]) -> Interface:
    return Interface(
        name=entry["name"],
        kind=entry.get("kind", "http"),
        path=entry.get("path", ""),
        stability=entry.get("stability", "experimental"),
        description=entry.get("description", ""),
        via_env=entry.get("via_env", ""),
    )


def _interface_dict(interface: Interface) -> dict[str, Any]:
    return {
        "name": interface.name,
        "kind": interface.kind,
        "path": interface.path,
        "stability": interface.stability,
        "description": interface.description,
        "via_env": interface.via_env,
    }


def _module_deps(table: Any) -> dict[str, list[str]]:
    """``[modules.<name>] depends_on``, tolerating a table that is not one.

    A block with no ``depends_on`` declares none, the same as no block at all.
    """
    if not isinstance(table, dict):
        return {}
    deps: dict[str, list[str]] = {}
    for name, block in table.items():
        if not isinstance(block, dict):
            continue
        listed = block.get("depends_on", [])
        deps[str(name)] = [str(item) for item in listed] if isinstance(listed, list) else []
    return deps


def append_module_block(path: Path, module: str) -> bool:
    """Add ``[modules.<module>] depends_on = []`` to *path* unless it is there.

    Returns whether the file changed. Appended as text rather than rewritten
    through a TOML serialiser: the standard library cannot write TOML, and a
    contract is a document whose comments are half its value -- round-tripping
    it would drop every one of them.
    """
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8")
    try:
        existing = tomllib.loads(content).get("modules", {})
    except tomllib.TOMLDecodeError:
        return False
    if isinstance(existing, dict) and module in existing:
        return False
    block = f"[modules.{module}]\ndepends_on = []\n"
    path.write_text(content.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    return True


def _async_safety(block: dict[str, Any]) -> AsyncSafety:
    return AsyncSafety(
        enabled=bool(block.get("enabled", True)),
        naive_datetime=bool(block.get("naive_datetime", True)),
        extra_blocking=dict(block.get("extra_blocking", {})),
        allow_in=list(block.get("allow_in", [])),
        follow_local_helpers=bool(block.get("follow_local_helpers", True)),
    )
