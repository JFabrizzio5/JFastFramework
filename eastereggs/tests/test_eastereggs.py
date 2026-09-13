"""Public behaviour of the terminal easter eggs."""

from __future__ import annotations

import contextlib
import importlib
import io
import math
import webbrowser
from types import ModuleType

import pytest

import mcqueen
import pene
import shrek
import vagina

EGGS = [shrek, mcqueen, pene, vagina]
PAIRS = [(shrek, mcqueen), (pene, vagina)]


def _name(module: ModuleType) -> str:
    return module.__name__


@pytest.mark.parametrize("egg", EGGS, ids=_name)
def test_importing_prints_nothing_and_opens_nothing(
    egg: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str, new: int = 0) -> bool:
        raise AssertionError(f"import opened {url}")

    monkeypatch.setattr(webbrowser, "open", refuse)
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        importlib.reload(egg)

    assert output.getvalue() == ""


@pytest.mark.parametrize("egg", EGGS, ids=_name)
def test_show_prints_the_bundled_art(egg: ModuleType) -> None:
    output = io.StringIO()

    egg.show(output)

    assert output.getvalue() == egg.ART


@pytest.mark.parametrize("egg", EGGS, ids=_name)
def test_the_art_starts_on_its_first_row(egg: ModuleType) -> None:
    """`r\"\"\"\\` keeps the backslash-newline, so the art opened with a stray `\\`.

    Comparing `show()` against `ART` cannot see it: both carry the backslash.
    """
    assert not egg.ART.startswith(("\\", "\n"))


@pytest.mark.parametrize(("opener", "companion"), PAIRS, ids=lambda m: _name(m))
def test_the_pair_opens_the_configured_video(
    opener: ModuleType, companion: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, int]] = []

    def open_url(url: str, new: int = 0) -> bool:
        opened.append((url, new))
        return True

    monkeypatch.setattr(webbrowser, "open", open_url)

    assert opener.play_video(companion) is True
    assert opened == [(opener.VIDEO_URL, 2)]


@pytest.mark.parametrize(("opener", "companion"), PAIRS, ids=lambda m: _name(m))
def test_play_video_refuses_any_other_module(
    opener: ModuleType, companion: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str, new: int = 0) -> bool:
        raise AssertionError(f"opened {url} for the wrong module")

    monkeypatch.setattr(webbrowser, "open", refuse)

    with pytest.raises(TypeError, match=companion.__name__):
        opener.play_video(math)
