# Encryption

Some secrets a service holds are its users' data, and it has to read them back:
the password that opens a customer's e-signature key, a token for their bank.
A hash does not work -- the value is needed in the clear to use it -- and
`SecretStr` only keeps a value out of logs; the database still holds the plain
text. `jfastframework.encryption` is for those.

```bash
pip install "jfastframework[encryption]"
python -c "from jfastframework.encryption import generate_key; print(generate_key())"
export JFAST_ENCRYPTION_KEYS="k1:<that key>"
```

## On a column

```python
from jfastframework.encryption import EncryptedString

class Credential(Base, TimestampMixin, TenantMixin):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfc: Mapped[str]
    password: Mapped[str] = mapped_column(EncryptedString(context="fiel"))
```

The ORM encrypts on every write and decrypts on every read; the code around it
sees plain strings. What is stored looks like `jf1.k1.3q8f...` -- the format,
the key that made it, and the sealed value.

It is not searchable: two encryptions of one value differ, by design. Look rows
up by something else (`rfc` here).

## By hand

```python
from jfastframework.encryption import SecretBox

box = SecretBox.from_env()
token = box.encrypt(password, context=f"fiel:{rfc}")
password = box.decrypt(token, context=f"fiel:{rfc}")
```

`context` is authenticated along with the value. A token copied from one
customer's row into another's does not decrypt there -- with the column type,
the context is the column; by hand, make it name the owner, as above.

## What it guarantees, and how

| | |
| --- | --- |
| Algorithm | AES-256-GCM, a fresh 96-bit nonce per value |
| Altered value | Fails with `DecryptionError`, never decrypts to garbage |
| Wrong context, wrong key | `DecryptionError` |
| Keys | From the environment, never from the database or `jfast.toml` |

## Rotating a key

`JFAST_ENCRYPTION_KEYS` is a list, primary first:

```bash
JFAST_ENCRYPTION_KEYS="k2:<new key>,k1:<old key>"
```

The first key encrypts; every key decrypts; each value names the key that made
it. So rotation is:

1. Put the new key first and keep the old one after it. Deploy. New writes use
   `k2`, old values still read.
2. Rewrite old values when convenient -- `box.rotate(token, context=...)`
   returns the same value under the primary key, and is a no-op on one already
   there. A background job over the table is enough.
3. When no value names `k1` any more, remove it.

**Losing every key loses every value**, which is the point of encrypting them.
Keep the keys where the rest of the service's secrets are -- a secret manager
through `load_secrets()`, not a file in the repository.

## What it is not

- **Not for passwords users log in with.** Those are hashed, never decrypted:
  the `accounts` plugin uses argon2id.
- **Not searchable, not sortable.** Encrypt the value; index something else.
