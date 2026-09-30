"""`jfast tenancy enable`: the switch from one customer to several, as files.

What the switch is, in the order it has to happen:

1. **Every existing row gets an owner.** A single-tenant service wrote
   ``tenant_id = NULL``, and after the switch a row with no tenant belongs to
   nobody: no request can see it. So the migration backfills NULL with the
   initial tenant -- the customer the service has served until now.
2. **Row-level security goes on**, after the backfill and never before it:
   ``FORCE ROW LEVEL SECURITY`` binds the migration's own role too, and a
   migration sets no tenant, so an ``UPDATE`` after the policy matches nothing.
3. **The RAG chunks are re-keyed** the same way, if the table exists, and put
   under the same policy.
4. **jfast.toml says so**: the tenancy plugin on, ``rls = true``, the store
   tenant-scoped.

Everything is generated as text a person reads before it runs. Nothing here
connects to a database, and nothing is applied: the command writes a revision,
and ``alembic upgrade head`` is still the step that changes data.

What cannot be generated is printed instead -- the routes to move to
``current_tenant``, the database role the policies bind -- from the same
report ``jfast check --multitenant-ready`` gives.
"""

from __future__ import annotations

import ast
import re
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jfastframework.multitenant._source import (
    TenantTable,
    enabled_plugins,
    plugin_table,
    source_files,
    tenant_tables,
)
from jfastframework.sql import safe_identifier

__all__ = [
    "MARKER",
    "SwitchError",
    "SwitchPlan",
    "edit_config",
    "plan_switch",
    "render_migration",
    "validate_tenant",
]

#: Written into the generated revision, so a second run can tell the switch
#: already happened instead of generating a second, conflicting one.
MARKER = "jfast:tenancy-enable"

DEFAULT_SOURCES = ("token", "user")

# The `user` source's rule, which is the loosest the tenancy plugin accepts: an
# identity provider's user id (a UUID, `auth0|abc`). The initial tenant ends up
# inside SQL in the generated revision, so this is what keeps it a literal.
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@|-]{0,127}$")


class SwitchError(ValueError):
    """The switch cannot be planned, with the fix in the message."""


def validate_tenant(tenant: str) -> str:
    if not _TENANT.match(tenant):
        raise SwitchError(
            f"{tenant!r} is not a usable tenant id: letters, digits and _ . : @ | -, starting "
            "with a letter or digit, up to 128 characters -- what the tenancy plugin accepts "
            "from a token."
        )
    return tenant


@dataclass(frozen=True)
class SwitchPlan:
    """Everything the command would write, before it writes anything."""

    tenant: str
    #: The models that carry the column, with where each is declared.
    tables: tuple[TenantTable, ...]
    #: Every table the revision touches: the models' plus any named by hand.
    table_names: tuple[str, ...]
    rag_table: str | None
    not_null: bool
    sources: tuple[str, ...]
    base_domain: str
    revision: str
    down_revision: str | None
    migration_path: Path
    migration: str
    config_before: str
    config_after: str


# ---------------------------------------------------------------------------
# Revisions on disk
# ---------------------------------------------------------------------------


def _versions_dir(root: Path) -> Path | None:
    from jfastframework.cli.migrations import versions_dir

    return versions_dir(root)


