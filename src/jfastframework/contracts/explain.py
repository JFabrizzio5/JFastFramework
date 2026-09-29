"""From a rule name back to the line that declares it, and the reason for it.

Everything else in this package runs in one direction: read ``contracts.toml``,
walk the code, report what broke. Every :class:`~jfastframework.contracts.
_scan.Violation` already carries a ``rule``, a ``message`` and a ``why``.

The direction nothing supported is the one an agent needs after a violation:
*which line declares this, and what was the author afraid of when they wrote
it?* Without that, the cheapest way to make ``contracts check`` pass is to
delete the import, copy the code into the second module, or take the rule out
of ``contracts.toml`` -- three fixes that satisfy the checker and damage the
design. ``docs/agents.md`` says exactly that, and this module is the answer.

Two things it does that the checker cannot:

* **It reads the file as text, not as parsed values.** ``Contract.load``
  produces the values and drops the line numbers and the comments -- which are
  precisely what makes an answer verifiable. The generated contracts carry
  their rationale in a comment above each declaration, so the reasoning quoted
  here is the one the project's author wrote, per layout, not prose invented at
  report time.
* **It answers about code that does not exist yet.** ``check`` needs a
  violation. ``explain`` answers "may this layer import that one" before the
  import is written, which is the cheaper moment.

The logic lives in the package rather than in ``cli/explain.py`` for the reason
``cli/insight.py`` gives: these are pure functions from a contract to a value,
so they are testable without a Typer runner, and an agent embedding the
framework can call them directly.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jfastframework import project as project_scan
from jfastframework.contracts._scan import WAIVER, python_files
from jfastframework.contracts._scan import resolve_relative as _resolve_relative
from jfastframework.contracts.blocking import NAIVE_RULE
from jfastframework.contracts.checker import (
    _layer_of_module,
    check,
    layer_matches,
)
from jfastframework.contracts.model import Contract, match_path
from jfastframework.contracts.placement import (
    CYCLE_RULE,
    LEAK_RULE,
    SHARED_RULE,
    SQL_RULE,
    UNDECLARED_RULE,
    UNKNOWN_RULE,
    _suggest_shared_target,
    crosses_to_facade,
    facade_path,
    is_facade,
    is_vocabulary,
)
from jfastframework.contracts.placement import (
    RULE as CROSS_MODULE,
)

__all__ = [
    "RULES",
    "WAIVER_COST",
    "ArchitectureDiff",
    "ContractSource",
    "Declaration",
    "EdgeDelta",
    "Explanation",
    "RuleDoc",
    "diff",
    "explain",
]

# The two halves of what waiving costs. Kept together and emitted with every
# answer: an agent that finds the inline form first and the silent one never
# will reach for whichever is cheaper, and editing the contract is cheaper.
WAIVER_COST: dict[str, str] = {
    "inline": f"# {WAIVER} <reason>",
    "scope": "one line, and only the rule that fired on it. The reason is required.",
    "listed_by": "jfast contracts waivers lists every one, so it stays a reviewable decision",
    "editing_the_contract": (
        "deleting the rule from contracts.toml removes it for every file and for everyone, "
        "silently, and nothing reports the next violation"
    ),
}

_SEPARATOR = re.compile(r"^#\s*-{4,}\s*$")
_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\]\[\s]+)\s*\]\]?")
_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*=")

# A section divider carries a label ("Rules", "Interfaces"). A label is a
# heading, not a rationale, and quoting it as one is worse than quoting
# nothing.
_MIN_RATIONALE = 24


@dataclass(frozen=True)
class Declaration:
    """One line of ``contracts.toml``, with the comment written above it."""

    file: str
    line: int
    text: str
    table: str
    comment: str = ""

    @property
    def where(self) -> str:
        return f"{Path(self.file).name}:{self.line}"

    def describe(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "text": self.text,
            "table": self.table,
            "comment": self.comment,
        }


@dataclass(frozen=True)
class _Block:
    table: str
    line: int
    keys: dict[str, int]


def _blocks(lines: Sequence[str]) -> list[_Block]:
    found: list[_Block] = [_Block("", 0, {})]
    for number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if text.startswith("#") or not text:
            continue
        header = _HEADER.match(text)
        if header:
            found.append(_Block(header.group(2), number, {}))
            continue
        key = _KEY.match(text)
        if key:
            found[-1].keys.setdefault(key.group(1), number)
    return found


def _rationale(lines: Sequence[str], line: int, *, allow_gap: bool) -> str:
    """The comment block above *line*, as the author wrote it.

    ``allow_gap`` lets a table header reach the banner separated from it by a
    blank line; a key is only ever explained by the comment touching it.

    A divider ends the block above it. Without that rule the banner for one
    section swallows the commented-out example that closes the previous one,
    and the answer quotes a `[[provides]]` sample as the reason for a
    placement rule.
    """
    collected: list[str] = []
    number = line - 1
    if allow_gap:
        while number >= 1 and not lines[number - 1].strip():
            number -= 1

    while number >= 1:
        text = lines[number - 1].strip()
        if not text.startswith("#"):
            break
        if _SEPARATOR.match(text):
            if collected:
                break
            number -= 1
            continue
        collected.append(text.lstrip("#").strip())
        number -= 1

    paragraphs: list[str] = []
    current: list[str] = []
    for text in reversed(collected):
        if text:
            current.append(text)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    joined = "\n".join(paragraphs)
    return joined if len(joined) >= _MIN_RATIONALE else ""


class ContractSource:
    """``contracts.toml`` as text, so a rule can be traced back to its line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[str] = path.read_text(encoding="utf-8").splitlines()
        self._parsed = _blocks(self.lines)

    @classmethod
    def for_contract(cls, contract: Contract) -> ContractSource | None:
        if contract.source is None or not contract.source.is_file():
            return None
        return cls(contract.source)

    def _at(self, line: int, table: str, *, allow_gap: bool) -> Declaration:
        return Declaration(
            file=str(self.path),
            line=line,
            text=self.lines[line - 1].strip(),
            table=table,
            comment=_rationale(self.lines, line, allow_gap=allow_gap),
        )

    def table(self, name: str) -> Declaration | None:
        for block in self._parsed:
            if block.table == name and block.line:
                return self._at(block.line, name, allow_gap=True)
        return None

    def key(self, table: str, key: str) -> Declaration | None:
        for block in self._parsed:
            if block.table == table and key in block.keys:
                return self._at(block.keys[key], table, allow_gap=False)
        return None

    def entries(self, table: str) -> list[Declaration]:
        """Every ``[[array.of.tables]]`` header with this name."""
        return [self._at(b.line, table, allow_gap=True) for b in self._parsed if b.table == table]

    def entry_for(self, table: str, key: str, value: str) -> Declaration | None:
        """The array entry whose *key* holds *value*, pointed at that key."""
        for block in self._parsed:
            if block.table != table or key not in block.keys:
                continue
            line = block.keys[key]
            if f'"{value}"' in self.lines[line - 1]:
                return self._at(line, table, allow_gap=False)
        return None


