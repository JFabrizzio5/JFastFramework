"""Public behavior of the two safe terminal novelty imports."""

from __future__ import annotations

import io
import math

import pytest

import pene
import vagina


def test_pene_prints_bundled_art() -> None:
    output = io.StringIO()

    pene.show(output)

    assert output.getvalue() == pene.ART


def test_vagina_prints_bundled_art() -> None:
    output = io.StringIO()

    vagina.show(output)

    assert output.getvalue() == vagina.ART


def test_combining_the_imports_opens_the_configured_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, int]] = []

    def open_url(url: str, new: int = 0) -> bool:
        opened.append((url, new))
        return True

    monkeypatch.setattr(pene.webbrowser, "open", open_url)

    assert pene.play_video(vagina) is True
    assert opened == [(pene.VIDEO_URL, 2)]


def test_combination_rejects_an_unrelated_module() -> None:
    with pytest.raises(TypeError, match="vagina module"):
        pene.play_video(math)
