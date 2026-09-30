"""The image creates every local storage disk the service declares.

The generated image runs as ``appuser`` and compose mounts a named volume on
each local disk's root. Docker creates a mount point the image does not have
as root, so a disk the Dockerfile never created cannot be written: a help desk
on 0.1.0a12 declared an ``adjuntos`` disk and got ``/ready`` 503 and a 500
``PermissionError`` on every upload, while ``/health`` said 200 (bitácora
F13). ``jfast deploy dockerfile`` regenerated the same file, because it never
read ``jfast.toml``.

The image itself is built and written to by ``scripts/smoke_compose.sh``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app
from jfastframework.deploy.compose import (
    local_disk_dirs,
    read_storage_disks,
    refresh_storage_block,
    render_dockerfile,
    storage_dirs,
    storage_mounts,
)
from jfastframework.plugins.builtin.storage import StoragePlugin

runner = CliRunner()

ADJUNTOS = """
[plugin.storage.disks.public]
driver = "local"
root = "storage/public"

[plugin.storage.disks.private]
driver = "local"
root = "storage/private"

[plugin.storage.disks.adjuntos]
driver = "local"
root = "storage/adjuntos"
visibility = "private"
"""


def _storage_run(dockerfile: str) -> tuple[list[str], list[str]]:
    """``(mkdir -p operands, chown appuser operands)`` of the storage line."""
    joined = dockerfile.replace("\\\n", " ")
    line = next(line for line in joined.splitlines() if line.startswith("RUN mkdir -p "))
    mkdir, _, chown = line.partition("&&")
    made = mkdir.split()[3:]
    owned = chown.split()[2:]
    assert chown.split()[:2] == ["chown", "appuser:appuser"], line
    return made, owned


def test_the_defaults_alone_are_what_0_1_0a12_generated() -> None:
    made, owned = _storage_run(render_dockerfile())
    assert made == ["/app/storage/public", "/app/storage/private"]
    assert owned == ["/app", "/app/storage", "/app/storage/public", "/app/storage/private"]


def test_a_disk_of_its_own_is_created_and_handed_to_appuser() -> None:
    made, owned = _storage_run(
        render_dockerfile(disks={"adjuntos": {"driver": "local", "root": "storage/adjuntos"}})
    )
    assert "/app/storage/adjuntos" in made
    assert "/app/storage/adjuntos" in owned
    # The defaults stay: an image built before the disk existed still has them.
    assert {"/app/storage/public", "/app/storage/private"} <= set(made)


def test_nested_absolute_and_defaulted_roots() -> None:
    disks = {
        "tickets": {"root": "uploads/tickets/2026"},  # driver defaults to local
        "scratch": {"driver": "local"},  # root defaults to "storage"
        "mounted": {"driver": "local", "root": "/data/files"},
        "media": {"driver": "s3", "bucket": "media"},
    }
    made, owned = _storage_run(render_dockerfile(disks=disks))
    assert "/app/uploads/tickets/2026" in made
    # mkdir -p creates the parents as root; they are handed over too.
    assert {"/app/uploads", "/app/uploads/tickets", "/app/uploads/tickets/2026"} <= set(owned)
    assert "/data/files" in made and "/data/files" in owned
    assert "/app/storage" in made or "/app/storage" in owned
    assert not any("media" in word for word in made)


def test_the_image_creates_exactly_what_compose_mounts() -> None:
    """One resolver behind both, so a mounted disk is always a created one."""
    disks = {
        "adjuntos": {"driver": "local", "root": "storage/adjuntos"},
        "legacy": {"root": "./old//uploads/"},
        "media": {"driver": "s3", "bucket": "media"},
    }
    mounted = [
        m.split(":", 1)[1] for m in storage_mounts(StoragePlugin({"disks": disks}), prefix="x")
    ]
    assert mounted == list(local_disk_dirs(disks).values())
    assert set(mounted) <= set(storage_dirs(disks))
    assert "/app/old/uploads" in mounted


def test_jfast_deploy_dockerfile_reads_the_disks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jfast.toml").write_text(
        '[app]\nname = "mesa"\n\n[plugins]\nenabled = ["storage"]\n' + ADJUNTOS,
        encoding="utf-8",
    )
    result = runner.invoke(app, ["deploy", "dockerfile"])
    assert result.exit_code == 0, result.output
    made, owned = _storage_run((tmp_path / "Dockerfile").read_text(encoding="utf-8"))
    assert "/app/storage/adjuntos" in made and "/app/storage/adjuntos" in owned
    assert "/app/storage/adjuntos" in result.output


def test_jfast_deploy_dockerfile_without_a_jfast_toml_keeps_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["deploy", "dockerfile", "--stdout"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == render_dockerfile().strip()


def test_refresh_rewrites_only_the_storage_line() -> None:
    edited = render_dockerfile().replace(
        "COPY . .\n", "COPY . .\n# ours: a private index\nRUN echo keep-me\n", 1
    )
    refreshed = refresh_storage_block(edited, {"adjuntos": {"root": "storage/adjuntos"}})
    assert refreshed is not None
    assert "RUN echo keep-me" in refreshed
    made, _ = _storage_run(refreshed)
    assert "/app/storage/adjuntos" in made
    # The rest of the file is untouched, byte for byte.
    assert refreshed.replace("\n", "").count("WORKDIR created /app as root") == 1
    assert edited.split("# WORKDIR created")[0] == refreshed.split("# WORKDIR created")[0]
    assert edited.split("EXPOSE 8000")[1] == refreshed.split("EXPOSE 8000")[1]
    # Twice is once.
    assert refresh_storage_block(refreshed, {"adjuntos": {"root": "storage/adjuntos"}}) == (
        refreshed
    )


def test_refresh_leaves_a_dockerfile_without_the_generated_line() -> None:
    assert refresh_storage_block("FROM python:3.12\nUSER appuser\n", None) is None


def test_read_storage_disks(tmp_path: Path) -> None:
    config = tmp_path / "jfast.toml"
    assert read_storage_disks(config) is None
    config.write_text("[plugin.storage]\ndefault = 'public'\n", encoding="utf-8")
    assert read_storage_disks(config) is None
    config.write_text(ADJUNTOS, encoding="utf-8")
    assert set(read_storage_disks(config) or {}) == {"public", "private", "adjuntos"}
    config.write_text("[plugin.storage\n", encoding="utf-8")
    assert read_storage_disks(config) is None


def test_jfast_add_storage_refreshes_the_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the help desk took: storage on, then a disk declared by hand.

    Run again, `jfast add storage` has nothing to enable and still gives the
    image the new root -- and keeps an edit made elsewhere in the Dockerfile.
    """
    service = tmp_path / "mesa"
    created = runner.invoke(app, ["new", "service", "mesa", "--target", str(service)])
    assert created.exit_code == 0, created.output
    dockerfile = service / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "COPY . .\n", "COPY . .\n# ours\nRUN echo keep-me\n", 1
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(service)
    first = runner.invoke(app, ["add", "storage", "--no-install"])
    assert first.exit_code == 0, first.output

    config = service / "jfast.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + '\n[plugin.storage.disks.adjuntos]\ndriver = "local"\nroot = "storage/adjuntos"\n',
        encoding="utf-8",
    )
    again = runner.invoke(app, ["add", "storage", "--no-install"])
    assert again.exit_code == 0, again.output
    text = dockerfile.read_text(encoding="utf-8")
    made, owned = _storage_run(text)
    assert "/app/storage/adjuntos" in made and "/app/storage/adjuntos" in owned
    assert "RUN echo keep-me" in text
    assert re.search(r"workspace compose", " ".join(again.output.split()))
