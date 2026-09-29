# Choosing datastores

Every datastore is a plugin. Enable what the service needs, and the client, the
health check and the container all arrive together.

```toml
[plugins]
enabled = ["observability", "database", "cache", "qdrant", "mongo"]
```

| Plugin | Store | Provides | Extra | Port offset |
| --- | --- | --- | --- | --- |
| `database` | PostgreSQL (+pgvector) | `db.engine`, `db.sessionmaker`, `db.databases` | `[db]` | +1 (and +2, +5, +6 for further instances) |
| `cache` | Redis | `cache`, `cache.client` | `[cache]` | +3 |
| `mongo` | MongoDB | `mongo.client`, `mongo.db` | `[mongo]` | +4 |
| `qdrant` | Qdrant | `qdrant.client` | `[qdrant]` | +7 (HTTP), +8 (gRPC) |

Running more than one is normal. Relational data with foreign keys belongs in
PostgreSQL; chat histories and scraped payloads are happier in Mongo. The
mistake is adopting a second store before the first one stops being enough —
each one is another thing to back up, monitor and restore at 3am.

---

## Named database instances

The `database` plugin used to hold one DSN. That single field is why there was
no read replica, no per-tenant database and no shard — not four missing
features, one missing structure: a database the service can **name**.

```toml
[plugin.database.connections.primary]
dsn_env = "JFAST_DB_DSN"

[plugin.database.connections.replica]
dsn_env = "JFAST_DB_REPLICA_DSN"
read_only = true
pool_size = 4
```

**Leaving `connections` out is the same thing, spelled shorter.** No block
means one instance called `default`, configured by the fields above it, reading
`JFAST_DB_DSN`. Every existing project, every generated template and
`ctx.require("db.engine")` keep working with nothing to change.

| Setting | Per connection | Default |
| --- | --- | --- |
| `dsn` / `dsn_env` | yes | `JFAST_DB_DSN`, else `JFAST_DB_<NAME>_DSN` |
| `read_only` | yes | `false` |
| `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`, `pool_pre_ping` | yes | the plugin-level value |
| `include_infra`, `image`, `port_offset`, `database`, `user` | yes | the plugin-level value |

`dsn_env` is a variable *name*, never a value. A connection with neither `dsn`
nor `dsn_env` reads `JFAST_DB_<NAME>_DSN`, so a `replica` connection needs no
line at all to find `JFAST_DB_REPLICA_DSN`.

### Which one is "the" database

`db.engine` and `db.sessionmaker` still mean the writable instance: the one
named `default`, or `primary`, or the first that is not `read_only`. Set
`default_connection` to say so explicitly. A configuration where every
connection is `read_only` is refused at boot — nothing in that service could
write, and finding out at the first `POST` is finding out too late.

The whole map is published as `db.databases`:

```python
databases = ctx.require("db.databases")
databases.names            # ("primary", "replica")
databases.engine("replica")
databases.sessionmaker()   # the default instance
```

`jfast describe --json` lists them, with the variable each one reads and never
its value, plus `max_connections` — the number of server connections one
process can open across every instance. That number is what has to fit under
PostgreSQL's `max_connections`, and it is the one people find out about during
an incident.

### Containers

Each instance with `include_infra` declares its own container, its own volume
and its own password variable:

```
postgres          8011:5432   POSTGRES_PASSWORD           postgres_data
postgres-replica  8012:5432   POSTGRES_REPLICA_PASSWORD   postgres_replica_data
```

One shared password would make a leak anywhere a leak everywhere. A service
owns ten ports and the other plugins already claim some (cache +3, mongo +4,
qdrant +7 and +8, gRPC +9), so databases get +1, +2, +5 and +6 — ask for a
fifth and the generator says so instead of colliding. Set
`include_infra = false` for an instance managed elsewhere, which is the normal
case for a cloud replica.

Kubernetes generates no database, on purpose (see [Kubernetes](kubernetes.md)),
but each bound instance reaches the pod as its own `secretKeyRef`:
`JFAST_DB_DSN` from `db-dsn`, `JFAST_DB_REPLICA_DSN` from `db-replica-dsn`.

---

