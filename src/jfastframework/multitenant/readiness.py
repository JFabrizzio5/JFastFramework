"""What a switch to multitenant would break, with file and line.

A single-tenant service is allowed to assume one customer, and does, in places
nothing marks: a repository built with ``tenant_id=None``, a route that opens a
session behind ``require_auth`` only, a report written in raw SQL, an upload
stored under ``invoices/{id}.pdf``, a cache key that is the same for everyone.
Each of them is correct today. The day a second customer arrives, each one is
either a leak or a bug, and finding them is a manual hunt through the tree.

This is that hunt, done once, the way ``jfast upgrade --check`` reports what
an upgrade breaks. Run it before ``jfast tenancy enable`` and after it: the
command prints the same report as its list of remaining manual steps.

**These are heuristics**, and they are documented as such (docs/multitenancy.md
lists every rule and what it cannot see). Where a rule cannot decide -- a key
that arrives as a parameter, a factory imported from another package -- it
stays quiet rather than guess, so what is reported is worth reading. What is
reported and deliberate is waived inline, with the comment ``jfast contracts
check`` already honours::

    rates = await cache.get("fx:usd")  # contracts: allow exchange rates are global

A waived finding is not dropped: it is listed as waived, with its reason, so
the decision stays reviewable.

Tests are not read. A test that passes ``tenant_id=None`` exercises the
single-tenant behaviour on purpose.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jfastframework.multitenant._source import (
    SourceFile,
    TenantTable,
    config_line,
    enabled_plugins,
    load_config,
    plugin_table,
    source_files,
    tenant_tables,
    waiver_for,
)
from jfastframework.project import SEVERITY_ORDER, Finding

__all__ = ["RULES", "Readiness", "ReadinessFinding", "readiness"]

#: Every rule, its severity, and one line on what it looks for. The table the
#: docs print, and the `--json` payload's `rules`, come from here.
RULES: dict[str, tuple[str, str]] = {
    "tenant-none-literal": (
        "high",
        "a call passing `tenant_id=None`: a repository, a facade, `rag` or `llm`",
    ),
    "factory-without-tenant": (
        "high",
        "a dependency that opens a database session and takes no tenant dependency",
    ),
    "route-without-tenant": (
        "high",
        "a route that opens a database session and takes no tenant dependency",
    ),
    "raw-sql-without-tenant": (
        "high",
        "an SQL string naming a tenant table and never `tenant_id`",
    ),
    "storage-key-without-tenant": (
        "high",
        "a storage key built without the tenant",
    ),
    "cache-key-without-tenant": (
        "high",
        "a cache key built without the tenant",
    ),
    "rag-unscoped": ("high", "`[plugin.rag] tenant_scoped = false`"),
    "scheduled-job-without-tenant": (
        "medium",
        "a scheduled task that enqueues a `Job` or builds an `Event` with no `tenant_id`, "
        "or opens a `TaskSession` -- a tick runs as no tenant",
    ),
    "llm-call-without-tenant": (
        "medium",
        "an `llm` call with no `tenant_id`, so no per-tenant budget applies",
    ),
}

HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"}
)

#: A dependency that ties the request to a tenant. `TenantSession` routes to the
#: tenant's own database and refuses without one, so it counts.
TENANT_MARKERS = ("current_tenant", "TenantSession", "tenant_session_dependency")

#: What opens a database session. `sessionmaker` covers a hand-written
#: dependency that opens its own (`ctx.require("db.sessionmaker")()`).
SESSION_MARKERS = (
    "DbSession",
    "ReadSession",
    "AsyncSession",
    "SessionRLS",
    "session_dependency",
    "read_session_dependency",
    "sessionmaker",
)

STORAGE_METHODS = frozenset(
    {"put", "put_stream", "write", "get", "delete", "exists", "stat", "url", "temporary_url"}
)
CACHE_METHODS = frozenset(
    {"get", "set", "delete", "exists", "get_or_set", "incr", "incrby", "expire", "ttl"}
)
LLM_METHODS = frozenset({"chat", "complete", "embed"})

_SQL = re.compile(r"\b(select|insert|update|delete)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReadinessFinding:
    """A finding, and the waiver that set it aside if one did."""

    finding: Finding
    waived: str | None = None

    def describe(self) -> dict[str, Any]:
        described = self.finding.describe()
        described["waived"] = self.waived
        return described


@dataclass(frozen=True)
class Readiness:
    """The whole report: what breaks, what was waived, and what was read."""

    findings: tuple[ReadinessFinding, ...]
    tables: tuple[TenantTable, ...]
    files_read: int
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def open(self) -> list[Finding]:
        return [item.finding for item in self.findings if item.waived is None]

    @property
    def waived(self) -> list[ReadinessFinding]:
        return [item for item in self.findings if item.waived is not None]


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _text(source: SourceFile, node: ast.AST) -> str:
    return ast.get_source_segment(source.text, node) or ast.unparse(node)


def _mentions_tenant(text: str) -> bool:
    return "tenant" in text.lower()


def _span(node: ast.AST) -> list[int]:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start) or start
    return list(range(start, end + 1))


def _called(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _docstrings(tree: ast.Module) -> set[int]:
    """Node ids of every docstring, which are prose even when they show SQL."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


