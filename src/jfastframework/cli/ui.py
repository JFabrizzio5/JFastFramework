"""Terminal presentation for the interactive commands.

Built on ``rich``, which arrives with Typer -- no new dependency for what is,
after all, decoration. That constraint is also why there is no arrow-key
multi-select here: rich does not do raw keyboard input, and adding a library
that does would put a dependency in every install so that one command in one
mode looks nicer. Numbered choices and grouped confirmations get most of the
way there and work over ssh, in CI logs, and in a terminal with no colour.

Everything degrades, in two directions. ``rich`` detects a non-tty and drops
styling on its own, so the same code produces clean output when piped to a
file -- which matters, because the installer's transcript is something people
paste into issues. And every symbol comes from :mod:`.glyphs`, resolved against
the console's real encoding, because a Windows codepage without ``✓`` in it
does not print a placeholder: it raises ``UnicodeEncodeError`` and kills the
installer halfway through writing a project.

The palette is the documentation site's, so a screenshot of the terminal and a
screenshot of the docs look like the same product: crimson on near-black, with
zinc greys for everything that is not the point of the line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from jfastframework.cli import glyphs

# RED.CORE: brand-500 for anything that has to catch the eye on black,
# brand-600 for borders, which sit behind the content and should not compete.
ACCENT = "bold #ef4444"
ACCENT_DEEP = "#dc2626"
# zinc-400 and zinc-500. Two greys, because one grey makes a hint and a label
# look like the same kind of thing.
DIM = "#a1a1aa"
FAINT = "#71717a"
TEXT = "#f4f4f5"
OK = "bold #4ade80"

console = Console()

#: Resolved once. A console's encoding does not change under it, and probing
#: per line would put a try/except in the middle of every print.
G = glyphs.for_console(console)


@dataclass(frozen=True)
class Choice:
    """One option in a select."""

    key: str
    label: str
    hint: str = ""


def banner(subtitle: str = "") -> None:
    """The mark, drawn in the terminal.

    Block characters rather than an ASCII-art font: it reads as the logo at any
    width and does not become a wall of noise in a CI log. On a codepage that
    cannot hold them the mark degrades to the wordmark alone -- losing the
    drawing is better than losing the command.
    """
    console.print()
    if G.unicode:
        mark = Text()
        mark.append("  ▟█████▙\n", style=ACCENT)
        mark.append(" ▟█▛▔▔▔▔▔\n", style=ACCENT)
        mark.append("▔▔▔▜█▙\n", style=ACCENT)
        mark.append("  ▚▄▄▟█▛\n", style=ACCENT)
        console.print(mark)

    title = Text()
    title.append("jfast", style=ACCENT)
    title.append("framework", style=f"bold {TEXT}")
    if subtitle:
        title.append(f"\n{subtitle}", style=FAINT)
    console.print(Panel.fit(title, border_style=ACCENT_DEEP, box=G.panel_box, padding=(0, 2)))


def rule(text: str) -> None:
    console.print()
    console.rule(Text(text, style=ACCENT), style=FAINT, align="left")


def _ask(prompt: str) -> str:
    """Read one answer, and keep the transcript readable when piped.

    An interactive answer ends with the user's Enter. Piped input carries no
    such newline, so the next question would land on the same line as the
    previous answer -- which is exactly the transcript somebody pastes into an
    issue.
    """
    answer = console.input(prompt)
    if not console.is_terminal:
        console.print()
    return answer.strip()


def select(question: str, choices: list[Choice], *, default: str) -> str:
    """Ask for one option, presented as a table and answered by key."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column(style=TEXT, no_wrap=True)
    table.add_column(style=FAINT)

    for choice in choices:
        marker = G.pointer if choice.key == default else " "
        # A choice whose label is its key printed the word twice.
        label = "" if choice.label == choice.key else choice.label
        table.add_row(f"  {marker} {choice.key}", label, choice.hint)

    console.print()
    console.print(Text(question, style="bold"))
    console.print(table)

    valid = {c.key for c in choices}
    while True:
        answer = _ask(f"[{FAINT}]  choice \\[{default}] {G.pointer} [/]") or default
        if answer in valid:
            return answer
        console.print(f"  [{ACCENT}]Pick one of: {', '.join(sorted(valid))}[/]")


def confirm(question: str, *, default: bool = False, hint: str = "") -> bool:
    """A yes/no, with the hint on the same line so the choice is informed."""
    suffix = "Y/n" if default else "y/N"
    line = Text("  ")
    line.append(question, style=TEXT)
    if hint:
        line.append(f"  {hint}", style=FAINT)
    console.print(line)
    # Escaped: rich reads square brackets as markup, so an unescaped "[y/N]"
    # is parsed as a style name and swallowed -- the prompt then showed the
    # default for one question and nothing for the rest.
    answer = _ask(f"[{FAINT}]      \\[{suffix}] {G.pointer} [/]").lower()
    if not answer:
        return default
    return answer in {"y", "yes", "s", "si", "sí"}


