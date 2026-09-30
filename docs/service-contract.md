# The service contract

A JFast service is not "a service written with JFast". It is a service that
satisfies this contract. That distinction is the whole reason a Go service and
a Python service can sit behind the same gateway, take ports from the same
workspace, and be fronted by the same Caddyfile — none of which know or care
which language produced them.

Keep this stable. The templates are implementation; this is the interface.

---

## 1. System endpoints

| Endpoint | Must | Must not |
| --- | --- | --- |
| `GET /health` | Return 200 while the process is up | Probe any dependency |
| `GET /ready` | Return 200 when serving, 503 when a **critical** dependency is down | Be used as the liveness probe |
| `GET /info` | Report version and inventory | Exist when `JFAST_ENV=prod` |

`/health` and `/ready` are separate because conflating them causes cascading
restarts: the database blips, liveness fails, the orchestrator kills healthy
pods, and the stampede finishes off the database.

A non-critical dependency failing makes `/ready` report `"degraded"` with a
200. A cold cache should not take a service out of rotation.

```json
// GET /health
{"status": "ok", "service": "billing", "version": "0.1.0", "env": "local"}

// GET /ready
{"status": "degraded", "service": "billing",
 "checks": {"cache": {"healthy": false, "detail": "cold", "critical": false}}}
```

## 2. Request correlation

Read `X-Request-ID` from the request. **Reuse it if present**, mint one if not.
Echo it on the response, attach it to every log line, and forward it on
outbound calls.

Minting a fresh id per service instead of reusing the caller's is the single
most common way a distributed trace becomes useless: every hop starts a new
"trace" and nothing joins up.

The same goes for W3C trace context. Read `traceparent` and `tracestate`;
drop a `traceparent` that is not valid (version `00`, lowercase hex, a 32-hex
trace id and a 16-hex parent id that are not all zeros, 2-hex flags) together
with its `tracestate`; log the trace id as `trace_id`; and send the context on
outbound calls. A service that exports spans (the Python `telemetry` plugin)
sends its own span as the parent. One that does not (the Go scaffold) passes
the caller's through **unchanged** -- inventing a parent id that is never
exported leaves a hole in the trace.

What an outbound call carries, in every language: `X-Request-ID`,
`traceparent`, `tracestate`. Not the tenant (the next service resolves it from
the token) and not `Authorization`, unless the caller opts in -- the Python
client's `forward_authorization = true`.

## 3. Errors

Every failure serialises to RFC 7807 `application/problem+json`:

```json
{"type": "about:blank", "title": "Not Found", "status": 404,
 "detail": "invoice 7 not found", "instance": "/invoices/7",
 "request_id": "9f2c…"}
```

Unhandled exceptions included. The `detail` for an unhandled error must be
generic unless `JFAST_DEBUG=true` — leaking internals to clients is an
information-disclosure finding, and the default has to be the safe one.

## 4. Configuration

From the environment, with these names:

| Variable | Meaning |
| --- | --- |
| `JFAST_APP_NAME` | Service name, used in logs and metrics |
| `JFAST_ENV` | `local` / `dev` / `staging` / `prod` |
| `JFAST_PORT` | HTTP port — the base of the service's block |
| `JFAST_DEBUG` | Whether internal error detail reaches clients |
| `JFAST_LOG_JSON_LOGS` | Structured logs on or off |

One `.env` convention covers every language. Do not invent per-language names.

## 5. Ports

A service owns **ten consecutive ports** from its base. Plugins claim offsets
inside that block:

| Offset | Use |
| --- | --- |
| +0 | HTTP |
| +1 | PostgreSQL |
| +2 | Kafka / PgBouncer |
| +3 | Redis |
| +4 | MongoDB |
| +5 / +6 | Prometheus / Grafana, RabbitMQ |
| +7 / +8 | Qdrant HTTP / gRPC |
| +9 | The service's own gRPC |

The workspace allocates the blocks. Do not hand-pick a port unless you have
checked what is already in one.

## 6. Logs

One JSON object per line, on stdout, carrying at least `service`, `env`,
`level`, `message` and — inside a request — `request_id`.

Not a file, not a socket. The runtime collects stdout; a service that manages
its own log files fights whatever is already collecting them.

