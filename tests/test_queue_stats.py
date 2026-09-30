"""GET /queue/stats: open in development, closed in production unless chosen.

Found building a SaaS from scratch: it answered every anonymous caller with
the service's task names and queue depths, on by default in every
environment, and no production checklist mentioned it.
"""

from __future__ import annotations

import pytest

from jfastframework.testing import build_test_app, client_for

STUB = "tests.test_scheduler:_Stub"


async def _status(env: str, **queue: object) -> int:
    app = build_test_app(
        plugins=["queue"], env=env, raw={"plugin": {"queue": {"backend": STUB, **queue}}}
    )
    async with client_for(app) as client:
        return (await client.get("/queue/stats")).status_code


@pytest.mark.parametrize(
    ("env", "queue", "exposed"),
    [
        ("local", {}, True),
        ("prod", {}, False),
        ("prod", {"expose_stats": True}, True),
        ("local", {"expose_stats": False}, False),
    ],
)
async def test_stats_follow_the_environment_unless_chosen(
    env: str, queue: dict[str, object], exposed: bool
) -> None:
    assert await _status(env, **queue) == (200 if exposed else 404)
