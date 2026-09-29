#!/usr/bin/env bash
# End-to-end smoke check:
#
#     bash scripts/smoke.sh
#
# Verifies that the framework tests pass, the CLI resolves config, both module
# layouts scaffold code that compiles and passes its own tests, the HTMX
# overlay lands where it should, a whole service scaffolds and boots, and
# deploy generation reflects the enabled plugin graph.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prefer a local .venv when there is one (development), fall back to whatever
# is on PATH (CI installs into the job's own environment, with no .venv).
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
  JFAST="${ROOT}/.venv/bin/jfast"
else
  PY="$(command -v python3 || command -v python)"
  JFAST="$(command -v jfast)"
fi
[[ -x "${PY}" ]] || { echo "no python found"; exit 1; }
[[ -n "${JFAST}" ]] || { echo "jfast not installed; run: pip install -e '.[all,dev]'"; exit 1; }

WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT

step() { printf '\n=== %s ===\n' "$1"; }

step "framework tests"
cd "${ROOT}"
"${PY}" -m pytest -q

step "cli: doctor"
"${JFAST}" doctor

step "cli: describe"
"${JFAST}" describe --text

step "cli: plugins available"
"${JFAST}" plugins list --all

step "scaffold a service (kind=web)"
cd "${WORK}"
"${JFAST}" new service storefront --kind web --port 8020
cd "${WORK}/storefront"
find . -type f -not -name '.jfast-template' | sort

step "scaffold a layered module with the htmx overlay"
"${JFAST}" new module product --ui htmx

step "scaffold a screaming module"
"${JFAST}" new module invoice --layout screaming

step "generated code compiles"
"${PY}" -m compileall -q modules web.py main.py
echo "compileall OK"

step "generated module tests pass"
# Tests import `modules.<name>`, so the project root must be on the path.
PYTHONPATH="${WORK}/storefront" "${PY}" -m pytest -q modules

step "htmx overlay landed in the right places"
test -f modules/product/web.py            || { echo "MISSING modules/product/web.py"; exit 1; }
test -f templates/product/index.html      || { echo "MISSING templates/product/index.html"; exit 1; }
test -f templates/product/_rows.html      || { echo "MISSING templates/product/_rows.html"; exit 1; }
test -f templates/base.html               || { echo "MISSING templates/base.html"; exit 1; }
test -f static/app.css                    || { echo "MISSING static/app.css"; exit 1; }
grep -q '{{ item.name }}' templates/product/_row.html \
  || { echo "runtime Jinja was consumed at scaffold time"; exit 1; }
# /ui/ because the HTML surface has its own prefix. Sharing one with the
# JSON router meant whichever registered first answered: browsing returned
# JSON and the form POSTed into the API handler.
grep -q 'hx-delete="/ui/products/{{ item.id }}"' templates/product/_row.html \
  || { echo "scaffold-time table name not substituted, or the /ui/ prefix is gone"; exit 1; }
echo "overlay OK"

step "table names are pluralised (and dodge SQL reserved words)"
# -r: where the model lives depends on the layout, and the default is modular.
grep -rq '__tablename__ = "products"' modules/product \
  || { echo "expected products table"; exit 1; }
grep -q '__tablename__ = "invoices"' modules/invoice/storage.py \
  || { echo "expected invoices table"; exit 1; }
echo "naming OK"

step "alembic is wired and sees both module layouts"
test -f alembic.ini || { echo "MISSING alembic.ini"; exit 1; }
# Offline mode runs migrations/env.py without touching a database, which is
# enough to prove the DSN wiring and the model auto-import actually work.
JFAST_DB_DSN="postgresql+asyncpg://u:p@localhost:5432/db" \
  PYTHONPATH="${WORK}/storefront" "${PY}" -m alembic upgrade head --sql > /dev/null
PYTHONPATH="${WORK}/storefront" "${PY}" - <<'PYEOF'
import importlib
import pkgutil
from pathlib import Path

from jfastframework.db import Base

for package in pkgutil.iter_modules([str(Path("modules"))]):
    for candidate in ("models", "storage"):
        try:
            importlib.import_module(f"modules.{package.name}.{candidate}")
        except ModuleNotFoundError:
            pass

tables = sorted(Base.metadata.tables)
assert tables == ["invoices", "products"], tables
# The pinned naming convention is what keeps autogenerate diffs reproducible.
constraints = {c.name for c in Base.metadata.tables["products"].constraints}
assert "pk_products" in constraints, constraints
print("alembic OK", tables)
PYEOF

step "the web service boots and renders"
PYTHONPATH="${WORK}/storefront" "${PY}" - <<'PYEOF'
import asyncio

import httpx
from httpx import ASGITransport

import main


async def go() -> None:
    transport = ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with main.app.router.lifespan_context(main.app):
            for path in ("/health", "/"):
                response = await client.get(path)
                print(f"{path} -> {response.status_code}")
                assert response.status_code == 200, response.text[:200]
            body = (await client.get("/")).text
            assert "storefront" in body.lower(), "home page did not render the service name"
            assert "htmx.org" in body, "htmx script tag missing"
    print("web service OK")


asyncio.run(go())
PYEOF

step "deploy: compose with postgres + qdrant + mongo"
cd "${WORK}/storefront"
cat > jfast.toml <<'TOML'
[app]
name = "storefront"
port = 8020

[plugins]
enabled = ["observability", "metrics", "database", "qdrant", "mongo", "rag"]

[plugin.rag]
store = "qdrant"
TOML
"${JFAST}" deploy compose --stdout

printf '\nSMOKE OK\n'
