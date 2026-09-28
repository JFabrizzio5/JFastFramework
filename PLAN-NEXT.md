# JFastFramework — the road from 0.1.0a1 to 1.0

A proposal. Each step below is written to be pasted into [PLAN.md](PLAN.md) as a
phase once accepted. Supersedes the earlier `PLAN-0.8.md`.

Legend: `[x]` done · `[~]` partial, gaps named · `[ ]` not started

Two rules order everything here:

1. **Dependency, not ambition.** A step lands only when what it needs already
   exists.
2. **Anything that changes a committed file format lands before the first PyPI
   release.** After publication the same change costs a deprecation window and
   every user's workspace.

A third rule governs claims, taken from [STATUS.md](STATUS.md): a subsystem is
promoted by a CI job that exercises it against the real dependency, never by
feeling finished.

---

## The finding that shapes everything

`jfast.workspace.toml` records `datastores = ["database", "cache"]` on a
service: a list of *types*, not of *instances*. Two consequences:

1. `deploy/workspace.py` emits a `billing-database` container and **nothing
   writes the `JFAST_DB_DSN` that points at it.** The compose file falls back to
   `env_file: ./billing/.env`, maintained by a human. The README's claim that
   infrastructure is derived from the plugin graph is true for *containers* and
   false for *connection strings*. That gap is where drift lives.
2. A second PostgreSQL cannot be expressed. `DatabasePlugin` provides
   `db.engine`, the registry rejects duplicate providers, and the workspace has
   no name to hang a second instance on.

Generated `.env` files, "which service talks to which database", local
monitoring, load balancing and the service-to-service client are not five
features. They are five views of one missing data structure. Step 2 builds it;
steps 3, 5, 6 and 7 read from it.

---

## Step 0 — Honesty (done)

- [x] Version reset `0.7.0` → `0.1.0a1`. Never published, so the renumbering is
      free today and impossible later. PEP 440 makes the packaging tool enforce
      the warning: `pip install jfastframework` will not resolve a pre-release
      without `--pre`.
- [x] Version single-sourced from `__init__.py` through `[tool.hatch.version]`.
      Two hand-edited version strings drift.
- [x] `Development Status :: 2 - Pre-Alpha`.
- [x] [STATUS.md](STATUS.md) — maturity per subsystem, with a promotion gate.
      One global number could not say "the kernel is tested and the Kafka client
      has never seen a broker" at the same time.
- [x] Renumbering rationale in the changelog; history kept verbatim.
- [x] Deleted the docstring claiming an internal HTTP client that does not exist.
- [x] Corrected the stale test count in the README (196 → 326).

---

## Step 0b — Event-loop safety (done)

The bug that never raises: a blocking call inside ``async def`` stalls every
other request on the worker, and the latency lands on endpoints that have
nothing to do with the cause.

- [x] Ruff's `ASYNC` ruleset enabled for the framework. `ASYNC109` ignored with
      a reason -- it wants a cancel scope instead of a `timeout` parameter, and
      `dequeue(timeout=...)` maps onto a broker primitive.
- [x] `async-blocking` contract rule, covering the three things a
      general-purpose linter cannot know: a synchronous client stored on
      `self`, one hop into a synchronous helper in the same file, and the
      functions only this team knows about (`[rules.async_safety]`).
- [x] Correct offloading (`asyncio.to_thread`, `run_in_executor`,
      `anyio.to_thread.run_sync`, `run_in_threadpool`) recognised and left
      alone, including the synchronous closure handed to it -- the shape
      `storage/s3.py` already uses.
- [x] Nineteen tests, most of them negative: the false-positive cases are what
      decides whether a rule survives its first month.
- [x] Two cases in `smoke_contracts.sh`: a generated service is made to block
      and the build fails; the same code offloaded correctly passes.
- [x] Fixed what it found in this repository: the `web` plugin ran two blocking
      `Path.is_dir()` calls on the event loop, once per readiness probe per
      replica.

---

## Step 1 — Correctness (0.1.0a2, done)

Bugs, not roadmap. Cheap, and they buy the credibility the rest of the plan
spends.

