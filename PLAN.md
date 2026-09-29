# JFastFramework — Roadmap

Phases are ordered by dependency, not by ambition. Each one ends with something
usable; nothing is half-shipped into the next phase.

Legend: `[x]` done · `[~]` partial, gaps named · `[ ]` not started

Two proposals sit beside this file and are **not** commitments:
[PLAN-NEXT.md](PLAN-NEXT.md) for the road to 1.0, and
[PLAN-CLI.md](PLAN-CLI.md) for growing the CLI from a generator into a
lifecycle tool, and [PLAN-A5.md](PLAN-A5.md) for the twenty findings a
second external report raised against 0.1.0a4. A step moves here only once
it is accepted.

---

## Phase 0 — Kernel (done)

The smallest thing that makes every later phase possible.

- [x] `create_app()` — plugin resolution, registration, lifespan orchestration
- [x] `JFastSettings` / `JFastConfig` — typed config from `jfast.toml` + env
- [x] `AppContext` — `provide` / `require` indirection between plugins
- [x] Plugin contract — `meta`, `Settings`, lifecycle hooks, `infra()`, `describe()`
- [x] Registry — entry-point discovery, allow/deny lists, dependency ordering,
      cycle detection, duplicate-provider detection
- [x] RFC 7807 error model with handlers for domain, HTTP, validation and
      unhandled exceptions
- [x] `/health`, `/ready`, `/info`
- [x] Test fixtures (`build_test_app`, `client_for`, `NullPlugin`)
- [x] Kernel test suite

**Exit criteria met:** a service is `create_app()` and a `jfast.toml`.

---

## Phase 1 — Built-in plugins (mostly done)

- [x] `observability` — JSON logging, request-id and tenant-id correlation,
      access log. Default-enabled, zero extra dependencies.
- [x] `metrics` — Prometheus RED metrics, route-template labels (no cardinality
      explosion from path parameters), `/metrics`.
- [x] `sentry` — off by default, `SecretStr` DSN.
- [x] `database` — async SQLAlchemy engine, session factory, request-scoped
      session dependency with commit/rollback.
- [x] `cache` — Redis facade plus raw client, non-critical health check.
- [x] `mongo` — Motor client and database handle, for document-shaped data.
- [x] `qdrant` — client, health check, container with HTTP and gRPC ports.
- [x] `web` — Jinja2, static files, HTMX partial rendering, HTML error
      fragments for HTMX requests.
- [x] `VectorStore` protocol with pgvector and Qdrant implementations; the
      `rag` plugin picks one from config and fails at startup, naming the
      missing plugin, when the choice does not match the enabled graph.
- [~] `rag` — pluggable store and embedder, ingest/search/delete router.
      **Gaps:** fixed-size chunking only (bad for code and tables); no
      reranking; no hybrid BM25 + vector search; `ensure_schema` runs DDL at
      startup instead of through Alembic.

### Remaining in this phase

- [ ] `auth` — JWT RS256 verification, JWKS fetch with rotation and cache,
      scope dependencies
- [ ] `worker` — Arq wrapper: task registry, retry policy, dead-letter queue
- [ ] `internal_client` — service-to-service HTTP with retry, exponential
      backoff, **circuit breaker**, `X-Request-ID` propagation
- [x] `websocket` — connection registry over the `Channel` Redis backplane;
      subprotocol handshake, bounded send buffer, heartbeat, tenant-scoped
      rooms. Cross-worker delivery proven against a real Redis locally, not yet
      in CI — see `STATUS.md`.

---

## Phase 2 — Multi-tenancy (partial)

The pitch of every SaaS factory and the part everyone gets wrong late. Decide
the strategy before writing any of it, because changing it afterwards is a data
migration, not a refactor.

- [x] Choose: column `tenant_id` + enforced filter · PostgreSQL row-level
      security · schema-per-tenant. **RLS**, on top of the column: the only
      option where forgetting a filter is not a data leak. (0.1.0a9)
