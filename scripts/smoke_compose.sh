#!/usr/bin/env bash
# Run the compose file the generators write, the way the panel says to run it.
#
#     bash scripts/smoke_compose.sh
#
# `smoke_docker.sh` proves the image is correct, but it builds that image with
# `docker build` and wires the network, the database and the environment by
# hand -- which is precisely the work the generated compose file exists to do.
# So every defect that lived in the compose file survived a green smoke run:
#
#   * no generator wrote a Dockerfile, and both compose files say `build:`, so
#     `docker compose up --build` stopped at "failed to read dockerfile";
#   * the api service loaded a .env whose DSN said `localhost`, which inside a
#     container is that container, so it crash-looped against its own port;
#   * `jfast start` rendered a module and never mounted it, so the endpoints
#     the generated tests covered did not exist at runtime.
#
# Nothing here inspects a file. It runs the two commands a new user runs and
# asks the running containers what they are serving. Both generators are
# covered because they fail differently: `jfast start` writes a workspace, and
# `jfast new service` plus `jfast deploy compose` writes a single service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
  PYTHON="${ROOT}/.venv/bin/python"
else
  JFAST="$(command -v jfast)"
  PYTHON="$(command -v python3)"
fi

WORK="$(mktemp -d)"
# The compose project name, so teardown reaches every container even when the
# run dies between `up` and the first assertion.
WORKSPACE_PROJECT="jfastworkspace$$"
SERVICE_PROJECT="jfastservice$$"
# Above the default block and above anything smoke_docker.sh publishes, so two
# jobs on one runner do not collide on a host port.
BASE_PORT=9600