- [x] **Redis queue has no visibility timeout.** `queues/redis.py:44` assigns
      `self._visibility` and never reads it. Recovery is `_recover_own()`, which
      drains only `jfast:jobs:processing:{hostname}:{id(self)}` — and `id(self)`
      is a memory address that changes every start, as does a pod name under a
      Deployment. A worker that dies leaves claimed jobs in a list nothing will
      ever read again, while `queues/base.py` documents the visibility timeout as
      a guarantee *every* backend provides.
      Fix: stable consumer id (`JFAST_WORKER_ID` → hostname, never `id(self)`), a
      `jfast:jobs:workers` hash of consumer → last-seen, a heartbeat on dequeue
      and ack, and a reaper returning the processing list of any consumer whose
      heartbeat is older than the visibility timeout.
      **Gated on** an integration job against a real Redis container.
- [x] **`/ready` has no timeout and runs serially** (`health.py`). A dependency
      that hangs at the TCP level — not refused, hung — blocks the probe until
      the socket gives up. Fix: `asyncio.gather` with a per-check
      `asyncio.timeout(readiness_timeout)`, and `timeout` reported as a status
      distinct from `fail`.
- [x] **`BaseRepository.paginate()` emits no `ORDER BY`** (`db/repository.py:71`).
      `LIMIT`/`OFFSET` without an order does not give stable pages in PostgreSQL:
      rows repeat and rows are skipped. Default to the primary key, allow an
      override.
- [x] **The tenant filter fails open** (`db/repository.py:47`): a model without
      the column silently returns every row. Replace with an explicit
      `tenant_scoped: ClassVar[bool]` that raises when the column is missing.
      Until RLS exists this is the whole isolation story and it must not no-op.
- [x] Delete the unused `visibility_timeout` argument from
      `queues/rabbitmq.py:41` — the broker covers redelivery there, and a
      parameter that does nothing is a promise the caller believes.
- [x] **Kernel HTTP hardening:** CORS, body-size limit, request timeout,
      `TrustedHostMiddleware`. Caddy covers some of it when it is in front;
      `jfast deploy function` puts a service on Lambda with nothing in front.
- [x] **`/docs` and `/openapi.json` stay open in production** while `/info`
      correctly closes (`settings.py:49` vs `health.py:70`). One rule, applied
      once.
- [x] `pip-audit` and `bandit` in CI. A framework shipping opinionated auth and
      storage defaults should audit its own tree.

**Exit:** suite green plus a Redis integration job, and no shipped docstring
describes something that is not there.

---

## Step 2 — The resource graph (0.2.0, done)

The keystone. Everything from here reads this structure.

- [x] **`[[workspace.resources]]`** — instances owned by the workspace, not by a
      service:

      ```toml
      [[workspace.resources]]
      name = "core-db"
      type = "postgres"
      image = "pgvector/pgvector:pg16"
      port = 5433
      database = "core"

      [[workspace.resources]]
      name = "analytics-db"
      type = "postgres"
      port = 5434

      [[workspace.resources]]
      name = "shared-redis"
      type = "redis"
      port = 6379
      ```

- [x] **`uses` on a service** — the binding and the variable it produces:

      ```toml
      [[workspace.services]]
      name = "billing"
      uses = [
        { resource = "core-db",      as = "JFAST_DB_DSN" },
        { resource = "shared-redis", as = "JFAST_CACHE_URL" },
      ]
      ```

      `as` defaults from the resource type, so the common case stays one word.

- [x] `jfast resource add <name> --type postgres` — allocates a port, attached to
      nothing yet.
- [x] `jfast link billing core-db` / `jfast unlink` — edits the workspace file,
      revalidates ports, regenerates `.env` and compose. This is the answer to
      "I want a second database — which service talks to it?"
- [x] **Everything downstream derives:** one container per *resource* (today one
      per service, so sharing is impossible), the DSN written into each service's
      `.env`, `depends_on` edges, Kubernetes ConfigMap and Secret references.
- [x] **One credential per resource.** `POSTGRES_PASSWORD` is currently a single
      workspace-wide variable shared by every generated PostgreSQL container.
      Generate one secret per resource into a gitignored workspace `.env`,
      referenced by the container and by every DSN bound to it.
- [x] **Backwards compatibility:** a service with the old `datastores = [...]`
      and no `uses` gets implicit resources named `<service>-<type>`, producing
      byte-identical output to today. `jfast workspace migrate-resources`
      rewrites it explicitly and prints the diff.
- [x] `jfast workspace validate` — a resource nobody uses, a binding to a
      resource that does not exist, two resources on one port, a service bound to
      a datastore whose plugin it does not enable. Non-zero exit.

