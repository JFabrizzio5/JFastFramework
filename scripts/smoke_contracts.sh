#!/usr/bin/env bash
# Contracts, end to end.
#
#     bash scripts/smoke_contracts.sh
#
# The load-bearing assertion is that a *freshly generated* service passes its
# own contract. A contract the generator itself violates teaches everyone to
# ignore the checker on day one.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "a generated service has no contract until a layout exists"
"${JFAST}" new service billing --with database > /dev/null
cd billing
# Deliberate, and the fix for the defect this script missed. A service is
# generated before any module, so there is no layout to write a contract for;
# guessing one put the layered contract into hexagonal, modular and screaming
# services, where its globs matched nothing and every layer rule was inert.
test ! -f contracts.toml || fail "a service with no module cannot know its layout"

step "the first module writes it, for its own layout"
# Layered on purpose: the steps below edit layered files. The default layout
# is modular, and its contract is checked at the end.
"${JFAST}" new module invoice --layout layered > /dev/null
test -f contracts.toml || fail "the first module wrote no contracts.toml"
grep -qF 'modules/*/router.py' contracts.toml || fail "not the layered contract"

step "and the service passes it out of the box"
"${JFAST}" contracts check

# shared/enums.py ships with the service, and `[rules.placement]` -- plus the
# checker's own cross-module message, plus shared/README.md -- tells you to move
# the twice-wanted enum into it. If the contract rejects that import, following
# the framework's own instructions fails on a project one command old, which
# teaches everyone to ignore the checker on day one.
step "the documented shared/ move passes"
cp modules/invoice/service.py "${WORK}/service.py.bak"
printf '\nfrom shared.enums import Environment\n' >> modules/invoice/service.py
"${JFAST}" contracts check || fail "a module importing shared/ must pass its own contract"
cp "${WORK}/service.py.bak" modules/invoice/service.py

step "and the direction is still one-way"
cp shared/enums.py "${WORK}/enums.py.bak"
printf '\nfrom modules.invoice.models import Invoice\n' >> shared/enums.py
if "${JFAST}" contracts check > /tmp/contracts_shared.txt 2>&1; then
  fail "shared/ importing a module should not pass"
fi
grep -q 'shared-direction' /tmp/contracts_shared.txt \
  || fail "wrong rule: $(cat /tmp/contracts_shared.txt)"
cp "${WORK}/enums.py.bak" shared/enums.py
"${JFAST}" contracts check || fail "restoring shared/enums.py should clear the finding"

step "a layer violation is caught"
cat > modules/invoice/repository.py <<'PY'
from fastapi import APIRouter

from jfastframework.db import BaseRepository

from .models import Invoice


class InvoiceRepository(BaseRepository[Invoice]):
    model = Invoice
PY
if "${JFAST}" contracts check > /tmp/contracts.txt 2>&1; then
  fail "a storage layer importing fastapi should not pass"
fi
grep -q 'layer-package' /tmp/contracts.txt || fail "wrong rule fired: $(cat /tmp/contracts.txt)"
echo "caught: $(grep layer-package /tmp/contracts.txt | head -1)"

step "a waiver clears it, and is listed"
# -i.bak rather than -i: BSD sed (macOS) reads a bare -i's next argument as
# the backup suffix.
sed -i.bak 's|^from fastapi import APIRouter$|from fastapi import APIRouter  # contracts: allow spike, JF-1|' \
  modules/invoice/repository.py
rm modules/invoice/repository.py.bak
"${JFAST}" contracts check
"${JFAST}" contracts waivers | grep -q 'spike, JF-1' || fail "waiver not listed"

step "a forbidden call is caught"
git checkout modules/invoice/repository.py 2> /dev/null || true
cat > modules/invoice/service.py <<'PY'
import os


class InvoiceService:
    def __init__(self, repository: object) -> None:
        self.repository = repository
        self.mode = os.getenv("MODE")
PY
if "${JFAST}" contracts check > /tmp/contracts2.txt 2>&1; then
  fail "os.getenv outside settings should not pass"
