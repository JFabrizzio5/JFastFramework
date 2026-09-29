"""PostgreSQL row-level security for tenant tables.

The repository filters on ``tenant_id``, and that filter is a convention: raw
SQL, a join through an unscoped table, a job without a tenant -- any of them
reads every tenant's rows and nothing complains. Row-level security moves the
rule into the database, where a query that forgets the filter gets no rows
instead of somebody else's.

Two halves, both needed:

1. **A policy on each tenant table**, written in a migration::

       from jfastframework.db.rls import enable_tenant_rls, disable_tenant_rls

       def upgrade() -> None:
           enable_tenant_rls(op, "invoices")

       def downgrade() -> None:
           disable_tenant_rls(op, "invoices")

2. **The tenant, set on every transaction.** With ``[plugin.database] rls =
   true`` each session runs ``set_config('jfast.tenant_id', ..., true)`` as its
   transaction begins, from the request's tenant -- or a queue job's, which
   the worker restores. ``true`` makes it transaction-local, so a pooled
   connection never carries one tenant into the next request, and it is safe
   behind PgBouncer in transaction mode.

A transaction with no tenant sees no rows of an RLS table and cannot write
one. That is the point, and it is also why cross-tenant work -- a report over
every tenant, a data migration -- has to say so: :func:`bypass_rls`, allowed
only on policies created with ``allow_bypass=True``.

**The role matters.** A superuser, or a role with ``BYPASSRLS``, ignores every
policy. The generated compose file connects as the database's superuser, so
turning RLS on there changes nothing until the service connects as a role of
its own. :func:`role_problem` checks, and the database plugin refuses to start
in production when it finds one.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from jfastframework.sql import safe_identifier

__all__ = [
    "POLICY",
    "TENANT_SETTING",
    "TenantScopedSession",
    "bypass_rls",
    "disable_tenant_rls",
    "enable_tenant_rls",
    "role_problem",
    "tenant_policy_sql",
]

POLICY = "jfast_tenant_isolation"
TENANT_SETTING = "jfast.tenant_id"
BYPASS_SETTING = "jfast.rls_bypass"

_bypass: ContextVar[bool] = ContextVar("jfast_rls_bypass", default=False)


def tenant_policy_sql(
    table: str, *, column: str = "tenant_id", allow_bypass: bool = False
) -> list[str]:
    """The statements that put ``table`` under tenant isolation.

    ``FORCE`` applies the policy to the table's owner too. Without it the role
    that ran the migration -- usually the one the service connects as -- is
    exempt, and the policy protects nothing.
    """
    table = safe_identifier(table, kind="table")
    column = safe_identifier(column, kind="column")
    matches = f"{column} = current_setting('{TENANT_SETTING}', true)"
    if allow_bypass:
        matches = f"({matches} OR current_setting('{BYPASS_SETTING}', true) = 'on')"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {POLICY} ON {table}",
        f"CREATE POLICY {POLICY} ON {table} USING ({matches}) WITH CHECK ({matches})",
    ]


def enable_tenant_rls(
    op: Any, table: str, *, column: str = "tenant_id", allow_bypass: bool = False
) -> None:
    """Alembic helper: put ``table`` under tenant isolation."""
    for statement in tenant_policy_sql(table, column=column, allow_bypass=allow_bypass):
        op.execute(statement)


def disable_tenant_rls(op: Any, table: str) -> None:
    """Alembic helper: the inverse of :func:`enable_tenant_rls`."""
    table = safe_identifier(table, kind="table")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


@contextlib.contextmanager
def bypass_rls() -> Iterator[None]:
    """Let sessions opened inside this block read and write across tenants.

    Only on tables whose policy was created with ``allow_bypass=True``; on the
    others it changes nothing, which is the safe way for it to fail. Use it
    for work that is about every tenant by definition, and keep it narrow::

        with bypass_rls():
            async with sessionmaker() as session:
                totals = await session.execute(select(...))
    """
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


class TenantScopedSession(Session):
    """A session that tells PostgreSQL which tenant each transaction is for.

    The tenant is ``session.info["tenant_id"]`` when set, otherwise the
    request's or job's tenant from the observability context.
    """


@event.listens_for(TenantScopedSession, "after_begin")
def _scope_transaction(session: Session, transaction: Any, connection: Any) -> None:
    if connection.dialect.name != "postgresql":
        return
    from jfastframework.plugins.builtin.observability import tenant_id_var

    tenant = session.info.get("tenant_id") or tenant_id_var.get()
    if tenant:
        connection.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": TENANT_SETTING, "value": str(tenant)},
        )
    if _bypass.get():
        connection.execute(text("SELECT set_config(:name, 'on', true)"), {"name": BYPASS_SETTING})


async def role_problem(engine: Any) -> str | None:
    """Why row-level security would not apply to this engine's role, if it would not."""
    if engine.dialect.name != "postgresql":
        return None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    user, superuser, bypass = row
    if superuser:
        return f"role {user!r} is a superuser, and superusers ignore row-level security"
    if bypass:
        return f"role {user!r} has BYPASSRLS, so row-level security does not apply to it"
    return None