class _Reporter:
    """Collects findings, applying the waiver and dropping duplicates."""

    def __init__(self) -> None:
        self.items: list[ReadinessFinding] = []
        self._seen: set[tuple[str, str | None, int | None]] = set()

    def add(
        self,
        code: str,
        message: str,
        why: str,
        *,
        source: SourceFile | None = None,
        path: str | None = None,
        line: int | None = None,
        span: Sequence[int] = (),
    ) -> None:
        where = source.path if source is not None else path
        key = (code, where, line)
        if key in self._seen:
            return
        self._seen.add(key)
        waived = None
        if source is not None and line is not None:
            waived = waiver_for(source.lines, line, *span)
        severity = RULES[code][0]
        self.items.append(
            ReadinessFinding(
                Finding(
                    severity=severity, code=code, message=message, why=why, path=where, line=line
                ),
                waived,
            )
        )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _none_literals(source: SourceFile, report: _Reporter) -> None:
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "tenant_id"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is None
            ):
                name = _called(node.func) or "a call"
                report.add(
                    "tenant-none-literal",
                    f"{name}(..., tenant_id=None)",
                    (
                        '`None` means "no tenant filter": a repository returns every '
                        "tenant's rows, a facade answers for all of them, and `rag`/`llm` "
                        "refuse or charge nobody. Pass the tenant the caller resolved -- "
                        "`current_tenant` in a route, `job.tenant_id` in a task."
                    ),
                    source=source,
                    line=keyword.value.lineno,
                    span=_span(node),
                )


@dataclass
class _Deps:
    touches_data: bool = False
    has_tenant: bool = False
    #: Resolved dependencies that open a session and take no tenant.
    factories: list[tuple[SourceFile, ast.FunctionDef | ast.AsyncFunctionDef]] = field(
        default_factory=list
    )


