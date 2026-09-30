# Multi-tenancy

One deployment, many customers, each seeing only their own data.

The plugin answers one question — **which tenant is this request for?** — and
puts the answer on `request.state.tenant_id`, in every log line, and in the
repository base class. What it does *not* do is enforce isolation. That
distinction matters and is spelled out at the end of this page.

```toml
[plugins]
enabled = ["observability", "auth", "tenancy"]

[plugin.tenancy]
sources = ["token", "subdomain"]
base_domain = "app.example.com"
```

## Sources are an order of trust

The list is tried in order and the first hit wins. That order is the design:

| Source | Controlled by | Trust |
| --- | --- | --- |
| `token` | your identity provider, cryptographically | high |
| `user` | the same signed token: the user *is* the tenant | high |
| `subdomain` | your DNS and TLS | medium |
| `path` | the URL | low |
| `header` | whoever sent the request | **none** |

`header` exists because it is genuinely useful in development and in tests. It
is not in the default list, and enabling it in production logs a warning,
because `X-Tenant-ID: acme` is one `curl` away from another tenant's data.

A signed claim always outranks the hostname. Someone who points `acme.` at your
IP has not become Acme; someone holding a token your identity provider signed
for Acme has.

## Every account is its own tenant: the `user` source

Most SaaS products start without organisations: a person signs up, and what
they upload is theirs. There is no `tenant_id` claim to read, and inventing a
tenant table to hold one row per user is ceremony. The `user` source makes the
signed-in user's id -- the token's `sub` -- the tenant:

```toml
[plugins]
enabled = ["observability", "database", "auth", "accounts", "tenancy"]

[plugin.tenancy]
sources = ["token", "user"]
```