**Exit:** a workspace with two services and three resources, one of them shared,
boots with `jfast workspace up`, and both services report `/ready: ok` without
anybody opening a `.env`.

**Cost accepted:** the workspace file format changes. Free now, expensive after
publication. This is the concrete reason not to publish yet.

---

## Step 3 — Config derived, and seen (0.2.0)

`.env.example` is hand-written and already drifts from the settings models.
Config documentation maintained by hand is documentation that is wrong.

- [ ] **`Plugin.env_schema()`**, derived from the pydantic Settings model:
      variable name, default, required, `SecretStr` or not, description. No new
      hand-kept list. Preparatory work: add `description=` to the settings fields
      that lack one — mechanical, and it doubles as the docs source.
- [ ] `jfast env example` — writes `.env.example` from the enabled plugin graph
      plus the resource bindings from Step 2. Secrets stay placeholders and are
      never generated into a committed file.
- [ ] `jfast env check` — missing, unknown, still-`CHANGEME`, secret-shaped value
      in a tracked file. Non-zero exit, CI-usable.
- [ ] `jfast env diff` — what changed after enabling a plugin or adding a
      resource: "these three variables are new, this one is now required."
- [ ] The framework's own `.env.example` becomes generated; CI fails on drift.
- [ ] `jfast doctor` absorbs `env check`.
- [ ] **`jfast status [--json]`** — service, port, container, `/health`,
      per-check `/ready` detail, bound resources, queue depth.
- [ ] **`jfast workspace graph --format mermaid|dot`** — services, gateway,
      resources, edges labelled with the variable that carries the binding.
      Committed to the workspace README, it stops being folklore.
- [ ] **`jfast dev`** — one supervisor: resource containers up, every Python
      service under `uvicorn --reload`, one prefixed multiplexed log stream,
      restart on crash. A development tool, and the docs say so.

**Exit:** adding `--with storage` regenerates the example, and `jfast status`
names the red service and what it is connected to without opening `docker ps`.

---

## Step 4 — Shared code that does not become a distributed monolith (0.3.0)

The workspace has N services and no way to share a type, so people copy-paste —
the exact failure the README argues against. Two scopes, two mechanisms.

- [ ] **Inside one service: `shared/`.** `jfast new shared <name>` creates a
      shared kernel next to the modules, plus contract rules that make the
      direction of dependency enforceable: modules may import `shared/`,
      `shared/` may not import a module. Cheap, and it is the classic fix for
      "this logic is repeated in three modules".
- [ ] **Across services: `packages/`.** `jfast new package shared-domain`
      creates a real installable package in the workspace, with its own
      pyproject, tests and `contracts.toml`. Services declare
      `packages = ["shared-domain"]`; the generator adds a path dependency and
      the Dockerfile learns a build context that includes it (the fiddly part —
      the build moves to the workspace root with a per-service Dockerfile).
- [ ] **The rule that keeps this from rotting, enforced by a contract:** share
      types and pure logic, never share the database. A shared package
      `forbid_packages = ["sqlalchemy", "fastapi"]` by default. Two services
      reaching into one schema is how microservices become a distributed
      monolith with extra latency.
- [ ] **Trade-off, stated in the docs:** in-workspace packages are path
      dependencies. You get atomic cross-service changes and no version skew;
      you lose independent deployability of the shared code. That is the
      monorepo bargain, and it is the right one at this size. Publishing shared
      packages to a private index is deliberately out of scope.
- [ ] **`internal_client`.** A microservice framework without a
      service-to-service client. Mandatory timeouts, retry with backoff and
      jitter on idempotent methods only, a circuit breaker, propagation of
      `X-Request-ID`, tenant and trace context — and destination resolution
      **from the graph**, so `client.for_service("catalog")` knows the URL
      because Step 2 recorded it. A generated typed client for a service is the
      first real `packages/` citizen.

---

## Step 5 — Data contracts, including the ones a schema cannot hold (0.3.0)

Extends the existing `contracts/` subsystem rather than inventing a parallel
one. The premise stays the same: a rule nothing checks is a suggestion.

- [ ] **`DataContract`** — one declaration, several outputs: the pydantic model,
      a JSON Schema committed under `contracts/data/<name>.schema.json`, an
      OpenAPI component, and TypeScript types.
