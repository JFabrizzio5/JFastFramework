"""Two commands written for a reader with a context window instead of eyes.

``jfast ai context`` is everything a model needs about a project before it
edits anything, in one call. ``jfast next`` is the answer to "what is still
unfinished", ordered so the steps can actually be done in that order.

Both are compositions. Nothing here re-reads the filesystem or re-implements a
check: :mod:`jfastframework.project` already knows what a module is,
:mod:`jfastframework.contracts` already knows what the rules are, and
:mod:`jfastframework.cli.insight` already knows how to draw both. What these
add is a single entry point, because the alternative -- five commands and a
guess about which of them to run first -- is exactly the thing an agent gets
wrong.

**The size is the design.** The obvious implementation of "everything a model
needs" is to concatenate ``docs/``, and it is wrong twice over: ``docs/`` is not
in the wheel (``pyproject.toml`` ships ``packages = ["src/jfastframework"]``),
so a ``pip install jfastframework`` user has none of it on disk, and the 53
pages come to 555 KB -- around 139k tokens, more than the answer is worth by
two orders of magnitude. What goes in here is *facts about this project*: what
exists, how it is wired, what the rules are, what is broken, what is next. The
manual is a URL. Everything left out is named in ``omitted``, with the command
that returns it, so a model can ask for the rest rather than assume there is
none.
"""

from __future__ import annotations

import json as jsonlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from jfastframework import project as project_model
from jfastframework.cli import insight, ui
from jfastframework.cli.exits import Code
from jfastframework.contracts import CONTRACTS_FILE, Contract, Violation, check, waivers

__all__ = ["STAGES", "Step", "Survey", "context_payload", "register", "steps", "survey"]

CONFIG_FILE = "jfast.toml"

DOCS_URL = "https://jfabrizzio5.github.io/JFastFramework/latest/"

#: How many findings and violations the context carries before it stops and
#: names the command that returns the rest. Twenty is enough to see the shape
#: of the problem; two hundred is a wall of text that costs more than reading
#: the code it describes.
DETAIL_CAP = 20

#: Strings the generated `contracts.toml` leaves for you to replace. A contract
#: still full of them has not been written yet, whatever the file's mtime says.
PLACEHOLDER = "TODO"

#: `analyze`'s code for a layer whose paths match no file, and `contracts
#: check`'s rule for the same fact. One problem, reported by two commands, and
#: `next` has to turn the pair into one step -- see :func:`_contract_steps`.
GOVERNS_NOTHING = "contract-governs-nothing"
UNMATCHED_RULE = "layer-unmatched"

#: Substituted into the remedy when the project records a module layout.
LAYOUT_SLOT = "<layout>"


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------

#: Each stage, and what makes it a *precondition* of the ones after it.
#:
#: This is the whole value of `jfast next`, and it is deliberately not severity
#: order. Severity says how bad a thing is; it says nothing about whether
#: fixing it now is wasted work. `code-outside-module` is a `low` finding and
#: `module-no-migration` is `medium`, yet the loose file has to move first:
#: moving it changes which tables the project declares, so a revision generated
#: before the move is a revision you regenerate after it.
STAGES: tuple[tuple[str, str], ...] = (
    ("boot", "the service has to start before anything below it can be observed at all"),
    ("scaffold", "there is nothing to wire, migrate or test until a module exists"),
    (
        "wire",
        "a module main.py never imports serves nothing: its tests pass, its routes 404, and "
        "every step below it about that module proves something about dead code",
    ),
    (
        "shape",
        "these steps move code between files, and a migration or a test written against the "
        "old location is rewritten when it moves",
    ),
    (
        "persist",
        "the table has to exist before any request can succeed, and the step above decides "
        "which tables there are",
    ),
    (
        "verify",
        "the contract is checked against where the files ended up, so it is worth checking "
        "once they have stopped moving",
    ),
    (
        "cover",
        "a test is worth writing once the code under it is wired, placed and backed by a "
        "table; before that it asserts the wrong thing and passes",
    ),
    ("document", "prose last: it describes what the stages above settled"),
)

RANK: dict[str, int] = {name: index for index, (name, _) in enumerate(STAGES)}

