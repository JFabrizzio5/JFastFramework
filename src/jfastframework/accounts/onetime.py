"""Single-use, expiring tokens: email verification, password reset, the MFA step.

The token is 256 random bits handed to exactly one place -- an email, or the
response to a correct password -- and the table keeps only its SHA-256. A copy
of the database therefore resets nobody's password and verifies nobody's
address.

Single use is enforced by the database, not by a read followed by a write:
consuming is ``UPDATE ... SET used_at = now WHERE used_at IS NULL``, and only
the request whose update touched the row gets it. Two clicks on one link, or
an attacker racing the owner, produce one success.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update

from jfastframework.accounts.models import one_time_tokens
from jfastframework.accounts.totp import hash_code

__all__ = [
    "MFA_ENROLL",
    "MFA_LOGIN",
    "RESET_PASSWORD",
    "VERIFY_EMAIL",
    "OneTimeTokens",
]

VERIFY_EMAIL = "verify_email"
RESET_PASSWORD = "reset_password"
MFA_LOGIN = "mfa_login"
MFA_ENROLL = "mfa_enroll"


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class OneTimeTokens:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def issue(self, user_id: str, purpose: str, lifetime: timedelta) -> str:
        """A new token for this purpose. Earlier unused ones stay valid until they expire.

        Keeping them valid is deliberate for email: the second "resend" must
        not break the link in the first email, which is often the one opened.
        """
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        await self.session.execute(
            insert(one_time_tokens).values(
                id=uuid.uuid4().hex,
                user_id=user_id,
                purpose=purpose,
                token_hash=hash_code(token),
                expires_at=now + lifetime,
                attempts=0,
                # The application's clock, not the database's: the cooldown
                # compares it with the application's clock.
                created_at=now,
            )
        )
        return token

    async def find(self, token: str, purpose: str) -> Any:
        """The live row for this token, or None: unknown, used, expired or another purpose."""
        if not token or len(token) > 200:
            return None
        digest = hash_code(token)
        row = (
            (
                await self.session.execute(
                    select(one_time_tokens).where(one_time_tokens.c.token_hash == digest)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        # The lookup was by hash, so this comparison guards nothing an attacker
        # can time; it is here so that the row's purpose and hash are checked
        # the same way wherever this pattern is copied to.
        if not hmac.compare_digest(str(row["token_hash"]), digest) or row["purpose"] != purpose:
            return None
        expires = _aware(row["expires_at"])
        if row["used_at"] is not None or expires is None or expires <= datetime.now(UTC):
            return None
        return row

    async def consume(self, token: str, purpose: str) -> Any:
        """Mark the token used and return its row -- once. Every later call gets None."""
        row = await self.find(token, purpose)
        if row is None:
            return None
        result = await self.session.execute(
            update(one_time_tokens)
            .where(and_(one_time_tokens.c.id == row["id"], one_time_tokens.c.used_at.is_(None)))
            .values(used_at=datetime.now(UTC))
        )
        return row if result.rowcount == 1 else None

    async def fail(self, row: Any, *, max_attempts: int) -> None:
        """Count a wrong code against the token; at the limit it is spent."""
        attempts = int(row["attempts"] or 0) + 1
        values: dict[str, Any] = {"attempts": attempts}
        if attempts >= max_attempts:
            values["used_at"] = datetime.now(UTC)
        await self.session.execute(
            update(one_time_tokens).where(one_time_tokens.c.id == row["id"]).values(**values)
        )

    async def spend_all(self, user_id: str, purpose: str) -> None:
        """Every outstanding token of this purpose stops working."""
        await self.session.execute(
            update(one_time_tokens)
            .where(
                and_(
                    one_time_tokens.c.user_id == user_id,
                    one_time_tokens.c.purpose == purpose,
                    one_time_tokens.c.used_at.is_(None),
                )
            )
            .values(used_at=datetime.now(UTC))
        )

    async def last_issued(self, user_id: str, purpose: str) -> datetime | None:
        """When the newest token of this purpose was issued: the resend cooldown reads it."""
        value = (
            await self.session.execute(
                select(func.max(one_time_tokens.c.created_at)).where(
                    and_(
                        one_time_tokens.c.user_id == user_id,
                        one_time_tokens.c.purpose == purpose,
                    )
                )
            )
        ).scalar_one_or_none()
        return _aware(value)

    async def prune(self, user_id: str) -> None:
        """Forget this user's tokens that can no longer be used. Keeps the table small."""
        await self.session.execute(
            delete(one_time_tokens).where(
                and_(
                    one_time_tokens.c.user_id == user_id,
                    one_time_tokens.c.expires_at < datetime.now(UTC) - timedelta(days=1),
                )
            )
        )
