#!/usr/bin/env bash
# Build the image the generator writes, and run it against a real PostgreSQL.
#
#     bash scripts/smoke_docker.sh
#
# This is the check that did not exist, and four defects lived in the gap: a
# requirements pin pip could not satisfy, a COPY of a file the generator never
# writes, a container that served 500s against an empty schema, and a database
# dependency that turned every route into a 422. Every other smoke script
# installs the framework from the checkout with all extras present and never
# builds an image, so none of them could see any of it.
#
# The framework is installed from PyPI, which is what a user receives. The
# Dockerfile installs dependencies before copying the source -- deliberately,
# for layer caching -- so a wheel built here would not exist yet at that step.
#
# At release time the version a generated service pins is not published yet.
# Rather than fail on exactly the commit that is correct, the pin is then
# pointed at this checkout's wheel -- see scripts/lib/checkout_wheel.sh for why
# not the newest published release -- and the substitution is announced.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
  PYTHON="${ROOT}/.venv/bin/python"
else
  JFAST="$(command -v jfast)"
  PYTHON="$(command -v python3)"
fi

NET="jfast-smoke-net-$$"
DB="jfast-smoke-db-$$"
API="jfast-smoke-api-$$"
IMAGE="jfast-smoke-image-$$"
WORK="$(mktemp -d)"
# shellcheck source=lib/checkout_wheel.sh
source "${ROOT}/scripts/lib/checkout_wheel.sh"

cleanup() {
  code=$?
  stop_checkout_wheel
  docker rm -f "${API}" "${DB}" > /dev/null 2>&1 || true
  docker network rm "${NET}" > /dev/null 2>&1 || true
  docker rmi -f "${IMAGE}" > /dev/null 2>&1 || true
  rm -rf "${WORK}"
  exit ${code}
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "generate a service and its Dockerfile"
cd "${WORK}"
"${JFAST}" workspace init smoke > /dev/null
"${JFAST}" new service billing --with database > /dev/null
cd billing
"${JFAST}" deploy dockerfile > /dev/null

grep -q 'COPY pyproject.toml\* ' Dockerfile \
  || fail "COPY pyproject.toml must be optional; the generator writes no pyproject"
grep -q 'alembic upgrade head' Dockerfile || fail "the image must migrate before serving"
grep -q 'exec uvicorn' Dockerfile || fail "uvicorn must be PID 1"
[ -f .dockerignore ] || fail "no .dockerignore: COPY . . would take .env into a layer"

# The step that makes the leak observable. `cp .env.example .env` is the third
# line of the panel `jfast new service` prints, and the Dockerfile ends in
# `COPY . .` -- so a filled-in .env reaches an image layer unless something
# excludes it, and deleting the file afterwards does not remove the layer.
step "a filled-in .env must not reach the image"
cp .env.example .env
echo "SMOKE_CANARY=this-must-not-ship" >> .env

step "make sure the pin resolves"
cat requirements.txt
pin_to_checkout_wheel

step "docker build"
docker build -q --add-host "${WHEEL_HOST}:host-gateway" -t "${IMAGE}" . > /dev/null \
  || fail "the generated Dockerfile does not build"

step "what the image did and did not pick up"
docker run --rm --entrypoint sh "${IMAGE}" -c 'ls -a /app' > "${WORK}/listing.txt"
grep -qx '.env' "${WORK}/listing.txt" \
  && fail "the .env is in the image; .dockerignore is not doing its job"
# Real directories only. Searching `/` matches the grep's own command line
# under /proc and reports every image as leaking.
if docker run --rm --entrypoint sh "${IMAGE}" \
     -c 'grep -rl SMOKE_CANARY /app /opt /home /root /etc /tmp 2>/dev/null | head -1' \
   | grep -q .; then
  fail "the secret from .env is somewhere in the image"
fi
docker run --rm --entrypoint sh "${IMAGE}" -c 'command -v gcc || command -v cc' > /dev/null 2>&1 \
  && fail "the compiler reached the final image; the build stage is not separate"
docker run --rm --entrypoint python "${IMAGE}" -c \
  'from zoneinfo import ZoneInfo; ZoneInfo("America/Mexico_City")' > /dev/null 2>&1 \
  || fail "no zone database in the image; a non-UTC timezone would stop the boot"
docker run --rm --entrypoint id "${IMAGE}" | grep -q 'uid=10001' \
  || fail "the image runs as root"
echo "  no .env, no secret, no compiler, zones resolve, non-root"

step "a real PostgreSQL"
docker network create "${NET}" > /dev/null
docker run -d --name "${DB}" --network "${NET}" \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=app \
  pgvector/pgvector:pg16 > /dev/null
for _ in $(seq 1 40); do
  docker exec "${DB}" pg_isready -U app > /dev/null 2>&1 && break
  sleep 1
done
docker exec "${DB}" pg_isready -U app > /dev/null 2>&1 || fail "postgres never became ready"

step "a failed migration stops the container instead of serving"
if docker run --name "${API}" --network "${NET}" \
     -e JFAST_DB_DSN="postgresql+asyncpg://app:wrong@${DB}:5432/app" \
     "${IMAGE}" > "${WORK}/fail.log" 2>&1; then
  fail "the container served despite a failed migration"
fi
grep -qi "password authentication failed" "${WORK}/fail.log" \
  || fail "expected the real database error: $(tail -3 "${WORK}/fail.log")"
docker rm -f "${API}" > /dev/null
echo "  stopped, with the database error surfaced"

step "against a reachable database it migrates, then serves"
docker run -d --name "${API}" --network "${NET}" -p 9457:8000 \
  -e JFAST_DB_DSN="postgresql+asyncpg://app:smoke@${DB}:5432/app" \
  "${IMAGE}" > /dev/null

ready=""
for _ in $(seq 1 40); do
  if curl -fsS http://localhost:9457/health > /dev/null 2>&1; then ready="yes"; break; fi
  sleep 1
done
[ -n "${ready}" ] || { docker logs "${API}" 2>&1 | tail -20; fail "the service never answered"; }

curl -fsS http://localhost:9457/health | grep -q '"status":"ok"' || fail "/health is not ok"
curl -fsS http://localhost:9457/ready > "${WORK}/ready.json" 2>&1 \
  || { cat "${WORK}/ready.json"; fail "/ready did not answer 200"; }
grep -q '"database":{"healthy":true' "${WORK}/ready.json" \
  || fail "the database check is not healthy: $(cat "${WORK}/ready.json")"
echo "  /health and /ready both ok, database reachable"

step "alembic really ran"
docker exec "${DB}" psql -U app -d app -tAc \
  "select count(*) from information_schema.tables where table_name='alembic_version';" \
  | grep -q '^1$' || fail "no alembic_version table: the migration did not run"
echo "  alembic_version exists"

printf '\nDOCKER SMOKE OK\n'
