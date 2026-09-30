# Trying it locally

JFastFramework is on PyPI as a pre-release. `pip install jfastframework` is
enough to use it; install from a checkout when you intend to change the
framework itself.

## From nothing to a running stack

```bash
pip install jfastframework
mkdir -p ~/projects/shop && cd ~/projects/shop
jfast start shop
```

No `--pre` is needed. Pip refuses a pre-release only when there is a stable
version to prefer instead; when every published version of a package is a
pre-release -- as every `0.1.0aN` here is -- it resolves the newest one.

Asking for `--pre` anyway costs something, because the flag is not scoped to
the package you named: it opts *every* dependency in the resolution into its
own pre-releases. You get an alpha FastAPI and an alpha pydantic under a
framework that was tested against neither, and the first failure lands in
somebody else's code.

## From a checkout, to change the framework itself

One block. Paste it into a Linux or WSL shell:

```bash
git clone https://github.com/JFabrizzio5/JFastFramework ~/github/JFastFramework \
  && cd ~/github/JFastFramework \
  && python3 -m venv .venv \
  && ./.venv/bin/pip install -e ".[all,dev]" \
  && export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH" \
  && mkdir -p ~/projects/shop && cd ~/projects/shop \
  && jfast start shop
```

That leaves you with a modular monolith, a Vue frontend, a compose file with
one container per datastore, a Caddyfile, and every connection string already
written. Then:

```bash
jfast workspace env      # generate the secrets and each service's .env
docker compose up -d     # the datastores
cd shop && ../.venv/bin/uvicorn main:app --reload --port 8010 --no-proxy-headers
```

`http://localhost:8010/docs` is the API, `/health` and `/ready` are the probes,
and `jfast workspace graph` prints what is connected to what.

**Windows:** run all of it inside WSL, not PowerShell. The generated scripts
are bash, and `npm` inside WSL resolves to the Windows binary unless Node is
installed in the distribution.

---

## Once, to install it

```bash
cd ~/github/JFastFramework
python3 -m venv .venv
./.venv/bin/pip install -e ".[all,dev]"
```

`-e` is an editable install: the `jfast` command runs the code in this
checkout, so editing the framework takes effect immediately, with no
reinstall.

Put it on your PATH for the session:

```bash
export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"
jfast version
```

Or make it permanent:

```bash
echo 'export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"' >> ~/.bashrc
```

### If `python3 -m venv` fails

Ubuntu splits `venv` out of the base Python. Either install it:

```bash
sudo apt install python3-venv
```

or bootstrap pip into the venv without it:

```bash
python3 -m venv --without-pip .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | ./.venv/bin/python
```

---

## A new project, the fast way

```bash
mkdir ~/projects/shop && cd ~/projects/shop
jfast start shop
```

That is the whole thing. You get:

```
shop/                  FastAPI + PostgreSQL/pgvector + Redis + background jobs
  modules/item/        a starter module whose tests already pass
  contracts.toml       the rules this service holds itself to
  alembic.ini          migrations, reading the app's own DSN
shop-web/              Vue 3 + Vite + Tailwind, pointed at the backend
docker-compose.yml     derived from the enabled plugins
Caddyfile              one hostname in front of both
jfast.workspace.toml   ports, and what the frontend should call
```

`jfast start` asks nothing, so two decisions are flags:

- **Customers.** `--single-tenant` (the default) or `--multitenant`. Multitenant
  turns on `tenancy` (reading the tenant from the signed token or the
  signed-in user), `auth` and `accounts`, generates routes that take the tenant
  from `current_tenant`, and writes a `JFAST_AUTH_SECRET` and a first-admin
  password into `shop/.env`. Single-tenant is the default because a
  multitenant service needs a user system and a signed-in caller before its
  first `curl` works -- the right shape for a SaaS, too much for anything else.
  Every table keeps its `tenant_id` column either way, so switching later is a
  backfill, not a schema rewrite.
- **Traces.** `telemetry` is on and exports nothing until
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. `--no-telemetry` leaves it out.

### Run the backend

```bash
cd shop
pip install -r requirements.txt
cp .env.example .env          # fill in POSTGRES_PASSWORD
pytest                        # the starter module's tests
jfast serve --port 8000
```

`jfast serve` rather than `uvicorn main:app` because uvicorn's own
`X-Forwarded-For` handling is on by default and trusts loopback, which is what
a dev server binds. It would rewrite the client address before the framework's
`trusted_proxies` ever saw a peer, so a rate limit tested here would pass for
the wrong reason. Starting uvicorn by hand needs the flag off:

