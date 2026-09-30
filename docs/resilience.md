# Resilience

What a service does when something it depends on stops answering: how long it
waits, whether it retries, when it stops asking, what the client gets, and what
`/ready` says. All of it is on by default. This page lists the numbers, the
reasons for them, and the drills that check them against real servers.

Three rules run through every plugin:

1. **Every external call has a deadline, a retry policy and a breaker.** A
   dependency that is *down* fails fast on its own — the connection is
   refused. The dangerous one is *hung*: it accepts the connection and never
   answers, which is what a paused container, a frozen VM or a full accept
   queue looks like. Without a deadline each request waits for the operating
   system to give up, and without a breaker every request pays that wait again.
2. **An outage answers 503, never 500 and never a hang.** A 500 tells a client
   to stop and a reader to look for a bug; a 503 says the same request can
   succeed in a moment.
3. **`/ready` fails only for what a restart or a reroute can fix.** A shared
   dependency being down is reported as *degraded*: taking every replica out of
   rotation over it turns one outage into two.

## Defaults per dependency

| Dependency | Deadline | Retries | Breaker | Settings |
| --- | --- | --- | --- | --- |
| PostgreSQL, connecting | 10 s (`connect_timeout`) | — | opens after 2 failed connects, 5 s | `[plugin.database]` |
| PostgreSQL, pooled connection check | 2 s (`ping_timeout`) | one new connection | — | `[plugin.database]` |
| PostgreSQL, statement | off (`command_timeout = 0`) | — | — | `[plugin.database]` |
| Redis (cache, rate limit, token store) | 1 s per command, 2 s to connect | — | opens after 3 failures, 5 s | `[plugin.cache]` |
| Identity provider (JWKS) | 5 s per fetch, whole | 2 tries, 429/5xx/network only | opens after 3 failed refreshes, 30 s | `[plugin.auth] jwks_*` |
| S3 / MinIO | 5 s connect, 30 s read | 3 tries (botocore "standard") | opens after 5 failures, 15 s | per disk |
| SMTP | 30 s per socket operation | the queue's (`max_attempts`) | — | `[plugin.mail]` |
| Model provider | see [LLM](llm.md) | | | `[plugin.llm]` |
| Sibling services | see [HTTP client](http-client.md) | | | `[plugin.http]` |

Why these numbers:

- **PostgreSQL connect, 10 s.** asyncpg's own default is 60 s — two request
  timeouts. Ten covers TLS and SCRAM on a busy server (measured at up to 4.4 s
  on a loaded laptop) and still leaves the request time to answer 503 itself,
  well inside the 30 s `[app] request_timeout`.
- **PostgreSQL ping, 2 s.** SQLAlchemy's `pool_pre_ping` has no deadline of its
  own: a pooled connection to a database that stopped answering held its
  request until the socket died — in practice, until the request timeout. The
  framework replaces it with a bounded `SELECT 1` (simple protocol, so it works
  behind PgBouncer in transaction mode); past 2 s the connection is terminated
  and a new one is tried.
- **PostgreSQL statements, off.** Migrations and reports legitimately run for
  minutes; a request is already bounded by `request_timeout` and a job by the
  worker's `job_timeout`. Set `command_timeout` on a service whose every query
  should be fast.
- **Redis, 1 s.** Redis answers in well under a millisecond. The rate limiter
  and the token store sit in front of every request, so this is also what the
  first requests of an outage wait, per command. Blocking commands (`BLMOVE`,
  `BLPOP`, `XREAD`…) and pub/sub are exempt: waiting is what they are for, and
  the queue and `channels` read them from the same client.
- **JWKS, 5 s, 30 s open.** Serving cached keys for thirty seconds is harmless —
  they were valid a moment ago — and it is thirty seconds of requests that do
  not each wait on an issuer that is down.
- **S3, 5 s / 30 s / 3 tries.** The read timeout is the gap between bytes, not
  the whole transfer. The breaker keeps an S3 outage from holding a worker
  thread per upload through three timeouts.
- **SMTP, no breaker.** Mail is sent from the queue: a mail server that is down
  costs a job's retries, not a request's latency.

## What fails open, what fails closed

| When this is down | What happens | Status | `/ready` |
| --- | --- | --- | --- |
| PostgreSQL | the route fails | **503** `Database Unavailable` | **unavailable** (503) |
| PostgreSQL pool exhausted | the route fails after `pool_timeout` | **503** | unchanged |
| Redis, `cache.get_or_set` | the loader runs, nothing is cached | 200 | degraded |
| Redis, `cache.get`/`set`/`delete` | raises (a `redis.ConnectionError`, also a 503 if it escapes) | 503 | degraded |
| Redis, rate limit (`fail_open = true`, default) | the request is not limited, a warning is logged | 200 | degraded |
| Redis, rate limit (`fail_open = false`) | the request is refused | **429** with `Retry-After` | degraded |
| Redis, token revocation (`revocation_fail_open = true`, default) | the token is accepted without the check | 200 | degraded |
| Redis, token revocation (`revocation_fail_open = false`) | every authenticated request is refused | **503** | degraded |
| Identity provider, keys cached | tokens verify with the cached keys | 200 | degraded |
| Identity provider, nothing cached | the token is not verified | **401** | **unavailable** |
| S3 bucket | the storage call fails | **503** once the breaker is open | degraded |
| Local disk missing or read-only | the storage call fails | 500 | **unavailable** |
| SMTP, transient (timeout, 4xx) | the job is retried with backoff | — | degraded |
| SMTP, permanent (5xx, too large, bad header) | the job goes to the dead letters at once | — | degraded |

