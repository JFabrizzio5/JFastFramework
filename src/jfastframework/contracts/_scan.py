"""Shared source-scanning primitives for the contract checks.

Extracted so ``checker`` and ``blocking`` agree on what a violation is, which
files are worth reading and how a waiver is written. Two checkers that disagree
about any of those produce reports nobody can reconcile.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WAIVER = "contracts: allow"

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "migrations",
    "dist",
    "build",
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str
    why: str = ""

    def __str__(self) -> str:
        tail = f"  ({self.why})" if self.why else ""
        return f"{self.path}:{self.line}: {self.rule}: {self.message}{tail}"


def python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
    ]


def waived(source_lines: list[str], line: int) -> str | None:
    """Return the waiver reason on this line, if any."""
    if not (1 <= line <= len(source_lines)):
        return None
    text = source_lines[line - 1]
    if WAIVER in text:
        return text.split(WAIVER, 1)[1].strip(" #\t").strip() or "(no reason given)"
    return None


def resolve_relative(module: str | None, level: int, current: Path, root: Path) -> str | None:
    """Turn a relative import into a repo-relative module path.

    ``from .storage import X`` inside ``modules/order/http.py`` resolves to
    ``modules/order/storage``.
    """
    if level == 0:
        return module
    base = current.parent
    for _ in range(level - 1):
        base = base.parent
    try:
        prefix = base.relative_to(root).as_posix().replace("/", ".")
    except ValueError:
        return None
    return f"{prefix}.{module}" if module else prefix


def call_name(node: ast.Call) -> str | None:
    """Dotted name of a call target, for the forms worth checking."""
    target: ast.expr = node.func
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
        return ".".join(reversed(parts))
    return None


def call_target(node: ast.Call) -> str | None:
    """Dotted target of a call, following one constructor call.

    ``Path(p).read_text()`` resolves to ``Path.read_text``, so an alias map can
    turn it into ``pathlib.Path.read_text``. ``self._client.ping()`` resolves to
    ``self._client.ping``: the syntax cannot say what that instance is, and a
    caller of this function is expected to treat an unresolvable head as
    unknown rather than guess.
    """
    target: ast.expr = node.func
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Call):
        # Follow the constructor exactly once: Path(p).read_text() is worth
        # resolving, deeper chains are guesswork.
        target = target.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
        return ".".join(reversed(parts))
    return None


def import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map every name this module binds by import to where it came from.

    ``import numpy as np`` gives ``np -> numpy``; ``from time import sleep as
    nap`` gives ``nap -> time.sleep``. Relative imports are skipped: they name
    modules inside the project, and the checks that use this map are about
    third-party and standard-library calls.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            for alias in node.names:
                origin = f"{module}.{alias.name}" if module else alias.name
                aliases[alias.asname or alias.name] = origin
    return aliases


def resolve(dotted: str, aliases: Mapping[str, str]) -> str | None:
    """Rewrite a call target through the file's imports, or give up.

    Giving up is the important half. ``self._client.ping()`` has no import to
    resolve through, and a checker that guessed would flag every ``ping`` in
    the codebase.
    """
    head, _, rest = dotted.partition(".")
    origin = aliases.get(head)
    if origin is None:
        return None
    return f"{origin}.{rest}" if rest else origin
