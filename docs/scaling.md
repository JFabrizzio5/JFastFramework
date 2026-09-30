# Scaling

Every claim about capacity on this page is a number from a run, with the
machine, the sizes and the method next to it. Where a number would only be a
guess, the page says so instead.

**Where these were measured:** Apple M5 laptop (10 cores, 16 GB), macOS,
Python 3.12.14, FastAPI 0.142.1. PostgreSQL 16.15 with pgvector 0.8.6 and
Redis 7 in Docker Desktop (a VM with 10 CPUs and 8 GB, shared with about 25
other containers). Other test suites were running on the same machine (load
average 4 to 12), which is why the framework's own numbers below are CPU time
rather than wall-clock time. Your hardware will give other absolute numbers;
the ratios and the shapes are what carry over.

## What a request costs, and the budget that keeps it there

`scripts/bench_overhead.py` drives each app's ASGI callable directly -- no
socket, no server -- and measures the **process CPU time** per request of the
same `GET /ping` in four apps:

| Scenario | CPU us / request | x FastAPI |
| --- | --- | --- |
| FastAPI alone | 20.6 | 1.00 |
| FastAPI + JWT and tenant by hand (`async def` dependency) | 70.5 | 3.26 |
| JFast, default plugins (observability, metrics), logs at `WARNING` | 53.2 | 2.48 |
| JFast + auth + tenancy + metrics, logs at `WARNING` | 114.3 | 5.43 |

(9 rounds of 5,000 requests; the scenarios take turns in slices of 200
requests; microseconds are the median of the per-round means, the ratio is
the median of the per-slice ratios.)

Why in-process and why CPU time: `ab` against uvicorn measures sockets and
HTTP parsing too, and on a shared runner that part moves by tens of percent
between two runs of the same commit. A budget that fails on noise gets turned
off. CPU time ignores the minutes another process held the core, and still
counts a threadpool hop -- one of the two regressions the budget exists for.
Why slices: Apple silicon moves a process between fast and slow cores; a
ratio inside 200 requests compares like with like. Three runs of the check on
a loaded machine gave ratios within 1.5 % of each other.

**The budget.** `tests/test_performance_budget.py` runs the same measurement
and fails if a budgeted ratio (`jfast_defaults`, `jfast_auth_tenancy_metrics`)
grows more than 20 % over `tests/performance_baseline.json`. It also proves it
has teeth: putting one `BaseHTTPMiddleware` back -- the 0.1.0a10 regression --
fails it. It is off by default, because it measures:

```bash
JFAST_PERF_BUDGET=1 pytest tests/test_performance_budget.py
```

The baseline holds one entry per platform (`darwin-arm64`, `linux-x86_64`...)
because ratios differ between CPU families; a platform without an entry skips
and says how to record one. To accept a change that costs more on purpose,
re-record and commit the file with the reason in the commit message:

```bash
python scripts/bench_overhead.py --write-baseline tests/performance_baseline.json
```

In CI the robust comparison is against the base branch on the same runner:
run the script on the base commit with `--json > base.json`, then the test
with `JFAST_PERF_BASELINE=base.json`. A bare result file is accepted as a
baseline for exactly this.

**Framework routes go last.** Starlette tries routes in order, and
`/health`, `/ready`, `/info`, `/metrics` and the docs used to be registered
first, so every request to an application route failed to match each of them
before reaching its own. They now move behind the application's routes at the
end of startup. Measured with the same method, two copies of the default app
differing only in route order: **2 to 4 us of CPU saved per application
request** (of about 43). Nobody's answer changes: each moved route is probed
with its own path, and when an application route would claim it -- a
`/{slug}` catch-all, or a `PUT /{slug}` that would turn `POST /health` into
its own 405 -- the framework route goes back in front of that route. Routes
with parameters are never moved, because no probe can prove moving them
harmless. `tests/test_route_order.py` holds all of this.

## Load-testing a running service: `jfast bench`

```bash
jfast bench http://localhost:8000
jfast bench http://localhost:8000 -r "GET /invoices" -r "GET /invoices/42" \
    -c 1,8,32,128 -d 15 --token "$TOKEN"
jfast bench http://localhost:8000 --k6 load.js --json --fail-on-break
```