@dataclass(frozen=True)
class RuleDoc:
    """What a rule name means, and where it is written down."""

    name: str
    summary: str
    declared_in: str
    instead: tuple[str, ...] = ()


RULES: dict[str, RuleDoc] = {
    "layer": RuleDoc(
        "layer",
        "a file in one layer imported another layer that its may_import does not list",
        "[layers.<layer>] may_import",
        (
            "move the code into a layer that may reach the target",
            "or move the thing being imported into a layer this one may already reach",
        ),
    ),
    "layer-package": RuleDoc(
        "layer-package",
        "a layer imported a third-party package it declares it must not touch",
        "[layers.<layer>] forbid_packages",
        ("do the work in a layer that is allowed the package, and pass the result across",),
    ),
    "forbid-import": RuleDoc(
        "forbid-import",
        "a package listed in a forbid_import rule was imported from a path the rule covers",
        "[[rules.forbid_import]]",
        ("use the replacement the rule names, or move the import to a path outside in_paths",),
    ),
    "forbid-call": RuleDoc(
        "forbid-call",
        "a call the contract forbids appears outside the paths it is excepted in",
        "[[rules.forbid_call]]",
        ("use the replacement the rule's `why` names",),
    ),
    "missing": RuleDoc(
        "missing",
        "a module is missing a file or directory every module has to have",
        "[[rules.require]]",
        ("create it -- the rule names the path and applies to every module",),
    ),
    CROSS_MODULE: RuleDoc(
        CROSS_MODULE,
        "one module imported something of another module other than its public.py",
        "[rules.placement]",
        (
            "import modules.<other>.public instead; if the function you need is not there, "
            "add it -- it takes the session and tenant_id and returns DTOs",
            "if what you imported is an enum or a type both modules speak, move it to "
            "shared/ and import it from both",
        ),
    ),
    UNDECLARED_RULE: RuleDoc(
        UNDECLARED_RULE,
        "a module called another module's public.py without listing it in depends_on",
        "[modules.<name>] depends_on",
        (
            "add the module to depends_on under [modules.<name>] in contracts.toml -- a "
            "reviewed, visible edge in the module graph",
            "or, if you only need to react to something the other module did, subscribe to "
            "its event through the outbox and depend on nothing",
        ),
    ),
    CYCLE_RULE: RuleDoc(
        CYCLE_RULE,
        "the module graph (declared depends_on plus actual public.py imports) has a cycle",
        "[modules.<name>] depends_on",
        (
            "keep the query in one direction and turn the other into an event: the "
            "downstream module subscribes instead of being called back",
            "or move the vocabulary both sides need (an enum, a type) into shared/",
        ),
    ),
    LEAK_RULE: RuleDoc(
        LEAK_RULE,
        "public.py hands across an ORM entity or imports fastapi/starlette",
        "[rules.placement]",
        (
            "return a DTO -- a dataclass or Pydantic model -- built from the entity",
            "take plain arguments (session, tenant_id, ids), never a Request, so a worker "
            "or another module can call it",
        ),
    ),
    UNKNOWN_RULE: RuleDoc(
        UNKNOWN_RULE,
        "a [modules.x] block or depends_on entry names no module under modules/",
        "[modules.<module>] depends_on",
        (
            "fix the spelling to the module's directory name",
            "or delete the entry if the module is gone",
        ),
    ),
    SQL_RULE: RuleDoc(
        SQL_RULE,
        "a string in one module holds SQL against a table another module owns",
        "[rules.placement]",
        (
            "read the data through the owning module's public.py, which returns DTOs",
            "if the query is a report that must join across modules, give it its own "
            "module that depends on both facades, or waive the line with the reason",
        ),
    ),
    SHARED_RULE: RuleDoc(
        SHARED_RULE,
        "shared/ imported a module, which reverses the one-way direction",
        "[rules.placement]",
        (
            "move the thing shared/ needs into shared/ as well, or pass it in as an "
            "argument from the module that has it",
        ),
    ),
    "async-blocking": RuleDoc(
        "async-blocking",
        "a call that stops the event loop appears inside an async def",
        "[rules.async_safety]",
        (
            "await the asynchronous client instead, or hand the blocking work to "
            "asyncio.to_thread(...)",
        ),
    ),
    NAIVE_RULE: RuleDoc(
        NAIVE_RULE,
        "a naive datetime was built -- one with no time zone, whose meaning "
        "depends on where the process runs",
        "[rules.async_safety] naive_datetime",
        ("use jfastframework.time.now() for the current instant, or pass tz= to the constructor",),
    ),
    "contract": RuleDoc(
        "contract",
        "contracts.toml contradicts itself, so no finding about the code would be trustworthy",
        "[layers.*]",
        ("give the clashing path to exactly one layer, or fix the may_import that names nothing",),
    ),
    "waiver": RuleDoc(
        "waiver",
        "not a violation: an inline exception someone took, listed so it stays reviewable",
        "the source line itself",
        ("re-read it and delete the waiver once the reason has expired",),
    ),
}


