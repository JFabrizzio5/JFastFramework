# Architecture

Every decision here has a cost. This document names both sides.

---

## 1. A versioned library, not a code generator

**Decision.** The runtime lives in `jfastframework` and is imported. The
generator emits only code that is genuinely per-service.

**Why.** The previous generation wrote its runtime as string literals inside
generator functions. Every generated service was a fork frozen at generation
time; a fix to the shared HTTP client meant patching N repositories by hand,
and templates that live inside string literals cannot be linted, diffed or
tested.

**Cost.** Services can no longer edit the shared layer freely. That was an
accidental feature — when something did not fit, teams patched locally. If the
extension points are not good enough, forks come back, only now hidden. This is
the risk the plugin contract has to earn its way out of.

---

## 2. Everything above the kernel is a plugin

**Decision.** Monitoring, database, cache, RAG and error tracking are plugins.
The kernel resolves, orders, starts and stops them. It knows nothing else.

**Why.** "Monitoring by default, removable on demand" is only true if
monitoring is not special-cased. Built-ins register through the same
`jfastframework.plugins` entry-point group third parties use, so a third-party
plugin can replace a built-in by claiming the same provider key.

**Cost.** Indirection. Reading `create_app` does not tell you what the app
does — you run `jfast describe`. That is the trade the design accepts, and why
introspection tooling is part of the kernel rather than an add-on.

---

## 3. Plugins communicate through string keys

**Decision.** `ctx.provide("db.engine", engine)` / `ctx.require("db.engine")`.
Plugins never import each other.

**Why.** Direct imports would make the graph rigid: swapping the Redis cache
for an in-memory one would touch every consumer.

**Cost.** No static type checking across the boundary. Mitigations:
`require(key, expected=Type)` checks at runtime, `meta.provides` is declared up
front, duplicate claims fail at build time, and a missing provider raises an
error naming what *is* available.

---

## 4. Plugins declare their infrastructure

**Decision.** `Plugin.infra()` returns the containers the plugin needs.
`jfast deploy compose` composes them into a compose file.

**Why.** Hand-maintained compose files drift from the app. Disable the cache
plugin and the Redis container should disappear — not linger for six months
until someone notices.

**Cost.** Plugins now know something about deployment, which is not strictly
their business. The alternative — a separate infra manifest — drifts, which is
the exact failure being designed out. Accepted deliberately.

**Convention.** A service owns a block of ten ports starting at its base port;
each plugin declares an offset inside the block. Inherited from the CometaX
scheme, minus the central IAM as a hard dependency.

---

## 5. Two health endpoints, not one

**Decision.** `/health` is liveness and never probes dependencies. `/ready`
aggregates every plugin's health check and returns 503 when a **critical** one
fails.

**Why.** Conflating them causes cascading restarts: the database blips, the
liveness probe fails, the orchestrator kills healthy pods, and the stampede
finishes off the database.

**Detail.** `HealthReport.critical=False` means degraded, not down. A cold
cache should not take the service out of rotation.

---

## 6. RFC 7807 for every error

**Decision.** All failures serialise to `application/problem+json`.

**Why.** One error shape across every service. Clients and sibling services
parse once.

**Detail.** The catch-all handler returns the exception message only when
`debug` is on. Leaking stack traces and internal paths to production clients is
an information-disclosure finding, and the default has to be the safe one.

---

## 7. Constraint naming convention pinned in `Base`

**Decision.** `MetaData(naming_convention=...)` in `jfastframework.db.base`.

**Why.** Without it, PostgreSQL invents constraint names and Alembic
`--autogenerate` produces different diffs on different machines. With dozens of
services running autogenerate, that is a permanent low-grade migration mess.

**Cost.** Existing databases with auto-named constraints need a one-time
migration to adopt it. Do it before the fleet grows, not after.

---

## 8. Templates are files, not strings

**Decision.** Jinja2 templates under `templates/`, rendered by `Scaffolder`.

**Why.** Templates that are files can be linted, diffed in review, and tested
by rendering them.

**Detail.** Each generated tree carries a `.jfast-template` stamp recording the
framework version and the render context. That stamp is what makes a future
`jfast upgrade` able to show a diff instead of a rewrite (phase 3).

---

## 9. Config precedence

```
CLI overrides  >  environment  >  jfast.toml  >  field defaults
```

Secrets belong in the environment as `SecretStr`, never in `jfast.toml` — that
file is committed. `SecretStr` also keeps DSNs out of `jfast describe` output
and the `/info` endpoint.

---

## 10. Optional dependencies fail late, not at discovery

**Decision.** A plugin whose extra is not installed is skipped during discovery
and recorded in `discover.broken`. It only raises if the service actually
enables it — and then the error names the import failure.

**Why.** Installing `jfastframework` without `[rag]` must not break
`jfast --help`. But enabling `rag` without `[rag]` must fail loudly, with the
reason.

---

## Request lifecycle

```
request
  → RequestContextMiddleware      assign/propagate X-Request-ID, bind contextvars
  → PrometheusMiddleware          RED metrics, labelled by route template
  → route handler
      → DbSession                     session; commits before the response
      → Service                       domain rules
      → Repository                    data access, tenant-filtered
  → response                      X-Request-ID echoed back
  ← exception                     → problem+json, request_id attached
```

## Startup lifecycle

```
create_app()
  1. load jfast.toml + env            → JFastConfig
  2. discover plugins                 → entry points + dotted paths
  3. select and order                 → allow/deny, requires, cycles, conflicts
  4. instantiate                      → each plugin gets its [plugin.<name>] block
  5. install error handlers
  6. plugin.register(ctx)             → routers, middleware, providers. No I/O.
  7. mount system + app routers

lifespan startup
  8. plugin.startup(ctx)              → in order. Pools, connections, DDL.
lifespan shutdown
  9. plugin.shutdown(ctx)             → reverse order. One failure does not
                                         block the rest.
```