fi
grep -q 'forbid-call' /tmp/contracts2.txt || fail "wrong rule: $(cat /tmp/contracts2.txt)"
grep -q 'Configuration is typed' /tmp/contracts2.txt || fail "the reason was not shown"

step "the contract is machine-readable"
"${JFAST}" contracts show --json | grep -q '"does_not_own"' || fail "no does_not_own in JSON"
# `check` exits non-zero by design, and pipefail would make that the
# pipeline's status. Capture first, then inspect.
CHECK_JSON="$("${JFAST}" contracts check --json || true)"
grep -q '"violations"' <<< "${CHECK_JSON}" || fail "no violations key"
grep -q '"ok": false' <<< "${CHECK_JSON}" || fail "ok should be false here"

step "CONTRACTS.md renders"
"${JFAST}" contracts render
grep -q 'May import' CONTRACTS.md || fail "no layer table"
grep -q 'contracts: allow' CONTRACTS.md || fail "no waiver instructions"

step "a module nobody chose a layout for gets the modular contract"
cd "${WORK}"
"${JFAST}" new service orders --with database > /dev/null
cd orders
"${JFAST}" new module order > /dev/null
grep -qF 'modules/*/api/*.py' contracts.toml || fail "not the modular contract"
"${JFAST}" contracts check

step "the screaming layout gets its own defaults, without being asked twice"
cd "${WORK}"
"${JFAST}" new service catalog --with database > /dev/null
cd catalog
"${JFAST}" new module product --layout screaming > /dev/null
grep -qF 'modules/*/use_cases/*.py' contracts.toml || fail "not the screaming contract"
"${JFAST}" contracts check

# The defect, reproduced on purpose: the layered contract over a screaming
# module. Nothing here is malformed and nothing imports anything it should not
# -- the contract simply describes a tree that is not this one, and before
# `layer-unmatched` existed that was reported as a pass.
step "a contract aimed at another layout is reported, not passed"
cd "${WORK}"
"${JFAST}" new service warehouse --with database > /dev/null
cd warehouse
"${JFAST}" contracts init --layout layered > /dev/null
"${JFAST}" new module pallet --layout hexagonal > /dev/null
if "${JFAST}" contracts check > /tmp/contracts_layout.txt 2>&1; then
  fail "a contract matching none of the code should not pass: $(cat /tmp/contracts_layout.txt)"
fi
grep -q 'layer-unmatched' /tmp/contracts_layout.txt \
  || fail "wrong rule: $(cat /tmp/contracts_layout.txt)"
echo "caught: $(grep layer-unmatched /tmp/contracts_layout.txt | head -1)"

step "a blocking call in a coroutine is caught"
cd "${WORK}"
"${JFAST}" new service reporting --with cache > /dev/null
cd reporting
# No module here, so no contract came with one. The event-loop rules are
# service-wide and this is the documented way to get them on their own.
"${JFAST}" contracts init > /dev/null
cat > blocking_demo.py <<'PY'
import time

import requests


def warm_cache() -> None:
    time.sleep(1)


class Reporter:
    async def send(self, url: str) -> int:
        warm_cache()
        return requests.get(url).status_code
PY
if "${JFAST}" contracts check > /tmp/contracts3.txt 2>&1; then
  fail "a blocking call inside async def should not pass"
fi
grep -q 'async-blocking' /tmp/contracts3.txt || fail "wrong rule: $(cat /tmp/contracts3.txt)"
grep -q 'requests.get() blocks the event loop' /tmp/contracts3.txt   || fail "the direct blocking call was missed: $(cat /tmp/contracts3.txt)"
grep -q 'which blocks the event loop' /tmp/contracts3.txt   || fail "the synchronous helper was missed: $(cat /tmp/contracts3.txt)"

step "offloading it correctly clears the finding"
cat > blocking_demo.py <<'PY'
import asyncio
import time

import httpx


def warm_cache() -> None:
    time.sleep(1)


class Reporter:
    async def send(self, url: str) -> int:
        await asyncio.to_thread(warm_cache)
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
        return response.status_code
PY
"${JFAST}" contracts check > /dev/null 2>&1 || fail "correct async code must pass"
rm -f blocking_demo.py

printf '\nCONTRACTS SMOKE OK\n'
