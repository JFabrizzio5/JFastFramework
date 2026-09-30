"""Tenant settings that contradict each other, or the code.

Isolation is configured piece by piece -- the ``tenancy`` plugin, ``[plugin.rag]
tenant_scoped``, ``[plugin.llm] tenant_budget_usd``, ``[plugin.database] rls``,
``current_tenant`` in a route -- and every piece is valid on its own. The
failures are between them: a store that demands a tenant in a service that
never resolves one, a policy that reads a setting no session ever sets. None of
them stops the service from starting. Each one surfaces later, as a 403 on
every request, an empty table, or a budget that never applies.

So this reads them together. It is a check of ``jfast check`` -- the
``tenancy`` section -- and every finding names the change that removes it.

It reads ``jfast.toml`` and, when ``jfast check`` has built the plugins, their
resolved settings, so an environment override is seen too. It never connects
to anything.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jfastframework.multitenant._source import (
    SourceFile,
    config_line,
    enabled_plugins,
    load_config,
    plugin_table,
    source_files,
    waiver_for,
)
from jfastframework.project import Finding

__all__ = ["DEFAULTS", "consistency_findings", "tenant_source"]

CONFIG_FILE = "jfast.toml"

#: The defaults this reads when a setting is not written, by plugin. Kept next
#: to each other so a changed default in a plugin is one line here -- and
#: pinned by a test against the plugins' own Settings classes, so the two
#: cannot drift without a failure.
DEFAULTS: dict[str, dict[str, Any]] = {
    "tenancy": {"sources": ["token", "subdomain"], "base_domain": ""},
    "rag": {"tenant_scoped": True, "store": "pgvector", "collection": "rag_chunks"},
    "llm": {"tenant_budget_usd": 0.0},
    "database": {"rls": False},
    "auth": {"tenant_claim": "tenant_id"},
}

#: What makes a tenancy source able to resolve anything, for the ones that need
#: something beyond the plugin itself.
NEEDS_AUTH = ("token", "user")


class _Settings:
    """One setting, from the built plugin when there is one, else from the file.

    The built plugin wins because it has read the environment too:
    ``JFAST_RAG_TENANT_SCOPED=false`` in a deployment is invisible in
    jfast.toml, and a finding that contradicts what the service actually runs
    with is worse than none.
    """

    def __init__(self, config: Mapping[str, Any], built: Mapping[str, Any] | None) -> None:
        self._config = dict(config)
        self._built = dict(built or {})

    def get(self, plugin: str, key: str) -> Any:
        settings = self._built.get(plugin)
        if settings is not None and hasattr(settings, key):
            return getattr(settings, key)
        table = plugin_table(self._config, plugin)
        if key in table:
            return table[key]
        return DEFAULTS[plugin][key]


def tenant_source(enabled: Sequence[str], settings: _Settings) -> str | None:
    """What can resolve a request's tenant here: ``tenancy``, ``claim`` or None.

    ``claim`` is the fallback ``current_tenant`` and the auth middleware share:
    with the auth plugin on and a ``tenant_claim`` configured, a token that
    carries the claim scopes the request even without the tenancy plugin.
    Whether the tokens really carry it is a fact about the identity provider,
    which is why a finding that rests on it is softer than one that does not.
    """
    if "tenancy" in enabled:
        return "tenancy"
    if "auth" in enabled and str(settings.get("auth", "tenant_claim") or ""):
        return "claim"
    return None


def _current_tenant_uses(files: Sequence[SourceFile]) -> list[tuple[SourceFile, int]]:
    """Every place code reads ``current_tenant`` -- the name, not its import."""
    found: list[tuple[SourceFile, int]] = []
    for source in files:
        for node in ast.walk(source.tree):
            used = (isinstance(node, ast.Name) and node.id == "current_tenant") or (
                isinstance(node, ast.Attribute) and node.attr == "current_tenant"
            )
            if used and isinstance(getattr(node, "ctx", None), ast.Load):
                found.append((source, node.lineno))
    return found


def _rls_policies(root: Path) -> list[tuple[SourceFile, int]]:
    """Revisions that put a table under a policy reading ``jfast.tenant_id``."""
    found: list[tuple[SourceFile, int]] = []
    for source in source_files(root, include_migrations=True):
        if not source.path.startswith("migrations/"):
            continue
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ("enable_tenant_rls", "enable_rls_policy"):
                found.append((source, node.lineno))
                break
    return found


def consistency_findings(
    root: Path,
    *,
    config: Mapping[str, Any] | None = None,
    enabled: Sequence[str] | None = None,
    built: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """Every contradiction between the tenant settings and the code, worst first.

    ``config`` is jfast.toml as data (read from *root* when omitted),
    ``enabled`` the resolved plugin list, and ``built`` each built plugin's
    settings object by name -- all three optional, so the check runs with or
    without the plugin graph.
    """
    raw = dict(config) if config is not None else load_config(root)
    plugins = tuple(enabled) if enabled is not None else enabled_plugins(raw)
    settings = _Settings(raw, built)
    source = tenant_source(plugins, settings)
    findings: list[Finding] = []

    # -- a store that demands a tenant nobody resolves ------------------
    if "rag" in plugins and "tenancy" not in plugins and settings.get("rag", "tenant_scoped"):
        findings.append(
            Finding(
                severity="medium",
                code="tenancy-rag-scoped-without-tenancy",
                message=(
                    "[plugin.rag] tenant_scoped is true (the default) and the tenancy plugin is off"
                ),
                why=(
                    "A tenant-scoped store refuses every ingest and search without a tenant, and "
                    "nothing in this service resolves one per request -- so either every call "
                    "passes one it invented, or every call fails with TenantRequiredError. One "
                    "customer: set `tenant_scoped = false` under [plugin.rag]. Several: `jfast "
                    "tenancy enable --tenant <id>` turns tenancy on and keeps the store scoped."
                ),
                path=CONFIG_FILE,
                line=config_line(root, "plugin.rag", "tenant_scoped")
                or config_line(root, "plugin.rag"),
            )
        )

    # -- a budget per tenant with no tenant -----------------------------
    budget = float(settings.get("llm", "tenant_budget_usd") or 0)
    if "llm" in plugins and "tenancy" not in plugins and budget > 0:
        findings.append(
            Finding(
                severity="medium",
                code="tenancy-budget-without-tenancy",
                message=(
                    f"[plugin.llm] tenant_budget_usd = {budget:g} and the tenancy plugin is off"
                ),
                why=(
                    "The per-tenant cap is charged to the tenant a call passes, and without "
                    "tenancy the calls pass none: the only cap that applies is the global one, "
                    "and the setting reads as protection it does not give. One customer: drop "
                    "tenant_budget_usd and size `budget_usd`. Several: `jfast tenancy enable`, "
                    "and pass `tenant_id=` to every llm call."
                ),
                path=CONFIG_FILE,
                line=config_line(root, "plugin.llm", "tenant_budget_usd"),
            )
        )

    # -- row-level security with nothing to set the tenant --------------
    rls = bool(settings.get("database", "rls"))
    if "database" in plugins and rls and source != "tenancy":
        soft = source == "claim"
        findings.append(
            Finding(
                severity="medium" if soft else "high",
                code="tenancy-rls-without-tenancy",
                message="[plugin.database] rls = true and the tenancy plugin is off",
                why=(
                    (
                        "Only a token carrying the `tenant_id` claim sets the tenant here; a "
                        "request whose token lacks it runs with none and every policy table "
                        "reads empty. If your identity provider always sets the claim, say so: "
                        '`[plugin.tenancy] sources = ["token"]`. Otherwise `jfast tenancy '
                        "enable` configures the rest."
                    )
                    if soft
                    else (
                        "Every transaction tells PostgreSQL its tenant, and nothing in this "
                        "service resolves one -- no tenancy plugin, no auth claim -- so every "
                        "table under a policy returns no rows and refuses every write. One "
                        "customer: set `rls = false`. Several: `jfast tenancy enable --tenant "
                        "<id>`."
                    )
                ),
                path=CONFIG_FILE,
                line=config_line(root, "plugin.database", "rls"),
            )
        )

    # -- policies in the schema that no session feeds -------------------
    if "database" in plugins and not rls:
        policies = _rls_policies(root)
        if policies:
            first, line = policies[0]
            findings.append(
                Finding(
                    severity="high",
                    code="tenancy-policies-without-rls",
                    message=(
                        f"{len(policies)} revision(s) put tables under a tenant policy, and "
                        "[plugin.database] rls is false"
                    ),
                    why=(
                        "The policy reads `jfast.tenant_id`, and only a session with `rls = "
                        "true` sets it: with it off, every query against those tables sees no "
                        "rows and every write is refused -- for the service's own role, since "
                        "FORCE applies the policy to the owner too. Set `rls = true` under "
                        "[plugin.database], or drop the policy in a new revision."
                    ),
                    path=first.path,
                    line=line,
                )
            )

    files = source_files(root)

    # -- current_tenant in a service that cannot answer it --------------
    if source is None:
        uses = [
            (file, line)
            for file, line in _current_tenant_uses(files)
            if waiver_for(file.lines, line) is None
        ]
        if uses:
            first, line = uses[0]
            more = f" (and {len(uses) - 1} more)" if len(uses) > 1 else ""
            findings.append(
                Finding(
                    severity="high",
                    code="tenancy-current-tenant-without-source",
                    message=f"`current_tenant` is used here{more}, and nothing resolves a tenant",
                    why=(
                        "There is no tenancy plugin and no auth plugin to read a `tenant_id` "
                        "claim, so `current_tenant` answers 401 or 403 to every request that "
                        "reaches it. One customer: depend on `require_auth` instead and drop "
                        "the tenant. Several: `jfast tenancy enable --tenant <id>`."
                    ),
                    path=first.path,
                    line=line,
                )
            )

    # -- tenancy on, with sources that can never answer -----------------
    if "tenancy" in plugins:
        sources = list(settings.get("tenancy", "sources") or [])
        line = config_line(root, "plugin.tenancy", "sources") or config_line(root, "plugin.tenancy")
        if "subdomain" in sources and not str(settings.get("tenancy", "base_domain") or ""):
            findings.append(
                Finding(
                    severity="high",
                    code="tenancy-source-unresolvable",
                    message='tenancy source "subdomain" has no [plugin.tenancy] base_domain',
                    why=(
                        "Without the base domain every hostname looks like a tenant, so the "
                        'plugin refuses to start. Set `base_domain = "app.example.com"`, or '
                        'remove "subdomain" from sources (the default list includes it).'
                    ),
                    path=CONFIG_FILE,
                    line=line,
                )
            )
        needing = [name for name in sources if name in NEEDS_AUTH]
        if needing and "auth" not in plugins:
            findings.append(
                Finding(
                    severity="high",
                    code="tenancy-source-unresolvable",
                    message=(
                        f"tenancy sources {', '.join(needing)} need the auth plugin, and it is off"
                    ),
                    why=(
                        "`token` reads a signed claim and `user` the signed-in subject; with no "
                        "auth plugin there is never a principal, so neither ever resolves and "
                        'the service runs as if it had no tenancy. Add "auth" to '
                        "[plugins].enabled, or choose sources this service can answer "
                        '(`"subdomain"` with a base_domain).'
                    ),
                    path=CONFIG_FILE,
                    line=line,
                )
            )

    order = ("critical", "high", "medium", "low")
    findings.sort(key=lambda f: (order.index(f.severity), f.code))
    return findings
