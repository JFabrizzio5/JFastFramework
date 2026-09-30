"""The S3 disk against a real S3 API: MinIO.

The storage tests elsewhere stub boto3, which proves the calls are made and
nothing about whether S3 accepts them -- a presigned URL signed for the wrong
host, a multipart part under 5 MiB, an abort that never happens all pass a
stub. Before a service runs more than one replica its files have to live in
a bucket (the local disk is per replica), so this is the driver that has to
be right. Never run against real S3: point it at MinIO.

    docker run -d -p 9010:9000 -e MINIO_ROOT_USER=jfastminio \\
        -e MINIO_ROOT_PASSWORD=jfastminio-secret minio/minio server /data
    JFAST_TEST_S3_URL=http://localhost:9010 \\
    JFAST_TEST_S3_ACCESS_KEY=jfastminio \\
    JFAST_TEST_S3_SECRET_KEY=jfastminio-secret pytest tests/test_storage_minio.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from jfastframework.storage.base import FileNotFound
from jfastframework.storage.s3 import S3Storage

S3_URL = os.environ.get("JFAST_TEST_S3_URL", "")
ACCESS_KEY = os.environ.get("JFAST_TEST_S3_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("JFAST_TEST_S3_SECRET_KEY", "")

pytestmark = pytest.mark.skipif(
    not (S3_URL and ACCESS_KEY and SECRET_KEY),
    reason="set JFAST_TEST_S3_URL, JFAST_TEST_S3_ACCESS_KEY and JFAST_TEST_S3_SECRET_KEY",
)

MIB = 1024 * 1024


@pytest.fixture
async def disk() -> AsyncIterator[S3Storage]:
    # A bucket per test: listings and multipart checks read the whole bucket.
    storage = S3Storage(
        "minio",
        bucket=f"jfast-test-{uuid.uuid4().hex[:12]}",
        endpoint_url=S3_URL,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        force_path_style=True,
    )
    await storage.ensure_bucket()
    yield storage

    def _empty() -> None:
        client = storage.client
        bucket = storage._bucket
        for upload in client.list_multipart_uploads(Bucket=bucket).get("Uploads", []):
            client.abort_multipart_upload(
                Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)

    await asyncio.to_thread(_empty)


def _open_uploads(disk: S3Storage) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = disk.client.list_multipart_uploads(Bucket=disk._bucket).get(
        "Uploads", []
    )
    return uploads


async def _chunks(total: int, size: int = MIB, *, fail_after: int | None = None) -> Any:
    sent = 0
    while sent < total:
        if fail_after is not None and sent >= fail_after:
            raise ConnectionResetError("client went away mid-upload")
        piece = min(size, total - sent)
        # Not all zeros: a part whose bytes are identical everywhere would
        # hide a part written twice or out of order.
        yield bytes([sent // size % 251]) * piece
        sent += piece


def _expected(total: int, size: int = MIB) -> bytes:
    return b"".join(
        bytes([offset // size % 251]) * min(size, total - offset)
        for offset in range(0, total, size)
    )


async def test_put_get_stat_exists_delete(disk: S3Storage) -> None:
    stored = await disk.put("invoices/2026/a.pdf", b"%PDF-1.7 hello", metadata={"tenant": "acme"})
    assert stored.key == "invoices/2026/a.pdf"
    assert stored.size == 14
    assert stored.etag

    assert await disk.get("invoices/2026/a.pdf") == b"%PDF-1.7 hello"
    assert await disk.exists("invoices/2026/a.pdf")
    stat = await disk.stat("invoices/2026/a.pdf")
    assert stat.size == 14
    assert stat.content_type == "application/pdf"
    assert stat.etag == stored.etag
    assert stat.metadata == {"tenant": "acme"}

    assert await disk.delete("invoices/2026/a.pdf") is True
    assert await disk.delete("invoices/2026/a.pdf") is False
    assert not await disk.exists("invoices/2026/a.pdf")


async def test_a_missing_object_is_file_not_found(disk: S3Storage) -> None:
    with pytest.raises(FileNotFound):
        await disk.get("nope.txt")
    with pytest.raises(FileNotFound):
        await disk.stat("nope.txt")


async def test_listing_is_scoped_to_the_prefix_and_limited(disk: S3Storage) -> None:
    for name in ("a/1.txt", "a/2.txt", "a/3.txt", "b/1.txt"):
        await disk.put(name, name.encode())
    listed = await disk.listing("a/")
    assert sorted(f.key for f in listed) == ["a/1.txt", "a/2.txt", "a/3.txt"]
    assert all(f.size == 7 for f in listed)
    assert len(await disk.listing("a/", limit=2)) == 2
    assert len(await disk.listing()) == 4


async def test_a_temporary_url_works_and_then_expires(disk: S3Storage) -> None:
    import httpx

    await disk.put("private/report.csv", b"a,b\n1,2\n")
    url = await disk.temporary_url("private/report.csv", expires_in=2)
    async with httpx.AsyncClient() as http:
        fresh = await http.get(url)
        assert fresh.status_code == 200
        assert fresh.content == b"a,b\n1,2\n"
        # Unsigned, the same object is refused: the bucket is private.
        unsigned = await http.get(url.split("?", 1)[0])
        assert unsigned.status_code == 403
        # S3 compares against whole seconds; three is past two on any clock.
        await asyncio.sleep(3)
        expired = await http.get(url)
    assert expired.status_code == 403
    assert b"expired" in expired.content.lower()


async def test_an_upload_url_accepts_a_direct_put(disk: S3Storage) -> None:
    import httpx

    url = await disk.upload_url("direct/photo.png", content_type="image/png")
    async with httpx.AsyncClient() as http:
        response = await http.put(
            url, content=b"\x89PNG fake", headers={"Content-Type": "image/png"}
        )
    assert response.status_code == 200
    assert await disk.get("direct/photo.png") == b"\x89PNG fake"
    assert (await disk.stat("direct/photo.png")).content_type == "image/png"


async def test_a_stream_larger_than_a_part_is_a_multipart_upload(disk: S3Storage) -> None:
    total = 2 * disk.PART_BYTES + 3 * MIB  # three parts, the last one short
    stored = await disk.put_stream("big/archive.zip", _chunks(total))
    assert stored.size == total
    # An S3 multipart ETag is "<md5 of the part md5s>-<parts>".
    assert stored.etag is not None and stored.etag.endswith("-3")
    assert (await disk.stat("big/archive.zip")).size == total
    assert await disk.get("big/archive.zip") == _expected(total)
    assert _open_uploads(disk) == []


async def test_a_short_stream_is_a_single_put(disk: S3Storage) -> None:
    stored = await disk.put_stream("small/notes.txt", _chunks(3 * MIB))
    assert stored.size == 3 * MIB
    assert stored.etag is not None and "-" not in stored.etag
    assert await disk.get("small/notes.txt") == _expected(3 * MIB)


async def test_a_stream_that_fails_part_way_is_aborted(disk: S3Storage) -> None:
    """Nothing under the key, and no parts left for the bucket to bill."""
    with pytest.raises(ConnectionResetError):
        await disk.put_stream(
            "big/broken.zip", _chunks(4 * disk.PART_BYTES, fail_after=disk.PART_BYTES + MIB)
        )
    assert not await disk.exists("big/broken.zip")
    assert _open_uploads(disk) == []


async def test_a_cancelled_stream_is_aborted_too(disk: S3Storage) -> None:
    """A client disconnect reaches the handler as cancellation, not an error."""
    first_part_sent = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        async for chunk in _chunks(disk.PART_BYTES + MIB):
            yield chunk
        first_part_sent.set()
        await asyncio.sleep(3600)
        yield b""

    task = asyncio.create_task(disk.put_stream("big/cancelled.zip", slow()))
    await asyncio.wait_for(first_part_sent.wait(), timeout=60)
    # Past the first part: the multipart upload exists now.
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await disk.exists("big/cancelled.zip")
    assert _open_uploads(disk) == []


async def test_health_reports_the_bucket(disk: S3Storage) -> None:
    healthy, detail = await disk.health()
    assert healthy, detail
    missing = S3Storage(
        "gone",
        bucket=f"jfast-missing-{uuid.uuid4().hex[:8]}",
        endpoint_url=S3_URL,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        force_path_style=True,
    )
    healthy, detail = await missing.health()
    assert not healthy and "unreachable" in detail


async def test_a_listing_past_one_page_is_not_cut_at_a_thousand(disk: S3Storage) -> None:
    """S3 answers at most 1,000 keys per request, whatever MaxKeys says.

    ``listing(limit=1500)`` returned 1,000 and said nothing: the caller asked
    for up to 1,500 and got a silently truncated answer. Found by this test.
    """

    def _fill() -> None:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(16) as pool:
            list(
                pool.map(
                    lambda i: disk.client.put_object(
                        Bucket=disk._bucket, Key=f"many/{i:05d}.txt", Body=b"x"
                    ),
                    range(1203),
                )
            )

    await asyncio.to_thread(_fill)
    assert len(await disk.listing("many/", limit=1500)) == 1203
    assert len(await disk.listing("many/", limit=1100)) == 1100
    assert len(await disk.listing("many/")) == 1000
