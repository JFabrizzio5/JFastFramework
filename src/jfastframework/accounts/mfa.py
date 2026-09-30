"""The second factor: TOTP enrolment, checking a code, recovery codes.

The TOTP secret is the one credential here that cannot be hashed -- the
server computes the expected code from it -- so it is stored encrypted with
``JFAST_ENCRYPTION_KEYS`` (``jfastframework.encryption``), bound to its user:
a secret copied into another user's row does not decrypt there. Recovery codes
can be hashed, and are.

Enrolment is two steps on purpose. ``begin`` stores a secret that does not yet
guard anything; only ``confirm``, with a code the app computed from it, turns
it on. A user who scans the QR code wrongly is not locked out by a factor they
never managed to set up.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update

from jfastframework.accounts.models import recovery_codes, users
from jfastframework.accounts.totp import (
    generate_recovery_codes,
    hash_code,
    new_secret,
    normalize_recovery_code,
    otpauth_uri,
    verify_totp,
)
from jfastframework.errors import ConflictError, ValidationError

__all__ = ["MfaService"]


def _context(user_id: str) -> str:
    return f"jfast.accounts.mfa:{user_id}"


class MfaService:
    def __init__(self, session: Any, *, box: Any, issuer: str, recovery_count: int = 10) -> None:
        self.session = session
        self.box = box
        self.issuer = issuer
        self.recovery_count = recovery_count

    # -- enrolment -----------------------------------------------------------

    async def begin(self, row: Any) -> dict[str, str]:
        """Store a new, not yet active secret and return what the app needs to read it."""
        if row["mfa_enabled_at"] is not None:
            raise ConflictError("two-factor authentication is already on; disable it first")
        secret = new_secret()
        await self.session.execute(
            update(users)
            .where(users.c.id == row["id"])
            .values(
                mfa_secret=self.box.encrypt(secret, context=_context(row["id"])),
                mfa_last_step=None,
            )
        )
        return {
            "secret": secret,
            "otpauth_uri": otpauth_uri(secret, account=row["email"], issuer=self.issuer),
        }

    async def confirm(self, row: Any, code: str) -> list[str]:
        """Turn the pending secret on with a code from it. Returns the recovery codes, once."""
        if row["mfa_enabled_at"] is not None:
            raise ConflictError("two-factor authentication is already on")
        if row["mfa_secret"] is None:
            raise ValidationError("start the set-up first: there is no secret to confirm")
        secret = self.box.decrypt(row["mfa_secret"], context=_context(row["id"]))
        step = verify_totp(secret, code)
        if step is None:
            raise ValidationError("the code is not correct", code="mfa_code_invalid")
        await self.session.execute(
            update(users)
            .where(users.c.id == row["id"])
            .values(mfa_enabled_at=datetime.now(UTC), mfa_last_step=step)
        )
        return await self.regenerate_codes(row["id"])

    async def disable(self, user_id: str) -> None:
        await self.session.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(mfa_secret=None, mfa_enabled_at=None, mfa_last_step=None)
        )
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )

    # -- checking ------------------------------------------------------------

    async def check(self, row: Any, code: str) -> str | None:
        """Whether ``code`` is a valid second factor for this user, spending it.

        Returns ``"totp"`` or ``"recovery"`` for a good code, None otherwise.
        A TOTP code is spent by moving ``mfa_last_step`` forward -- in one
        conditional UPDATE, so two requests racing with one code get one yes --
        and a recovery code by marking it used, the same way.
        """
        if row["mfa_enabled_at"] is None or row["mfa_secret"] is None:
            return None
        digits = "".join(ch for ch in code if not ch.isspace())
        if digits.isdigit():
            secret = self.box.decrypt(row["mfa_secret"], context=_context(row["id"]))
            step = verify_totp(secret, digits, after_step=row["mfa_last_step"])
            if step is None:
                return None
            moved = await self.session.execute(
                update(users)
                .where(
                    and_(
                        users.c.id == row["id"],
                        or_(users.c.mfa_last_step.is_(None), users.c.mfa_last_step < step),
                    )
                )
                .values(mfa_last_step=step)
            )
            return "totp" if moved.rowcount == 1 else None

        normalized = normalize_recovery_code(code)
        if len(normalized) != 16:
            return None
        spent = await self.session.execute(
            update(recovery_codes)
            .where(
                and_(
                    recovery_codes.c.user_id == row["id"],
                    recovery_codes.c.code_hash == hash_code(normalized),
                    recovery_codes.c.used_at.is_(None),
                )
            )
            .values(used_at=datetime.now(UTC))
        )
        return "recovery" if spent.rowcount == 1 else None

    # -- recovery codes ------------------------------------------------------

    async def regenerate_codes(self, user_id: str) -> list[str]:
        """A fresh set; every earlier code stops working."""
        codes = generate_recovery_codes(self.recovery_count)
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )
        await self.session.execute(
            insert(recovery_codes),
            [
                {
                    "id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "code_hash": hash_code(normalize_recovery_code(code)),
                }
                for code in codes
            ],
        )
        return codes

    async def remaining_codes(self, user_id: str) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(recovery_codes)
                    .where(
                        and_(
                            recovery_codes.c.user_id == user_id,
                            recovery_codes.c.used_at.is_(None),
                        )
                    )
                )
            ).scalar_one()
        )
