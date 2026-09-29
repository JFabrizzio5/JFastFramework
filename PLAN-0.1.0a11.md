# JFastFramework — plan for 0.1.0a11 and after

Written 2026-09-29, from building two real services on 0.1.0a10 — Cuadra (expenses +
AI advisor) and Dictamen (RAG for law firms) — and measuring them. Every item names
the evidence it came from; see `Cuadra/bitacora/PRUEBA-JFAST-0.1.0a10.md` for the run.

Legend: `[x]` done · `[~]` partial · `[ ]` not started

The four goals, and the one question each phase answers:

| Goal | The question |
| --- | --- |
| **No bad dependencies** | Can a project grow to twenty modules without two of them knowing each other's insides? |
| **Less work for the AI** | Does the first draft an agent writes already pass every check, with nothing to delete? |
| **Robust against errors** | When a dependency fails, does the service say so, degrade, and recover on its own? |
| **Scales** | Is every claim about capacity a number a CI job re-measures? |

Order rules, carried over from PLAN-NEXT.md:

1. **Dependency, not ambition.** A phase lands only when what it needs exists.
2. **A claim is promoted by a CI job against the real dependency**, never by feeling finished.
3. **Nothing the framework recommends may be a dead end.** New in this plan, because
   the worst finding below broke it.

**Release decision (2026-09-29):** the owner is effectively the only user of these
pre-releases, so all phases ship together as **0.1.0a11** instead of three releases. The
rigour does not change: each phase lands with its tests, the performance budget is
re-measured, and Cuadra is migrated as the acceptance test. (0.1.0a10 is on PyPI and a
version cannot be re-uploaded, so this is a11, not a10.)

---

## Phase 0 — Trust the baseline (first, because everything else is measured on it)

Nothing else is worth building while a freshly generated project fails its own checks.

- [ ] **A generated project passes ruff, format, mypy and pytest.** Evidence: `jfast start`
  + `jfast new module`, untouched: ruff 19 errors (B008 on `Depends`, I001 in templates),
  4 files unformatted, mypy 5 (`migrations/env.py`, generated `FakeX` tests).
  *Done when* a CI job generates a service in every layout, with every plugin, and runs
  all four with zero findings.
- [ ] **Upgrade smoke, a(n-1) → a(n).** Generate a project with the previous release from
  PyPI, install this checkout's wheel, run `jfast upgrade --check`, assert the exact
  warnings, apply the documented fixes, boot it, run its tests. *Done when* a CI job does
  that on every PR.
- [ ] **Tests for every `upgrades.py` detector**: an affected fixture and a clean one each.
  The nine added in a10 have none; they were only run by hand on Cuadra and Dictamen.
- [ ] **Windows clock in the default test run.** The JWKS single-flight bug passed on
  macOS/Linux for months. A pytest plugin that coarsens `time.monotonic` to 15.625 ms and
  reports that resolution to asyncio reproduced it (23 of 60 runs). Run the concurrency
  tests under it on every OS.

---

## Phase 1 — Modules that react to each other without depending on each other

The a10 contracts force modules through `public.py` and reject cycles — and then
recommend an event to break a cycle, which does not work in the default stack.

- [ ] **Local, durable domain events.** `@subscribe("comprobante.registrado")` declared in
  the subscribing module; `outbox.publish` with no Kafka creates one job per subscriber in
  the same transaction. With Kafka enabled, the same code goes through the broker.
  Evidence: in Cuadra, `outbox.publish` answered 201 and the event sat `pending` for 7
  attempts with `no event bus is configured`.
  *Done when* the Cuadra `alerta` flow runs on an event, the string-task workaround in
  `comprobante_service._publicar_registrado` is deleted, and `contracts check` still
  passes with no cycle.
- [ ] **`Event` inherits `tenant_id` and `request_id` from context**, as `Job` does.
  Today an event published inside a request reaches its consumer with no tenant.
- [ ] **Undeliverable is an error at boot, not a silent retry loop.** A published topic
  with no bus and no local subscriber stops startup with the fix in the message;
  `/ready` degrades while a message is stuck (it said `ok`); the relay log names the cause.