@dataclass(frozen=True)
class Explanation:
    """One answer: what the rule is, where it is declared, and what to do."""

    question: str
    verdict: str
    rule: str = ""
    summary: str = ""
    declarations: tuple[Declaration, ...] = ()
    why: tuple[str, ...] = ()
    instead: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "verdict": self.verdict,
            "rule": self.rule,
            "summary": self.summary,
            "declarations": [d.describe() for d in self.declarations],
            "why": list(self.why),
            "instead": list(self.instead),
            "unknown": list(self.unknown),
            "waiver": dict(WAIVER_COST),
            "context": self.context,
        }


def _facts(contract: Contract, root: Path) -> dict[str, Any]:
    """What the answer is being decided against, carried in every payload.

    An agent that has to open ``contracts.toml`` to interpret the answer is an
    agent that will edit it.
    """
    return {
        "project": contract.project,
        "contract": str(contract.source) if contract.source else "",
        "layers": {
            name: {
                "paths": layer.paths,
                "may_import": layer.may_import,
                "forbid_packages": layer.forbid_packages,
                "description": layer.description,
            }
            for name, layer in contract.layers.items()
        },
        "modules": _module_names(root),
        "module_deps": {name: list(deps) for name, deps in sorted(contract.module_deps.items())},
        "placement_enforced": contract.enforce_placement,
    }


def _module_names(root: Path) -> list[str]:
    directory = root / "modules"
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    )


def _kind_of(contract: Contract, root: Path, name: str) -> str:
    if name in contract.layers:
        return "layer"
    if name in _module_names(root):
        return "module"
    packages = {p for layer in contract.layers.values() for p in layer.forbid_packages}
    packages |= {p for rule in contract.forbid_imports for p in rule.packages}
    if name in packages:
        return "package"
    if "/" in name or name.endswith(".py"):
        return "path"
    return "unknown"


def _rule_declarations(
    contract: Contract, source: ContractSource | None, rule: str
) -> tuple[tuple[Declaration, ...], tuple[str, ...], tuple[str, ...]]:
    """``(declarations, why, unknown)`` for a rule, across the whole contract."""
    if source is None:
        return ((), (), ("contracts.toml could not be read, so no line can be named",))

    found: list[Declaration] = []
    why: list[str] = []
    unknown: list[str] = []

    if rule == "layer":
        found = [
            d
            for name in contract.layers
            if (d := source.key(f"layers.{name}", "may_import")) is not None
        ]
        why = [layer.description for layer in contract.layers.values() if layer.description]
    elif rule == "layer-package":
        found = [
            d
            for name in contract.layers
            if (d := source.key(f"layers.{name}", "forbid_packages")) is not None
        ]
    elif rule == "forbid-call":
        found = [
            source.entry_for("rules.forbid_call", "pattern", entry.pattern)
            or Declaration(str(source.path), 0, entry.pattern, "rules.forbid_call")
            for entry in contract.forbid_calls
        ]
        why = [entry.why for entry in contract.forbid_calls if entry.why]
    elif rule == "forbid-import":
        found = source.entries("rules.forbid_import")
        why = [entry.why for entry in contract.forbid_imports if entry.why]
    elif rule == "missing":
        found = source.entries("rules.require")
        why = [entry.why for entry in contract.requirements if entry.why]
    elif rule in (CROSS_MODULE, SHARED_RULE, LEAK_RULE, SQL_RULE):
        declaration = source.table("rules.placement")
        if declaration is None:
            unknown.append(
                "[rules.placement] is not declared in contracts.toml; it defaults to enabled"
            )
        else:
            found = [declaration]
    elif rule in (UNDECLARED_RULE, CYCLE_RULE, UNKNOWN_RULE):
        found = [
            d
            for name in sorted(contract.module_deps)
            if (d := source.key(f"modules.{name}", "depends_on")) is not None
        ]
        if not found:
            # Not "cannot fire": a module with no block depends on nothing, so
            # the first undeclared call is exactly what this rule reports.
            unknown.append(
                "no [modules.<name>] block declares depends_on yet, so every module depends "
                "on nothing and any call to another module's public.py is undeclared"
            )
    elif rule in ("async-blocking", NAIVE_RULE):
        # One switch, two rules. `naive-datetime` is not about async at
        # all; it rides [rules.async_safety] because both are 'calls whose
        # damage does not show up where they are written'.
        declaration = source.table("rules.async_safety")
        if declaration is None:
            unknown.append(
                "[rules.async_safety] is not declared in contracts.toml; it defaults to enabled"
            )
        else:
            found = [declaration]
    elif rule == "contract":
        unknown.append(
            "nothing declares this rule: it fires when contracts.toml contradicts itself"
        )
    elif rule == "waiver":
        unknown.append("nothing declares this rule: a waiver is written on the source line itself")

    why += [d.comment for d in found if d.comment]
    if not found and not unknown:
        unknown.append(
            f"contracts.toml declares no {RULES[rule].declared_in}, "
            f"so {rule!r} cannot fire in this project"
        )
    return tuple(found), tuple(dict.fromkeys(w for w in why if w)), tuple(unknown)


