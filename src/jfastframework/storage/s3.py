"""S3 disk — and MinIO, which speaks the same API.

MinIO is not a separate driver. Point `endpoint_url` at it and turn on
path-style addressing, and every call below is unchanged. That is the whole
reason to develop against MinIO: the code path you test is the code path that
runs in production.

    [plugin.storage.disks.uploads]
    driver = "s3"
    bucket = "uploads"
    endpoint_url = "http://localhost:9006"   # MinIO
    force_path_style = true

boto3 is synchronous, so every call runs in a worker thread. That is honest
rather than clever: an async S3 client would add a dependency and a second code
path for a workload that is almost always a handful of uploads per request. If
you are streaming gigabytes, reach for a dedicated client and say why.

Requires: ``pip install jfastframework[s3]``
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from typing import Any

from jfastframework.storage.base import (
    FileNotFound,
    StorageError,
    StoredFile,
    guess_content_type,
    normalise_key,
)
from jfastframework.storage.pipeline import StreamCheck, Upload, UploadPipeline, guard_stream


class S3Storage:
    #: S3's minimum part is 5 MiB for every part but the last.
    PART_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        name: str,
        *,
        bucket: str,
        visibility: str = "private",
        region: str = "us-east-1",
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        force_path_style: bool = False,
        public_base_url: str = "",
        pipeline: UploadPipeline | None = None,
    ) -> None:
        self.name = name
        self.visibility = visibility
        self._pipeline = pipeline or UploadPipeline()
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._public_base_url = public_base_url.rstrip("/")
        self._force_path_style = force_path_style
        self._access_key = access_key
        self._secret_key = secret_key
        self._client: Any = None

    def _build_client(self) -> Any:
        import boto3
        from botocore.config import Config

        config = Config(
            region_name=self._region,
            signature_version="s3v4",
            s3={"addressing_style": "path" if self._force_path_style else "auto"},
            # Bound the damage of a slow bucket: without these a hung S3 call
            # holds a worker thread until the process restarts.
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        credentials: dict[str, Any] = {}
        if self._access_key and self._secret_key:
            credentials = {
                "aws_access_key_id": self._access_key,
                "aws_secret_access_key": self._secret_key,
            }
        # No explicit credentials: fall through to the standard chain --
        # environment, shared config, instance role. On EKS or EC2 that means
        # no long-lived keys anywhere, which is the goal.
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url or None,
            config=config,
            **credentials,
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- reads and writes ----------------------------------------------

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredFile:
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
        safe = normalise_key(key)
        resolved_type = content_type or guess_content_type(key)

        def _put() -> dict[str, Any]:
            return self.client.put_object(  # type: ignore[no-any-return]
                Bucket=self._bucket,
                Key=safe,
                Body=data,
                ContentType=resolved_type,
                Metadata=metadata or {},
            )

        response = await asyncio.to_thread(_put)
        return StoredFile(
            key=safe,
            size=len(data),
            content_type=resolved_type,
            modified_at=datetime.now(UTC),
            etag=(response.get("ETag") or "").strip('"') or None,
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
        """A multipart upload, one part per ``PART_BYTES`` of the stream.

        Nothing is visible under ``key`` until the upload completes, and a
        failure part way aborts it, so S3 does not keep -- and bill for -- the
        parts of an upload that will never finish. A stream shorter than one
        part is a single ``put_object``.
        """
        safe = normalise_key(key)
        check = StreamCheck()
        guarded = guard_stream(self._pipeline, disk=self.name, key=safe, chunks=chunks, check=check)
        buffer = bytearray()
        parts: list[dict[str, Any]] = []
        upload_id: str | None = None

        def resolved_type() -> str:
            return check.content_type or content_type or guess_content_type(safe)

        async def flush(final: bool) -> None:
            nonlocal upload_id
            if not buffer and not final:
                return
            if upload_id is None:
                created = await asyncio.to_thread(
                    self.client.create_multipart_upload,
                    Bucket=self._bucket,
                    Key=safe,
                    ContentType=resolved_type(),
                    Metadata=metadata or {},
                )
                upload_id = str(created["UploadId"])
            number = len(parts) + 1
            response = await asyncio.to_thread(
                self.client.upload_part,
                Bucket=self._bucket,
                Key=safe,
                UploadId=upload_id,
                PartNumber=number,
                Body=bytes(buffer),
            )
            parts.append({"ETag": response["ETag"], "PartNumber": number})
            buffer.clear()

        try:
            async for chunk in guarded:
                buffer += chunk
                if len(buffer) >= self.PART_BYTES:
                    await flush(final=False)
            if upload_id is None:
                return await self.write(
                    safe, bytes(buffer), content_type=resolved_type(), metadata=metadata
                )
            if buffer:
                await flush(final=True)
            response = await asyncio.to_thread(
                self.client.complete_multipart_upload,
                Bucket=self._bucket,
                Key=safe,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException:
            if upload_id is not None:
                await asyncio.to_thread(
                    self.client.abort_multipart_upload,
                    Bucket=self._bucket,
                    Key=safe,
                    UploadId=upload_id,
                )
            raise
        return StoredFile(
            key=safe,
            size=check.size,
            content_type=resolved_type(),
            modified_at=datetime.now(UTC),
            etag=(response.get("ETag") or "").strip('"') or None,
            metadata=metadata or {},
        )

    async def get(self, key: str) -> bytes:
        safe = normalise_key(key)

        def _get() -> bytes:
            try:
                response = self.client.get_object(Bucket=self._bucket, Key=safe)
            except self.client.exceptions.NoSuchKey:
                raise FileNotFound(f"{self.name}: no object at {key!r}") from None
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(_get)

    async def exists(self, key: str) -> bool:
        safe = normalise_key(key)

        def _head() -> bool:
            from botocore.exceptions import ClientError

            try:
                self.client.head_object(Bucket=self._bucket, Key=safe)
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

        return await asyncio.to_thread(_head)

    async def delete(self, key: str) -> bool:
        safe = normalise_key(key)
        existed = await self.exists(key)

        def _delete() -> None:
            # S3 delete is idempotent and does not report whether anything was
            # there, so the caller's contract needs the head above.
            self.client.delete_object(Bucket=self._bucket, Key=safe)

        await asyncio.to_thread(_delete)
        return existed

    async def stat(self, key: str) -> StoredFile:
        safe = normalise_key(key)

        def _head() -> StoredFile:
            from botocore.exceptions import ClientError

            try:
                response = self.client.head_object(Bucket=self._bucket, Key=safe)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    raise FileNotFound(f"{self.name}: no object at {key!r}") from None
                raise
            return StoredFile(
                key=safe,
                size=int(response.get("ContentLength", 0)),
                content_type=response.get("ContentType", "application/octet-stream"),
                modified_at=response.get("LastModified"),
                etag=(response.get("ETag") or "").strip('"') or None,
                metadata=dict(response.get("Metadata", {})),
            )

        return await asyncio.to_thread(_head)

    async def listing(self, prefix: str = "", *, limit: int = 1000) -> list[StoredFile]:
        safe_prefix = normalise_key(prefix) if prefix else ""

        def _list() -> list[StoredFile]:
            # S3 returns at most 1,000 keys per request whatever MaxKeys
            # asks for, so a larger limit has to follow the continuation
            # token; one request silently cut every listing at 1,000.
            files: list[StoredFile] = []
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": safe_prefix}
            while len(files) < limit:
                response = self.client.list_objects_v2(
                    **kwargs, MaxKeys=min(limit - len(files), 1000)
                )
                files.extend(
                    StoredFile(
                        key=item["Key"],
                        size=int(item.get("Size", 0)),
                        content_type=guess_content_type(item["Key"]),
                        modified_at=item.get("LastModified"),
                        etag=(item.get("ETag") or "").strip('"') or None,
                    )
                    for item in response.get("Contents", [])
                )
                token = response.get("NextContinuationToken")
                if not response.get("IsTruncated") or not token:
                    break
                kwargs["ContinuationToken"] = token
            return files[:limit]

        return await asyncio.to_thread(_list)

    # -- URLs ----------------------------------------------------------

    def url(self, key: str) -> str:
        if self.visibility != "public":
            raise StorageError(
                f"disk {self.name!r} is private; use temporary_url() instead. "
                f"A permanent URL to a private disk is how private files become public."
            )
        safe = normalise_key(key)
        if self._public_base_url:
            # A CDN or a custom domain in front of the bucket.
            return f"{self._public_base_url}/{safe}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{safe}"
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{safe}"

    async def temporary_url(self, key: str, *, expires_in: int = 300) -> str:
        safe = normalise_key(key)

        def _sign() -> str:
            return self.client.generate_presigned_url(  # type: ignore[no-any-return]
                "get_object",
                Params={"Bucket": self._bucket, "Key": safe},
                ExpiresIn=expires_in,
            )

        return await asyncio.to_thread(_sign)

    async def upload_url(
        self, key: str, *, expires_in: int = 300, content_type: str | None = None
    ) -> str:
        """A presigned URL the client PUTs to directly.

        Worth using for anything large: the bytes go browser-to-bucket instead
        of through this service, which stops one big upload from occupying a
        worker for a minute.
        """
        safe = normalise_key(key)
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": safe}
        if content_type:
            params["ContentType"] = content_type

        def _sign() -> str:
            return self.client.generate_presigned_url(  # type: ignore[no-any-return]
                "put_object", Params=params, ExpiresIn=expires_in
            )

        return await asyncio.to_thread(_sign)

    async def ensure_bucket(self) -> None:
        """Create the bucket if it is missing.

        For MinIO in development. On real S3 the bucket is infrastructure --
        it has a lifecycle policy, a retention rule and an owner, and an
        application creating it on boot bypasses all three.
        """

        def _ensure() -> None:
            from botocore.exceptions import ClientError

            try:
                self.client.head_bucket(Bucket=self._bucket)
            except ClientError:
                kwargs: dict[str, Any] = {"Bucket": self._bucket}
                if self._region and self._region != "us-east-1":
                    kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
                self.client.create_bucket(**kwargs)

        await asyncio.to_thread(_ensure)

    async def health(self) -> tuple[bool, str]:
        def _head() -> tuple[bool, str]:
            try:
                self.client.head_bucket(Bucket=self._bucket)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return False, f"bucket {self._bucket!r} unreachable: {exc}"
            return True, f"bucket {self._bucket!r} reachable"

        return await asyncio.to_thread(_head)

    def __repr__(self) -> str:
        return f"<S3Storage {self.name!r} bucket={self._bucket!r} visibility={self.visibility!r}>"