- [ ] **`jfast contracts check` learns data contracts.** Does the model still
      match the committed schema? A removed field or a narrowed type fails CI
      unless a migration is declared next to it. This is the thing that makes a
      document store safe to refactor.
- [ ] **Mongo gets server-side enforcement.** MongoDB supports `$jsonSchema`
      collection validators and almost nobody uses them. `jfast mongo
      sync-validators` applies the generated schema to the collection, so the
      contract is enforced by the database rather than by everyone remembering.
      That converts "a JSON contract of how it should look" into a rule the
      server refuses to break.
- [ ] **`jfast mongo migrate`** — versioned scripts plus a `_jfast_migrations`
      collection. Document shapes change and need backfills; pretending
      schemaless means migration-less is how a collection ends up holding four
      generations of one document.
- [ ] **`MongoRepository`** mirroring `BaseRepository` — get, find, paginate,
      tenant scoping — so the call sites do not change when the store does.
- [ ] **`jfast contracts ts --out frontend/src/types`** — the generated SPA gets
      types from the same contract the API validates against, checked in CI.
      Backend and frontend disagreeing about a field is the most common bug in
      this shape of project, and it becomes a build failure.

---

## Step 4b — Seeing the system (0.3.0)

Diagrams generated from the code, committed as text, and checked for drift.

**The decision that makes this work: generate text, not images.** A committed
PNG is a binary blob nobody can review and that goes stale in silence. Mermaid
renders on GitHub, in the docs site and in most editors, diffs line by line,
and rasterises to SVG or PNG on demand when a slide needs one.

- [ ] **`jfast diagram db`** -- a Mermaid `erDiagram` from the SQLAlchemy
      metadata. The machinery already exists: `migrations/env.py` imports every
      module's models so autogenerate cannot miss one, and the same import gives
      the full table graph.
- [ ] **Three things worth marking on it**, because they are the questions
      people actually ask: which tables carry `tenant_id` (and therefore which
      do not), which foreign keys cross a module boundary (those are the seams a
      service split would cut), and which tables no module owns.
- [ ] **Per-module views plus a whole-system view.** One diagram of sixty tables
      is wall art. The per-module diagram is the one anybody reads.
- [ ] **`jfast diagram modules`** -- the import graph between modules, drawn
      from the same layer definitions the contract checks. Cycles are the point:
      they are invisible in a file tree and obvious in a graph.
- [ ] **`jfast diagram layers`** -- the declared layers with the edges that
      actually exist, violations in red. The picture of the contract.
- [ ] **`jfast diagram workspace`** -- services, resources, gateway, edges
      labelled with the variable that carries the binding. Reads the graph from
      Step 2.
- [ ] **`jfast diagram classes <path>`** -- and only with a path.
      **Trade-off, stated:** a class diagram of a whole codebase is decoration;
      nobody reads it and nobody notices when it rots. Scoped to one module it
      is a real answer to "what shape is this".
- [ ] **`--check` on all of them**, so a committed diagram that no longer
      matches the code fails the build. The same trick as `.env.example`
      drift, and it is what stops a diagram from becoming a lie with a
      timestamp.
- [ ] Mongo collections join the ER view once the data contracts of Step 5
      exist -- the JSON Schema is the shape, so it can be drawn.

---

## Step 5b — Use cases as contracts (0.3.0)

The `screaming` layout already puts one file per use case on disk. Nothing
declares what those use cases *are*, so the answer to "what does this service
do" still lives in somebody's head.

- [ ] **`[[use_cases]]` in `contracts.toml`** -- name, one line of intent, the
      actor, the interface it is reached through (route, job, event, CLI), the
      invariants it must uphold, and a status of `designed`, `implemented` or
      `deprecated`.
- [ ] **Checked, not decorative:**
      a use case marked `implemented` whose file is absent is a violation;
      one with no test that names it is a violation;
      one whose declared route is not in `jfast routes --json` is a violation.
      A use case marked `designed` is none of those things -- it is a plan, and
      the contract is where a plan is allowed to live.
- [ ] **`jfast usecases show --json`** for an agent, `USECASES.md` for a human,
      both generated.
- [ ] **A flow diagram per use case** where the steps are declared, joining
      Step 4b.

This is the piece that turns "what does this service do" from tribal knowledge
into something a build can fail on.

---

## Step 5c — The context handoff (0.3.0)

