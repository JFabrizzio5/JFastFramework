"""The upload pipeline: what happens to bytes before they land on a disk.

Without it, `put()` is a hole: any caller can write any number of bytes of any
content to any disk, and the only thing between an upload form and the bucket
is the key validator. Size and type rules are not a per-handler concern — every
handler that forgets one is the hole — so they belong to the *disk*, declared
in configuration next to the driver and the visibility:

    [plugin.storage.disks.uploads]
    driver = "s3"
    pipeline = ["validate"]

    [plugin.storage.disks.uploads.validate]
    max_bytes = "10MB"
    allow = ["image/jpeg", "image/png", "application/pdf"]

A step takes an `Upload` and returns one. Returning rather than mutating means
a step that rewrites the bytes — image optimisation, redaction — and one that
only inspects them read the same way, and a step that raises half way through
cannot leave a partly-rewritten upload behind for the next step to see.

Steps are `async def` because the pipeline runs inside a request. A step doing
real CPU work — decoding an image, hashing a large file — must hand it to
`asyncio.to_thread` the way `LocalStorage.put` does. A step that blocks for
400ms blocks every other request on that worker, and `contracts/blocking.py`
is the checker that says so.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from jfastframework.storage.base import StorageError


class UploadRejected(StorageError):
    """A pipeline step refused the upload."""


class PipelineConfigError(StorageError):
    """A disk's pipeline is misconfigured. Raised at startup, never per request."""


@dataclass(frozen=True)
class Upload:
    """One object on its way to a disk.

    `content_type` is what the *caller* declared, which for an inbound upload
    means what the browser sent. It is a hint and nothing more; `validate`
    overwrites it with the type it read out of the bytes, because that is the
    value that gets stored and later served back.
    """

    disk: str
    key: str
    data: bytes
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.data)


@runtime_checkable
class UploadStep(Protocol):
    """One stage of a disk's upload pipeline."""

    name: str

    async def process(self, upload: Upload) -> Upload:
        """Return the upload to hand to the next step, or raise `UploadRejected`."""
        ...


class UploadPipeline:
    """The ordered steps of one disk."""

    def __init__(self, steps: Sequence[UploadStep] = ()) -> None:
        self._steps = tuple(steps)

    @property
    def steps(self) -> tuple[UploadStep, ...]:
        return self._steps

    def __bool__(self) -> bool:
        return bool(self._steps)

    def describe(self) -> list[str]:
        return [step.name for step in self._steps]

    async def run(self, upload: Upload) -> Upload:
        for step in self._steps:
            upload = await step.process(upload)
        return upload


# -- sizes --------------------------------------------------------------

_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?B?)\s*$", re.IGNORECASE)


def parse_size(value: str | int) -> int:
    """Bytes from `10MB`, `512 kb`, `1.5GB` or a plain integer.

    Binary units throughout: `1KB` is 1024 bytes. A limit written as a number
    of bytes is unreadable at review time, which is the only time anyone looks
    at it, so the string form is the one documented.
    """
    if isinstance(value, bool):  # bool is an int; a boolean size is a typo
        raise ValueError(f"size must be a number or a string like '10MB', not {value!r}")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"size must be positive, got {value}")
        return value

    match = _SIZE.match(value)
    if not match:
        raise ValueError(
            f"cannot read {value!r} as a size; write it like '10MB', '512KB' or a byte count"
        )
    number, unit = match.group(1), (match.group(2) or "B").upper()
    if unit in ("K", "M", "G"):
        unit += "B"
    size = int(float(number) * _UNITS[unit])
    if size <= 0:
        raise ValueError(f"size must be positive, got {value!r}")
    return size


def humanise_size(size: int) -> str:
    for unit, scale in (("GB", _UNITS["GB"]), ("MB", _UNITS["MB"]), ("KB", _UNITS["KB"])):
        if size >= scale:
            return f"{size / scale:.1f}{unit}"
    return f"{size}B"


# -- content sniffing ---------------------------------------------------