def _explain_rule(
    contract: Contract, root: Path, source: ContractSource | None, rule: str
) -> Explanation:
    doc = RULES[rule]
    found, why, unknown = _rule_declarations(contract, source, rule)
    return Explanation(
        question=f"what is the {rule!r} rule, and where does it come from?",
        verdict="informational",
        rule=rule,
        summary=doc.summary,
        declarations=found,
        why=why,
        instead=doc.instead,
        unknown=unknown,
        context=_facts(contract, root),
    )


def _layers_that_may_import(contract: Contract, target: str) -> list[str]:
    return sorted(name for name, layer in contract.layers.items() if target in layer.may_import)


def _layer_pair(
    contract: Contract, root: Path, source: ContractSource | None, left: str, right: str
) -> Explanation:
    layer = contract.layers[left]
    permitted = right in layer.may_import
    question = f"may {left!r} import {right!r}?"
    facts = _facts(contract, root)

    found: list[Declaration] = []
    if source is not None:
        for candidate in (
            source.key(f"layers.{left}", "may_import"),
            source.table(f"layers.{left}"),
            source.table(f"layers.{right}"),
        ):
            if candidate is not None:
                found.append(candidate)

    why = [d.comment for d in found if d.comment]
    for name in (left, right):
        if contract.layers[name].description:
            why.append(f"{name}: {contract.layers[name].description}")

    if permitted:
        return Explanation(
            question=question,
            verdict="allowed",
            rule="layer",
            summary=f"{left!r} lists {right!r} in may_import: {', '.join(layer.may_import)}",
            declarations=tuple(found),
            why=tuple(dict.fromkeys(why)),
            context=facts,
        )

    reachable = _layers_that_may_import(contract, right) or ["none"]
    allowed = ", ".join(layer.may_import) or "nothing"
    instead = [
        f"put the code that needs {right!r} in a layer that may reach it: {', '.join(reachable)}",
        f"or move what you need out of {right!r} into a layer {left!r} may import: {allowed}",
        (
            f"adding {right!r} to may_import in contracts.toml changes the architecture for "
            f"every file in {left!r}, not just this one"
        ),
        f"waive this one line with {WAIVER_COST['inline']}",
    ]
    return Explanation(
        question=question,
        verdict="forbidden",
        rule="layer",
        summary=f"{left!r} may import: {allowed}. {right!r} is not on that list.",
        declarations=tuple(found),
        why=tuple(dict.fromkeys(why)),
        instead=tuple(instead),
        context=facts,
    )


def _layer_package(
    contract: Contract, root: Path, source: ContractSource | None, left: str, package: str
) -> Explanation:
    layer = contract.layers[left]
    question = f"may {left!r} import the {package!r} package?"
    facts = _facts(contract, root)
    forbidden = package in layer.forbid_packages

    found: list[Declaration] = []
    if source is not None and forbidden:
        declaration = source.key(f"layers.{left}", "forbid_packages")
        if declaration is not None:
            found.append(declaration)

    if not forbidden:
        return Explanation(
            question=question,
            verdict="allowed",
            rule="layer-package",
            summary=f"{left!r} forbids: {', '.join(layer.forbid_packages) or 'no package'}",
            unknown=(
                f"no rule in contracts.toml mentions {package!r}; that is not the same as "
                f"the import being a good idea",
            ),
            context=facts,
        )

    open_layers = sorted(
        name for name, other in contract.layers.items() if package not in other.forbid_packages
    ) or ["none"]
    why = [d.comment for d in found if d.comment]
    if layer.description:
        why.append(f"{left}: {layer.description}")

    return Explanation(
        question=question,
        verdict="forbidden",
        rule="layer-package",
        summary=f"{left!r} declares forbid_packages = {layer.forbid_packages}",
        declarations=tuple(found),
        why=tuple(dict.fromkeys(why)),
        instead=(
            f"do the {package!r} work in a layer that is allowed it: {', '.join(open_layers)}, "
            f"and pass the result across",
            f"waive this one line with {WAIVER_COST['inline']}",
        ),
        context=facts,
    )


