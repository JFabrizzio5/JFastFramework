# Transactions

When a request's writes are committed, what a failed write turns into, and how
two requests that race for the same row are kept from both winning.

```python
from jfastframework.plugins.builtin.database import DbSession

@router.post("/invoices", status_code=201)
async def create_invoice(payload: InvoiceCreate, session: DbSession) -> InvoiceRead:
    invoice = await InvoiceRepository(session).create(**payload.model_dump())
    return InvoiceRead.model_validate(invoice, from_attributes=True)
```

One request, one transaction. The repository flushes; the session commits when
the endpoint returns; an exception anywhere rolls the whole request back.

---

## The commit happens before the response

`DbSession`, `ReadSession` and `TenantSession` are the session dependencies
with `scope="function"` applied. The scope is what decides *when* the commit
runs, and the default is the wrong answer.

FastAPI runs the code after a dependency's `yield` **after the response has
been sent**, unless the dependency is function-scoped. The session commits in
that code. Through `0.1.0a8` that meant:

- **A commit that failed had already been answered `201`.** A deferred
  constraint, a serialisation failure, a connection dropped at the wrong
  moment -- the client was told the row exists, and it does not.
- **A client could read its own write before it existed.** The response left,
  the client asked for the row it had just created, and the commit had not run
  yet. That is the read-after-write failure the replica pin exists to prevent,
  reproduced without any replica.

With `scope="function"` the commit runs when the endpoint returns, before the
response is built, so a failed commit is the `500` it should be and a `201`
means the row is there.

**The database plugin refuses to start** while any route reaches a session
dependency with another scope, and names each route. Spell it either way:

```python
async def create(session: DbSession): ...
async def create(session = Depends(session_dependency, scope="function")): ...
```

A generator dependency of your own that wraps a session has to be
function-scoped too: FastAPI refuses a request-scoped dependency that depends on
a function-scoped one.

### A session dependency of your own: `@transactional`

A service sometimes opens its own session -- to set extra row-level security
values, to pick a database the framework does not know about -- and commits in
its own `yield` dependency. That commit runs after the response exactly like
the framework's would, and the check above cannot see it: it only knows the
dependencies it ships. Mark yours, and it is held to the same rule:

```python
from jfastframework.plugins.builtin.database import transactional

@transactional
async def session_with_companies(request: Request) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session
        await session.commit()

@router.post("/invoices")
async def create(session = Depends(session_with_companies, scope="function")): ...
```

A route that depends on it without `scope="function"` stops the service from
starting, with the route and the dependency named: `POST /invoices ->
session_with_companies`.

**Unmarked dependencies are not ignored.** At startup the plugin also reads the
source of every request-scoped generator dependency, and names the routes whose
dependency calls `.commit(` after its `yield`. That is a warning, not a
refusal -- a heuristic can be wrong, the marker cannot -- and it goes away when
you either mark the dependency or scope it.