- [ ] **Events are part of the contract.** `[modules.x] publishes = [...]`, subscriptions
  discovered from code; `contracts show`, CONTRACTS.md and `jfast ai context` list who
  publishes what and who listens. A subscriber to an event nobody publishes is a violation.
- [ ] **Hidden coupling is detected.** `Job(task="<other_module>.…")` and
  `publish(... "<other>.…")` from a module that does not declare the relation are
  `undeclared-dependency`. Evidence: the Cuadra workaround passes today.
- [ ] **`depends_on` stays true.** An unused `depends_on` entry is reported (a stale
  declaration is how a graph starts lying), and `jfast inspect`'s own `module-cycle`
  uses declared dependencies as `contracts check` does (they disagree today).

---

## Phase 2 — Background work as a first-class citizen

Queue is on by default and nothing consumes it.

- [ ] **`@task` declared in the owning module**, discovered like routers: no handler
  living in a `worker.py` far from its logic.
- [ ] **`jfast worker`** — boots the app lifespan, registers every module's tasks and
  subscriptions, runs the worker; **`jfast dev` starts it** next to the API; the generated
  **compose and Kubernetes manifests include a worker service**.
- [ ] **A task receives a tenant-scoped session.** `async def revisar(payload, session:
  TaskSession)` — opened with the job's tenant, committed on return, rolled back on error.
  Today every handler writes `async with sessionmaker() ... commit()` and checks the
  tenant by hand.
- [ ] **Idempotency without thinking.** `@task(..., idempotent_on=lambda p: p["id"])`
  records the key with `claim_once` in the handler's transaction.
- [ ] **Dead letters you can see and replay.** `jfast jobs dead` / `jfast jobs retry <id>`,
  and the count on `/ready`.
- [ ] **Long AI work goes through the queue by default.** Evidence: reading a receipt
  holds a request 5–20 s. Recipe + generated example: upload → job → status endpoint.

---

## Phase 2b — Telemetry: optional, recommended, and across services

Logs and metrics exist; nothing follows one request through the API, the database, a
queued job, a model call and a second service. That is what is missed first when
something is slow in production.

- [ ] **`telemetry` plugin (OpenTelemetry traces).** Spans for each request, each SQL
  statement, each outbound HTTP call, each job, each `llm` call and each `rag` search,
  with tenant and request id as attributes. **Never prompt text, documents or answers** --
  the same rule as the `llm` ledger. Exports over OTLP to anything that speaks it
  (Jaeger, Grafana Tempo, Honeycomb, Datadog, an OpenTelemetry Collector).
- [ ] **Free until configured.** Enabled by default in generated services but exports
  nothing until `OTEL_EXPORTER_OTLP_ENDPOINT` is set; the cost with no endpoint is
  measured and kept inside the performance budget (Phase 5).
- [ ] **Across microservices.** W3C `traceparent`/`tracestate` injected by the `http`
  client and forwarded by the `gateway`, read by every service's middleware, so one trace
  spans every hop. Today the `http` client propagates `X-Request-ID` only.
  *Done when* a two-service workspace test (API → service B → its worker) produces one
  trace with every span in it.
- [ ] **Across the queue and events.** A job or event carries the trace context of the
  request that created it (as it already carries tenant and request id), so the
  worker's spans join the request's trace instead of starting an orphan one.
- [ ] **Infra on request.** `include_infra = true` adds an OpenTelemetry Collector and
  Jaeger to the generated compose, as `metrics` does for Prometheus and Grafana; one
  collector for the whole workspace, not one per service.
- [ ] **In the installer.** `jfast init` offers it pre-checked and labelled
  *recommended*; `jfast start` includes it; `--no-telemetry` leaves it out;
  `jfast add telemetry` / `jfast remove telemetry` for an existing project.

## Phase 2c — Accounts a SaaS can launch with

- [ ] **Email verification**, through the `mail` plugin, token single-use and expiring.
- [ ] **Password reset** by email, same token rules, every session revoked on reset.
- [ ] **MFA (TOTP)** with recovery codes, enforceable per role.
- [ ] **Login rate-limited by default** when `cache` is on (moved here from Phase 4).
- [ ] The generated front has the screens for all four.