def _module_pair(
    contract: Contract, root: Path, source: ContractSource | None, left: str, right: str
) -> Explanation:
    question = f"may module {left!r} import module {right!r}?"
    facts = _facts(contract, root)
    found, why, unknown = _rule_declarations(contract, source, CROSS_MODULE)

    if not contract.enforce_placement:
        return Explanation(
            question=question,
            verdict="allowed",
            rule=CROSS_MODULE,
            summary="[rules.placement] is disabled in this contract, so modules may import "
            "each other and nothing checks the direction",
            declarations=found,
            unknown=unknown,
            context=facts,
        )

    crossing = _crossing(root, left, right)
    facade = facade_path(right)
    facade_exists = (root / facade).is_file()
    facts["imported_names"] = list(crossing.names)
    facts["evidence"] = list(crossing.evidence)
    facts["facade"] = facade
    facts["facade_exists"] = facade_exists

    declared = right in contract.module_deps.get(left, [])
    deps_line = source.key(f"modules.{left}", "depends_on") if source else None
    declarations = (*found, deps_line) if deps_line is not None else found
    create = (
        ""
        if facade_exists
        else f" ({facade} does not exist yet: create it with a function that takes the "
        f"session and tenant_id and returns DTOs)"
    )

    if declared:
        return Explanation(
            question=question,
            verdict="allowed",
            rule=CROSS_MODULE,
            summary=f"{left!r} declares {right!r} in depends_on, so it may import "
            f"modules.{right}.public -- and nothing else of {right!r}",
            declarations=declarations,
            why=why,
            instead=(
                f"import modules.{right}.public{create}",
                f"everything else in modules/{right}/ is private to it: add a function to "
                f"{facade} rather than importing a service, repository or entity",
            ),
            unknown=unknown,
            context=facts,
        )

    vocabulary = next(
        (d for d in crossing.dotted_all if is_vocabulary(d)),
        crossing.dotted if is_vocabulary(crossing.dotted) else None,
    )
    instead = [
        f"to read data {right!r} owns: call a function in {facade} that returns DTOs{create}, "
        f"and add {right!r} to depends_on under [modules.{left}] in contracts.toml",
        f"to react to something {right!r} did: subscribe to the event it publishes through "
        f"the outbox, and depend on nothing",
        f"if it is an enum or a type both modules speak: move it to "
        f"{_suggest_shared_target(vocabulary or f'modules.{right}.enums')} and import it "
        f"from both",
        f"if only {left!r} needs it, it belongs in {left!r}",
        f"waive this one line with {WAIVER_COST['inline']} while the move is in flight",
    ]
    return Explanation(
        question=question,
        verdict="forbidden",
        rule=CROSS_MODULE,
        summary=f"{left!r} does not declare {right!r} in depends_on, so it may not import it "
        f"-- and even when it does, only through {facade}",
        declarations=declarations,
        why=why,
        instead=tuple(instead),
        unknown=unknown,
        context=facts,
    )


def _shared_direction(
    contract: Contract, root: Path, source: ContractSource | None, module: str
) -> Explanation:
    found, why, unknown = _rule_declarations(contract, source, SHARED_RULE)
    return Explanation(
        question=f"may shared/ import module {module!r}?",
        verdict="forbidden" if contract.enforce_placement else "allowed",
        rule=SHARED_RULE,
        summary="the direction is one-way: modules import shared/, shared/ imports no module",
        declarations=found,
        why=why,
        instead=RULES[SHARED_RULE].instead,
        unknown=unknown,
        context=_facts(contract, root),
    )


def _unknown_pair(
    contract: Contract, root: Path, left: str, right: str, notes: list[str]
) -> Explanation:
    facts = _facts(contract, root)
    return Explanation(
        question=f"may {left!r} import {right!r}?",
        verdict="unknown",
        summary="neither name resolves to something this contract declares",
        unknown=(
            *notes,
            f"known layers: {', '.join(sorted(contract.layers)) or 'none'}",
            f"modules on disk: {', '.join(facts['modules']) or 'none'}",
            "ask about a layer, a module, or a package the contract names",
        ),
        context=facts,
    )


def _explain_pair(
    contract: Contract, root: Path, source: ContractSource | None, left: str, right: str
) -> Explanation:
    left_kind = _kind_of(contract, root, left)
    right_kind = _kind_of(contract, root, right)

    # `shared` is both a layer and a directory. As the *importer* it is only
    # ever the placement question, which is the one with an answer.
    if left == "shared" and right_kind == "module":
        return _shared_direction(contract, root, source, right)
    if left_kind == "layer" and right_kind == "layer":
        return _layer_pair(contract, root, source, left, right)
    if left_kind == "layer" and right_kind in ("package", "unknown"):
        return _layer_package(contract, root, source, left, right)
    if left_kind == "module" and right_kind == "module":
        return _module_pair(contract, root, source, left, right)

    notes = [f"{name!r} is {kind}" for name, kind in ((left, left_kind), (right, right_kind))]
    return _unknown_pair(contract, root, left, right, notes)


