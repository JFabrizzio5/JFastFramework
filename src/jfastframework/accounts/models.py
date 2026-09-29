"""The accounts tables: users, roles, and what each role may do.

Framework-owned (``jfast_*``), created by the accounts plugin at startup and
left alone by the service's Alembic history. A permission is a string such as
``invoices:write``; a role is a named set of them; a user holds roles. Tokens
carry the permissions as scopes, so ``require_scopes`` -- or its alias
``require_permission`` -- is the whole authorization check on a route.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
)

from jfastframework.db.base import UTCDateTime
from jfastframework.db.framework import framework_metadata

__all__ = ["ACCOUNT_TABLES", "role_permissions", "roles", "user_roles", "users"]

users = Table(
    "jfast_users",
    framework_metadata,
    Column("id", String(32), primary_key=True),
    # NULL for a service with no tenants, or for a platform-level user.
    Column("tenant_id", String(255)),
    # Stored lower-cased: two spellings of one address are one person.
    Column("email", String(320), nullable=False),
    # NULL for a user who only ever signs in through a provider.
    Column("password_hash", String(255)),
    Column("display_name", String(200)),
    # "google:1234..." -- see OIDCIdentity.federated_id.
    Column("federated_id", String(320), unique=True),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("failed_logins", Integer, nullable=False, default=0),
    Column("locked_until", UTCDateTime),
    Column("last_login_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Column("updated_at", UTCDateTime, nullable=False, server_default=func.now()),
    UniqueConstraint(
        "tenant_id", "email", name="uq_jfast_users_tenant_email", postgresql_nulls_not_distinct=True
    ),
)

roles = Table(
    "jfast_roles",
    framework_metadata,
    Column("id", String(32), primary_key=True),
    Column("tenant_id", String(255)),
    Column("name", String(100), nullable=False),
    Column("description", String(500)),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    UniqueConstraint(
        "tenant_id", "name", name="uq_jfast_roles_tenant_name", postgresql_nulls_not_distinct=True
    ),
)

role_permissions = Table(
    "jfast_role_permissions",
    framework_metadata,
    Column("role_id", String(32), ForeignKey("jfast_roles.id", ondelete="CASCADE"), nullable=False),
    Column("permission", String(200), nullable=False),
    PrimaryKeyConstraint("role_id", "permission"),
)

user_roles = Table(
    "jfast_user_roles",
    framework_metadata,
    Column("user_id", String(32), ForeignKey("jfast_users.id", ondelete="CASCADE"), nullable=False),
    Column("role_id", String(32), ForeignKey("jfast_roles.id", ondelete="CASCADE"), nullable=False),
    PrimaryKeyConstraint("user_id", "role_id"),
)

#: In creation order: a table before the ones that reference it.
ACCOUNT_TABLES = ("jfast_users", "jfast_roles", "jfast_role_permissions", "jfast_user_roles")