def _module_value(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            value = node.value
        if value is not None:
            try:
                return ast.literal_eval(value)
            except ValueError:
                return None
    return None


def heads(directory: Path) -> tuple[list[str], bool]:
    """``(head revision ids, whether a previous switch is among the revisions)``.

    Read from the files, not from alembic: the environment ``jfast`` runs in is
    not necessarily the one the revisions import in. A merge revision's tuple
    ``down_revision`` is honoured, which is the case a hand-rolled reader gets
    wrong and reports two heads for a tree that has one.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    switched = False
    for path in sorted(directory.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError):
            continue
        revision = _module_value(tree, "revision")
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        down = _module_value(tree, "down_revision")
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, tuple | list):
            parents.update(item for item in down if isinstance(item, str))
        switched = switched or MARKER in text
    return sorted(revisions - parents), switched


# ---------------------------------------------------------------------------
# The revision
# ---------------------------------------------------------------------------


def _rag_statements(table: str, tenant: str) -> list[str]:
    """The chunks table's re-key and policy, guarded on the table existing.

    The rag plugin creates its table at startup rather than in a revision, so a
    service can have migrated to head and still not have one. A ``DO`` block
    decides on the server, which keeps ``alembic upgrade --sql`` working: the
    alternative, asking the connection, has no connection offline.
    """
    pieces = [
        f"DO $$ BEGIN IF to_regclass('{table}') IS NOT NULL THEN ",
        # nosec B608 below: callers pass safe_identifier(table) and validate_tenant(tenant),
        # whose alphabet has no quote -- and this is text written into a revision file.
        f"UPDATE {table} SET tenant_id = '{tenant}' WHERE tenant_id IS NULL; ",  # nosec B608
    ]
    # Here, not at the top: jfastframework.db needs SQLAlchemy (the `db`
    # extra), and the CLI imports this module on every `jfast` command --
    # a top-level import broke `jfast version` on a bare install.
    from jfastframework.db.rls import tenant_policy_sql

    for statement in tenant_policy_sql(table):
        # One clause a line, so the literal stays readable in a diff.
        for clause in re.split(r"(?= USING \(| WITH CHECK \()", statement):
            pieces.append(clause)
        pieces[-1] += "; "
    pieces.append("END IF; END $$")
    return pieces


def _py_str(value: str) -> str:
    """A double-quoted Python literal, the way ruff format writes one."""
    if '"' not in value:
        return '"' + value.replace("\\", "\\\\") + '"'
    return repr(value)


def render_migration(
    *,
    tenant: str,
    tables: list[str],
    rag_table: str | None,
    not_null: bool,
    revision: str,
    down_revision: str | None,
    created: datetime | None = None,
) -> str:
    """The revision's source. Explicit statements, one per table, no loops.

    Written to be read: every table is a line a reviewer can strike out, and
    ``jfast migration check`` reads literal statements where it could not read
    a loop.
    """
    tenant = validate_tenant(tenant)
    tables = [safe_identifier(table, kind="table") for table in tables]
    if rag_table is not None:
        rag_table = safe_identifier(rag_table, kind="rag table")
    stamp = (created or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S")
    names = ", ".join(tables) or "no model tables"

    upgrade: list[str] = []
    downgrade: list[str] = []
    for table in tables:
        upgrade.append(
            f"    op.execute(\n"
            # Source text for the revision file; the table went through safe_identifier.
            f'        sa.text("UPDATE {table} SET tenant_id = :tenant WHERE tenant_id IS NULL")'  # nosec B608
            f".bindparams(\n"
            f"            tenant=INITIAL_TENANT\n"
            f"        )\n"
            f"    )"
        )
        if not_null:
            upgrade.append(f'    op.alter_column("{table}", "tenant_id", nullable=False)')
        upgrade.append(f'    enable_tenant_rls(op, "{table}")')
        upgrade.append("")
        downgrade.append(f'    disable_tenant_rls(op, "{table}")')
        if not_null:
            downgrade.append(f'    op.alter_column("{table}", "tenant_id", nullable=True)')
    if rag_table is not None:
        upgrade.append(
            "    # The chunks of every document ingested so far become the initial\n"
            "    # tenant's, and the table goes under the same policy. The rag plugin\n"
            "    # sets jfast.tenant_id itself on each call, so it keeps working.\n"
            "    op.execute(RAG_REKEY)"
        )
        downgrade.append(
            "    op.execute(\n"
            f"        \"DO $$ BEGIN IF to_regclass('{rag_table}') IS NOT NULL THEN \"\n"
            f'        "DROP POLICY IF EXISTS jfast_tenant_isolation ON {rag_table}; "\n'
            f'        "ALTER TABLE {rag_table} NO FORCE ROW LEVEL SECURITY; "\n'
            f'        "ALTER TABLE {rag_table} DISABLE ROW LEVEL SECURITY; END IF; END $$"\n'
            "    )"
        )
    while upgrade and upgrade[-1] == "":
        upgrade.pop()

    rag_constant = ""
    if rag_table is not None:
        pieces = "\n".join(f"    {_py_str(piece)}" for piece in _rag_statements(rag_table, tenant))
        rag_constant = f"\nRAG_REKEY = (\n{pieces}\n)\n"

    down = _py_str(down_revision) if down_revision is not None else "None"
    sa_import = "\nimport sqlalchemy as sa" if tables else ""
    rls_import = (
        "\nfrom jfastframework.db.rls import disable_tenant_rls, enable_tenant_rls\n"
        if tables
        else ""
    )
    not_null_note = (
        "\n``tenant_id`` becomes NOT NULL on every table. Declare it on each model too\n"
        "(``tenant_id: Mapped[str] = mapped_column(index=True)``), or the next\n"
        "``alembic revision --autogenerate`` makes it nullable again.\n"
        if not_null
        else ""
    )
    return f'''"""Switch to multitenant: rows to tenant {tenant!r}, row-level security on.

Generated by `jfast tenancy enable --tenant {tenant}` ({MARKER}). Read it
before applying it -- this is the one migration that decides who owns every
row the service has written so far.

Tables: {names}.

The order is the point. Each table is backfilled *before* its policy exists:
FORCE ROW LEVEL SECURITY binds this migration's role too, and a migration sets
no tenant, so an UPDATE after the policy would match no rows.
{not_null_note}
The downgrade removes the policies and leaves the backfilled tenant in place:
a single-tenant service reads those rows with no filter either way, and
guessing which rows were NULL before would be a second, silent data change.

Revision ID: {revision}
Revises:{" " + down_revision if down_revision else ""}
Create Date: {stamp}
"""

from __future__ import annotations
{sa_import}
from alembic import op
{rls_import}
revision: str = {_py_str(revision)}
down_revision: str | None = {down}
branch_labels: str | None = None
depends_on: str | None = None

INITIAL_TENANT = {_py_str(tenant)}
{rag_constant}

def upgrade() -> None:
{chr(10).join(upgrade) if upgrade else "    pass"}


def downgrade() -> None:
{chr(10).join(downgrade) if downgrade else "    pass"}
'''


# ---------------------------------------------------------------------------
# jfast.toml
# ---------------------------------------------------------------------------

_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*(#.*)?$")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return str(value)


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """``(name, header line index, end index exclusive)`` for every table."""
    found: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match:
            found.append((match.group(1), index))
    return [
        (name, start, found[i + 1][1] if i + 1 < len(found) else len(lines))
        for i, (name, start) in enumerate(found)
    ]


def _last_content(lines: list[str], start: int, end: int) -> int:
    """The index of the last key line in a section, so an insert lands inside it."""
    last = start
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            last = index
    return last


def _key_span(lines: list[str], start: int, end: int, key: str) -> tuple[int, int] | None:
    """Where ``key = ...`` sits in a section, including a multi-line array."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if not pattern.match(lines[index]):
            continue
        stop = index
        text = lines[index].split("#", 1)[0]
        if "[" in text and text.count("[") > text.count("]"):
            depth = text.count("[") - text.count("]")
            while depth > 0 and stop + 1 < end:
                stop += 1
                piece = lines[stop].split("#", 1)[0]
                depth += piece.count("[") - piece.count("]")
        return index, stop
    return None


def _set_key(lines: list[str], section: str, key: str, value: Any, *, comment: str = "") -> None:
    """Set ``key`` in ``[section]``, creating the section after its siblings.

    A line editor rather than a TOML writer: jfast.toml is mostly comments that
    explain its settings, and a round trip through a parser that drops them is
    a worse file than the one it started from.
    """
    rendered = f"{key} = {_toml_value(value)}"
    sections = _sections(lines)
    for name, start, end in sections:
        if name != section:
            continue
        span = _key_span(lines, start, end, key)
        if span is not None:
            first, last = span
            trailing = ""
            if first == last and "#" in lines[first].split("=", 1)[1]:
                # Keep a comment on the same line, if the value itself had none.
                value_part = lines[first].split("=", 1)[1]
                if value_part.count('"') % 2 == 0:
                    trailing = "  #" + value_part.split("#", 1)[1]
            lines[first : last + 1] = [rendered + trailing]
        else:
            at = _last_content(lines, start, end) + 1
            block = ([f"# {comment}"] if comment else []) + [rendered]
            lines[at:at] = block
        return

    # A new section goes after the last `[plugin.*]` one, where a reader looks
    # for it, or after `[plugins]` when there is none.
    anchor = None
    for name, start, end in sections:
        if name.startswith("plugin.") or name == "plugins":
            anchor = _last_content(lines, start, end) + 1
    block = ["", f"[{section}]"] + ([f"# {comment}"] if comment else []) + [rendered]
    if anchor is None:
        lines.extend(block)
    else:
        lines[anchor:anchor] = block


def edit_config(
    text: str,
    *,
    sources: list[str] | None,
    base_domain: str = "",
    rag: bool,
) -> str:
    """jfast.toml with tenancy on, row-level security on and the store scoped.

    ``sources`` None keeps a ``[plugin.tenancy] sources`` already written, and
    writes the default otherwise. The result is parsed before it is returned,
    and an edit that did not land raises rather than writing a file that says
    something other than what was printed.
    """
    lines = text.splitlines()
    config = tomllib.loads(text)
    plugins = config.get("plugins", {}) if isinstance(config.get("plugins"), dict) else {}
    enabled = list(plugins.get("enabled", []) or [])
    if "tenancy" not in enabled:
        # After auth, which is where the plugin graph orders it anyway: a list
        # read top to bottom should agree with the order things happen.
        at = enabled.index("auth") + 1 if "auth" in enabled else len(enabled)
        enabled.insert(at, "tenancy")
    _set_key(lines, "plugins", "enabled", enabled)
    disabled = list(plugins.get("disabled", []) or [])
    if "tenancy" in disabled:
        _set_key(lines, "plugins", "disabled", [d for d in disabled if d != "tenancy"])

    existing = plugin_table(config, "tenancy").get("sources")
    chosen = list(sources) if sources is not None else list(existing or DEFAULT_SOURCES)
    _set_key(
        lines,
        "plugin.tenancy",
        "sources",
        chosen,
        comment="Ordered by trust: the first source that resolves a tenant wins.",
    )
    if base_domain:
        _set_key(lines, "plugin.tenancy", "base_domain", base_domain)
    _set_key(
        lines,
        "plugin.database",
        "rls",
        True,
        comment="Every transaction tells PostgreSQL its tenant; see docs/multitenancy.md.",
    )
    if rag:
        _set_key(lines, "plugin.rag", "tenant_scoped", True)

    result = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    try:
        parsed = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise SwitchError(
            f"editing jfast.toml produced a file that does not parse ({exc}). Make the "
            "changes by hand: the list is printed under --dry-run."
        ) from exc
    checks: list[tuple[str, Any, Any]] = [
        ("tenancy enabled", "tenancy" in enabled_plugins(parsed), True),
        ("tenancy sources", plugin_table(parsed, "tenancy").get("sources"), chosen),
        ("database rls", plugin_table(parsed, "database").get("rls"), True),
    ]
    if rag:
        checks.append(("rag tenant_scoped", plugin_table(parsed, "rag").get("tenant_scoped"), True))
    wrong = [name for name, got, want in checks if got != want]
    if wrong:
        raise SwitchError(
            f"editing jfast.toml did not set {', '.join(wrong)}. Make the changes by hand: "
            "the list is printed under --dry-run."
        )
    return result


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _rag_table(config: dict[str, Any]) -> str | None:
    """The chunks table, when this service keeps its RAG chunks in PostgreSQL."""
    if "rag" not in enabled_plugins(config):
        return None
    rag = plugin_table(config, "rag")
    if str(rag.get("store", "pgvector")) != "pgvector":
        return None
    return str(rag.get("collection", "rag_chunks"))


def plan_switch(
    root: Path,
    *,
    tenant: str,
    not_null: bool = False,
    sources: list[str] | None = None,
    base_domain: str = "",
    rag_table: str | None = None,
    skip_rag: bool = False,
    extra_tables: list[str] | None = None,
    created: datetime | None = None,
    revision: str | None = None,
) -> SwitchPlan:
    """Everything `jfast tenancy enable` would write, without writing it."""
    tenant = validate_tenant(tenant)
    config_path = root / "jfast.toml"
    try:
        before = config_path.read_text(encoding="utf-8")
        config = tomllib.loads(before)
    except OSError as exc:
        raise SwitchError(f"no jfast.toml in {root}. Run this inside a service.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SwitchError(f"jfast.toml does not parse ({exc}). Fix it first.") from exc

    enabled = enabled_plugins(config)
    if "database" not in enabled:
        raise SwitchError(
            "this service has no database plugin, so there is nothing to backfill and no "
            "policy to create. Enable tenancy by hand: add it to [plugins].enabled."
        )

    existing = plugin_table(config, "tenancy").get("sources")
    chosen = list(sources) if sources is not None else list(existing or DEFAULT_SOURCES)
    if not chosen:
        raise SwitchError("--sources is empty; the plugin would resolve nothing.")
    unknown = sorted(set(chosen) - {"token", "user", "subdomain", "path", "header"})
    if unknown:
        raise SwitchError(
            f"unknown tenancy source(s): {', '.join(unknown)}. Choose from token, user, "
            "subdomain, path, header."
        )
    needing = [name for name in chosen if name in ("token", "user")]
    if needing and "auth" not in enabled:
        raise SwitchError(
            f"sources {', '.join(needing)} read the signed-in principal, and the auth plugin "
            'is off: nothing would ever resolve. Add "auth" to [plugins].enabled first, or '
            "pass --sources subdomain --base-domain <domain>."
        )
    current_domain = str(plugin_table(config, "tenancy").get("base_domain", "") or "")
    if "subdomain" in chosen and not (base_domain or current_domain):
        raise SwitchError(
            'source "subdomain" needs a base domain, or every hostname looks like a tenant. '
            "Pass --base-domain app.example.com."
        )

    directory = _versions_dir(root)
    if directory is None:
        raise SwitchError(
            f"no migrations/versions under {root}. The switch is a revision on top of the "
            "one that created the tables: create that first "
            "(jfast exec -- alembic revision --autogenerate)."
        )
    found_heads, switched = heads(directory)
    if switched:
        raise SwitchError(
            f"a revision in {directory.relative_to(root)} already carries {MARKER}: this "
            "service was switched before. Delete that revision if it was never applied, or "
            "write the next change by hand."
        )
    if not found_heads:
        raise SwitchError(
            f"no revision under {directory.relative_to(root)} yet, so nothing has created the "
            "tables this would backfill. Create them first (jfast exec -- alembic revision "
            "--autogenerate, then jfast exec -- alembic upgrade head), then run this again."
        )
    if len(found_heads) > 1:
        raise SwitchError(
            f"the revisions have {len(found_heads)} heads ({', '.join(found_heads)}). Merge "
            "them first -- `jfast exec -- alembic merge heads` -- so the switch has one parent."
        )

    tables = tuple(tenant_tables(source_files(root)))
    names = sorted({t.table for t in tables} | set(extra_tables or ()))
    rag = None if skip_rag else (rag_table if rag_table is not None else _rag_table(config))
    if not names and rag is None:
        raise SwitchError(
            "no model here carries a tenant_id column (TenantMixin), and there is no RAG "
            "table: there is nothing to switch. Add TenantMixin to the models that hold a "
            "customer's data, or name tables with --table."
        )

    revision_id = revision or uuid.uuid4().hex[:12]
    down = found_heads[0] if found_heads else None
    migration = render_migration(
        tenant=tenant,
        tables=names,
        rag_table=rag,
        not_null=not_null,
        revision=revision_id,
        down_revision=down,
        created=created,
    )
    after = edit_config(
        before,
        sources=list(sources) if sources is not None else None,
        base_domain=base_domain,
        rag="rag" in enabled,
    )
    return SwitchPlan(
        tenant=tenant,
        tables=tables,
        table_names=tuple(names),
        rag_table=rag,
        not_null=not_null,
        sources=tuple(chosen),
        base_domain=base_domain or current_domain,
        revision=revision_id,
        down_revision=down,
        migration_path=directory / f"{revision_id}_switch_to_multitenant.py",
        migration=migration,
        config_before=before,
        config_after=after,
    )