## Read/write split

```toml
[plugin.database]
read_write_split = true
```

Reads go to a `read_only` instance, writes to the primary:

```python
from jfastframework.plugins.builtin.database import DbSession, ReadSession

@router.get("/invoices")
async def list_invoices(session: ReadSession):
    ...

@router.post("/invoices")
async def create_invoice(session: DbSession):
    ...
```

`DbSession` and `ReadSession` are `session_dependency` and
`read_session_dependency` with `scope="function"` already applied, which is
what makes the commit happen before the response is sent -- see
[Transactions](transactions.md). With no replica configured, `ReadSession` is
the same database as `DbSession`. Use it everywhere from the start and the split arrives
later as one configuration block.

### The part that is not optional: pinning

**A replica is behind.** Save a row, redirect, read from the replica, and the
row is not there yet. It is an intermittent 404 that appears under load and
never reproduces on a laptop, because a laptop has no replica. No flag fixes
it: a split without a pin is a bug you have shipped, not a feature.

So after a write, that client's reads go to the **primary** for `pin_window`
seconds (default 5).

**How the pin travels.** As a token the client carries — a `jfast_rw` cookie
and an `X-JFast-Read-Pin` header — not as an entry in a table in this process.
A redirect can land on any replica of the service, and a pin the next process
cannot see is a pin that silently is not there. Browsers carry the cookie for
free; an API client that keeps no cookies echoes the header.

**Why trusting a client-controlled value is safe here.** The only direction the
client can push is *towards the primary*, which is never stale. The cost of a
forged value is primary capacity, so the value is clamped to `pin_window` from
now: nobody can pin themselves permanently.

**What triggers the pin.** An unsafe method (`POST`, `PUT`, `PATCH`, `DELETE`)
that answered below 400. This has to be decided *before* the handler returns: a
session commits during dependency teardown, which runs after the response
headers are already on the wire, so a commit cannot be what sets the cookie.
For the rare write behind a `GET` — a lazy upsert, a counter — say so:

```python
from jfastframework.plugins.builtin.database import mark_write

@router.get("/reports/{id}")
async def report(request: Request, session: DbSession):
    await touch_last_seen(session, id)
    mark_write(request)
```

The approximation costs a `POST` that read nothing one window of pinned reads.
That is load, not incorrectness, and `pin_on_unsafe_methods = false` turns it
off for a service that marks its writes by hand.

**Why five seconds, and what would be better.** A healthy standby on the same
network is milliseconds behind; five seconds still covers a checkpoint spike or
a stalled WAL sender, and pinning one client for five seconds after a write is
negligible on a read-heavy workload. The exact answer is LSN-based: record
`pg_current_wal_lsn()` on the write, compare it with the replica's
`pg_last_wal_replay_lsn()`, and stop pinning the moment the replica has caught
up. That costs a round trip per read and only works against a real physical
standby, so it is the upgrade path rather than the default.

**Writes cannot reach a replica.** Two guards, because one of them cannot see
raw SQL. A read session refuses to end with pending ORM changes
(`ReadOnlySessionError`), and every `read_only` asyncpg connection sets
`default_transaction_read_only = on`, so an `INSERT` smuggled through
`session.execute(text(...))` is refused by PostgreSQL itself.

### Sharding is not this

Named instances are what a shard map will be built *on* — key resolution,
per-shard migrations and cross-shard queries are their own piece of work, and
none of it was expressible while the plugin held one DSN. It is not built.

A database per tenant is: see [Multi-tenancy](multitenancy.md).

---

## Pagination

`BaseRepository` pages three ways, and the choice is about what the response
costs, not about style.

| Call | Cost | Gives you |
| --- | --- | --- |
| `paginate()` | one `COUNT` + one `LIMIT`/`OFFSET` | exact `total`, random access |
| `paginate(with_total=False)` | one `LIMIT limit + 1` | `has_more`, random access, no `COUNT` |
| `paginate_keyset(after=...)` | one range scan | `has_more` + `next_cursor`, flat cost at any depth |

`OFFSET n` makes the database walk and discard n rows before returning
anything, so page 200 costs 200 pages of work. A keyset page is a range scan
from a known point and costs the same wherever it lands. The trade is random
access: there is a next page, not a page 40.

