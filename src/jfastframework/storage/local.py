"""Filesystem disk.

The right default: no bucket to create, no credentials to leak, and a
developer can look at the files. It stops being right the moment you run more
than one replica — two pods do not share a local directory, so an upload
handled by one is a 404 from the other. Switch the disk's driver to `s3` then;
nothing above the disk changes.

Temporary URLs are HMAC-signed here rather than presigned by a provider. The
signature covers the key *and* the expiry, so neither can be edited without
invalidating it — a URL that only carried an expiry the client could change
would not be a security control at all.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path

from jfastframework.storage.base import (
    FileNotFound,
    StorageError,
    StoredFile,
    UrlSigner,
    guess_content_type,
    normalise_key,
)
from jfastframework.storage.pipeline import StreamCheck, Upload, UploadPipeline, guard_stream


class LocalStorage:
    def __init__(
        self,
        name: str,
        *,
        root: str | Path,
        visibility: str = "private",
        url_prefix: str = "",
        signing_key: str = "",
        public_base_url: str = "",
        pipeline: UploadPipeline | None = None,
    ) -> None:
        self.name = name
        self.visibility = visibility
        self._root = Path(root).resolve()
        self._url_prefix = url_prefix.rstrip("/")
        self._public_base_url = public_base_url.rstrip("/")
        self._signing_key = signing_key
        self._signer = UrlSigner(signing_key) if signing_key else None
        self._pipeline = pipeline or UploadPipeline()
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            # The ordinary cause is a container running as a non-root user in
            # a directory root owns -- or a volume mounted on a path the image
            # never created, which Docker then creates as root.
            raise PermissionError(
                f"local storage cannot create {self._root}: this user cannot write there. "
                f"In a container, create it owned by the app user before USER in the "
                f"Dockerfile, e.g. `RUN mkdir -p {self._root} && chown appuser:appuser "
                f"{self._root}`, so a volume mounted on it starts out writable."
            ) from exc

    def _path(self, key: str) -> Path:
        safe = normalise_key(key)
        path = (self._root / safe).resolve()
        # Belt and braces: normalise_key already rejects traversal, but a
        # symlink inside the root could still point outside it, and that check
        # can only be made after resolving.
        if not path.is_relative_to(self._root):
            raise StorageError(f"key {key!r} resolves outside the disk root")
        return path

    # -- reads and writes ----------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        # The pipeline runs in the backend rather than in the plugin, for the
        # same reason normalise_key does: a disk used directly -- from a
        # worker, from a script -- must obey the same rules as one used
        # through a route.
        upload = await self._pipeline.run(
            Upload(
                disk=self.name,
                key=normalise_key(key),
                data=data,
                content_type=content_type,
                metadata=dict(metadata or {}),
            )
        )
        return await self.write(
            upload.key,
            upload.data,
            content_type=upload.content_type,
            metadata=upload.metadata,
        )

    async def write(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file and rename: a reader never sees a
            # half-written object, and a crash mid-write leaves the previous
            # version intact rather than a truncated one.
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            temporary.write_bytes(data)
            temporary.replace(path)

        await asyncio.to_thread(_write)
        return StoredFile(
            key=normalise_key(key),
            size=len(data),
            content_type=content_type or guess_content_type(key),
            modified_at=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def put_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
        safe = normalise_key(key)
        path = self._path(safe)
        check = StreamCheck()
        guarded = guard_stream(self._pipeline, disk=self.name, key=safe, chunks=chunks, check=check)
        # A name of its own per upload: two streams to one key must not write
        # into the same temporary file.
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        handle = await asyncio.to_thread(temporary.open, "wb")
        try:
            async for chunk in guarded:
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(temporary.replace, path)
        except BaseException:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(temporary.unlink, True)
            raise
        return StoredFile(
            key=safe,
            size=check.size,
            content_type=check.content_type or content_type or guess_content_type(safe),
            modified_at=datetime.now(UTC),
            metadata=metadata or {},
        )

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise FileNotFound(f"{self.name}: no object at {key!r}") from None

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> bool:
        path = self._path(key)

        def _delete() -> bool:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def stat(self, key: str) -> StoredFile:
        path = self._path(key)
        try:
            info = await asyncio.to_thread(path.stat)
        except FileNotFoundError:
            raise FileNotFound(f"{self.name}: no object at {key!r}") from None
        return StoredFile(
            key=normalise_key(key),
            size=info.st_size,
            content_type=guess_content_type(key),
            modified_at=datetime.fromtimestamp(info.st_mtime, tz=UTC),
        )

    async def listing(self, prefix: str = "", *, limit: int = 1000) -> list[StoredFile]:
        base = self._root / normalise_key(prefix) if prefix else self._root

        def _walk() -> list[StoredFile]:
            if not base.exists():
                return []
            found: list[StoredFile] = []
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                info = path.stat()
                found.append(
                    StoredFile(
                        key=path.relative_to(self._root).as_posix(),
                        size=info.st_size,
                        content_type=guess_content_type(path.name),
                        modified_at=datetime.fromtimestamp(info.st_mtime, tz=UTC),
                    )
                )
                if len(found) >= limit:
                    break
            return found

        return await asyncio.to_thread(_walk)

    # -- URLs ----------------------------------------------------------

    def url(self, key: str) -> str:
        if self.visibility != "public":
            raise StorageError(
                f"disk {self.name!r} is private; use temporary_url() instead. "
                f"A permanent URL to a private disk is how private files become public."
            )
        safe = normalise_key(key)
        if self._public_base_url:
            # A CDN, a custom domain, or this app's own absolute origin. The
            # root-relative form below is a 404 rendered as nothing when the
            # client is a single-page app on a different host.
            return f"{self._public_base_url}/{safe}"
        return f"{self._url_prefix}/{safe}"

    def sign(self, key: str, expires_at: int) -> str:
        """HMAC over key and expiry, so neither can be edited."""
        if self._signer is None:
            raise StorageError(
                f"disk {self.name!r} has no signing key; set JFAST_STORAGE_SIGNING_KEY"
            )
        return self._signer.sign(key, expires_at)

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if self._signer is None:
            return False
        return self._signer.verify(key, expires_at, signature)

    async def temporary_url(self, key: str, *, expires_in: int = 300) -> str:
        # Deliberately not prefixed with public_base_url: that points at a CDN
        # or cache, and a cache in front of a signed URL serves the object to
        # the next caller after the signature has expired.
        expires_at = int(time.time()) + expires_in
        signature = self.sign(key, expires_at)
        return f"{self._url_prefix}/{normalise_key(key)}?expires={expires_at}&signature={signature}"

    async def health(self) -> tuple[bool, str]:
        if not self._root.is_dir():
            return False, f"{self._root} does not exist"
        if not os.access(self._root, os.W_OK):
            return False, f"{self._root} is not writable"
        return True, f"local disk at {self._root}"

    def __repr__(self) -> str:
        return f"<LocalStorage {self.name!r} root={self._root} visibility={self.visibility!r}>"