class _RouteScan:
    """Routes, the dependencies they reach, and whether a tenant is among them.

    A dependency is followed when it is a function this project defines: in the
    same file, or anywhere in the same module directory (``modules/<name>/``),
    because that is where ``get_service`` lives in every generated layout.
    Resolved by name within that scope, since every module has a
    ``get_service`` and resolving across modules would mix them up. A
    dependency imported from anywhere else is not followed; it counts only
    through the names it carries.

    ``Annotated`` aliases declared by the project (``CurrentTenant =
    Annotated[str, Depends(current_tenant)]``) are expanded project-wide.
    """

    def __init__(self, files: Sequence[SourceFile]) -> None:
        self.files = [f for f in files if not f.is_test]
        self.aliases: dict[str, str] = {}
        self.scopes: dict[str, dict[str, tuple[SourceFile, Any]]] = {}
        for source in self.files:
            scope = self.scopes.setdefault(self._scope(source), {})
            for node in source.tree.body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and "Annotated" in _text(source, node.value):
                        self.aliases[target.id] = _text(source, node.value)
            for function in _functions(source.tree):
                scope.setdefault(function.name, (source, function))

    @staticmethod
    def _scope(source: SourceFile) -> str:
        parts = Path(source.path).parts
        if len(parts) >= 3 and parts[0] == "modules":
            return "/".join(parts[:2])
        return source.path

    def _expand(self, text: str) -> str:
        for name, value in self.aliases.items():
            if re.search(rf"\b{re.escape(name)}\b", text):
                text = f"{text} {value}"
        return text

    def _dependency_names(self, node: ast.AST) -> list[str]:
        names: list[str] = []
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and _called(call.func) in ("Depends", "Security")
                and call.args
                and isinstance(call.args[0], ast.Name)
            ):
                names.append(call.args[0].id)
        return names

    def closure(
        self,
        source: SourceFile,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        seen: set[int] | None = None,
        extra: Sequence[ast.AST] = (),
    ) -> tuple[_Deps, bool]:
        """What *function* reaches, and whether it opens a session itself."""
        seen = seen if seen is not None else set()
        seen.add(id(function))
        deps = _Deps()
        arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        defaults = [*function.args.defaults, *function.args.kw_defaults]
        parts: list[ast.AST] = [a.annotation for a in arguments if a.annotation is not None]
        parts.extend(d for d in defaults if d is not None)
        parts.extend(extra)

        own = " ".join(self._expand(_text(source, part)) for part in parts)
        direct = any(marker in own for marker in SESSION_MARKERS)
        # A factory that opens its own session inside the body.
        body = " ".join(_text(source, statement) for statement in function.body)
        direct = direct or "sessionmaker" in body
        deps.touches_data = direct
        deps.has_tenant = any(marker in own for marker in TENANT_MARKERS)

        names: list[str] = []
        for part in parts:
            names.extend(self._dependency_names(part))
        for alias, value in self.aliases.items():
            if re.search(rf"\b{re.escape(alias)}\b", own):
                names.extend(re.findall(r"Depends\(\s*([A-Za-z_][A-Za-z0-9_]*)", value))

        scope = self.scopes.get(self._scope(source), {})
        for name in names:
            if name in TENANT_MARKERS:
                deps.has_tenant = True
                continue
            target = scope.get(name)
            if target is None or id(target[1]) in seen:
                continue
            inner, _ = self.closure(target[0], target[1], seen)
            deps.touches_data = deps.touches_data or inner.touches_data
            deps.has_tenant = deps.has_tenant or inner.has_tenant
            deps.factories.extend(inner.factories)
            if inner.touches_data and not inner.has_tenant:
                deps.factories.append(target)
        return deps, direct

    def routes(
        self,
    ) -> Iterator[tuple[SourceFile, ast.FunctionDef | ast.AsyncFunctionDef, list[ast.expr]]]:
        for source in self.files:
            router_tenant = any(
                isinstance(node, ast.Call)
                and _called(node.func) == "APIRouter"
                and any(
                    k.arg == "dependencies" and _mentions_tenant_marker(_text(source, k.value))
                    for k in node.keywords
                )
                for node in ast.walk(source.tree)
            )
            for function in _functions(source.tree):
                decorators = [
                    d
                    for d in function.decorator_list
                    if isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Attribute)
                    and d.func.attr in HTTP_METHODS
                ]
                if not decorators:
                    continue
                extra = [k.value for d in decorators for k in d.keywords if k.arg == "dependencies"]
                if router_tenant:
                    continue
                yield source, function, extra


def _mentions_tenant_marker(text: str) -> bool:
    return any(marker in text for marker in TENANT_MARKERS)