```bash
uvicorn main:app --reload --port 8000 --no-proxy-headers
```

```bash
curl localhost:8000/health
curl localhost:8000/docs      # OpenAPI UI
```

Endpoints that need the database only work once one is running:

```bash
docker compose up -d shop-database
jfast exec -- alembic revision --autogenerate -m "initial"
jfast exec -- alembic upgrade head
```

`jfast exec --` runs a command with the service's `.env` translated for your
machine. In a workspace that `.env` is written for compose -- the database is
`shop-database:5432` and the password is `${SHOP_DATABASE_PASSWORD}`, which
only compose fills in -- so a bare `alembic` or `pytest` on the host fails
with a DNS error. `jfast serve`, `jfast worker` and `jfast dev` translate it by
themselves; everything else goes through `jfast exec --`. With no
`docker-compose.yml` beside the service or one level up it changes nothing.

### Run the frontend

```bash
cd ../shop-web
npm install
npm run dev                   # http://localhost:8010
```

The home page calls the backend's `/health` on load, so if the wiring is wrong
you see it immediately rather than on your first real feature.

### Or all of it in Docker

```bash
cd ~/projects/shop
docker compose up --build
```

---

## A new project, choosing as you go

```bash
jfast init
```

Asks what you are building, which datastores you want, whether the app serves
several customers, which capabilities, and on which port -- then generates
exactly what the flags would have.

**"Does this app serve several customers (multitenant)?"** One answer sets
every piece that has to agree, so they cannot contradict each other:

| | Yes | No |
| --- | --- | --- |
| `tenancy` | on, `sources = ["token", "user"]` | off |
| generated routes | `current_tenant` (401/403 without one) | `require_auth` with auth, open without |
| `[plugin.rag] tenant_scoped` | `true` | `false` |
| `[plugin.llm] tenant_budget_usd` | set | absent |
| pre-checked | + `auth`, `accounts` | |
| `tenant_id` columns | kept | kept |

**Capabilities** are listed from the plugin catalog, so a plugin added to the
framework cannot be missing from the menu. The recommended ones are
pre-checked, labelled *recommended* and listed first -- pressing Enter keeps
them:

- `telemetry` -- traces cost nothing until an endpoint is set, and they are
  what you need first when something is slow in production, when turning them
  on no longer helps.
- `queue` -- anything slower than a request belongs off it; the backend is the
  PostgreSQL the service already has, and `jfast worker` consumes it.
- `auth` and `accounts`, when the answer above is yes: a tenant comes from
  somebody signed in.

A plugin the installed framework does not have is skipped with a note rather
than written into a `jfast.toml` that would not boot.

The same, with flags:

```bash
jfast new service billing --with database,cache,queue
jfast new service edge --language go
jfast new service admin --kind spa --frontend vue
jfast new service saas --with database,queue,telemetry --multitenant
```

A frontend draws sign-in, registration and a security page, and keeps its
routes private by default, only when some backend in the workspace enables
`accounts`; otherwise it stays public with no account pages.

### Adding and removing a plugin later

```bash
jfast add telemetry          # [plugins].enabled, the extra, its settings block
jfast add accounts           # also enables auth (and database), which it requires
jfast remove telemetry       # refuses while another enabled plugin requires it
```

`jfast add` edits `[plugins].enabled` in `jfast.toml` -- keeping every comment
-- pins the plugin's extra in `requirements.txt`, appends the settings block a
new service would have been generated with, and prints what is left: the
variables it reads and the step after (`alembic upgrade head`, `jfast worker`).
`jfast remove` drops the extra when nothing else enabled uses it and leaves
the `[plugin.<name>]` block for the day it comes back. `jfast add` with no
argument lists plugins and capabilities together.

---

## The loop you will actually use

```bash
jfast dev                         # containers, migrations, API and frontend
jfast new module invoice          # asks which architecture; registers itself
jfast exec -- pytest modules/invoice/tests
jfast contracts check             # layer boundaries, forbidden calls
jfast exec -- alembic revision --autogenerate -m "add invoices"
```

The module mounts itself: the generator splices the import and the router into
`main.py` at the markers it left there. See [The local loop](dev.md) for what
`jfast dev` does at each stage and what it skips when something is missing.

---

## If `jfast` is not on your PATH

```bash
python -m jfastframework --help
python -m jfastframework start shop
```

Identical to the `jfast` script. Useful on a Windows install where `Scripts/`
is not on PATH, in a virtualenv nobody activated, or in a CI step that would
rather not guess where pip put the binary.

