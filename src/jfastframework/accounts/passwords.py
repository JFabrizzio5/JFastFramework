"""Password hashing: argon2id, off the event loop.

argon2id is what OWASP recommends and what ``argon2-cffi`` defaults to. A hash
costs tens of milliseconds of CPU on purpose -- that is what makes a stolen
table expensive to crack -- and tens of milliseconds on the event loop would
stall every request on the worker, so both directions run in a thread.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

__all__ = ["check_password", "hash_password"]

#: Longer than any real password, short enough that nobody can make the
#: server hash a megabyte.
MAX_LENGTH = 1024


@lru_cache(maxsize=1)
def _hasher() -> Any:
    try:
        from argon2 import PasswordHasher
    except ModuleNotFoundError as exc:  # pragma: no cover - named by the plugin
        raise ModuleNotFoundError(
            'accounts needs argon2-cffi: pip install "jfastframework[accounts]"', name="argon2"
        ) from exc
    return PasswordHasher()


@lru_cache(maxsize=1)
def _decoy() -> str:
    # Verified when there is no such user, so that "no user" and "wrong
    # password" take the same time and a login form cannot be used to find
    # out which addresses have accounts.
    return str(_hasher().hash("decoy password that matches nothing"))


async def hash_password(password: str) -> str:
    if len(password) > MAX_LENGTH:
        raise ValueError("password too long")
    return str(await asyncio.to_thread(_hasher().hash, password))


async def check_password(password: str, stored: str | None) -> tuple[bool, bool]:
    """Whether ``password`` matches, and whether the hash should be upgraded.

    ``stored`` of None (a social-only user, or no user at all) still costs one
    verification against a decoy, and never matches.
    """
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    hasher = _hasher()
    target = stored or _decoy()
    if len(password) > MAX_LENGTH:
        password = password[:MAX_LENGTH]
    try:
        await asyncio.to_thread(hasher.verify, target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if stored is None:
        return False, False
    return True, bool(hasher.check_needs_rehash(stored))