def _routes(files: Sequence[SourceFile], report: _Reporter) -> None:
    scan = _RouteScan(files)
    users: dict[int, tuple[SourceFile, Any, list[str]]] = {}
    for source, function, extra in scan.routes():
        deps, direct = scan.closure(source, function, extra=extra)
        if not deps.touches_data or deps.has_tenant:
            continue
        span = [d.lineno for d in function.decorator_list] + [function.lineno]
        if direct or not deps.factories:
            report.add(
                "route-without-tenant",
                f"route {function.name}() opens a database session and takes no tenant",
                (
                    "Today the tenant is whatever `request.state` holds, which is nothing: the "
                    "repository runs unfiltered and, once row-level security is on, the query "
                    "returns no rows. Add `tenant: str = Depends(current_tenant)` and hand it "
                    "to the repository -- a 401/403 before the query is the failure you want."
                ),
                source=source,
                line=function.lineno,
                span=span,
            )
        for factory_source, factory in deps.factories:
            entry = users.setdefault(id(factory), (factory_source, factory, []))
            entry[2].append(function.name)

    for factory_source, factory, routes in users.values():
        names = sorted(set(routes))
        shown = ", ".join(f"{name}()" for name in names[:4])
        more = f" and {len(names) - 4} more" if len(names) > 4 else ""
        report.add(
            "factory-without-tenant",
            (
                f"{factory.name}() opens a database session with no tenant dependency; "
                f"used by {shown}{more}"
            ),
            (
                'The generated factory reads `getattr(request.state, "tenant_id", None)`, '
                "and `None` builds a repository with no tenant filter. Take the tenant as a "
                "dependency -- `tenant: str = Depends(current_tenant)` -- and pass it on, so a "
                "request with no tenant stops at 401/403 instead of reading every customer."
            ),
            source=factory_source,
            line=factory.lineno,
            span=[d.lineno for d in factory.decorator_list] + [factory.lineno],
        )


