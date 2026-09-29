"""Users, roles and permissions, as operations on a session.

Every method takes the caller's session and writes through it, so an account
change commits with whatever else the request did. Tenant scoping is explicit:
every query names the tenant, and an administrator of one tenant cannot see or
touch another's users.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from jfastframework.accounts.models import role_permissions, roles, user_roles, users
from jfastframework.accounts.passwords import MAX_LENGTH, check_password, hash_password
from jfastframework.errors import ConflictError, NotFoundError, ValidationError

__all__ = ["AccountsService", "LoginResult", "User", "normalize_email"]

# Deliberately loose: one @, something on each side, a dot in the domain. A
# stricter pattern rejects real addresses; deliverability is what a
# verification email is for.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if len(value) > 320 or not _EMAIL.match(value):
        raise ValidationError("that is not an email address")
    return value


def _new_id() -> str:
    return uuid.uuid4().hex


def _tenant_is(column: Any, tenant_id: str | None) -> Any:
    return column.is_(None) if tenant_id is None else column == tenant_id


@dataclass(frozen=True)
class User:
    id: str
    tenant_id: str | None
    email: str
    display_name: str | None
    is_active: bool
    has_password: bool
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    last_login_at: datetime | None = None
    created_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "display_name": self.display_name,
            "is_active": self.is_active,
            "roles": list(self.roles),
            "permissions": list(self.permissions),
            "last_login_at": self.last_login_at,
            "created_at": self.created_at,
        }


@dataclass
class LoginResult:
    user: User | None = None
    # Why it failed, for the log. Never shown to the client: "no such user",
    # "wrong password" and "locked" all answer the same 401.
    reason: str = ""
    locked_until: datetime | None = field(default=None)

    @property
    def ok(self) -> bool:
        return self.user is not None


class AccountsService:
    def __init__(
        self,
        session: Any,
        *,
        min_password_length: int = 10,
        max_failed_logins: int = 5,
        lockout: timedelta = timedelta(minutes=15),
    ) -> None:
        self.session = session
        self.min_password_length = min_password_length
        self.max_failed_logins = max_failed_logins
        self.lockout = lockout

    # -- users -----------------------------------------------------------

    def _check_password_policy(self, password: str) -> None:
        if len(password) < self.min_password_length:
            raise ValidationError(
                f"a password needs at least {self.min_password_length} characters"
            )
        if len(password) > MAX_LENGTH:
            raise ValidationError(f"a password can have at most {MAX_LENGTH} characters")

    async def create_user(
        self,
        email: str,
        *,
        password: str | None,
        tenant_id: str | None = None,
        display_name: str | None = None,
        role_names: list[str] | None = None,
        federated_id: str | None = None,
    ) -> User:
        address = normalize_email(email)
        if password is not None:
            self._check_password_policy(password)
        existing = await self._row_by_email(address, tenant_id)
        if existing is not None:
            raise ConflictError("a user with this email already exists")
        user_id = _new_id()
        try:
            await self.session.execute(
                insert(users).values(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=address,
                    password_hash=await hash_password(password) if password else None,
                    display_name=display_name,
                    federated_id=federated_id,
                    is_active=True,
                    failed_logins=0,
                )
            )
        except IntegrityError as exc:
            # Two sign-ups for one address at once: the check above passed
            # for both, the constraint lets one through.
            raise ConflictError("a user with this email already exists") from exc
        if role_names:
            await self.set_roles(user_id, role_names, tenant_id=tenant_id)
        return await self.get(user_id, tenant_id=tenant_id)

    async def _row_by_email(self, email: str, tenant_id: str | None) -> Any:
        query = select(users).where(
            and_(users.c.email == email, _tenant_is(users.c.tenant_id, tenant_id))
        )
        return (await self.session.execute(query)).mappings().first()

    async def _row(self, user_id: str, tenant_id: str | None) -> Any:
        query = select(users).where(
            and_(users.c.id == user_id, _tenant_is(users.c.tenant_id, tenant_id))
        )
        return (await self.session.execute(query)).mappings().first()

    async def _user(self, row: Any) -> User:
        role_list, permission_list = await self.grants_of(row["id"])
        return User(
            id=row["id"],
            tenant_id=row["tenant_id"],
            email=row["email"],
            display_name=row["display_name"],
            is_active=bool(row["is_active"]),
            has_password=row["password_hash"] is not None,
            roles=role_list,
            permissions=permission_list,
            last_login_at=row["last_login_at"],
            created_at=row["created_at"],
        )

    async def get(self, user_id: str, *, tenant_id: str | None) -> User:
        row = await self._row(user_id, tenant_id)
        if row is None:
            raise NotFoundError(f"user {user_id!r} not found")
        return await self._user(row)

    async def find(self, user_id: str, *, tenant_id: str | None) -> User | None:
        row = await self._row(user_id, tenant_id)
        return None if row is None else await self._user(row)

    async def list_users(
        self, *, tenant_id: str | None, limit: int = 50, offset: int = 0
    ) -> tuple[list[User], int]:
        where = _tenant_is(users.c.tenant_id, tenant_id)
        total = (
            await self.session.execute(select(func.count()).select_from(users).where(where))
        ).scalar_one()
        rows = (
            (
                await self.session.execute(
                    select(users).where(where).order_by(users.c.email).limit(limit).offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [await self._user(row) for row in rows], int(total)

    async def update_user(
        self,
        user_id: str,
        *,
        tenant_id: str | None,
        is_active: bool | None = None,
        display_name: str | None = None,
        role_names: list[str] | None = None,
    ) -> User:
        if await self._row(user_id, tenant_id) is None:
            raise NotFoundError(f"user {user_id!r} not found")
        values: dict[str, Any] = {}
        if is_active is not None:
            values["is_active"] = is_active
        if display_name is not None:
            values["display_name"] = display_name
        if values:
            values["updated_at"] = datetime.now(UTC)
            await self.session.execute(update(users).where(users.c.id == user_id).values(**values))
        if role_names is not None:
            await self.set_roles(user_id, role_names, tenant_id=tenant_id)
        return await self.get(user_id, tenant_id=tenant_id)

    async def change_password(
        self, user_id: str, *, tenant_id: str | None, current: str | None, new: str
    ) -> None:
        row = await self._row(user_id, tenant_id)
        if row is None:
            raise NotFoundError(f"user {user_id!r} not found")
        if row["password_hash"] is not None:
            ok, _ = await check_password(current or "", row["password_hash"])
            if not ok:
                raise ValidationError("the current password is not correct")
        self._check_password_policy(new)
        await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(password_hash=await hash_password(new), updated_at=datetime.now(UTC))
        )

    # -- login -------------------------------------------------------------

    async def authenticate(
        self, email: str, password: str, *, tenant_id: str | None
    ) -> LoginResult:
        """Check a password, counting failures and locking after too many.

        Returns rather than raises, so the caller can commit the failure count:
        an exception would roll it back, and a lockout that never persists is
        no lockout.
        """
        try:
            address = normalize_email(email)
        except ValidationError:
            await check_password(password, None)
            return LoginResult(reason="malformed email")
        row = await self._row_by_email(address, tenant_id)
        now = datetime.now(UTC)
        if row is None:
            await check_password(password, None)
            return LoginResult(reason="no such user")

        locked = row["locked_until"]
        if locked is not None and locked.tzinfo is None:
            locked = locked.replace(tzinfo=UTC)
        if locked is not None and locked > now:
            await check_password(password, None)
            return LoginResult(reason="locked", locked_until=locked)

        ok, rehash = await check_password(password, row["password_hash"])
        if not ok:
            failures = int(row["failed_logins"]) + 1
            values: dict[str, Any] = {"failed_logins": failures}
            until = None
            if failures >= self.max_failed_logins:
                until = now + self.lockout
                values = {"failed_logins": 0, "locked_until": until}
            await self.session.execute(
                update(users).where(users.c.id == row["id"]).values(**values)
            )
            return LoginResult(reason="wrong password", locked_until=until)

        if not row["is_active"]:
            return LoginResult(reason="inactive")

        values = {"failed_logins": 0, "locked_until": None, "last_login_at": now}
        if rehash:
            values["password_hash"] = await hash_password(password)
        await self.session.execute(update(users).where(users.c.id == row["id"]).values(**values))
        return LoginResult(user=await self.get(row["id"], tenant_id=tenant_id))

    async def sign_in_federated(
        self,
        *,
        federated_id: str,
        email: str | None,
        email_verified: bool,
        display_name: str | None,
        tenant_id: str | None,
        allow_signup: bool,
        default_roles: list[str],
    ) -> User | None:
        """The user a provider-verified identity belongs to, linking or creating one.

        An existing account is linked only through a *verified* email: an
        unverified one would let anybody who can type an address into a
        provider take over the account that has it.
        """
        row = (
            (await self.session.execute(select(users).where(users.c.federated_id == federated_id)))
            .mappings()
            .first()
        )
        if row is not None:
            if row["tenant_id"] != tenant_id or not row["is_active"]:
                return None
            await self.session.execute(
                update(users).where(users.c.id == row["id"]).values(last_login_at=datetime.now(UTC))
            )
            return await self._user(row)

        if email and email_verified:
            address = normalize_email(email)
            row = await self._row_by_email(address, tenant_id)
            if row is not None:
                if not row["is_active"]:
                    return None
                await self.session.execute(
                    update(users)
                    .where(users.c.id == row["id"])
                    .values(federated_id=federated_id, last_login_at=datetime.now(UTC))
                )
                return await self.get(row["id"], tenant_id=tenant_id)
            if allow_signup:
                return await self.create_user(
                    address,
                    password=None,
                    tenant_id=tenant_id,
                    display_name=display_name,
                    role_names=default_roles,
                    federated_id=federated_id,
                )
        return None

    # -- roles -------------------------------------------------------------

    async def grants_of(self, user_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The user's role names and the union of their permissions, sorted."""
        role_rows = (
            await self.session.execute(
                select(roles.c.id, roles.c.name)
                .join(user_roles, user_roles.c.role_id == roles.c.id)
                .where(user_roles.c.user_id == user_id)
            )
        ).all()
        role_ids = [row[0] for row in role_rows]
        permissions: set[str] = set()
        if role_ids:
            permissions = set(
                (
                    await self.session.execute(
                        select(role_permissions.c.permission).where(
                            role_permissions.c.role_id.in_(role_ids)
                        )
                    )
                ).scalars()
            )
        return tuple(sorted(row[1] for row in role_rows)), tuple(sorted(permissions))

    async def _role_rows(self, tenant_id: str | None) -> list[Any]:
        return list(
            (
                await self.session.execute(
                    select(roles)
                    .where(_tenant_is(roles.c.tenant_id, tenant_id))
                    .order_by(roles.c.name)
                )
            )
            .mappings()
            .all()
        )

    async def list_roles(self, *, tenant_id: str | None) -> list[dict[str, Any]]:
        result = []
        for row in await self._role_rows(tenant_id):
            granted = (
                await self.session.execute(
                    select(role_permissions.c.permission)
                    .where(role_permissions.c.role_id == row["id"])
                    .order_by(role_permissions.c.permission)
                )
            ).scalars()
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "permissions": list(granted),
                }
            )
        return result

    async def ensure_role(
        self,
        name: str,
        *,
        tenant_id: str | None,
        permissions: list[str],
        description: str | None = None,
    ) -> str:
        """Create the role if it is missing and set its permissions. Returns its id."""
        existing = (
            await self.session.execute(
                select(roles.c.id).where(
                    and_(roles.c.name == name, _tenant_is(roles.c.tenant_id, tenant_id))
                )
            )
        ).scalar_one_or_none()
        role_id = existing or _new_id()
        if existing is None:
            try:
                await self.session.execute(
                    insert(roles).values(
                        id=role_id, tenant_id=tenant_id, name=name, description=description
                    )
                )
            except IntegrityError as exc:
                raise ConflictError(f"role {name!r} already exists") from exc
        elif description is not None:
            await self.session.execute(
                update(roles).where(roles.c.id == role_id).values(description=description)
            )
        await self._set_permissions(role_id, permissions)
        return role_id

    async def create_role(
        self,
        name: str,
        *,
        tenant_id: str | None,
        permissions: list[str],
        description: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("a role needs a name")
        exists = (
            await self.session.execute(
                select(roles.c.id).where(
                    and_(roles.c.name == name, _tenant_is(roles.c.tenant_id, tenant_id))
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise ConflictError(f"role {name!r} already exists")
        role_id = await self.ensure_role(
            name, tenant_id=tenant_id, permissions=permissions, description=description
        )
        return await self._role(role_id, tenant_id)

    async def update_role(
        self,
        role_id: str,
        *,
        tenant_id: str | None,
        permissions: list[str] | None,
        description: str | None,
    ) -> dict[str, Any]:
        await self._require_role(role_id, tenant_id)
        if description is not None:
            await self.session.execute(
                update(roles).where(roles.c.id == role_id).values(description=description)
            )
        if permissions is not None:
            await self._set_permissions(role_id, permissions)
        return await self._role(role_id, tenant_id)

    async def delete_role(self, role_id: str, *, tenant_id: str | None) -> None:
        await self._require_role(role_id, tenant_id)
        # Children first: not every database enforces ON DELETE CASCADE --
        # SQLite only does with a pragma the test suites do not set.
        await self.session.execute(delete(user_roles).where(user_roles.c.role_id == role_id))
        await self.session.execute(
            delete(role_permissions).where(role_permissions.c.role_id == role_id)
        )
        await self.session.execute(delete(roles).where(roles.c.id == role_id))

    async def _require_role(self, role_id: str, tenant_id: str | None) -> None:
        found = (
            await self.session.execute(
                select(roles.c.id).where(
                    and_(roles.c.id == role_id, _tenant_is(roles.c.tenant_id, tenant_id))
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(f"role {role_id!r} not found")

    async def _role(self, role_id: str, tenant_id: str | None) -> dict[str, Any]:
        for role in await self.list_roles(tenant_id=tenant_id):
            if role["id"] == role_id:
                return role
        raise NotFoundError(f"role {role_id!r} not found")

    async def _set_permissions(self, role_id: str, permissions: list[str]) -> None:
        wanted = sorted({p.strip() for p in permissions if p.strip()})
        for permission in wanted:
            if len(permission) > 200 or " " in permission:
                raise ValidationError(f"{permission!r} is not a permission name")
        await self.session.execute(
            delete(role_permissions).where(role_permissions.c.role_id == role_id)
        )
        if wanted:
            await self.session.execute(
                insert(role_permissions),
                [{"role_id": role_id, "permission": p} for p in wanted],
            )

    async def set_roles(
        self, user_id: str, role_names: list[str], *, tenant_id: str | None
    ) -> None:
        """Replace the user's roles with these, by name, within the tenant."""
        names = sorted(set(role_names))
        found: dict[str, str] = {}
        if names:
            rows = (
                await self.session.execute(
                    select(roles.c.name, roles.c.id).where(
                        and_(roles.c.name.in_(names), _tenant_is(roles.c.tenant_id, tenant_id))
                    )
                )
            ).all()
            found = {row[0]: row[1] for row in rows}
        missing = [name for name in names if name not in found]
        if missing:
            raise ValidationError(f"no such role(s): {', '.join(missing)}")
        await self.session.execute(delete(user_roles).where(user_roles.c.user_id == user_id))
        if found:
            await self.session.execute(
                insert(user_roles),
                [{"user_id": user_id, "role_id": role_id} for role_id in found.values()],
            )

    async def count_users(self) -> int:
        return int(
            (await self.session.execute(select(func.count()).select_from(users))).scalar_one()
        )