The access log line is `"message": "request"` with `http_method`,
`http_path`, `http_status`, `duration_ms`, and -- when the request has them --
`trace_id` and `tenant_id`.

## 7. Identity and tenant (when the service authenticates)

Optional, and off by default. A service that authenticates verifies the
workspace's JWTs by the rules of the Python `auth` plugin and reads the same
variables, so one token behaves the same against every service:

| Variable | Meaning | Default |
| --- | --- | --- |
| `JFAST_AUTH_MODE` | `secret` (HMAC), `public_key` (pinned PEM) or `jwks` | -- |
| `JFAST_AUTH_ALGORITHMS` | Allowed algorithms, e.g. `["HS256"]`. Never read from the token; `none` never allowed; HMAC and RSA/EC never mixed | `["RS256"]` |
| `JFAST_AUTH_SECRET` / `JFAST_AUTH_PUBLIC_KEY` / `JFAST_AUTH_JWKS_URL` | The key material for the mode | -- |
| `JFAST_AUTH_ISSUER` / `JFAST_AUTH_AUDIENCE` | Checked, and then required, when set | empty |
| `JFAST_AUTH_LEEWAY` | Clock skew allowed on `exp`, `nbf`, `iat`, in seconds | `30` |
| `JFAST_AUTH_TENANT_CLAIM` / `_SCOPE_CLAIM` / `_ROLES_CLAIM` | Where the token keeps them | `tenant_id` / `scope` / `roles` |
| `JFAST_TENANCY_SOURCES` | Where the tenant comes from, in order of trust: `token`, `user`, `subdomain`, `path`, `header` | `["token", "subdomain"]` |
| `JFAST_TENANCY_BASE_DOMAIN` | Needed by the `subdomain` source | empty |
| `JFAST_TENANCY_REQUIRE_TENANT` | Refuse with 403 any request that resolves to no tenant, outside `/health`, `/ready` and the like | `false` |

The rules: `exp`, `iat` and `sub` are required; a token with `typ: refresh`
is not a bearer; a bad token on an open route is ignored, not rejected. Answer
**401** when there is no verified caller and **403** when there is one without
the scope, role or tenant -- a client refreshes its session on a 401 and gives
up on a 403. A tenant comes from a signed claim before anything a request can
choose; `X-Tenant-ID` is only a source when `header` is listed.