def _explain_layer(
    contract: Contract, root: Path, source: ContractSource | None, name: str
) -> Explanation:
    layer = contract.layers[name]
    found: list[Declaration] = []
    if source is not None:
        for candidate in (
            source.table(f"layers.{name}"),
            source.key(f"layers.{name}", "paths"),
            source.key(f"layers.{name}", "may_import"),
            source.key(f"layers.{name}", "forbid_packages"),
        ):
            if candidate is not None:
                found.append(candidate)

    why = [d.comment for d in found if d.comment]
    if layer.description:
        why.insert(0, layer.description)

    return Explanation(
        question=f"what is the {name!r} layer allowed to do?",
        verdict="informational",
        rule="layer",
        summary=f"{name!r} claims {', '.join(layer.paths) or 'no path'} and may import "
        f"{', '.join(layer.may_import) or 'nothing'}",
        declarations=tuple(found),
        why=tuple(dict.fromkeys(why)),
        context=_facts(contract, root),
    )


def _explain_file(
    contract: Contract, root: Path, source: ContractSource | None, relative: str
) -> Explanation:
    relative = relative.replace("\\", "/").lstrip("./")
    facts = _facts(contract, root)
    facts["file"] = relative
    facts["violations"] = [
        {"path": v.path, "line": v.line, "rule": v.rule, "message": v.message, "why": v.why}
        for v in check(contract, root)
        if v.path == relative
    ]

    layer = contract.layer_for(relative)
    if layer is None:
        return Explanation(
            question=f"what may {relative} do?",
            verdict="unknown",
            summary="no layer claims this path, so the layer rules are not applied to it",
            unknown=(
                f"no layer in contracts.toml has a paths pattern matching {relative!r}",
                "a path is opted in by naming it in a layer; nothing is guessed",
                "the call, import and async rules still apply to it",
            ),
            context=facts,
        )

    facts["layer"] = layer.name
    facts["may_import"] = layer.may_import
    facts["forbid_packages"] = layer.forbid_packages
    facts["forbidden_calls"] = [
        {"pattern": rule.pattern, "why": rule.why}
        for rule in contract.forbid_calls
        if not any(match_path(relative, pattern) for pattern in rule.except_in)
    ]

    answer = _explain_layer(contract, root, source, layer.name)
    return Explanation(
        question=f"what may {relative} do?",
        verdict="informational",
        rule="layer",
        summary=f"{relative} is in the {layer.name!r} layer, which may import "
        f"{', '.join(layer.may_import) or 'nothing'}",
        declarations=answer.declarations,
        why=answer.why,
        instead=(
            f"may import: {', '.join(layer.may_import) or 'nothing'}",
            f"must not import: {', '.join(layer.forbid_packages) or 'no package'}",
            f"one import at a time: jfast contracts explain {layer.name} <target>",
            f"waive one line with {WAIVER_COST['inline']}",
        ),
        context=facts,
    )


def _catalogue(contract: Contract, root: Path, source: ContractSource | None) -> Explanation:
    facts = _facts(contract, root)
    facts["rules"] = []
    for name, doc in RULES.items():
        found, why, unknown = _rule_declarations(contract, source, name)
        facts["rules"].append(
            {
                "rule": name,
                "summary": doc.summary,
                "declared_in": doc.declared_in,
                "declared_at": [d.where for d in found if d.line],
                "why": list(why),
                "unknown": list(unknown),
            }
        )
    return Explanation(
        question="which rules can fire here, and where is each one declared?",
        verdict="informational",
        summary=f"{len(RULES)} rules, declared in {Path(source.path).name if source else '?'}",
        context=facts,
    )


def explain(
    contract: Contract,
    root: Path,
    *,
    subject: Sequence[str] = (),
    rule: str | None = None,
    file: str | None = None,
) -> Explanation:
    """Answer one question about the contract, or say why it cannot be answered."""
    source = ContractSource.for_contract(contract)

    if rule is not None:
        if rule not in RULES:
            return Explanation(
                question=f"what is the {rule!r} rule?",
                verdict="unknown",
                summary=f"{rule!r} is not a rule this checker reports",
                unknown=(f"known rules: {', '.join(RULES)}",),
                context=_facts(contract, root),
            )
        return _explain_rule(contract, root, source, rule)

    if file is not None:
        return _explain_file(contract, root, source, file)

    subject = [part for part in subject if part]
    if len(subject) >= 2:
        return _explain_pair(contract, root, source, subject[0], subject[1])

    if len(subject) == 1:
        name = subject[0]
        if name in RULES:
            return _explain_rule(contract, root, source, name)
        kind = _kind_of(contract, root, name)
        if kind == "layer":
            return _explain_layer(contract, root, source, name)
        if kind == "path":
            return _explain_file(contract, root, source, name)
        if kind == "module":
            return _module_pair(contract, root, source, name, "<any other module>")
        return Explanation(
            question=f"what is {name!r}?",
            verdict="unknown",
            summary="that name is not a rule, a layer, a module or a path this contract knows",
            unknown=(
                f"known rules: {', '.join(RULES)}",
                f"known layers: {', '.join(sorted(contract.layers)) or 'none'}",
            ),
            context=_facts(contract, root),
        )

    return _catalogue(contract, root, source)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

