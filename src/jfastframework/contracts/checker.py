"""Verify code against its declared contract.

Static, AST-based, and deliberately **conservative**: it only reports what it
can prove from the syntax. A checker that cries wolf gets an ignore file within
a week, and then the contract is decoration again.

Three consequences of that choice, all intentional:

* Files that match no layer are not layer-checked. You opt a path *in* by
  giving it to a layer; nothing is guessed. The one thing that is not silent
  about it is a layer that ended up governing nothing while such files exist:
  see ``check_coverage``, which is what tells a pass from a no-op.
* Only static imports and direct calls are inspected. ``importlib`` and
  ``getattr`` chains are out of scope — a contract is a design guardrail, not
  a sandbox.
* Every violation can be waived inline with ``# contracts: allow <reason>``.
  The reason is required, so a waiver is a decision someone can review rather
  than a silent bypass.
"""

from __future__ import annotations

import ast
from pathlib import Path

from jfastframework.contracts._scan import (
    SKIP_DIRS,
    WAIVER,
    Violation,
    call_name,
    python_files,
    waived,
)
from jfastframework.contracts._scan import resolve_relative as _resolve_relative
from jfastframework.contracts.blocking import check_blocking
from jfastframework.contracts.model import Contract, Layer, match_path
from jfastframework.contracts.placement import check_placement, crosses_to_facade

__all__ = [
    "SKIP_DIRS",
    "WAIVER",
    "Violation",
    "check",
    "check_coverage",
    "layer_matches",
    "waivers",
]


def _module_to_paths(dotted: str) -> list[str]:
    """Candidate file paths a dotted module could live in."""
    stem = dotted.replace(".", "/")
    return [f"{stem}.py", f"{stem}/__init__.py"]


def _layer_of_module(contract: Contract, dotted: str) -> Layer | None:
    for candidate in _module_to_paths(dotted):
        layer = contract.layer_for(candidate)
        if layer is not None:
            return layer
    return None


def _top_package(name: str) -> str:
    return name.split(".", 1)[0]


def check_imports(contract: Contract, root: Path) -> list[Violation]:
    violations: list[Violation] = []

    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        layer = contract.layer_for(relative)

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            # A file that does not parse is ruff's problem, not the
            # contract's. Reporting it twice helps nobody.
            continue
        lines = source.splitlines()

        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = [(alias.name, node.lineno) for alias in node.names]
                dotted_targets = [(name, line) for name, line in names]
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_relative(node.module, node.level, path, root)
                dotted_targets = [(resolved, node.lineno)] if resolved else []
                imported = tuple(alias.name for alias in node.names)
            else:
                continue

            for dotted, line in dotted_targets:
                if dotted is None:
                    continue
                reason = waived(lines, line)

                # 1. Layer boundaries. Another module's public.py is not a
                # layer of this one: that edge belongs to `placement`.
                if layer is not None:
                    target_layer = (
                        None
                        if crosses_to_facade(relative, dotted, imported)
                        else _layer_of_module(contract, dotted)
                    )
                    if (
                        target_layer is not None
                        and target_layer.name != layer.name
                        and target_layer.name not in layer.may_import
                    ):
                        allowed = ", ".join(layer.may_import) or "nothing"
                        violation = Violation(
                            relative,
                            line,
                            "layer",
                            f"{layer.name!r} imports {target_layer.name!r} "
                            f"({dotted}); it may import: {allowed}",
                            layer.description,
                        )
                        if reason is None:
                            violations.append(violation)

                    # 2. Packages this layer must not touch at all.
                    package = _top_package(dotted)
                    if package in layer.forbid_packages and reason is None:
                        violations.append(
                            Violation(
                                relative,
                                line,
                                "layer-package",
                                f"{layer.name!r} must not import {package!r}",
                                layer.description,
                            )
                        )

                # 3. Explicit forbid_import rules.
                for rule in contract.forbid_imports:
                    if rule.in_paths and not any(match_path(relative, p) for p in rule.in_paths):
                        continue
                    if _top_package(dotted) in rule.packages and reason is None:
                        violations.append(
                            Violation(
                                relative,
                                line,
                                "forbid-import",
                                f"{dotted} is not allowed here",
                                rule.why,
                            )
                        )

    return violations


def check_calls(contract: Contract, root: Path) -> list[Violation]:
    if not contract.forbid_calls:
        return []

    violations: list[Violation] = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name is None:
                continue

            for rule in contract.forbid_calls:
                if any(match_path(relative, pattern) for pattern in rule.except_in):
                    continue
                if not _call_matches(name, rule.pattern):
                    continue
                if waived(lines, node.lineno) is None:
                    violations.append(
                        Violation(
                            relative,
                            node.lineno,
                            "forbid-call",
                            f"{name}() is not allowed here",
                            rule.why,
                        )
                    )
                break
    return violations


def _call_matches(name: str, pattern: str) -> bool:
    """Does this call site match a forbidden pattern?

    A dotted pattern also matches the bare name, because
    ``from os import getenv`` is the same mistake as ``os.getenv`` and the
    contract should not have to list both. A bare pattern matches only
    exactly, so forbidding ``print`` does not also flag ``report.print``.
    """
    if name == pattern:
        return True
    if "." not in pattern:
        return False
    return name.endswith("." + pattern) or name == pattern.rsplit(".", 1)[1]


