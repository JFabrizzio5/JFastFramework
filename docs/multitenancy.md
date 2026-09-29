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
| `subdomain` | your DNS and TLS | medium |
| `path` | the URL | low |
| `header` | whoever sent the request | **none** |

`header` exists because it is genuinely useful in development and in tests. It
is not in the default list, and enabling it in production logs a warning,
because `X-Tenant-ID: acme` is one `curl` away from another tenant's data.

A signed claim always outranks the hostname. Someone who points `acme.` at your
IP has not become Acme; someone holding a token your identity provider signed
for Acme has.

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
request, and safe behind PgBouncer in transaction mode.

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

## See also

- [Authentication](auth.md) — the `tenant_id` claim
- [Deployment](deploy.md) — Caddy as the edge
