#!/usr/bin/env bash
# Export real spans over OTLP/HTTP and read them back from Jaeger.
#
#     JAEGER_URL=http://localhost:16686 \
#     OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
#     bash scripts/smoke_telemetry.sh
#
# tests/test_telemetry.py checks every span the framework makes, but against
# the in-memory exporter: nothing there serialises a span, sends it over HTTP,
# or proves a backend accepts what it receives. This does -- one request to a
# service with the telemetry plugin exporting to Jaeger's OTLP port, the
# lifespan closed so the batch is flushed, then Jaeger's query API asked for
# the service by name until the route-template span shows up.
#
# Needs a Jaeger (2.x all-in-one: OTLP/HTTP on 4318, query API on 16686) and
# `pip install -e ".[telemetry,dev]"`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

export JAEGER_URL="${JAEGER_URL:-http://localhost:16686}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
# One name per run, so a Jaeger that outlives a run cannot answer for the next.
export SMOKE_SERVICE="jfast-telemetry-smoke-$$-$(date +%s)"

"${PYTHON}" - <<'PY'
import asyncio
import os
import sys
import time

import httpx
from fastapi import APIRouter

from jfastframework.testing import build_test_app, client_for

JAEGER = os.environ["JAEGER_URL"].rstrip("/")
SERVICE = os.environ["SMOKE_SERVICE"]
EXPECTED = "GET /users/{user_id}"


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/users/{user_id}")
    async def user(user_id: int) -> dict[str, int]:
        return {"id": user_id}

    return r


async def send() -> None:
    raw = {"plugin": {"telemetry": {"exporter": "otlp", "set_global_provider": False}}}
    app = build_test_app(
        plugins=["observability", "telemetry"], routers=[router()], raw=raw, app_name=SERVICE
    )
    # Leaving client_for runs the lifespan's shutdown, which flushes the batch.
    async with client_for(app) as client:
        for user_id in (1, 2, 3):
            response = await client.get(f"/users/{user_id}")
            assert response.status_code == 200, response.text
        ready = (await client.get("/ready")).json()
        print(f"/ready telemetry: {ready.get('checks', ready).get('telemetry')}")


def span_names() -> set[str]:
    response = httpx.get(f"{JAEGER}/api/traces", params={"service": SERVICE, "limit": 20})
    if response.status_code != 200:
        return set()
    return {
        span["operationName"]
        for trace in response.json().get("data") or []
        for span in trace.get("spans", [])
    }


asyncio.run(send())

deadline = time.monotonic() + 60
names: set[str] = set()
while time.monotonic() < deadline:
    names = span_names()
    if EXPECTED in names:
        print(f"ok: Jaeger has {SERVICE} with spans {sorted(names)}")
        sys.exit(0)
    time.sleep(2)

print(f"FAIL: no '{EXPECTED}' span for {SERVICE} in Jaeger after 60 s; saw {sorted(names)}")
sys.exit(1)
PY
