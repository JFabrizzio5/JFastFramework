"""Every `[section]` a help text names reaches the terminal.

Rich reads `[word]` as a style tag and drops it, so "Defaults to [scaffold]
language." printed "Defaults to  language." Found building a SaaS from
scratch. Escape with a backslash: `\\[scaffold]`.
"""

from __future__ import annotations

import re
from typing import Any

import typer
from typer.testing import CliRunner

from jfastframework.cli.main import app

BRACKETED = re.compile(r"(?<!\\)\[[A-Za-z_<>.\-]+\]")


def _walk(command: Any, path: list[str]) -> list[tuple[list[str], Any]]:
    # Not isinstance(click.Group): typer ships its own click, and a check
    # against the upstream class silently walked the root only.
    found = [(path, command)]
    for name, sub in (getattr(command, "commands", None) or {}).items():
        found += _walk(sub, [*path, name])
    return found


def test_no_bracketed_name_is_swallowed_by_the_help_renderer() -> None:
    runner = CliRunner()
    commands = _walk(typer.main.get_command(app), [])
    assert len(commands) > 50, "the walk did not reach the subcommands"
    lost: list[str] = []
    for path, command in commands:
        texts = [command.help or ""] + [str(getattr(p, "help", "") or "") for p in command.params]
        wanted = {match for text in texts for match in BRACKETED.findall(text)}
        if not wanted:
            continue
        shown = " ".join(
            runner.invoke(app, [*path, "--help"], env={"COLUMNS": "200"}).output.split()
        )
        lost += [f"jfast {' '.join(path)}: {w}" for w in sorted(wanted) if w not in shown]
    assert lost == [], "escape these with a backslash: " + ", ".join(lost)