```python
class MessageRepository(BaseRepository[Message]):
    model = Message
    order_by = ("-edited_at",)


page = await repository.paginate_keyset(limit=50)
while page.has_more:
    page = await repository.paginate_keyset(limit=50, after=page.next_cursor)
```

### Nullable ordering columns

`edited_at`, `last_message_at`, `archived_at` — the columns a feed sorts by are
usually the ones that are NULL until something happens. That is supported, and
it is worth knowing what the framework does about it, because the naive version
of keyset paging **loses rows without saying so**.

Two facts collide. NULL compares UNKNOWN against everything, including itself,
so a cursor holding a NULL matches no row at all: the next page comes back
empty, `has_more` is `False`, and the walk reports the table finished. And the
backends disagree about where the NULL block even sits — PostgreSQL sorts NULLs
last ascending and first descending, SQLite sorts them first either way — so the
same code loses a different set of rows on each. On a 200-row table with 40
NULL sort keys, that was 180 rows unreachable on SQLite and 40 on PostgreSQL,
with no error either time.

So every ordering the repository builds pins the NULL block to the end:

```sql
ORDER BY messages.edited_at DESC NULLS LAST, messages.id DESC
```

and every keyset comparison is written against that pinning — a tie on a NULL
is `IS NULL`, and a step past a real value also admits the NULL block behind
it. A cursor whose first value is `None` is a legitimate cursor pointing into
that block, so **whatever encoding you put a cursor through for a URL has to
survive a `None`**; JSON does, a naive `",".join(...)` does not.

Two consequences worth stating:

- **`NULLS LAST` is PostgreSQL, SQLite ≥ 3.30 and Oracle.** MySQL, MariaDB and
  SQL Server reject the syntax outright. JFastFramework targets PostgreSQL and
  is tested against SQLite, so both are covered; a third backend is not a
  configuration change here.
- **Only nullable columns get the clause.** A `NOT NULL` ordering column keeps
  the plain `ORDER BY c DESC`, because `DESC NULLS LAST` cannot be answered by
  a plain descending btree index — PostgreSQL defaults that index to
  `NULLS FIRST` — and would buy a sort in exchange for a guarantee the column
  already gives.

The alternative considered was refusing the query: raise when an ordering
column is nullable. It is a smaller change and it turns data loss into a loud
error, but it refuses "newest edits first", which is not a mistake anyone is
making. Returning a silent subset was never an option.

### What correctness costs here

`paginate_keyset` is flat-cost — the same work at page 2 and page 2,000 —
**while the ordering columns are NOT NULL**. A nullable one gives that up. The
predicate grows an `OR c IS NULL` disjunct, and PostgreSQL cannot turn a
disjunction into an index range: it demotes the range scan to an index scan
with a filter and starts reading from the beginning of the index again.

Measured on 200k rows, an index on `(edited_at, id)`, one 50-row page at depth
100k:

| Ordering column | Plan | Buffers |
| --- | --- | --- |
| `NOT NULL` | `Index Cond: ROW(edited_at, id) > ROW(...)` | 4 |
| nullable, cursor inside the NULL block | `Index Cond: edited_at IS NULL AND id > ...` | 5 |
| nullable, cursor on a real value | `Filter: ... OR edited_at IS NULL`, 80k rows discarded | 840 |
| the same page by `OFFSET` | index scan, 100k rows discarded | 1049 |

So it is still the cheapest of the three and it is no longer flat. If that
matters more than the convenience, make the column `NOT NULL` with a sentinel
(`edited_at DEFAULT created_at`) and the range scan comes back. Splitting the
scan into the non-NULL range plus the terminal NULL block would recover it
without the sentinel; that is not built.

### Ties are not the same problem

`paginate_keyset` appends the primary key to the ordering, so the sort is
total and no row can straddle a page boundary. `paginate` does not: it orders
by `order_by` alone, so rows that tie on the sort key come back in whatever
sequence the planner picked, and the NULL block is one large tie. Membership is
still correct — every row is on exactly one page — but if you need the sequence
inside a tie group to be stable across requests, put a unique column in
`order_by` yourself.

