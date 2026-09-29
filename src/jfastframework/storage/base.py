"""The storage contract: disks, visibility, and URLs.

Modelled on Laravel's disks, because the idea is right: code writes to a named
disk, and *where* that disk lives is configuration. The same handler writes to
the local filesystem in development and to S3 in production without an edit.

Two ideas do the work.

**Visibility.** A `public` disk is served directly — by the app in development,
by Caddy or a CDN in production — and its URLs never expire. A `private` disk
is reachable only through a URL that expires. Getting this wrong is how invoice
PDFs end up indexed by a search engine, so the disk declares it and the code
cannot accidentally publish to the wrong one.

**Keys are not paths.** A key is a logical name inside a disk. Every backend
normalises it and rejects anything that tries to escape — path traversal is the
single most common storage vulnerability, and the only reliable place to stop
it is before the key reaches a filesystem or a bucket.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import posixpath
import re
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# Keys are deliberately restrictive: letters, digits, and a few separators.
# Anything else -- backslashes, control characters, colons -- is either an
# attempt to escape or a portability problem between backends.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class StorageError(RuntimeError):
    """A storage operation failed."""


class InvalidKey(StorageError):
    """The key is unsafe or malformed."""


class FileNotFound(StorageError):
    """No object at that key."""


def normalise_key(key: str) -> str:
    """Validate and canonicalise a storage key.

    Rejects traversal, absolute paths, empty segments and backslashes. This
    runs in every backend rather than in the plugin, so a backend used directly
    is as safe as one used through the façade.
    """
    if not key or not isinstance(key, str):
        raise InvalidKey("key must be a non-empty string")
    if "\\" in key:
        raise InvalidKey(f"key {key!r} contains a backslash; use '/' separators")
    if key.startswith("/"):
        raise InvalidKey(f"key {key!r} must be relative to the disk root")
    if "\x00" in key:
        raise InvalidKey("key contains a null byte")

    # Resolve `.` and `..` *before* checking, so `a/../../b` is caught rather
    # than passed through to the filesystem to be resolved there.
    resolved = posixpath.normpath(key)
    if resolved.startswith("..") or resolved == "." or resolved.startswith("/"):
        raise InvalidKey(f"key {key!r} escapes the disk root")
    if not _SAFE_KEY.match(resolved):
        raise InvalidKey(
            f"key {key!r} contains unsupported characters; "
            f"use letters, digits, '.', '_', '-' and '/'"
        )
    if len(resolved) > 1024:
        raise InvalidKey("key is longer than 1024 characters")
    return resolved


@dataclass(frozen=True)
class StoredFile:
    """Metadata about one stored object."""

    key: str
    size: int
    content_type: str = "application/octet-stream"
    modified_at: datetime | None = None
    etag: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class StorageBackend(Protocol):
    """What a disk must be able to do."""

    name: str
    visibility: str

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        """Write an object, running the disk's upload pipeline first."""
        ...

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        """Write without running the pipeline.

        `put` is the door uploads come in through; this is the primitive under
        it. Internal transfers — a move between disks, a copy-on-read during a
        migration — use this, because re-validating an object that is already
        stored means tightening a disk's rules breaks the migration of files
        that were legal when they were written.
        """
        ...

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        """Write an object from a stream, never holding it whole in memory.

        For files too big for ``put``: a multi-gigabyte ZIP of CFDI, a video.
        The disk's ``validate`` rules run as the bytes pass -- the size limit
        stops the upload at the limit, the type is decided from its head -- and
        a disk whose pipeline rewrites the bytes refuses, because that needs
        the whole file. The object appears only when the last byte is written.
        """
        ...

    async def get(self, key: str) -> bytes:
        """Read an object. Raises ``FileNotFound``."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool:
        """Remove an object. False when it was not there."""
        ...

    async def stat(self, key: str) -> StoredFile:
        """Metadata without the body. Raises ``FileNotFound``."""
        ...

    async def listing(self, prefix: str = "", *, limit: int = 1000) -> list[StoredFile]: ...

    def url(self, key: str) -> str:
        """A permanent URL. Only valid on a public disk."""
        ...

    async def temporary_url(self, key: str, *, expires_in: int = 300) -> str:
        """A URL that stops working. The only way to reach a private disk."""
        ...

    async def health(self) -> tuple[bool, str]: ...


def guess_content_type(key: str, fallback: str = "application/octet-stream") -> str:
    """Content type from the key's extension.

    Only ever used for objects *this service wrote*. A type derived from a
    user-supplied filename must never be echoed back on download — see the
    note on `Content-Disposition` in the plugin.
    """
    import mimetypes

    guessed, _ = mimetypes.guess_type(key)
    return guessed or fallback


def sanitised_download_headers(
    filename: str, content_type: str, *, inline: bool = False
) -> dict[str, str]:
    """Response headers for serving a stored object.

    Serving user uploads from the application's own origin is an XSS vector:
    an uploaded `.html` or `.svg` rendered inline runs script on your domain,
    against your cookies. So, unless the caller explicitly asks for inline:

    * ``Content-Disposition: attachment`` — the browser saves rather than renders;
    * ``X-Content-Type-Options: nosniff`` — no MIME sniffing around the type;
    * a quoted, stripped filename — no header injection through a newline.
    """
    safe_name = re.sub(r"[^\w.\- ]", "_", filename.rsplit("/", 1)[-1])[:200] or "download"
    disposition = "inline" if inline else "attachment"
    return {
        "Content-Type": content_type,
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "X-Content-Type-Options": "nosniff",
    }


class UrlSigner:
    """HMAC over a key and an expiry, for URLs this service serves itself.

    Lives here rather than on the local backend because the same secret has to
    verify a link after the object behind it has moved to another disk — a
    signature tied to one backend instance would stop working at exactly the
    moment a migration needs it to keep working.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def sign(self, key: str, expires_at: int) -> str:
        if not self._secret:
            raise StorageError("no signing key; set JFAST_STORAGE_SIGNING_KEY")
        message = f"{normalise_key(key)}:{expires_at}".encode()
        digest = hmac.new(self._secret.encode(), message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        # Constant time: a fast comparison leaks how much of the signature was
        # right, which is enough to forge one a byte at a time.
        return hmac.compare_digest(self.sign(key, expires_at), signature)


@dataclass
class DiskConfig:
    """One named disk."""

    name: str
    driver: str = "local"
    visibility: str = "private"
    # Steps run before every `put` on this disk, in order.
    pipeline: list[str] = field(default_factory=list)
    pipeline_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    # local
    root: str = "storage"
    url_prefix: str = ""
    # s3 / MinIO
    bucket: str = ""
    region: str = "us-east-1"
    endpoint_url: str = ""
    access_key: str = ""
    secret_key: str = ""
    # Path-style addressing: MinIO needs it, real S3 does not.
    force_path_style: bool = False
    # Absolute origin the public objects on this disk are reachable at: a CDN,
    # a custom domain, or just this app's own host when the client is a
    # single-page app on another origin. Applies to both drivers.
    public_base_url: str = ""

    def describe(self) -> dict[str, Any]:
        """Safe to log: never the credentials."""
        return {
            "name": self.name,
            "driver": self.driver,
            "visibility": self.visibility,
            "bucket": self.bucket or None,
            "endpoint_url": self.endpoint_url or None,
            "pipeline": list(self.pipeline),
        }


# Which keys mean anything to which driver. A key that is accepted and ignored
# is worse than one that does not exist: a `public_base_url` silently dropped
# on a local disk shows up as an image that renders as nothing, with no failed
# request to find.
COMMON_KEYS = frozenset({"driver", "visibility", "pipeline", "public_base_url"})
DRIVER_KEYS: dict[str, frozenset[str]] = {
    "local": COMMON_KEYS | {"root", "url_prefix"},
    "s3": COMMON_KEYS
    | {"bucket", "region", "endpoint_url", "access_key", "secret_key", "force_path_style"},
}
