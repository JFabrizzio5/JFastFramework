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
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

from jfastframework.db.base import UTCDateTime
from jfastframework.db.framework import framework_metadata

__all__ = [
    "ACCOUNT_TABLES",
    "one_time_tokens",
    "recovery_codes",
    "role_permissions",
    "roles",
    "user_roles",
    "user_sessions",
    "users",
]

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
    # -- added in 0.1.0a11. Nullable with no server default, every one: an
    # existing table gains them at startup through ensure_columns, and only a
    # column like that can be added to a table with rows in one instant ALTER.
    #
    # When the owner proved the address is theirs. NULL is "not yet".
    Column("email_verified_at", UTCDateTime),
    # The TOTP secret, encrypted with JFAST_ENCRYPTION_KEYS (never a hash: the
    # server needs it back to compute the code). Set during enrolment, before
    # mfa_enabled_at; both NULL means no second factor.
    Column("mfa_secret", Text),
    Column("mfa_enabled_at", UTCDateTime),
    # The last TOTP time step accepted. A code is refused unless its step is
    # later, which is what stops one observed code being used twice.
    Column("mfa_last_step", Integer),
    # Refresh tokens issued before this instant are refused at their next
    # refresh: the backstop for "sign this person out everywhere" when a
    # session was not recorded in jfast_user_sessions.
    Column("sessions_valid_after", UTCDateTime),
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

one_time_tokens = Table(
    "jfast_account_tokens",
    framework_metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("jfast_users.id", ondelete="CASCADE"), nullable=False),
    # verify_email | reset_password | mfa_login | mfa_enroll
    Column("purpose", String(32), nullable=False),
    # SHA-256 of the token. The token itself exists only in the email or the
    # response that carried it: a copy of this table opens nothing.
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("used_at", UTCDateTime),
    # Wrong codes presented with an MFA token; it dies at the limit.
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Index("ix_jfast_account_tokens_user_purpose", "user_id", "purpose"),
)

recovery_codes = Table(
    "jfast_recovery_codes",
    framework_metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("jfast_users.id", ondelete="CASCADE"), nullable=False),
    # SHA-256 of the code. Eighty random bits each, so a fast hash is enough:
    # nobody enumerates 2**80 of them, salted or not.
    Column("code_hash", String(64), nullable=False),
    Column("used_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Index("ix_jfast_recovery_codes_user", "user_id"),
)

user_sessions = Table(
    "jfast_user_sessions",
    framework_metadata,
    # The refresh family auth minted at sign-in. One row per sign-in, not per
    # refresh: rotation keeps the family.
    Column("family", String(64), primary_key=True),
    Column("user_id", String(32), ForeignKey("jfast_users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_jfast_user_sessions_user", "user_id"),
)

#: In creation order: a table before the ones that reference it.
ACCOUNT_TABLES = (
    "jfast_users",
    "jfast_roles",
    "jfast_role_permissions",
    "jfast_user_roles",
    "jfast_account_tokens",
    "jfast_recovery_codes",
    "jfast_user_sessions",
)