The idea underneath the previous two steps, and the most valuable thing in this
project: **the developer talks to the team, and to the next agent, through
artifacts that are checked** -- not through a wiki page written once.

- [ ] **`jfast context --json` / `--md`** -- one assembled briefing: the
      contract, the use cases, the schema, the workspace graph, provided and
      consumed interfaces, open waivers, the decision log, and the current
      maturity of each part. An agent reads the JSON before writing a line; a
      new teammate reads the Markdown on day one. Neither is hand-maintained,
      and CI fails when it drifts.
- [ ] **Waivers are already context and are currently discarded.** `jfast
      contracts waivers` lists them; they should carry a date and an author and
      appear in the briefing. A waiver is a decision taken under pressure, and
      it is exactly the thing the next person needs to know.
- [ ] **`jfast decide "<title>"`** -- writes an ADR with a stable id under
      `decisions/`. A code comment `# see: ADR-014` is then checkable: the
      checker verifies the record exists, so a reference cannot rot into a dead
      pointer.
- [ ] **`.jfast/skills/` learns from the contract.** A skill that says "add a
      module" should read the layer rules rather than restate them, so the two
      cannot disagree.

**Why this ordering:** contracts already exist and are enforced. Use cases,
diagrams, decisions and the assembled briefing are the rest of the same idea,
and they are worth more than another plugin.

---

## Step 6 — Realtime (0.4.0)

"Everything works" for websockets means one specific thing: a message published
by one replica reaches a client connected to another.

- [ ] **`websockets` plugin** — connection manager with rooms, Redis pub/sub
      backplane. Because the state lives in Redis, **no sticky sessions are
      required at the load balancer**, which is the whole design win and belongs
      in the docs.
- [ ] **Auth reuses the `auth` plugin.** Token in `Sec-WebSocket-Protocol` or a
      first-message handshake — never a query parameter, which lands in every
      access log and proxy trace.
- [ ] **Tenancy is enforced on channel names**, so a client cannot subscribe
      across tenants.
- [ ] **Backpressure is a decision, not an accident:** a bounded per-connection
      send queue with a stated policy when it fills. Plus ping/pong and an idle
      timeout.
- [ ] **`sse` helper** alongside it. Server-to-client only, no upgrade, works
      through every proxy, pairs naturally with the HTMX story. Most features
      people build with websockets are one-way and should use this.
- [ ] Gateway and Caddy pass the upgrade through; Kubernetes Ingress timeouts
      documented.
- [ ] **Verification gate:** a CI job with two replicas and a real Redis,
      asserting a message published on replica A reaches a client on replica B.
      Without that test the plugin is a claim, and this repository does not ship
      claims.

---

## Step 7 — Replicas and load balancing (0.4.0)

- [ ] **`replicas = 3` on a service entry** — one number, three outputs: compose
      `deploy.replicas`, multiple Caddy upstreams, Kubernetes `replicas` and HPA
      bounds.
- [ ] **Gateway upstream pools** — a prefix routes to a list, with a strategy,
      passive health checking (eject after N consecutive 5xx, re-probe) and
      retry on a *different* upstream for idempotent methods only.
- [ ] **Recommendation, stated with its trade-off:** do not build a sophisticated
      balancer in Python. Caddy already has load-balancing policies and active
      plus passive health checks; generating its config correctly from the graph
      is most of the value at a fraction of the risk. The Python gateway keeps
      the application concerns — auth, tenant, rate limit — where it has an
      advantage. Kubernetes replaces both with a Service and needs neither.
- [ ] **`ratelimit` plugin** — token bucket in Redis, keyed by tenant, IP or API
      key, usable on a route and at the gateway. The behaviour when Redis is down
      is fail-open, chosen and documented rather than discovered.
- [ ] **`tracing` plugin (OpenTelemetry).** The third signal. With a gateway, N
      backends, replicas and a queue, `request_id` gives you grep but never tells
      you where the 800ms went. The propagation path already exists; the trace
      context rides it. Opt-in, extra `[otel]`, spans around queue handlers too.

---

## Step 8 — Dependencies, and the capability catalog (0.5.0)

These two belong together: a curated catalog is only trustworthy if something
tests the combinations.

### Versioning

- [ ] **`docs/versioning.md`:** a library must not pin exact versions — its
      dependents cannot resolve around it. Reproducibility comes from a lock used
      by CI, never from published metadata.