In Python the plugin settings can also come from `jfast.toml`
(`[plugin.auth]`, `[plugin.tenancy]`), which wins over the environment --
except `JFAST_ENV` and `JFAST_DEBUG`, which beat `[app] env` and `debug` when
set, because they describe the deployment ([deploy](deploy.md#which-wins-jfasttoml-or-the-environment)). A
service in another language only has the environment, so write the values it
must share -- mode, algorithms, secret or public key, issuer, audience,
tenancy sources -- into its `.env`. Nothing copies them between services for
you: a Python service gets its secret the same way, from its own `.env`.

The Go scaffold switches each part on from the environment alone, since it
has no plugin list: auth when `JFAST_AUTH_MODE` is set, or when exactly one of
the key variables is (the key names the mode, and the algorithms default to
`HS256` for a secret, `RS256` otherwise); tenancy when `JFAST_TENANCY_SOURCES`
is set. A configuration it cannot enforce -- `jwks`, mixed algorithm
families, a short secret in production, a PEM used as an HMAC secret --
stops it at boot rather than letting it answer requests unchecked.

---

## Languages

| | Python | Go |
| --- | --- | --- |
| Toolchain | `python3` | `go` |
| Contract implementation | `jfastframework` (imported) | `internal/jfast/` (vendored, standard library only, ~1,250 lines of code) |
| Trace context | propagated; spans with the `telemetry` plugin | passed through unchanged; no spans (add `otelhttp` yourself) |
| Auth (section 7) | `auth` plugin: `jwks`, `public_key`, `secret`; issues tokens | verify only: `public_key`, `secret`; `jwks` refuses to start |
| Token revocation | checked in Redis on every request | **not checked**: a revoked access token works until it expires (15 min by default) |
| Tenancy (section 7) | `tenancy` plugin | same sources and 401/403; no per-tenant time zones |
| Plugin system | yes | no |
| Module generator | yes | one sample module |
| Migrations | Alembic | bring your own |
| Queue worker | yes | no -- the table format is documented below |
| Kinds | `api`, `web`, `gateway` | `api` |

```bash
jfast new service billing                     # Python
jfast new service edge --language go          # Go
jfast new service edge --language go --grpc   # + the .proto contract
```

You only need the toolchain for the languages you actually use. A Python-only
team never installs Go.

### Go: contract yes, framework no

The Go scaffold ships what a Go service needs to be a good citizen of a
workspace whose other services are Python, and nothing else: the endpoints,
the request id, the trace context, the error shape, the logs, the tokens and
the tenant. Router, ORM and workers are yours -- Gin, Echo, Chi, `pgx`,
whatever the team already knows.

Everything in `internal/jfast/` is plain `net/http` middleware
(`func(http.Handler) http.Handler`) with no third-party dependency. A Gin,
Echo or Chi engine is an `http.Handler`, so `jfast.Chain(engine, ...)` wraps
it as it is; handlers read `jfast.ClaimsFrom(ctx)` and `jfast.TenantFrom(ctx)`.
`main.go` wires, outermost first: `RequestID`, `AccessLog`, `Trace`,
`Recover`, `Authenticate`, `ResolveTenant`; routes opt into `RequireAuth`,
`RequireScopes`, `RequireRoles` and `RequireTenant`. Outbound calls go through
`jfast.PropagatingTransport` or `jfast.Propagate`.

What CI proves about it: the generated service passes `gofmt`, `go vet` and
its own tests (trace context, HS/RS/ES tokens, `alg: none`, algorithm
confusion, issuer and audience, tenant order, 401/403); the binary starts and
answers; tokens minted by the Python auth plugin's own issuer are accepted by
it and resolve the same tenant a JFast app resolves; a `traceparent` reaches
its outgoing call unchanged; and `jwks` mode refuses to start.

### Why Go vendors the contract instead of importing a shared module

At two services `internal/jfast/` is a few files you can read in one sitting.
At ten, extract it into its own Go module and import it. Extracting on day one
buys a versioning problem before there is anything to version.

### Where Go earns its keep

A hot path, a long-lived connection handler, a binary you want to be 12MB and
start in 5ms. Not "because it is faster" — the plugin system, the migrations
and the module generator are worth more than the milliseconds on most services.

---

## Consuming the queue from another language

The PostgreSQL queue is a table, so a service in any language can read it.
This is a **documented format, not a supported client**: JFast ships no Go (or
other) worker, and nothing here is covered by a compatibility promise beyond
this page and the changelog. Read `jfastframework/queues/postgres.py` for the
version you run.

**Give the other language its own table.** A Python worker claims every row
of the table it drains and dead-letters, on the first attempt, any task it has
no handler for (`UnknownTask`). A Go consumer sharing `jfast_jobs` with a
Python worker loses those jobs. Create a second `PostgresQueue(engine,
table="edge_jobs")` on the producing side for the jobs the Go service owns.

### The table

What `PostgresQueue.setup()` creates (default name `jfast_jobs`, set by
`[plugin.queue] name`):

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | `TEXT` primary key | Job id; 32 hex characters by default. Inserts are `ON CONFLICT (id) DO NOTHING`, so an id is enqueued once. |
| `task` | `TEXT` | The handler name. |
| `payload` | `JSONB`, default `{}` | The handler's argument. |
| `attempts` | `INTEGER`, default 0 | Incremented by every claim. |
| `max_attempts` | `INTEGER`, default 3 | Past it, the job is dead. |
| `available_at` | `TIMESTAMPTZ` | Not before this instant: delays and retry backoff. |
| `locked_until` | `TIMESTAMPTZ`, null | The lease of a running job. |
| `request_id` | `TEXT`, null | The request that queued it. |
| `tenant_id` | `TEXT`, null | The tenant it runs as. |
| `status` | `TEXT` | `pending`, `running` or `dead`. A finished job is deleted, not marked. |
| `last_error` | `TEXT`, null | Why the last attempt failed, up to 2,000 characters. |
| `created_at` | `TIMESTAMPTZ` | Enqueue time. |
| `trace` | `JSONB`, null | W3C carrier of the code that queued it: `{"traceparent": ..., "tracestate": ...}`, or null without telemetry. |

### The lifecycle, as the Python worker runs it

- **Claim** atomically, one row at a time, with `FOR UPDATE SKIP LOCKED`:

  ```sql
  UPDATE edge_jobs SET status = 'running', attempts = attempts + 1,
         locked_until = NOW() + make_interval(secs => $1)   -- the visibility timeout, 300 s by default
  WHERE id = (
      SELECT id FROM edge_jobs
      WHERE available_at <= NOW()
        AND (status = 'pending' OR (status = 'running' AND locked_until < NOW()))
      ORDER BY available_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1)
  RETURNING id, task, payload, attempts, max_attempts, request_id, tenant_id, trace;
  ```

  The second branch of the `WHERE` is what returns a job whose worker died.
  Nothing extends the lease: a handler that runs longer than the visibility
  timeout is claimed again while it is still running.
- **Ack**: `DELETE FROM edge_jobs WHERE id = $1`.
- **Retry**: `UPDATE ... SET status = 'pending', locked_until = NULL,
  available_at = NOW() + make_interval(secs => $delay), last_error = $error`,
  with `delay = min(2 * 2^(attempts - 1), 300)` seconds.
- **Dead** when `attempts >= max_attempts`, or at once for a task nothing
  handles: `SET status = 'dead', locked_until = NULL, last_error = $error`.
  `jfast jobs dead` lists them and `jfast jobs retry` returns them to
  `pending` with `attempts = 0`.
- **Release** on shutdown: `SET status = 'pending', locked_until = NULL,
  available_at = NOW(), attempts = GREATEST(attempts - 1, 0) WHERE id = $1
  AND status = 'running'` -- a job stopped by a deploy did not fail.

### Events on the queue

A subscriber's job is `task = <subscriber task>` with
`payload = {"topic": ..., "event": <Event.to_dict()>}`, and its id derived
from the event id and the task, so publishing twice queues it once. The event
envelope:

```json
{"id": "9f2c…", "type": "comprobante.registrado", "source": "billing",
 "occurred_at": "2026-09-30T12:00:00+00:00",
 "request_id": "…", "tenant_id": "acme",
 "trace": {"traceparent": "00-…-…-01"}, "key": null,
 "data": {"id": 42}}
```

`trace` is `{}` when the publisher had no telemetry. The same envelope is what
goes to Kafka when an event bus is configured.

### What a consumer must do to be safe

1. **Claim atomically**, with the statement above or its equivalent. Two
   consumers that read and then update race and run the job twice.
2. **Restore the tenant** from `tenant_id` before touching data (in the Go
   scaffold, `jfast.WithTenant(ctx, tenantID)`), and log `request_id` and the
   trace id from `trace` so the job joins the request that queued it. A job
   run with no tenant reads or writes every tenant's rows.
3. **Be idempotent.** Delivery is at least once: a consumer can die between
   committing its work and deleting the row. Either record
   `(consumer, message_id)` in `jfast_inbox` inside the transaction that does
   the work -- `INSERT ... ON CONFLICT DO NOTHING`, and skip the job when no
   row was inserted, which is what the Python `claim_once` does -- or
   deduplicate on the job id in your own table.
4. **Finish inside the visibility timeout**, or split the work.
5. **Bound the retries** with `max_attempts` and dead-letter the rest, so one
   poison job cannot occupy a consumer forever.

---

## Adding a language

1. Implement sections 1 to 6 above, and section 7 if the service
   authenticates.
2. Add a `LanguageSpec` to `jfastframework/languages.py`.
3. Add a `service_<lang>/` template tree.
4. Add a CI job that **builds and runs** the generated service. A scaffold
   nobody has run is a liability that looks like a feature.

Step 4 is not optional. The Go support in this repo exists because CI compiles
the generated service, runs its tests, starts the binary and curls it — not
because the template looks right.

## What is deliberately not in the contract

- **A shared client library.** Services talk HTTP or gRPC. A shared client is
  a shared deploy.
- **A common ORM or serialisation format** beyond JSON on the wire.
- **A required tracing vendor.** `X-Request-ID` and passing `traceparent`
  through are the floor; exporting spans is the `telemetry` plugin in Python
  and your own `otelhttp` in Go, not a mandate.
