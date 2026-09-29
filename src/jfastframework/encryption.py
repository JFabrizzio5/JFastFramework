"""Reversible encryption for business secrets: AES-256-GCM, with key rotation.

Some secrets a service holds are not its own configuration but its users'
data, and it has to be able to read them back: the password that opens a
customer's e-signature key, an API token for their bank. A hash does not work
-- the value is needed in the clear to use it -- and ``SecretStr`` only keeps a
value out of logs; the database still holds the plain text. This module is the
missing piece:

    from jfastframework.encryption import SecretBox

    box = SecretBox.from_env()                   # JFAST_ENCRYPTION_KEYS
    token = box.encrypt(password, context=f"fiel:{rfc}")
    password = box.decrypt(token, context=f"fiel:{rfc}")

Or on a column, where the ORM does it on every read and write:

    from jfastframework.encryption import EncryptedString

    class Credential(Base):
        password: Mapped[str] = mapped_column(EncryptedString(context="fiel"))

The rules, each for a reason:

* **AES-256-GCM, a fresh 96-bit nonce per value.** Authenticated: a token that
  was altered, or encrypted under another key, fails to decrypt instead of
  decrypting to garbage.
* **Context is authenticated too.** ``context`` goes in as associated data, so
  a token copied from one customer's row to another's does not decrypt there.
* **Keys rotate without a migration.** ``JFAST_ENCRYPTION_KEYS`` is a list of
  ``id:key`` pairs; the first encrypts, all of them decrypt, and every token
  names the key that made it. Add a new key first in the list, and old tokens
  keep working until :meth:`SecretBox.rotate` rewrites them.
* **The key is never in the database or in jfast.toml.** It comes from the
  environment -- or a secret manager through ``load_secrets`` -- and losing it
  loses every value it encrypted, which is the point.

Requires ``cryptography`` (the ``encryption`` extra; ``auth`` brings it too).
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

__all__ = [
    "DecryptionError",
    "EncryptedString",
    "EncryptionConfigError",
    "SecretBox",
    "configure",
    "generate_key",
]

ENV_VAR = "JFAST_ENCRYPTION_KEYS"
_PREFIX = "jf1"
_NONCE_BYTES = 12


class EncryptionConfigError(ValueError):
    """The keys are missing or malformed."""


class DecryptionError(ValueError):
    """A token that does not decrypt: altered, wrong context, or an unknown key."""


def generate_key() -> str:
    """A new 256-bit key, base64-encoded, for ``JFAST_ENCRYPTION_KEYS``."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _key_id_ok(key_id: str) -> bool:
    return bool(key_id) and len(key_id) <= 32 and key_id.replace("-", "").replace("_", "").isalnum()


class SecretBox:
    """Encrypts with the primary key and decrypts with any key it holds."""

    def __init__(self, keys: Mapping[str, bytes], *, primary: str) -> None:
        if not keys:
            raise EncryptionConfigError("a SecretBox needs at least one key")
        if primary not in keys:
            raise EncryptionConfigError(f"primary key {primary!r} is not among the keys")
        for key_id, key in keys.items():
            if not _key_id_ok(key_id):
                raise EncryptionConfigError(
                    f"key id {key_id!r} must be 1-32 letters, digits, '-' or '_'"
                )
            if len(key) != 32:
                raise EncryptionConfigError(
                    f"key {key_id!r} is {len(key)} bytes; AES-256 needs 32 "
                    f"(generate one with jfastframework.encryption.generate_key())"
                )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self.primary = primary

    @classmethod
    def from_env(cls, var: str = ENV_VAR) -> SecretBox:
        """Keys from ``var``: ``id:base64key`` pairs, comma-separated, primary first."""
        raw = os.environ.get(var, "").strip()
        if not raw:
            raise EncryptionConfigError(
                f"{var} is not set. Generate a key with "
                f"`python -c 'from jfastframework.encryption import generate_key; "
                f"print(generate_key())'` and set {var}=k1:<key>"
            )
        keys: dict[str, bytes] = {}
        order: list[str] = []
        for item in raw.split(","):
            key_id, sep, encoded = item.strip().partition(":")
            if not sep:
                raise EncryptionConfigError(f"{var}: {item!r} is not id:key")
            try:
                keys[key_id] = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except ValueError as exc:
                raise EncryptionConfigError(f"{var}: key {key_id!r} is not base64") from exc
            order.append(key_id)
        return cls(keys, primary=order[0])

    def encrypt(self, plaintext: str | bytes, *, context: str = "") -> str:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = self._ciphers[self.primary].encrypt(nonce, data, context.encode("utf-8"))
        body = base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")
        return f"{_PREFIX}.{self.primary}.{body}"

    def decrypt_bytes(self, token: str, *, context: str = "") -> bytes:
        from cryptography.exceptions import InvalidTag

        prefix, _, rest = token.partition(".")
        key_id, _, body = rest.partition(".")
        if prefix != _PREFIX or not body:
            raise DecryptionError("not an encrypted value")
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise DecryptionError(
                f"encrypted with key {key_id!r}, which this service does not hold"
            )
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            return cipher.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode("utf-8"))
        except (InvalidTag, ValueError) as exc:
            raise DecryptionError("the value was altered, or the context does not match") from exc

    def decrypt(self, token: str, *, context: str = "") -> str:
        return self.decrypt_bytes(token, context=context).decode("utf-8")

    def key_of(self, token: str) -> str:
        """The id of the key a token was encrypted with."""
        return token.split(".", 2)[1] if token.count(".") >= 2 else ""

    def rotate(self, token: str, *, context: str = "") -> str:
        """The same value under the primary key. Unchanged if already there."""
        if self.key_of(token) == self.primary:
            return token
        return self.encrypt(self.decrypt_bytes(token, context=context), context=context)


_configured: SecretBox | None = None


def configure(box: SecretBox | None) -> None:
    """Set the box ``EncryptedString`` columns use. ``None`` reverts to the environment."""
    global _configured
    _configured = box


def _box() -> SecretBox:
    global _configured
    if _configured is None:
        _configured = SecretBox.from_env()
    return _configured


class EncryptedString(TypeDecorator[str]):
    """A text column that stores its value encrypted and hands it back in the clear.

    ``context`` binds every value to the column it lives in, so a token copied
    into another encrypted column fails to decrypt. The stored text is longer
    than the value -- about 4/3 of it plus 40 characters -- which is why the
    column is ``TEXT``.

    It is not searchable: two encryptions of one value differ, by design.
    Look rows up by something else.
    """

    impl = Text
    cache_ok = True

    def __init__(self, *, context: str) -> None:
        super().__init__()
        self.context = context

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return _box().encrypt(value, context=self.context)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return _box().decrypt(value, context=self.context)

    def copy(self, **kw: Any) -> EncryptedString:
        return EncryptedString(context=self.context)