- [ ] **Upper bounds only where the ecosystem earns them.** FastAPI, Starlette
      and pydantic-settings move without a SemVer promise: cap them. SQLAlchemy
      promises major-version stability: `>=2.0,<3`. Document the reason beside
      each cap so the list does not become cargo cult.
- [ ] **Commit `uv.lock`.** Cost: `uv` joins the toolchain. Runtime dependencies
      are untouched.
- [ ] **Three resolutions in CI, not one:**
      - `pinned` — from the lock. Must always be green.
      - `lowest-direct` — proves the declared floors are real. `fastapi>=0.115`
        is currently a guess nobody has ever executed.
      - `latest` — nightly, unconstrained, opens an issue on failure. This is the
        early warning, and it fires before a user's build does.
- [ ] **`COMPAT.md`, generated per release** — the exact resolved versions each
      version was tested against. Diff two rows and you know what moved.
- [ ] **Generated services get a tested set:** ship `constraints.txt` with each
      release; the generated `requirements.txt` pins the framework exactly and
      references it. A service scaffolded today and installed in six months
      resolves to what was tested, not to whatever shipped meanwhile.
- [ ] `jfast doctor --deps` names the package that drifted from the tested set.
- [ ] Renovate or Dependabot, grouped weekly.

### The catalog

- [ ] **`jfast add <capability>`** — `dataframes`, `vision`, `ml`, `excel`,
      `pdf`, `scraping`. One command resolves the extra, pins it inside the
      tested constraint set, enables any matching plugin, updates
      `.env.example`, and prints exactly what changed.
- [ ] **Not installed by default, and here is why.** pandas, OpenCV and their
      transitive numpy/ABI stack would add hundreds of megabytes to a service
      that serves JSON, and would make the plugin graph — the thing that makes
      this framework legible — stop describing what the service actually needs.
      Laravel is not robust because it bundles image processing; it is robust
      because `composer require` pulls a first-party package that the framework
      version-tests. That is the model worth copying.
- [ ] **Opinions the catalog carries, so users do not have to research them:**
      polars as the default dataframe (lazy execution, no index, lower memory)
      with pandas offered for ecosystem reasons; `opencv-python-headless` rather
      than `opencv-python`, because the GUI build drags X11 in and breaks inside
      a container — exactly the trap a framework should absorb once.
- [ ] **Heavy dependencies get their own Docker layer** (`requirements-heavy.txt`
      earlier in the file) so an application change does not rebuild numpy.
- [ ] Heavy extras are tested in a separate nightly matrix; the main CI stays
      fast.

---

## Step 9 — The parts that make it feel like a framework (0.5.0)

Laravel-shaped robustness is not bundled libraries. It is conventions strong
enough that you stop deciding, one CLI that does everything, and first-party
packages tested together. Two of those are covered above; this is the CLI.

- [ ] **`jfast shell`** — a REPL with the app booted: plugins started, a session
      open, models imported. Tinker is one of the most-used commands in Laravel
      and this repository has no equivalent. Cheap, high value.
- [ ] **`jfast routes [--json]`** — every mounted route with its schema, auth
      dependency and tags. Already on the roadmap for agents; it is equally for
      humans.
- [ ] **`jfast db seed` plus model factories** — deterministic seed data and
      factories the test fixtures can use. Pairs with what `testing/fixtures.py`
      already provides.
- [ ] **`jfast worker run` and `jfast schedule run`** — the queue and scheduler
      need entry points, not a hand-written `__main__`.
- [ ] **`scheduler`** — periodic tasks with leader election through a Redis or
      PostgreSQL lock, so N replicas do not each fire the same cron. Reuses
      `TaskRegistry`. Delayed jobs exist; recurring ones do not, and every SaaS
      needs "charge subscriptions daily".
- [ ] **`jfast monitor`** — the dev inspector: the graph rendered, health,
      recent requests, slow queries, job outcomes. Telescope, scoped to
      development.
- [ ] **Outbox** — `publish_in_transaction()` plus a relay, so `events` cannot
      commit a row and lose the event. The PostgreSQL queue backend already
      avoids this by construction; Kafka needs it explicitly. The PostgreSQL
      queue does *not* avoid the other half: `enqueue` commits in its own
      transaction, apart from the request's rows, so it needs
      `enqueue(job, session=...)` writing through the request's session.
