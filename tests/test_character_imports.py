"""Public behavior of the Shrek and McQueen terminal imports."""

from __future__ import annotations

import contextlib
import importlib
import io
import math
import sys

import pytest

import mcqueen
import shrek


def test_imports_have_no_terminal_or_browser_side_effects() -> None:
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        importlib.reload(shrek)
        importlib.reload(mcqueen)

    assert output.getvalue() == ""


def test_shrek_show_prints_bundled_art() -> None:
    output = io.StringIO()

    shrek.show(output)

    assert output.getvalue() == shrek.ART


def test_mcqueen_show_prints_bundled_art() -> None:
    output = io.StringIO()

    mcqueen.show(output)

    assert output.getvalue() == mcqueen.ART


def test_combining_the_imports_opens_the_configured_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int]] = []

    def open_url(url: str, new: int = 0) -> bool:
        opened.append((url, new))
        return True

    monkeypatch.setattr(shrek.webbrowser, "open", open_url)

    assert shrek.play_video(mcqueen) is True
    assert opened == [(shrek.VIDEO_URL, 2)]


def test_combination_rejects_an_unrelated_module() -> None:
    with pytest.raises(TypeError, match="mcqueen module"):
        shrek.play_video(math)


def test_modules_are_importable_by_their_public_names() -> None:
    assert sys.modules["shrek"] is shrek
    assert sys.modules["mcqueen"] is mcqueen
