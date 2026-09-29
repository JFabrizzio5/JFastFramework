"""Marker-based patching of hand-written files.

Generators that only create files stop being useful the moment the new code
has to be *registered* somewhere -- a route table, a sidebar, a plugin list.
So the generated frontend ships marker comments:

    /*nuevaRuta*/       in src/router/index.js
    /*nuevoModulo*/     in src/menuAside.js

and this module splices into them.

Three properties the naive version of this gets wrong, and which are the whole
reason it lives in its own tested module:

1. **Idempotent.** Running the generator twice must not produce two routes and
   two sidebar entries. Every insertion carries a guard string; if the guard is
   already in the file, nothing happens.
2. **Loud.** A missing file or a missing marker raises with the path and the
   marker in the message. Silently doing nothing is how you end up debugging a
   blank page.
3. **Marker-preserving.** The marker is written back after the inserted block,
   so the next module has somewhere to go.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class PatchError(RuntimeError):
    """A file could not be patched, with the reason in the message."""


@dataclass
class PatchResult:
    path: Path
    changed: bool
    reason: str

    def __str__(self) -> str:
        state = "patched" if self.changed else "unchanged"
        return f"  {state:<18} {self.path}  ({self.reason})"


def _marker_pattern(marker: str, path: Path | None = None) -> re.Pattern[str]:
    """Match a marker tolerantly: any inner spacing, any case.

    Two comment syntaxes, chosen by the file being patched rather than by a
    flag the caller has to remember::

        /*nuevaRuta*/         .js, .ts, .vue
        # [jfast:routers]     .py

    A Python file cannot carry the first form -- it is a syntax error, not a
    comment -- so with the frontend's marker alone the backend could never be
    patched.

    Hand-edited files drift: somebody reformats and ``/*nuevaRuta*/`` becomes
    ``/* nuevaRuta */``. Matching strictly would turn that into a silent no-op,
    which is exactly the failure this module exists to prevent.
    """
    name = re.escape(marker)
    if path is not None and path.suffix == ".py":
        return re.compile(rf"#\s*\[\s*{name}\s*\]", re.IGNORECASE)
    return re.compile(rf"/\*\s*{name}\s*\*/", re.IGNORECASE)


def _marker_text(marker: str, path: Path) -> str:
    """The marker as it should be written back, in this file's syntax."""
    if path.suffix == ".py":
        return f"# [{marker}]"
    return f"/*{marker}*/"


def insert_at_marker(
    path: Path,
    marker: str,
    block: str,
    *,
    guard: str,
    indent: str = "  ",
) -> PatchResult:
    """Insert ``block`` where ``marker`` sits, keeping the marker after it.

    Args:
        path: File to patch.
        marker: Marker name, without the comment syntax (``nuevaRuta``).
        block: Text to insert.
        guard: Substring whose presence means this insertion already happened.
        indent: Leading whitespace applied to every inserted line.
    """
    if not path.is_file():
        raise PatchError(
            f"Cannot patch {path}: file does not exist. Run this from the frontend project root."
        )

    content = path.read_text(encoding="utf-8")

    if guard in content:
        return PatchResult(path, changed=False, reason="already registered")

    pattern = _marker_pattern(marker, path)
    if not pattern.search(content):
        raise PatchError(
            f"Cannot patch {path}: marker {_marker_text(marker, path)} not found. "
            f"Put it back where generated entries should go, or add the entry "
            f"by hand."
        )

    indented = "\n".join(indent + line if line.strip() else line for line in block.splitlines())
    # The marker goes back after the block so the next module has a home.
    replacement = f"{indented}\n{indent}{_marker_text(marker, path)}"
    patched = pattern.sub(lambda _: replacement.lstrip(), content, count=1)

    path.write_text(patched, encoding="utf-8")
    return PatchResult(path, changed=True, reason=f"inserted at {_marker_text(marker, path)}")


def ensure_import(path: Path, statement: str, *, guard: str | None = None) -> PatchResult:
    """Add an import line if it is not already there.

    Placed after the file's existing leading imports rather than at line 1, so
    the result stays sorted enough for a linter to leave alone.
    """
    if not path.is_file():
        raise PatchError(f"Cannot patch {path}: file does not exist.")

    content = path.read_text(encoding="utf-8")
    if (guard or statement) in content:
        return PatchResult(path, changed=False, reason="import already present")

    lines = content.splitlines()
    last_import = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "const ")) and (
            " from " in stripped or "require(" in stripped
        ):
            last_import = index
        elif last_import >= 0 and stripped and not stripped.startswith(("import", "//", "/*", "*")):
            break

    insert_at = last_import + 1
    lines.insert(insert_at, statement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return PatchResult(path, changed=True, reason="import added")


def ensure_named_import(path: Path, module: str, name: str) -> PatchResult:
    """Add ``name`` to an existing ``import { ... } from 'module'`` line.

    Used for icon imports: the sidebar already imports from ``@mdi/js``, and a
    new module needs one more name from that same statement rather than a
    second import of the same module.
    """
    if not path.is_file():
        raise PatchError(f"Cannot patch {path}: file does not exist.")

    content = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"import\s*\{(?P<names>[^}]*)\}\s*from\s*['\"]" + re.escape(module) + r"['\"]"
    )
    match = pattern.search(content)

    if match is None:
        return ensure_import(path, f"import {{ {name} }} from '{module}'")

    existing = [part.strip() for part in match.group("names").split(",") if part.strip()]
    if name in existing:
        return PatchResult(path, changed=False, reason=f"{name} already imported")

    merged = ", ".join(sorted([*existing, name]))
    patched = (
        content[: match.start()] + f"import {{ {merged} }} from '{module}'" + content[match.end() :]
    )
    path.write_text(patched, encoding="utf-8")
    return PatchResult(path, changed=True, reason=f"{name} added to {module} import")