cleanup() {
  code=$?
  stop_checkout_wheel
  for project in "${WORKSPACE_PROJECT}" "${SERVICE_PROJECT}"; do
    docker compose -p "${project}" down -v --remove-orphans > /dev/null 2>&1 || true
  done
  docker rmi -f "${WORKSPACE_PROJECT}-demo" "${SERVICE_PROJECT}-api" > /dev/null 2>&1 || true
  rm -rf "${WORK}"
  exit ${code}
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

# At release time the version a generated service pins is not published yet, and
# the image installs from PyPI. Rather than fail on exactly the commit that is
# correct, the pin is pointed at this checkout's wheel -- the same trade
# smoke_docker.sh makes; scripts/lib/checkout_wheel.sh has why. Announced,
# never silent.
# shellcheck source=lib/checkout_wheel.sh
source "${ROOT}/scripts/lib/checkout_wheel.sh"

# `docker compose up --build` for the generated file, plus the override that
# lets the build reach the wheel server. Everything else about the command is
# what the panel prints.
up_built() {
  local project="$1" service="$2" base
  shift 2
  base="$(default_compose_file)" || fail "no compose file in $(pwd)"
  docker compose -p "${project}" -f "${base}" -f "$(wheel_hosts_override "${service}")" \
    up -d --build "$@"
}

# Ask the running container rather than the host: the host port is a mapping
# that may be absent, and what is being tested is the network the compose file
# built. `python` is in the image because the image is a Python service.
inside() {
  local project="$1" service="$2" port="$3" path="$4"
  docker compose -p "${project}" exec -T "${service}" python - "${port}" "${path}" <<'PY'
import sys, urllib.error, urllib.request
port, path = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        sys.stdout.write(response.read().decode())
except urllib.error.HTTPError as error:
    # The body says which check failed; a bare "HTTP Error 503" says nothing.
    sys.stdout.write(error.read().decode())
    sys.exit(1)
PY
}

wait_for_ready() {
  local project="$1" service="$2" port="$3"
  for _ in $(seq 1 60); do
    if inside "${project}" "${service}" "${port}" /ready > /dev/null 2>&1; then
      return 0
    fi
    if [ -z "$(docker compose -p "${project}" ps -q "${service}")" ]; then
      break
    fi
    sleep 2
  done
  docker compose -p "${project}" logs --tail 40 "${service}" || true
  fail "${service} never became ready"
}


# -- the flagship command, end to end -----------------------------------

step "jfast start, then the first line of its own panel"
cd "${WORK}"
mkdir workspace && cd workspace
"${JFAST}" start demo --port "${BASE_PORT}" > /dev/null

[ -f demo/Dockerfile ] || fail "no demo/Dockerfile: \`build:\` in the compose file has nothing to read"

# The first thing most services add is an upload. `jfast add storage` edits
# requirements.txt, which is all the production image installs: in 0.1.0a11
# the storage extra lacked python-multipart, so dev worked and this image
# failed to import main.py at its first UploadFile route.
(cd demo && "${JFAST}" add storage --no-install > /dev/null) \
  || fail "jfast add storage failed on a project nobody had touched"
cat >> demo/main.py <<'PY'


# smoke_compose: one real upload through the production image.
from fastapi import UploadFile  # noqa: E402


@app.post("/_smoke/upload")
async def _smoke_upload(file: UploadFile) -> dict[str, int]:
    return {"bytes": len(await file.read())}
PY

# Not in a subshell: the wheel server it may start has to be stopped by cleanup.
cd demo && pin_to_checkout_wheel && cd ..

up_built "${WORKSPACE_PROJECT}" demo demo \
  || fail "docker compose up --build failed on a project nobody had touched"
wait_for_ready "${WORKSPACE_PROJECT}" demo "${BASE_PORT}"

step "the datastores are reachable from inside the network"
READY="$(inside "${WORKSPACE_PROJECT}" demo "${BASE_PORT}" /ready || true)"
echo "${READY}" | grep -q '"status":"ok"' || {
  echo "${READY}"
  docker compose -p "${WORKSPACE_PROJECT}" logs --tail 60 demo || true
  fail "/ready is not ok: the api container cannot reach a datastore it depends on"
}

step "the module jfast start generated is actually served"
# The failure this catches is silent: the module exists, its tests pass, and
# the routes are absent because nothing mounted the router.
inside "${WORKSPACE_PROJECT}" demo "${BASE_PORT}" /openapi.json | grep -q '"/items"' \
  || fail "/items is not in the schema: the generated module was never mounted"

step "a file uploaded to the production image arrives as a file"
docker compose -p "${WORKSPACE_PROJECT}" exec -T demo python - "${BASE_PORT}" <<'PY' \
  || fail "the upload did not arrive: the image lacks what an UploadFile route needs"
import json, sys, urllib.request
boundary = "jfastsmoke"
body = (
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.pdf\"\r\n"
    "Content-Type: application/pdf\r\n\r\n%PDF-1.4 smoke\r\n"
    f"--{boundary}--\r\n"
).encode()
request = urllib.request.Request(
    f"http://127.0.0.1:{sys.argv[1]}/_smoke/upload", data=body, method="POST",
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
with urllib.request.urlopen(request, timeout=10) as response:
    answer = json.load(response)
assert answer == {"bytes": 14}, answer
PY

docker compose -p "${WORKSPACE_PROJECT}" down -v > /dev/null 2>&1 || true


# -- the single-service path, run exactly as the panel prints it --------

step "jfast new service, then deploy compose, then up"
cd "${WORK}"
mkdir service && cd service
"${JFAST}" new service billing --port $((BASE_PORT + 20)) > /dev/null
cd billing
# Copied rather than edited: the .env is what a user has at this point, and the
# addresses in it are the host's. The compose file has to override them.
cp .env.example .env
"${JFAST}" deploy compose -o docker-compose.yml > /dev/null
pin_to_checkout_wheel

up_built "${SERVICE_PROJECT}" api \
  || fail "docker compose up failed on the file jfast deploy compose just wrote"
wait_for_ready "${SERVICE_PROJECT}" api $((BASE_PORT + 20))

inside "${SERVICE_PROJECT}" api $((BASE_PORT + 20)) /ready | grep -q '"status":"ok"' \
  || fail "/ready is not ok: the api container is reading the host's DSN"

step "every check passed"
