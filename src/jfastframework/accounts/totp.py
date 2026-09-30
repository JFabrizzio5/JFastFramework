"""Time-based one-time passwords (RFC 6238), and recovery codes.

The standard library is enough: TOTP is HMAC-SHA1 over a counter (RFC 4226),
with the counter being the number of 30-second steps since the epoch. A
dependency would add a supply-chain surface to twenty lines of arithmetic.

What an authenticator app expects, and what this produces: a 160-bit secret,
base32 without padding, six digits, thirty seconds, SHA-1. Other parameters
are valid RFC 6238 and are silently ignored by several popular apps, which
then show codes that never match -- so they are not offered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

__all__ = [
    "DIGITS",
    "PERIOD",
    "generate_recovery_codes",
    "hash_code",
    "hotp",
    "new_secret",
    "normalize_recovery_code",
    "otpauth_uri",
    "secret_bytes",
    "step_at",
    "verify_totp",
]

DIGITS = 6
PERIOD = 30

# Crockford-style: no I, L, O or U to misread, 32 symbols so each is 5 bits.
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ0123456789"
_RECOVERY_LENGTH = 16  # 80 bits


def new_secret() -> str:
    """A fresh secret, base32 without padding, as authenticator apps take it."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, *, digits: int = DIGITS, algorithm: str = "sha1") -> str:
    """RFC 4226: the code for one counter value."""
    digest = hmac.new(key, struct.pack(">Q", counter), algorithm).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def step_at(moment: float | None = None, *, period: int = PERIOD) -> int:
    return int((time.time() if moment is None else moment) // period)


def verify_totp(
    secret: str,
    code: str,
    *,
    moment: float | None = None,
    window: int = 1,
    after_step: int | None = None,
    digits: int = DIGITS,
) -> int | None:
    """The time step ``code`` belongs to, or None when it matches none.

    ``window`` steps either side of now are accepted -- one, by default: a
    phone whose clock is thirty seconds off still works, and the code a user
    was typing when the step turned still counts.

    ``after_step`` is the last step this secret was accepted for. A code at or
    before it is refused even though it is correct: that is replay, and within
    the tolerance window a code seen over someone's shoulder is otherwise good
    for a minute and a half.

    Every candidate is compared, and with ``compare_digest``, so how long this
    takes says nothing about which one was close.
    """
    candidate = "".join(ch for ch in code if not ch.isspace())
    if len(candidate) != digits or not candidate.isdigit():
        return None
    key = secret_bytes(secret)
    now = step_at(moment)
    matched: int | None = None
    for step in range(now - window, now + window + 1):
        if hmac.compare_digest(hotp(key, step, digits=digits), candidate) and matched is None:
            matched = step
    if matched is None or (after_step is not None and matched <= after_step):
        return None
    return matched


def otpauth_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator app reads from a QR code."""
    label = quote(f"{issuer}:{account}" if issuer else account, safe="@:")
    query = {"secret": secret, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD}
    if issuer:
        query["issuer"] = issuer
    return f"otpauth://totp/{label}?{urlencode(query, quote_via=quote)}"


def generate_recovery_codes(count: int) -> list[str]:
    """``count`` single-use codes, formatted ``XXXX-XXXX-XXXX-XXXX``."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append("-".join(raw[i : i + 4] for i in range(0, _RECOVERY_LENGTH, 4)))
    return codes


def normalize_recovery_code(code: str) -> str:
    """Upper-case, no separators: how a code is typed should not matter."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(value: str) -> str:
    """SHA-256 hex of a high-entropy secret: a one-time token or a recovery code.

    A fast hash is right here and wrong for passwords: these values are random
    with 80 bits or more, so there is no dictionary to try against a stolen
    table, and a lookup by hash stays one indexed query.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