def _sql_text(source: SourceFile, node: ast.AST) -> str | None:
    """The literal text of a string expression, with each hole kept as its source."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            else:
                pieces.append("{" + _text(source, value) + "}")
        return "".join(pieces)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _sql_text(source, node.left)
        right = _sql_text(source, node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _raw_sql(source: SourceFile, tables: Sequence[str], report: _Reporter) -> None:
    if not tables:
        return
    docstrings = _docstrings(source.tree)
    inside: set[int] = set()
    patterns = [
        (
            table,
            re.compile(
                rf"\b(from|join|update|into)\s+(\"?\w+\"?\.)?\"?{re.escape(table)}\"?\b",
                re.IGNORECASE,
            ),
        )
        for table in tables
    ]
    for node in ast.walk(source.tree):
        if id(node) in inside or id(node) in docstrings:
            continue
        text = _sql_text(source, node)
        if text is None:
            continue
        # The outermost string expression speaks for its parts.
        for child in ast.walk(node):
            if child is not node:
                inside.add(id(child))
        if not _SQL.search(text) or _mentions_tenant(text):
            continue
        named = [table for table, pattern in patterns if pattern.search(text)]
        if not named:
            continue
        report.add(
            "raw-sql-without-tenant",
            f"SQL against {', '.join(named)} with no tenant_id",
            (
                "Raw SQL bypasses the repository's filter, so today it reads and writes every "
                "tenant's rows. Row-level security turns that into no rows at all -- a "
                "visible bug instead of a leak -- but the query is still wrong. Add "
                "`WHERE tenant_id = :tenant` (and `tenant_id` to an INSERT)."
            ),
            source=source,
            line=getattr(node, "lineno", None),
            span=_span(node),
        )


def _receiver(func: ast.expr, source: SourceFile) -> str:
    return _text(source, func.value).lower() if isinstance(func, ast.Attribute) else ""


def _enclosing_assignment(
    function: ast.AST | None, name: str, before: int, source: SourceFile
) -> str | None:
    """The source of the last ``name = ...`` before line *before*, in *function*."""
    if function is None:
        return None
    found: str | None = None
    found_line = -1
    for node in ast.walk(function):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        line = getattr(node, "lineno", -1)
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or not (found_line < line < before):
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            found, found_line = _text(source, value), line
    return found


def _key_is_unscoped(key: ast.expr, function: ast.AST | None, source: SourceFile) -> bool | None:
    """True when the key is visibly built without the tenant, None when undecidable."""
    if isinstance(key, ast.Name):
        assigned = _enclosing_assignment(function, key.id, key.lineno, source)
        if assigned is None:
            # A parameter or a global: whoever built it is not in view.
            return None
        return not _mentions_tenant(assigned) and not _mentions_tenant(key.id)
    if isinstance(key, ast.Constant | ast.JoinedStr | ast.BinOp):
        return not _mentions_tenant(_text(source, key))
    if (
        isinstance(key, ast.Call)
        and isinstance(key.func, ast.Attribute)
        and key.func.attr == "format"
    ):
        return not _mentions_tenant(_text(source, key))
    return None


def _keys(source: SourceFile, report: _Reporter) -> None:
    owners: dict[int, ast.AST] = {}
    for function in _functions(source.tree):
        for child in ast.walk(function):
            owners[id(child)] = function
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        receiver = _receiver(node.func, source)
        key: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "key":
                key = keyword.value
        if key is None:
            continue
        kind: str | None = None
        if method in STORAGE_METHODS and ("storage" in receiver or "disk" in receiver):
            kind = "storage"
        elif method in CACHE_METHODS and "cache" in receiver:
            kind = "cache"
        if kind is None:
            continue
        if _key_is_unscoped(key, owners.get(id(node)), source) is not True:
            continue
        if kind == "storage":
            report.add(
                "storage-key-without-tenant",
                f"storage key {_text(source, key)} does not include the tenant",
                (
                    "Two tenants with the same id write the same object, and one reads the "
                    "other's file. Prefix the key with the tenant -- `f\"{tenant}/invoices/"
                    "{id}.pdf\"` -- and move existing objects under the initial tenant's prefix "
                    "when you switch."
                ),
                source=source,
                line=node.lineno,
                span=_span(node),
            )
        else:
            report.add(
                "cache-key-without-tenant",
                f"cache key {_text(source, key)} does not include the tenant",
                (
                    "The cache is shared by every tenant, so the first tenant to fill this key "
                    'answers for all of them. Put the tenant in the key -- `f"{tenant}:'
                    'report:{month}"` -- or waive it if the value really is global.'
                ),
                source=source,
                line=node.lineno,
                span=_span(node),
            )


def _llm_calls(source: SourceFile, report: _Reporter) -> None:
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LLM_METHODS or "llm" not in _receiver(node.func, source):
            continue
        if any(k.arg == "tenant_id" or k.arg is None for k in node.keywords):
            continue
        report.add(
            "llm-call-without-tenant",
            f"llm.{node.func.attr}() with no tenant_id",
            (
                "The per-tenant budget is charged to the tenant a call names; with none, one "
                "tenant can spend the whole service's allowance. Pass `tenant_id=`."
            ),
            source=source,
            line=node.lineno,
            span=_span(node),
        )


def _scheduled(files: Sequence[SourceFile], report: _Reporter) -> None:
    """Work a scheduled task starts, which runs as no tenant.

    A tick is not a request: it has no tenant, so a ``Job`` or ``Event`` built
    inside the handler inherits none, and a ``TaskSession`` it receives is
    scoped to nobody. Scheduled handlers are found two ways -- a task
    decorator carrying ``every=`` or ``cron=``, and ``tasks.schedule("name",
    ...)`` resolved to the ``@tasks.task("name")`` handler by its name.
    """
    handlers: dict[str, list[tuple[SourceFile, Any]]] = {}
    scheduled: list[tuple[SourceFile, Any]] = []
    names: set[str] = set()
    for source in files:
        if source.is_test:
            continue
        for function in _functions(source.tree):
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if _called(decorator.func) != "task":
                    continue
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    handlers.setdefault(str(decorator.args[0].value), []).append((source, function))
                if any(k.arg in ("every", "cron") for k in decorator.keywords):
                    scheduled.append((source, function))
        for node in ast.walk(source.tree):
            if (
                isinstance(node, ast.Call)
                and _called(node.func) == "schedule"
                and any(k.arg in ("every", "cron") for k in node.keywords)
            ):
                task = node.args[0] if node.args else None
                for keyword in node.keywords:
                    if keyword.arg == "task":
                        task = keyword.value
                if isinstance(task, ast.Constant) and isinstance(task.value, str):
                    names.add(task.value)
    for name in names:
        scheduled.extend(handlers.get(name, []))

    for source, function in scheduled:
        arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        for argument in arguments:
            if argument.annotation is not None and "TaskSession" in _text(
                source, argument.annotation
            ):
                report.add(
                    "scheduled-job-without-tenant",
                    (
                        f"scheduled task {function.name}() opens a TaskSession, and a tick "
                        "has no tenant"
                    ),
                    (
                        "The session is scoped to the job's tenant, and a scheduled tick has "
                        "none: today it reads every tenant's rows, and under row-level "
                        "security it reads none. Make the tick a fan-out -- list the tenants "
                        "and enqueue one job each with `tenant_id=` -- and do the work there."
                    ),
                    source=source,
                    line=argument.lineno,
                    span=[function.lineno, argument.lineno],
                )
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and _called(node.func) in ("Job", "Event")
                and not any(k.arg in ("tenant_id", None) for k in node.keywords)
            ):
                kind = _called(node.func)
                report.add(
                    "scheduled-job-without-tenant",
                    f"scheduled task {function.name}() builds a {kind} with no tenant_id",
                    (
                        "A scheduled tick runs as no tenant, so this job does too: its "
                        "repositories are unfiltered today and see no rows under row-level "
                        "security. Loop over the tenants and enqueue one job each, with "
                        "`tenant_id=` set."
                    ),
                    source=source,
                    line=node.lineno,
                    span=_span(node),
                )


def readiness(
    root: Path,
    *,
    config: Mapping[str, Any] | None = None,
    extra_tables: Sequence[str] = (),
) -> Readiness:
    """Everything a switch to multitenant would break in the service at *root*."""
    raw = dict(config) if config is not None else load_config(root)
    files = source_files(root)
    tables = tenant_tables(files)
    table_names = sorted({t.table for t in tables} | set(extra_tables))
    report = _Reporter()

    rag = plugin_table(raw, "rag")
    if "rag" in enabled_plugins(raw) and rag.get("tenant_scoped") is False:
        report.add(
            "rag-unscoped",
            "[plugin.rag] tenant_scoped = false",
            (
                "Every search covers every document, and chunks are stored with no tenant. "
                "`jfast tenancy enable` sets it to true and re-keys the stored chunks to the "
                "initial tenant; every `rag` call must then pass `tenant_id=`."
            ),
            path="jfast.toml",
            line=config_line(root, "plugin.rag", "tenant_scoped"),
        )

    readable = [source for source in files if not source.is_test]
    for source in readable:
        _none_literals(source, report)
        _raw_sql(source, table_names, report)
        _keys(source, report)
        _llm_calls(source, report)
    _routes(readable, report)
    _scheduled(readable, report)

    items = sorted(
        report.items,
        key=lambda item: (
            SEVERITY_ORDER.index(item.finding.severity),
            item.finding.path or "",
            item.finding.line or 0,
            item.finding.code,
        ),
    )
    return Readiness(findings=tuple(items), tables=tuple(tables), files_read=len(readable))
