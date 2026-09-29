# Subsystem maturity

One version number cannot describe this repository honestly. The kernel is
tested, typed and exercised by CI on every push; the Kafka client has never
spoken to a broker. Calling both "0.7.0" told a reader nothing, which is why
the package restarts at `0.1.0a1` and why maturity is tracked here, per
subsystem, instead.

## Levels

| Level | Means |
| --- | --- |
| `beta` | Exercised by CI against the real thing. API may still change, behaviour is known. |
| `alpha` | Tested in isolation, used by the author, not proven against production traffic. |
| `experimental` | Shipped to get feedback. Expect the API to move. |
| `unverified` | Written against documented APIs. **Never executed against the real dependency.** |
| `broken` | A known defect, named below. Do not build on it yet. |

**Promotion gate:** a subsystem does not move up a level because it feels more
finished. It moves when a CI job exercises it against the real dependency —
a container, a broker, a cluster. That is the same rule that let the Go
scaffold ship and kept the Angular one out.

## Kernel

| Subsystem | Level | Notes |
| --- | --- | --- |
| `create_app`, plugin registry, `AppContext` | `beta` | Full test suite, `mypy --strict`, entry-point discovery and cycle detection covered. |
| Settings and `jfast.toml` loading | `beta` | Typed, env-overridable. CORS, trusted hosts, body-size limit and request timeout are settings; all off unless configured. |
| RFC 7807 error model | `beta` | Handlers for domain, HTTP, validation and unhandled errors. |
| `/health`, `/ready`, `/info` | `beta` | Readiness runs every check concurrently under a timeout, and reports `timeout` apart from `fail`. `/docs` and `/openapi.json` close in production with `/info`. |
| Test fixtures | `beta` | `build_test_app`, `client_for`, `NullPlugin`. |

## Plugins

