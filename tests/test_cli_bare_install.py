"""`jfast` works on `pip install jfastframework` -- no extras at all.

The release workflow installs the bare wheel and runs `jfast version`. In
0.1.0a11 that failed: `jfast tenancy` imported `jfastframework.db` at module
level, which needs SQLAlchemy (the `db` extra), and the CLI imports every
command module to register it -- so one command broke all of them. The test
suite never saw it, because the development venv has every extra installed.

This runs the CLI in a subprocess where every optional dependency is made
unimportable, and asks every command and subcommand for its help.
"""

from __future__ import annotations

import subprocess  # nosec B404 - this interpreter, a fixed script
import sys
import textwrap

#: Top-level import names of every optional extra in pyproject.toml. Not
#: `opentelemetry`: FastAPI 0.142 imports its API itself, so every install has it.
OPTIONAL = [
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "redis",
    "prometheus_client",
    "sentry_sdk",
    "motor",
    "pymongo",
    "qdrant_client",
    "httpx",
    "multipart",
    "python_multipart",
    "openpyxl",
    "pypdf",
    "img2pdf",
    "PIL",
    "weasyprint",
    "lxml",
    "signxml",
    "polars",
    "pandas",
    "cv2",
    "email_validator",
    "phonenumbers",
    "babel",
    "tenacity",
    "jwt",
    "argon2",
    "cryptography",
    "aio_pika",
    "boto3",
    "botocore",
    "google",
    "aiokafka",
    "arq",
    "uvicorn",
]

SCRIPT = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED = set(sys.argv[1:])

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"No module named {name!r} (blocked)", name=name)
            return None

    sys.meta_path.insert(0, Block())

    import click
    import typer
    from typer.testing import CliRunner

    from jfastframework.cli.main import app

    runner = CliRunner()
    root = typer.main.get_command(app)
    failures = []

    def walk(command, path):
        result = runner.invoke(app, [*path, "--help"])
        if result.exit_code != 0 or result.exception is not None:
            failures.append((" ".join(path) or "jfast", repr(result.exception)))
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])

    walk(root, [])
    version = runner.invoke(app, ["version"])
    if version.exit_code != 0:
        failures.append(("version", repr(version.exception)))
    for where, why in failures:
        print(f"FAIL {where}: {why}")
    sys.exit(1 if failures else 0)
    """
)


def test_every_command_loads_without_any_extra() -> None:
    result = subprocess.run(  # nosec B603 - this interpreter, a fixed script
        [sys.executable, "-c", SCRIPT, *OPTIONAL],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