- [x] Tenant resolution from JWT claim / header / subdomain
- [x] Tenant context propagated to the DB session (transaction-local
      `set_config('jfast.tenant_id', ...)`, `[plugin.database] rls = true`)
- [ ] Per-tenant rate limiting and quotas
- [x] Tests that a query without tenant context returns zero rows
      (`tests/test_rls.py`, against PostgreSQL as a non-superuser role)

**What is still a convention:** a service that does not turn `rls` on, or that
connects as a superuser, has the repository filter and nothing else. Production
refuses the second case; the first is a choice the service makes.

---

## Phase 3 — The generator (partially done)

- [x] Jinja2 templates as real files — linted, diffed, tested
- [x] `jfast new module <name>` — layered layout with tests and a README
- [x] `--layout screaming` — framework-free domain, one file per use case
- [x] `--ui htmx` — overlay composed onto either layout, not duplicated
- [x] `jfast new service <name> --kind api|web` — a whole service from zero
- [x] Separate Jinja environments so HTML templates keep their runtime `{{ }}`
- [x] Pluralised table names, overridable with `--table`
- [x] `.jfast-template` stamp recording framework version and context
- [x] Alembic layout in the generated service (env.py reads the app DSN and
      auto-imports module models; compare_type + compare_server_default on)
- [x] pytest.ini and conftest.py with app/client fixtures
- [x] `jfast init` — interactive installer: kind, frontend, datastores, port
- [x] `jfast new service --with database,cache,qdrant,rag` — plugin list,
      config blocks, .env keys and pinned extras all derived from it
- [x] `--kind spa --frontend vue|react` — Vite + Tailwind v4 project
- [x] `jfast new view` — Modulo<Name> structure with idempotent router and
      sidebar patching
- [ ] A CI workflow in the generated service
- [ ] `jfast upgrade` — re-apply newer templates over an existing tree and show
      a diff. **This is what the CometaX generator never had**, and the reason
      generated services drift.
- [ ] `jfast new plugin <name>` — scaffold a third-party plugin package
- [ ] Angular scaffold (`--frontend angular`), gated on a CI job that runs
      `ng build` — a config nobody has built is worse than no scaffold
- [ ] A Node job in CI that runs `npm install` + `vite build` on the generated
      Vue and React projects. **Until this exists, those scaffolds are
      unverified beyond file rendering.**
- [ ] Dockerfile and compose entry for `--kind spa` projects
- [ ] RAG improvements: structure-aware chunking, reranking, hybrid search

---

## Phase 3b — Workspaces and the gateway (done)

- [x] `jfast.workspace.toml` — services register themselves, take the next free
      ten-port block, and record what the frontend should call
- [x] `jfast workspace init | list | gateway | env`
- [x] `gateway` plugin — prefix routing, hop-by-hop header stripping,
      `X-Request-ID` propagation, 502/504 as problem+json, no catch-all, no
      upstream probing in readiness
- [x] The gateway is generated automatically at the second backend, and not
      before
- [ ] One compose file for the whole workspace, gateway in front, shared network
- [ ] Auth at the gateway (needs the `auth` plugin from phase 1)
- [ ] Rate limiting at the gateway

---

## Phase 4 — Deployment (partially done)

- [x] `Plugin.infra()` — plugins declare their containers
- [x] `jfast deploy compose` — docker-compose derived from the plugin graph
- [x] `jfast deploy dockerfile` — non-root, healthcheck, layer-cached
- [x] `extra_ports` for multi-port containers (Qdrant HTTP + gRPC)
- [ ] `jfast deploy k8s` — Deployment, Service, ConfigMap, Secret, HPA
- [ ] `jfast deploy env` — `.env.example` derived from every plugin's settings
      schema, so config docs cannot go stale
- [x] Port allocation registry — the CometaX 10-port-block scheme without a
      central IAM as a hard dependency (jfast.workspace.toml)
- [ ] GitHub Actions workflow template

---

## Phase 5 — Agent surface (partially done)