Everything downstream works unchanged: `BaseRepository` filters by it, it is in
every log line, [row-level security](#isolation-the-database-enforces-row-level-security)
reads it, the [`rag` store](rag.md) scopes by it, and the [`llm` budget](llm.md)
charges it.

Put `user` **after** `token`. The day a user joins an organisation and their
token starts carrying a `tenant_id` claim, the claim wins and they move to the
organisation's data without a code change. The reverse order would keep them in
their personal space forever.

A user id is chosen by the identity provider, not by you -- a UUID, a hex id,
`auth0|abc123` -- so it is validated more loosely than a subdomain slug, but
still refuses anything that could be read as a path or SQL. Before sign-in
there is no user, so login, registration and the health checks resolve no
tenant, which is what they should do.

## Reading the tenant in a route

```python
from fastapi import Depends
from jfastframework.plugins.builtin.tenancy import current_tenant

@router.get("/invoices")
async def invoices(tenant: str = Depends(current_tenant), session: DbSession = ...):
    return await InvoiceService(InvoiceRepository(session, tenant_id=tenant)).list()
```

`current_tenant` returns what the plugin resolved. With nobody signed in it
answers **401** -- an expired token must make the client refresh, and clients
refresh on a 401, not on a 403 -- and with a signed-in caller the sources could
not scope, **403**. It reads nothing else -- not a header, not a body field. In a
service without the tenancy plugin it falls back to the token's `tenant_id`
claim, so a service that only uses `auth` still works.

Prefer it to `getattr(request.state, "tenant_id", None)`: a `None` that reaches
a repository means "no tenant filter", and the dependency makes that a 403
before it gets there.

## Subdomains

`acme.app.example.com` with `base_domain = "app.example.com"` resolves to
`acme`. The rules, all of them deliberate:

- only the leftmost label, and only one — `a.b.app.example.com` is a mistake,
  not a tenant called `a.b`;
- the bare base domain is not a tenant;
- `www`, `api`, `app`, `admin`, `static`, `cdn`, `mail` and friends are
  reserved and never resolve to a tenant;
- the label must match `[a-z0-9][a-z0-9-]{0,62}` — the slug ends up in
  hostnames, log fields and SQL parameters, and must be safe in all three.

`base_domain` is required when `subdomain` is a source. Without it, every
hostname looks like a tenant, so the plugin refuses to start rather than
resolve nonsense.

### Caddy

```bash
jfast workspace caddy --hostname app.example.com --production --wildcard-tenants
```

That emits a `app.example.com, *.app.example.com` site block with on-demand
TLS, plus the global `ask` endpoint that gates it:

```
{
	on_demand_tls {
		ask http://api:8000/internal/tenant-exists
		interval 2m
		burst 5
	}
}
```

**The `ask` endpoint is not optional.** Caddy cannot get a wildcard certificate
from a wildcard *match*, so it issues one per hostname on first sight. Without
`ask`, anyone who points a DNS record at your server can make you request
certificates for it until Let's Encrypt rate-limits your domain.

You implement it. It answers `200` if the tenant exists and anything else if it
does not:

```python
@router.get("/internal/tenant-exists")
async def tenant_exists(domain: str) -> Response:
    slug = domain.split(".", 1)[0]
    if await tenants.exists(slug):
        return Response(status_code=200)
    return Response(status_code=404)
```

Keep it off the public router, and make it cheap — it runs on every new
hostname Caddy sees, including the ones probing you.

You also need a wildcard DNS record (`*.app.example.com`) pointing at the same
address.

## Requiring a tenant

```toml
[plugin.tenancy]
require_tenant = true
```

Any request that resolves to no tenant gets a `403` in problem+json. Health
checks, metrics, `/docs` and `/openapi.json` are exempt — a readiness probe has
no tenant and must not fail.

## A database per tenant

One column per row is the default and the right answer for most services. A
database per tenant is the answer when isolation has to be physical: a
regulated customer, a restore that must not touch anyone else, a tenant with a
data volume of its own.

```toml
[plugin.database]
tenant_dsn_env_template = "JFAST_DB_DSN_{tenant}"
tenant_dsn_template = "postgresql+asyncpg://app:pw@db:5432/{tenant}"
tenant_max_engines = 25
tenant_pool_size = 2
tenant_max_overflow = 2
```

```python
from jfastframework.plugins.builtin.database import TenantSession

@router.get("/invoices")
async def list_invoices(session: TenantSession):
    ...
```

The tenant comes from `request.state.tenant_id`, so this needs the `tenancy`
plugin. A request that resolves to no tenant raises rather than picking a
database to guess at.

### The trap: pool explosion

A dict of engines keyed by tenant is the obvious implementation, and it takes
PostgreSQL down. 200 tenants at `pool_size = 10` is 2000 connections against a
server whose default `max_connections` is 100. Nothing in that code looks
wrong; it just runs out of a resource nobody counted.

So the map is a **bounded LRU** and the bound is a number you can read:

```
tenant_max_engines × (tenant_pool_size + tenant_max_overflow) = 25 × 4 = 100
```

`jfast describe --json` prints it as `tenant_max_connections`. Size it against
your server's `max_connections`, divided by the number of processes — a
container running four uvicorn workers opens four of these maps, not one.

Per-tenant pools are small on purpose. A tenant is a slice of your traffic, not
all of it, and ten idle connections per tenant is where the arithmetic goes
wrong.

### What happens when an engine is evicted mid-request

Eviction never closes an engine a request is still using. An evicted entry
leaves the map immediately — so nothing new checks it out — and is disposed
when the last lease is released. Disposing on eviction would close the
connection under a running query, which surfaces as a random
`InterfaceError` on the tenants that are busiest.

Two consequences worth knowing:

- **The least recently used engine with no active request is the one evicted.**
  A busy tenant is never the victim of a quiet one arriving.
- **A full map of busy engines refuses.** When every engine is in use and a new
  tenant arrives, `TenantPoolExhausted` is raised rather than opening engine
  number `max_engines + 1`. Going past the ceiling under load is the connection
  storm the ceiling exists to prevent, and a 503 is recoverable in a way that a
  dead database is not. If you see it, `tenant_max_engines` is below your
  concurrent-tenant working set.

Evicting a tenant by hand — after a plan change, a migration, a deletion — is
the same mechanism:

```python
databases = ctx.require("db.databases")
await databases.tenants.evict("acme")
```

### Resolving the DSN

`tenant_dsn_env_template` is tried first (`JFAST_DB_DSN_ACME`), then
`tenant_dsn_template`. Neither fits a service that keeps its tenants in a
control-plane table, so supply a resolver:

```python
databases.tenants.set_resolver(lambda tenant: catalogue[tenant])
```

An unresolvable tenant raises a `PluginError` naming both settings, not a
`KeyError` from inside a pool.

## Ordering, and why the token source works

The middleware runs **innermost**, after auth. This is not incidental: Starlette's
`add_middleware` puts a middleware outermost, which would run tenancy *before*
auth and leave the signed claim unreadable, because no principal exists that
early. The plugin appends instead, so every source — including the token —
is available when it resolves.

## Isolation the database enforces: row-level security

The resolved tenant reaches `BaseRepository`, so a query that forgets to filter
is still filtered by the repository. That is a convention: raw SQL, a join
through an unscoped table, a bug in a repository method all read every tenant's
rows. Row-level security moves the rule into PostgreSQL, where a query that
forgets the filter gets no rows instead of someone else's.

**1. Put each tenant table under a policy**, in a migration:

```python
from jfastframework.db.rls import enable_tenant_rls, disable_tenant_rls

def upgrade() -> None:
    enable_tenant_rls(op, "invoices")

def downgrade() -> None:
    disable_tenant_rls(op, "invoices")
```

**2. Turn it on**, and every transaction tells PostgreSQL its tenant -- the
request's, or a queue job's, which the worker restores:

```toml
[plugin.database]
rls = true
```

It is `set_config('jfast.tenant_id', ..., true)` as each transaction begins:
transaction-local, so a pooled connection never carries a tenant into the next
request, and safe behind PgBouncer in transaction mode -- verified, not assumed:
see [Behind PgBouncer](#behind-pgbouncer).

**3. Connect as a role the policies bind.** A superuser, or a role with
`BYPASSRLS`, ignores every policy -- and the generated compose file connects as
the database's superuser. Give the service a role of its own:

```sql
CREATE ROLE app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app;
```

Run migrations as the owner and the service as `app`. With `rls = true` the
database plugin checks the role at startup: production refuses to start under a
superuser or `BYPASSRLS` role, anywhere else it warns.

What that buys, each verified against PostgreSQL in `tests/test_rls.py`:

| | |
| --- | --- |
| `SELECT * FROM invoices`, no `WHERE` at all | only this tenant's rows |
| A transaction with no tenant | no rows, and no writes |
| `INSERT` with another tenant's id | refused by the policy |
| The next transaction on the same connection | starts with no tenant |

Work that is about every tenant by definition -- a report, a data fix -- says
so: create that table's policy with `allow_bypass=True` and run the work inside
`with bypass_rls():`. Tables without the flag stay closed even there.

The repository filter stays: it is what makes queries use the index, and it is
the first line. Row-level security is the one that holds when the first fails.

### More than the tenant: `transaction_setting`

The tenant is sometimes not the whole rule. Inside one tenant, a user may see
only some of its companies, or branches, or warehouses -- and that value has to
reach PostgreSQL the same way the tenant does, per transaction, or a pooled
connection carries one user's companies into another's request.

Register a function that returns the value for the current request or job, and
every tenant-scoped session sets it next to the tenant:

```python
from jfastframework.auth import current_principal
from jfastframework.db.rls import transaction_setting

@transaction_setting("app.companies")
def companies() -> str | None:
    principal = current_principal()
    if principal is None:
        return None
    return "{" + ",".join(principal.claims.get("companies", [])) + "}"
```

Then write the policy yourself, with the values it needs, in a migration:

```python
from jfastframework.db.rls import disable_rls_policy, enable_rls_policy

def upgrade() -> None:
    enable_rls_policy(
        op,
        "invoices",
        predicate="tenant_id = current_setting('jfast.tenant_id', true) "
        "AND company = ANY(current_setting('app.companies', true)::text[])",
    )

def downgrade() -> None:
    disable_rls_policy(op, "invoices")
```

The rules it keeps:

- **The name is checked.** `prefix.name`, lowercase, because it goes into
  `set_config`. `jfast.tenant_id` and `jfast.rls_bypass` are the framework's and
  are refused.
- **`None` means no rows.** The setting stays unset for that transaction, the
  policy reads NULL, and nothing matches -- the same way a transaction with no
  tenant sees nothing.
- **Register at import time**, next to the policies that read it. Registration
  is process-wide.
- **The predicate is SQL written by the migration's author**, not escaped:
  never build it from request data. The values reach it through
  `current_setting`, which is.

`tests/test_rls.py` runs this shape against PostgreSQL: a tenant with two
companies, a user who sees one, then both, then none.

This is what a service that opened its own session only to set a second value
can use instead -- see [`@transactional`](transactions.md) for the ones that
still need their own.

## The layers, and what each one catches

Isolation is not one feature. It is a stack, and each layer is there for the
bug that got past the one above it:

| Layer | What does it | What it catches |
| --- | --- | --- |
| Resolution | this plugin, from signed sources first | a tenant chosen by whoever sent the request |
| The route | `Depends(current_tenant)` | a handler running with no tenant at all |
| The repository | `BaseRepository(tenant_id=...)` | a query that forgot `WHERE tenant_id` |
| The database | row-level security, `rls = true` | raw SQL, a bad join, a repository bug |
| Between modules | each module's `public.py` takes `tenant_id` explicitly ([modules](modules.md)) | a module reading another's tables with its own idea of the tenant |
| Background work | jobs carry the tenant; the worker restores it | a job that runs as "nobody" and sees everything |
| Retrieval | the `rag` store refuses a call without a tenant ([RAG](rag.md)) | a search across every customer's documents |
| Spending | `tenant_budget_usd` in the [`llm` plugin](llm.md) | one tenant spending everyone's AI budget |
| Configuration | the `tenancy` check of `jfast check` | two of the settings above contradicting each other |
| The switch | `jfast check --multitenant-ready` | code that still assumes one customer, before a second one arrives |

Turn them on from the top. The first three cost nothing and come with the
framework; row-level security is one setting, one migration and one database
role; the rest are on as soon as the plugin is.

What none of them catches: **a tenant id that is wrong but well-formed.** If
your own code maps a user to the wrong organisation, every layer will
faithfully enforce the wrong answer. That mapping -- where it lives, who can
change it -- deserves the most careful review in the service.

## Going multitenant later

Most services start with one customer, and should: tenancy you do not need is
configuration you have to keep consistent. What makes the later switch cheap is
decided on day one and costs nothing -- **keep the `tenant_id` column**. Every
generated entity carries `TenantMixin` (a nullable `tenant_id`) and
tenant-aware unique keys even in a single-tenant service, so going
multitenant is a data backfill, not a schema rewrite.

The rest is three tools, in the order you use them.

### Settings that contradict each other

Isolation is configured piece by piece, and every piece is valid on its own.
The failures are between them, and none stops the service from starting. The
`tenancy` check of `jfast check` reads them together:

| Code | Severity | What is wrong | The fix it names |
| --- | --- | --- | --- |
| `tenancy-rag-scoped-without-tenancy` | medium | `[plugin.rag] tenant_scoped` is true (the default) and the tenancy plugin is off: every rag call needs a tenant nothing resolves | `tenant_scoped = false` for one customer; `jfast tenancy enable` for several |
| `tenancy-budget-without-tenancy` | medium | `tenant_budget_usd > 0` with no tenancy: only the global cap ever applies | drop it and size `budget_usd`, or enable tenancy |
| `tenancy-rls-without-tenancy` | high (medium when an auth `tenant_id` claim could set it) | `rls = true` and nothing resolves a tenant: every policy table reads empty | `rls = false`, or enable tenancy |
| `tenancy-policies-without-rls` | high | a revision calls `enable_tenant_rls` and `rls = false`: no session sets the tenant, so those tables read empty for the service's own role | `rls = true`, or drop the policy |
| `tenancy-current-tenant-without-source` | high | code depends on `current_tenant` and there is neither a tenancy plugin nor an auth claim: every request gets 401/403 | `require_auth` for one customer; enable tenancy for several |
| `tenancy-source-unresolvable` | high | a source that can never answer here: `subdomain` without `base_domain`, `token`/`user` without the auth plugin | set the base domain, enable auth, or change the sources |

It reads the built plugins' settings when the plugin graph resolves, so an
override in the environment (`JFAST_RAG_TENANT_SCOPED=false`) is what is judged.

### What a switch would break: `jfast check --multitenant-ready`

A single-tenant service is right to assume one customer, and does, in places
nothing marks. This lists them with file and line, the way `jfast upgrade
--check` lists what an upgrade breaks:

```
  shop: what a switch to multitenant would break
  tenant tables: customers, invoices

  ✗ modules/invoice/api/routes.py:26  factory-without-tenant  [high]
      get_service() opens a database session with no tenant dependency;
      used by create_invoice(), delete_invoice(), get_invoice(),
      list_invoice() and 1 more
      → fix
        The generated factory reads `getattr(request.state,
        "tenant_id", None)`, and `None` builds a repository with no
        tenant filter. Take the tenant as a dependency ...
```

| Rule | Severity | Looks for |
| --- | --- | --- |
| `tenant-none-literal` | high | a call passing `tenant_id=None`: a repository, a facade, `rag`, `llm` |
| `route-without-tenant` | high | a route that opens a database session and has no tenant dependency |
| `factory-without-tenant` | high | the same, in a dependency (`get_service`), reported once with the routes that use it |
| `raw-sql-without-tenant` | high | an SQL string naming a tenant table and never `tenant_id` |
| `storage-key-without-tenant` | high | `storage.put(f"invoices/{id}.pdf", ...)`: a key built without the tenant |
| `cache-key-without-tenant` | high | `cache.get(f"report:{month}")`: a key built without the tenant |
| `rag-unscoped` | high | `[plugin.rag] tenant_scoped = false` |
| `scheduled-job-without-tenant` | medium | a task scheduled with `every=`/`cron=` (or `tasks.schedule`) that builds a `Job` with no `tenant_id` -- a tick runs as no tenant |
| `llm-call-without-tenant` | medium | `llm.chat(...)` with no `tenant_id`: no per-tenant budget applies |

**These are heuristics**, read from the source without importing it, and they
are built to stay quiet when they cannot decide rather than to guess:

- A tenant dependency is `current_tenant`, `TenantSession`, or an `Annotated`
  alias of either declared in the project. A dependency is followed into
  functions of the same file or the same `modules/<name>/`; one imported from
  elsewhere is not followed.
- A storage or cache call is recognised by its receiver's name (`storage`,
  `disk`, `cache`), and its key must *visibly* lack the tenant: a literal, an
  f-string, a `.format()`, or a local variable assigned one. A key that arrives
  as a parameter is not judged -- whoever built it is not in view.
- Raw SQL is a string literal (or f-string, or `+` of them) containing
  `SELECT`/`INSERT`/`UPDATE`/`DELETE` and a tenant table after `FROM`, `JOIN`,
  `UPDATE` or `INTO`, and no `tenant` anywhere. Docstrings are prose and skipped.
- Tenant tables are the models whose base closure includes `TenantMixin` or
  that declare `tenant_id`.
- Tests and `migrations/` are not read: a test passing `tenant_id=None`
  exercises single-tenant behaviour on purpose, and a revision is history.

What is reported and deliberate is waived inline with the comment
`jfast contracts check` already honours, on the line of the finding or on a
comment line directly above it:

```python
rates = await cache.get("fx:usd")  # contracts: allow exchange rates are global
```

A waived finding is listed as waived, with its reason, so the decision stays
reviewable. `--json` carries `findings`, `waived`, `tenant_tables` and the
`rules` table. Exit 1 while anything is left to fix, 0 when nothing is.

It is a flag of `jfast check` and not a check in its battery because its
findings are about a hypothetical: a correct single-tenant service would fail
`jfast check` forever. The flag replaces the battery with this one report.

### The switch: `jfast tenancy enable`

```bash
jfast tenancy enable --tenant acme --dry-run   # print everything, write nothing
jfast tenancy enable --tenant acme             # write the revision and jfast.toml
alembic upgrade head                           # the step that changes data
```

It writes **one Alembic revision**, on top of the current head, with a
statement per table a reviewer can strike out:

1. every NULL `tenant_id` becomes `--tenant` -- the customer served until now;
2. with `--not-null`, the column becomes NOT NULL (declare it on the models
   too, or the next autogenerate reverts it);
3. `enable_tenant_rls` on each table -- **after** its backfill, because
   `FORCE ROW LEVEL SECURITY` binds the migration's own role and a migration
   sets no tenant;
4. the RAG chunks table, when `[plugin.rag]` keeps chunks in pgvector: its NULL
   `tenant_id` re-keyed to the same tenant and the same policy applied, inside
   a `DO` block guarded on the table existing (the rag plugin creates it at
   startup, so a migrated service may not have it yet).

And it edits `jfast.toml` in place, keeping its comments: `tenancy` in
`[plugins].enabled`, `[plugin.tenancy] sources = ["token", "user"]` (or
`--sources`, with `--base-domain` for `subdomain`), `[plugin.database] rls =
true`, `[plugin.rag] tenant_scoped = true`.

Then it prints what it cannot do, in order: apply the revision as the tables'
owner; create the role the policies bind (the SQL above); decide who can see the
backfilled rows; fix what the readiness report still finds. It refuses, with
the fix in the message, when there is no revision yet, when the revisions have
several heads (`alembic merge heads`), when a switch revision already exists,
and when the chosen sources could never resolve (`token`/`user` without auth).

**Choosing `--tenant`.** Existing rows belong to it, so a request sees them only
when it resolves to it: a token whose `tenant_id` claim says so, or -- with the
`user` source -- the user whose id it is. If the app so far was one person's,
that person's user id is the right value.

The downgrade removes the policies and leaves the backfilled tenant in place: a
single-tenant service reads those rows with no filter either way, and guessing
which rows were NULL before would be a second, silent data change.

### Row-level security is the safety net

The readiness report is a list of heuristics and it will miss something. The
switch turns row-level security on so that what it misses fails *closed*: a raw
query nobody moved to the tenant returns no rows instead of another customer's,
a write for the wrong tenant is refused by the database. A visible bug, not a
leak. `tests/test_tenancy_enable_pg.py` proves it on a generated service, as a
role with neither SUPERUSER nor BYPASSRLS: after `jfast tenancy enable` and
`alembic upgrade head`, a second tenant reads zero rows from every table --
chunks included -- with no `WHERE` at all, its `UPDATE` and `DELETE` touch
nothing, and an `INSERT` claiming the first tenant's id fails on the policy.

### Behind PgBouncer

Transaction pooling hands one server connection to many clients, one
transaction at a time. What `tests/test_rls_pgbouncer.py` verifies there
(PgBouncer 1.25, `pool_mode = transaction`, `default_pool_size = 1`, so every
transaction is on the same backend):

- **The tenant is transaction-local.** After a transaction for `acme`, the next
  client's transaction on the same server connection reads
  `current_setting('jfast.tenant_id', true)` as empty and sees no rows.
- **Interleaved tenants never see each other.** Fifty concurrent transactions,
  two tenants, one backend: each sees only its own rows, and a tenant-less
  transaction afterwards sees none.
- **Row-level security needs nothing from the pooler.** No `SET`, no
  `server_reset_query`: `set_config(..., true)` ends with the transaction.

**asyncpg needs one setting.** asyncpg caches prepared statements per client
connection; behind a transaction pool the next transaction may run on another
server connection, where the statement was never prepared:

```
prepared statement "__asyncpg_stmt_7__" does not exist
```

Measured: with `max_prepared_statements = 0` and more than one server
connection in the pool, eight concurrent workers fail within twenty
transactions on asyncpg's defaults. The fix is one setting:

```toml
[plugin.database]
pgbouncer = true   # per connection: [plugin.database.connections.x] pgbouncer = true
```

It sets `statement_cache_size = 0` (asyncpg), `prepared_statement_cache_size =
0` (SQLAlchemy) and a unique name per statement, so two processes sharing one
server connection cannot collide on `__asyncpg_stmt_1__` either. On PgBouncer
1.21+ with `max_prepared_statements > 0` (200 by default in the 1.25 we ran)
asyncpg's defaults also worked in the same run, because PgBouncer tracks the
statements itself; the setting is harmless there and correct where tracking is
off.

**Startup parameters.** The database plugin pins `timezone` as a startup
parameter, which PgBouncer forwards natively. A read replica's
`default_transaction_read_only` is not one PgBouncer knows: the connection is
refused with `unsupported startup parameter` unless PgBouncer (1.20+) has
`track_extra_parameters = default_transaction_read_only` -- verified: two
clients, one read-only, alternating on one backend, each saw its own value.
Do not put it in `ignore_startup_parameters`: that drops the replica guard
silently.

CI, for GitHub Actions (the two URLs the test reads):

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    env: { POSTGRES_USER: jfast, POSTGRES_PASSWORD: jfast, POSTGRES_DB: jfast }
    ports: ["5499:5432"]
    options: >-
      --health-cmd "pg_isready -U jfast" --health-interval 5s --health-retries 10
  pgbouncer:
    image: edoburu/pgbouncer:latest   # pin the tag you verified
    env:
      DB_HOST: postgres
      DB_PORT: "5432"
      DB_NAME: jfast
      DB_USER: jfast_bouncer          # created by the test, NOSUPERUSER NOBYPASSRLS
      DB_PASSWORD: jfast_bouncer
      AUTH_TYPE: scram-sha-256
      POOL_MODE: transaction
      DEFAULT_POOL_SIZE: "1"          # every transaction on one backend
      MAX_PREPARED_STATEMENTS: "0"    # the strict case
    ports: ["6435:5432"]
env:
  JFAST_TEST_PG_URL: postgresql+asyncpg://jfast:jfast@localhost:5499
  JFAST_TEST_PGBOUNCER_URL: postgresql+asyncpg://jfast_bouncer:jfast_bouncer@localhost:6435/jfast
```

The same environment variables were checked against a local container. The
PgBouncer image connects to PostgreSQL when the first client logs in, which is
after the test has created the role, so no ordering is needed between the two
services. `JFAST_TEST_PGBOUNCER_MULTI_URL` -- a PgBouncer database with several
server connections and `max_prepared_statements = 0` -- additionally runs the
test that reproduces the asyncpg failure and shows the setting fixing it.

## See also

- [Authentication](auth.md) — the `tenant_id` claim
- [Accounts](accounts.md) — users and sign-up, with the `user` source
- [RAG and vector search](rag.md) — tenant-scoped retrieval
- [Language models](llm.md) — per-tenant budgets
- [Deployment](deploy.md) — Caddy as the edge