| Plugin | Level | Notes |
| --- | --- | --- |
| `observability` | `beta` | JSON logs, request-id and tenant correlation. Zero dependencies. |
| `metrics` | `beta` | Route-template labels, so path parameters cannot explode cardinality. |
| `contracts` (checker) | `beta` | Runs in CI against a generated service; a violation fails the build. Layers, forbidden calls and imports, required structure, and event-loop blocking. |
| `database` | `alpha` | Named connections, a read/write split with primary pinning, keyset and count-free pagination, per-tenant engines behind a bounded LRU, and a tenant filter that raises rather than failing open. The pinning and the pools are exercised against a real PostgreSQL, with the replica simulated as a second database that is deliberately behind — that reproduces lag, which is what breaks read-after-write, and **not** streaming replication or failover. No physical standby has ever been tested. Every pool number is per process and the image runs one worker per CPU: through `0.1.0a7` the defaults were 30 connections × 8 workers = 240 against a PostgreSQL that accepts 100, which nothing had multiplied. The default is 5 + 5 now and `jfast check` does the arithmetic against `server_max_connections`. The tenant LRU was probed under pressure: it holds its ceiling, never disposes an engine a request is holding, and raises rather than opening one past it. Through `0.1.0a8` the request session committed after the response was sent, so a failed commit answered 2xx; it commits before now, and the plugin refuses to start on a route that would not -- a service's own session dependency included, once marked `@transactional`; unmarked ones that commit after their `yield` are named in a warning. Constraint violations and stale versions are 409/412, and the races -- twenty concurrent inserts against a unique constraint, a counter under `FOR UPDATE` and under an advisory lock, a real serialisation failure retried to success -- run against PostgreSQL in `tests/test_transactions.py`. |
| `cache` | `alpha` | Redis facade and health check. Not run against a real Redis in CI. |
| `auth` | `alpha` | Verification, JWKS rotation, refresh reuse detection and the refused-attack defaults are tested. Reuse detection was tested under a single-session assumption until `0.1.0a4` — the test asserted `is_family_revoked(subject)`, which is the bug written as an expectation — so a logout revoked every session of that person and poisoned their next login. Families are per session now, and two-session cases are covered. Until `0.1.0a8` all of that was per *process*: without the `cache` plugin the store is in memory, the generated image runs one worker per CPU, and a logout revoked on one worker while the token kept working on the others — with refreshes sent to any other worker refused as revoked, a revocation that never happened. Production refuses to start in that configuration now, `jfast check` reports it at any environment, and both failures are reproduced in `tests/test_session_store.py` before the guard is asserted. Verification itself is exercised against a battery: a refresh token used as a bearer, a wrong signing key, `alg=none`, an expired token and a foreign audience are each refused. Still no PKCE, no mTLS, and no real identity provider in CI. |
| `accounts` | `alpha` | Users, argon2id password login, lockout after repeated failures, roles and permissions carried as token scopes, per-tenant administration, and `on_refresh` so a removed permission or a deactivated account ends at the next refresh. Tested through HTTP on both SQLite and PostgreSQL in `tests/test_accounts.py`. No email verification, password reset or MFA yet. |
| `tenancy` | `alpha` | Resolution order is correct and tested. On its own the repository filter is a **convention**: a raw `session.execute` bypasses it. With `[plugin.database] rls = true` and `enable_tenant_rls` in a migration it is enforced by PostgreSQL row-level security: every transaction sets its tenant with a transaction-local `set_config`, and a query with no tenant sees no rows. Verified against PostgreSQL as a non-superuser role in `tests/test_rls.py`, including that the tenant does not survive into the next transaction on a pooled connection. Production refuses RLS under a superuser or `BYPASSRLS` role, where every policy is ignored. Policies on more than the tenant -- `transaction_setting` plus `enable_rls_policy` -- are verified the same way, with a tenant, its companies and a user who sees some of them. Not yet exercised behind PgBouncer. |
| `storage` (local disk) | `alpha` | Key validation, symlink check, atomic writes, signed URLs, and an upload pipeline whose `validate` step sniffs the content type from the bytes rather than the filename — XML, JSON, CSV and text by parsing, with `DOCTYPE`/`ENTITY` refused — all tested. `put_stream` and `guard_stream` write uploads that do not fit in memory, validated on the first 64 KiB and capped as they arrive. Disk-independent key resolution and copy-on-read are off by default; the shipped ledger is in-memory and documented as dev-only. |
| `storage` (S3 / MinIO) | `unverified` | Never run against a real S3 or MinIO endpoint. The multipart `put_stream`, including the abort on failure, is tested against an in-memory double only. |
| `encryption` | `alpha` | AES-256-GCM with authenticated context and key rotation, on a column or by hand. Round trip, tampering, a value moved to another context, rotation and the column type are tested; key storage is the service's, through the environment or `load_secrets`. |
| `web` (Jinja + HTMX) | `alpha` | Rendered and smoke-tested; no browser-level test. |
| `gateway` | `alpha` | Prefix routing, header stripping, problem+json errors. One upstream per prefix: no pool and no load balancing. Rate limiting is the `ratelimit` plugin below; its README promised one for a year before it existed. |
| `ratelimit` | `alpha` | Token bucket in a Lua script, so the read/decide/write is indivisible; the concurrency assertion was checked against a naive Python read-then-write first, where all 20 requests passed a limit of 5. Runs against a real Redis. Fails open by design, logging and reporting `/ready` degraded rather than turning a cache outage into a total one. Enforced as a FastAPI dependency, so a request matching no route is not limited — that is the edge's job. Not `beta` until CI has run it. |
| `queue` (worker, every backend) | `alpha` | A job takes the queuing request's tenant and request id, and the worker restores both around the handler; through `0.1.0a8` neither was ever filled, so every job ran with no tenant. `enqueue` still commits in its own transaction, apart from the request's rows. Covered by `tests/test_transactions.py`. |
| `queue` (PostgreSQL) | `alpha` | `FOR UPDATE SKIP LOCKED`, correct reclaim of jobs from a dead worker. Nothing extends the claim while a handler runs, so the handler has to finish inside `visibility_timeout` — through `0.1.0a7` the worker's `job_timeout` defaulted to the same 300 seconds from a different file, which raced at the boundary and ran the job twice past it. The worker derives its ceiling from the backend now and refuses a longer one. Delivery is at-least-once by design: `ack` follows the handler, so a crash mid-job redelivers, and handlers must be idempotent. Not run against a real PostgreSQL in CI. |
| `queue` (Redis) | `alpha` | Visibility timeout implemented: workers heartbeat into a registry on server time and any worker reclaims a dead peer's in-flight jobs. Tested against an in-memory double of the Redis commands, not yet against a real server. |
| `queue` (RabbitMQ) | `alpha` | Delays and retry backoff are held by the broker in a cascade of fixed-TTL queues, so a long delay cannot block a short one. Verified against rabbitmq:3.13 in `tests/test_rabbitmq_queue.py`: delay, due order, head-of-line, retry backoff, dead-lettering, delays longer than the cascade, and a worker retrying a failing handler. CI requires it not to skip, but **no CI run has executed it yet**. Not tested in a cluster, across a broker restart, or with quorum queues; a broker crash during a hop between levels can lose that message. |
| `queue` scheduler | `alpha` | Every replica runs it; ticks are claimed in PostgreSQL (`jfast_schedule_ticks`) or Redis, and the job id is deterministic. Verified against real PostgreSQL 16 (20 concurrent claims land once, claim and job commit together, two running apps enqueue each tick once, pruning) and real Redis 7 in `tests/test_scheduler.py`; cron and DST covered by 77 unit tests. A process killed between claiming and enqueueing loses that tick when the store and the queue are different systems. No CI run has executed the real-server tests yet. |
| `http` (service client) | `alpha` | Timeouts, retries, retry budget, circuit breaker, bulkhead and header propagation are tested against `httpx.MockTransport` with a fake clock. **Never sent over a real socket**; no TLS, no load test; streaming not supported. Breakers are per process. |
| `outbox` | `alpha` | Messages written through the request's session commit with its rows or not at all; on the PostgreSQL queue in the same database the job goes straight into `jfast_jobs`. The relay claims rows with `FOR UPDATE SKIP LOCKED`, backs off, and sets a message aside as dead after `max_attempts`; `claim_once` deduplicates on the consumer's side. Two relays racing over 60 rows send each exactly once against PostgreSQL in `tests/test_outbox.py`. The relay has not been run against a real Redis, RabbitMQ or Kafka. |
| `idempotency` | `alpha` | `Idempotency-Key` recorded in the request's transaction: a retry replays the stored response, a different body with the same key is 422, a concurrent duplicate waits on the first insert and gets 409 or the replay. Four simultaneous requests with one key write one row against PostgreSQL in `tests/test_idempotency.py`. The response is recorded after it is sent, so a process that dies in between leaves the key in progress until it expires. |
| `events` (Kafka) | `unverified` | Never run against a broker. No outbox, so a committed row can lose its event. |
| `mongo` | `alpha` | Client and handle only. No document contract, no migrations, no repository parity with SQL. |
| `qdrant` | `alpha` | Client, health check, container. |
| `rag` | `experimental` | Fixed-width chunking, no reranking, no hybrid search, and `ensure_schema` runs DDL at startup instead of through Alembic. |
| `notifications` (FCM) | `unverified` | Payload construction is tested; a delivery has never been made from CI. |
| `channels` | `alpha` | Declared pub/sub. The memory backend is covered by tests; the redis and kafka backends are not run against a real server in CI. |
| `mail` | `alpha` | Templates, queueing and the console backend are tested. Through `0.1.0a7` a service could ship to production on the default `console` backend, which prints every message to stdout while `send` reports success — no bounce, no error, and no mail; production refuses both silent backends now. No message has been sent through a real SMTP server from CI, which is the reason this is not `beta`: the backend that actually delivers is the one nothing exercises. |
| `websocket` | `alpha` | Handshake, registry, backpressure, heartbeat and token expiry are covered in isolation. Cross-worker delivery is asserted against a real `redis:7-alpine` — two app instances, a socket on each, exactly-once — and the author mutation-tested that assertion, finding it passed with a double-delivery bug injected before fixing it. CI now starts Redis and fails if the test skips, but **no CI run has executed it yet**, so this stays `alpha` until one does. No browser client, no proxy, no load test, and two workers in one process rather than two processes. |
| `sentry` | `alpha` | Off by default. |