def multiselect(question: str, choices: list[Choice], *, defaults: set[str]) -> list[str]:
    """Several options, asked one at a time.

    A checkbox list needs raw keyboard handling, which is the dependency this
    module exists to avoid. Asking in sequence is slower to answer and cannot
    be got wrong -- and it prints a transcript of what was decided, which a
    checkbox list does not.
    """
    console.print()
    console.print(Text(question, style="bold"))
    picked: list[str] = []
    for choice in choices:
        if confirm(
            f"{choice.key:<14}{choice.label}", default=choice.key in defaults, hint=choice.hint
        ):
            picked.append(choice.key)
    return picked


def summary(title: str, rows: list[tuple[str, str]]) -> None:
    """What is about to happen, before it happens."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style=FAINT, no_wrap=True)
    table.add_column(style=TEXT)
    for label, value in rows:
        table.add_row(label, value)
    console.print()
    console.print(
        Panel(
            table,
            title=Text(title, style=ACCENT),
            border_style=ACCENT_DEEP,
            box=G.panel_box,
            title_align="left",
            padding=(1, 2),
        )
    )


def created(path: str, note: str = "") -> None:
    line = Text(f"  {G.tick} ", style=OK)
    # Padded to a column, but never at the cost of running the note into the
    # path: a long path just pushes the note along instead of swallowing it.
    line.append(path.ljust(26) if len(path) < 26 else path, style=TEXT)
    if note:
        line.append(f"  {note}", style=FAINT)
    console.print(line)


def file_tree(title: str, paths: list[tuple[str, bool]], *, note: str = "") -> None:
    """The files a command wrote, as the shape they were written in.

    A scaffold produces forty-odd paths. Printed flat they are a wall nobody
    reads, and the one line that matters -- a file skipped because it already
    existed -- looks exactly like the thirty-nine that were not. A tree shows
    the shape instead, and the skipped ones are the only entries not in green.

    ``paths`` is (path, created); ``created=False`` means the file was already
    there and was left alone. Directory nodes are inferred, so callers pass a
    flat list and get the nesting for free.

    Rich picks ASCII guides on its own when the console encoding cannot hold
    box-drawing characters, so the tree needs no fallback of its own -- unlike
    the panels, whose box style is the caller's choice and so is set here.
    """
    root = Tree(Text(title, style=ACCENT), guide_style=FAINT)
    nodes: dict[str, Tree] = {"": root}

    for path, was_created in sorted(paths):
        parts = path.replace("\\", "/").strip("/").split("/")
        prefix = ""
        for part in parts[:-1]:
            parent = prefix
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in nodes:
                nodes[prefix] = nodes[parent].add(Text(f"{part}/", style=DIM))
        leaf = Text()
        if was_created:
            leaf.append(f"{G.tick} ", style=OK)
            leaf.append(parts[-1], style=TEXT)
        else:
            leaf.append(f"{G.bullet} ", style=FAINT)
            leaf.append(f"{parts[-1]}  exists, left alone", style=FAINT)
        nodes[prefix].add(leaf)

    console.print()
    console.print(root)
    if note:
        console.print(Text(f"  {note}", style=FAINT))


def step(text: str) -> None:
    console.print(Text(f"  {text}", style=FAINT))


def next_steps(title: str, commands: list[tuple[str, str]]) -> None:
    """The commands to run now, with what each one does."""
    table = Table.grid(padding=(0, 3))
    table.add_column(style=ACCENT, no_wrap=True)
    table.add_column(style=FAINT)
    for command, description in commands:
        table.add_row(command, description)
    console.print()
    console.print(
        Panel(
            table,
            title=Text(title, style=f"bold {TEXT}"),
            border_style=ACCENT_DEEP,
            box=G.panel_box,
            title_align="left",
            padding=(1, 2),
        )
    )


def warn(text: str) -> None:
    console.print(Text(f"  {G.cross} {text}", style="bold #fbbf24"))


def note(text: str) -> None:
    console.print(Text(f"  {text}", style=FAINT))


def working(description: str) -> Any:
    """A spinner for the seconds a scaffold takes, as a context manager.

    ``dots`` is braille, which a legacy codepage cannot encode -- so on those
    terminals the spinner that exists to show the command is alive would be the
    thing that kills it. ``line`` is ASCII.
    """
    return console.status(Text(description, style=FAINT), spinner="dots" if G.unicode else "line")


def ask(question: str, *, default: str = "") -> str:
    """Free text, with the default shown and used when the answer is empty."""
    shown = f" \\[{default}]" if default else ""
    return _ask(f"[{FAINT}]  {question}{shown} {G.pointer} [/]") or default


def ask_int(question: str, *, default: int) -> int:
    """A number, re-asked rather than crashed on.

    Calling int() on whatever comes back would let one stray answer end the
    installer with a traceback after every other question had already been
    answered.
    """
    while True:
        raw = ask(question, default=str(default))
        try:
            return int(raw)
        except ValueError:
            console.print(f"  [{ACCENT}]{raw!r} is not a number.[/]")
