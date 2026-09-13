#!/usr/bin/env bash
# The easter eggs are a separate distribution so that `pip install
# jfastframework` never carries them. Both wheels are built from the checkout
# and installed into clean environments, the way a user gets them -- a test
# importing from `src/` would pass whether or not the framework's wheel
# shipped them too.
#
#     bash scripts/smoke_eastereggs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi
WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

EGGS=(shrek mcqueen pene vagina)

step "build both wheels"
"${PY}" -m pip wheel --quiet --no-deps --wheel-dir "${WORK}/dist" "${ROOT}"
"${PY}" -m pip wheel --quiet --no-deps --wheel-dir "${WORK}/dist" "${ROOT}/eastereggs"
FRAMEWORK_WHEEL="$(ls "${WORK}"/dist/jfastframework-[0-9]*.whl)"
EGGS_WHEEL="$(ls "${WORK}"/dist/jfastframework_eastereggs-*.whl)"

top_level() {
  "${PY}" - "$1" <<'PY'
import sys
import zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
print("\n".join(sorted({name.split("/", 1)[0] for name in names})))
PY
}

step "the framework wheel carries none of them"
top_level "${FRAMEWORK_WHEEL}" | tee "${WORK}/framework.txt"
for egg in "${EGGS[@]}"; do
  if grep -qx "${egg}" "${WORK}/framework.txt"; then
    fail "${egg} is inside $(basename "${FRAMEWORK_WHEEL}")"
  fi
done

step "the eggs wheel carries all of them, and not the framework"
top_level "${EGGS_WHEEL}" | tee "${WORK}/eggs.txt"
for egg in "${EGGS[@]}"; do
  grep -qx "${egg}" "${WORK}/eggs.txt" || fail "${egg} missing from $(basename "${EGGS_WHEEL}")"
done
if grep -qx jfastframework "${WORK}/eggs.txt"; then
  fail "the eggs wheel ships the framework package"
fi

# Each wheel goes into an empty directory with `pip install --target`, and is
# imported with `python -S`: -S keeps site-packages off sys.path, so the
# environment running this script -- which may well have the eggs installed --
# cannot answer for the wheel. A venv would isolate the same way but needs
# ensurepip, which Debian and Ubuntu split into a package that is often absent.
#
# --no-deps: the question is which modules a wheel puts on disk, and FastAPI's
# tree does not change the answer.
step "installing the framework alone: no easter eggs importable"
"${PY}" -m pip install --quiet --no-deps --target "${WORK}/plain" "${FRAMEWORK_WHEEL}"
test -d "${WORK}/plain/jfastframework" || fail "the framework wheel installed nothing"
for egg in "${EGGS[@]}"; do
  # find_spec rather than `import`: an import can fail for a reason unrelated
  # to the module being absent, and that would read as a pass here.
  if ! PYTHONPATH="${WORK}/plain" "${PY}" -S -c \
    "import importlib.util, sys; sys.exit(importlib.util.find_spec('${egg}') is not None)"; then
    fail "${egg} is importable with only jfastframework installed"
  fi
done
echo "plain install OK"

step "installing the eggs: every one imports and draws"
"${PY}" -m pip install --quiet --no-deps --target "${WORK}/eggs" "${EGGS_WHEEL}"
PYTHONPATH="${WORK}/eggs" "${PY}" -S - "${EGGS[@]}" <<'PY'
import contextlib
import importlib
import io
import sys

for name in sys.argv[1:]:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        module = importlib.import_module(name)
    assert captured.getvalue() == "", f"import {name} printed something"
    drawn = io.StringIO()
    module.show(drawn)
    art = drawn.getvalue()
    assert art.strip(), f"{name}.show() drew nothing"
    assert not art.startswith("\\"), f"{name}.show() opens with a stray backslash"
    print(f"{name}: {len(art.splitlines())} rows")
PY
echo "eggs install OK"
