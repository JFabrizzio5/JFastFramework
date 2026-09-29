"""Helpers every command module shares: output, and the config it reads.

They sat at the top of ``main.py`` while every command lived there. Here they
are imported once, so no command module has to import another one to print.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Any

import typer

from jfastframework.cli import ui
from jfastframework.settings import JFastConfig


def _echo(payload: Any, as_json: bool, human: str) -> None:
    if as_json:
        typer.echo(jsonlib.dumps(payload, indent=2, default=str))
    else:
        typer.echo(human)


def _load(config_path: str) -> JFastConfig:
    return JFastConfig.load(config_path=config_path)


def _root_of(config_path: str | Path) -> Path:
    """The project directory a jfast.toml describes -- its own."""
    return Path(config_path).resolve().parent


def _declared_paths(config: JFastConfig) -> dict[str, str]:
    """``[plugins.paths]``: plugins that live in the project, not in a wheel."""
    paths: dict[str, str] = config.raw.get("plugins", {}).get("paths", {})
    return paths


def _report(written: list[Any], title: str = "") -> None:
    """Show what a scaffold wrote.

    Every generating command funnels through here, so the shape of the output
    is decided once. A tree rather than a flat list: forty paths in a column is
    a wall, and the line that actually matters -- a file left alone because it
    already existed -- reads the same as the thirty-nine that were written.

    Falls back to one line per file when the paths share no root to hang a tree
    from, which is the case for the commands that write a single file next to
    the caller.
    """
    if not written:
        return

    paths = [(str(item.path), bool(item.created)) for item in written]
    roots = {path.replace("\\", "/").split("/")[0] for path, _ in paths}
    if len(roots) == 1 and len(paths) > 1:
        # The root becomes the tree's label, so it is stripped from the
        # branches: printing it once at the top and again on every path is how
        # a tree ends up wider and less readable than the list it replaced.
        root = next(iter(roots))
        relative = [
            (path.replace("\\", "/").removeprefix(f"{root}/"), created) for path, created in paths
        ]
        ui.file_tree(title or f"{root}/", relative)
        return

    for path, was_created in paths:
        if was_created:
            ui.created(path)
        else:
            ui.note(f"{ui.G.bullet} {path}  exists, left alone")