- [ ] **Idempotency keys** — an `Idempotency-Key` dependency backed by a table
      `(tenant_id, key, request_hash, status, response)`, inserted with
      `ON CONFLICT` in the request's own transaction. The same key with the
      same body replays the stored response; with a different body, 422. What
      a client that retries a `POST` after a timeout needs when there is no
      natural unique key to stop the second row.

---

## Step 10 — Agents and MCP (0.6.0)

The repository already argues that AI agents are a first-class audience. MCP is
the protocol that argument has been waiting for.

- [ ] **`mcp` plugin** — exposes the service over MCP (streamable HTTP). Tools
      come from routes that opt in with a decorator, or from a tag. Auth reuses
      the `auth` plugin, mapping scopes to tool permissions, so an agent gets
      exactly the surface a user of that token would get.
- [ ] **`jfast new service --kind mcp`** — a service whose whole job is being an
      MCP server, satisfying the same service contract as every other kind.
- [ ] **The framework's own agent surface becomes MCP resources:**
      `jfast describe --json`, `contracts show --json`, `workspace list --json`,
      the OpenAPI document. An agent then discovers the system instead of being
      told about it.
- [ ] **Marked `experimental`, and gated** on a CI job that starts the server and
      performs a real `tools/list` and `tools/call` round-trip. The spec moves
      quickly; the SDK gets pinned and the level in STATUS.md stays honest.

---

## Step 11 — Hardening and publication (0.7.0 → 1.0.0)

- [ ] **PostgreSQL row-level security.** The single most important gap on the
      original plan and still true: until it exists, tenancy is a convention the
      repository enforces, not isolation the database enforces.
- [ ] Integration suites against real PostgreSQL, Redis, RabbitMQ, Kafka, MinIO —
      promoting five subsystems out of `unverified` in STATUS.md.
- [ ] Apply the Kubernetes manifests to a kind cluster in CI. NetworkPolicies,
      ServiceMonitor, a pre-deploy migration Job ordered against the rollout.
- [ ] Coverage measured, then gated at the number already reached, then raised.
- [ ] Kernel overhead benchmark against bare FastAPI, published.
- [ ] `jfast upgrade` with a three-way diff. Without it, "a versioned runtime
      rather than a generator" covers only the imported half and the generated
      half drifts exactly the way the README says generators do.
- [ ] `release.yml`: PyPI trusted publishing on a tag after the full matrix,
      TestPyPI dry run first.
- [ ] Deprecation-window policy.
- [ ] A comparison section in the README. It explains the problem well and never
      answers "why not FastAPI plus cookiecutter, or Litestar" — the first
      question anyone evaluating it asks.

---

## The shape of it

| Release | Contents | Gate |
| --- | --- | --- |
| `0.1.0a1` | Step 0 — honesty | done |
| `0.1.0a1` | Step 0b — event-loop safety | done: the rule caught two real blocking calls here |
| `0.1.0a2` | Step 1 — bugs, edge protections, security gates | done: 380 tests, bandit and pip-audit green |
| `0.2.0` | Step 2 — the resource graph | done: two databases, a shared cache, and generated DSNs, all gated in CI |
| `0.2.x` | Step 3 — env from the schema, `status`, `dev` | `.env.example` regenerates itself and CI fails on drift |
| `0.3.0` | Steps 4–5c — shared layers, internal client, data contracts, diagrams, use cases, context handoff | a stale diagram and an undeclared use case both fail the build |
| `0.4.0` | Steps 6–7 — websockets and SSE, replicas, rate limit, tracing | cross-replica message delivery proven in CI |
| `0.5.0` | Steps 8–9 — versioning, `jfast add`, shell/seed/scheduler/monitor | nightly `latest` job is what discovers the next FastAPI break |
| `0.6.0` | Step 10 — MCP | a real `tools/call` round-trip in CI |
| `0.7.0` | Step 11 — RLS, integration suites, kind cluster | five subsystems leave `unverified` |
| `1.0.0` | publish | STATUS.md has no `broken` and no `unverified` row |

By `0.7.0` the number means what it originally claimed. That is the point of
having reset it.

**The main risk, named:** Step 2 changes `jfast.workspace.toml`. Before the
first PyPI release that costs one migration command run on your own machines.
After it, a deprecation window and every user's workspace. That is the argument
for not publishing yet — which is the call already made.