@dataclass(frozen=True)
class Signature:
    """A content type and the byte markers that identify it."""

    content_type: str
    markers: tuple[tuple[int, bytes], ...]

    def matches(self, head: bytes) -> bool:
        return all(head[at : at + len(marker)] == marker for at, marker in self.markers)


# Order matters only where one marker is a prefix of another; it is not here.
# Everything in this table is identified by bytes at a fixed offset, which is
# the only kind of check worth making without a dependency. Formats that can
# only be recognised by parsing -- SVG, CSV, JSON, plain text -- are
# deliberately absent: a "sniffer" that decides text/plain by looking for
# printable characters says yes to a shell script, and an uploaded .svg is an
# XSS vector, not an image.
SIGNATURES: tuple[Signature, ...] = (
    Signature("image/png", ((0, b"\x89PNG\r\n\x1a\n"),)),
    Signature("image/jpeg", ((0, b"\xff\xd8\xff"),)),
    Signature("image/gif", ((0, b"GIF87a"),)),
    Signature("image/gif", ((0, b"GIF89a"),)),
    Signature("image/webp", ((0, b"RIFF"), (8, b"WEBP"))),
    Signature("image/tiff", ((0, b"II*\x00"),)),
    Signature("image/tiff", ((0, b"MM\x00*"),)),
    Signature("image/avif", ((4, b"ftyp"), (8, b"avif"))),
    Signature("image/heic", ((4, b"ftyp"), (8, b"heic"))),
    Signature("image/heic", ((4, b"ftyp"), (8, b"heix"))),
    Signature("image/heic", ((4, b"ftyp"), (8, b"mif1"))),
    Signature("video/mp4", ((4, b"ftyp"), (8, b"isom"))),
    Signature("video/mp4", ((4, b"ftyp"), (8, b"iso2"))),
    Signature("video/mp4", ((4, b"ftyp"), (8, b"mp41"))),
    Signature("video/mp4", ((4, b"ftyp"), (8, b"mp42"))),
    Signature("video/mp4", ((4, b"ftyp"), (8, b"avc1"))),
    Signature("audio/mpeg", ((0, b"ID3"),)),
    Signature("audio/ogg", ((0, b"OggS"),)),
    Signature("application/pdf", ((0, b"%PDF-"),)),
    # Every OOXML and OpenDocument file is a zip, so allowing docx means
    # allowing zip and everything else wearing one. Say so in the config
    # review, not in a comment nobody reads at upload time.
    Signature("application/zip", ((0, b"PK\x03\x04"),)),
    Signature("application/gzip", ((0, b"\x1f\x8b"),)),
    Signature("font/woff", ((0, b"wOFF"),)),
    Signature("font/woff2", ((0, b"wOF2"),)),
)

# Longest marker ends at byte 12; reading more would not change an answer.
_HEAD_BYTES = 16

RECOGNISED_TYPES: tuple[str, ...] = tuple(sorted({s.content_type for s in SIGNATURES}))


def sniff_content_type(data: bytes) -> str | None:
    """The content type read out of the bytes, or None when nothing matches.

    Never derived from the key or from a client-declared `Content-Type`: both
    are chosen by whoever is uploading, and a `.png` extension on a PHP file
    is the oldest upload attack there is. `guess_content_type` in `base` is
    the extension-based one, and it is only ever right for objects this
    service wrote itself.
    """
    head = data[:_HEAD_BYTES]
    for signature in SIGNATURES:
        if signature.matches(head):
            return signature.content_type
    return None


# -- the validate step --------------------------------------------------


