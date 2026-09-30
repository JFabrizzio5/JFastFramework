"""What each framework version broke, as data, and how to see it in a project.

The obvious implementation of ``jfast upgrade --check`` parses ``CHANGELOG.md``.
It is the wrong one, for three reasons that are each fatal on their own:

* prose is not a data format. "Breaking" is a heading today; the moment somebody
  writes "Breaking changes" the parser reports that nothing broke, and reports
  it confidently.
* the file does not ship. ``[tool.hatch.build.targets.wheel] packages =
  ["src/jfastframework"]`` puts the package in the wheel and nothing else, so an
  installed framework has no changelog to read.
* it answers the wrong question. A changelog says what changed; the person
  upgrading needs to know what changes **to their project**, and a list of
  twenty release notes with no way to tell which three apply is a list nobody
  reads twice.

So the breaking changes are declared here, in the package, each with a
``detect`` that looks at the project on disk and returns the evidence -- the
tables that need the migration, the layers missing a permission, the settings
about to acquire a default. A change whose ``detect`` returns nothing is not
reported at all. That rule is the whole value of the command: one warning that
does not apply teaches people to skip the output, and the next one is skipped
with it.

``detect`` is ``None`` only where nothing on disk can decide the question. Then
the entry is informational and says so.

Version comparison is PEP 440-aware and **vendored**, in :func:`parse_version`.
``packaging`` is not a dependency of this framework -- neither a direct one nor
a transitive one of fastapi, pydantic, pydantic-settings, typer or jinja2 -- so
importing it would work on a development machine and fail on a plain
``pip install jfastframework``. The vendored key follows ``packaging``'s own
ordering, and ``tests/test_cli_upgrade.py`` pins that agreement against the real
implementation, which *is* installed for development.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jfastframework.contracts.model import match_path
from jfastframework.project import SKIP_DIRS, Project
from jfastframework.settings import (
    UPLOAD_MAX_BODY_BYTES,
    UPLOAD_PLUGIN,
    UPLOAD_REQUEST_TIMEOUT,
    JFastSettings,
    raises_request_limits,
)

__all__ = [
    "CHANGES",
    "Applicable",
    "Change",
    "Pin",
    "applicable",
    "parse_version",
    "pinned_version",
]

KINDS = ("breaking", "deprecated", "behaviour")


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VERSION = re.compile(
    r"""^\s*v?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>\d*))?
    (?:[-_.]?post[-_.]?(?P<post>\d*))?
    (?:[-_.]?dev[-_.]?(?P<dev>\d*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

# Sorts after every pre-release of the same version, and after every dev.
_FINAL = (3, 0)
# Sorts before every pre-release: `1.0.dev1` precedes `1.0a1`.
_BEFORE_ANY_PRE = (-1, 0)
_NO_DEV = 1 << 62

_SortKey = tuple[tuple[int, ...], tuple[int, int], int, int]


def parse_version(text: str) -> _SortKey:
    """A sortable key for *text*, ordered as PEP 440 orders versions.

    String comparison is what this exists to avoid: it reads ``0.1.0a10`` as
    older than ``0.1.0a9`` and quietly reports that a project is up to date.

    Epochs and local versions are not accepted. This framework has never
    published one, and a key that silently ignores half of what it was given is
    worse than one that refuses it.
    """
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"not a PEP 440 version: {text!r}")

    numbers = [int(part) for part in match.group("release").split(".")]
    # 1.0 and 1.0.0 are the same version, so trailing zeros cannot count.
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    release = tuple(numbers)

    pre_letter = match.group("pre_l")
    post = match.group("post")
    dev = match.group("dev")

    if pre_letter is not None:
        pre = (_PRE_RANK[pre_letter.lower()], int(match.group("pre_n") or 0))
    elif post is None and dev is not None:
        pre = _BEFORE_ANY_PRE
    else:
        pre = _FINAL

    return (
        release,
        pre,
        -1 if post is None else int(post or 0),
        _NO_DEV if dev is None else int(dev or 0),
    )


# ---------------------------------------------------------------------------
# What the project pins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """The framework version a project asks for, and where that was written."""

    version: str
    source: str


STAMP_FILE = ".jfast-template"

_REQUIREMENT = re.compile(
    r"^\s*jfastframework(?:\[[^\]]*\])?\s*(?:==|~=|>=|<=|=)\s*(?P<version>[^\s,;#]+)",
    re.IGNORECASE | re.MULTILINE,
)


def pinned_version(root: Path) -> Pin | None:
    """The version this project runs on, or `None` when nothing says.

    ``requirements.txt`` wins over the scaffold stamp because it is the one pip
    acts on: a project upgraded by editing that line and reinstalling is on the
    new version whatever the stamp still remembers about the day it was
    generated.
    """
    requirements = root / "requirements.txt"
    if requirements.is_file():
        try:
            text = requirements.read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = _REQUIREMENT.search(text)
        if match:
            return Pin(match.group("version"), "requirements.txt")

    stamp = root / STAMP_FILE
    if stamp.is_file():
        versions = _stamped_versions(stamp)
        if versions:
            return Pin(max(versions, key=parse_version), STAMP_FILE)
    return None


def _stamped_versions(stamp: Path) -> list[str]:
    try:
        loaded = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, dict):
        return []
    templates = loaded.get("templates")
    if not isinstance(templates, dict):
        return []
    found = []
    for entry in templates.values():
        if isinstance(entry, dict):
            version = entry.get("framework_version")
            # A stamp written by a future version can carry anything; a
            # template whose version does not parse is skipped, not fatal.
            if isinstance(version, str) and _parses(version):
                found.append(version)
    return found


def _parses(version: str) -> bool:
    try:
        parse_version(version)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Reading the project
# ---------------------------------------------------------------------------


def _config(project: Project) -> dict[str, Any]:
    """``jfast.toml`` as a plain mapping.

    Read here rather than taken from `Project`, which keeps the plugin *names*
    and drops their settings -- and the settings are exactly what decides
    whether two of the changes below apply.
    """
    path = project.root / "jfast.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            loaded: dict[str, Any] = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded


def _table(config: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = config
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def _active_plugins(project: Project) -> frozenset[str]:
    """The plugins the registry will load for this project, not the list it wrote.

    ``[plugins].enabled`` is an allow-list with two rules the literal list does
    not show: an empty one loads every ``default_enabled`` plugin (metrics
    among them), and a plugin another one ``requires`` is loaded without being
    named (``ratelimit`` pulls in ``cache``). ``disabled`` wins over both. A
    detector reading the list itself reports the service somebody meant to
    write, not the one that runs.
    """
    return _resolved_plugins(tuple(project.plugins), tuple(project.disabled))


@functools.lru_cache(maxsize=64)
def _resolved_plugins(enabled: tuple[str, ...], disabled: tuple[str, ...]) -> frozenset[str]:
    from jfastframework.plugins import registry

    try:
        chosen = registry.select(
            _installed_plugins(), enabled=list(enabled), disabled=list(disabled)
        )
    except Exception:  # noqa: BLE001 -- a graph that will not resolve is `jfast check`'s report
        # The list as written, minus what it disables: the best statement of
        # intent available when a named plugin is not installed here.
        return frozenset(name for name in enabled if name not in disabled)
    return frozenset(cls.meta.name for cls in chosen)


@functools.lru_cache(maxsize=1)
def _installed_plugins() -> dict[str, Any]:
    from jfastframework.plugins import registry

    return dict(registry.discover())


def _issues_tokens(project: Project) -> bool:
    """Whether this service is the one minting tokens, not just verifying them.

    A service that only validates somebody else's tokens is untouched by every
    change to the issuing endpoints, which is most services with `auth` on.
    """
    if "auth" not in _active_plugins(project):
        return False
    return bool(_table(_config(project), "plugin", "auth").get("issue_tokens", False))


def _skipped(path: Path, root: Path) -> bool:
    """Whether *path* sits in a directory this report never reads.

    Tested on the parts *below the project root*, not on the absolute path: a
    checkout at `~/build/billing` or `/srv/site/billing` is a project, and
    testing the absolute parts skipped every file in it -- so every note that
    reads source reported a clean project.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part in SKIP_DIRS for part in parts)


def _files(root: Path, *suffixes: str) -> list[Path]:
    """Every file under *root* ending in one of *suffixes*, SKIP_DIRS pruned.

    Pruned while walking rather than filtered afterwards: a `.venv` inside the
    project holds tens of thousands of files, and `rglob` reads every one of
    them before a filter can throw them away.
    """
    found: list[Path] = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SKIP_DIRS]
        found += [Path(directory, name) for name in names if name.endswith(suffixes)]
    return sorted(found)


def _python_files(root: Path) -> list[Path]:
    return _files(root, ".py")


def _parsed_files(root: Path) -> list[tuple[str, ast.Module]]:
    """Every Python file that parses, with its path relative to the project.

    A file that does not parse is skipped rather than fatal: this report is
    wanted most on a project part-way through an upgrade, which is exactly
    when one module is half-edited.
    """
    found: list[tuple[str, ast.Module]] = []
    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        found.append((path.relative_to(root).as_posix(), tree))
    return found


def _classes(files: list[tuple[str, ast.Module]]) -> list[tuple[str, ast.ClassDef]]:
    return [
        (where, node)
        for where, tree in files
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    ]


def _assignments(statement: ast.stmt) -> Iterator[tuple[ast.expr, ast.expr]]:
    """`(target, value)` for each name this statement binds, if it binds any.

    `ast.AnnAssign` is included because SQLAlchemy 2.0 style annotates the rest
    of the class body, and `__tablename__: str = "..."` is what that habit
    produces; a model written that way is invisible to a parser reading only
    `ast.Assign`, and the table it declares is then missing from the migration
    this report exists to hand over.
    """
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            yield target, statement.value
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        # `__tablename__: str` with no value declares nothing.
        yield statement.target, statement.value


def _class_attribute(node: ast.ClassDef, name: str) -> Any:
    """The constant *name* assigned in this class's own body, or None.

    Its own body, not `ast.walk`: an attribute of a nested class belongs to
    that class, and a base class's belongs to the base.
    """
    for statement in node.body:
        for target, value in _assignments(statement):
            if (
                isinstance(target, ast.Name)
                and target.id == name
                and isinstance(value, ast.Constant)
            ):
                return value.value
    return None


TIMESTAMP_MIXIN = "TimestampMixin"


def _base_names(node: ast.ClassDef) -> list[str]:
    """The bases of *node*, by their last name segment.

    `TimestampMixin` and `db.TimestampMixin` are the same base, and which
    module either was reached through is not decidable without importing the
    project -- which this report never does.
    """
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _carries_mixin(node: ast.ClassDef, bases: dict[str, list[str]]) -> bool:
    """Whether `TimestampMixin` is anywhere in this class's base closure.

    *bases* is keyed by class name across the whole project, so a model that
    reaches the mixin through a base declared in `shared/` is found too. Two
    classes of the same name in different files are indistinguishable here --
    which costs a table listed twice, against a table left out entirely.
    """
    seen: set[str] = set()
    queue = _base_names(node)
    while queue:
        name = queue.pop()
        if name == TIMESTAMP_MIXIN:
            return True
        if name in seen:
            continue
        seen.add(name)
        queue.extend(bases.get(name, ()))
    return False


def _timestamp_migration(project: Project) -> list[str]:
    """One `ALTER TABLE` per model class that carries `TimestampMixin`.

    Resolved per class, not per file. A models file routinely holds both the
    tables that mix the timestamps in and projection or view tables that do
    not, and an `ALTER` naming `created_at` on a table without one aborts the
    revision -- after every statement before it has already taken ACCESS
    EXCLUSIVE and rewritten its own table.
    """
    files = _parsed_files(project.root)
    classes = _classes(files)
    bases = {node.name: _base_names(node) for _, node in classes}

    affected: list[str] = []
    for where, node in classes:
        if not _carries_mixin(node, bases):
            continue
        # `__abstract__` declares a base that owns no table, so there is
        # nothing to alter and nothing missing either.
        if _class_attribute(node, "__abstract__") is True:
            continue
        table = _class_attribute(node, "__tablename__")
        if not isinstance(table, str):
            affected.append(
                f"{where}: {node.name} carries TimestampMixin, declares no __tablename__"
            )
            continue
        affected.append(
            f"{where}  ->  {table}\n"
            f"ALTER TABLE {table}\n"
            f"    ALTER COLUMN created_at TYPE timestamptz "
            f"USING created_at AT TIME ZONE 'UTC',\n"
            f"    ALTER COLUMN updated_at TYPE timestamptz "
            f"USING updated_at AT TIME ZONE 'UTC';"
        )
    return affected


def _layers_without_shared(project: Project) -> list[str]:
    """Layers whose `may_import` never names `shared`.

    Parsed with `tomllib` rather than through `contracts.Contract`: that loader
    reads the whole file including rule tables this question does not need, and
    a project with one malformed rule would get no report at all instead of the
    report it came for.
    """
    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    layers = raw.get("layers")
    if not isinstance(layers, dict):
        return []

    offenders = []
    for name, block in layers.items():
        # `shared` is the one layer that must not import a layer: it is a leaf,
        # and the moment it can import upward the graph is a circle.
        if name == "shared" or not isinstance(block, dict):
            continue
        allowed = block.get("may_import", [])
        if not isinstance(allowed, list) or "shared" not in allowed:
            current = ", ".join(f'"{item}"' for item in allowed) if allowed else ""
            offenders.append(f'[layers.{name}]  may_import = [{current}]  ->  add "shared"')
    return offenders


# The raised pair, taken from the kernel rather than copied: the plain pair is
# the field default and the raised one is what `storage` resolves to, so the
# report cannot quote a number the running service disagrees with. That drift
# is what this entry was reporting before -- 25 MiB from a rule only the
# scaffold applied, against the 2 MiB every other service actually got.
_RAISED_LIMITS: dict[str, int | float] = {
    "max_body_bytes": UPLOAD_MAX_BODY_BYTES,
    "request_timeout": UPLOAD_REQUEST_TIMEOUT,
}


def _request_limit_defaults(project: Project) -> list[str]:
    """Only the limits this project has not set for itself."""
    app = _table(_config(project), "app")
    uploads = raises_request_limits(project.plugins, project.disabled)
    affected = []
    for field, raised in _RAISED_LIMITS.items():
        if field in app:
            continue
        value = raised if uploads else JFastSettings.model_fields[field].default
        note = f"  (this service enables {UPLOAD_PLUGIN})" if uploads else ""
        affected.append(f"{field} = {value}{note}")
    return affected


_PAGINATORS = frozenset({"paginate", "paginate_keyset"})


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _imports_jfast_db(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jfastframework.db"):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("jfastframework.db") for alias in node.names
        ):
            return True
    return False


def _pagination_call_sites(project: Project) -> list[str]:
    """Every call whose `Page.total` can now come back `None`.

    Gated on the project importing `jfastframework.db` somewhere: `paginate`
    is a common enough method name that the calls on their own would report
    projects that have never held a `Page`.
    """
    files = _parsed_files(project.root)
    if not any(_imports_jfast_db(tree) for _, tree in files):
        return []

    affected = []
    for where, tree in files:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node.func)
            if name in _PAGINATORS:
                affected.append(f"{where}:{node.lineno}  ->  {name}(...)")
    return affected


_SESSION_DEPENDENCIES = frozenset(
    {"session_dependency", "read_session_dependency", "tenant_session_dependency"}
)


def _sessions_committing_after_the_response(project: Project) -> list[str]:
    """Every ``Depends(<session dependency>)`` without ``scope="function"``.

    Those commit after the response is sent, and ``0.1.0a9``'s database
    plugin refuses to start while a route has one. ``DbSession`` and its
    siblings carry the scope already, so they never appear here.
    """
    affected = []
    for where, tree in _parsed_files(project.root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node.func) != "Depends":
                continue
            if not node.args:
                continue
            target = _called_name(node.args[0])
            if target not in _SESSION_DEPENDENCIES:
                continue
            scope = next((k.value for k in node.keywords if k.arg == "scope"), None)
            if isinstance(scope, ast.Constant) and scope.value == "function":
                continue
            affected.append(f"{where}:{node.lineno}  ->  Depends({target})")
    return affected


def _env_drops_framework_tables(project: Project) -> list[str]:
    """A migrations/env.py generated before it learned to skip ``jfast_*`` tables.

    Such an env.py lets ``alembic revision --autogenerate`` propose dropping the
    queue's table and every other table a plugin creates, because none of them
    is among the service's models.
    """
    affected = []
    for env in sorted(project.root.rglob("migrations/env.py")):
        if _skipped(env, project.root):
            continue
        try:
            source = env.read_text(encoding="utf-8")
        except OSError:
            continue
        if "include_name" not in source:
            affected.append(env.relative_to(project.root).as_posix())
    return affected


def _token_store_implementations(project: Project) -> list[str]:
    """Classes of this project's own that implement `rotate_refresh`.

    A project that only uses the shipped stores is unaffected: the framework
    calls its own implementations and reads the new return value correctly.
    The break is for whoever wrote the protocol themselves.
    """
    affected = []
    for where, node in _classes(_parsed_files(project.root)):
        for statement in node.body:
            if (
                isinstance(statement, ast.AsyncFunctionDef | ast.FunctionDef)
                and statement.name == "rotate_refresh"
                # Already migrated: the grace keyword is the half of the new
                # protocol a signature shows. A store fixed before the pin was
                # bumped is not told to do what it has done.
                and not _takes_argument(statement, "grace")
            ):
                affected.append(f"{where}:{statement.lineno}  ->  {node.name}.rotate_refresh")
    return affected


def _takes_argument(function: ast.AsyncFunctionDef | ast.FunctionDef, name: str) -> bool:
    arguments = function.args
    return any(
        argument.arg == name
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    )


def _refresh_grace_unset(project: Project) -> list[str]:
    """Token issuers that have not chosen a grace window, so they get the default."""
    if not _issues_tokens(project):
        return []
    if "refresh_grace_seconds" in _table(_config(project), "plugin", "auth"):
        return []
    return ["[plugin.auth] refresh_grace_seconds is not set, so the window is on"]


_TEMPLATES = Path(__file__).parent / "templates"


def _layout_files(layout: str, module: str) -> list[str]:
    """Every Python file `jfast new module --layout <layout>` writes for *module*.

    Read off the templates rather than listed here, so this cannot drift from
    what the generator produces -- and so a layout added later needs no edit.
    A hand-written stand-in is what lets a checker and its fixture agree with
    each other and disagree with the product.
    """
    root = _TEMPLATES / f"module_{layout}"
    if not root.is_dir():
        return []
    return [
        "modules/"
        + path.relative_to(root).as_posix().removesuffix(".j2").replace("{{module}}", module)
        for path in sorted(root.rglob("*.py.j2"))
    ]


def _recorded_layouts(project: Project) -> dict[str, str]:
    """`[modules.<name>].layout` for every module that recorded one."""
    modules = _table(_config(project), "modules")
    found = {}
    for name, entry in modules.items():
        if isinstance(entry, dict) and isinstance(entry.get("layout"), str):
            found[name] = entry["layout"]
    return found


def _contract_layers(root: Path) -> dict[str, list[str]]:
    """`[layers.*] paths` from `contracts.toml`, by layer name.

    Parsed with `tomllib` rather than through `contracts.Contract`, for the
    reason `_layers_without_shared` documents: the real loader reads rule
    tables this question does not need, and one malformed rule would replace
    the report with nothing.
    """
    path = root / "contracts.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    layers = raw.get("layers")
    if not isinstance(layers, dict):
        return {}

    found = {}
    for name, block in layers.items():
        if not isinstance(block, dict):
            continue
        paths = block.get("paths", [])
        found[name] = [item for item in paths if isinstance(item, str)] if paths else []
    return found


def _contracts_layout_mismatch(project: Project) -> list[str]:
    """Layers whose `paths` describe a layout none of this project's modules use.

    `shared` is left out: it governs `shared/`, not a module, so it says
    nothing about which layout the modules are in.
    """
    layouts = _recorded_layouts(project)
    layers = _contract_layers(project.root)
    if not layouts or not layers:
        return []

    files: list[str] = []
    known: set[str] = set()
    for module, layout in sorted(layouts.items()):
        written = _layout_files(layout, module)
        if written:
            known.add(layout)
            files.extend(written)
    if not files:
        return []

    in_use = ", ".join(sorted(known))
    return [
        f"[layers.{name}]  paths = {paths}  ->  matches no {in_use} module"
        for name, paths in sorted(layers.items())
        if name != "shared"
        # `contracts.Contract.layer_for`'s matcher, not `fnmatch`, and the test
        # named after this line is why: the two have to agree on which layers
        # `contracts check` will reject, and `fnmatch`'s `*` crosses a `/` while
        # the checker's no longer does.
        and not any(match_path(file, pattern) for pattern in paths for file in files)
    ]


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One thing a version broke, and how to see it in a project.

    `detect` returns the evidence found in *this* project -- table names, layer
    names, the values a setting is about to acquire. An empty list means the
    project cannot be affected and the change is not reported.
    """

    version: str
    kind: str
    code: str
    summary: str
    detail: str
    detect: Callable[[Project], list[str]] | None
    remedy: str

    def describe(self, affected: list[str]) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "code": self.code,
            "summary": self.summary,
            "detail": self.detail,
            "remedy": self.remedy,
            "affected": affected,
        }