COMPARES = (
    "the architecture contracts.toml permits against the imports the code actually makes. "
    "This is not a git diff: no previous revision is read, and nothing here knows what the "
    "code looked like yesterday."
)

LIMITS = (
    "`+` is an edge the code has and the contract does not permit -- the same finding "
    "`contracts check` reports, restated as an architecture change.",
    "`-` is an edge the contract permits that no import uses: a permission that could be "
    "tightened, not something that was removed.",
    "Only static imports are counted, and only between declared layers and directories "
    "under modules/.",
)

#: Appended to LIMITS only when some layer governs nothing, so a report with no
#: `~` line does not carry an explanation of a mark it never printed.
UNGOVERNED_LIMIT = (
    "`~` is a permission on a layer that governs no file here. It is neither unused nor a "
    "candidate for tightening: no import could have used it, because the layer holds nothing. "
    "`contracts check` reports the same state as `layer-unmatched`."
)


@dataclass(frozen=True)
class EdgeDelta:
    """One difference between the permitted architecture and the built one."""

    kind: str
    source: str
    target: str
    state: str
    rule: str
    reason: str
    evidence: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    declaration: Declaration | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "rule": self.rule,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "names": list(self.names),
            "declaration": self.declaration.describe() if self.declaration else None,
        }


@dataclass(frozen=True)
class ArchitectureDiff:
    project: str
    compares: str
    added: tuple[EdgeDelta, ...]
    removed: tuple[EdgeDelta, ...]
    costs: tuple[str, ...]
    limits: tuple[str, ...] = LIMITS
    ungoverned: tuple[str, ...] = ()
    """Layers whose paths match no file here, so nothing they permit is observable."""
    unsound: tuple[EdgeDelta, ...] = ()
    """Permissions held out of `removed` because a layer in them governs nothing."""

    def describe(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "compares": self.compares,
            "added": [d.describe() for d in self.added],
            "removed": [d.describe() for d in self.removed],
            "unsound": [d.describe() for d in self.unsound],
            "ungoverned_layers": list(self.ungoverned),
            "costs": list(self.costs),
            "limits": list(self.limits),
        }


def _observed_layer_edges(contract: Contract, root: Path) -> dict[tuple[str, str], list[str]]:
    """Layer-to-layer imports the code makes, resolved the way `check` does.

    The resolution has to be the checker's own, or `diff` and `check` disagree
    about the same import and neither can be trusted.
    """
    edges: dict[tuple[str, str], list[str]] = {}
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        layer = contract.layer_for(relative)
        if layer is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue

        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                targets = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_relative(node.module, node.level, path, root)
                targets = [(resolved, node.lineno)] if resolved else []
                names = tuple(alias.name for alias in node.names)
            else:
                continue
            for dotted, line in targets:
                if dotted is None:
                    continue
                # Another module's facade is a module edge, reported below with
                # its own rule -- the same exemption `check` makes.
                if crosses_to_facade(relative, dotted, names):
                    continue
                target = _layer_of_module(contract, dotted)
                if target is None or target.name == layer.name:
                    continue
                edges.setdefault((layer.name, target.name), []).append(f"{relative}:{line}")
    return edges


@dataclass(frozen=True)
class _Crossing:
    """One module reaching into another, as the source shows it."""

    names: tuple[str, ...]
    evidence: tuple[str, ...]
    # The dotted module actually imported, so the suggested destination is the
    # one `check` already printed. Two commands naming different files for the
    # same move is the confusion this command exists to remove.
    dotted: str
    # Imports that reach past the facade. Empty means every import of the
    # target went through its public.py, which is a permitted edge once
    # declared rather than a violation.
    private: tuple[str, ...] = ()
    dotted_all: tuple[str, ...] = ()


def _crossing(root: Path, source: str, target: str) -> _Crossing:
    """What *source* imports from *target*, and the lines it does it on.

    The edge itself comes from `project.module_edges`; this only annotates it,
    so that the cost of enforcing the contract can be stated as "you lose this
    name" rather than "delete that line".
    """
    fallback = f"modules.{target}"
    directory = root / "modules" / source
    if not directory.is_dir():
        return _Crossing((), (), fallback)

    names: list[str] = []
    evidence: list[str] = []
    dotted_seen: list[str] = []
    private: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            dotted = _resolve_relative(node.module, node.level, path, root)
            if dotted is None or not (dotted == fallback or dotted.startswith(fallback + ".")):
                continue
            evidence.append(f"{relative}:{node.lineno}")
            dotted_seen.append(dotted)
            imported = [alias.name for alias in node.names]
            names.extend(imported)
            if not is_facade(dotted, imported):
                private.append(f"{relative}:{node.lineno}")
    reached = [d for d in dotted_seen if not is_facade(d, ["public"] if d == fallback else ())]
    return _Crossing(
        tuple(dict.fromkeys(names)),
        tuple(evidence),
        (reached or dotted_seen or [fallback])[0],
        tuple(private),
        tuple(dict.fromkeys(dotted_seen)),
    )