It reads the service's `/openapi.json` and loads every `GET` whose parameters
it can fill from an example, a default or an enum; `--route` picks routes
instead (a template from the schema, or a literal path). Writes are included
with `--method POST` only when the schema has an example body -- invented
bodies measure the 422s. Each step holds N concurrent clients for
`--duration` seconds and reports:

```text
clients     req/s   p50 ms   p95 ms   p99 ms  errors  non-2xx  ready
      1     1,720      0.5      1.1      1.8    0.0%        0  ok
      8     2,160      3.0      7.2     13.9    0.0%        0  ok
     32     2,806     10.3     18.2     31.2    0.0%        0  ok
     64     3,035     18.9     33.1     50.9    0.0%        0  ok

Did not break up to 64 clients (p99 <= 500 ms, errors <= 1.0%).
Throughput stops growing past 32 clients: size replicas from there.
The load generator used a whole core in some steps: ...
```

- **Where it breaks:** the first step whose p99 exceeds `--max-p99-ms` (500)
  or whose error rate exceeds `--max-error-rate` (1 %). Errors are 5xx, 429
  and transport failures; a 401 or 404 is the scenario's fault and is counted
  as non-2xx instead.
- **Where it saturates:** the step after which more clients bought less than
  10 % more throughput. That is the number to size replicas by.
- **Which dependency gave:** after each step `/ready` is read, and any check
  that is not `ok` is printed next to the step.
- `--k6 load.js` writes the same scenario as a k6 script (`ramping-vus`, one
  stage per step, the thresholds as k6 thresholds); the token is read from
  k6's `TOKEN` variable, never written into the file.

**Its limit, measured:** one Python process drives 3,000 to 3,600 requests a
second (the run above, against a bare FastAPI `/ping`, where `ab` did 21,000
against the same server). The report says so when the generator's own CPU is
the bottleneck; for more load, export to k6. Each simulated client has its
own httpx client: one shared pool drove 430 req/s with 32 clients against the
1,900 of a single client, which is contention in the pool, not the service.

## Several replicas

`tests/test_multi_replica.py` runs two replicas -- each with its own engine,
its own Redis connection, its own relay, scheduler, LLM client or app, sharing
nothing in memory -- against one PostgreSQL and one Redis:

