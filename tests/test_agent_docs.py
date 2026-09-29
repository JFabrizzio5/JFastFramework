"""A document that names a file must name a file that is there.

``AGENTS.md`` and the shipped skills are read by an agent *before* it looks at
the tree, which is the whole point of them -- and the reason a wrong path in
one is worse than no path at all. An agent told that business logic lives in
``modules/invoice/service.py`` inside a hexagonal project does not discover the
mistake; it creates the file, and the module now has two service layers.

So the rule under test is not "the docs were written". It is:

1. every path the docs state exists in the tree they describe, for each of the
   four layouts;
2. no document names a file that belongs to a *different* layout -- the check
   that catches the original bug, where ``AGENTS.md`` hardcoded the layered
   tree for every project;
3. the docs still state enough paths to be worth reading, so deleting every
   path is not a way to pass.

Paths are read out of code spans and fenced blocks only. Prose is not scanned:
a sentence mentioning "the router" is not a promise about a filename, and the
tests should fail on broken promises, not on English.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jfastframework.cli import modules as module_registry
from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    MODULE_LAYOUTS,
    TEMPLATE_ROOT,
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)
from jfastframework.contracts import Contract
from jfastframework.contracts.checker import layer_matches

MODULE = "invoice"

#: Suffixes that make a token a claim about a file rather than a word.
FILE_SUFFIXES = (
    ".py",
    ".toml",
    ".md",
    ".txt",
    ".ini",
    ".cfg",
    ".json",
    ".css",
    ".js",
    ".jsx",
    ".vue",
    ".html",
    ".sh",
    ".yaml",
    ".yml",
    ".mako",
    ".example",
)

#: ``application/json`` is a media type, not a directory. So are its siblings.
MEDIA_TYPE_ROOTS = frozenset(
    {"application", "text", "image", "audio", "video", "multipart", "font"}
)

#: Placeholders the docs use for the module being worked on.
PLACEHOLDERS = {"<name>": MODULE, "<module>": MODULE, "<n>": MODULE}

FENCE = re.compile(r"^\s*```(\S*)\s*$")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
TREE_ENTRY = re.compile(r"^(?P<indent>[\s│|]*)(?:├──|└──|\|--|`--)\s+(?P<name>\S+)")


# ---------------------------------------------------------------------------
# Scaffolding the thing the docs describe
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, layout: str, *, ui: str = "api") -> Path:
    """A service with the agent surface, holding one module in ``layout``.

    The same sequence a user runs, and only that: ``jfast new service
    --agent-docs``, then ``jfast new module --layout X``. The contract comes
    with the module, so nothing here corrects it afterwards -- a fixture that
    ran ``contracts init`` would test a path the user does not take, which is
    how the scaffold shipped one contract for four layouts. The layout is
    recorded in ``jfast.toml`` because that is where a later command -- or an
    agent -- is told to look it up.
    """
    root = tmp_path / "shop"
    root.mkdir(parents=True, exist_ok=True)
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        service_trees("api", None, root, agent_docs=True),
        service_context("shop", kind="api", plugins=["database"], agent_docs=True),
    )
    scaffolder.render_trees(
        module_trees(layout, ui, root / "modules", root),
        module_context(MODULE, layout=layout, ui=ui),
    )
    module_registry.record(root, MODULE, layout=layout, ui=ui)
    return root


def _scaffold_rendered(
    tmp_path: Path, kind: str, frontend: str | None, template: str = "nexora"
) -> Path:
    """A service whose agent surface includes the design skill.

    The design skill names the stylesheet it is about, and that file is in a
    different place for a server-rendered service than for an SPA -- and for
    a nexora SPA than for a classic one. The same class of promise as a
    module's file map, so it is checked the same way.
    """
    root = tmp_path / "ui"
    scaffolder = Scaffolder()
    context = service_context(
        "ui", kind=kind, frontend=frontend, agent_docs=True, frontend_template=template
    )
    scaffolder.render_trees(
        service_trees(kind, frontend, root, agent_docs=True, frontend_template=template), context
    )
    if kind == "web":
        scaffolder.render_trees(
            module_trees("layered", "api", root / "modules", root),
            module_context(MODULE, layout="layered"),
        )
        module_registry.record(root, MODULE, layout="layered", ui="api")
    return root


def _agent_docs(root: Path) -> list[Path]:
    """Every file the agent surface ships, in the order an agent meets them."""
    docs = [root / "AGENTS.md"]
    docs.extend(sorted(root.glob(".jfast/skills/*/SKILL.md")))
    return [path for path in docs if path.is_file()]


# ---------------------------------------------------------------------------
# Pulling paths out of a document
# ---------------------------------------------------------------------------


def _resolve_placeholders(token: str) -> str | None:
    for placeholder, value in PLACEHOLDERS.items():
        token = token.replace(placeholder, value)
    # Anything still standing in for something else cannot be checked. A doc
    # that says `modules/<whatever>/` is vague, and vague is allowed.
    if "<" in token or "{" in token:
        return None
    return token


def _is_path_claim(token: str) -> bool:
    """Does this token assert that a file or directory exists?"""
    if not token or token.startswith(("-", "/", "http", "$")):
        return False
    if any(ch in token for ch in ":@?=\\"):
        return False
    if token.split("/", 1)[0] in MEDIA_TYPE_ROOTS:
        return False
    if token.endswith("/"):
        return True
    if token.endswith(FILE_SUFFIXES):
        return True
    # `modules/invoice/tests` -- a directory named without a trailing slash.
    return "/" in token and "." not in token.rsplit("/", 1)[1]


def _tokens(text: str) -> list[str]:
    return re.split(r"[\s,;]+", text.strip())


def _tree_paths(lines: list[str]) -> list[str]:
    """Reconstruct full paths from an ASCII tree block.

    Without this the tree is the least honest part of the document: every entry
    in it is a bare basename, so ``router.py`` under ``modules/<name>/`` would
    pass a naive scan by matching nothing at all.
    """
    stack: list[str] = []
    found: list[str] = []
    for line in lines:
        match = TREE_ENTRY.match(line)
        if match is None:
            continue
        depth = len(match.group("indent")) // 4
        name = match.group("name").rstrip("/")
        del stack[depth:]
        stack.append(name)
        found.append("/".join(stack))
    return found


def _stated_paths(document: Path) -> set[str]:
    """Every path the document claims exists, relative to the project root."""
    body = document.read_text(encoding="utf-8")
    claims: list[str] = []

    in_fence = False
    language = ""
    block: list[str] = []
    prose: list[str] = []
    for line in body.splitlines():
        fence = FENCE.match(line)
        if fence is not None:
            if in_fence:
                # A block with no language and tree glyphs is a tree; anything
                # else is commands, and its paths are ordinary tokens.
                if not language and any(glyph in "\n".join(block) for glyph in ("├──", "└──")):
                    claims.extend(_tree_paths(block))
                else:
                    for entry in block:
                        claims.extend(_tokens(entry))
                block = []
            in_fence = not in_fence
            language = fence.group(1)
            continue
        (block if in_fence else prose).append(line)

    for line in prose:
        for span in CODE_SPAN.findall(line):
            claims.extend(_tokens(span))

    resolved = set()
    for claim in claims:
        # Only the trailing dot goes: `.env.example` and `.jfast/skills/` start
        # with one, and stripping it turns a real path into a missing one.
        token = claim.strip("`*_\"',;()[]").rstrip(".")
        if not _is_path_claim(token):
            continue
        candidate = _resolve_placeholders(token)
        if candidate is not None:
            resolved.add(candidate.rstrip("/"))
    return resolved


def _missing(root: Path, document: Path) -> list[str]:
    """Claims with nothing behind them.

    A bare basename -- ``router.py`` in a sentence about routers -- is checked
    anywhere in the tree rather than at the root. It is still a claim: a
    hexagonal module has no ``router.py`` at any depth, which is the failure
    this whole file exists to produce.
    """
    gone = []
    for claim in sorted(_stated_paths(document)):
        if "*" in claim:
            found = bool(list(root.glob(claim)))
        elif "/" in claim:
            found = (root / claim).exists()
        else:
            found = (root / claim).exists() or bool(next(root.rglob(claim), None))
        if not found:
            gone.append(claim)
    return gone


# ---------------------------------------------------------------------------
# Which basenames belong to which layout
# ---------------------------------------------------------------------------


def _layout_basenames(layout: str) -> set[str]:
    source = TEMPLATE_ROOT / f"module_{layout}"
    names = set()
    for path in source.rglob("*.j2"):
        name = path.name.removesuffix(".j2").replace("{{module}}", MODULE)
        if name.endswith(".py"):
            names.add(name)
    return names


def _exclusive_basenames(layout: str) -> set[str]:
    """Python filenames this layout has and no other layout does.

    Derived from the templates rather than listed here, so a layout that grows
    a file does not silently stop being covered.
    """
    others: set[str] = set()
    for other in MODULE_LAYOUTS:
        if other != layout:
            others |= _layout_basenames(other)
    return _layout_basenames(layout) - others


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_every_path_the_agent_surface_states_exists(tmp_path: Path, layout: str) -> None:
    root = _scaffold(tmp_path, layout)
    docs = _agent_docs(root)
    assert docs, "--agent-docs wrote nothing"

    broken = {
        str(document.relative_to(root)): _missing(root, document)
        for document in docs
        if _missing(root, document)
    }
    assert not broken, (
        f"{layout}: the agent surface names paths that are not in the tree it describes: {broken}"
    )


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_agent_surface_never_names_another_layouts_files(tmp_path: Path, layout: str) -> None:
    """The original bug, stated as a rule.

    ``AGENTS.md`` hardcoded ``router.py`` / ``service.py`` / ``repository.py``.
    Those files do not exist in a hexagonal, modular or screaming module, so an
    agent following the document wrote into paths that were not there.
    """
    root = _scaffold(tmp_path, layout)
    foreign = {
        name for other in MODULE_LAYOUTS if other != layout for name in _exclusive_basenames(other)
    }

    trespass = {}
    for document in _agent_docs(root):
        body = document.read_text(encoding="utf-8")
        named = sorted(n for n in foreign if re.search(rf"(?<![\w/]){re.escape(n)}\b", body))
        if named:
            trespass[str(document.relative_to(root))] = named
    assert not trespass, (
        f"{layout}: the agent surface names files belonging to another layout: {trespass}"
    )


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_agent_surface_still_says_where_things_are(tmp_path: Path, layout: str) -> None:
    """Deleting every path is not a way to pass the two tests above.

    A document may be vague about a module's internals -- it is generated
    before any module exists, and a service can hold four layouts at once. It
    may not be vague about the service: the root files and the two directories
    an agent has to know about are the same whatever the layout.
    """
    root = _scaffold(tmp_path, layout)
    agents = root / "AGENTS.md"
    stated = _stated_paths(agents)

    for required in ("contracts.toml", "main.py", "shared", "modules"):
        assert required in stated, f"AGENTS.md never mentions {required}"
    assert len(stated) >= 6, f"AGENTS.md states only {sorted(stated)}"


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_agent_surface_sends_the_reader_to_the_modules_own_note(
    tmp_path: Path, layout: str
) -> None:
    """The per-module file map is per module, so the docs must point at it.

    ``AGENTS.md`` is written once, at service-scaffold time, when no module
    exists and no layout has been chosen. The only honest way for it to be
    specific about a module's files is to name the file that is written *with*
    the module and lists them.
    """
    root = _scaffold(tmp_path, layout)
    body = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "README.md" in body, "AGENTS.md never sends the reader to the module's own README"
    assert (root / "modules" / MODULE / "README.md").is_file()


@pytest.mark.parametrize("layout", MODULE_LAYOUTS)
def test_the_contract_beside_the_document_is_this_layouts_contract(
    tmp_path: Path, layout: str
) -> None:
    """`AGENTS.md` rule 1, checked against what the scaffold actually wrote.

    The document asserts that every layout forbids ``sqlalchemy`` on the layer
    answering HTTP. That was true of the four templates and false on disk: the
    scaffold wrote the layered contract whatever the module was, so in three
    layouts every layer glob named a file that did not exist and the rule
    applied to nothing -- while the check reported a pass.
    """
    root = _scaffold(tmp_path, layout)
    stamped = json.loads((root / ".jfast-template").read_text(encoding="utf-8"))["templates"]
    assert CONTRACT_TEMPLATE_FOR[layout] in stamped, sorted(stamped)

    counts = layer_matches(Contract.load(root / "contracts.toml"), root)
    empty = sorted(name for name, count in counts.items() if count == 0)
    assert not empty, f"{layout}: the contract declares layers that govern no file: {empty}"


@pytest.mark.parametrize(
    ("kind", "frontend", "template"),
    [
        ("web", None, "nexora"),
        ("spa", "vue", "nexora"),
        ("spa", "react", "nexora"),
        ("spa", "vue", "classic"),
        ("spa", "react", "classic"),
    ],
)
def test_the_design_skill_names_the_stylesheet_that_is_there(
    tmp_path: Path, kind: str, frontend: str | None, template: str
) -> None:
    root = _scaffold_rendered(tmp_path, kind, frontend, template)
    docs = _agent_docs(root)
    assert any(document.parent.name == "design-system" for document in docs)

    broken = {
        str(document.relative_to(root)): _missing(root, document)
        for document in docs
        if _missing(root, document)
    }
    assert not broken, f"{kind}: the agent surface names paths that are not there: {broken}"


def test_the_repo_skills_cover_every_layout_the_cli_offers() -> None:
    """`create-module` is where the layout is chosen. It must know them all.

    It listed two of four. An agent reading it could not have picked `modular`
    or `hexagonal`, because it was never told they exist.
    """
    skill = Path(__file__).resolve().parent.parent / ".jfast/skills/create-module/SKILL.md"
    body = skill.read_text(encoding="utf-8")
    missing = [layout for layout in MODULE_LAYOUTS if f"`{layout}`" not in body]
    assert not missing, f"create-module does not offer: {missing}"


def test_the_repo_skills_only_name_layouts_that_exist() -> None:
    """A `--layout` in any skill has to be one the CLI accepts."""
    skills = Path(__file__).resolve().parent.parent / ".jfast/skills"
    named = set()
    for path in sorted(skills.glob("*/SKILL.md")):
        named |= set(re.findall(r"--layout\s+([\w-]+)", path.read_text(encoding="utf-8")))
    assert named <= set(MODULE_LAYOUTS), (
        f"unknown layouts named in skills: {named - set(MODULE_LAYOUTS)}"
    )


@pytest.mark.parametrize(
    ("kind", "frontend", "template"),
    [("web", None, "nexora"), ("spa", "vue", "nexora"), ("spa", "react", "classic")],
)
def test_the_design_skill_lets_the_user_change_the_look(
    tmp_path: Path, kind: str, frontend: str | None, template: str
) -> None:
    """The skill used to say "stick to these tokens" and nothing else, and an
    agent read that as a reason to refuse a different look. It has to say the
    opposite, and name the way to get one."""
    root = _scaffold_rendered(tmp_path, kind, frontend, template)
    body = (root / ".jfast/skills/design-system/SKILL.md").read_text(encoding="utf-8")

    assert "The user's look wins" in body
    assert "--template" in body
    if kind == "spa":
        # It describes the look this project actually has, and only that one.
        assert f"generated with the **{template}** look" in body
        assert ("src/nexora/nexora.css" in body) is (template == "nexora")