---

## Cache

`get_or_set` is the read path. Everything else on the facade is a primitive
you reach for when read-through is the wrong shape.

```python
cache = ctx.require("cache")

report = await cache.get_or_set(
    f"report:{tenant_id}",
    lambda: build_report(tenant_id),   # any zero-argument coroutine function
    ttl=300,
)
```

It does three things `get` + `set` by hand does not.

**It survives the cache being down.** Every backend failure inside
`get_or_set` degrades to calling the loader. A Redis restart costs those
requests a recomputation, not a 500. This is what makes the plugin's
`health_critical=False` true rather than aspirational — and it is true *only
on this path*:

| Call | Redis unreachable |
| --- | --- |
| `get_or_set(...)` | returns the loader's value |
| `get` / `set` / `delete` / `exists` / `publish` | raises |

The primitives raise on purpose. A service that cannot tell "nothing cached"
from "Redis is gone" serves stale answers forever and nobody finds out. If you
call them directly on a request path, you own the `try/except`.

Errors raised by the *loader* always propagate. Degrading past a cache outage
is the point; degrading past a broken query is how a service returns wrong
answers quietly.

**It collapses concurrent misses.** When a hot key expires under load, the
naive read-through sends every in-flight request to the database at once. Here
the first caller to miss takes a short Redis lock and recomputes; the others
poll for its result for up to `stampede_wait` and then give up and load for
themselves.

The trade, stated plainly: the lock costs one extra round trip on every miss,
and a caller that loses the race waits up to `stampede_wait` before falling
back. The bound is what makes it safe — a stalled loader costs duplicated
work, never a queue of stalled requests.

The alternative, recomputing early before the TTL expires, avoids the round
trip but needs every value wrapped in an envelope carrying its logical expiry.
That changes what is stored, and this Redis is routinely shared with something
that is not a JFast service. Keeping cached values plain JSON documents was
worth more than the round trip.

```toml
[plugin.cache]
stampede_wait = 2.0      # 0 turns the lock off entirely
stampede_lock_ttl = 10   # ceiling on how long one caller may hold it
```

**It is counted.** When the `metrics` plugin is enabled, the cache registers
four counters on the shared registry and `/metrics` serves them with
everything else:

| Counter | Meaning |
| --- | --- |
| `cache_hits_total` | reads served from the cache |
| `cache_misses_total` | reads that found nothing stored |
| `cache_errors_total` | operations the backend refused |
| `cache_stampede_suppressed_total` | loader runs skipped by waiting for another caller |

`metrics` is a soft dependency (`after`, not `requires`): a service that wants
a cache should not have to carry `prometheus-client` to get one. Disable
`metrics` and the counters silently become no-ops.

### TTL

`ttl=None` means "use `default_ttl`", so it cannot also mean "never expire".
`ttl=0` is that escape hatch:

```python
await cache.set("feature-flags", flags, ttl=0)   # until something deletes it
```

A negative `ttl` raises `ValueError` at the call site rather than at the round
trip, so the message can name the key.

### Two things that look like bugs and are not

**`publish` does not apply the key prefix.** Every other method namespaces its
key; channel names are a contract with whoever else is on this Redis — often a
Laravel app that never heard of our prefix. Silently renaming the channel
would break the interop the shared Redis exists for. Namespacing channels is
the `channels` plugin's job, under its own `prefix` setting.

**`get` returns the raw string when the value is not JSON.** Same reason: this
service is not the only writer. A key another system wrote is worth returning
as a string, not worth raising over.

---

## Vector search: pgvector or Qdrant

The `rag` plugin talks to a `VectorStore` protocol, so the backend is one line
of config. How to ingest, search and answer with it -- tenants, hybrid search,
incremental ingest -- is in [RAG and vector search](rag.md); this is only the
choice of store.

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"       # the default
collection = "rag_chunks"
dimensions = 1536
```

Switching to Qdrant:

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
```

The `rag` service, the `SearchHit` shape and the scores do not change -- each
store normalises to cosine similarity in [0, 1]. What does change: Qdrant has
no hybrid (full-text + vector) search here, and the service falls back to
vector search.

