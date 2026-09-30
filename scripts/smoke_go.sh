#!/usr/bin/env bash
# Generate a Go service and actually build, test and run it.
#
#     bash scripts/smoke_go.sh
#
# The point of generating a Go service is that it compiles. A template that
# has never been through `go build` is a liability that looks like a feature,
# so this runs the whole path: vet, test, build, start the binary, curl it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

# `go` when it is installed (CI's setup-go). Locally without it, the golang
# image if Docker already has it -- never pulled from here: vet and test run
# in the container, and the binary is cross-compiled for this machine so the
# rest of the script is identical.
GO_IMAGE="${JFAST_SMOKE_GO_IMAGE:-golang:1.23}"
if command -v go > /dev/null; then
  gocmd() { go "$@"; }
  gofmtcmd() { gofmt "$@"; }
elif command -v docker > /dev/null && docker image inspect "${GO_IMAGE}" > /dev/null 2>&1; then
  echo "go is not installed; using ${GO_IMAGE} through Docker"
  HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  case "$(uname -m)" in
    arm64 | aarch64) HOST_ARCH=arm64 ;;
    *) HOST_ARCH=amd64 ;;
  esac
  gocmd() {
    docker run --rm -v "${PWD}":/src -w /src -e GOOS -e GOARCH "${GO_IMAGE}" go "$@"
  }
  gofmtcmd() { docker run --rm -v "${PWD}":/src -w /src "${GO_IMAGE}" gofmt "$@"; }
else
  echo "go is not installed; skipping (CI's setup-go provides it)"
  exit 0
fi

WORK="$(mktemp -d)"
PID=""
# Preserve the failing status; see the note in scripts/smoke.sh.
trap 'code=$?; rm -rf "${WORK}"; [ -n "${PID}" ] && kill "${PID}" 2>/dev/null; exit ${code}' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "generate"
"${JFAST}" workspace init poly > /dev/null
"${JFAST}" new service edge --language go --with cache --grpc
cd edge
find . -type f -not -name '.jfast-template' | sort

step "gofmt"
# Generated code that gofmt would rewrite is the first diff a Go reviewer sees.
unformatted="$(gofmtcmd -l .)"
[[ -z "${unformatted}" ]] || fail "gofmt would rewrite: ${unformatted}"

step "go vet"
gocmd vet ./...

step "go test"
gocmd test ./...

step "go build"
if declare -p HOST_OS > /dev/null 2>&1; then
  GOOS="${HOST_OS}" GOARCH="${HOST_ARCH}" gocmd build -o edge-bin .
  mv edge-bin "${WORK}/edge-bin"
else
  go build -o "${WORK}/edge-bin" .
fi
ls -la "${WORK}/edge-bin"

step "run it"
JFAST_PORT=9123 JFAST_LOG_JSON_LOGS=true "${WORK}/edge-bin" > "${WORK}/log.txt" 2>&1 &
PID=$!
for _ in $(seq 1 40); do
  curl -fsS localhost:9123/health > /dev/null 2>&1 && break
  sleep 0.25
done

curl -fsS localhost:9123/health | grep -q '"status":"ok"' || fail "/health"
curl -fsS localhost:9123/ready | grep -q '"status":"ok"' || fail "/ready"

curl -fsS -X POST localhost:9123/items -H 'content-type: application/json' \
  -d '{"id":0,"name":"first","is_active":true}' | grep -q '"id":1' || fail "POST /items"

# Errors must be problem+json -- the same shape the Python services emit.
curl -sS -i localhost:9123/items/999 | grep -qi 'application/problem+json' \
  || fail "404 is not problem+json"

# A trace that restarts at every hop is a trace nobody can follow.
curl -sS -i -H 'X-Request-ID: trace-me' localhost:9123/health \
  | grep -qi 'x-request-id: trace-me' || fail "request id was not propagated"

# Structured logs on stdout, carrying the correlation id.
grep -q '"request_id":"trace-me"' "${WORK}/log.txt" || fail "request id missing from logs"

# W3C trace context: a valid traceparent is logged as trace_id, an invalid
# one is dropped rather than logged.
TRACEPARENT="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
curl -fsS -H "traceparent: ${TRACEPARENT}" localhost:9123/health > /dev/null
curl -fsS -H "traceparent: 00-00000000000000000000000000000000-00f067aa0ba902b7-01" \
  localhost:9123/ready > /dev/null
grep -q '"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"' "${WORK}/log.txt" \
  || fail "trace id missing from logs"
grep '"http_path":"/ready"' "${WORK}/log.txt" | grep -q trace_id \
  && fail "an invalid traceparent was logged"

step "the gRPC contract was generated"
test -f proto/edge.proto || fail "no proto"
grep -q 'service Health' proto/edge.proto || fail "proto missing the health contract"

kill "${PID}"; PID=""