What makes "describe an app and get one" real rather than a demo.

- [x] `jfast describe --json` — settings schema, plugin graph, providers, infra
- [x] `jfast doctor` — config resolves, enabled plugins import
- [x] `.jfast/skills/` layout with starter skills
- [x] `AGENTS.md` contract
- [ ] `jfast routes --json` — every mounted route with its schema
- [ ] `jfast skills list` — enumerate skills so an agent can choose one
- [ ] `DESIGN.md` convention wired into the frontend templates
- [x] `build-frontend` skill
- [ ] Recipe skills: `add-auth`, `add-worker`, `add-websocket`

---

## Phase 6 — Hardening (started)

- [x] `mypy --strict` clean across `src/`
- [x] `ruff check` + `ruff format --check` clean
- [x] CI: lint, format, types, tests and two scaffold smoke suites on 3.11–3.13
- [ ] 90% test coverage on the kernel
- [ ] Integration suite against real PostgreSQL and Redis
- [ ] Benchmark: kernel overhead per request vs bare FastAPI (publish the number)
- [ ] Semantic versioning policy and a deprecation window
- [ ] Documentation site

---

## Explicit non-goals

Saying no keeps the kernel small.

- **Not an ORM.** SQLAlchemy is already good.
- **Not a control plane.** JFast services can register with an IAM, but the
  framework must run standalone. The CometaX `ApiIam` coupling is exactly what
  made the previous generation hard to reuse.
- **Not a frontend framework.** Templates for Vue 3, opinions about design
  tokens, nothing more.
- **Not multi-language.** Python only. The PHP/Laravel half of CometaX is a
  separate problem with a separate solution.

---

## Migrating from CometaXMicroservices

1. Pick one generated service — `FrameworkTest` is the natural first victim.
2. Replace its copied `core/` and `config/` with `jfastframework` imports.
3. Move every `os.getenv` into a plugin settings model.
4. Convert each `modules/<name>/` to the JFast module layout.
5. Delete the copied files and pin `jfastframework~=0.1`.
6. Only then migrate the second service.

Do not migrate all of them at once. The first migration is where the framework's
missing pieces surface, and you want to find them with one service at risk.

---

## Phase 3c — Polyglot, queues and the edge (0.4.0)

- [x] `docs/service-contract.md` — what every service must satisfy, in any
      language. The interface; templates are implementation.
- [x] `jfastframework/languages.py` — language registry, toolchain detection
- [x] Go service scaffold, zero third-party dependencies, verified in CI by
      `go vet` + `go test` + `go build` + running the binary and curling it
- [~] gRPC — the `.proto` contract is generated and the port reserved.
      **Gaps:** no stub generation, no server wiring. Both are gated on a CI
      job that round-trips a real call.
- [x] `queue` plugin: PostgreSQL / Redis / RabbitMQ backends, task registry,
      worker with bounded backoff, dead-lettering and draining
- [x] `events` plugin: Kafka publish/subscribe, partition keys, commit after
      handling
- [x] `jfast start` — the opinionated default stack in one command
- [x] `jfast workspace compose` / `jfast workspace caddy`
- [x] Documentation site with versioned publishing and a link/asset checker
- [ ] Integration tests against real RabbitMQ and Kafka containers.
      **Until this exists, those two backends are unverified.**
- [ ] `worker` CLI entry point (`jfast worker run`)
- [x] Outbox pattern: publish an event in the same transaction as the write (0.1.0a9)

## Phase 3d — More languages and frontends (not started)

Every item here is gated on the same rule: **a CI job that builds and runs what
it generates.** That rule is why Go shipped and Angular did not.

- [ ] Angular scaffold, gated on `ng build` in CI
- [ ] React Native scaffold, gated on a Metro bundle in CI
- [ ] Laravel — a `LanguageSpec`, a template tree satisfying the contract, and
      a CI job running `php artisan test`
- [ ] .NET — same shape, gated on `dotnet build` and `dotnet test`
- [ ] Node/TypeScript service, for teams already there

