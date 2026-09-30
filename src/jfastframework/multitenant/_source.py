"""Reading a service's source for the multitenant checks, without importing it.

Everything here is `ast` and text, for the reason every other report in this
framework gives: a checker that only works when the project already imports is
unavailable in the moment it is needed -- mid-migration, half-edited, with a
dependency missing.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jfastframework.contracts._scan import WAIVER
from jfastframework.project import SKIP_DIRS

__all__ = [
    "SourceFile",
    "TenantTable",
    "config_line",
    "enabled_plugins",
    "load_config",
    "plugin_table",
    "source_files",
    "tenant_tables",
    "waiver_for",
]

#: Directories a report never reads. `migrations` because a revision is history:
#: the SQL in it ran once, against the schema of its day, and flagging it would
#: ask for an edit to something that must never be edited.
SKIPPED = frozenset({*SKIP_DIRS, "migrations", "node_modules"})

TENANT_MIXIN = "TenantMixin"


@dataclass(frozen=True)
class SourceFile:
    """One parsed file: its path relative to the project, its lines, its tree."""

    path: str
    text: str
    tree: ast.Module

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self.text.splitlines())

    @property
    def is_test(self) -> bool:
        """Tests are read by nothing that reports a *switch* would break.

        A test passing ``tenant_id=None`` is exercising the single-tenant
        behaviour on purpose, and asking for it to be rewritten before the
        switch would be asking for the evidence to be deleted first.
        """
        parts = Path(self.path).parts
        name = parts[-1]
        return (
            "tests" in parts[:-1]
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        )


def source_files(root: Path, *, include_migrations: bool = False) -> list[SourceFile]:
    """Every parseable `.py` under *root*, skipping the noise directories.

    A file that does not parse is skipped rather than reported: a syntax error
    is the interpreter's report to make, and refusing to read the other twenty
    files because one is mid-edit would make the check useless when it matters.
    """
    skipped = SKIPPED - {"migrations"} if include_migrations else SKIPPED
    found: list[SourceFile] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in skipped for part in relative.parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        found.append(SourceFile(relative.as_posix(), text, tree))
    return found


def waiver_for(lines: tuple[str, ...], *candidates: int) -> str | None:
    """The `# contracts: allow <reason>` on any of these lines, if one is there.

    The same comment `jfast contracts check` honours, so a project learns one
    way to say "this is deliberate". Accepted on the line of the finding and,
    for a finding that spans lines (a decorated route, a call split over
    several), on any line the caller passes -- plus a comment standing on its
    own on the line directly above the first of them, because a waiver that
    has to share a line with a decorator is a waiver nobody can format.
    """
    wanted = sorted({line for line in candidates if line >= 1})
    if not wanted:
        return None
    above = wanted[0] - 1
    if 1 <= above <= len(lines) and lines[above - 1].lstrip().startswith("#"):
        wanted.insert(0, above)
    for line in wanted:
        if line > len(lines):
            continue
        text = lines[line - 1]
        if WAIVER in text:
            return text.split(WAIVER, 1)[1].strip(" #\t").strip() or "(no reason given)"
    return None


# ---------------------------------------------------------------------------
# jfast.toml
# ---------------------------------------------------------------------------


def load_config(root: Path) -> dict[str, Any]:
    """`jfast.toml` as data; empty when it is missing or does not parse.

    Empty rather than an exception: `jfast check`'s `config` check owns the
    report that the file is broken, and a second voice on it would be noise.
    """
    path = root / "jfast.toml"
    try:
        loaded: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded


def enabled_plugins(config: dict[str, Any]) -> tuple[str, ...]:
    plugins = config.get("plugins", {})
    enabled = plugins.get("enabled", []) if isinstance(plugins, dict) else []
    disabled = set(plugins.get("disabled", []) or []) if isinstance(plugins, dict) else set()
    return tuple(name for name in enabled or [] if name not in disabled)


def plugin_table(config: dict[str, Any], name: str) -> dict[str, Any]:
    table = config.get("plugin", {})
    section = table.get(name, {}) if isinstance(table, dict) else {}
    return section if isinstance(section, dict) else {}


_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*(#.*)?$")


def config_line(root: Path, section: str, key: str | None = None) -> int | None:
    """The line of ``key`` in ``[section]`` of jfast.toml, or of the header.

    A finding about a setting points at the setting, so an editor jumps to the
    line that has to change. Falls back to the section header, and to nothing
    when the section is not written at all -- the default is then the value,
    and there is no line to point at.
    """
    try:
        lines = (root / "jfast.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    header: int | None = None
    inside = False
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=") if key else None
    for number, text in enumerate(lines, start=1):
        match = _HEADER.match(text)
        if match:
            inside = match.group(1) == section
            if inside:
                header = number
            continue
        if inside and key_pattern is not None and key_pattern.match(text):
            return number
    return header


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantTable:
    """A table whose rows carry a tenant, and the class that declares it."""

    table: str
    model: str
    path: str
    line: int


def _base_names(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _body_assignments(node: ast.ClassDef) -> Iterator[tuple[str, ast.expr | None]]:
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield target.id, statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            yield statement.target.id, statement.value


def _declares_tenant_column(node: ast.ClassDef) -> bool:
    return any(name == "tenant_id" for name, _ in _body_assignments(node))


def _tablename(node: ast.ClassDef) -> str | None:
    for name, value in _body_assignments(node):
        if (
            name == "__tablename__"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def tenant_tables(files: list[SourceFile]) -> list[TenantTable]:
    """Every table whose model carries a ``tenant_id``, by name, sorted.

    ``TenantMixin`` anywhere in the base closure, or a ``tenant_id`` column
    declared by hand -- in the class itself or in a local base. Bases are
    resolved by name across the whole project, so a model that reaches the
    mixin through ``shared/`` is found; two classes of one name in two files
    are indistinguishable here, which costs a table listed that should not be
    rather than one left out, and a table left out of a switch is the leak.
    """
    classes: dict[str, list[tuple[SourceFile, ast.ClassDef]]] = {}
    for source in files:
        if source.is_test:
            continue
        for node in ast.walk(source.tree):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((source, node))

    def carries_tenant(node: ast.ClassDef) -> bool:
        seen: set[str] = set()
        queue = [node]
        while queue:
            current = queue.pop()
            if _declares_tenant_column(current):
                return True
            for base in _base_names(current):
                if base == TENANT_MIXIN:
                    return True
                if base in seen:
                    continue
                seen.add(base)
                queue.extend(candidate for _, candidate in classes.get(base, []))
        return False

    found: dict[str, TenantTable] = {}
    for entries in classes.values():
        for source, node in entries:
            table = _tablename(node)
            if table is None or table in found or not carries_tenant(node):
                continue
            found[table] = TenantTable(table, node.name, source.path, node.lineno)
    return [found[name] for name in sorted(found)]