def _globs_that_narrowed(project: Project) -> list[str]:
    """Files a layer used to claim under the old glob and does not claim now.

    Layer globs went through `fnmatch` until 0.1.0a5, which translates `*` to
    `.*` -- so `modules/*/repository.py` reached
    `modules/billing/infrastructure/repository.py`, arbitrarily deep. `*` stops
    at `/` now.

    The answer has to be computed against this project's own files, not read
    off the patterns: whether a pattern narrowed is a fact about the tree it
    runs on. A contract full of `**` is unaffected and gets no report.
    """
    from fnmatch import fnmatch

    from jfastframework.contracts.model import match_path

    layers = _contract_layers(project.root)
    if not layers:
        return []

    lost: list[str] = []
    for path in project.root.rglob("*.py"):
        if not path.is_file():
            continue
        relative = path.relative_to(project.root).as_posix()
        if any(
            part in {".venv", "__pycache__", ".git"}
            for part in path.relative_to(project.root).parts
        ):
            continue
        # The layer each matcher *resolves* the file to, not each layer on its
        # own: a screaming contract's catch-all also matched `use_cases/*.py`
        # under fnmatch, but the more specific use_cases layer won then and wins
        # now, so nothing changed hands.
        was = _resolve_layer(layers, relative, fnmatch)
        now = _resolve_layer(layers, relative, match_path)
        if was is not None and was != now:
            now_text = "no layer" if now is None else f"layer {now!r}"
            lost.append(f"{relative} (was layer {was!r})  ->  {now_text}")
    return sorted(lost)


def _resolve_layer(
    layers: dict[str, list[str]], relative: str, matches: Callable[[str, str], bool]
) -> str | None:
    """The layer `Contract.layer_for` would pick for *relative* under *matches*.

    Its specificity rule, restated so it can run under the old matcher too:
    fewest wildcards, then the longer pattern, then the layer name.
    """
    best: tuple[int, int, str] | None = None
    for name, patterns in layers.items():
        for pattern in patterns:
            if not matches(relative, pattern):
                continue
            candidate = (-sum(pattern.count(char) for char in "*?["), len(pattern), name)
            if best is None or candidate > best:
                best = candidate
    return best[2] if best else None


def _naive_datetimes(project: Project) -> list[str]:
    """Calls the new `naive-datetime` rule will now reject.

    The rule rides `[rules.async_safety]`, which existing contracts already
    have on, so it arrives enabled without anyone opting in. A build that
    passed yesterday fails today, and the failure names a rule the project has
    never seen.
    """
    try:
        from jfastframework.contracts import Contract, check_blocking
        from jfastframework.contracts.blocking import NAIVE_RULE
    except ImportError:  # pragma: no cover -- contracts is not optional
        return []

    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        contract = Contract.load(path)
    except Exception:  # noqa: BLE001 -- a broken contract is its own report
        return []
    try:
        findings = check_blocking(contract, project.root)
    except Exception:  # noqa: BLE001
        return []
    # `f.rule`, not `getattr(f, "code", "")`: a default turns a renamed
    # field into an empty report, which reads exactly like a clean project.
    return sorted(f"{f.path}:{f.line}" for f in findings if f.rule == NAIVE_RULE)


def _session_store_missing(project: Project) -> list[str]:
    """A service that mints tokens with nothing shared to record them.

    `0.1.0a8` refuses to register this in production, which is the point: the
    store is per process, the image runs one worker per CPU, and both logout
    and refresh-reuse detection were per worker with it. The refusal lands at
    boot, and a boot is the worst place to learn it -- so it is reported here,
    on a laptop, before the deploy that would have failed.
    """
    if not _issues_tokens(project):
        return []
    # Resolved, not read off the list: `ratelimit` requires `cache`, and the
    # registry loads it unasked -- so auth finds `cache.client` and never refuses.
    if "cache" in _active_plugins(project):
        return []
    return ['[plugins] enabled has "auth" with issue_tokens = true and no "cache"']


def _silent_mail_backend(project: Project) -> list[str]:
    """A mail backend that accepts every message and delivers none.

    `console` is the default and the right default -- nobody emails a real
    customer from a laptop. In production it prints to stdout while `send`
    reports success, so `0.1.0a8` refuses it there.
    """
    if "mail" not in project.plugins:
        return []
    backend = _table(_config(project), "plugin", "mail").get("backend", "console")
    if backend not in ("console", "memory"):
        return []
    return [f'[plugin.mail] backend = "{backend}"']


def _rag_settings(project: Project) -> dict[str, Any] | None:
    if "rag" not in project.plugins:
        return None
    return _table(_config(project), "plugin", "rag")


def _rag_without_tenant_scope(project: Project) -> list[str]:
    """A rag service that will now refuse calls without a tenant.

    Before 0.1.0a10 ``tenant_id=None`` searched every tenant's chunks. It is a
    ``TenantRequiredError`` now unless the service says it has one tenant.
    """
    rag = _rag_settings(project)
    if rag is None or rag.get("tenant_scoped") is False:
        return []
    return ["[plugin.rag] tenant_scoped defaults to true"]


def _rag_router_default(project: Project) -> list[str]:
    """A rag service whose router moved: gone by default, or behind auth when kept.

    Only an explicit ``false`` means nothing is left to do. ``true`` was legal in
    0.1.0a9 without auth; 0.1.0a10 refuses to start it that way, and with auth
    the router wants a signed-in caller and ignores the tenant in the body.
    """
    rag = _rag_settings(project)
    if rag is None or rag.get("mount_router") is False:
        return []
    if "mount_router" not in rag:
        return ["[plugin.rag] mount_router now defaults to false (POST /rag/search is gone)"]
    auth = "" if "auth" in _active_plugins(project) else ", and auth is not enabled"
    return [
        "[plugin.rag] mount_router = true: the router now needs a signed-in caller and takes "
        f"the tenant from the token{auth}"
    ]


def _rag_on_qdrant(project: Project) -> list[str]:
    """Qdrant point ids now include the tenant; a 0.1.0a9 collection must be re-ingested."""
    rag = _rag_settings(project)
    if rag is None or rag.get("store") != "qdrant":
        return []
    return [f'[plugin.rag] store = "qdrant", collection = "{rag.get("collection", "rag_chunks")}"']


def _rag_chunking_default(project: Project) -> list[str]:
    rag = _rag_settings(project)
    if rag is None or "chunk_strategy" in rag:
        return []
    return ['[plugin.rag] chunk_strategy defaults to "recursive"']


_NEW_BOUNDARY_RULES = frozenset(
    {
        "undeclared-dependency",
        "module-cycle",
        "public-leak",
        "cross-module-sql",
        "unknown-dependency",
    }
)


def _module_boundary_violations(project: Project) -> list[str]:
    """What the new module-boundary rules report in this project today.

    Run for real rather than guessed: the rules read the source, and an
    upgrade note that lists the actual lines is one that gets acted on.
    """
    from jfastframework.contracts.model import Contract
    from jfastframework.contracts.placement import check_placement

    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        contract = Contract.load(path)
    except Exception:  # noqa: BLE001 - an unreadable contract is `contracts check`'s to report
        return []
    return [
        f"{v.path}:{v.line} {v.rule}: {v.message}"
        for v in check_placement(contract, project.root)
        if v.rule in _NEW_BOUNDARY_RULES
        or (v.rule == _CROSS_MODULE and _widened_cross_module(project.root, v.path, v.line))
    ][:20]


_CROSS_MODULE = "cross-module"