### Which one

| | pgvector | Qdrant |
| --- | --- | --- |
| Operational cost | none — it is the database you already run | a second service to run and back up |
| Scale | comfortable to a few million chunks | far beyond that |
| Hybrid search | yes, full text + vectors | no (needs sparse vectors) |
| Row-level security | yes, like any table | no — the store's filter is the only one |
| Filtering | tenant, documents, metadata | tenant, documents, metadata, indexed |
| Quantisation | no | yes |

**Start with pgvector.** Move to Qdrant when you hit a specific wall you can
name — filter complexity, index build time, memory. "It might scale better" is
not that wall.

### Picking the wrong one fails loudly

Choosing `pgvector` without the `database` plugin, or `qdrant` without the
`qdrant` plugin, raises at startup with the fix in the message:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

Not at the first search, in production, on a Friday.

A custom store -- Weaviate, pgvecto.rs, anything -- implements the protocol
described in [RAG: a custom store](rag.md#a-custom-store).

---

## Embedding dimensions

The mistake that costs an afternoon: `dimensions` must match the embedding
model. `nomic-embed-text` is 768; `mxbai-embed-large` is 1024;
`text-embedding-3-small` is 1536. The plugin compares the embedder's
`dimensions` with the table's at startup, but changing it later still means
re-embedding every document you have already ingested. Decide it before the
first ingest.

---

## Enums: which half of the guarantee you are buying

`Enum` looks like one decision and is two. Both answers matter, and the
defaults do not give you the pair most people assume.

```python
class Status(enum.Enum):
    pending = "pending"
    done = "done"


status: Mapped[Status] = mapped_column(Enum(Status, native_enum=False))
```

That column is a plain `VARCHAR`. **No `CHECK` constraint is created.**
`create_constraint` has defaulted to `False` since SQLAlchemy 1.4, and
`native_enum=False` only turns off the PostgreSQL `ENUM` type — it does not
put anything in its place. Nothing but the application stops a bad value:

```sql
-- with native_enum=False and nothing else
CREATE TABLE t (s VARCHAR(7))
```

The three options, and what each one costs:

| | Storage | Invalid value can be written | Adding a member |
|---|---|---|---|
| `Enum(Status)` (default) | native `status_enum` type | no — the server rejects it | `ALTER TYPE ... ADD VALUE`, a migration |
| `Enum(Status, native_enum=False)` | `VARCHAR(n)` | **yes** — by any client but the ORM | nothing; deploy the code |
| `Enum(Status, native_enum=False, create_constraint=True)` | `VARCHAR(n)` + `CHECK` | no | a migration to rewrite the `CHECK` |

```sql
-- with create_constraint=True
CREATE TABLE t (s VARCHAR(7), CONSTRAINT status_enum CHECK (s IN ('pending', 'done')))
```

**Use `native_enum=False` and no constraint** for a set that is still moving —
statuses, kinds, anything a product decision changes. Adding a member is a code
deploy and nothing else, which is the whole reason to give up the native type.
Accept in exchange that a psql session, a bulk `COPY`, or another service on
the same database can write `"pendign"` and the column will take it. Validate
at the edge, where the value arrives, and treat unknown values as a real case
when reading — a `ValueError` out of `Status(row.status)` is a 500 that reads
like a bug in the wrong place.

**Add `create_constraint=True`** when something other than this service writes
the table, or when a wrong value is a correctness problem rather than a display
one. You pay a migration per member, same as the native type — but a `CHECK` is
cheaper to change than a PostgreSQL `ENUM`, which cannot drop a value at all.

Note that autogenerate does not reliably notice enum membership changes in
either direction. Whichever you pick, write that migration by hand.

---

## Deployment

Enabled datastore plugins contribute their containers to the generated compose
file automatically:

```bash
jfast deploy compose --stdout
```

Disable `cache` and the Redis container is gone from the next generation. That
is the point of deriving infrastructure from the plugin graph rather than
maintaining it alongside.

Secrets stay in the environment. The generated compose references them with
compose's fail-fast form, so a missing password stops the stack instead of
starting PostgreSQL wide open:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```
