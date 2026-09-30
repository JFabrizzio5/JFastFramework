#!/usr/bin/env bash
# A freshly generated project passes its own gates, with nothing edited.
#
# `jfast check` tells the user to run ruff, ruff format, mypy and pytest, and
# 0.1.0a10's `jfast start` + `jfast new module` failed three of the four on a
# tree nobody had touched: 19 lint errors (B008 on `Depends`, unsorted
# imports), 4 unformatted files, 5 type errors. The first thing an agent did in
# a new project was "fix" the generator's output, and the diff it produced was
# noise a reviewer had to read.
#
# So every shape the generator writes is generated here and gated:
#
#   start      `jfast start` (single-tenant, the default)
#   saas       `jfast start --multitenant` (tenancy, auth, accounts)
#   everything `jfast new service` with every plugin that needs no extra server
#
# and in each, one module per layout in each of its three forms -- the
# example fields, `--fields ... --unique ...`, and `--bare` -- plus a module
# whose fields use every type in the grammar. Then, per project:
#
#   ruff check .   ruff format --check .   mypy .   pytest
#
# The two installer paths also run `jfast check --ci` and, multitenant, `jfast
# check --multitenant-ready`, which must find nothing in code it just wrote.
#
#   JFAST=jfast PY=python scripts/smoke_generated_quality.sh
#
# Slow (mypy is strict and checks the framework it imports): a few minutes
# cold, well under one with a warm cache. MYPY_CACHE_DIR is shared between the
# projects so the framework is analysed once.
set -uo pipefail

JFAST=${JFAST:-jfast}
PY=${PY:-python3}
RUFF=${RUFF:-${PY} -m ruff}
MYPY=${MYPY:-${PY} -m mypy}
WORK=$(mktemp -d)
KEEP=${KEEP_WORK:-}
export MYPY_CACHE_DIR="${WORK}/.mypy_cache"
rc=0

cleanup() {
  saved=$?
  if [ -n "${KEEP}" ]; then
    echo "kept ${WORK}"
  else
    rm -rf "${WORK}"
  fi
  exit "${saved}"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*"
  rc=1
}

# Every plugin that boots without a server of its own besides PostgreSQL and
# Redis, which the generated compose provides. Left out: events (Kafka),
# mongo, qdrant, notifications (Firebase), gateway (a service kind of its own).
EVERYTHING="database,cache,telemetry,rag,llm,queue,outbox,idempotency,auth,accounts"
EVERYTHING="${EVERYTHING},ratelimit,channels,websocket,web,sentry,http,storage,tenancy,mail"

FIELDS="cartera_id:int, mes:str(7), gasto:money, leida:bool=false, nota:text?"
ALL_TYPES="a:int, b:bigint?, c:str(20)=abierto, d:text?, e:bool=true, f:float?"
ALL_TYPES="${ALL_TYPES}, g:decimal(12,2)=0, h:money, i:date, j:datetime?, k:json?"

modules() {
  local layout
  for layout in layered modular screaming hexagonal; do
    "${JFAST}" new module "ejemplo_${layout}" --layout "${layout}" > /dev/null \
      || fail "$1: new module ejemplo_${layout} (example fields)"
    "${JFAST}" new module "presupuesto_${layout}" --layout "${layout}" \
      --fields "${FIELDS}" --unique "cartera_id,mes" > /dev/null \
      || fail "$1: new module presupuesto_${layout} (--fields)"
    "${JFAST}" new module "vacio_${layout}" --layout "${layout}" --bare > /dev/null \
      || fail "$1: new module vacio_${layout} (--bare)"
  done
  "${JFAST}" new module tipos --layout modular --fields "${ALL_TYPES}" \
    --unique "a" --unique "c,i" > /dev/null \
    || fail "$1: new module tipos (every type)"
}

gates() {
  local name=$1
  echo "  ruff check"
  ${RUFF} check . --output-format concise || fail "${name}: ruff check"
  echo "  ruff format --check"
  ${RUFF} format --check . > /dev/null || { ${RUFF} format --diff . | head -40; fail "${name}: ruff format"; }
  echo "  mypy (strict)"
  ${MYPY} . || fail "${name}: mypy"
  echo "  pytest"
  "${PY}" -m pytest -q -p no:cacheprovider || fail "${name}: pytest"
}

echo "### start (single-tenant)"
mkdir -p "${WORK}/start" && cd "${WORK}/start"
if "${JFAST}" start tienda > start.log 2>&1; then
  cd tienda
  modules start
  gates start
  "${JFAST}" check --ci > check.log 2>&1 || { cat check.log; fail "start: jfast check --ci"; }
else
  cat start.log
  fail "jfast start"
fi

echo "### saas (jfast start --multitenant)"
mkdir -p "${WORK}/saas" && cd "${WORK}/saas"
if "${JFAST}" start tienda --multitenant > start.log 2>&1; then
  cd tienda
  modules saas
  gates saas
  "${JFAST}" check --ci > check.log 2>&1 || { cat check.log; fail "saas: jfast check --ci"; }
  # The routes generated for a multitenant service take the tenant from
  # current_tenant; the readiness report must have nothing to say about them.
  "${JFAST}" check --multitenant-ready > ready.log 2>&1
  grep -q "0 to fix" ready.log || { cat ready.log; fail "saas: --multitenant-ready found something"; }
else
  cat start.log
  fail "jfast start --multitenant"
fi

echo "### everything (jfast new service --with ...)"
mkdir -p "${WORK}/everything" && cd "${WORK}/everything"
if "${JFAST}" new service todo --with "${EVERYTHING}" > new.log 2>&1; then
  cd todo
  modules everything
  gates everything
else
  cat new.log
  fail "jfast new service --with ${EVERYTHING}"
fi

if [ "${rc}" -eq 0 ]; then
  echo "OK: every generated project passes ruff, ruff format, mypy --strict and pytest"
fi
exit "${rc}"
