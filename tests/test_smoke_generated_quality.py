"""Runs scripts/smoke_generated_quality.sh: every generated shape passes its gates.

Minutes, not seconds -- mypy --strict checks the framework each generated
project imports -- so it is off in the default run and on in the CI job that
exists for it::

    JFAST_SMOKE_GENERATED=1 pytest tests/test_smoke_generated_quality.py

The fast half, the templates on their own without mypy, is
tests/test_generator_fields.py and runs every time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_generated_quality.sh"

pytestmark = pytest.mark.skipif(
    os.environ.get("JFAST_SMOKE_GENERATED") != "1",
    reason="slow: set JFAST_SMOKE_GENERATED=1 to generate and gate every project shape",
)


def test_every_generated_project_passes_ruff_format_mypy_and_pytest() -> None:
    bin_dir = Path(sys.executable).parent
    jfast = bin_dir / "jfast"
    env = {
        **os.environ,
        "PY": sys.executable,
        "JFAST": str(jfast) if jfast.exists() else "jfast",
    }
    done = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout[-8000:] + done.stderr[-2000:]
    assert "OK: every generated project passes" in done.stdout