The contract is the extension point. Adding a language is: implement the six
sections of `docs/service-contract.md`, register a `LanguageSpec`, add a
template tree, add the CI job.

---

## Phase 1b — Auth (0.6.0)

- [x] `auth` plugin: JWKS / public key / shared secret verification
- [x] Algorithm pinning, `aud` and `iss` verification, 30s leeway; mixing
      symmetric and asymmetric algorithms is refused at startup
- [x] `require_auth` / `require_scopes` / `require_roles` / `optional_auth`
- [x] JWKS rotation with a rate-limited refresh and cached-key fallback
- [x] Token issuance, refresh rotation with reuse detection, family revocation
- [x] Revocation store: Redis when `cache` is on, in-memory otherwise — and
      the in-memory one reports itself as not shared
- [x] `tenant_id` from a signed claim rather than the `X-Tenant-ID` header
- [x] Social login (0.7.0): Google / Microsoft / GitHub presets, state and
      nonce verified, audience and issuer checked, `@auth.on_identity` as the
      seam where a verified identity becomes your user
- [ ] PKCE, for a public client talking to the provider directly
- [ ] mTLS / SPIFFE identity for service-to-service calls
- [ ] Per-tenant key isolation
- [ ] An `auth` contract rule: "no route without a dependency" as a check

## Phase 4b — Kubernetes (0.6.0)

- [x] `jfast workspace k8s` — kustomize base plus `dev`/`prod` overlays
- [x] `jfast init` asks whether Kubernetes is needed
- [x] Deployment, Service, ConfigMap, HPA, PodDisruptionBudget, Ingress
- [x] Liveness on `/health`, readiness on `/ready`, startup probe; non-root,
      read-only root filesystem, dropped capabilities, `maxUnavailable: 0`
- [x] Secret templates with placeholders; databases deliberately not generated
- [ ] Apply the manifests to a kind cluster in CI. **Until then they are
      structurally asserted, not proven.**
- [ ] NetworkPolicies (default-deny plus explicit allows)
- [ ] ServiceMonitor for the Prometheus Operator
- [ ] A pre-deploy Job for migrations, ordered safely against the rollout

---

## Phase 6 — Storage, tenancy and cloud (0.7.0)

- [x] `storage` plugin: named disks with a visibility, local and S3/MinIO
      drivers behind one protocol
- [x] Key validation in every backend (traversal, absolute paths, backslashes,
      null bytes), plus a post-resolution symlink check on local disks
- [x] HMAC-signed temporary URLs covering key *and* expiry, constant-time
      comparison, one indistinguishable 403 for expired and forged
- [x] `attachment` + `nosniff` on every download, so an uploaded `.html`
      cannot run script on your origin
- [x] MinIO in the generated compose file, opt-in, at port offset `+6`
- [x] `tenancy` plugin: token claim, subdomain, path or header, in that order
      of trust; the middleware runs innermost so the signed claim is readable
- [x] Wildcard Caddy site block with on-demand TLS and the `ask` endpoint that
      gates it
- [x] `load_secrets()` from AWS Secrets Manager or Google Secret Manager, with
      the environment winning over the stored copy
- [x] `jfast deploy function` for AWS Lambda and Cloud Run — private by
      default, and it writes scripts rather than running them
- [x] `notifications` plugin: FCM HTTP v1, with a console backend for
      development
- [x] **PostgreSQL row-level security.** `enable_tenant_rls` in a migration and
      `[plugin.database] rls = true`. (0.1.0a9)
- [ ] Per-tenant storage prefixes applied automatically rather than by
      convention in the key
- [ ] Streaming uploads and downloads. `put()` takes bytes; a large upload
      should presign straight to S3 and never touch the application
- [ ] Virus scanning for public uploads
- [ ] An FCM send verified against a real project in CI. The payload
      construction is tested; delivery is not
- [ ] `jfast deploy function` applied in CI against a real account