#: Which stage each `analyze` finding belongs to. A code missing from here is a
#: new check in `project.py`, and it lands in `shape` rather than vanishing --
#: a step nobody sees is worse than one filed slightly early.
STAGE_OF_CODE: dict[str, str] = {
    "plugin-unknown": "boot",
    "module-unregistered": "wire",
    "module-cycle": "shape",
    "route-conflict": "shape",
    "shared-imports-module": "shape",
    "cross-module-import": "shape",
    "code-outside-module": "shape",
    "module-no-migration": "persist",
    # `verify`, not `shape`: nothing moves. The files are where they belong and
    # the contract is the thing describing the wrong tree.
    GOVERNS_NOTHING: "verify",
}

#: The remedy for each finding, as an imperative. `analyze` explains *why* in a
#: paragraph; a step needs the one line you type next.
REMEDY: dict[str, str] = {
    "plugin-unknown": "install its extra, or drop the name from [plugins].enabled",
    "module-unregistered": "edit main.py between the [jfast:imports] and [jfast:routers] markers",
    "module-cycle": "query one way through public.py, turn the other way into an event",
    "route-conflict": "give each router its own prefix",
    "shared-imports-module": "move the shared piece into shared/, or invert the import",
    "cross-module-import": "go through modules/<other>/public.py  (jfast contracts check names it)",
    "code-outside-module": "move it into a module, or into shared/",
    "module-no-migration": "jfast exec -- alembic revision --autogenerate",
    # The contract describes a tree this project does not have, so editing its
    # globs by hand is the long way round: the layout's own contract is a
    # template that ships. `<layout>` is filled in from the recorded layout
    # when there is one -- see `_contract_steps`.
    GOVERNS_NOTHING: f"jfast contracts init --layout {LAYOUT_SLOT} --force",
}


@dataclass(frozen=True)
class Step:
    """One thing left to do, and the command or edit that does it."""

    stage: str
    what: str
    do: str
    why: str
    path: str | None = None

    @property
    def rank(self) -> int:
        return RANK[self.stage]

    def describe(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "stage": self.stage,
            "what": self.what,
            "do": self.do,
            "why": self.why,
            "path": self.path,
        }


# ---------------------------------------------------------------------------
# Reading everything once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Survey:
    """Every source of truth about a project, read once.

    Both commands need the same four scans and each of them walks every file.
    Doing them once and passing the result around is the difference between a
    command an agent runs before every edit and one it learns to skip.
    """

    project: project_model.Project
    findings: list[project_model.Finding]
    contract: Contract | None
    contract_error: str | None
    violations: list[Violation]
    waived: list[Violation]
    known_plugins: frozenset[str]
    broken_plugins: dict[str, str]

    @property
    def root(self) -> Path:
        return self.project.root


def _plugin_state() -> tuple[frozenset[str], dict[str, str]]:
    """Every plugin name this installation can resolve, and why any failed.

    Deliberately not shared with `main.py`'s copy of the same three lines:
    `main.py` imports this module to register the commands, so importing it
    back would be a cycle. Three lines is the cheaper half of that trade.
    """
    from jfastframework.plugins import registry

    available = registry.discover()
    broken: dict[str, str] = getattr(registry.discover, "broken", {})
    return frozenset(available) | frozenset(broken), dict(broken)


def survey(root: Path) -> Survey:
    """Load, analyse and check *root*."""
    project = project_model.load(root)
    known, broken = _plugin_state()

    contract: Contract | None = None
    error: str | None = None
    source = root / CONTRACTS_FILE
    if source.is_file():
        try:
            contract = Contract.load(source)
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
            error = f"{CONTRACTS_FILE} does not parse: {exc}"

    return Survey(
        project=project,
        findings=project_model.analyze(project, known_plugins=known),
        contract=contract,
        contract_error=error,
        violations=check(contract, root) if contract else [],
        waived=waivers(root) if contract else [],
        known_plugins=known,
        broken_plugins=broken,
    )


# ---------------------------------------------------------------------------
# next
# ---------------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _placeholders(contract: Contract) -> list[str]:
    """Fields of the generated contract still holding their TODO text."""
    unfilled: list[str] = []
    if not contract.owns.strip() or contract.owns.strip().startswith(PLACEHOLDER):
        unfilled.append("owns")
    if not contract.does_not_own.strip() or contract.does_not_own.strip().startswith(PLACEHOLDER):
        unfilled.append("does_not_own")
    if any(rule.strip().startswith(PLACEHOLDER) for rule in contract.invariants):
        unfilled.append("invariants")
    return unfilled


