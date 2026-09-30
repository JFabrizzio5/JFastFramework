"""The import the docs show, ``from jfastframework.auth import require_auth``,
type-checks under ``mypy --strict``.

The dependencies are exposed lazily by a module ``__getattr__`` so that
``jfastframework.auth`` stays importable without FastAPI. A ``__getattr__`` is
all mypy could see, and it was typed ``-> object``, so every generated project
that followed the docs failed its own strict gate with::

    Argument 1 to "Depends" has incompatible type "object"

Found by a developer building a help desk on 0.1.0a12 (bitácora F4).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

DOCUMENTED = """\
from fastapi import APIRouter, Depends

from jfastframework.auth import (
    Principal,
    optional_auth,
    principal_of,
    require_auth,
    require_roles,
    require_scopes,
)

router = APIRouter()


@router.get("/me")
async def me(caller: Principal = Depends(require_auth)) -> str:
    return caller.subject


@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))) -> str:
    return caller.subject


@router.delete("/invoices")
async def purge(caller: Principal = Depends(require_roles("admin"))) -> str:
    return caller.subject


@router.get("/maybe")
async def maybe(caller: Principal | None = Depends(optional_auth)) -> str:
    return caller.subject if caller else ""


@router.get("/raw")
async def raw(caller: Principal = Depends(principal_of)) -> str:
    return caller.subject
"""


def test_the_lazy_names_are_the_plugins_dependencies() -> None:
    import jfastframework.auth as public
    from jfastframework.plugins.builtin import auth as plugin

    for name in ("require_auth", "require_scopes", "require_roles", "optional_auth"):
        assert getattr(public, name) is getattr(plugin, name)
    assert public.principal_of is plugin.principal_of


def test_importing_the_package_still_does_not_import_the_plugin() -> None:
    # The TYPE_CHECKING block must never run: a worker imports the token
    # functions without FastAPI's request machinery.
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, jfastframework.auth; "
            "print('jfastframework.plugins.builtin.auth' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "False"


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy not installed")
def test_the_documented_import_passes_mypy_strict(tmp_path: Path) -> None:
    snippet = tmp_path / "routes.py"
    snippet.write_text(DOCUMENTED, encoding="utf-8")
    # Run from tmp_path so this repository's [tool.mypy] is not the config: a
    # generated project has its own, and --strict is what it sets.
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(snippet),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