def diff(contract: Contract, root: Path) -> ArchitectureDiff:
    """Compare the architecture the contract permits with the one the code built.

    The `-` half is the delicate one. It is `permitted - observed`, and that
    subtraction means "no import uses this" only while every layer in the edge
    governs at least one file. On a contract whose globs match nothing --
    a service scaffolded with the layered contract holding hexagonal modules --
    every permission it declares lands in `-` at once, and the command sells
    the outage as ten opportunities to tighten the contract.

    The answer is not to refuse the whole report: edges between layers that do
    govern files are still answerable, and dropping them would cost a working
    diagnostic to fix a broken one. The unanswerable edges are separated out
    under `~` with the reason they are unanswerable, and the layers governing
    nothing are named. `layer_matches` decides which those are -- the same
    function `check_coverage` uses, so `diff` and `contracts check` cannot
    disagree about which layer is empty.
    """
    source = ContractSource.for_contract(contract)
    placement = source.table("rules.placement") if source else None

    added: list[EdgeDelta] = []
    removed: list[EdgeDelta] = []
    unsound: list[EdgeDelta] = []
    costs: list[str] = []

    observed = _observed_layer_edges(contract, root)
    permitted = {
        (name, target) for name, layer in contract.layers.items() for target in layer.may_import
    }
    ungoverned = sorted(name for name, count in layer_matches(contract, root).items() if not count)

    for (left, right), evidence in sorted(observed.items()):
        if (left, right) in permitted:
            continue
        declaration = source.key(f"layers.{left}", "may_import") if source else None
        allowed = ", ".join(contract.layers[left].may_import) or "nothing"
        added.append(
            EdgeDelta(
                kind="layer",
                source=left,
                target=right,
                state="added",
                rule="layer",
                reason=f"{left!r} may import: {allowed}",
                evidence=tuple(evidence),
                declaration=declaration,
            )
        )

    for left, right in sorted(permitted - set(observed)):
        declaration = source.key(f"layers.{left}", "may_import") if source else None
        empty = [name for name in (left, right) if name in ungoverned]
        if empty:
            names = " and ".join(repr(name) for name in empty)
            unsound.append(
                EdgeDelta(
                    kind="layer",
                    source=left,
                    target=right,
                    state="ungoverned",
                    # The rule `contracts check` reports for this state, so the
                    # two commands name one condition one way.
                    rule="layer-unmatched",
                    reason=f"{names} {'governs' if len(empty) == 1 else 'govern'} no file",
                    declaration=declaration,
                )
            )
            continue
        removed.append(
            EdgeDelta(
                kind="layer",
                source=left,
                target=right,
                state="removed",
                rule="layer",
                reason="permitted, and no import uses it",
                declaration=declaration,
            )
        )

    snapshot = project_scan.load(root)
    observed_modules = set(project_scan.module_edges(snapshot))
    for left, right in sorted(observed_modules):
        if not contract.enforce_placement:
            continue
        crossing = _crossing(root, left, right)
        declared = right in contract.module_deps.get(left, [])
        facade = facade_path(right)
        if crossing.private:
            added.append(
                EdgeDelta(
                    kind="module",
                    source=left,
                    target=right,
                    state="added",
                    rule=CROSS_MODULE,
                    reason=f"reaches past {facade}, the only file another module may import",
                    evidence=crossing.private,
                    names=crossing.names,
                    declaration=placement,
                )
            )
            for name in crossing.names:
                if name == "public":
                    continue
                if is_vocabulary(crossing.dotted):
                    remedy = (
                        f"move it to {_suggest_shared_target(crossing.dotted)} "
                        f"and import it from both"
                    )
                else:
                    remedy = f"expose what it needs from {facade} as a function returning DTOs"
                costs.append(
                    f"{left} loses direct access to {right}.{name} when that import goes -- "
                    f"{remedy}"
                )
            continue
        if declared:
            # Through the facade and declared: the architecture as permitted.
            continue
        added.append(
            EdgeDelta(
                kind="module",
                source=left,
                target=right,
                state="added",
                rule=UNDECLARED_RULE,
                reason=f"calls {facade} without {right!r} in depends_on",
                evidence=crossing.evidence,
                names=crossing.names,
                declaration=source.table(f"modules.{left}") if source else None,
            )
        )

    if contract.enforce_placement:
        known = set(snapshot.module_names)
        for left, deps in sorted(contract.module_deps.items()):
            for right in deps:
                if (left, right) in observed_modules or right not in known:
                    continue
                removed.append(
                    EdgeDelta(
                        kind="module",
                        source=left,
                        target=right,
                        state="removed",
                        rule=UNDECLARED_RULE,
                        reason="declared in depends_on, and no import uses it",
                        declaration=source.key(f"modules.{left}", "depends_on") if source else None,
                    )
                )

    for file, module in sorted(snapshot.shared_module_imports):
        if not contract.enforce_placement:
            continue
        added.append(
            EdgeDelta(
                kind="module",
                source="shared",
                target=module,
                state="added",
                rule=SHARED_RULE,
                reason="the direction is one-way: modules import shared/, never the reverse",
                evidence=(file,),
                declaration=placement,
            )
        )

    return ArchitectureDiff(
        project=contract.project,
        compares=COMPARES,
        added=tuple(added),
        removed=tuple(removed),
        costs=tuple(costs),
        limits=(*LIMITS, UNGOVERNED_LIMIT) if ungoverned else LIMITS,
        ungoverned=tuple(ungoverned),
        unsound=tuple(unsound),
    )