| What | Proved | How |
| --- | --- | --- |
| Outbox relay | 400 messages written through both replicas, two relays and two workers: each relayed once (the relays' counts add to 400, nothing left pending), each consumed once. Both relays and both workers took part. | `FOR UPDATE SKIP LOCKED` on the outbox rows |
| Scheduler | 39 ticks of a one-minute schedule, both replicas passing at the same instants: exactly one enqueue per tick, on the SQL tick store and on the Redis one (the Redis queue does not deduplicate job ids, so a double enqueue would show as a 40th job) | a claim per tick in a shared store |
| LLM budget | 60 concurrent calls of $0.10 across both replicas against a $1.00 cap: exactly 10 sent, 50 refused, the ledger ends at $1.00. A tenant cap of $0.30: exactly 3 per tenant. | `RedisLedger`: reserve atomically, roll back on refusal, settle to the real cost |
| Token revocation | A logout on replica A is refused on replica B, access and refresh token alike. The same refresh token sent to both at once rotates once; the loser gets 401 and the winner's new pair works on both. | `RedisTokenStore` and its rotate script |

What is **not** promised, so it is not tested as if it were: delivery stays
at least once (a relay can publish and die before marking the row), so
consumers deduplicate with `claim_once`; a scheduler killed between claiming a
tick and enqueueing it loses that tick when the claim store and the queue are
different systems; the in-memory token store and ledger are per process by
design, and the health checks say so.

**Files:** a local disk is per replica. Before a second replica, move the
disks that matter to S3 -- see the next section.

## Files on S3, verified against MinIO

`tests/test_storage_minio.py` runs the S3 disk against a real S3 API. It is
skipped without these variables:

```bash
docker run -d -p 9010:9000 -e MINIO_ROOT_USER=jfastminio \
    -e MINIO_ROOT_PASSWORD=jfastminio-secret minio/minio server /data
JFAST_TEST_S3_URL=http://localhost:9010 JFAST_TEST_S3_ACCESS_KEY=jfastminio \
JFAST_TEST_S3_SECRET_KEY=jfastminio-secret pytest tests/test_storage_minio.py
```

Covered: put, get, stat (content type, ETag, metadata), exists, delete (and
its "was it there"), listing by prefix and limit, `temporary_url` fetched with
httpx and then refused once expired, the unsigned URL refused, `upload_url`
accepting a direct PUT, `put_stream` as a real three-part multipart upload
(its ETag ends in `-3`), a short stream as a single PUT, a stream that fails
part way and one that is cancelled both aborted with no parts left behind, and
the health check. It found one bug, fixed: S3 answers at most 1,000 keys per
request, and `listing(limit=1500)` silently returned 1,000. It now follows the
continuation token. Never point these tests at real S3.

## RAG at scale

`scripts/bench_rag.py` drives `PgVectorStore`, the store the `rag` plugin
uses, with synthetic embeddings (a few topic centroids per tenant, plus
noise; uniform random vectors would be HNSW's worst case and no corpus looks
like that). 384 dimensions, 20 chunks per document, 8 documents written at
once, `hnsw.ef_search = 100` unless stated. 1M chunks were not run: the plan's
target, but 300,000 already took 6.5 minutes and 1.5 GB here.

**300,000 chunks, 1,000 tenants (300 each):**

| | |
| --- | --- |
| Ingest, HNSW index in place | 1,424 chunks/s |
| Ingest, no HNSW index | 3,036 chunks/s |
| HNSW build, 300,000 chunks | 257.6 s (serial, `maintenance_work_mem` 1 GB) |
| Table + TOAST / indexes / total | 866 MB / 683 MB (HNSW 586, full-text GIN 62) / 1,548 MB |

| Search, 10 results | p50 | p95 | p99 | queries/s at 16 at once |
| --- | --- | --- | --- | --- |
| Vector (tenant filter always on) | 2.5 ms | 5.1 ms | 6.9 ms | 1,037 |
| Vector + metadata filter | 6.5 ms | 9.0 ms | 16.5 ms | 365 |
| Hybrid (vector + full text, fused) | 3.0 ms | 4.4 ms | 6.4 ms | 1,349 |

Recall@10 against an exact scan: **1.0** -- because with 300 chunks per
tenant the planner never uses the HNSW index. It reads the tenant's rows by
the `(tenant_id, document_id)` btree and sorts them exactly. The 586 MB HNSW
index costs memory and halves the ingest rate without serving these queries.

**100,000 chunks, 4 tenants (25,000 each)** -- the planner walks the HNSW
graph and filters by tenant (pgvector's iterative scan):

| `ef_search` | Vector p50 / p99 | + metadata p50 / p99 | Hybrid p50 / p99 | Recall@10 |
| --- | --- | --- | --- | --- |
| 100 (default) | 3.1 / 11.0 ms | 20.4 / 66.1 ms | 4.1 / 13.1 ms | 0.918 |
| 200 | 3.3 / 7.1 ms | 19.0 / 47.9 ms | 5.6 / 12.4 ms | 0.950 |
| 400 | 3.7 / 6.4 ms | 30.4 / 84.5 ms | 9.6 / 67.8 ms | 0.966 |

Ingest 2,180 chunks/s with the index, 3,599 without; build 36.9 s; 522 MB in
total. What this means for a deployment:

- **Hybrid search costs little** next to vector search here: 0.4 to 1 ms at
  p50.
- **A tenant filter on a shared HNSW index loses recall** once tenants are big
  enough for the planner to use it: 8 % of the true top 10 missing at the
  default `ef_search`. Raise `hnsw_ef_search` for large tenants, or give them
  their own partial index or partition.
- **Build the index after a bulk load**, not before: ingest is 1.6 to 2.1
  times faster without it. A parallel build keeps the graph in shared memory,
  which in a container is `/dev/shm`; the generated compose sets
  `shm_size: 1gb`, and `maintenance_work_mem` above it fails the build with
  "could not resize shared memory segment".
- **Known problem, not fixed here:** the store's writes find a document's
  rows with `tenant_id IS NOT DISTINCT FROM`, which no btree serves. At
  300,000 chunks one document's DELETE took 19.2 ms instead of 0.03 ms, and it
  grows with the table. `tests/test_rag_scale.py` asks the planner and is a
  strict expected failure until the store is fixed.

## Aggregates that stay fast with data

A dashboard that sums a tenant's expenses by category for this month, the
last twelve months and the year runs those aggregates on every request.
`jfastframework.db.rollups.MonthlyRollup` keeps them in a table of their own,
one row per tenant, local month and group:

```python
from sqlalchemy import Integer, Numeric, String, func
from jfastframework.db.rollups import MonthlyRollup, rollup_table
from jfastframework.time import today

expenses_monthly = rollup_table(
    "expenses_monthly", Base.metadata,
    group_by={"category": String(64)},
    measures={"total": Numeric(14, 2), "count": Integer},
)
rollup = MonthlyRollup(
    source=Expense.__table__, target=expenses_monthly,
    at=Expense.paid_at, tenant=Expense.tenant_id,
    group_by=[Expense.category],
    measures={"total": func.sum(Expense.amount), "count": func.count()},
    where=Expense.status != "cancelled",
    zone="America/Mexico_City",
)

# In the handler of the event the write publishes -- or after the write,
# in its own transaction:
await rollup.refresh_at(session, tenant_id=event.tenant_id, moment=paid_at)

# Nightly, from a scheduled task, to heal anything that skipped the events:
await rollup.rebuild(session, tenant_id=tenant, since=date(2024, 1, 1), until=today())
```

It **recomputes the bucket from its rows** instead of adding a delta: events
and jobs arrive at least once, and `total = total + amount` doubles the
second time one is delivered, where a recompute is right however often it
runs. Two refreshes of one bucket serialise on an advisory lock (without it,
ten concurrent refreshes failed with a unique violation -- tested). Months are
local months in the zone you give, half-open in UTC.

Measured with `scripts/bench_aggregates.py`: 3,000,000 expense rows over 36
months and 1,000 tenants, one of them holding 990,000 rows; the six
aggregates of a spending panel, with a `(tenant_id, paid_at)` index on the
source table; 30 repetitions.

| Panel of six aggregates | p50 | p95 |
| --- | --- | --- |
| Large tenant (990,000 rows), on the fly | 455 ms | 803 ms |
| Large tenant, from the rollup | 3.3 ms | 6.0 ms |
| Typical tenant (2,012 rows), on the fly | 4.1 ms | 5.7 ms |
| Typical tenant, from the rollup | 2.8 ms | 4.2 ms |

What keeping it costs: refreshing one bucket, what a write pays, 8.1 ms (p95
11.8) for the large tenant's month of about 27,000 rows and 1.5 ms for the
typical one; rebuilding 37 months, 1.1 s and 0.06 s. The rollup table is 0.2
MB next to the source's 367 MB.

So: at a few thousand rows per tenant the index is enough and a rollup saves
a millisecond. It is for the tenant that has grown, and for the admin views
that aggregate across tenants -- the ones that are fine in the demo and not
at the customer who pays most.

## Reproducing all of it

```bash
python scripts/bench_overhead.py                     # the per-request cost
python scripts/bench_overhead.py --route-order       # what route order saves
JFAST_PERF_BUDGET=1 pytest tests/test_performance_budget.py
pytest tests/test_multi_replica.py                   # needs JFAST_TEST_PG_URL, JFAST_TEST_REDIS_URL
pytest tests/test_storage_minio.py                   # needs JFAST_TEST_S3_*
python scripts/bench_rag.py --pg "$JFAST_TEST_PG_URL" --chunks 300000 --tenants 1000
python scripts/bench_aggregates.py --pg "$JFAST_TEST_PG_URL" --rows 3000000
```
