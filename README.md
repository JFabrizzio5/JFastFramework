# JFastFramework

**Generate the backend nobody wants to write twice, and keep it from decaying
while a team — or an agent — works on it.**

One command gives you a FastAPI service with PostgreSQL, Redis, background
jobs, a Vue frontend and a reverse proxy, wired together and running under
Compose. What you write is the part only you know: the rules of your business.
What keeps it coherent afterwards is a contract that CI enforces.

Status: `0.1.0a5` — alpha, on PyPI. Maturity is tracked per subsystem rather
than by one version number: [STATUS.md](STATUS.md) says what is tested against
real infrastructure, what is unverified, and what is known broken. Read it
before depending on any single part.

---

**[jfastframework on PyPI](https://pypi.org/project/jfastframework/)** &middot; **[Documentation](https://jfabrizzio5.github.io/JFastFramework/latest/)**

---

## One command

```bash
pip install jfastframework
jfast start shop
```

```
shop/                 FastAPI · PostgreSQL · pgvector · Redis · jobs
shop-web/             Vue 3 · Vite · Tailwind
docker-compose.yml    one container per resource
Caddyfile             one hostname, TLS, static assets
.env                  every DSN, generated from the bindings
```

```bash
jfast dev             # containers up, migrations applied, API and frontend running
```

Prefer to choose? `jfast init` asks. Prefer flags? Every choice is one.

---

## Who this is for

| Reach for it when | Because |
| --- | --- |
| You start backends often — agency work, internal tools, a product with several services | The plumbing is generated and versioned, so the tenth one costs what the first did |
| A small team will maintain what you write | Layer boundaries are checked by CI, so a review is about the feature rather than about where the file went |
| You are driving AI agents through a codebase | Every rule is machine-readable and enforced, so an agent that drifts fails the build instead of merging |
| You want to start as a monolith and split later | Module boundaries are checked, so the seams stay real; when one outgrows the rest, `jfast new service` stands up its deployment and rewires the workspace |

### When not to use it

Honest answers matter more here than another feature.

| Do not reach for it when | Use instead |
| --- | --- |
| You are writing one endpoint, or a script with a web UI bolted on | Plain FastAPI. This is a lot of structure for one file |
| The team already has a house framework and conventions that work | Yours. The value here is the opinions, and you already have some |
| You need Django's admin, its ORM ecosystem or its auth out of the box | Django. This is not trying to be that |
| You are on a synchronous stack and do not want async | Flask, or FastAPI without this |
| You need production-grade multi-tenancy isolation today | Not yet — tenancy here is a convention, not row-level security. [STATUS.md](STATUS.md) is explicit about it |

---

## What you actually get

```
shop/                       one service
├── main.py                 routers register themselves here
├── jfast.toml              which plugins are on; which layout each module uses
├── contracts.toml          the rules, checked by CI
├── modules/
│   └── invoice/            one business capability
│       ├── router.py           HTTP in, response out
│       ├── service.py          the rules — no SQL, no Request
│       ├── repository.py       queries — no HTTP concepts
│       ├── schemas.py
│       └── tests/
├── shared/                 what two modules both need
└── migrations/
```

The framework is a **library your service imports**, not code copied into it.
Fix something in the framework and every service gets it on the next version
bump. What is generated is only the part that is genuinely yours.

---

## Use cases it was built for

These are the shapes it fits. If yours is not one of them, the table above is
the honest guide.

**An agency starting a client project every few weeks.** The eighth backend
should not cost what the first did. `jfast start` produces the same stack every
time, so the developer who picks it up in a year finds the layout they already
know, and a framework fix reaches all eight through a version bump rather than
eight patches.

**An internal tool that will outlive its author.** The contract is the
handover: `jfast contracts show --json` states what the service owns, what it
does not, and which boundaries are enforced. The next person does not have to
infer the architecture from the code.

**A product that starts as one service and does not know its seams yet.** You
begin as a modular monolith with real module boundaries — checked, so they do
not quietly erode — and promote a module to its own service when the load or
the team says so. Splitting later is a move; un-splitting is a rewrite.

**A codebase where agents write most of the code.** The rules are executable.
An agent that queries the database from a router, imports one module into
another, or blocks the event loop fails `jfast contracts check` with the file,
the line and the fix. That is the difference between an agent you supervise and
one you can leave running.

---

## The architecture you get

**A modular monolith, with the seams visible.** One deployable, several
modules, and boundaries that are enforced rather than agreed:

- A module never imports another module. What two modules both need moves to
  `shared/`, and the checker names the file to move it to.
- `shared/` never imports a module. Without that second rule, `shared/` becomes
  where everything ends up.
- Nothing in `shared/` touches the database. Two modules sharing a repository
  is two modules sharing a table.

**Each module picks its own shape.** A catalogue is four files; an orders
module that must be testable without a database wants ports and adapters.
Four layouts — `layered`, `modular`, `screaming`, `hexagonal` — chosen per
module, and remembered in `jfast.toml` so later commands know where a new file
belongs. [docs/modules.md](docs/modules.md) has the question that tells you
which to pick.

**And a way out.** When a module outgrows the monolith,
`jfast new service billing` scaffolds its service and the workspace rewires the
ports, the DSNs and the gateway around it. **Moving the module's code across is
still yours to do** -- nothing here extracts it for you. What the tool removes
is the infrastructure work, which is the part that usually stops people. You do
not have to guess the seams on day one, which is the whole reason to start as a
monolith.

---

## What problem this solves

Scaffolding generators copy code. Every generated service becomes a fork frozen
at generation time: fix a bug in the shared HTTP client and you patch it in
twelve places by hand.

JFast splits the two concerns that generators conflate:

| | |
| --- | --- |
| **Runtime** (`jfastframework`) | A versioned library your services **import**. Fix it once, bump the pin. |
| **Generator** (`jfast` CLI) | Emits only the code that is genuinely yours. |

The second half is the one most tools stop before: **generated structure decays
unless something holds it.** So the generator also emits a `contracts.toml`,
and `jfast contracts check` fails the build when the code drifts from it.

Everything above the kernel is a plugin.

```toml
[plugins]
enabled = ["observability", "metrics", "database", "cache", "queue"]
disabled = ["sentry"]
```

Delete `"cache"` and the Redis client, its health check and its container in
the generated compose file all disappear together. Infrastructure is derived
from the plugin graph, so it cannot drift from what the app actually loads.

## Plugins

| Plugin | Does | Extra |
| --- | --- | --- |
| `observability` | JSON logs, request-id and tenant correlation | — |
| `metrics` | Prometheus RED metrics, `/metrics` | `metrics` |
| `database` | Async SQLAlchemy, sessions, Alembic wiring | `db` |
| `cache` | Redis cache, pub/sub | `cache` |
| `queue` | Background jobs on PostgreSQL, Redis or RabbitMQ | `queue` |
| `events` | Kafka publish/subscribe | `kafka` |
| `mongo` | MongoDB via Motor | `mongo` |
| `qdrant` | Qdrant vector database | `qdrant` |
| `rag` | Retrieval over pgvector or Qdrant | `rag` |
| `web` | Jinja2 + HTMX partial rendering | `web` |
| `gateway` | Prefix-based reverse proxy | `gateway` |
| `auth` | JWT verification, scopes, revocation, social login | `auth` |
| `ratelimit` | Token bucket per tenant and per subject, in Redis | `cache` |
| `channels` | Declared pub/sub channels over memory, Redis or Kafka | — |
| `websocket` | Authenticated connections, fan-out through Redis | `server` |
| `mail` | Email with templates, queued by default | `mail` |
| `storage` | Files on local disks, S3 or MinIO | `storage` |
| `tenancy` | Tenant from a token claim, subdomain or path | — |
| `notifications` | Push over Firebase Cloud Messaging | `fcm` |
| `sentry` | Error and performance reporting | `sentry` |

Third-party plugins register through the same entry-point group, so nothing
here is privileged.

---

## Not only Python

A JFast service is not "a service written with JFast" — it is a service that
satisfies [the contract](docs/service-contract.md): `/health`, `/ready`,
`X-Request-ID`, `problem+json`, `JFAST_*` config, a ten-port block, JSON logs
on stdout.

```bash
jfast new service edge --language go --grpc
```

That Go service has **zero third-party dependencies** and implements the
contract in ~300 vendored lines. The gateway routes to it without knowing it is
Go; the workspace allocates its ports; Caddy fronts it alongside everything
else — because they all talk to the contract, not to the language.

CI runs `go vet`, `go test`, `go build`, starts the binary and curls it. A
scaffold nobody has run is a liability that looks like a feature.

`--grpc` generates the `.proto` contract. It does **not** generate stubs or
wire a server — see [proto/README.md](src/jfastframework/templates/proto/proto/README.md.j2)
for why, and for the commands to do it yourself.

---

## Queues and events

Two different things, two plugins:

```toml
[plugin.queue]
backend = "postgres"    # or "redis", "rabbitmq"
```

```python
@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None: ...

await queue.enqueue(Job(task="send_invoice_email", payload={"id": 7}))
```

Start with PostgreSQL: enqueueing shares the transaction that produced the
work, so a rollback takes the job with it. Redis buys latency, RabbitMQ buys
routing. [Which to pick, and why](docs/queues-and-events.md).

Delivery is at-least-once — handlers must be idempotent. Retries are bounded
and backoff is capped; exhausted jobs are dead-lettered rather than looping.

Events are the other half: `events` is Kafka, for "this happened" rather than
"do this".

---

## Frontends

**Server-rendered**, no build step:

```bash
jfast new service storefront --kind web
jfast new module product --ui htmx
```

**SPA**, Vue 3 or React with Vite and Tailwind v4:

```bash
jfast new service admin --kind spa --frontend vue
cd admin && jfast new view Facturas
```

`jfast new view` creates `src/ModuloFacturas/{Components,Pages,Routes,Services}`
and registers it in the router and the sidebar at their marker comments —
idempotently, failing loudly if a marker is gone.

Both frontends are installed and built in CI. That job exists because of a real
bug: the marker comment sat inside a block comment, whose inner `*/` closed it
early and left the router syntactically invalid. Every grep passed. Only
`vite build` caught it.

Angular and React Native are **not** generated. [Why](docs/frontend.md#angular).

---

## Workspaces, gateway, Caddy

```bash
jfast workspace init cometax
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag     # a gateway appears here
jfast workspace compose && jfast workspace caddy
```

Services register themselves and take the next free ten-port block. At the
**second** backend a gateway is generated automatically — one backend
deliberately does not get one, because it would add a hop and an outage surface
for nothing.

Caddy is the edge (TLS, HTTP/3, compression, the built SPA); the gateway is the
application proxy behind it. Backends live under `/api` either way, so the
frontend's production build survives a gateway appearing.

[docs/workspaces.md](docs/workspaces.md) · [docs/deploy.md](docs/deploy.md)

---

## Migrations and tests, wired

Every generated service ships `alembic.ini`, `migrations/env.py`, `pytest.ini`
and `conftest.py`. `env.py` reads the same `JFAST_DB_DSN` the app does — a
migration cannot run against a different database than the service — and
imports every module's models automatically, so autogenerate never silently
emits an empty migration. [Details and the traps](docs/migrations-and-tests.md).

---

## Authentication

```toml
[plugin.auth]
mode = "jwks"                    # jwks | public_key | secret
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

Verification, scopes and roles, JWKS key rotation, refresh rotation with reuse
detection, and revocation shared across replicas through Redis.

The defaults refuse the attacks that do not look like failures: `alg: none`,
RS256→HS256 confusion (configuring both algorithm families at once is rejected
at startup — that combination *is* the attack), tokens minted for a sibling
service, and a generous clock skew. Rejection reasons go to the log; the client
gets a plain 401.

**Tenancy stops being forgeable.** Without auth, `tenant_id` comes from the
`X-Tenant-ID` header — settable by anyone with curl. With it, from a signed
claim.

There is no `/auth/login`: checking a password against your user table is your
application's job. `auth.issuer` is provided for your own route.
[docs/auth.md](docs/auth.md).

## Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` asks whether you need it. You get a kustomize tree: Deployment,
Service, ConfigMap, HPA and PodDisruptionBudget per service, one Ingress
serving `/api` — the same public shape as the generated Caddyfile — and
`dev`/`prod` overlays.

Liveness probes `/health`, readiness probes `/ready`. That two-endpoint
contract is what stops a database blip from restarting every healthy pod at
once.

**Databases are not generated.** A StatefulSet for PostgreSQL from a scaffolder
is how people lose data. The manifests read a DSN from a Secret.
[docs/kubernetes.md](docs/kubernetes.md).

## Contracts: rules an agent cannot drift past

`AGENTS.md` says what to do. A **contract** says what is allowed, and something
checks it — which is the difference between a rule and a suggestion.

Every generated service ships a `contracts.toml` you own:

```toml
[project]
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py"]
why = "Configuration is typed. Add a field to a settings model."
```

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'
  (Data access. No business rules.)
```

The same file turns on the check for the bug that never raises: a blocking
call inside `async def`, which stalls every other request on the worker and
shows up as latency somewhere else entirely.

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
```

Non-zero exit — in CI, a failed build. An agent generating code at speed drifts
past prose; it does not drift past a failing check.

Three audiences, one file: the build reads it through `check`, an agent through
`jfast contracts show --json`, a reviewer through the generated `CONTRACTS.md`.
Waivers are inline and require a reason. [docs/contracts.md](docs/contracts.md).

## Why AI agents are a first-class audience

```bash
jfast contracts show --json  # the rules THIS project holds itself to
jfast describe --json        # settings schema, plugin graph, providers, infra
jfast workspace list --json  # services, ports, API base URL, needs_gateway
jfast doctor
```

No grepping. Plus `.jfast/skills/` — one folder per task with a `SKILL.md`
stating when to use it and the exact steps — and [AGENTS.md](AGENTS.md), the
rules an agent must follow here.

---

## Safe terminal novelty imports

The distribution also includes two opt-in, side-effect-free terminal helpers:

```python
def surprise() -> bool:
    import pene
    import vagina

    pene.show()                # bundled terminal art
    vagina.show()              # bundled terminal art
    return pene.play_video(vagina)
```

The combined helper opens the configured YouTube video in a new browser tab.
Playback still follows the browser's autoplay policy. Importing either module
alone prints nothing and does not open a browser.

---

## Documentation

The site is built from these same files: **<https://jfabrizzio5.github.io/JFastFramework/>**

| Document | Contents |
| --- | --- |
| [docs/local-setup.md](docs/local-setup.md) | Installing from a checkout and making your first project |
| [docs/dev.md](docs/dev.md) | The local loop: containers, migrations, API and frontend in one command |
| [docs/agents.md](docs/agents.md) | Working with AI agents: what is enforced, and what is not |
| [docs/contracts.md](docs/contracts.md) | Per-project rules, enforced |
| [docs/auth.md](docs/auth.md) | JWT: modes, the attacks refused, revocation, Google login |
| [docs/ratelimit.md](docs/ratelimit.md) | A token bucket that does not leak under load |
| [docs/websockets.md](docs/websockets.md) | Sockets across workers, and what is not delivered |
| [docs/upgrading.md](docs/upgrading.md) | What breaks on a version bump, filtered to what applies to your project |
| [docs/storage.md](docs/storage.md) | Disks, signed URLs, S3 and MinIO |
| [docs/multitenancy.md](docs/multitenancy.md) | Subdomains, trust order, what it is not |
| [docs/cloud.md](docs/cloud.md) | Secret managers, serverless functions, push |
| [docs/kubernetes.md](docs/kubernetes.md) | Manifests, probes, what is not generated |
| [docs/service-contract.md](docs/service-contract.md) | What every service must do, in any language |
| [docs/modules.md](docs/modules.md) | Module layouts, HTMX, service kinds |
| [docs/datastores.md](docs/datastores.md) | PostgreSQL, Redis, Mongo, Qdrant |
| [docs/queues-and-events.md](docs/queues-and-events.md) | Jobs, streams, backends |
| [docs/frontend.md](docs/frontend.md) | HTMX, Vue, React, the view generator |
| [docs/workspaces.md](docs/workspaces.md) | Many services, the gateway |
| [docs/migrations-and-tests.md](docs/migrations-and-tests.md) | Alembic, pytest |
| [docs/plugins.md](docs/plugins.md) | Writing a plugin |
| [docs/deploy.md](docs/deploy.md) | Compose, Caddy, Dockerfile |
| [docs/skills.md](docs/skills.md) | Writing a skill |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Decisions and their costs |
| [PLAN.md](PLAN.md) | Done, partial, not started |

## Verify

```bash
pytest                             # 397 framework tests
ruff check src tests docs-site && ruff format --check src tests docs-site
mypy src                           # strict

bash scripts/smoke.sh              # both module layouts, HTMX, alembic, a booting service
bash scripts/smoke_contracts.sh    # a generated service passes its own contract
bash scripts/smoke_auth_k8s.sh     # auth guards routes; manifests parse
bash scripts/smoke_workspace.sh    # workspace, gateway, view patching
bash scripts/smoke_start.sh        # the default stack, end to end
bash scripts/smoke_docs.sh         # the quickstart, run exactly as written
bash scripts/smoke_go.sh           # go vet, test, build, run, curl   (needs go)
bash scripts/smoke_frontend.sh     # npm install + vite build         (needs node)
python docs-site/build.py --version latest --output site/latest
python docs-site/check.py site/latest
```

## What is not verified

Said plainly, because a framework that overstates its coverage is worse than
one that admits the gap:

- **RabbitMQ and Kafka** are written against documented APIs but never
  round-tripped against real brokers in CI.
- **Multi-tenancy** is a convention enforced by `BaseRepository`, not an
  isolation guarantee. Row-level security is phase 2.
- **RAG** chunks at fixed width with no reranking.
- **Angular, React Native, Laravel, .NET** are not generated at all.

## Working on the framework itself

```bash
git clone https://github.com/JFabrizzio5/JFastFramework
cd JFastFramework
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

`[dev]` pulls every plugin extra, because the suite imports what the plugins
import. The suites needing a real server skip without one; CI runs them against
service containers:

```bash
JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5432 \
JFAST_TEST_REDIS_URL=redis://localhost:6379/0 pytest -q
```

Before opening a pull request, the four gates CI runs:

```bash
ruff check src tests && ruff format --check src tests && mypy src && pytest -q
```

`mypy --platform win32 src` too, if you touched anything that branches on the
operating system.

## License

MIT. See [LICENSE](LICENSE).