def _recorded_layout(project: project_model.Project) -> str | None:
    """The layout this project's modules are actually in, if they agree on one.

    Two modules in two layouts is a real thing to be in the middle of, and
    naming either one in a `--force` command that overwrites the contract would
    be a guess with a destructive edit attached. Silence is the honest answer.
    """
    layouts = {module.layout for module in project.modules if module.layout}
    return layouts.pop() if len(layouts) == 1 else None


def _contract_steps(found: Survey) -> list[Step]:
    """The contract's own state, as at most one step.

    Three sources say the same thing about a contract aimed at another layout:
    `analyze` files one `contract-governs-nothing` per empty layer, `contracts
    check` files one `layer-unmatched` per empty layer, and both are true.
    Listed separately, four layers would produce four steps *plus* a
    `contracts check fails (4 violations)` summary of the same four -- five
    lines, one fact, and a remedy (`jfast analyze`) that only re-prints what
    the reader is already looking at.

    So the layers collapse into one step, and the violation count drops the
    rules that step already covers. A step is a thing to do; there is one thing
    to do here.
    """
    if found.contract_error:
        return [
            Step(
                stage="verify",
                what=found.contract_error,
                do=f"fix {CONTRACTS_FILE}, then jfast contracts check",
                why="Nothing is enforced while the contract does not parse, and nothing says so.",
                path=CONTRACTS_FILE,
            )
        ]
    if found.contract is None:
        return [
            Step(
                stage="verify",
                what=f"this service has no {CONTRACTS_FILE}",
                do="jfast contracts init",
                why=(
                    "Without one, the layer boundaries are a convention, and a convention is "
                    "what an agent generating code at speed drifts past without noticing."
                ),
            )
        ]

    collected: list[Step] = []
    ungoverned = [f for f in found.findings if f.code == GOVERNS_NOTHING]
    if ungoverned:
        layout = _recorded_layout(found.project)
        remedy = REMEDY[GOVERNS_NOTHING]
        collected.append(
            Step(
                stage=STAGE_OF_CODE[GOVERNS_NOTHING],
                what=(
                    f"{CONTRACTS_FILE} governs nothing: "
                    f"{_plural(len(ungoverned), 'layer')} match no file here"
                ),
                do=remedy.replace(LAYOUT_SLOT, layout) if layout else remedy,
                why=ungoverned[0].why,
                path=CONTRACTS_FILE,
            )
        )

    remaining = [v for v in found.violations if v.rule != UNMATCHED_RULE]
    if remaining:
        collected.append(
            Step(
                stage="verify",
                what=f"contracts check fails ({_plural(len(remaining), 'violation')})",
                do="jfast contracts check",
                why="Each violation names its file, its line and the rule it broke.",
            )
        )
    return collected


def steps(found: Survey) -> list[Step]:
    """What is left to do, in the order it can be done.

    Sorted by stage, then by path, never by severity -- see `STAGES` for what
    each stage is a precondition of.
    """
    project = found.project
    collected: list[Step] = []

    for finding in found.findings:
        if finding.code == GOVERNS_NOTHING:
            # Owned by `_contract_steps`, which states it once instead of once
            # per layer and knows the command that fixes it.
            continue
        stage = STAGE_OF_CODE.get(finding.code, "shape")
        collected.append(
            Step(
                stage=stage,
                what=finding.message,
                do=REMEDY.get(finding.code, f"jfast analyze  # {finding.code}"),
                why=finding.why,
                path=finding.path,
            )
        )

    if not project.modules:
        collected.append(
            Step(
                stage="scaffold",
                what="this service has no modules",
                do="jfast new module <name>",
                why="A service with no module is an app that serves only its health probes.",
            )
        )

    # `analyze` stays silent about migrations when there are none at all, so a
    # freshly generated project is not greeted with a finding. `next` is the
    # command whose entire job is the step after the one you just did, and on
    # that project the step is the first revision.
    if not project.migrations:
        for module in project.modules:
            if not module.tables:
                continue
            collected.append(
                Step(
                    stage="persist",
                    what=f"{', '.join(module.tables)} has no revision (this project has none)",
                    do="jfast exec -- alembic revision --autogenerate",
                    why=(
                        "The table is never created. The service starts and the first query "
                        "fails on a relation that does not exist."
                    ),
                    path=f"{module.path}/",
                )
            )

    collected.extend(_contract_steps(found))

    for module in project.modules:
        if not module.has_tests:
            collected.append(
                Step(
                    stage="cover",
                    what=f"{module.path} has no tests",
                    do=f"add {module.path}/tests/",
                    why="A module with no tests is a module nobody can change safely.",
                    path=f"{module.path}/",
                )
            )

    for module in project.modules:
        if not module.has_readme:
            collected.append(
                Step(
                    stage="document",
                    what=f"{module.path} has no README",
                    do=f"add {module.path}/README.md",
                    why=(
                        "One paragraph on what the module owns, and the file map for its "
                        "layout. It is the file the next reader opens first."
                    ),
                    path=f"{module.path}/",
                )
            )

    if found.contract is not None and (unfilled := _placeholders(found.contract)):
        collected.append(
            Step(
                stage="document",
                what=f"{CONTRACTS_FILE} still has its generated placeholders: "
                f"{', '.join(unfilled)}",
                do=f"edit {CONTRACTS_FILE}",
                why=(
                    "The defaults are a floor. The lines worth writing are the ones only you "
                    "know: what this service does not own, and the invariants no checker sees."
                ),
                path=CONTRACTS_FILE,
            )
        )

    collected.sort(key=lambda step: (step.rank, step.path or "", step.what))
    return collected