A permanent SMTP failure is not retried because retrying cannot change the
answer — and for a rejected recipient, hammering the server is how a sending
domain gets flagged. The dead letter keeps the message for a replay once the
cause is fixed.

## What `/ready` reports

`/ready` runs every plugin's check concurrently under `[app] readiness_timeout`
(2 s). One rule decides the answer: a failure fails readiness (**503**,
`"status": "unavailable"`) only when the plugin is declared critical **and** the
report says critical. Everything else that is unhealthy is **degraded** and
answers **200**, so the orchestrator keeps the replica in rotation.

| Plugin | Critical? |
| --- | --- |
| `database` | yes |
| `auth` | yes when no key can be fetched; degraded while serving cached keys or with the revocation store down |
| `storage` | a local disk: yes; an object store: degraded |
| `cache`, `ratelimit`, `mail`, `channels`, `websocket`, `http` | never |

A plugin declared non-critical can no longer fail readiness through a branch
of its check that forgot `critical=False`, or through a check that raises.
Every entry names its plugin, so the dependency is in the body:

```json
{"status": "degraded", "checks": {"cache": {"healthy": false, "status": "fail",
 "detail": "redis unreachable: calls to 'redis' are suspended ...", "critical": false,
 "meta": {"breaker": {"state": "open", "retry_after": 3.2}}}}}
```

A check that does not answer in time is `"status": "timeout"`, distinct from
`"fail"`: one is a dependency that said no, the other one that did not answer.

## The drills

`tests/test_failure_drills.py` takes each dependency away from a running
service and gives it back. For each one it checks that the route answers the
documented status inside its deadline, that `/ready` names the dependency, and
that the next request after recovery succeeds — the same app and the same pools,
no restart.

PostgreSQL and Redis are *paused* (`docker pause`), not stopped: the socket stays
open and nothing answers, which is the hard case. Pausing a server breaks every
other suite using it, so these run only when told which container is theirs:

```bash
JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5499 \
JFAST_DRILL_PG_CONTAINER=jfast-pg \
JFAST_TEST_REDIS_URL=redis://localhost:6379/0 \
JFAST_DRILL_REDIS_CONTAINER=jfast-redis \
pytest -q -s tests/test_failure_drills.py
```

The identity-provider drill needs no container — the issuer is an httpx mock
transport that answers, hangs or fails on command — and runs in every suite.
The model provider's 429/500 are covered by the LLM tests.

### Measured

Framework defaults, PostgreSQL 16 and Redis 7 under Docker Desktop on macOS, on
a laptop under heavy load. Your numbers will be smaller; the shape will not.

| Drill | Step | Time | Status |
| --- | --- | --- | --- |
| PostgreSQL paused | request on a pooled connection (ping 2 s + connect 10 s) | 12.0 s | 503 |
| | `/ready` | 2.0 s | 503, `database` failing |
| | request with no pooled connection (connect 10 s) | 10.0 s | 503 |
| | request once the connect breaker is open | < 0.01 s | 503 |
| | `/ready` once the breaker is open | < 0.01 s | 503 |
| | first request after unpause (after the 5 s cool-down) | 0.04 s | 200 |
| Redis paused | first request (rate limit 1 s + cache read 1 s) | 2.0 s | 200 |
| | second request (third failure opens the breaker) | 1.0 s | 200 |
| | every request after | < 0.01 s | 200 |
| | `/ready` | < 0.01 s | 200 `degraded`, `cache` and `ratelimit` failing |
| | rate limit with `fail_open = false` | 1.0 s | 429 |
| | first request after unpause (after the 5 s cool-down) | 0.02 s | 200 |
| Identity provider hangs | first three requests after the keys expire (`jwks_timeout = 0.5`) | 1.0–1.2 s | 200 |
| | every request after (breaker open) | < 0.01 s | 200 |
| | `/ready` | < 0.01 s | 200 `degraded`, `auth` names the failed refresh |
| | first request after the issuer is back (after the cool-down) | 0.01 s | 200 |

A PostgreSQL outage costs at most two requests of ten to twelve seconds per
process before the connect breaker answers the rest at once; the pooled
connections that went bad are replaced on recovery without a restart.

## Boot fails on misconfiguration

A value that would only fail on the first query, send or request refuses to
boot instead, with a message that names the setting and the fix. Among them:
an unknown `session_timezone`, `pool_size = 0` (which SQLAlchemy reads as
*unlimited*), a tenant DSN template with no `{tenant}`, a cache URL that is not
Redis, a storage `visibility` that is neither `public` nor `private`, an S3
disk with an access key and no secret, an SMTP port out of range, `use_ssl`
with `use_starttls`, a JWT algorithm PyJWT does not know, `public_key` mode
asked to issue tokens, a queue table name that is not an identifier — and, in
production, a plain-http `jwks_url`, an HMAC secret or storage signing key
shorter than 32 bytes, an SMTP sender that is not an address, and the RabbitMQ
`guest@localhost` default. `tests/test_boot_validation.py` has one test per
rule.

## What this does not cover

- **Statements that run long on a healthy database** — `command_timeout` is off
  by default; the request and job timeouts bound them.
- **Blocking Redis reads and pub/sub** have no command deadline; a queue worker
  long-polling a Redis that is paused waits on the socket.
- **Breakers are per process.** Each worker opens its own on its own evidence,
  so a recovering dependency sees up to one probe per process.
- **The broker drill** (RabbitMQ paused) is not written yet.
