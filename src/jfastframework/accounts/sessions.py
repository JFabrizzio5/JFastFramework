"""Every session a user has, so that all of them can be ended at once.

``auth`` revokes one refresh *family* at a time -- one sign-in -- and keeps no
list of which families belong to whom: a family keyed on the person would end
their next session too. Accounts is the part that knows the person, so it
writes one row per sign-in here, and "sign this user out everywhere" -- a
password reset, a deactivation, a sign-out-everywhere button -- is a walk over
those rows revoking each family in auth's store. Revoking a family also stops
the access token that carries it, at once, not at its expiry.

``jfast_users.sessions_valid_after`` backs this up for sessions that were never
written here: minted before this table existed, or by application code calling
``auth.issuer`` directly. Their next refresh is refused. Their access token,
which this table cannot name, runs out its lifetime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, insert, select, update

from jfastframework.accounts.models import user_sessions, users

__all__ = ["record_session", "revoke_all_sessions"]


async def record_session(session: Any, *, user_id: str, family: str, lifetime: timedelta) -> None:
    now = datetime.now(UTC)
    # Forget this user's sessions that have ended on their own: nothing left to revoke.
    await session.execute(
        delete(user_sessions).where(
            and_(user_sessions.c.user_id == user_id, user_sessions.c.expires_at < now)
        )
    )
    await session.execute(
        insert(user_sessions).values(
            family=family, user_id=user_id, created_at=now, expires_at=now + lifetime
        )
    )


async def revoke_all_sessions(session: Any, store: Any, *, user_id: str) -> int:
    """End every session of this user. Returns how many families were revoked."""
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(user_sessions.c.family, user_sessions.c.expires_at).where(
                user_sessions.c.user_id == user_id
            )
        )
    ).all()
    revoked = 0
    for family, expires_at in rows:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        remaining = int((expires_at - now).total_seconds())
        if remaining > 0 and store is not None:
            await store.revoke_family(family, ttl=remaining)
            revoked += 1
    await session.execute(delete(user_sessions).where(user_sessions.c.user_id == user_id))
    # Whole seconds: a refresh token's `iat` is in whole seconds, and a
    # sign-in completed in the same second as the reset must survive it.
    cutoff = now.replace(microsecond=0)
    await session.execute(
        update(users).where(users.c.id == user_id).values(sessions_valid_after=cutoff)
    )
    return revoked