def _settled(found: Survey) -> str:
    """What was checked, on a project with nothing outstanding.

    Stated as facts rather than praise: the point of the line is that a reader
    can tell which checks ran, and therefore what "nothing outstanding" covers.
    """
    project = found.project
    counted = len(project.modules)
    lines = [
        f"  {counted} module{'' if counted == 1 else 's'}, every one registered, "
        f"tested and documented.",
        f"  {project.migrations} revision{'' if project.migrations == 1 else 's'} "
        f"cover every declared table.",
    ]
    if found.contract is not None:
        lines.append(f"  {CONTRACTS_FILE} passes, and its placeholders are filled in.")
    return "\n".join(lines)


def render_steps(found: Survey, collected: list[Step]) -> str:
    """The steps, numbered, each with what to type next to it."""
    glyph = ui.G
    if not collected:
        return f"  {glyph.tick} {found.project.name}: nothing outstanding\n\n{_settled(found)}"

    # Wide enough for the usual case, capped so one long message does not push
    # every remedy off the right edge of an 80-column terminal.
    width = min(max(len(step.what) for step in collected), 52)
    lines = [f"  next  {found.project.name}", ""]
    for index, step in enumerate(collected, start=1):
        head = f"  {index:>2}. {step.what}"
        if len(step.what) <= width:
            lines.append(f"{head}{' ' * (width - len(step.what))}   {step.do}")
        else:
            lines.append(head)
            lines.append(f"      {glyph.corner}{glyph.hbar} {step.do}")

    order: list[str] = []
    for step in collected:
        if step.stage not in order:
            order.append(step.stage)
    lines.append("")
    lines.append(f"  {' -> '.join(order)}   (dependency order, not severity)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ai context
# ---------------------------------------------------------------------------

COMMANDS: tuple[dict[str, str], ...] = (
    {
        "command": "jfast inspect --json",
        "returns": "every module: layout, routes, wiring, tables, files",
        "instead_of": "listing modules/ and opening each __init__.py",
    },
    {
        "command": "jfast inspect module <name> --json",
        "returns": "one module in full, including its file list",
        "instead_of": "walking a directory",
    },
    {
        "command": "jfast analyze --json",
        "returns": "structural findings, each with a severity and a remedy",
        "instead_of": "reading main.py to work out what is actually wired",
    },
    {
        "command": "jfast graph --format json",
        "returns": "module-to-module import edges",
        "instead_of": "reading every import statement in the project",
    },
    {
        "command": "jfast contracts show --json",
        "returns": "layers, forbidden calls and imports, interfaces, invariants",
        "instead_of": "inferring this project's rules from its code",
    },
    {
        "command": "jfast contracts check --json",
        "returns": "violations, each with file, line and rule",
        "instead_of": "asking whether a change is allowed",
    },
    {
        "command": "jfast migration check --json",
        "returns": "what each unapplied revision does to a database with rows in it",
        "instead_of": "reading migrations/versions/ and guessing which one locks the table",
    },
    {
        "command": "jfast next --json",
        "returns": "what is unfinished, in the order it can be done",
        "instead_of": "guessing which step of the last task was skipped",
    },
    {
        "command": "jfast describe --json",
        "returns": "resolved settings, plugin graph and routes -- imports the app, "
        "so it needs the dependencies installed and the code to parse",
        "instead_of": "reading jfast.toml and every plugin's source",
    },
)


def _module_summary(module: project_model.Module, *, with_files: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": module.name,
        "path": module.path,
        "layout": module.layout,
        "ui": module.ui,
        "routes": list(module.route_prefixes),
        "registered": module.registered,
        "has_tests": module.has_tests,
        "has_readme": module.has_readme,
        "tables": list(module.tables),
        "imports": list(module.imports),
        "external": list(module.external),
        "file_count": len(module.files),
        # The graph an agent writing a new module needs, without opening a
        # file: what each module offers (its facade), what it announces and
        # reacts to, and the background work it owns.
        "facade": list(module.facade),
        "depends_on": list(module.depends_on),
        "publishes": list(module.publishes),
        "subscribes": list(module.subscribes),
        "tasks": list(module.tasks),
    }
    if with_files:
        summary["files"] = list(module.files)
    return summary


def _step_dict(step: Step, *, brief: bool) -> dict[str, Any]:
    """A step as data. `--brief` keeps the instruction and drops the argument."""
    described = step.describe()
    if brief:
        return {key: described[key] for key in ("rank", "stage", "what", "do")}
    return described


def _violation_dict(violation: Violation) -> dict[str, Any]:
    return {
        "path": violation.path,
        "line": violation.line,
        "rule": violation.rule,
        "message": violation.message,
        "why": violation.why,
    }


def _omitted(*, module: str | None) -> dict[str, str]:
    """What is not in this payload, and the exact way to get it.

    Naming the gaps matters more than it looks. A model handed a partial answer
    with no seam in it assumes it is a complete one, and then writes code
    against a file it was never shown.
    """
    dropped = {
        "documentation": (
            f"53 pages, 555 KB (~139k tokens), and docs/ is not in the wheel -- a "
            f"pip-installed project has none of it on disk. Read it at {DOCS_URL}"
        ),
        "source": (
            "no file contents, ever. This says what exists and how it is wired; open the "
            "files it names."
        ),
        "runtime": (
            "resolved settings, the live plugin graph and the route table are not here: "
            "they need the app imported. `jfast describe --json`"
        ),
        "migrations": (
            "the revision count is here; what each unapplied revision would do to a "
            "database with rows in it is not -- a table rewrite, a lock, a column "
            "dropped. `jfast migration check --json`, and `jfast migration plan` for the "
            "safe rewrite of the next risky one."
        ),
        "history": "no git log, no CHANGELOG. `git log` is better at that than a JSON blob.",
    }
    if module is None:
        dropped["module_files"] = (
            "per-module file listings. `jfast ai context --module <name>` narrows to one "
            "module and lists its files"
        )
    else:
        dropped["scope"] = (
            f"--module narrowed modules, findings and violations to {module}. `next` is "
            f"deliberately still project-wide: the step that blocks you may be elsewhere."
        )
    return dropped


#: `--brief` replaces `omitted` wholesale rather than adding a line to it. The
#: paragraph explaining what was dropped is itself one of the larger things in
#: the payload, and a "brief" form that spends a fifth of its budget saying so
#: has misunderstood the request.
BRIEF_OMITTED = (
    "--brief: no contract rules, no finding or violation lists, no shared/, no docs, "
    f"no source. Re-run without --brief, then see the commands. Manual: {DOCS_URL}"
)


def context_payload(
    found: Survey,
    *,
    module: str | None = None,
    brief: bool = False,
) -> dict[str, Any]:
    """Everything a model needs about this project, and nothing it does not."""
    project = found.project
    chosen = [m for m in project.modules if module is None or m.name == module]

    findings = found.findings
    violations = found.violations
    if module is not None:
        prefix = f"modules/{module}"
        findings = [f for f in findings if (f.path or "").startswith(prefix)]
        violations = [v for v in violations if v.path.startswith(prefix)]

    unknown = sorted(name for name in project.plugins if name not in found.known_plugins)

    payload: dict[str, Any] = {
        "schema_version": project_model.SCHEMA_VERSION,
        "generated_by": "jfast ai context",
        "project": {
            "name": project.name,
            "version": project.version,
            "env": project.env,
            "root": str(project.root),
            "frontend": project.frontend,
            "migrations": project.migrations,
            "module_count": len(project.modules),
        },
        "plugins": {
            "enabled": list(project.plugins),
            "disabled": list(project.disabled),
            "enabled_but_not_installed": unknown,
            "installed_but_broken": found.broken_plugins,
        },
        "modules": [_module_summary(m, with_files=module is not None) for m in chosen],
        "graph": insight.graph_payload(project, module),
        "contract": None,
        "checks": {
            "analyze": {
                "ok": not findings,
                "counts": insight.severity_counts(findings),
            },
            "contracts": {
                "ok": found.contract is not None and not violations,
                "declared": found.contract is not None,
                "violations": len(violations),
                "waivers": len(found.waived),
            },
        },
        "next": [_step_dict(step, brief=brief) for step in steps(found)],
        "commands": [entry["command"] for entry in COMMANDS] if brief else list(COMMANDS),
        "omitted": BRIEF_OMITTED if brief else _omitted(module=module),
    }

    if found.contract is not None:
        described = found.contract.describe()
        payload["contract"] = (
            {
                "project": described["project"],
                "owns": described["owns"],
                "does_not_own": described["does_not_own"],
                "layers": sorted(described["layers"]),
                "invariants": described["invariants"],
                "full": "jfast contracts show --json",
            }
            if brief
            else described
        )
    elif found.contract_error:
        payload["contract"] = {"error": found.contract_error}

    if not brief:
        from jfastframework.contracts.wiring import graph
        from jfastframework.contracts.wiring import scan as scan_wiring

        # Who publishes each event, who listens, who owns each task and who
        # queues it -- the edges no import shows.
        payload["events"] = graph(
            found.contract.module_publishes if found.contract else {}, scan_wiring(project.root)
        )
        payload["shared"] = {
            "files": list(project.shared_files),
            "imports_modules": [
                {"file": file, "module": name} for file, name in project.shared_module_imports
            ],
        }
        analyze_block = payload["checks"]["analyze"]
        analyze_block["findings"] = [f.describe() for f in findings[:DETAIL_CAP]]
        contracts_block = payload["checks"]["contracts"]
        contracts_block["detail"] = [_violation_dict(v) for v in violations[:DETAIL_CAP]]
        if len(findings) > DETAIL_CAP or len(violations) > DETAIL_CAP:
            payload["checks"]["truncated"] = {
                "findings": len(findings),
                "violations": len(violations),
                "full": "jfast analyze --json, jfast contracts check --json",
            }

    return payload


def _sizes(payload: dict[str, Any]) -> list[tuple[str, int]]:
    """Bytes each top-level section costs, biggest first."""
    measured = [
        (key, len(jsonlib.dumps(value, default=str).encode("utf-8")))
        for key, value in payload.items()
    ]
    measured.sort(key=lambda pair: -pair[1])
    return measured


def render_size(found: Survey) -> str:
    """How much this project's context costs, measured rather than guessed.

    The token figures are bytes/4. That is the standard rough estimate and it
    is not a tokenizer; the framework does not ship one, and installing a
    dependency so a diagnostic can be 5% more precise is a poor trade.
    """
    full = context_payload(found)
    brief = context_payload(found, brief=True)
    full_bytes = len(jsonlib.dumps(full, indent=2, default=str).encode("utf-8"))
    brief_bytes = len(jsonlib.dumps(brief, indent=2, default=str).encode("utf-8"))

    lines = [
        f"  ai context  {found.project.name}  ({len(found.project.modules)} modules)",
        "",
        f"  full     {full_bytes:>8,} bytes   ~{full_bytes // 4:>6,} tokens",
        f"  --brief  {brief_bytes:>8,} bytes   ~{brief_bytes // 4:>6,} tokens",
        "",
        "  by section (full, compact)",
    ]
    lines.extend(f"    {key:<18}{size:>8,}" for key, size in _sizes(full))
    lines.append("")
    lines.append("  token figures are bytes/4, an estimate and not a tokenizer.")
    return "\n".join(lines)


def render_context(found: Survey, payload: dict[str, Any]) -> str:
    """The same facts for a human, so `--text` is not a wall of JSON."""
    glyph = ui.G
    project = found.project
    lines = [f"  {project.name} {project.version} ({project.env})", ""]
    for module in payload["modules"]:
        marks = "".join(
            (
                "r" if module["registered"] else "-",
                "t" if module["has_tests"] else "-",
                "d" if module["has_readme"] else "-",
            )
        )
        routes = ", ".join(module["routes"]) or "-"
        lines.append(f"    {module['name']:<16}{marks}  {routes}")
    lines.append("")
    lines.append(f"  contract   {'declared' if found.contract else 'none'}")
    lines.append(f"  findings   {sum(payload['checks']['analyze']['counts'].values())}")
    lines.append(f"  violations {payload['checks']['contracts']['violations']}")
    lines.append("")
    outstanding = len(payload["next"])
    if outstanding:
        lines.append(f"  {glyph.cross} {outstanding} outstanding   ->  jfast next")
    else:
        lines.append(f"  {glyph.tick} nothing outstanding")
    lines.append("")
    lines.append("  --json is the form this command exists for.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

ai_app = typer.Typer(
    help="One call that tells a model everything it needs about this project.",
    no_args_is_help=True,
)


def _survey_or_exit(path: Path) -> Survey:
    root = path.resolve()
    if not (root / CONFIG_FILE).is_file():
        typer.echo(
            f"no {CONFIG_FILE} in {root}\nRun this inside a service, or point at one with --path."
        )
        raise typer.Exit(Code.CONFIG)
    return survey(root)


@ai_app.command("context")
def ai_context(
    path: Annotated[Path, typer.Option("--path", "-p", help="Project root.")] = Path("."),
    module: Annotated[
        str | None,
        typer.Option("--module", "-m", help="Narrow to one module, and list its files."),
    ] = None,
    brief: Annotated[
        bool, typer.Option("--brief", help="Drop the detail, keep the shape.")
    ] = False,
    size: Annotated[
        bool, typer.Option("--size", help="What this costs, by section. Nothing else.")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json/--text", help="Machine-readable output.")
    ] = True,
) -> None:
    """Everything a model needs about this project, in one call.

    Composed from `inspect`, `analyze`, `graph`, `contracts` and `next` rather
    than reimplementing any of them, so it cannot disagree with the command it
    tells you to run.

    What it is not is a documentation dump. `docs/` is not in the wheel, and
    the 53 pages come to roughly 139k tokens -- a context command that expensive
    is one nobody calls twice. This carries facts about *this* project and names
    everything it left out under `omitted`, with the command that returns it.
    Narrow with `--module <name>`, or drop the detail with `--brief`.
    """
    found = _survey_or_exit(path)
    if module is not None and found.project.module(module) is None:
        names = ", ".join(found.project.module_names) or "none"
        typer.echo(f"no module {module!r}. Found: {names}")
        raise typer.Exit(Code.USAGE)

    if size:
        typer.echo(render_size(found))
        return

    payload = context_payload(found, module=module, brief=brief)
    if json_out:
        typer.echo(jsonlib.dumps(payload, indent=2, default=str))
    else:
        typer.echo(render_context(found, payload))


def next_steps(
    path: Annotated[Path, typer.Option("--path", "-p", help="Project root.")] = Path("."),
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """What is unfinished, in the order it can be done.

    The same engine as `jfast analyze`, turned around: `analyze` says what is
    wrong, this says what to do next. The ordering is the point. A module
    main.py never imports comes before its missing tests, because a test over
    an unwired module passes while the route 404s -- so the order is derived
    from what each step depends on, never from severity. `jfast next --json`
    carries the stage and its rank; `STAGES` in this module says what each
    stage is a precondition of.

    Always exits 0. It is a question, not a gate -- `jfast analyze --fail-on`
    is the gate.
    """
    found = _survey_or_exit(path)
    collected = steps(found)
    payload = {
        "schema_version": project_model.SCHEMA_VERSION,
        "project": found.project.name,
        "ok": not collected,
        "steps": [step.describe() for step in collected],
        "stages": [{"stage": name, "why_before_the_next": why} for name, why in STAGES],
    }
    if json_out:
        typer.echo(jsonlib.dumps(payload, indent=2, default=str))
    else:
        typer.echo(render_steps(found, collected))


def register(app: typer.Typer) -> None:
    """Attach `jfast ai ...` and `jfast next` to *app*.

    A function rather than decorators on a shared `app` object so this module
    never imports `main.py`: five commands growing in one file at once is five
    merge conflicts in the same import block.
    """
    app.add_typer(ai_app, name="ai")
    app.command("next")(next_steps)