class ValidateStep:
    """Size and content-type rules, enforced on the bytes themselves."""

    name = "validate"

    def __init__(
        self,
        disk: str,
        *,
        max_bytes: str | int | None = None,
        allow: Sequence[str] | None = None,
    ) -> None:
        self.disk = disk
        self.max_bytes = parse_size(max_bytes) if max_bytes is not None else None
        self.max_bytes_config = max_bytes
        self.allow = tuple(allow) if allow else ()

        unknown = [item for item in self.allow if item not in RECOGNISED_TYPES]
        if unknown:
            raise PipelineConfigError(
                f"storage disk {disk!r}: validate cannot enforce {', '.join(sorted(unknown))} "
                f"because it cannot recognise those types from bytes. "
                f"It recognises: {', '.join(RECOGNISED_TYPES)}."
            )

    async def process(self, upload: Upload) -> Upload:
        if not upload.data:
            raise UploadRejected(f"disk {self.disk!r}: {upload.key!r} is empty")

        if self.max_bytes is not None and upload.size > self.max_bytes:
            raise UploadRejected(
                f"disk {self.disk!r}: {upload.key!r} is {humanise_size(upload.size)} "
                f"({upload.size} bytes), over this disk's max_bytes of "
                f"{self.max_bytes_config} ({self.max_bytes} bytes)"
            )

        sniffed = sniff_content_type(upload.data)
        if sniffed is None:
            raise UploadRejected(
                f"disk {self.disk!r}: cannot identify {upload.key!r} from its first bytes, "
                f"so it is refused rather than trusted. "
                f"validate recognises: {', '.join(RECOGNISED_TYPES)}."
            )
        if self.allow and sniffed not in self.allow:
            raise UploadRejected(
                f"disk {self.disk!r}: {upload.key!r} contains {sniffed}, which this disk "
                f"does not accept; allowed: {', '.join(self.allow)}"
            )

        # The sniffed type replaces whatever the caller declared. This is the
        # value stored on the object and served back on download, so trusting
        # the client's here would let an uploader choose the Content-Type of a
        # response from our own origin.
        return replace(upload, content_type=sniffed)


# -- the registry -------------------------------------------------------

StepFactory = Callable[[str, Mapping[str, Any]], UploadStep]


def _build_validate(disk: str, config: Mapping[str, Any]) -> UploadStep:
    unknown = set(config) - {"max_bytes", "allow"}
    if unknown:
        raise PipelineConfigError(
            f"storage disk {disk!r}: validate has no setting "
            f"{', '.join(sorted(repr(key) for key in unknown))}; it takes max_bytes and allow"
        )
    try:
        return ValidateStep(disk, max_bytes=config.get("max_bytes"), allow=config.get("allow"))
    except ValueError as exc:
        raise PipelineConfigError(f"storage disk {disk!r}: validate {exc}") from exc


def _build_optimise_image(disk: str, config: Mapping[str, Any]) -> UploadStep:
    """Imported at build time, not at import time: the step carries Pillow.

    The name is in the table either way, so a disk that misspells its pipeline
    still gets the list of real steps, and one that configures the step without
    running it still gets told so -- neither of which needs the extra to be
    installed.
    """
    from jfastframework.storage.images import build_optimise_image

    return build_optimise_image(disk, config)


STEP_FACTORIES: dict[str, StepFactory] = {
    "validate": _build_validate,
    "optimise-image": _build_optimise_image,
}


def register_step(name: str, factory: StepFactory) -> None:
    """Add a step an application or a plugin can name in a disk's pipeline.

    Registration is global and last-one-wins on purpose: replacing `validate`
    with a stricter local version is a legitimate thing to want, and refusing
    it would only push people into monkey-patching.
    """
    STEP_FACTORIES[name] = factory


def build_pipeline(
    disk: str,
    steps: Sequence[str],
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> UploadPipeline:
    """Assemble one disk's pipeline from its configured step names."""
    config = config or {}
    built: list[UploadStep] = []
    for name in steps:
        factory = STEP_FACTORIES.get(name)
        if factory is None:
            raise PipelineConfigError(
                f"storage disk {disk!r}: no pipeline step named {name!r}. "
                f"Available: {', '.join(sorted(STEP_FACTORIES))}."
            )
        built.append(factory(disk, config.get(name, {})))
    return UploadPipeline(built)