def _widened_cross_module(root: Path, relative: str, line: int) -> bool:
    """A `cross-module` finding on a spelling the rule only catches since 0.1.0a10.

    Relative imports across modules, and the second and later names in
    `import a, b`. An absolute `from modules.x import y` failed 0.1.0a9's check
    as well, so it is not news to the project -- but these passed yesterday.
    """
    try:
        text = (root / relative).read_text(encoding="utf-8").splitlines()[line - 1].strip()
    except (OSError, IndexError):
        return False
    return text.startswith("from .") or (text.startswith("import ") and "," in text)


def _screaming_contract_without_public_layer(project: Project) -> list[str]:
    """A screaming contract from 0.1.0a9 classifies public.py as domain."""
    from jfastframework.contracts.model import Contract

    path = project.root / "contracts.toml"
    if not path.is_file() or not any(m.layout == "screaming" for m in project.modules):
        return []
    try:
        contract = Contract.load(path)
    except Exception:  # noqa: BLE001
        return []
    return [] if "public" in contract.layers else ["contracts.toml has no [layers.public]"]


_NOW_ASYNC = frozenset({"require_auth", "optional_auth", "current_tenant", "tenant_zone"})


def _direct_calls_to_async_dependencies(project: Project) -> list[str]:
    """Plain calls to a dependency that is a coroutine function since 0.1.0a10.

    ``Depends(require_auth)`` passes the function and is unaffected. A call --
    ``require_auth(request)`` in a service factory -- now returns a coroutine,
    and the first attribute read on it fails.
    """
    from jfastframework.contracts._scan import call_name, import_aliases, resolve

    found = []
    for relative, tree in _parsed_files(project.root):
        awaited = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        }
        aliases = import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in awaited:
                continue
            # Resolved through the file's imports: `current_tenant` is a name any
            # multi-tenant codebase may already have, and a helper of its own
            # never became a coroutine.
            written = call_name(node)
            origin = resolve(written, aliases) if written else None
            if origin is None or not origin.startswith("jfastframework."):
                continue
            name = origin.rsplit(".", 1)[-1]
            if name in _NOW_ASYNC:
                found.append(f"{relative}:{node.lineno} {name}(...)")
    return found


def _sync_request_factories(project: Project) -> list[str]:
    """``def get_service(request, session: DbSession)``: a threadpool hop per request.

    It still works. It is the single largest per-request cost the 0.1.0a10
    benchmark found -- about 80 us, more than every middleware together --
    because FastAPI runs a plain ``def`` dependency in its threadpool.
    """
    found = []
    for relative, tree in _parsed_files(project.root):
        if not relative.startswith("modules/"):
            continue
        # The module's own body: a dependency is a module-level function. A
        # `get_service` method is ordinary Python that FastAPI never calls, and
        # making it `async` -- the remedy -- breaks every caller.
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("get_service", "get_use_cases"):
                found.append(f"{relative}:{node.lineno} def {node.name}")
    return found


def _metrics_enabled(project: Project) -> list[str]:
    """Metrics as the registry resolves it: `disabled` wins, an empty list loads it."""
    if "metrics" not in _active_plugins(project):
        return []
    if "metrics" in project.plugins:
        return ["[plugins] enabled includes metrics"]
    if not project.plugins:
        return ["[plugins] enabled is empty, so metrics loads by default"]
    return ["metrics loads as a dependency of another enabled plugin"]


def _job_timeout_past_the_claim(project: Project) -> list[str]:
    """A worker allowed to run a handler past the claim that protects it.

    The claim is invisible to other workers for ``visibility_timeout`` and
    nothing extends it, so a handler that outlives the window is claimed again
    while the first run is still inside it. ``Worker`` refuses that pairing
    now, and the two numbers live in different files -- one in ``jfast.toml``,
    one at the call site -- which is why nobody had compared them.
    """
    visibility = _table(_config(project), "plugin", "queue").get("visibility_timeout", 300)
    if not isinstance(visibility, int | float):
        return []

    found: list[str] = []
    for name, tree in _parsed_files(project.root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node.func) != "Worker":
                continue
            for keyword in node.keywords:
                if keyword.arg != "job_timeout":
                    continue
                value = keyword.value
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, int | float)
                    and value.value >= visibility
                ):
                    found.append(
                        f"{name}:{value.lineno}: job_timeout={value.value:g}s "
                        f"against visibility_timeout={visibility:g}s"
                    )
    return sorted(found)


def _stale_deploy_artifacts(project: Project) -> list[str]:
    """Generated deployment files this project has that cannot bring it up.

    Read as text rather than parsed: these files are generated, their shape is
    known, and a project that has since hand-edited one is exactly the project
    that must not have its compose file silently declared fine by a parser
    tolerant enough to miss the edit.

    Only files that already exist are reported. A project with no compose file
    has nothing stale -- the next `jfast deploy compose` writes the current one.
    """
    findings: list[str] = []

    composes = sorted(
        path
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose.y*ml")
        for path in project.root.glob(pattern)
        if path.is_file()
    )
    if not composes:
        return []

    # Every datastore plugin declares the variable its client reads. Absent from
    # the compose file, the container falls back to the .env, which holds the
    # host's addresses.
    expected = _client_env_vars(project)

    for compose in composes:
        try:
            body = compose.read_text(encoding="utf-8")
        except OSError:
            continue
        name = compose.name
        if "build:" in body and not (project.root / "Dockerfile").is_file():
            findings.append(f"{name}: builds an image, and there is no Dockerfile to build")
        missing = sorted(var for var in expected if var not in body)
        if missing:
            findings.append(f"{name}: no internal address for {', '.join(missing)}")

    return findings


def _client_env_vars(project: Project) -> set[str]:
    """The variables this project's enabled plugins publish to their clients.

    Built from the plugins themselves rather than a list kept here: a plugin
    that gains a container later gains this check with it, and one that never
    had a single address -- storage -- contributes nothing, which is correct.
    """
    from jfastframework.plugins import registry
    from jfastframework.settings import JFastConfig

    try:
        config = JFastConfig.load(config_path=str(project.root / "jfast.toml"))
        instances = registry.build(config)
    except Exception:  # noqa: BLE001 -- a config that will not load is its own report
        return set()

    variables: set[str] = set()
    for plugin in instances:
        try:
            for infra in plugin.infra(None):
                variables.update(infra.client_env)
        except Exception:  # noqa: BLE001 -- generation-time failures belong to `deploy`
            continue
    return variables


# ---------------------------------------------------------------------------
# 0.1.0a11
# ---------------------------------------------------------------------------


def _eager_adapter_imports(project: Project) -> list[str]:
    """A module package init that imports its HTTP adapter at import time.

    0.1.0a10's hexagonal template bound ``CreatePayload`` with a plain
    ``from .adapters.http import X as CreatePayload`` at the top of
    ``modules/<name>/__init__.py``. Python runs that file on the way to
    ``modules.<name>.domain``, so importing the domain loaded FastAPI, Pydantic
    and SQLAlchemy -- and the module's own domain test, which checks exactly
    that, fails. Only the module's own body counts: the same import under
    ``if TYPE_CHECKING:`` or inside ``__getattr__`` is the fix, not the bug.
    """
    modules = project.root / "modules"
    if not modules.is_dir():
        return []
    found = []
    for init in sorted(modules.glob("*/__init__.py")):
        try:
            tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
        except (OSError, SyntaxError, ValueError):
            continue
        relative = init.relative_to(project.root).as_posix()
        for node in tree.body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 1
                and (node.module or "").split(".", 1)[0] == "adapters"
            ):
                names = ", ".join(
                    f"{alias.name} as {alias.asname}" if alias.asname else alias.name
                    for alias in node.names
                )
                found.append(f"{relative}:{node.lineno} from .{node.module} import {names}")
    return found


_TASK_FILES = ("modules/*/tasks.py", "modules/*/tasks/*.py")


def _tasks_without_a_tasks_layer(project: Project) -> list[str]:
    """Module ``tasks.py`` files the project's contract gives to no tasks layer.

    Contracts generated before 0.1.0a11 have no ``[layers.tasks]``. Under the
    screaming contract its catch-all claims ``tasks.py`` as domain, so a task
    that calls the use cases -- what one is for -- is a layer violation; under
    the others the file matches no layer and no layer rule applies to it at
    all. Reported only where a tasks file exists: a contract with nothing to
    misclassify has nothing to fix yet.
    """
    from jfastframework.contracts.model import Contract

    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        contract = Contract.load(path)
    except Exception:  # noqa: BLE001 -- an unreadable contract is `contracts check`'s to report
        return []

    found = []
    files = sorted(
        {
            file.relative_to(project.root).as_posix()
            for pattern in _TASK_FILES
            for file in project.root.glob(pattern)
            if file.is_file() and not _skipped(file, project.root)
        }
    )
    for relative in files:
        layer = contract.layer_for(relative)
        if layer is not None and (
            layer.name == "tasks" or any("tasks" in pattern for pattern in layer.paths)
        ):
            continue
        found.append(
            f"{relative}  ->  "
            + (f"layer {layer.name!r}" if layer is not None else "no layer, so no layer rule")
        )
    return found


def _event_and_task_wiring(project: Project) -> list[str]:
    """What the event and task-name rules report in this project today.

    Run for real, as `module-boundaries` is: orphan-subscription,
    undeclared-event and unused-dependency are new, and undeclared-dependency
    now also covers a job queued by name for a task another module owns --
    ``Job(task="alerta.revisar")`` from ``comprobante`` is a call into
    ``alerta`` spelt so no import-based check could see it.
    """
    from jfastframework.contracts.model import Contract
    from jfastframework.contracts.placement import UNDECLARED_RULE, check_placement
    from jfastframework.contracts.wiring import (
        ORPHAN_RULE,
        UNDECLARED_EVENT_RULE,
        UNUSED_RULE,
    )

    path = project.root / "contracts.toml"
    if not path.is_file():
        return []
    try:
        contract = Contract.load(path)
    except Exception:  # noqa: BLE001 -- an unreadable contract is `contracts check`'s to report
        return []
    new = {ORPHAN_RULE, UNDECLARED_EVENT_RULE, UNUSED_RULE}
    return [
        f"{v.path}:{v.line} {v.rule}: {v.message}"
        for v in check_placement(contract, project.root)
        if v.rule in new or (v.rule == UNDECLARED_RULE and " queues task " in v.message)
    ][:20]


def _events_nobody_receives(project: Project) -> list[str]:
    """Event types built in a module that nothing in this service can receive.

    ``Outbox.publish`` delivers to this service's own ``@subscribe`` handlers
    through the queue, and to other services through the event bus. With
    neither it raises ``UndeliverableEvent`` in the request now -- a 500 where
    0.1.0a10 answered 201 and wrote a row the relay could only kill. Decided
    from string literals, as the contract rules are: a type built at run time
    is not guessed at.
    """
    if "events" in _active_plugins(project):
        return []
    from jfastframework.contracts.wiring import scan

    wiring = scan(project.root)
    received = {site.name for site in wiring.subscriptions}
    return [
        f'{site.file}:{site.line} Event(type="{site.name}") -- no @subscribe("{site.name}") '
        f"in this service and no events plugin"
        for site in sorted(wiring.publications, key=lambda s: (s.file, s.line))
        if site.name not in received and not site.waived
    ]


def _framework_calls(tree: ast.Module) -> Iterator[tuple[ast.Call, str]]:
    """Each call in *tree* whose target resolves to a name from jfastframework.

    Resolved through the file's own imports, so a project class that happens
    to be called ``Worker`` or ``Outbox`` is not mistaken for the framework's.
    """
    from jfastframework.contracts._scan import call_name, import_aliases, resolve

    aliases = import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        written = call_name(node)
        origin = resolve(written, aliases) if written else None
        if origin is not None and origin.startswith("jfastframework."):
            yield node, origin