## Generator and deployment

| Area | Level | Notes |
| --- | --- | --- |
| `jfast new module` / `new service` (Python) | `beta` | Every layout (`modular` by default), Spanish or English table names, the HTMX overlay, Alembic and pytest wiring are rendered and run in CI. |
| `jfast start` | `beta` | Through `0.1.0a6` it generated a module it never mounted, so the service answered 404 on its own endpoints while every test passed, and the compose file it wrote could not build because no generator wrote the Dockerfile that `build:` needs. Nothing caught either: the tests covered the pieces and nothing ran the sequence. Promoted because CI now does — `scripts/smoke_compose.sh` runs `docker compose up --build` on what this command generates and asks the running container what it serves, and the `compose` job passed on `0.1.0a7`. `tests/test_quickstart.py` holds the same assertions statically. |
| Vue and React scaffolds | `alpha` | `npm install` plus `vite build` run in CI, which is what caught the router marker bug. Both looks -- `nexora` (the default) and `classic` -- are built in both frameworks by `smoke_frontend.sh`, and the look is recorded in `.jfast-template` and followed by `jfast new view`. The Nexora screens and accent picker were checked by hand in Chrome only: not Safari or Firefox, and the no-WebGL fallback has not been seen. No runtime test. |
| Go service scaffold | `alpha` | `go vet`, `go test`, `go build`, then the binary is started and curled. |
| gRPC | `experimental` | The `.proto` contract is generated and the port reserved. No stubs, no server wiring. |
| Workspaces and port allocation | `beta` | Resources are named instances and services bind to them under a variable; the whole flow is exercised in CI. A 0.1 file still loads and `migrate-resources` converts it. |
| `deploy compose` / `dockerfile` | `beta` | Derived from the plugin graph. One container per resource with the connection strings beside them, and the generated image is built and run against a real PostgreSQL in CI. |
| `workspace k8s` | `unverified` | Manifests are structurally asserted in tests. They have never been applied to a cluster, not even kind. |
| `deploy function` (Lambda / Cloud Run) | `unverified` | Writes scripts rather than running them; never applied against a real account. |
| `load_secrets` (AWS / GCP) | `unverified` | Never run against a real secret manager. |

## Not present at all

Named here so nobody has to grep to find out:

- **No distributed tracing.** Logs and metrics only; `request_id` gives you grep, not spans.
- **No schema or module diagrams.** `jfast workspace graph` draws the services and resources; the database schema and the module import graph do not.
- **No declared use cases.** What a service does is not written down anywhere a build can check.
- **No SSE.** Websockets exist (see `websocket` above); the server-sent-events helper, which is what most one-way features should use instead, does not.
- **No dependency lock, and floors nobody tests.** The four packages in the request path have ceilings, so a major release cannot arrive unannounced. The floors are declared but CI installs the newest of everything, so a floor that stopped being enough goes unnoticed -- `0.1.0a9` found FastAPI's had been wrong for this reason. There is no record of which versions a given commit was tested against.
- **Published, pre-release only.** `pip install jfastframework` resolves the latest alpha, because pip takes a pre-release when no final release exists. The first final release changes that: from then on an alpha needs `--pre`.

[PLAN-NEXT.md](PLAN-NEXT.md) is the ordered plan for closing all of it.
