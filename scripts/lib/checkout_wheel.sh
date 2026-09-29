# Sourced by the smoke scripts that build an image from a generated Dockerfile.
#
# The image installs jfastframework from requirements.txt before the source is
# copied in -- deliberately, for layer caching. At release time the version a
# generated service pins is not on PyPI yet. These scripts used to fall back to
# the newest published release, which only works while the templates need
# nothing that release lacks; the first template to import a new module
# (0.1.0a9's env.py importing `jfastframework.db.framework`) turned that into
# an image that could not migrate.
#
# So the fallback is this checkout: its wheel is built and served over HTTP
# from the host, and the requirement becomes a direct reference to it. The
# build reaches the host as `jfastwheels`, mapped to `host-gateway` -- the
# host's address on Linux and on Docker Desktop alike.
#
# Needs ROOT, PYTHON and WORK set by the caller.

WHEEL_HOST="jfastwheels"
WHEEL_URL=""
WHEEL_SERVER_PID=""

serve_checkout_wheel() {
  [ -n "${WHEEL_URL}" ] && return 0
  mkdir -p "${WORK}/wheels"
  "${PYTHON}" -m pip wheel --quiet --no-deps -w "${WORK}/wheels" "${ROOT}" > /dev/null
  local wheel port
  wheel="$(cd "${WORK}/wheels" && ls jfastframework-*.whl | head -1)"
  port="$("${PYTHON}" -c 'import socket; s = socket.socket(); s.bind(("", 0)); print(s.getsockname()[1])')"
  "${PYTHON}" -m http.server "${port}" --bind 0.0.0.0 --directory "${WORK}/wheels" \
    > /dev/null 2>&1 &
  WHEEL_SERVER_PID=$!
  WHEEL_URL="http://${WHEEL_HOST}:${port}/${wheel}"
}

# Rewrite ./requirements.txt to this checkout when its pin does not resolve.
# Announced, never silent.
pin_to_checkout_wheel() {
  if "${PYTHON}" -m pip install --dry-run --quiet --pre \
       --target "${WORK}/resolve" -r requirements.txt > /dev/null 2>&1; then
    return 0
  fi
  serve_checkout_wheel
  echo "  the pinned version is not on PyPI yet; building against this checkout (${WHEEL_URL##*/})"
  "${PYTHON}" - "${WHEEL_URL}" <<'PY'
import re, sys
body = open("requirements.txt", encoding="utf-8").read()
body = re.sub(r"(jfastframework\[[^\]]*\])[^\s]*", r"\1 @ " + sys.argv[1], body)
open("requirements.txt", "w", encoding="utf-8").write(body)
print("  " + next(line for line in body.splitlines() if line.startswith("jfastframework")))
PY
}

# A compose override that lets `service`'s build reach the wheel server, for
# `docker compose -f <generated> -f <this>`. The generated file is not edited.
wheel_hosts_override() {
  local service="$1" file="${WORK}/wheel-hosts-$1.yml"
  cat > "${file}" <<YAML
services:
  ${service}:
    build:
      extra_hosts:
        - "${WHEEL_HOST}:host-gateway"
YAML
  echo "${file}"
}

# The compose file docker compose would pick in the current directory.
default_compose_file() {
  local name
  for name in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
    if [ -f "${name}" ]; then
      echo "${name}"
      return 0
    fi
  done
  return 1
}

stop_checkout_wheel() {
  if [ -n "${WHEEL_SERVER_PID}" ]; then
    kill "${WHEEL_SERVER_PID}" 2> /dev/null || true
  fi
}
