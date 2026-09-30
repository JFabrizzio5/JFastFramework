"""What each extra must bring for the plugin it is named after to work."""

from __future__ import annotations

import tomllib
from pathlib import Path

EXTRAS = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["optional-dependencies"]


def _names(extra: str) -> set[str]:
    return {spec.split(">")[0].split("=")[0].split("[")[0].strip() for spec in EXTRAS[extra]}


def test_storage_brings_what_an_upload_route_needs() -> None:
    # Found building a receipts SaaS from scratch: dev worked because the dev
    # extra pulls python-multipart, and the production image -- requirements.txt
    # with [storage] -- failed to import main.py at its first UploadFile route.
    assert "python-multipart" in _names("storage")
