"""Streamed uploads, and types that are recognised by parsing rather than bytes.

The two gaps a CFDI archive fell into: a multi-gigabyte ZIP had to fit in
memory to be stored, and a disk for XML could not validate XML at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jfastframework.storage.local import LocalStorage
from jfastframework.storage.pipeline import (
    PipelineConfigError,
    UploadRejected,
    build_pipeline,
    recognise,
)
from jfastframework.storage.s3 import S3Storage

CFDI = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" Version="4.0" Total="116.00">'
    b'<cfdi:Emisor Rfc="AAA010101AAA"/></cfdi:Comprobante>'
)
XXE = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _chunks(data: bytes, size: int = 1000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _temporaries(root: Path) -> list[Path]:
    return list(root.rglob("*.tmp"))


def _disk(tmp_path: Path, **validate: Any) -> LocalStorage:
    steps = ["validate"] if validate else []
    config = {"validate": validate} if validate else {}
    return LocalStorage("files", root=tmp_path, pipeline=build_pipeline("files", steps, config))


# -- recognising by parsing ------------------------------------------------------


def test_xml_is_recognised_only_where_the_disk_asks_for_it() -> None:
    assert recognise(CFDI, ["application/xml"]) == "application/xml"
    assert recognise(CFDI, []) is None
    assert recognise(CFDI, ["image/png"]) is None


def test_xml_with_a_doctype_or_entity_is_not_xml_here() -> None:
    assert recognise(XXE, ["application/xml"]) is None
    assert recognise(b'<!ENTITY a "b"><r/>', ["application/xml"]) is None


def test_svg_and_html_are_not_accepted_as_data() -> None:
    assert recognise(SVG, ["application/xml"]) is None
    assert recognise(b"<html><body/></html>", ["application/xml"]) is None


def test_json_and_text() -> None:
    assert recognise(b'{"total": 116}', ["application/json"]) == "application/json"
    assert recognise(b'"just a string"', ["application/json"]) is None
    assert recognise(b"line one\nline two\n", ["text/plain"]) == "text/plain"
    assert recognise(b"binary\x00zero", ["text/plain"]) is None
    assert recognise(b"rfc,total\nAAA,116\n", ["text/csv"]) == "text/csv"


def test_a_signature_still_wins_over_a_parsed_type() -> None:
    assert recognise(PNG, ["image/png", "text/plain"]) == "image/png"


async def test_a_disk_for_cfdi_stores_xml_and_refuses_the_rest(tmp_path: Path) -> None:
    disk = _disk(tmp_path, allow=["application/xml"], max_bytes="1MB")
    stored = await disk.put("2026/04/uuid.xml", CFDI)
    assert stored.content_type == "application/xml"
    for bad in (XXE, SVG, PNG):
        with pytest.raises(UploadRejected):
            await disk.put("bad.xml", bad)


def test_an_unknown_type_in_allow_is_a_config_error() -> None:
    with pytest.raises(PipelineConfigError):
        build_pipeline("files", ["validate"], {"validate": {"allow": ["application/x-yaml"]}})


# -- streaming to a local disk ---------------------------------------------------


async def test_a_stream_lands_whole_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    disk = _disk(tmp_path)
    data = bytes(range(256)) * 12_000  # ~3 MB, never held by the disk in one piece
    stored = await disk.put_stream("big/archive.bin", _chunks(data, 64_000))
    assert stored.size == len(data)
    assert await disk.get("big/archive.bin") == data
    assert not _temporaries(tmp_path)


async def test_a_stream_past_the_limit_stops_and_leaves_nothing(tmp_path: Path) -> None:
    disk = _disk(tmp_path, max_bytes="10KB", allow=["application/zip"])
    data = b"PK\x03\x04" + b"\x00" * 50_000
    with pytest.raises(UploadRejected, match="max_bytes"):
        await disk.put_stream("too-big.zip", _chunks(data))
    assert not await disk.exists("too-big.zip")
    assert not _temporaries(tmp_path)


async def test_a_streamed_file_is_typed_from_its_head(tmp_path: Path) -> None:
    disk = _disk(tmp_path, allow=["application/xml", "application/zip"])
    stored = await disk.put_stream("a.xml", _chunks(CFDI, 7))
    assert stored.content_type == "application/xml"
    with pytest.raises(UploadRejected):
        await disk.put_stream("x.xml", _chunks(XXE, 7))
    with pytest.raises(UploadRejected):
        await disk.put_stream("p.png", _chunks(PNG, 7))


async def test_an_empty_stream_is_refused(tmp_path: Path) -> None:
    disk = _disk(tmp_path, allow=["application/xml"])
    with pytest.raises(UploadRejected, match="empty"):
        await disk.put_stream("empty.xml", _chunks(b""))


async def test_a_disk_that_rewrites_uploads_refuses_a_stream(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    pipeline = build_pipeline("images", ["validate", "optimise-image"], {})
    disk = LocalStorage("images", root=tmp_path, pipeline=pipeline)
    with pytest.raises(UploadRejected, match="whole file"):
        await disk.put_stream("a.png", _chunks(PNG))


# -- streaming to S3 ----------------------------------------------------------------


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.parts: list[bytes] = []
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("put_object")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": '"one"'}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"create:{kwargs['ContentType']}")
        return {"UploadId": "u-1"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"part:{kwargs['PartNumber']}")
        self.parts.append(kwargs["Body"])
        return {"ETag": f'"p{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"complete:{len(kwargs['MultipartUpload']['Parts'])}")
        self.objects[kwargs["Key"]] = b"".join(self.parts)
        return {"ETag": '"multi"'}

    def abort_multipart_upload(self, **kwargs: Any) -> None:
        self.calls.append("abort")


def _s3(**validate: Any) -> tuple[S3Storage, FakeS3]:
    steps = ["validate"] if validate else []
    disk = S3Storage(
        "files",
        bucket="b",
        pipeline=build_pipeline("files", steps, {"validate": validate} if validate else {}),
    )
    fake = FakeS3()
    disk._client = fake
    disk.PART_BYTES = 20_000  # type: ignore[misc]
    return disk, fake


async def test_a_short_stream_is_one_put_object() -> None:
    disk, fake = _s3()
    await disk.put_stream("small.bin", _chunks(b"x" * 500, 100))
    assert fake.calls == ["put_object"]


async def test_a_long_stream_is_a_multipart_upload() -> None:
    disk, fake = _s3(allow=["application/zip"])
    data = b"PK\x03\x04" + b"z" * 200_000
    stored = await disk.put_stream("big.zip", _chunks(data, 10_000))
    # The type is decided from the head before the upload is created, so the
    # object carries the sniffed type, not the caller's.
    assert fake.calls[0] == "create:application/zip"
    assert fake.calls[-1].startswith("complete:")
    assert fake.objects["big.zip"] == data
    assert stored.size == len(data)


async def test_a_failed_stream_aborts_the_multipart_upload() -> None:
    disk, fake = _s3(max_bytes="150KB", allow=["application/zip"])
    data = b"PK\x03\x04" + b"z" * 400_000
    with pytest.raises(UploadRejected):
        await disk.put_stream("big.zip", _chunks(data, 10_000))
    assert "abort" in fake.calls
    assert "big.zip" not in fake.objects


async def test_a_stream_refused_from_its_head_never_reaches_the_bucket() -> None:
    disk, fake = _s3(allow=["application/zip"])
    with pytest.raises(UploadRejected):
        await disk.put_stream("not-a-zip.zip", _chunks(PNG + b"\x00" * 100_000, 10_000))
    assert fake.calls == []
