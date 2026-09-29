#!/usr/bin/env bash
# Generate the Vue and React frontends, in both looks, and actually install and
# build them.
#
#     bash scripts/smoke_frontend.sh
#     JFAST=/path/to/jfast bash scripts/smoke_frontend.sh   # a specific install
#
# This job exists because of a real bug it caught: the generator's marker
# comment `/*nuevaRuta*/` sat inside a `/* ... */` block comment, whose inner
# `*/` closed the comment early and left the router file syntactically
# invalid. Every grep-based check passed -- the marker *was* there. Only
# `vite build` said otherwise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${JFAST:-}" ]]; then
  :
elif [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

if ! command -v npm > /dev/null; then
  echo "npm is not installed; skipping (CI's setup-node provides it)"
  exit 0
fi

WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

"${JFAST}" workspace init fronts > /dev/null
"${JFAST}" new service billing --with database > /dev/null

for TEMPLATE in nexora classic; do
for FW in vue react; do
  step "${FW} / ${TEMPLATE}"
  APP="app_${FW}_${TEMPLATE}"
  if [[ "${TEMPLATE}" == nexora ]]; then
    # No --template: nexora is the default, and this is what proves it.
    "${JFAST}" new service "${APP}" --kind spa --frontend "${FW}" > /dev/null
  else
    "${JFAST}" new service "${APP}" --kind spa --frontend "${FW}" --template "${TEMPLATE}" > /dev/null
  fi
  cd "${WORK}/${APP}"
  "${JFAST}" new view Facturas > /dev/null

  # The look is recorded, and the generated page is drawn in it: a nexora
  # project whose new pages come out classic is the bug this whole option
  # exists to prevent, and it builds without complaint.
  grep -q "\"frontend_template\": \"${TEMPLATE}\"" .jfast-template \
    || fail "${FW}/${TEMPLATE}: the look is not recorded in .jfast-template"
  PAGE="$(ls src/ModuloFacturas/Pages/FacturasView.*)"
  if [[ "${TEMPLATE}" == nexora ]]; then
    grep -q 'erp-table' "${PAGE}" || fail "${FW}/nexora: jfast new view wrote a classic page"
    grep -q '"three"' package.json || fail "${FW}/nexora: three is not a dependency"
  else
    grep -q 'bg-brand-600' "${PAGE}" || fail "${FW}/classic: jfast new view wrote a nexora page"
    test ! -e src/nexora || fail "${FW}/classic: nexora files leaked into a classic project"
    if grep -q '"three"' package.json; then fail "${FW}/classic: three is a dependency"; fi
  fi

  echo "--- npm install ---"
  npm install --no-audit --no-fund 2>&1 | tail -2

  echo "--- npm run build ---"
  npm run build 2>&1 | tail -6

  test -f dist/index.html || fail "${FW}/${TEMPLATE}: no dist/index.html"

  # The production build must use the relative /api base, not the dev port.
  # Behind Caddy the API lives at /api whether or not a gateway exists, so one
  # build works in every environment — and a baked-in http://localhost:8010
  # would be a white screen in production.
  grep -q '"/api"' dist/assets/*.js || fail "${FW}: production build is not using /api"
  # Match a port, not bare "localhost": Vue's own bundle contains
  # `window.location.href || "http://localhost"` as a fallback, which is not
  # our configuration. A dev *port* is.
  if grep -qE 'localhost:[0-9]{4}' dist/assets/*.js; then
    fail "${FW}: a development URL was baked into the production build"
  fi

  # The generated module must be in the bundle: a route that silently fails to
  # register still builds, and only shows up as a blank page.
  grep -q 'Facturas' dist/assets/*.js || fail "${FW}: the generated view is not in the bundle"

  if [[ "${TEMPLATE}" == nexora ]]; then
    # three.js is lazy: out of the entry chunk, into one of its own. Imported
    # statically by mistake, every visitor downloads it before first paint
    # and the build still passes.
    ENTRY="dist$(grep -o '/assets/index-[^"]*\.js' dist/index.html)"
    if grep -q 'WebGLRenderer' "${ENTRY}"; then fail "${FW}: three.js is in the entry chunk"; fi
    grep -lq 'WebGLRenderer' dist/assets/*.js || fail "${FW}: the ribbon chunk is missing"

    # A project's default accent is a build-time value. A colour that is in
    # none of the presets, so finding it proves it came from the variable.
    VITE_ACCENT='#12AB34' npm run build > /dev/null 2>&1 || fail "${FW}: build with VITE_ACCENT failed"
    grep -qi '12AB34' dist/assets/*.js || fail "${FW}: VITE_ACCENT did not reach the bundle"
  fi

  cd "${WORK}"
done
done

printf '\nFRONTEND SMOKE OK\n'