def _root_workers(project: Project) -> list[str]:
    """A hand-written worker at the root of the service.

    Before 0.1.0a11 that was the only way to run jobs: a ``worker.py`` that
    booted the app, registered handlers with ``registry.task(...)`` and ran
    ``Worker(...)``. Modules now declare their own with ``@task`` and
    ``@subscribe`` in ``tasks.py`` and ``jfast worker`` runs them, which is
    what the generated deployments start.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(project.root.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        workers = [
            call for call, origin in _framework_calls(tree) if origin.rsplit(".", 1)[-1] == "Worker"
        ]
        if not workers:
            continue
        found += [(path.name, call.lineno, "Worker(...)") for call in workers]
        # `tasks.task(...)` on the registry the app published: an attribute
        # call on a local name, which no import resolves -- so it counts only
        # in a file that is running a framework worker at all.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "task"
                and isinstance(node.func.value, ast.Name)
            ):
                found.append((path.name, node.lineno, f"{node.func.value.id}.task(...)"))
    return [f"{name}:{line} {what}" for name, line, what in sorted(found)]


def _outboxes_built_by_hand(project: Project) -> list[str]:
    """``Outbox(...)`` constructed in the project without an event bus.

    The plugin passes ``events=`` itself. A hand-built outbox without it now
    delivers an event only to this service's own subscribers, and raises when
    there are none, where it used to write a row for the relay to publish.
    """
    found = []
    for relative, tree in _parsed_files(project.root):
        for node, origin in _framework_calls(tree):
            if origin.rsplit(".", 1)[-1] != "Outbox" or not origin.startswith(
                "jfastframework.outbox"
            ):
                continue
            if any(keyword.arg == "events" or keyword.arg is None for keyword in node.keywords):
                continue
            found.append(f"{relative}:{node.lineno} Outbox(...) without events=")
    return found


def _workers_without_a_drain_window(project: Project) -> list[str]:
    """``Worker(...)`` built in the project that stops on the new default window."""
    from jfastframework.queues.worker import DEFAULT_DRAIN_SECONDS

    found = []
    for relative, tree in _parsed_files(project.root):
        for node, origin in _framework_calls(tree):
            if origin.rsplit(".", 1)[-1] != "Worker" or not origin.startswith(
                "jfastframework.queues"
            ):
                continue
            if any(keyword.arg in ("drain_timeout", None) for keyword in node.keywords):
                continue
            found.append(
                f"{relative}:{node.lineno} Worker(...) drains for "
                f"{DEFAULT_DRAIN_SECONDS:g}s, then releases what is still running"
            )
    return found


def _workspace_of(project: Project) -> tuple[Path, list[tuple[str, str]]] | None:
    """The workspace this service belongs to: its root and its `(kind, path)` entries.

    Only when the workspace file lists this service. A workspace file above an
    unrelated project is an accident of where it was cloned, and its compose
    file says nothing about this one.
    """
    from jfastframework.workspace import Workspace

    try:
        path = Workspace.find(project.root)
        if path is None:
            return None
        workspace = Workspace.load(path)
    except Exception:  # noqa: BLE001 -- a broken workspace file is `jfast workspace`'s report
        return None
    root = path.parent
    entries = [(service.kind, service.path) for service in workspace.services]
    mine = project.root.resolve()
    if not any((root / where).resolve() == mine for _, where in entries):
        return None
    return root, entries


def _label(path: Path, project: Project) -> str:
    """*path* as the person at the project root would type it."""
    return Path(os.path.relpath(path, project.root)).as_posix()


def _compose_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose.y*ml")
        for path in directory.glob(pattern)
        if path.is_file()
    )


_WORKER_WORD = re.compile(r"\bworker\b", re.IGNORECASE)


def _deployments_without_a_worker(project: Project) -> list[str]:
    """Deployment files for a service with a queue, none of which runs a worker.

    Generated deployments start one next to the API since 0.1.0a11 -- the same
    image running ``jfast worker``. Any container that mentions a worker counts
    as one, a hand-written ``python worker.py`` included: the question is
    whether anything consumes the queue, not whose command does it.
    """
    if "queue" not in _active_plugins(project):
        return []
    candidates: list[tuple[str, list[Path]]] = [
        (_label(path, project), [path]) for path in _compose_files(project.root)
    ]
    k8s = project.root / "k8s"
    if k8s.is_dir():
        candidates.append((_label(k8s, project) + "/", sorted(k8s.rglob("*.y*ml"))))
    workspace = _workspace_of(project)
    if workspace is not None and workspace[0].resolve() != project.root.resolve():
        root = workspace[0]
        candidates += [(_label(path, project), [path]) for path in _compose_files(root)]
        if (root / "k8s").is_dir():
            candidates.append(
                (_label(root / "k8s", project) + "/", sorted((root / "k8s").rglob("*.y*ml")))
            )

    found = []
    for label, files in candidates:
        texts = []
        for file in files:
            try:
                texts.append(file.read_text(encoding="utf-8"))
            except OSError:
                continue
        if texts and not any(_WORKER_WORD.search(text) for text in texts):
            found.append(f"{label}: no worker container, so nothing consumes the queue")
    return found


def _workspace_compose_missing_client_env(project: Project) -> list[str]:
    """The workspace compose file without the addresses this service's plugins read.

    ``jfast workspace compose`` wired the datastores it declared as workspace
    resources and dropped the rest of each plugin's ``client_env`` -- the Kafka
    brokers, a RabbitMQ URL, an S3 endpoint -- so those containers fell back to
    the .env, which holds the host's addresses. 0.1.0a11 writes them.
    """
    workspace = _workspace_of(project)
    if workspace is None:
        return []
    root, _ = workspace
    expected = _client_env_vars(project)
    found = []
    for compose in _compose_files(root):
        try:
            body = compose.read_text(encoding="utf-8")
        except OSError:
            continue
        missing = sorted(var for var in expected if var not in body)
        if missing:
            found.append(
                f"{_label(compose, project)}: no internal address for {', '.join(missing)}"
            )
    return found


def _queue_settings(project: Project) -> dict[str, Any]:
    return _table(_config(project), "plugin", "queue")


def _framework_tables_owned_elsewhere(project: Project) -> list[str]:
    """Framework tables that gain a column at startup, which this project also manages.

    ``jfast_jobs`` gains ``trace`` and ``jfast_users`` five nullable columns,
    each an ``ADD COLUMN IF NOT EXISTS`` the service runs as it starts. The
    role that created the table owns it and may alter it, so a table the
    service created itself is no concern. One that the project's own
    migrations or SQL create, grant or alter was likely created by another
    role -- and then the service's role cannot alter it and the start fails.
    """
    active = _active_plugins(project)
    tables = []
    queue = _queue_settings(project)
    if "queue" in active and queue.get("backend", "postgres") == "postgres":
        tables.append(str(queue.get("name", "jfast_jobs")))
    if "accounts" in active:
        tables.append("jfast_users")
    if not tables:
        return []

    pattern = re.compile(r"\b(" + "|".join(re.escape(table) for table in tables) + r")\b")
    sources = [
        path
        for path in _files(project.root, ".sql", ".py")
        if path.suffix == ".sql" or "migrations" in path.relative_to(project.root).parts
    ]
    found = []
    for path in sources:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            match = pattern.search(line)
            if match:
                found.append(f"{_label(path, project)}:{number} names {match.group(1)}")
                break
    return found


# Where a generated frontend keeps each piece, in both frameworks.
_FRONTEND_AUTH = ("src/stores/auth.store.js", "src/services/auth.service.js")
_FRONTEND_API = ("src/services/api.js",)
_FRONTEND_ROUTER = ("src/router/index.js", "src/router/index.jsx")


def _frontends(project: Project) -> list[Path]:
    """Frontend projects that talk to this service.

    The workspace's `spa` entries when the workspace lists this service --
    ``jfast start`` puts the frontend beside the API, not inside it -- and any
    directory directly under the service that is a Vite project of its own.
    """
    found: list[Path] = []
    workspace = _workspace_of(project)
    if workspace is not None:
        root, entries = workspace
        found += [root / where for kind, where in entries if kind == "spa"]
    for child in sorted(project.root.iterdir()):
        if (
            child.is_dir()
            and child.name not in SKIP_DIRS
            and (child / "package.json").is_file()
            and (child / "src").is_dir()
        ):
            found.append(child)
    unique: dict[Path, Path] = {}
    for directory in found:
        if directory.is_dir():
            unique.setdefault(directory.resolve(), directory)
    return list(unique.values())


def _frontend_matches(
    project: Project, files: tuple[str, ...], pattern: re.Pattern[str]
) -> Iterator[tuple[str, int, str]]:
    """`(label, line, text)` for each line of those frontend files matching *pattern*."""
    for frontend in _frontends(project):
        for name in files:
            path = frontend / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    yield _label(path, project), number, text


_READS_LOGIN_USER = re.compile(r"\bdata\.user\b")


def _frontends_expecting_the_user_from_login(project: Project) -> list[str]:
    """An auth store that takes the user from ``/auth/login`` and never asks for it.

    ``accounts``' ``/auth/login`` answers with tokens -- and, with MFA on, with
    a challenge instead of them -- never with the user. The store generated
    before 0.1.0a11 read ``data.user`` and so held ``null`` for the whole
    session; the current one asks ``/auth/account`` after signing in.
    """
    if "accounts" not in _active_plugins(project):
        return []
    found = []
    for label, number, text in _frontend_matches(project, _FRONTEND_AUTH, _READS_LOGIN_USER):
        if "/auth/login" in text and "/auth/account" not in text:
            found.append(f"{label}:{number} reads data.user from /auth/login")
    return found


_PUBLIC_BY_DEFAULT = re.compile(r"\bPUBLIC_BY_DEFAULT\s*=\s*true\b")


def _frontends_public_by_default(project: Project) -> list[str]:
    """A router that lets every page through unless it says otherwise, over accounts."""
    if "accounts" not in _active_plugins(project):
        return []
    return [
        f"{label}:{number} PUBLIC_BY_DEFAULT = true"
        for label, number, _ in _frontend_matches(project, _FRONTEND_ROUTER, _PUBLIC_BY_DEFAULT)
    ]


_THIRTY_SECONDS = re.compile(r"\btimeout\s*:\s*30000\b")


def _effective_request_timeout(project: Project) -> float | None:
    """What this service resolves ``request_timeout`` to, None meaning unlimited."""
    app = _table(_config(project), "app")
    if "request_timeout" in app:
        value = app["request_timeout"]
        return float(value) if isinstance(value, int | float) and value else None
    if raises_request_limits(project.plugins, project.disabled):
        return UPLOAD_REQUEST_TIMEOUT
    default = JFastSettings.model_fields["request_timeout"].default
    return float(default) if default else None


def _frontends_giving_up_first(project: Project) -> list[str]:
    """A frontend that abandons a request at 30 s that this API still answers.

    Only where the API is built to take longer: its own request timeout is
    above 30 s (or off), or it calls a language model. Otherwise the two
    agree and there is nothing to change.
    """
    timeout = _effective_request_timeout(project)
    reasons = []
    if timeout is None:
        reasons.append("this API has no request timeout")
    elif timeout > 30:
        reasons.append(f"this API answers for up to {timeout:g}s")
    if "llm" in _active_plugins(project):
        reasons.append("it calls a language model")
    if not reasons:
        return []
    return [
        f"{label}:{number} timeout: 30000 -- {' and '.join(reasons)}"
        for label, number, _ in _frontend_matches(project, _FRONTEND_API, _THIRTY_SECONDS)
    ]


def _accounts_settings(project: Project) -> dict[str, Any] | None:
    if "accounts" not in _active_plugins(project):
        return None
    return _table(_config(project), "plugin", "accounts")


def _verification_required(project: Project) -> list[str]:
    accounts = _accounts_settings(project)
    if accounts is None or accounts.get("email_verification") != "required":
        return []
    return ['[plugin.accounts] email_verification = "required"']


def _mfa_on(project: Project) -> list[str]:
    accounts = _accounts_settings(project)
    if accounts is None:
        return []
    found = []
    if accounts.get("mfa") is True:
        found.append("[plugin.accounts] mfa = true")
    roles = accounts.get("mfa_required_roles")
    if isinstance(roles, list) and roles:
        found.append(f"[plugin.accounts] mfa_required_roles = {roles}")
    return found


def _sign_in_rate_limited(project: Project) -> list[str]:
    """Sign-in limited by default, which only takes effect with a Redis to count in."""
    accounts = _accounts_settings(project)
    if accounts is None or "rate_limit" in accounts or "cache" not in _active_plugins(project):
        return []
    from jfastframework.plugins.builtin.accounts import AccountsSettings

    fields = AccountsSettings.model_fields

    def default(name: str) -> Any:
        return accounts.get(name, fields[name].default)

    return [
        f"[plugin.accounts] rate_limit defaults to true: {default('login_limit_per_ip')} "
        f"sign-ins per IP and {default('login_limit_per_account')} per account every "
        f"{default('login_window_seconds'):g}s"
    ]


def _tenancy_contradictions(project: Project) -> list[str]:
    """What `jfast check`'s new tenancy step reports for this project.

    Run for real rather than restated: the step fails ``jfast check --ci`` at
    any severity, so a project that passed yesterday's CI can fail today's.
    """
    from jfastframework.multitenant.consistency import consistency_findings

    try:
        findings = consistency_findings(
            project.root,
            config=_config(project),
            enabled=sorted(_active_plugins(project)),
        )
    except Exception:  # noqa: BLE001 -- the check reports its own failures
        return []
    return [
        f"{finding.path or 'jfast.toml'}:{finding.line or 0} {finding.code} "
        f"({finding.severity}): {finding.message}"
        for finding in findings
    ]


_POOLER_DSN = re.compile(
    r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']*?(?:pgbouncer|pooler|:6432/|:6543/)[^\s\"']*",
    re.IGNORECASE,
)


def _database_behind_a_pooler(project: Project) -> list[str]:
    """A DSN that goes through PgBouncer, with the setting that makes it safe unset.

    In transaction mode consecutive statements of one session can land on
    different server connections, and both asyncpg's and SQLAlchemy's prepared
    statement caches then fail with "prepared statement does not exist".
    ``[plugin.database] pgbouncer = true`` turns them off. Decided from the
    DSN: a host named for a pooler, or PgBouncer's port (6432) or Supabase's
    transaction pooler port (6543).
    """
    if "database" not in _active_plugins(project):
        return []
    database = _table(_config(project), "plugin", "database")
    if "pgbouncer" in database:
        return []
    directories = [project.root]
    workspace = _workspace_of(project)
    if workspace is not None and workspace[0].resolve() != project.root.resolve():
        directories.append(workspace[0])
    found = []
    for directory in directories:
        files = sorted(directory.glob(".env*")) + _compose_files(directory)
        for path in files:
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, start=1):
                if line.lstrip().startswith("#"):
                    continue
                if _POOLER_DSN.search(line):
                    found.append(f"{_label(path, project)}:{number} a DSN through a pooler")
                    break
    return found


def _settings_refused_at_boot(project: Project) -> list[str]:
    """Values in jfast.toml that 0.1.0a11 refuses at startup instead of at first use.

    Static, and deliberately limited to what the file holds. The plugins'
    own boot checks also judge values from the environment -- the JWKS URL,
    the HMAC secret, the RabbitMQ URL -- and running them here would judge the
    shell this command runs in, which is not the one the service deploys to.
    """
    config = _config(project)
    active = _active_plugins(project)
    production = _table(config, "app").get("env") == "prod"
    found: list[str] = []

    def plugin(name: str) -> dict[str, Any]:
        return _table(config, "plugin", name) if name in active else {}

    database = plugin("database")
    connections = database.get("connections")
    blocks = [("[plugin.database]", database)] + [
        (f"[plugin.database.connections.{name}]", block)
        for name, block in (connections.items() if isinstance(connections, dict) else [])
        if isinstance(block, dict)
    ]
    for where, block in blocks:
        size = block.get("pool_size")
        if isinstance(size, int) and size < 1:
            found.append(f"{where} pool_size = {size}: SQLAlchemy reads it as unlimited")
    zone = database.get("session_timezone")
    if isinstance(zone, str) and zone and not _is_zone(zone):
        found.append(f'[plugin.database] session_timezone = "{zone}" is not an IANA zone')
    for field in ("tenant_dsn_template", "tenant_dsn_env_template"):
        template = database.get(field)
        if isinstance(template, str) and template and "{tenant}" not in template:
            found.append(f"[plugin.database] {field} has no {{tenant}} placeholder")

    storage = plugin("storage")
    disks = storage.get("disks")
    if isinstance(disks, dict):
        from jfastframework.plugins.builtin.storage import VISIBILITIES

        for name, disk in sorted(disks.items()):
            if not isinstance(disk, dict):
                continue
            where = f"[plugin.storage.disks.{name}]"
            visibility = disk.get("visibility")
            if visibility is not None and visibility not in VISIBILITIES:
                found.append(f'{where} visibility = "{visibility}": public or private')
            if disk.get("driver") == "s3" and bool(disk.get("access_key")) != bool(
                disk.get("secret_key")
            ):
                found.append(f"{where} sets one of access_key and secret_key")

    queue = plugin("queue")
    if queue.get("backend", "postgres") == "postgres" and "name" in queue:
        from jfastframework.plugins.builtin.queue import _TABLE_NAME

        if not _TABLE_NAME.match(str(queue["name"])):
            found.append(f'[plugin.queue] name = "{queue["name"]}" is not a table name')
    if production and queue.get("backend") == "rabbitmq":
        from jfastframework.plugins.builtin.queue import DEFAULT_RABBITMQ_URL

        if queue.get("rabbitmq_url") == DEFAULT_RABBITMQ_URL:
            found.append("[plugin.queue] rabbitmq_url is guest@localhost, in production")

    auth = plugin("auth")
    if auth.get("mode") == "public_key" and auth.get("issue_tokens") is True:
        found.append('[plugin.auth] mode = "public_key" with issue_tokens = true')
    algorithms = auth.get("algorithms")
    if isinstance(algorithms, list) and algorithms:
        known = _jwt_algorithms()
        unknown = sorted(str(a) for a in algorithms if known and a not in known)
        if unknown:
            found.append(f"[plugin.auth] algorithms has {', '.join(unknown)}, unknown to PyJWT")
    if production:
        url = auth.get("jwks_url")
        if isinstance(url, str) and url.lower().startswith("http://"):
            found.append("[plugin.auth] jwks_url is plain http, in production")
        from jfastframework.plugins.builtin.auth import MIN_SECRET_BYTES

        secret = auth.get("secret")
        if isinstance(secret, str) and len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            found.append(f"[plugin.auth] secret is under {MIN_SECRET_BYTES} bytes, in production")

    mail = plugin("mail")
    sender = mail.get("from_email")
    if isinstance(sender, str) and sender and "@" not in sender:
        found.append(f'[plugin.mail] from_email = "{sender}" is not an address')

    cache = plugin("cache")
    url = cache.get("url")
    if isinstance(url, str) and url.partition("://")[0].lower() not in ("redis", "rediss", "unix"):
        found.append("[plugin.cache] url is not redis://, rediss:// or unix://")
    return found


def _is_zone(name: str) -> bool:
    import zoneinfo

    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _jwt_algorithms() -> frozenset[str]:
    try:
        import jwt
    except ImportError:  # pragma: no cover -- auth's extra not installed here
        return frozenset()
    return frozenset(jwt.algorithms.get_default_algorithms())


# Redis commands that routinely run past a one-second deadline: server-side
# scripts, full scans, and the blocking reads, which wait by design.
_SLOW_REDIS = frozenset(
    {
        "eval",
        "evalsha",
        "fcall",
        "register_script",
        "scan_iter",
        "blpop",
        "brpop",
        "blmove",
        "brpoplpush",
        "bzpopmin",
        "bzpopmax",
        "xread",
        "xreadgroup",
    }
)


def _slow_redis_commands(project: Project) -> list[str]:
    """Calls to Redis commands a 1 s ``command_timeout`` will now cut off.

    Only in files that reach Redis at all -- they name ``cache.client`` or
    import ``redis`` -- because ``eval`` and ``scan_iter`` are ordinary method
    names elsewhere.
    """
    if "cache" not in _active_plugins(project):
        return []
    if "command_timeout" in _table(_config(project), "plugin", "cache"):
        return []
    found = []
    for path in _python_files(project.root):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        if "cache.client" not in source and not re.search(
            r"^\s*(?:from|import)\s+redis\b", source, re.MULTILINE
        ):
            continue
        relative = path.relative_to(project.root).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _SLOW_REDIS
            ):
                found.append(f"{relative}:{node.lineno} .{node.func.attr}(...)")
    return found


_DATABASE_ERRORS = frozenset(
    {"DBAPIError", "OperationalError", "InterfaceError", "DisconnectionError", "PoolTimeout"}
)


def _own_database_error_handlers(project: Project) -> list[str]:
    """Exception handlers of the project's own for the errors the database now maps to 503.

    Starlette picks the most specific class, and the last handler registered
    for a class: a handler of the project's own for ``OperationalError`` keeps
    answering whatever it answered, and one for ``DBAPIError`` registered
    after the app is built replaces the framework's.
    """
    if "database" not in _active_plugins(project):
        return []
    found = []
    for relative, tree in _parsed_files(project.root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _called_name(node.func) not in ("exception_handler", "add_exception_handler"):
                continue
            handled = _called_name(node.args[0])
            if handled in _DATABASE_ERRORS:
                found.append(f"{relative}:{node.lineno} handles {handled}")
    return found


def _dockerfile_owned_dirs(text: str) -> tuple[set[str], set[str]]:
    """The directories a Dockerfile hands to appuser: ``(chown'd, chown -R'd)``.

    Read from ``chown appuser...`` commands with their continuation lines
    joined, which is how both the generated line and a hand-written one say it.
    """
    joined = text.replace("\\\n", " ")
    plain: set[str] = set()
    recursive: set[str] = set()
    for line in joined.splitlines():
        for command in re.split(r"&&|;|\|\|", line):
            words = command.split()
            if "chown" not in words:
                continue
            args = words[words.index("chown") + 1 :]
            flags = {a for a in args if a.startswith("-")}
            operands = [a for a in args if not a.startswith("-")]
            if len(operands) < 2 or not operands[0].startswith("appuser"):
                continue
            target = recursive if flags & {"-R", "--recursive"} else plain
            target.update(p.rstrip("/") or "/" for p in operands[1:])
    return plain, recursive


def _image_cannot_write_local_storage(project: Project) -> list[str]:
    """A local storage disk whose root a USER appuser image leaves to root."""
    if "storage" not in _active_plugins(project):
        return []
    dockerfile = project.root / "Dockerfile"
    if not dockerfile.is_file():
        return []
    text = dockerfile.read_text(encoding="utf-8", errors="replace")
    if "USER appuser" not in text:
        return []
    disks = _table(_config(project), "plugin", "storage").get("disks")

    from jfastframework.deploy.compose import IMAGE_WORKDIR, local_disk_dirs

    local = local_disk_dirs(disks if isinstance(disks, dict) else None)
    if not local:
        return []
    plain, recursive = _dockerfile_owned_dirs(text)
    if IMAGE_WORKDIR not in plain | recursive:
        return ["Dockerfile: USER appuser, and /app is still owned by root"]

    def owned(directory: str) -> bool:
        if directory in plain or directory in recursive:
            return True
        return any(directory.startswith(parent + "/") for parent in recursive)

    # A disk's root has to exist in the image, owned by appuser, or the volume
    # compose mounts on it is created as root and the disk cannot write:
    # 0.1.0a12's Dockerfile created only the default disks.
    return [
        f"Dockerfile: {directory} (disk {name}) is not created for appuser; "
        "a volume mounted there starts out owned by root"
        for name, directory in local.items()
        if not owned(directory)
    ]


def _nullable_unique_keys(project: Project) -> list[str]:
    """A generated unique key over an optional column, still NULLS NOT DISTINCT."""
    found: list[str] = []
    for relative, tree in _parsed_files(project.root):
        if not relative.startswith("modules/") or "/tests/" in relative:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            optional = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and "None" in ast.unparse(item.annotation)
            }
            for call in ast.walk(node):
                if not (
                    isinstance(call, ast.Call)
                    and ast.unparse(call.func).split(".")[-1] == "UniqueConstraint"
                    and any(
                        k.arg == "postgresql_nulls_not_distinct"
                        and isinstance(k.value, ast.Constant)
                        and k.value.value is True
                        for k in call.keywords
                    )
                ):
                    continue
                columns = [
                    a.value
                    for a in call.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                nullable = [c for c in columns if c in optional and c != "tenant_id"]
                if nullable:
                    found.append(f"{relative}:{call.lineno} {node.name}: {', '.join(nullable)}")
    return found


def _facades_with_an_optional_tenant(project: Project) -> list[str]:
    """A multitenant service's module facades that still accept ``tenant_id=None``.

    What 0.1.0a11 generated for every layout, tenancy or not. Only a service
    with tenancy is told: with one customer, None is the right value (its rows
    carry no tenant), and ``jfast check --multitenant-ready`` lists the same
    signatures for the day that changes.
    """
    from jfastframework.multitenant.readiness import optional_tenant_parameters

    if "tenancy" not in _active_plugins(project):
        return []
    found: list[str] = []
    for relative, tree in _parsed_files(project.root):
        parts = relative.split("/")
        if len(parts) != 3 or parts[0] != "modules" or parts[2] != "public.py":
            continue
        for function, argument in optional_tenant_parameters(tree):
            spelled = ast.unparse(argument.annotation) if argument.annotation else "= None"
            found.append(f"{relative}:{argument.lineno} {function.name}(tenant_id: {spelled})")
    return found


def _deployment_keys_in_the_file(project: Project) -> list[str]:
    """``[app] env`` or ``debug`` written in ``jfast.toml``.

    Exactly the projects whose running environment can change: from 0.1.0a12
    ``JFAST_ENV`` and ``JFAST_DEBUG``, when the process environment sets them,
    win over these two keys, and a project without them already took both
    from the environment. Every project ``jfast start`` generated before
    0.1.0a12 has ``env = "local"``.
    """
    from jfastframework.settings import DEPLOYMENT_KEYS

    app = _table(_config(project), "app")
    present = [key for key in DEPLOYMENT_KEYS if key in app]
    if not present:
        return []
    lines: dict[str, int] = {}
    table = ""
    try:
        text = (project.root / "jfast.toml").read_text(encoding="utf-8")
    except OSError:
        text = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("["):
            table = stripped.strip("[] ")
            continue
        if table != "app":
            continue
        match = re.match(r"(\w+)\s*=", stripped)
        if match and match.group(1) in present:
            lines.setdefault(match.group(1), number)
    found = []
    for key in present:
        value = app[key]
        spelled = f'"{value}"' if isinstance(value, str) else str(value).lower()
        found.append(
            f"jfast.toml:{lines.get(key, 0)} [app] {key} = {spelled}: "
            f"{DEPLOYMENT_KEYS[key]} in the environment now wins over it"
        )
    return found


def _unsigned_tenant_sources_with_auth(project: Project) -> list[str]:
    """`auth` and `tenancy` on, with a subdomain, path or header among the sources.

    Only that combination changed: without `auth` there is no principal to
    check and the resolved tenant stays usable, and `token`/`user` are signed.
    The sources read are the ones the plugin will use -- its default is
    ``["token", "subdomain"]`` -- and ``subdomain`` counts only with a
    ``base_domain``, since without one the service refuses to start at all.
    """
    active = _active_plugins(project)
    if "auth" not in active or "tenancy" not in active:
        return []
    tenancy = _table(_config(project), "plugin", "tenancy")
    sources = tenancy.get("sources", ["token", "subdomain"])
    if not isinstance(sources, list):
        return []
    unsigned = [
        str(source)
        for source in sources
        if source in ("path", "header")
        or (source == "subdomain" and str(tenancy.get("base_domain") or ""))
    ]
    if not unsigned:
        return []
    spelled = ", ".join(f'"{source}"' for source in sources)
    written = "sources" in tenancy
    where = f"[plugin.tenancy] sources = [{spelled}]" + ("" if written else " (the default)")
    return [f"{where}: {', '.join(unsigned)} no longer grants a tenant without a session"]


def _tenant_header_without_tenancy(project: Project) -> list[str]:
    """Code that sends or configures the tenant header, in a service without ``tenancy``.

    Only the places that show the header is really used -- its name in the
    service's own code or its frontends, or a configured ``tenant_header`` --
    because every service reads ``request.state.tenant_id`` and nearly none
    ever relied on a bare header for it.
    """
    if "tenancy" in _active_plugins(project):
        return []
    observability = _table(_config(project), "plugin", "observability")
    header = str(observability.get("tenant_header") or "X-Tenant-ID")
    found: list[str] = []
    if "tenant_header" in observability:
        found.append(f"[plugin.observability] tenant_header = {header!r}")
    pattern = re.compile(re.escape(header), re.IGNORECASE)
    paths = [p for p in _python_files(project.root) if "tests" not in p.parts]
    for frontend in _frontends(project):
        paths += _files(frontend / "src", ".js", ".ts", ".vue", ".jsx", ".tsx")
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            code = line.strip()
            if code.startswith(("#", "//", "*")) or not pattern.search(code):
                continue
            found.append(f"{_label(path, project)}:{number} {code[:90]}")
            if len(found) >= 10:
                return found
    return found


def _revocation_fails_open(project: Project) -> list[str]:
    """Revocation checked against Redis, with the new fail-open default unchosen.

    A memory store never fails, so only a service whose tokens are checked
    against the shared store -- auth with ``cache`` -- can reach the outage.
    """
    active = _active_plugins(project)
    if "auth" not in active or "cache" not in active:
        return []
    auth = _table(_config(project), "plugin", "auth")
    if auth.get("check_revocation") is False or "revocation_fail_open" in auth:
        return []
    return ["[plugin.auth] revocation_fail_open is not set, so it defaults to true"]


# The version on a note is the version the described code LANDED in, never the
# version being prepared. `applicable` keeps changes in `(current, installed]`,
# so a note tagged with the version a project is already pinned to is skipped
# in silence -- and the published version is what every project is pinned to.
# Three notes shipped tagged 0.1.0a4 for code that does not exist in 0.1.0a4;
# `jfast upgrade` answered "nothing between those versions affects this
# project" to every one of them.
CHANGES: tuple[Change, ...] = (
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="hexagonal-eager-create-payload",
        summary="A hexagonal module's __init__.py imports its HTTP adapter; its own test fails.",
        detail=(
            "0.1.0a10's hexagonal template bound CreatePayload with a plain `from "
            ".adapters.http import ... as CreatePayload` at the top of the package init. "
            "Python runs that file on the way to modules.<name>.domain, so importing the "
            "domain loaded FastAPI, Pydantic and SQLAlchemy -- the one thing the layout "
            "promises it does not -- and the module's generated domain test, which checks "
            "exactly that in a subprocess, fails. 0.1.0a11's template resolves "
            "CreatePayload in __getattr__, as it already did for router."
        ),
        detect=_eager_adapter_imports,
        remedy=(
            "Delete the listed import and resolve the name on first access instead: in the "
            "module's `__getattr__`, next to the `router` branch, add `if name == "
            '"CreatePayload": from .adapters.http import <Name>Create; return <Name>Create`. '
            "Keep the import under `if TYPE_CHECKING:` for type checkers. "
            "`from modules.<name> import CreatePayload` keeps working."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="publish-without-receiver",
        summary="Publishing an event nothing can receive raises now, in the request.",
        detail=(
            "Outbox.publish delivers to this service's own @subscribe handlers through the "
            "queue, and to other services through the event bus. With neither it raises "
            "UndeliverableEvent -- a 500 where 0.1.0a10 answered 201 and wrote an outbox row "
            "the relay could only mark dead. The events listed are built in a module, have "
            "no subscriber in this service, and the events plugin is off."
        ),
        detect=_events_nobody_receives,
        remedy=(
            'Add @subscribe("<type>") in the module that reacts, in its tasks.py, and declare '
            'the type under [modules.<publisher>] publishes = ["<type>"] in contracts.toml. '
            'If another service consumes it, enable the "events" plugin instead.'
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="contracts-event-rules",
        summary="contracts check reports undeclared events, orphan subscriptions, tasks by name.",
        detail=(
            "Events are how modules react to each other without importing each other, and "
            "they are now a declared part of a module's API. A subscription to a type no "
            "module declares under publishes never runs (orphan-subscription); an event a "
            "module builds without declaring it is undeclared-event; a depends_on entry "
            "nothing uses is unused-dependency. And a job queued by name for a task another "
            'module owns -- Job(task="alerta.revisar") from comprobante -- is now an '
            "undeclared-dependency: a call into that module, spelt as a string so no import "
            "check could see it. A task's owner is the module whose @task declares it, or "
            "the name's `<module>.` prefix when none does."
        ),
        detect=_event_and_task_wiring,
        remedy=(
            "For a task queued by name: publish an event instead (declare it under "
            "[modules.<publisher>] publishes) and @subscribe to it in the owning module's "
            "tasks.py -- no dependency either way -- or add the owner to depends_on. Declare "
            "each event a module builds under its publishes; fix or delete orphan "
            "subscriptions and unused depends_on entries. `jfast contracts explain <rule>` "
            "gives the fix for each."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="outbox-manual-construction",
        summary="An Outbox built by hand without events= no longer relays events to a bus.",
        detail=(
            "Outbox takes the event bus as events= now. Without it an event reaches only this "
            "service's own subscribers, and publish raises UndeliverableEvent when there are "
            "none -- where it used to write a row for the relay to send. The outbox plugin "
            "passes it; an Outbox constructed in project code does not."
        ),
        detect=_outboxes_built_by_hand,
        remedy=(
            'Pass the bus: Outbox(queue=..., events=ctx.require("events")) when the events '
            'plugin is on. Better, take the plugin\'s: ctx.require("outbox").'
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="frontend-login-without-account",
        summary="The frontend reads the user from /auth/login, which never returns one.",
        detail=(
            "accounts' /auth/login answers with tokens -- and, with MFA on, with a challenge "
            "{mfa_required | mfa_enrollment_required, mfa_token, expires_in} instead of them. "
            "The auth store generated before 0.1.0a11 took `data.user` from that response, "
            "so it held null for the whole session, and it has no step for the challenge."
        ),
        detect=_frontends_expecting_the_user_from_login,
        remedy=(
            "After a successful sign-in, GET /auth/account and keep that as the user. When "
            "the login response has mfa_required or mfa_enrollment_required, ask for the code "
            "and POST it with the mfa_token to /auth/login/mfa. The store a fresh `jfast new "
            "service <name> --kind spa` writes does both, and can be copied over."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="accounts-verification-required",
        summary='email_verification = "required" locks out every existing user until verified.',
        detail=(
            "Verification is new, and required mode refuses sign-in to an unverified "
            "address. Every row already in jfast_users has email_verified_at NULL, so "
            "switching it on refuses every account that exists today, and each needs a "
            "verification email to get back in."
        ),
        detect=_verification_required,
        remedy=(
            "Grandfather the accounts you already trust, in a migration or by hand, before "
            "deploying: UPDATE jfast_users SET email_verified_at = created_at WHERE "
            "email_verified_at IS NULL. New sign-ups are verified from then on."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="accounts-mfa-login-challenge",
        summary="With MFA on, /auth/login can answer with a challenge instead of tokens.",
        detail=(
            "A user with TOTP enrolled -- or in a role listed in mfa_required_roles -- gets "
            "{mfa_required | mfa_enrollment_required, mfa_token, expires_in} and no tokens. "
            "Every client that assumes a successful login always carries access_token "
            "breaks for those users, scripts and mobile apps included."
        ),
        detect=_mfa_on,
        remedy=(
            "Handle the challenge in each client: ask for the code and POST it with the "
            "mfa_token to /auth/login/mfa, which answers with the tokens. Service accounts "
            "that sign in unattended should not be in a role that requires MFA."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="settings-refused-at-boot",
        summary="Settings that used to fail at first use now stop the service starting.",
        detail=(
            "Each plugin checks its settings as it starts, and a value that could never work "
            "is a refused boot rather than an error on the first query, send or upload: "
            "pool_size = 0 (SQLAlchemy reads it as unlimited), a session_timezone that is "
            "not an IANA zone, a tenant DSN template without {tenant}, a storage visibility "
            "other than public or private, an S3 key without its secret, a queue name that "
            "is not a table name, public_key mode that issues tokens, an algorithm PyJWT "
            "does not know, a from_email that is not an address. In production also a plain "
            "http jwks_url, an HMAC secret under 32 bytes and RabbitMQ's guest default. Only "
            "what jfast.toml holds is listed here; values from the environment are checked "
            "at boot where they are known."
        ),
        detect=_settings_refused_at_boot,
        remedy="Correct each value listed. Every refusal names its key and the fix at boot too.",
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="tenancy-consistency-check",
        summary="jfast check has a tenancy step, and `jfast check --ci` fails on its findings.",
        detail=(
            "It reports tenant settings that contradict each other or the code -- a "
            "tenant-scoped rag store with no tenancy plugin to resolve a tenant, a per-tenant "
            "LLM budget with no tenant, and the like. Plain `jfast check` still exits 0 on a "
            "medium finding; `--ci` fails on any, so a pipeline that passed yesterday fails "
            "today."
        ),
        detect=_tenancy_contradictions,
        remedy=(
            "One customer: set the store single-tenant (for rag, [plugin.rag] tenant_scoped "
            "= false). Several: `jfast tenancy enable --tenant <id>` turns tenancy on and "
            "writes the migration. `jfast check` prints the reason under each finding."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="frontend-public-by-default",
        summary="The frontend router lets every page through, over a service with accounts.",
        detail=(
            "Generated frontends default to PUBLIC_BY_DEFAULT = false when the workspace has "
            "accounts: a page must say public: true to be reachable signed out. The router "
            "generated before 0.1.0a11 said true, so a page that forgot requiresAuth was "
            "open, and the API's 401 was the only guard."
        ),
        detect=_frontends_public_by_default,
        remedy=(
            "Set PUBLIC_BY_DEFAULT = false and mark the pages that must work signed out -- "
            "login, register, verify-email, forgot and reset password, a landing page -- "
            "with meta public: true (handle.public in React)."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="frontend-api-timeout",
        summary="The frontend gives up at 30 s on requests this API still answers.",
        detail=(
            "Generated frontends read VITE_API_TIMEOUT and default to 60 s. A client timeout "
            "shorter than the slowest endpoint turns a slow answer into an error the user "
            "retries -- which runs the expensive call a second time, and for a language "
            "model that is money."
        ),
        detect=_frontends_giving_up_first,
        remedy=(
            "In src/services/api.js use `timeout: Number(import.meta.env.VITE_API_TIMEOUT) || "
            "60000` and add VITE_API_TIMEOUT to the frontend's .env files, at least as long "
            "as this API's request_timeout."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="accounts-sign-in-rate-limit",
        summary="Sign-in is rate limited per IP and per account by default.",
        detail=(
            "With cache on, accounts counts sign-in attempts in Redis and answers 429 with "
            "Retry-After past the limit, and limits verification and reset emails too. "
            "Clients that share one address -- a load test, an e2e suite, every user behind "
            "a proxy whose X-Forwarded-For this service does not trust -- share one budget."
        ),
        detect=_sign_in_rate_limited,
        remedy=(
            "Make sure the client address is the real one (trusted proxies), then raise "
            "login_limit_per_ip for a shared egress. Set [plugin.accounts] rate_limit = false "
            "to keep 0.1.0a10's behaviour."
        ),
    ),
    Change(
        version="0.1.0a12",
        kind="breaking",
        code="image-cannot-write-local-storage",
        summary="The generated image cannot write a local storage disk: boot fails or /ready 503s.",
        detail=(
            "The Dockerfile runs as appuser, but WORKDIR created /app as root and --chown only "
            "reached the copied files, so local storage could not create storage/ and the "
            "service failed to start. A volume mounted on a path the image never created is "
            "created as root too: a disk other than public and private (storage/adjuntos) "
            "answered /ready 503 and every upload 500 with PermissionError. Development on "
            "the host never sees it."
        ),
        detect=_image_cannot_write_local_storage,
        remedy=(
            "Regenerate it with `jfast deploy dockerfile` (it reads [plugin.storage.disks]), "
            "or run `jfast add storage` to rewrite only its storage line. By hand: RUN mkdir "
            "-p <each local disk root> && chown appuser:appuser /app <each root and its "
            "parents>. A volume docker already created as root keeps root: chown it once "
            "(docs/storage.md, `A volume created as root`)."
        ),
    ),
    Change(
        version="0.1.0a12",
        kind="behaviour",
        code="unique-key-on-optional-field",
        summary="A unique key over an optional field allowed one row without a value.",
        detail=(
            "`jfast new module --unique` generated NULLS NOT DISTINCT for every key, so two "
            "rows with the optional field empty collided, and the rule looked None up: the "
            "second receipt without a UUID answered 409. 0.1.0a12 generates a partial unique "
            "index (WHERE field IS NOT NULL) and skips the rule when the value is missing."
        ),
        detect=_nullable_unique_keys,
        remedy=(
            "Replace the UniqueConstraint with Index(name, 'tenant_id', 'field', unique=True, "
            "postgresql_nulls_not_distinct=True, postgresql_where=text('field IS NOT NULL')), "
            "return early from the availability rule when the field is None, and write the "
            "migration that drops the constraint and creates the index."
        ),
    ),
    Change(
        version="0.1.0a12",
        kind="behaviour",
        code="facade-tenant-optional",
        summary="Generated facades accepted tenant_id=None, which reads every tenant's rows.",
        detail=(
            "public.py's get_<module>(session, *, tenant_id: str | None, ...) built its "
            "repository with tenant_id as given, and None means no tenant filter: a task or "
            "another module passing a variable that happened to be None read every tenant. "
            "`jfast check --multitenant-ready` only caught the literal None. 0.1.0a12 "
            "generates tenant_id: str in a service with tenancy, and the readiness report "
            "flags a facade signature that admits None (facade-tenant-optional)."
        ),
        detect=_facades_with_an_optional_tenant,
        remedy=(
            "Change each listed parameter to `tenant_id: str` and run mypy: it names every "
            "caller that can still pass None. Give those the tenant they run for -- "
            "current_tenant in a route, job.tenant_id in a task."
        ),
    ),
    Change(
        version="0.1.0a12",
        kind="behaviour",
        code="jfast-env-wins-over-the-file",
        summary="JFAST_ENV and JFAST_DEBUG now win over [app] env and debug in jfast.toml.",
        detail=(
            'jfast start wrote [app] env = "local", and jfast.toml won over the environment, '
            "so JFAST_ENV=prod -- the switch docs/deploy.md's checklist names -- did nothing: "
            "the production image ran with /docs, /info and /queue/stats open, console mail "
            "and no HSTS. env and debug describe the deployment, so the process environment "
            "now beats the file for those two (every other key still loses to the file), and "
            "a disagreement is a WARNING at boot. A deployment that sets JFAST_ENV now gets "
            "that value instead of the file's."
        ),
        detect=_deployment_keys_in_the_file,
        remedy=(
            "Delete the listed line (env defaults to local) and set JFAST_ENV=prod where the "
            "service is deployed, under compose's environment: rather than in a copied .env. "
            "Keep it only if every environment that runs this file should share the value "
            "and none sets JFAST_ENV."
        ),
    ),
    Change(
        version="0.1.0a12",
        kind="breaking",
        code="unsigned-tenant-needs-a-session",
        summary=(
            "With auth on, a subdomain, path or header names a tenant but no longer grants one."
        ),
        detail=(
            "current_tenant returned the subdomain's tenant with nobody signed in, so "
            "`curl -H 'Host: acme.example.com' /tickets` listed and created acme's rows, and a "
            "signed-in user of one tenant on another's subdomain was served as that tenant. "
            "Now, with auth on: no session is a 401; a token whose tenant claim differs from "
            "the subdomain, path or header is a 403; a token with no tenant is a 403 unless "
            "[plugin.tenancy] trust_unscoped_principals = true. request.state.tenant_id, the "
            "RLS session and TenantSession get only the granted tenant; the named one is on "
            "request.state.tenant_requested, which the sign-in routes read."
        ),
        detect=_unsigned_tenant_sources_with_auth,
        remedy=(
            "Nothing for routes that use current_tenant: sign in on the subdomain and send the "
            "token. A page that is public on purpose (a tenant's sign-in form, its branding) "
            "takes Depends(requested_tenant) instead. If tokens carry no tenant claim and the "
            "service checks membership itself, set trust_unscoped_principals = true."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="breaking",
        code="tenant-header-not-a-tenant",
        summary="A bare X-Tenant-ID header no longer sets the tenant; only tenancy can trust it.",
        detail=(
            "observability used to copy the header into request.state.tenant_id when nothing "
            "else had resolved a tenant, and current_tenant, the RLS session and every Job or "
            "Event built in the request trusted that value. With auth on and tenancy off an "
            "anonymous request was served as whichever tenant it named. The header is now only "
            "a log field, tenant_claimed; the tenant comes from a signed token or from tenancy."
        ),
        detect=_tenant_header_without_tenancy,
        remedy=(
            "If a trusted gateway in front of this service sets the header, enable tenancy with "
            'sources = ["header"] (after "token" if tokens carry a tenant). Otherwise put the '
            "tenant in the token (auth's tenant claim) and stop sending the header."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="revocation-fail-open",
        summary="A revocation store that is down now accepts tokens instead of failing requests.",
        detail=(
            "When Redis does not answer, the revocation check is skipped and the token is "
            "accepted, with a warning in the log -- so an outage of the cache does not take "
            "down every authenticated route. The cost is that a token revoked by a logout "
            "works until Redis is back or it expires."
        ),
        detect=_revocation_fails_open,
        remedy=(
            "Keep the default for availability, or set [plugin.auth] revocation_fail_open = "
            "false where a logout that does not take effect for a few minutes is worse than "
            "an outage: requests then get 503 while the store is down."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="redis-command-timeout",
        summary="Every Redis command gives up after 1 s now.",
        detail=(
            "The cache client had no command deadline, so a Redis that stopped answering "
            "hung every request that touched it. command_timeout is 1 s by default, with a "
            "breaker behind it. A server-side script, a full scan or a blocking read waits "
            "longer than that by design, and is now cut off."
        ),
        detect=_slow_redis_commands,
        remedy=(
            "Raise [plugin.cache] command_timeout above the slowest call listed, or give that "
            "call its own client; for a blocking read, pass it a timeout below the deadline. "
            "0 turns the deadline off."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="database-unavailable-503",
        summary="A lost database connection or a full pool answers 503, not 500.",
        detail=(
            "The database plugin registers a handler for DBAPIError and PoolTimeout: a lost "
            "connection or a pool with nothing free is backpressure, and a client may retry "
            "it. A handler of this project's own for those classes either keeps answering "
            "what it did (a subclass such as OperationalError wins) or replaces the "
            "framework's (the same class, registered after it)."
        ),
        detect=_own_database_error_handlers,
        remedy=(
            "Drop the handlers listed unless they do more than choose a status code, or make "
            "them answer 503 with Retry-After for a lost connection and a full pool."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="regenerate-deploy-for-worker",
        summary="Generated deployments run a worker now; nothing here consumes the queue.",
        detail=(
            "A queue nobody consumes is jobs piling up while the API answers 201. The "
            "compose and Kubernetes generators add a worker next to the API -- the same "
            "image running `jfast worker`, with a stop grace period longer than its drain "
            "window -- and the files listed predate that."
        ),
        detect=_deployments_without_a_worker,
        remedy=(
            "Regenerate: `jfast deploy compose` for the service's own compose file, `jfast "
            "workspace compose` and `jfast workspace k8s` for the workspace's. A file edited "
            "by hand gains a service with the API's build, environment and volumes, command "
            '["jfast", "worker", "--grace=25"] and stop_grace_period: 30s.'
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="workspace-compose-client-env",
        summary="The workspace compose file lacks the internal addresses some plugins read.",
        detail=(
            "`jfast workspace compose` wired the datastores declared as workspace resources "
            "and dropped the rest of each plugin's client addresses -- Kafka brokers, a "
            "RabbitMQ URL, an S3 endpoint -- so those containers fell back to the .env, which "
            "holds the host's addresses: localhost, inside a container, is the container. "
            "0.1.0a11 writes them under each service's environment."
        ),
        detect=_workspace_compose_missing_client_env,
        remedy=(
            "Run `jfast workspace compose` from the workspace root to rewrite it, or add "
            "each variable listed under this service's `environment:` by hand."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="worker-drain-timeout",
        summary="A stopping Worker waits 25 s for running jobs, then hands them back.",
        detail=(
            "Worker used to wait for every in-flight job however long it took, and the "
            "orchestrator's SIGKILL -- 30 s after SIGTERM in Kubernetes -- then killed it "
            "mid-job, leaving the job invisible until the visibility timeout. It now waits "
            "drain_timeout seconds and releases the rest to the queue without spending an "
            "attempt. A job longer than the window is cancelled on shutdown and runs again "
            "from the start."
        ),
        detect=_workers_without_a_drain_window,
        remedy=(
            "If a job must not be cut short, pass drain_timeout= above it and raise the "
            "orchestrator's grace period (stop_grace_period, terminationGracePeriodSeconds) "
            "above that. Make long jobs safe to re-run either way."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="deprecated",
        code="worker-py-to-module-tasks",
        summary="A hand-written root worker.py is superseded by module tasks and `jfast worker`.",
        detail=(
            "Handlers registered in a root worker.py belong to no module, so the contract "
            "cannot see which module owns a task or who queues it, and the generated "
            "deployments start `jfast worker`, not this file. Modules now declare their own "
            "work with @task and @subscribe in tasks.py, get a session with TaskSession, and "
            "`jfast worker` runs every module's tasks. The file keeps working."
        ),
        detect=_root_workers,
        remedy=(
            'Move each handler into modules/<owner>/tasks.py as `@task("<name>")` (from '
            "jfastframework.tasks), taking its session from TaskSession; delete worker.py; "
            "run `jfast worker`. Copy [layers.tasks] from a freshly generated contracts.toml "
            "so the contract governs the new files. To react to another module, prefer an "
            "event and @subscribe over queuing its task by name."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="contracts-tasks-layer",
        summary="Module tasks.py files fall in no layer, or in the wrong one, under this contract.",
        detail=(
            "Contracts generated since 0.1.0a11 have a [layers.tasks] for modules/*/tasks.py: "
            "an entry point like a route, which may call the service or use cases and may "
            "not import FastAPI or SQLAlchemy. Without it the screaming contract's catch-all "
            "claims tasks.py as domain -- so a task that calls a use case is a layer "
            "violation -- and the other layouts' contracts leave it ungoverned."
        ),
        detect=_tasks_without_a_tasks_layer,
        remedy=(
            "Copy the [layers.tasks] block from a freshly generated contracts.toml for this "
            "layout (`jfast new service tmp` then `jfast new module x --layout <layout>`), "
            "adjusting may_import to this contract's layer names."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="framework-tables-altered-at-startup",
        summary="jfast_jobs and jfast_users gain columns at startup, which needs ALTER on them.",
        detail=(
            "The PostgreSQL queue adds a nullable trace column to its table, and accounts "
            "adds five nullable columns to jfast_users, each with ADD COLUMN IF NOT EXISTS "
            "as the service starts -- a catalogue change, no row is rewritten. The role that "
            "created a table owns it and may alter it. These files also manage the table, "
            "which suggests another role created it; then the service's role cannot, and "
            "the start fails."
        ),
        detect=_framework_tables_owned_elsewhere,
        remedy=(
            "Run the ALTERs as the owner before deploying -- ALTER TABLE jfast_jobs ADD "
            "COLUMN IF NOT EXISTS trace JSONB; for jfast_users, start the service once as "
            "the owning role -- or make the service's role the owner of those tables."
        ),
    ),
    Change(
        version="0.1.0a11",
        kind="behaviour",
        code="database-behind-pgbouncer",
        summary="This DSN goes through a pooler, and [plugin.database] pgbouncer is new.",
        detail=(
            "In PgBouncer's transaction mode consecutive statements of one session can land "
            "on different server connections, and asyncpg's and SQLAlchemy's prepared "
            "statement caches then fail with 'prepared statement does not exist' under "
            "load. pgbouncer = true turns both caches off and names statements uniquely; "
            "tenant row-level security behind it is verified."
        ),
        detect=_database_behind_a_pooler,
        remedy=(
            "Set [plugin.database] pgbouncer = true when the pooler runs in transaction "
            "mode (per connection under [plugin.database.connections.<name>]). Session mode "
            "needs nothing: set pgbouncer = false to say so and silence this note."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="breaking",
        code="async-dependencies",
        summary="require_auth, optional_auth, current_tenant and tenant_zone are async.",
        detail=(
            "FastAPI runs a plain `def` dependency in its threadpool, and that hop cost "
            "75-85 us per request in the 0.1.0a10 benchmark -- more than every middleware "
            "together -- for functions that only read request.state. As Depends(...) nothing "
            "changes. A direct call now returns a coroutine, and the first attribute read "
            "on it fails."
        ),
        detect=_direct_calls_to_async_dependencies,
        remedy=(
            "Inside plain code use `principal_of(request)` (from "
            "jfastframework.plugins.builtin.auth) or `request.state.principal`; for the "
            "tenant, `request.state.tenant_id`. Or make the caller `async def` and await it. "
            "Better still, take them as parameters: `tenant: str = Depends(current_tenant)`."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="behaviour",
        code="sync-service-factories",
        summary="A `def` service factory costs a threadpool hop on every request.",
        detail=(
            "Modules generated before 0.1.0a10 wire their service with `def get_service("
            "request, session: DbSession)`. FastAPI runs a plain def dependency in its "
            "threadpool: about 80 us per request, the largest single cost measured. New "
            "modules generate `async def`. Nothing breaks either way."
        ),
        detect=_sync_request_factories,
        remedy=(
            "Add `async` in front of each listed `def`. The body stays the same -- it awaits "
            "nothing, and it does not need to."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="behaviour",
        code="metrics-route-labels",
        summary="Metrics are labelled by route template; the in-flight gauge by method only.",
        detail=(
            "The middleware read the matched route before routing had run, found none, and "
            "labelled every request by its raw path: /users/41, /users/42... one series per "
            "id, and a registry that grew without bound under a scanner. `endpoint` is now "
            "the route template (/users/{user_id}), `<unmatched>` for a path no route "
            "matched, and http_requests_in_progress carries `method` only, because the route "
            "is not known while a request is still in flight."
        ),
        detect=_metrics_enabled,
        remedy=(
            "Dashboards and alerts that filter http_requests_total or "
            "http_request_duration_seconds on a raw path must use the template; queries on "
            "http_requests_in_progress must drop the endpoint label."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="breaking",
        code="rag-tenant-required",
        summary="The rag store refuses to read or write without a tenant.",
        detail=(
            "A chunk's identity was (document_id, chunk_index), so two tenants with a "
            "document of the same id overwrote each other, and a search with tenant_id=None "
            "searched every tenant's documents. Identity is (tenant_id, document_id, "
            "chunk_index) now, every statement filters by tenant, and a tenant-scoped store "
            "-- the default -- raises TenantRequiredError (a 403) when a call has none. "
            "ensure_schema upgrades a pgvector table in place and keeps its rows."
        ),
        detect=_rag_without_tenant_scope,
        remedy=(
            "Pass tenant_id to every rag call -- the `rag` service's ingest, search and delete "
            "take it as a keyword. A service with exactly one tenant sets [plugin.rag] "
            "tenant_scoped = false and keeps passing None."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="breaking",
        code="rag-router-off",
        summary="The rag HTTP router is off by default, and needs auth when on.",
        detail=(
            "It was mounted by default, checked no token, and took the tenant from the "
            "request body -- anyone who could reach the service could search any tenant. "
            "Modules should call the `rag` service with the tenant they resolved. When "
            "mounted, the router now requires the auth plugin and a signed-in caller, and "
            "takes the tenant from the tenancy plugin or the token."
        ),
        detect=_rag_router_default,
        remedy=(
            "If a client calls /rag/documents or /rag/search, set [plugin.rag] mount_router "
            "= true, enable the auth plugin, and stop sending tenant_id in the body (it is "
            "ignored). Otherwise nothing to do."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="breaking",
        code="rag-qdrant-point-ids",
        summary="Qdrant point ids now include the tenant: re-ingest existing collections.",
        detail=(
            "Point ids were derived from (document_id, chunk_index), so the same document "
            "id in two tenants was the same point. They are derived from (tenant_id, "
            "document_id, chunk_index) now. Points written by 0.1.0a9 keep their old ids: "
            "a re-ingest writes new points beside them instead of replacing them."
        ),
        detect=_rag_on_qdrant,
        remedy=(
            "Delete the collection (or its points) and ingest every document again. The "
            "collection is recreated with a tenant-aware payload index on startup."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="breaking",
        code="module-boundaries",
        summary="Modules talk through modules/<name>/public.py, declared in depends_on.",
        detail=(
            "A module could not import another, and the advice was to move the thing to "
            "shared/ -- right for an enum, wrong for behaviour, so modules read each other's "
            "tables with raw SQL that no check could see. Now each module may expose "
            "public.py, the only file others may import, returning DTOs and taking the "
            "caller's session and tenant_id; the importer declares it in [modules.<name>] "
            "depends_on. `contracts check` adds undeclared-dependency, module-cycle, "
            "public-leak, cross-module-sql and unknown-dependency, and cross-module now also "
            "catches relative imports across modules and every name in `import a, b`."
        ),
        detect=_module_boundary_violations,
        remedy=(
            "For each cross-module-sql line: add a function to the owning module's public.py "
            "that runs the query there and returns a dataclass or Pydantic model, call it, and "
            "add the owner to depends_on. `jfast contracts explain <rule>` gives the fix for "
            "each rule. Waive a line with `# contracts: allow <reason>` only for a report that "
            "must join across modules."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="behaviour",
        code="screaming-public-layer",
        summary="A screaming contract from 0.1.0a9 reads public.py as domain code.",
        detail=(
            "Its catch-all layer glob matches modules/<name>/public.py, so a facade that "
            "builds a repository reports layer and layer-package violations. New contracts "
            "carry a [layers.public] that claims the file first."
        ),
        detect=_screaming_contract_without_public_layer,
        remedy=(
            "Copy the [layers.public] block from a freshly generated contracts.toml "
            "(`jfast new service tmp --layout screaming`) into this one."
        ),
    ),
    Change(
        version="0.1.0a10",
        kind="behaviour",
        code="rag-recursive-chunking",
        summary="Documents are chunked at headings, paragraphs and sentences by default.",
        detail=(
            "chunk_text cut fixed character windows, which split clauses and tables in "
            "half. The default is now `recursive`. A document ingested again after upgrading "
            "is re-chunked, and its chunks re-embedded, once."
        ),
        detect=_rag_chunking_default,
        remedy='Set [plugin.rag] chunk_strategy = "fixed" to keep 0.1.0a9 chunks.',
    ),
    Change(
        version="0.1.0a5",
        kind="breaking",
        code="layer-globs-narrowed",
        summary="A layer glob's `*` stops at `/` now. Files below changed hands.",
        detail=(
            "Layer paths went through fnmatch, which translates `*` to `.*` and crosses "
            "directory separators. `modules/*/repository.py` therefore claimed "
            "`modules/billing/infrastructure/repository.py` as well, so a layer could "
            "appear to govern a tree it was never written for -- and fnmatch case-folds "
            "on Windows, so the same contract passed on a laptop and failed in CI. The "
            "files listed above matched a layer yesterday and match none today: whatever "
            "that layer forbids is no longer enforced on them."
        ),
        detect=_globs_that_narrowed,
        remedy=(
            "For each file above, decide which it is. If the layer was meant to reach it, "
            "widen that pattern to `**` -- `modules/**/repository.py` crosses directories "
            "on purpose and says so. If it was never meant to, the file is now ungoverned: "
            "run `jfast contracts check` to see what it does that no layer allows."
        ),
    ),
    Change(
        version="0.1.0a5",
        kind="breaking",
        code="naive-datetime-rule",
        summary="datetime.now() with no tz is a contract violation now.",
        detail=(
            "The rule ships on [rules.async_safety], which your contract already enables, "
            "so it arrives without an opt-in and a build that passed yesterday fails "
            "today. A naive datetime has no zone, so its meaning is whatever zone the "
            "process happens to run in -- the same row written by a laptop and by a "
            "container in production means two different instants, and neither says so."
        ),
        detect=_naive_datetimes,
        remedy=(
            "Replace each with jfastframework.time.now(), which returns an aware UTC "
            "datetime, or pass tz= to the call. To defer the whole rule, set "
            "`naive_datetime = false` under [rules.async_safety] -- that leaves the "
            "async-blocking half on, which is the half you already had."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="timestamps-timezone-aware",
        summary="TimestampMixin columns are timezone-aware. Existing tables need a migration.",
        detail=(
            "created_at and updated_at mapped to TIMESTAMP WITHOUT TIME ZONE, so a row "
            "serialised as 2026-08-29T20:55:15 with no offset and every JavaScript client "
            "read it as local time. They are timestamptz now, and the columns already in "
            "your database are not."
        ),
        detect=_timestamp_migration,
        remedy=(
            "Write the statements above into an Alembic revision by hand. The USING clause "
            "is load-bearing and autogenerate omits it: without USING, PostgreSQL converts "
            "through the implicit cast, reads every stored value in the server's own "
            "TimeZone, and silently shifts the whole table on any server not set to UTC. "
            "Nothing fails; the timestamps are just wrong afterwards."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="contracts-shared-import",
        summary="Generated contracts let every layer import shared/. Yours predate that.",
        detail=(
            "contracts.toml belongs to the project once generated, so this one was not "
            "rewritten. The placement rule tells you to move a twice-wanted enum into "
            "shared/, and a layer that may not import shared/ has no legal way to follow "
            "the instruction the checker itself prints."
        ),
        detect=_layers_without_shared,
        remedy=(
            'Add "shared" to the may_import list of each layer above, by hand. Do not run '
            "contracts init --force: it writes the corrected defaults and overwrites the "
            "whole file, discarding the project-specific lines that are the part worth "
            "having."
        ),
    ),
    Change(
        version="0.1.0a5",
        kind="breaking",
        code="contracts-layout-mismatch",
        summary="jfast new service no longer writes contracts.toml. The one it wrote may not fit.",
        detail=(
            "The first jfast new module --layout X writes the contract for X now, so the "
            "contract and the tree agree by construction. A project generated before that "
            "has the layered contract on disk whatever its modules turned out to be, and "
            "contracts check reports every layer matching nothing as layer-unmatched -- "
            "exit 5, which this release also made distinct from a plain failure."
        ),
        detect=_contracts_layout_mismatch,
        remedy=(
            "Point the paths of each layer above at the folders your modules really use, or "
            "delete contracts.toml and let the next jfast new module --layout X write the "
            "matching one. contracts init --force also fixes it and discards the entire "
            "file -- every layer, rule and waiver this project added to it -- so it is the "
            "last resort here, not the first."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="refresh-tokens-rejected",
        summary="Refresh tokens minted by 0.1.0a3 and earlier are refused with a 401.",
        detail=(
            "Every active session re-authenticates once, at the moment it deploys. Those "
            "sessions were already broken: rotating one returned a token with no scopes, "
            "which authenticated and was authorized for nothing."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]
            if _issues_tokens(project)
            else []
        ),
        remedy=(
            "Nothing to change in code. Deploy at a quiet hour, or expect one sign-in per "
            "active session."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="logout-ends-one-session",
        summary="/auth/logout ends the calling session only. It used to end all of them.",
        detail=(
            "There is no sign-out-everywhere replacement in this release: a subject-level "
            "cursor needs a TokenStore change and ships separately. A UI whose button says "
            "'sign out of all devices' now tells the truth about one device."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service serves /auth/logout"]
            if _issues_tokens(project)
            else []
        ),
        remedy=(
            "Reword any UI that promised more, and re-check tests that asserted the old behaviour."
        ),
    ),
    Change(
        version="0.1.0a5",
        kind="breaking",
        code="token-store-rotate-refresh",
        summary="TokenStore.rotate_refresh returns rotated/raced/replayed, not a bool.",
        detail=(
            "It takes a grace keyword as well. A bool could not tell a client retrying "
            "apart from a stolen token being replayed -- both are 'this one was already "
            "used' -- and only the store can "
            "answer the two together, because between a False and a second round trip the "
            "winner of a race may not have recorded itself yet. Every truthiness test on "
            "the old return value now also passes for 'replayed', which is the one outcome "
            "that has to end the family."
        ),
        detect=_token_store_implementations,
        remedy=(
            "Give rotate_refresh a grace: int = 0 keyword and return one of the three "
            "literals in jfastframework.auth.store.RefreshOutcome. MemoryTokenStore and "
            "RedisTokenStore in that module are worked examples; a store that cannot honour "
            "a grace window returns 'replayed' wherever it used to return False."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="pagination-total-optional",
        summary="Page.total is int | None, so a paginated response can carry a null total.",
        detail=(
            "The modes that skip the COUNT never knew a total, and reporting one anyway was "
            "a number nobody could act on; those pages answer has_more from a row read past "
            "the page instead. This reaches clients, not only code that type-checks: the "
            "JSON of every paginated endpoint this framework generated can now hold "
            '"total": null, and a response model declaring total: int fails validation on '
            "the page that produces it."
        ),
        detect=_pagination_call_sites,
        remedy=(
            "At each call site above, decide what a missing total renders as -- has_more "
            "answers 'is there a next page' without one. Widen any response model or DTO "
            "field that mirrors it to int | None, including the generated PageResponse if "
            "you copied it into a module."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="behaviour",
        code="access-token-fam-claim",
        summary="Access tokens carry a fam claim, so revoking a session kills its access tokens.",
        detail=(
            "Previously an access token outlived the revocation of its session until it "
            "expired on its own. Anything that cached a decoded token payload sees a new "
            "claim it did not before."
        ),
        detect=lambda project: (
            ["[plugin.auth] issue_tokens = true -- this service mints the tokens"]
            if _issues_tokens(project)
            else []
        ),
        remedy="No action unless something of yours asserts on the exact claim set.",
    ),
    Change(
        version="0.1.0a5",
        kind="behaviour",
        code="refresh-grace-seconds",
        summary="refresh_grace_seconds is new, defaults to 10, and narrows reuse detection.",
        detail=(
            "For that many seconds after a rotation, the token it replaced is answered as a "
            "race rather than treated as theft, which stops a client that double-submits "
            "from signing itself out. The cost is the part worth stating: a stolen refresh "
            "token replayed inside the window is not detected and the family is not "
            "revoked. Ten seconds was chosen for a retry, not for an attacker."
        ),
        detect=_refresh_grace_unset,
        remedy=(
            "Set [plugin.auth] refresh_grace_seconds = 0 to keep strict reuse detection, at "
            "the price of ending a family every time a client retries a refresh. Any value "
            "above 0 is a window in which a replay is indistinguishable from a race."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="breaking",
        code="request-limit-defaults",
        summary="max_body_bytes and request_timeout have values now. Both were None.",
        detail=(
            "A request larger than the limit is refused with 413 and one that outlives the "
            "timeout is answered with 504, where both used to be accepted. 0 and None both "
            "still mean unlimited, so an explicit 0 keeps the old behaviour."
        ),
        detect=_request_limit_defaults,
        remedy=(
            "Set both in [app] of jfast.toml if these are wrong for what this service "
            "accepts. A service taking uploads wants the raised pair; 0 turns a limit off."
        ),
    ),
    Change(
        version="0.1.0a4",
        kind="behaviour",
        code="cli-exit-codes",
        summary="contracts check exits 5, doctor exits 2 or 3. Both used to exit 1.",
        detail=(
            "Informational, and stated unconditionally: nothing in a project says whether "
            "its pipeline branches on an exit code, so this cannot be detected and will "
            "not be guessed at. Codes are documented in jfastframework.cli.exits."
        ),
        detect=None,
        remedy=(
            "Grep your CI config for these commands. A step asserting `exit code == 1` now "
            "passes when it should fail; one testing `!= 0` is unaffected."
        ),
    ),
    Change(
        version="0.1.0a8",
        kind="breaking",
        code="session-store-per-process",
        summary="auth minting tokens without the cache plugin will not start in production.",
        detail=(
            "The token store is in memory without it, which is per worker, and the "
            "generated image runs one worker per CPU. A logout revoked on the process "
            "that served it and nowhere else, so the token kept working on every other "
            "worker; and a refresh reaching any worker but the issuing one found no "
            "family for it and was answered 401 'this session has been revoked' -- a "
            "revocation that never happened, three times in four on four cores. That is "
            "not a degraded mode, so production refuses to start rather than serve it."
        ),
        detect=_session_store_missing,
        remedy=(
            'Add "cache" to [plugins].enabled and install it: pip install '
            '"jfastframework[cache]". The compose file gains a Redis container from the '
            "plugin graph on the next `jfast deploy compose`. If this service only "
            "verifies tokens somebody else minted, set [plugin.auth] issue_tokens = "
            "false instead -- it then holds no session of its own and starts as before."
        ),
    ),
    Change(
        version="0.1.0a8",
        kind="breaking",
        code="mail-backend-silent",
        summary="A mail backend that delivers nothing will not start in production.",
        detail=(
            "`console` is the default and the right default: nobody emails a real "
            "customer from a laptop. In production it printed every message to stdout "
            "while `send` reported success -- no bounce, no error, no queue backing up. "
            "The verification link, the password reset and the invoice never arrived, "
            "and the only symptom was a customer saying so a week later. The smtp "
            "branch already refused to start with credentials missing; this is the same "
            "failure with no first send to fail on."
        ),
        detect=_silent_mail_backend,
        remedy=(
            'Set [plugin.mail] backend = "smtp" and provide JFAST_MAIL_USERNAME and '
            "JFAST_MAIL_PASSWORD in the deployed environment. Local runs are unaffected: "
            'the refusal is only at env = "prod", so `console` stays the default '
            "everywhere else. Drop the mail plugin if this service sends none."
        ),
    ),
    Change(
        version="0.1.0a9",
        kind="behaviour",
        code="autogenerate-drops-framework-tables",
        summary="Autogenerate can write a migration that drops the framework's own tables.",
        detail=(
            "The queue has always created jfast_jobs at startup, and 0.1.0a9's outbox, "
            "idempotency and accounts plugins create more jfast_* tables the same way. None "
            "of them is among the service's models, so an env.py without a filter reads them "
            "as tables the service deleted, and `alembic revision --autogenerate` writes "
            "op.drop_table for each -- a migration that empties the job queue. Generated "
            "env.py files now pass include_name to skip them; this one does not."
        ),
        detect=_env_drops_framework_tables,
        remedy=(
            "Add `from jfastframework.db.framework import include_name` to migrations/env.py "
            "and pass `include_name=include_name` to both context.configure(...) calls. Read "
            "any autogenerated revision you have not applied yet for drop_table on a jfast_* "
            "table, and delete those lines."
        ),
    ),
    Change(
        version="0.1.0a9",
        kind="breaking",
        code="session-commits-after-response",
        summary="A route whose session commits after the response now stops the service starting.",
        detail=(
            "FastAPI runs the code after a dependency's `yield` once the response has been "
            "sent, unless the dependency is function-scoped. The session dependencies commit "
            "there, so a commit that failed -- a deferred constraint, a serialisation "
            "failure, a dropped connection -- had already answered 201 for a row that does "
            "not exist, and a client that read its own write straight away could get there "
            "before the commit. The database plugin now refuses to start while any route "
            'reaches a session dependency without scope="function", and the FastAPI '
            "floor is 0.121, the first release that has the parameter."
        ),
        detect=_sessions_committing_after_the_response,
        remedy=(
            "Replace `session=Depends(session_dependency)` with `session: DbSession` "
            "(and read_session_dependency with ReadSession, tenant_session_dependency with "
            "TenantSession), imported from jfastframework.plugins.builtin.database. Or keep "
            'Depends and add scope="function". A generator dependency of your own that '
            "wraps a session must be function-scoped as well: FastAPI refuses a "
            "request-scoped dependency that depends on a function-scoped one."
        ),
    ),
    Change(
        version="0.1.0a8",
        kind="breaking",
        code="job-timeout-past-visibility",
        summary="A worker whose job_timeout reaches past the claim now refuses to start.",
        detail=(
            "A claim is invisible to other workers for visibility_timeout and nothing "
            "extends it while a handler runs, so a job that outlives the window is "
            "claimed again -- by another worker, while the first is still inside it. "
            "The job then runs twice and neither run knows: a charge taken twice, an "
            "email sent twice. Both numbers defaulted to 300 seconds and lived in "
            "different files, so the pair raced at the boundary and raising one without "
            "the other made the duplicate certain."
        ),
        detect=_job_timeout_past_the_claim,
        remedy=(
            "Drop the job_timeout argument and the worker derives one from the backend "
            "-- 80% of the window, which leaves room for the nack to land. If a handler "
            "genuinely needs longer, raise [plugin.queue] visibility_timeout above the "
            "longest job this worker runs and keep job_timeout below it."
        ),
    ),
    Change(
        version="0.1.0a7",
        kind="behaviour",
        code="compose-artifacts-stale",
        summary="Deployment files generated before 0.1.0a7 cannot bring this service up.",
        detail=(
            "Two things were missing from what the generators wrote. The compose file gives "
            "the application service `build:` and nothing wrote the Dockerfile that entry "
            "reads, so `docker compose up --build` stopped before starting a container. And "
            "that service loaded a .env holding the host's addresses -- localhost and the "
            "published port -- which inside a container is that container, so it "
            "crash-looped against its own port. Both are generated correctly now, and "
            "neither file is rewritten by upgrading: they belong to the project."
        ),
        detect=_stale_deploy_artifacts,
        remedy=(
            "Run `jfast deploy dockerfile` if a Dockerfile is listed above, then "
            "`jfast deploy compose -o <your compose file>` to rewrite the compose file from "
            "the plugin graph. Both are generated and say so in their header. A compose "
            "file since edited by hand should instead gain each datastore's internal "
            "address under the application service's `environment:`, which beats "
            "`env_file`."
        ),
    ),
)


Applicable = list[tuple[Change, list[str]]]


def applicable(project: Project, *, current: str, installed: str) -> Applicable:
    """Changes landing in ``(current, installed]`` that this project can feel.

    Ordered as the manifest declares them: oldest version first, and within a
    version by how much work the change costs the reader.
    """
    low = parse_version(current)
    high = parse_version(installed)

    found: Applicable = []
    for change in CHANGES:
        landed = parse_version(change.version)
        if not (low < landed <= high):
            continue
        if change.detect is None:
            found.append((change, []))
            continue
        affected = change.detect(project)
        if affected:
            found.append((change, affected))
    return found
