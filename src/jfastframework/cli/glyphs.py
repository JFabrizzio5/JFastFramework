"""Symbols that survive the terminal they are printed to.

A Windows console is cp1252 or cp850 far more often than it is UTF-8, and
neither codepage has ``✓``, ``▟`` or the rounded box characters ``rich`` draws
panels with. Writing one of those is not a cosmetic problem: ``sys.stdout``
raises ``UnicodeEncodeError`` mid-write, so the installer dies with a traceback
after it has already created half a project.

So every glyph is declared with an ASCII twin, and the set is resolved once
against the encoding the console actually reports. The output is plainer on a
legacy codepage and identical everywhere else, which is the right trade: a
box-drawing character is decoration, and the alternative is a crash.

The check is a real ``str.encode`` rather than a list of known-good codepages.
Encodings are added, terminals lie about themselves, and ``PYTHONIOENCODING``
overrides all of it -- asking the codec is the only answer that stays true.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from rich import box
from rich.console import Console

# Every non-ASCII character this package prints. If a glyph is added below it
# belongs here too, or the probe passes and the write still fails.
_PROBE = "─│╭╮╰╯├└✓✗›·▏█▟▙▛▜▔▚▄→—"


def encoding_supports(sample: str, encoding: str | None) -> bool:
    """Whether ``encoding`` can represent ``sample``.

    ``LookupError`` counts as a no: an encoding name Python does not know is
    not one we should be betting the output on.
    """
    if not encoding:
        return False
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def console_encoding(console: Console | None = None) -> str | None:
    """What the output stream will actually encode with.

    ``rich`` reports its own encoding, but a Console built around a file, a
    pipe or a captured buffer may not -- so fall back to the real stdout, and
    then to nothing, which reads as "assume the worst".
    """
    if console is not None:
        reported = getattr(console, "encoding", None)
        if reported:
            return str(reported)
    return getattr(sys.stdout, "encoding", None)


@dataclass(frozen=True)
class Glyphs:
    """One symbol per job, already resolved for this terminal."""

    unicode: bool

    tick: str
    cross: str
    arrow: str
    pointer: str
    bullet: str
    bar: str
    #: The separator in a heading like `billing - api`. Its own glyph because
    #: cp850 and cp437 have no em dash, and a literal one bypasses every
    #: check in this module on the way to the encoder.
    dash: str

    #: Tree branches drawn by hand rather than by ``rich.tree`` -- the
    #: module graph is plain text so it can be piped, diffed and asserted
    #: on in a test without a Console in the way.
    branch: str
    corner: str
    hbar: str

    #: Panel and table borders. ``rich`` will happily draw a rounded box into a
    #: codepage that cannot hold one, so the box is chosen here rather than
    #: left to the default.
    panel_box: box.Box
    #: Tree guides. Passed to ``rich.tree.Tree(guide_style=...)`` -- the shape
    #: itself comes from ``ascii_only`` on the render options, which is why the
    #: tree helper in ``ui`` sets that too.
    tree_ascii: bool

    @classmethod
    def resolve(cls, encoding: str | None) -> Glyphs:
        if encoding_supports(_PROBE, encoding):
            return cls(
                unicode=True,
                tick="✓",
                cross="✗",
                arrow="→",
                pointer="›",
                bullet="·",
                bar="▏",
                dash="—",
                branch="├",
                corner="└",
                hbar="─",
                panel_box=box.ROUNDED,
                tree_ascii=False,
            )
        # The ASCII set is chosen to stay legible rather than to imitate: "+"
        # reads as added, "->" as a direction. A "?" placeholder would not.
        return cls(
            unicode=False,
            tick="+",
            cross="x",
            arrow="->",
            pointer=">",
            bullet="-",
            bar="|",
            dash="--",
            branch="|",
            corner="`",
            hbar="-",
            panel_box=box.ASCII,
            tree_ascii=True,
        )


def for_console(console: Console | None = None) -> Glyphs:
    return Glyphs.resolve(console_encoding(console))


__all__ = ["Glyphs", "console_encoding", "encoding_supports", "for_console"]