---

## Phase 2d — Single-tenant today, multitenant tomorrow

Tenant isolation is configured piece by piece today (`tenancy` plugin, `rag.tenant_scoped`,
`llm.tenant_budget_usd`, `current_tenant` in routes), so they can contradict each other,
and moving a single-tenant app to multitenant is a manual hunt for every place that
assumed one customer.

- [ ] **One question in `jfast init`: "Does this app serve several customers?"** The answer
  sets every piece consistently. *No*: tenancy off, `rag.tenant_scoped = false`, no
  per-tenant budget, generated routes use `require_auth`. *Yes*: tenancy on (`user` or
  `token` source), tenant-scoped RAG, per-tenant budget, generated routes use
  `current_tenant`, RLS recommended.
- [ ] **The `tenant_id` column stays either way.** Every generated entity keeps its
  nullable `tenant_id` and its tenant-aware unique keys even when the answer is *No*:
  it costs almost nothing, and it is what makes the later switch a data backfill instead
  of a schema rewrite.
- [ ] **`jfast check` reports contradictions**: tenancy off while `rag.tenant_scoped` is on,
  `current_tenant` in a service with no tenant source, RLS policies with no tenant set.
- [ ] **`jfast check --multitenant-ready`** lists what a switch would break, with file and
  line, the way `upgrade --check` does: calls passing `tenant_id=None`, routes that touch
  data behind `require_auth` only, raw SQL without a tenant filter, storage keys and
  cache keys without the tenant, an unscoped RAG store, scheduled jobs with no tenant.
- [ ] **`jfast tenancy enable`** does the switch: a migration that backfills existing rows
  with an initial tenant, tenant RLS on every tenant table, the tenancy plugin on,
  RAG chunks re-keyed to the tenant and the store scoped, generated routes moved to
  `current_tenant`. *Done when* Cuadra, run as single-tenant, is switched by the
  command and a second tenant's data is invisible to the first -- verified by the
  database, not only by the repositories.
- [ ] **RLS is the safety net of the switch.** During and after it, a query the report
  missed returns no rows instead of another customer's rows: a visible bug, not a leak.

---

## Phase 3 — Less work for the AI (and for people)

Every module in Cuadra and Dictamen started by deleting generated code: 654 lines
generated, 276 kept for `alerta`.

- [ ] **`jfast new module X --fields "cartera_id:int, mes:str(7), leida:bool=false"
  --unique "cartera_id,mes"`** — entity, Pydantic models, `public.py` DTO and tests from
  the fields; **`--bare`** for no example fields at all.
- [ ] **Generated factories and dependencies are async** `[x]` (a10) — keep the guard.
- [ ] **Agent context shows the graph.** `jfast ai context` includes module facades,
  events, tasks and the RLS tables, so an agent writing a new module sees what exists
  instead of re-reading files.
- [ ] **Recipes that are tested.** `docs/recipes/`: upload → AI → review, budget alert by
  event, RAG answer with citations, tenant-per-user SaaS. Each one is a test that
  generates it and runs it — a recipe that stops working fails CI.
- [ ] **`jfast init` knows every plugin and recommends.** Its capabilities list stops at
  a8 (no `llm`, `accounts`, `outbox`, `idempotency`, `ratelimit`, `http`, `websocket`,
  `channels`, `telemetry`) and pre-selects nothing, so pressing Enter yields a service
  with none of them. Recommended ones pre-checked and labelled; the list generated from
  `PLUGIN_CATALOG` so a new plugin cannot be forgotten again.
- [ ] **Front template speaks `accounts`.** Evidence: the generated auth store expected
  `/auth/login` to return the user; `accounts` does not. Login, register and `/auth/account`
  work out of the box; axios timeout fits AI calls.

---

## Phase 4 — Robust against errors

- [ ] **Every external call has a deadline, a retry policy and a breaker by default** —
  `llm`, JWKS, storage, mail. The `http` plugin has breakers; the others do not.