If the only reason for your own dependency was a second value for row-level
security, you may not need it at all: see
[extra values on every transaction](multitenancy.md#more-than-the-tenant-transaction_setting).

---

## What a failed write turns into

`BaseRepository` flushes after every write, and the flush is where the database
says no. The driver's exceptions become the error the client should see:

| What happened | Error | Status |
| --- | --- | --- |
| Unique or exclusion constraint violated | `ConflictError` | 409 |
| Foreign key points at nothing, or a delete orphans a child | `ConflictError` | 409 |
| Row changed by another request since this one read it | `ConflictError` | 409 |
| `update(expected_version=n)` and the row is past `n` | `PreconditionFailedError` | 412 |

After any of them the transaction is over -- PostgreSQL refuses every statement
until it rolls back -- and the request-scoped session rolls it back.

### Check-then-insert is a race; the constraint is the rule

```python
if await repository.by_name(payload.name) is not None:
    raise ConflictError(...)
return await repository.create(**payload.model_dump())
```

Two requests can both pass the check. The generated models carry
`UniqueConstraint("tenant_id", "name", postgresql_nulls_not_distinct=True)`,
so only one of them passes the insert and the other gets the same `409`. The
check stays for its message. `NULLS NOT DISTINCT` is what makes the constraint
hold on rows without a tenant; it needs PostgreSQL 15 or later.

---

## Lost updates: `VersionedMixin`

Two people open the same invoice. Both edit it. Without a version, whoever
saves second silently erases the first. With one:

```python
from jfastframework.db import Base, TimestampMixin, VersionedMixin

class Invoice(Base, VersionedMixin, TimestampMixin):   # Versioned first
    ...
```

Two protections come from the one column:

- **Between requests.** The client reads `version: 3`, sends it back with the
  edit, and the service passes it on:
  `repository.update(invoice, expected_version=payload.version, **changes)`.
  If someone else saved in between, the row is at 4 and the answer is `412`
  with `current_version` in the body. The client reads again and decides.
- **Inside the race.** Two requests that both read version 3 in the same
  instant both pass that check. SQLAlchemy's `UPDATE` carries
  `WHERE version = 3`; the second matches no row, and it becomes a `409`.

`VersionedMixin` goes **before** `TimestampMixin` in the bases. Both set
`__mapper_args__` and the first wins; this one carries the timestamp mixin's
setting too. The other order would drop the versioning without a word, so it
raises `TypeError` at import instead.

Generated `layered` modules come with it: the model is versioned, `Read`
returns `version`, `Update` accepts it. Leaving it out of a `PATCH` keeps the
old last-write-wins behaviour.

---

## Values that cannot be one `UPDATE`

A balance, a stock count, the next folio number: read, compute, write. Two
requests that read the same value both write their own result, and one of the
two changes is gone. The version column turns that into a `409`; when the
right answer is to wait instead, lock.

**The row**, when there is one:

```python
account = await repository.get_for_update(account_id)   # SELECT ... FOR UPDATE
account.balance += amount
```

**A key**, when the thing being protected is not one row -- "one open invoice
per customer per month":

```python
from jfastframework.db import advisory_lock

await advisory_lock(session, f"open-invoice:{customer_id}:{month}")
```

`pg_advisory_xact_lock` releases on commit or rollback, so there is no unlock to
forget and no lock outliving a crashed request. On SQLite, which allows one
writer at a time already, it does nothing.

---

## Retrying a transaction the database gave up on

`40001` (serialisation failure) and `40P01` (deadlock) mean the database rolled
the transaction back to protect another one. The same work run again usually
succeeds. `run_in_transaction` does exactly that, and nothing else:

```python
from jfastframework.db import run_in_transaction

async def settle(session):
    ...

await run_in_transaction(sessionmaker, settle, attempts=3)
```

- Every attempt is a fresh session and a fresh transaction, and the work runs
  from its first line. A retry never resumes half a unit of work.
- Only those two codes are retried. A constraint violation or a bug fails on
  the first attempt.
- Backoff with full jitter, so two transactions that deadlocked each other do
  not collide again on the same schedule.

For jobs and scripts. A request already has its transaction, and retrying
inside it would replay only the part after the retry point. Keep effects
outside the database -- an email, an HTTP call -- out of the retried function:
they happen once per attempt.

---

## A message that commits with the rows: the outbox

Saving an order and queueing its receipt are two writes. As two transactions,
either can happen alone: the job is queued and the order rolls back, or the
order commits and the process dies before the job exists. The `outbox` plugin
makes them one:

```python
from jfastframework.queues import Job

@router.post("/orders", status_code=201)
async def create(payload: OrderIn, request: Request, session: DbSession):
    order = await OrderRepository(session).create(**payload.model_dump())
    outbox = request.app.state.jfast.require("outbox")
    await outbox.enqueue(session, Job(task="send_receipt", payload={"id": order.id}))
    return order
```

`outbox.enqueue` and `outbox.publish` write through the request's session, so
the message exists if and only if the order does. With the PostgreSQL queue on
the same database the job goes straight into it; otherwise a relay in every
process moves committed messages to the queue or the event bus, with `FOR
UPDATE SKIP LOCKED` so two relays never send one message. See
[Queues and events](queues-and-events.md) for the consumer's half.

## A retried POST: idempotency keys

A client whose connection drops after sending `POST /payments` cannot tell
whether it went through, so it retries -- and without help the server charges
twice. With the `idempotency` plugin, a route that asks for the key records it
in the same transaction as the payment:

```python
from jfastframework.idempotency import IdempotencyKey

@router.post("/payments", status_code=201)
async def pay(payload: PaymentIn, session: DbSession, key: IdempotencyKey): ...
```

| The client sends `Idempotency-Key: k` and | It gets |
| --- | --- |
| `k` is new | The request runs; its response is recorded |
| the same request again | The recorded response, with `Idempotent-Replayed: true` |
| a different request with `k` | 422: a key names one operation |
| the same request while the first is still running | 409 |

If the first request fails, the key rolls back with it and the retry runs from
scratch. Keys are per tenant and expire after `ttl_hours` (24).
`RequiredIdempotencyKey` refuses a request without one.

---

## What this is not

**Not a circuit breaker.** A breaker stops calling something that is down. It
does nothing for work that failed half way; the transaction boundary, the
outbox and the idempotency key do. The service-to-service client with timeouts,
retries and a breaker is in `PLAN-NEXT.md`.