step "auth and tenancy, with tokens minted by the Python auth plugin"
SECRET="smoke-secret-that-is-at-least-thirty-two-bytes"
mint() {
  "${PY}" - "$1" <<PYEOF
import sys
from datetime import timedelta
from jfastframework.auth import issue
token, _, _ = issue("user-" + sys.argv[1], key="${SECRET}", algorithm="HS256",
                    lifetime=timedelta(minutes=5), audience="edge", tenant_id=sys.argv[1])
print(token)
PYEOF
}
ACME="$(mint acme)"
GLOBEX="$(mint globex)"

JFAST_PORT=9124 JFAST_AUTH_SECRET="${SECRET}" JFAST_AUTH_AUDIENCE=edge \
  JFAST_TENANCY_SOURCES='["token"]' "${WORK}/edge-bin" > "${WORK}/auth-log.txt" 2>&1 &
PID=$!
for _ in $(seq 1 40); do
  curl -fsS localhost:9124/health > /dev/null 2>&1 && break
  sleep 0.25
done
curl -fsS localhost:9124/health > /dev/null || fail "the service did not start with auth on"

curl -fsS -X POST localhost:9124/items -H 'content-type: application/json' \
  -H "Authorization: Bearer ${ACME}" -d '{"name":"acme-only","is_active":true}' \
  | grep -q '"acme-only"' || fail "POST /items as acme"
curl -fsS localhost:9124/items -H "Authorization: Bearer ${ACME}" \
  | grep -q '"acme-only"' || fail "acme does not see its own item"
curl -fsS localhost:9124/items -H "Authorization: Bearer ${GLOBEX}" \
  | grep -q '"acme-only"' && fail "globex sees acme's item"
curl -fsS localhost:9124/items | grep -q '"acme-only"' && fail "an anonymous caller sees acme's item"
# A forged header is not a tenant unless "header" is a configured source.
curl -fsS localhost:9124/items -H 'X-Tenant-ID: acme' \
  | grep -q '"acme-only"' && fail "X-Tenant-ID chose a tenant"

grep -q '"tenant_id":"acme"' "${WORK}/auth-log.txt" || fail "tenant missing from the access log"
grep -q 'revoked tokens are not checked' "${WORK}/auth-log.txt" \
  || fail "no startup line saying revocation is not checked"
kill "${PID}"; PID=""

step "with auth on, a subdomain names a tenant but never grants one"
# 0.1.0a12's F1: `curl -H "Host: acme.localhost"` needs no DNS. The routes are
# open, so a refused tenant shows up as an empty tenant, never as acme's rows.
JFAST_PORT=9126 JFAST_AUTH_SECRET="${SECRET}" JFAST_AUTH_AUDIENCE=edge \
  JFAST_TENANCY_SOURCES='["token","subdomain"]' JFAST_TENANCY_BASE_DOMAIN=localhost \
  "${WORK}/edge-bin" > "${WORK}/subdomain-log.txt" 2>&1 &
PID=$!
for _ in $(seq 1 40); do
  curl -fsS localhost:9126/health > /dev/null 2>&1 && break
  sleep 0.25
done
curl -fsS localhost:9126/health > /dev/null || fail "the service did not start with a subdomain source"
curl -fsS -X POST localhost:9126/items -H 'Host: acme.localhost:9126' -H 'content-type: application/json' \
  -H "Authorization: Bearer ${ACME}" -d '{"name":"acme-secret","is_active":true}' \
  | grep -q '"acme-secret"' || fail "acme cannot write on its own subdomain"
curl -fsS localhost:9126/items -H 'Host: acme.localhost:9126' -H "Authorization: Bearer ${ACME}" \
  | grep -q '"acme-secret"' || fail "acme does not see its item on its subdomain"
curl -fsS localhost:9126/items -H 'Host: acme.localhost:9126' \
  | grep -q '"acme-secret"' && fail "an anonymous request on acme.localhost read acme's items"
curl -fsS -X POST localhost:9126/items -H 'Host: acme.localhost:9126' -H 'content-type: application/json' \
  -d '{"name":"planted","is_active":true}' > /dev/null || true
curl -fsS localhost:9126/items -H 'Host: acme.localhost:9126' -H "Authorization: Bearer ${ACME}" \
  | grep -q '"planted"' && fail "an anonymous POST on acme.localhost wrote into acme"
curl -fsS localhost:9126/items -H 'Host: acme.localhost:9126' -H "Authorization: Bearer ${GLOBEX}" \
  | grep -q '"acme-secret"' && fail "globex's token on acme.localhost read acme's items"
kill "${PID}"; PID=""

step "a configuration the service cannot enforce stops it"
if JFAST_PORT=9125 JFAST_AUTH_MODE=jwks JFAST_AUTH_JWKS_URL=https://id.example.test/jwks.json \
  "${WORK}/edge-bin" > "${WORK}/jwks-log.txt" 2>&1; then
  fail "jwks mode started"
fi
grep -q 'keyfunc' "${WORK}/jwks-log.txt" || fail "the jwks refusal does not name a library"

printf '\nGO SMOKE OK\n'