- [ ] **Failure drills in CI.** Postgres down, Redis down, broker down, model provider
  returning 429/500: the service answers the documented status, `/ready` says which
  dependency, and it recovers without a restart. Today only the happy paths run against
  real servers.
- [ ] **Graceful shutdown for workers**: finish or release in-flight jobs on SIGTERM
  inside the visibility window; verified by killing a worker mid-job.
- [ ] **Boot fails on misconfiguration, never on first use.** Extend what `llm`/`rag` do
  (dimensions, budget period, missing plugin) to every plugin; add a test per rule.
- [ ] **Quiet by default where nothing is wrong.** `storage: no signing key` logs on every
  start of a service that never signs a URL.

---

## Phase 5 — Scale, with numbers a CI job keeps honest

Measured on 0.1.0a10 (docs/deploy.md#performance): the framework does ~8,500 req/s per
core with auth+tenancy on an endpoint without a database; with PostgreSQL, Cuadra's real
endpoints did 480–1,500 req/s on a laptop, and the database was the limit.

- [ ] **Performance budget in CI.** `ab`/`oha` against a generated service, framework
  overhead per request compared with bare FastAPI on the same runner; the build fails if
  it grows more than 20 %. The a10 fix (2,411 → 8,581 req/s) must not quietly regress.
- [ ] **A load-test harness shipped with the framework.** `jfast bench` generates a k6
  scenario from the routes (login, list, write, upload) with a mocked model, and reports
  where the service breaks: req/s, p99, errors, and which dependency saturated first.
- [ ] **Framework routes after the app's routes.** Health/info/ready are matched before
  every app route today (~8 µs per request measured); order them last or mount them apart.
- [ ] **Storage verified against MinIO in CI** (never run against S3); required before
  multi-replica deploys, because the local disk is per replica.
- [ ] **RLS behind PgBouncer (transaction mode) verified**, and read replicas under load.
- [ ] **Multi-replica proof.** Two API replicas + two workers against one Postgres and one
  Redis: outbox relay, scheduler, LLM budget and token revocation each happen exactly once.
- [ ] **RAG at scale.** 1M chunks across 1,000 tenants: ingest rate, p99 search with
  filters, HNSW build time, memory. Hybrid search's cost measured, not assumed.
- [ ] **Aggregates that stay fast with data.** A pattern (and generator) for pre-computed
  monthly totals refreshed by events — Cuadra's panel runs six aggregates per request,
  fine at 186 rows and not at millions.

---

## Order, and why

| # | Phase | Why here |
| --- | --- | --- |
| 0 | Trust the baseline | Every later claim is measured on a generated project; it must be green first. |
| 1 | Modules react without depending | The worst finding: the recommended pattern is a dead end. |
| 2 | Background work | Phase 1's events run as jobs; they need a real worker. |
| 2b | Telemetry | Traces must follow requests into jobs and other services, so it lands once jobs and events are first-class. |
| 2c | Accounts | Needs `mail` and the queue; without reset and verification no SaaS can open its doors. |
| 2d | Single-tenant → multitenant | Needs the worker (RAG re-key and backfill run as jobs on large tables) and accounts (the `user` source). |
| 3 | Less work for the AI | Generators encode Phases 1–2; building them earlier would encode the workarounds. |
| 4 | Robust against errors | Needs Phase 2 (workers, dead letters) to drill failures against. |
| 5 | Scale | Needs everything above to be the thing being measured. |

**All of it ships as 0.1.0a11** (see the release decision at the top). Acceptance:
the Cuadra workaround in `comprobante_service` is gone, Cuadra runs with telemetry
exporting to a local Jaeger, and every "Done when" above is a passing CI job.

## Still missing after this plan (next, in priority order)

1. **Backups and restore drills** for PostgreSQL -- scheduled, and a restore actually tested.
2. **Zero-downtime migrations** -- checks that flag a locking `ALTER TABLE` on a large table.
3. **Billing and plans** (Stripe), with per-plan limits that the `llm` budget can read.
4. **Admin panel** -- see and fix a customer's data without touching the database.
5. **Personal data export and deletion** per user.
6. **External security review** -- not something this repository can do for itself.
