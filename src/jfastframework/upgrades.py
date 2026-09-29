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
import json
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


def _issues_tokens(project: Project) -> bool:
    """Whether this service is the one minting tokens, not just verifying them.

    A service that only validates somebody else's tokens is untouched by every
    change to the issuing endpoints, which is most services with `auth` on.
    """
    if "auth" not in project.plugins:
        return False
    return bool(_table(_config(project), "plugin", "auth").get("issue_tokens", False))


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


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
        if any(part in SKIP_DIRS for part in env.parts):
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
            ):
                affected.append(f"{where}:{statement.lineno}  ->  {node.name}.rotate_refresh")
    return affected


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
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        for layer, patterns in sorted(layers.items()):
            was = any(fnmatch(relative, p) for p in patterns)
            now = any(match_path(relative, p) for p in patterns)
            if was and not now:
                lost.append(f"{relative} (was layer {layer!r})")
    return sorted(lost)


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
    if "cache" in project.plugins:
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
    """A rag service that relied on the router being mounted by default."""
    rag = _rag_settings(project)
    if rag is None or "mount_router" in rag:
        return []
    return ["[plugin.rag] mount_router now defaults to false (POST /rag/search is gone)"]


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
    ][:20]


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
    found = []
    for relative, tree in _parsed_files(project.root):
        awaited = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        }
        found += [
            f"{relative}:{node.lineno} {_called_name(node.func)}(...)"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _called_name(node.func) in _NOW_ASYNC
            and id(node) not in awaited
        ]
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
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("get_service", "get_use_cases"):
                found.append(f"{relative}:{node.lineno} def {node.name}")
    return found


def _metrics_enabled(project: Project) -> list[str]:
    if "metrics" not in project.plugins:
        return []
    return ["[plugins] enabled includes metrics"]


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


# The version on a note is the version the described code LANDED in, never the
# version being prepared. `applicable` keeps changes in `(current, installed]`,
# so a note tagged with the version a project is already pinned to is skipped
# in silence -- and the published version is what every project is pinned to.
# Three notes shipped tagged 0.1.0a4 for code that does not exist in 0.1.0a4;
# `jfast upgrade` answered "nothing between those versions affects this
# project" to every one of them.
CHANGES: tuple[Change, ...] = (
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