def check_requirements(contract: Contract, root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for requirement in contract.requirements:
        for module_dir in sorted(root.glob(requirement.applies_to)):
            if not module_dir.is_dir() or module_dir.name.startswith((".", "_")):
                continue
            if not (module_dir / requirement.path).exists():
                relative = module_dir.relative_to(root).as_posix()
                violations.append(
                    Violation(
                        f"{relative}/",
                        0,
                        "missing",
                        f"{requirement.path} is required but absent",
                        requirement.why,
                    )
                )
    return violations


def check_contract(contract: Contract) -> list[Violation]:
    """Check the contract itself before checking the code against it.

    Two authoring mistakes produce confident, wrong results rather than
    errors, so they are caught here:

    * two layers claiming the same path — whichever wins the tie decides the
      rules, and the answer is silently arbitrary;
    * ``may_import`` naming a layer that does not exist — the permission has
      no effect and the import is rejected for a reason nobody can find.
    """
    violations: list[Violation] = []
    source = contract.source.name if contract.source else CONTRACTS_FILE_FALLBACK

    claims: dict[str, list[str]] = {}
    for layer in contract.layers.values():
        for pattern in layer.paths:
            claims.setdefault(pattern, []).append(layer.name)

    for pattern, owners in sorted(claims.items()):
        if len(owners) > 1:
            violations.append(
                Violation(
                    source,
                    0,
                    "contract",
                    f"path {pattern!r} is claimed by {' and '.join(sorted(owners))}; "
                    f"one of them must give it up",
                )
            )

    for layer in contract.layers.values():
        for target in layer.may_import:
            if target not in contract.layers:
                known = ", ".join(sorted(contract.layers)) or "none"
                violations.append(
                    Violation(
                        source,
                        0,
                        "contract",
                        f"layer {layer.name!r} may_import {target!r}, which is not a layer. "
                        f"Known layers: {known}",
                    )
                )
    return violations


CONTRACTS_FILE_FALLBACK = "contracts.toml"


def layer_matches(contract: Contract, root: Path) -> dict[str, int]:
    """How many files each layer actually governs, by layer name.

    Counted through ``layer_for`` rather than by raw globbing, because that is
    the question worth answering: a layer whose every match is taken by a more
    specific pattern applies its rules to nothing either, and no glob count
    would show it.
    """
    counts = dict.fromkeys(contract.layers, 0)
    for path in python_files(root):
        layer = contract.layer_for(path.relative_to(root).as_posix())
        if layer is not None:
            counts[layer.name] += 1
    return counts


def _governed_prefixes(contract: Contract) -> set[str]:
    """The top-level directories the contract claims, from its layer globs."""
    prefixes = set()
    for layer in contract.layers.values():
        for pattern in layer.paths:
            head = pattern.split("/", 1)[0]
            if head and not any(char in head for char in "*?["):
                prefixes.add(head)
    return prefixes


def check_coverage(contract: Contract, root: Path) -> list[Violation]:
    """Layers that govern no file, while files they should govern go unclaimed.

    The case it exists for: a service scaffolded with the layered contract
    holding only hexagonal modules. Every layer glob misses, so
    `forbid_packages` on the HTTP layer enforces nothing -- and a check that
    reports a pass there is worse than no check at all.

    Both halves decide whether this fails a build, and both are needed. A layer
    that matched nothing in a tree where every governed file *is* claimed is a
    layer for code not written yet; failing on that would make a service with
    one module unbuildable. A layer that matched nothing while governed files
    go unclaimed means the contract describes a tree other than this one.
    """
    if not contract.layers:
        return []
    counts = layer_matches(contract, root)
    empty = sorted(name for name, count in counts.items() if count == 0)
    if not empty:
        return []

    prefixes = _governed_prefixes(contract)
    governed = [
        relative
        for relative in (path.relative_to(root).as_posix() for path in python_files(root))
        if relative.split("/", 1)[0] in prefixes
    ]
    unclaimed = [relative for relative in governed if contract.layer_for(relative) is None]
    if not unclaimed:
        return []

    source = contract.source.name if contract.source else CONTRACTS_FILE_FALLBACK
    where = ", ".join(f"{prefix}/" for prefix in sorted(prefixes))
    # A package marker is unclaimed in every healthy project too, so it is the
    # one file that says nothing about which layout the tree is in.
    example = next((r for r in unclaimed if not r.endswith("__init__.py")), unclaimed[0])
    return [
        Violation(
            source,
            0,
            "layer-unmatched",
            f"layer {name!r} matched 0 of the {len(governed)} file(s) under {where} "
            f"(paths: {', '.join(contract.layers[name].paths) or 'none'}); "
            f"{len(unclaimed)} of them match no layer at all, {example} among them",
            "A layer that governs no file enforces nothing, and the check still passes.",
        )
        for name in empty
    ]


def check(contract: Contract, root: Path) -> list[Violation]:
    """Every check, in a stable order.

    The contract is validated first: if it contradicts itself, the findings
    below are answers to the wrong question.
    """
    own = check_contract(contract)
    if own:
        return own

    violations = [
        *check_imports(contract, root),
        *check_calls(contract, root),
        *check_requirements(contract, root),
        *check_blocking(contract, root),
        *check_placement(contract, root),
        *check_coverage(contract, root),
    ]
    return sorted(violations, key=lambda v: (v.path, v.line, v.rule))


def waivers(root: Path) -> list[Violation]:
    """Every inline waiver, so they can be reviewed rather than accumulate.

    A waiver is a decision. Decisions that nobody ever looks at again turn
    into the reason a contract stopped meaning anything.
    """
    found: list[Violation] = []
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if WAIVER in text:
                reason = text.split(WAIVER, 1)[1].strip(" #\t").strip()
                found.append(Violation(relative, number, "waiver", reason or "(no reason given)"))
    return found
