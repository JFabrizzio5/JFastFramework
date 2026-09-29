"""Reversible encryption: what decrypts, what refuses to, and rotation."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework import encryption
from jfastframework.encryption import (
    DecryptionError,
    EncryptedString,
    EncryptionConfigError,
    SecretBox,
    generate_key,
)


def _key() -> bytes:
    return base64.urlsafe_b64decode(generate_key())


@pytest.fixture
def box() -> SecretBox:
    return SecretBox({"k1": _key()}, primary="k1")


def test_a_value_round_trips_and_is_not_stored_in_the_clear(box: SecretBox) -> None:
    token = box.encrypt("clave de la FIEL", context="fiel:AAA010101AAA")
    assert "FIEL" not in token and token.startswith("jf1.k1.")
    assert box.decrypt(token, context="fiel:AAA010101AAA") == "clave de la FIEL"


def test_two_encryptions_of_one_value_differ(box: SecretBox) -> None:
    assert box.encrypt("same") != box.encrypt("same")


def test_a_token_moved_to_another_row_does_not_decrypt(box: SecretBox) -> None:
    token = box.encrypt("secret", context="fiel:AAA010101AAA")
    with pytest.raises(DecryptionError):
        box.decrypt(token, context="fiel:BBB020202BBB")


def test_an_altered_token_does_not_decrypt(box: SecretBox) -> None:
    token = box.encrypt("secret")
    flipped = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(DecryptionError):
        box.decrypt(flipped)
    with pytest.raises(DecryptionError):
        box.decrypt("plain text that was never encrypted")


def test_rotation_keeps_old_tokens_readable_and_rewrites_them() -> None:
    old_key, new_key = _key(), _key()
    old = SecretBox({"k1": old_key}, primary="k1").encrypt("secret", context="c")

    rotated = SecretBox({"k2": new_key, "k1": old_key}, primary="k2")
    assert rotated.decrypt(old, context="c") == "secret"
    rewritten = rotated.rotate(old, context="c")
    assert rotated.key_of(rewritten) == "k2"
    assert rotated.rotate(rewritten, context="c") == rewritten
    # A service that dropped the old key can no longer read what it made.
    with pytest.raises(DecryptionError, match="k1"):
        SecretBox({"k2": new_key}, primary="k2").decrypt(old, context="c")


@pytest.fixture(autouse=True)
def _reset_configured_box() -> Iterator[None]:
    yield
    encryption.configure(None)


def test_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = generate_key(), generate_key()
    monkeypatch.setenv("JFAST_ENCRYPTION_KEYS", f"new:{first}, old:{second}")
    box = SecretBox.from_env()
    assert box.primary == "new"
    assert set(box._ciphers) == {"new", "old"}


@pytest.mark.parametrize(
    "value",
    ["", "no-colon", "k1:not base64!!", "k1:" + base64.urlsafe_b64encode(b"short").decode()],
)
def test_bad_keys_are_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JFAST_ENCRYPTION_KEYS", value)
    with pytest.raises(EncryptionConfigError):
        SecretBox.from_env()


class Base(DeclarativeBase):
    pass


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    password: Mapped[str] = mapped_column(EncryptedString(context="fiel"))


async def test_a_column_encrypts_on_write_and_decrypts_on_read(box: SecretBox) -> None:
    encryption.configure(box)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(Credential(id=1, password="12345678a"))
        await session.commit()
    async with maker() as session:
        stored = (await session.execute(text("SELECT password FROM credentials"))).scalar_one()
        loaded = (await session.execute(select(Credential))).scalar_one()
    await engine.dispose()
    assert stored.startswith("jf1.") and "12345678a" not in stored
    assert loaded.password == "12345678a"