---

## A note on Windows terminals

The CLI resolves every symbol it prints against the encoding your console
actually reports, and falls back to ASCII when a glyph will not fit:

```
+---------------------------------+
|  jfastframework                 |
|  the opinionated default stack  |
+---------------------------------+
  + jfast.workspace.toml        workspace
```

This is not cosmetic. A Windows console is `cp1252` or `cp850` far more often
than UTF-8, and neither has `✓` or the box-drawing characters — writing one
does not print a placeholder, it raises `UnicodeEncodeError` mid-write. Before
the fallback existed, `jfast start` died with a traceback **after** creating
half a project.

Nothing to configure. If you want the drawn version in a terminal that can
handle it, set `PYTHONIOENCODING=utf-8`.

---

## Working on the framework itself

```bash
cd ~/github/JFastFramework
pytest                                  # unit tests, ~1s
ruff check src tests docs-site
mypy src

bash scripts/smoke.sh                   # both module layouts, HTMX, alembic
bash scripts/smoke_contracts.sh         # contracts catch what they should
bash scripts/smoke_workspace.sh         # workspace, gateway, view patching
bash scripts/smoke_start.sh             # the default stack end to end
bash scripts/smoke_go.sh                # needs go on PATH
bash scripts/smoke_frontend.sh          # needs npm on PATH
```

The smoke scripts each generate a project in a temp directory, run it, and
delete it. They are the only checks that catch a template which renders
cleanly and produces code that does not work — `pytest` alone cannot.

### Optional toolchains

The Go and frontend suites skip themselves when their toolchain is missing.
To run them locally without touching your system:

```bash
mkdir -p ~/.jfast-toolchains && cd ~/.jfast-toolchains
curl -sSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz | tar xz
curl -sSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz | tar xJ
mv node-v22.12.0-linux-x64 node
export PATH="$HOME/.jfast-toolchains/go/bin:$HOME/.jfast-toolchains/node/bin:$PATH"
```

Delete the directory to undo it. Nothing is installed system-wide.

---

## Common problems

**`jfast: command not found`** — the venv is not on PATH. Either export it as
above, or call it directly: `~/github/JFastFramework/.venv/bin/jfast`.

**`No jfast.toml found`** — `jfast describe`, `doctor` and `deploy` run inside
a service directory. `jfast start`, `init` and `workspace` run above it.

**`Port 8011 is already taken by 'billing'`** — the workspace allocates
ten-port blocks. Let it pick (`jfast new service …` with no `--port`) rather
than choosing by hand.

**Alembic can't reach the database** — `migrations/env.py` reads
`JFAST_DB_DSN` from `.env`, deliberately the same value the app uses. Start
the container first: `docker compose up -d <service>-database`. If the error is
`nodename nor servname provided` or `Name or service not known` naming
`<workspace>-database`, the `.env` is the compose one: run it as `jfast exec --
alembic ...`.

**Windows** — run all of this inside WSL. The generated Dockerfiles, the
compose files and the shell scripts assume a POSIX shell.

**`413 Payload Too Large` on a request that used to work** — a body limit
ships on, at 2 MiB, and a request timeout at 30 seconds. Enabling the `storage`
plugin raises the pair to 25 MiB and 120 seconds. The kernel does that, not the
scaffold: `effective_max_body_bytes` and `effective_request_timeout` read the
plugin list, so a service that enables `storage` a year after it was generated
gets the same pair without regenerating anything. Writing the field in
`jfast.toml` always wins over the raise — including `max_body_bytes = 0`, which
lifts the limit entirely, and `request_timeout = 0`, which lifts the timeout.
`jfast new service --with storage` writes both out at the raised values, so the
number is visible in the file; lower them there to what the service really
accepts. A `504 Gateway Timeout` on a slow endpoint is the same story with
`request_timeout`.

**The browser blocked a script or a stylesheet** — a Content-Security-Policy
ships on. It allows what the framework's own pages load and nothing else, so
the first third-party asset you add to a template gets refused. Add its origin
to `csp` in `jfast.toml`; [deploy.md](deploy.md) has the full policy and the
path to a tighter one.

**`http://localhost` suddenly redirects to HTTPS** — that is HSTS, and it did
not come from here: HSTS stays off outside production and is withheld from any
request that did not arrive over HTTPS. Something else you ran on `localhost`
sent it, and the browser remembers it per host for the whole `max-age`. Clear
it at `chrome://net-internals/#hsts` (or the Firefox equivalent) — clearing
the site's cache will not.
