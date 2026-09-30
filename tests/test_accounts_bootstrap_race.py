"""Several processes creating the bootstrap administrator at once (F3).

The first `jfast dev` on an empty database starts the API and the worker
together, and the production image starts one uvicorn worker per CPU: every
one of them runs the accounts plugin's startup, sees no administrator, and
inserts the `admin` role. One wins; the rest died on
`uq_jfast_roles_tenant_name` with a 400-line traceback, and `jfast dev` took
everything down with them.

Against real PostgreSQL, because the race is between connections: each app
here has its own engine, and the startups run truly concurrently -- in one
event loop, and in separate processes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from jfastframework.accounts.models import roles, users
from jfastframework.testing import build_test_app

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
DSN = f"{PG_BASE}/jfast"
SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ADMIN = ("admin@example.com", "correct horse battery")
TABLES = (
    "jfast_user_sessions, jfast_recovery_codes, jfast_account_tokens, jfast_user_roles, "
    "jfast_role_permissions, jfast_roles, jfast_users"
)


@pytest.fixture
async def empty_database() -> str:
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLES}"))
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        pytest.skip(f"no PostgreSQL at {PG_BASE}")
    finally:
        await engine.dispose()
    return DSN


def _app(dsn: str) -> Any:
    return build_test_app(
        plugins=["database", "auth", "accounts"],
        raw={
            "plugin": {
                "database": {"dsn": dsn},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issue_tokens": True,
                },
                "accounts": {
                    "bootstrap_admin_email": ADMIN[0],
                    "bootstrap_admin_password": ADMIN[1],
                },
            }
        },
    )


async def _counts(dsn: str) -> tuple[int, int]:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            admins = await conn.scalar(
                select(func.count()).select_from(users).where(users.c.email == ADMIN[0])
            )
            admin_roles = await conn.scalar(
                select(func.count()).select_from(roles).where(roles.c.name == "admin")
            )
    finally:
        await engine.dispose()
    return int(admins or 0), int(admin_roles or 0)


async def test_concurrent_startups_create_one_administrator(empty_database: str) -> None:
    apps = [_app(empty_database) for _ in range(6)]
    contexts = [app.router.lifespan_context(app) for app in apps]
    # Every startup at once: each has its own engine, so each inserts on its
    # own connection, as separate processes do.
    results = await asyncio.gather(
        *(context.__aenter__() for context in contexts), return_exceptions=True
    )
    try:
        failures = [r for r in results if isinstance(r, BaseException)]
        assert failures == [], failures
    finally:
        await asyncio.gather(
            *(
                context.__aexit__(None, None, None)
                for context, result in zip(contexts, results, strict=True)
                if not isinstance(result, BaseException)
            ),
            return_exceptions=True,
        )
    assert await _counts(empty_database) == (1, 1)


async def test_a_restart_after_the_race_finds_the_administrator(empty_database: str) -> None:
    first = _app(empty_database)
    async with first.router.lifespan_context(first):
        pass
    again = _app(empty_database)
    async with again.router.lifespan_context(again):
        pass
    assert await _counts(empty_database) == (1, 1)


STARTUP = textwrap.dedent(
    """
    import asyncio, sys, time

    sys.path.insert(0, {tests!r})
    from test_accounts_bootstrap_race import _app

    async def main() -> None:
        app = _app({dsn!r})
        # Start together: every process waits for the same instant.
        await asyncio.sleep(max(0.0, {start} - time.time()))
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(main())
    """
)


async def test_concurrent_processes_create_one_administrator(
    empty_database: str, tmp_path: Path
) -> None:
    """What `jfast dev` (API + worker) and a multi-worker image actually do."""
    import time

    script = tmp_path / "startup.py"
    script.write_text(
        STARTUP.format(
            tests=str(Path(__file__).parent), dsn=empty_database, start=time.time() + 3.0
        ),
        encoding="utf-8",
    )
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(4)
    ]
    outputs = await asyncio.wait_for(
        asyncio.gather(*(process.communicate() for process in processes)), timeout=60
    )
    failed = [
        err.decode()[-2000:]
        for process, (_out, err) in zip(processes, outputs, strict=True)
        if process.returncode != 0
    ]
    assert failed == [], failed
    assert await _counts(empty_database) == (1, 1)
