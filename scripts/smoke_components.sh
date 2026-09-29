#!/usr/bin/env bash
# The generated frontends must build.
#
# Rendering a template proves the braces were right. It does not prove the
# component imports something that exists, that the store it reaches for is the
# one the host renders, or that JSX with a typo in it compiles. Only a real
# `npm run build` does, and every one of those three was wrong at some point in
# the base components.
#
# Also asserts that ToastHost is actually mounted: a toast system nothing
# renders fails silently, which is the worst way for it to fail.
#
# The three checks after the build are all of the same kind -- a feature that
# exists in the templates but that nothing in the shipped bundle can reach.
# Each of them was true at some point: a theme with three states and a UI that
# could only ever pin two of them, and a 401 that no interceptor answered.
set -uo pipefail

JFAST=${JFAST:-jfast}
WORK=$(mktemp -d)
rc=0

cleanup() {
  saved=$?
  rm -rf "${WORK}"
  exit "${saved}"
}
trap cleanup EXIT

if ! command -v npm > /dev/null 2>&1; then
  echo "SKIP: npm is not installed"
  exit 0
fi

# Every property below belongs to the shared tree, so both looks must keep it.
for template in nexora classic; do
for framework in vue react; do
  echo "### ${framework} / ${template}"
  dir="${WORK}/${framework}-${template}"
  mkdir -p "${dir}"
  cd "${dir}" || exit 1

  if ! "${JFAST}" new service web --kind spa --frontend "${framework}" --template "${template}" \
       --agent-docs > "${dir}/gen.log" 2>&1; then
    echo "  FAIL: generate"; tail -10 "${dir}/gen.log"; rc=1; continue
  fi
  cd web || exit 1

  # The host has to be mounted somewhere, or every toast is invisible.
  if ! grep -rq "ToastHost" src/main.js src/main.jsx src/App.vue 2>/dev/null; then
    echo "  FAIL: ToastHost is never mounted"
    rc=1
    continue
  fi

  # One toast store, not two. Two means a toast raised from a service lands in
  # a list the host does not render, and nothing appears with no error.
  stores=$(grep -rlE "create\(|defineStore\(" src/stores src/hooks src/composables 2>/dev/null \
    | xargs -r grep -lE "toasts" 2>/dev/null | wc -l)
  if [ "${stores}" -gt 1 ]; then
    echo "  FAIL: ${stores} competing toast stores"
    rc=1
    continue
  fi

  echo "  installing"
  if ! npm install --silent --no-audit --no-fund > "${dir}/install.log" 2>&1; then
    echo "  FAIL: npm install"; tail -15 "${dir}/install.log"; rc=1; continue
  fi

  echo "  building"
  if ! npm run build > "${dir}/build.log" 2>&1; then
    echo "  FAIL: npm run build"
    tail -25 "${dir}/build.log"
    rc=1
    continue
  fi

  css=$(find dist/assets -name '*.css' | head -1)
  if [ -z "${css}" ]; then
    echo "  FAIL: no stylesheet emitted"; rc=1; continue
  fi

  # Both halves of the dark variant. The second is what makes an explicit light
  # choice beat a system set to dark; without it the toggle works one way only.
  for selector in '[data-theme=dark]' ':not([data-theme=light])' 'color-scheme'; do
    if ! grep -qF -- "${selector}" "${css}"; then
      echo "  FAIL: ${selector} missing from the stylesheet"; rc=1
    fi
  done

  # Surfaces have to stay indirect. If these resolved at build time the toggle
  # would change the attribute and nothing else.
  if ! grep -qF -- 'var(--ui-panel)' "${css}"; then
    echo "  FAIL: surface tokens resolved at build time, not through a variable"; rc=1
  fi

  # Set before the first paint, or every reload flashes the other theme.
  if ! grep -q 'data-theme' dist/index.html; then
    echo "  FAIL: no theme boot script in index.html"; rc=1
  fi

  # One neutral ramp, and it is the tokens. Two ramps is why a UI looks subtly
  # wrong with nothing identifiably broken in it.
  if grep -rqE --include='*.vue' --include='*.jsx' '(slate|zinc)-[0-9]' src/; then
    echo "  FAIL: a component still hardcodes a neutral ramp"
    grep -rlE --include='*.vue' --include='*.jsx' '(slate|zinc)-[0-9]' src/
    rc=1
  fi

  # Every chunk, not the first one found: a nexora build has two, and the
  # three.js one says nothing about themes or sessions.
  js=$(find dist/assets -name '*.js')
  if [ -z "${js}" ]; then
    echo "  FAIL: no script emitted"; rc=1; continue
  fi

  # The theme has three states and the toggle can only ever pin two of them, so
  # something in the bundle has to offer the third. Asserted through the label
  # because that is the part a user can find: a `setTheme` export no component
  # calls leaves "system" unreachable the moment anyone touches the switch.
  if ! grep -qF -- 'Follow the system theme' ${js}; then
    echo "  FAIL: nothing shipped can put the theme back on 'system'"; rc=1
  fi

  # A 401 that no interceptor answers is an app that renders as logged in with
  # every panel empty. `/auth/refresh` alone does not prove that: the Vue auth
  # store carried a `refresh()` nothing called for a while, and the string was
  # in the bundle the whole time. `?next=` is only built by the two places that
  # send someone to sign in, so the pair is what says the path is wired.
  for needle in '/auth/refresh' '?next='; do
    if ! grep -qF -- "${needle}" ${js}; then
      echo "  FAIL: no refresh-on-401 in the bundle (${needle} missing)"; rc=1
    fi
  done

  # And the route those redirects aim at has to be registered. A redirect to a
  # path with no route is a blank page.
  if ! grep -qF -- '/login' ${js}; then
    echo "  FAIL: the sign-in route the 401 path redirects to is not in the bundle"; rc=1
  fi

  # A grep, not a build check: the build above already proved the layout
  # compiles. What can still go missing is the lock itself, and then the page
  # behind the open drawer scrolls under the finger.
  if ! grep -rq 'body.style.overflow' src/layouts/; then
    echo "  FAIL: the mobile drawer does not lock body scroll"; rc=1
  fi

  echo "  ok: ${framework} / ${template} builds, and the theme survives the build"
done
done

echo
if [ "${rc}" -eq 0 ]; then
  echo "PASS: both frontends build, in both looks"
else
  echo "FAIL"
fi
exit "${rc}"
