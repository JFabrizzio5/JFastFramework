# shared/, enums, and channels

Two features, one idea: **coupling that is easy to add, invisible once added,
and expensive to remove after a second person has copied the pattern.** Both
are therefore checked rather than agreed.

---

## Where an enum goes

```bash
jfast new enum InvoiceStatus --module invoice
jfast new enum Currency --shared
```

Run without a flag it asks, because the placement *is* the decision:

```
  Will more than one module use it?  yes puts it in shared/, no puts it in one module
```

You do not have to answer it correctly on day one. Start an enum in the module
that needs it, and the check tells you the day a second one wants it.

### The rule

**`shared/` is vocabulary, not behaviour.** An enum, a type or a pure function
that two modules both speak moves here, and the check names the file when a
module imports one from another:

```
modules/payment/service.py:41: cross-module: module 'payment' imports modules.invoice.enums, private to module 'invoice'
  (an enum or type two modules both speak is vocabulary: move it to shared/enums.py and import it from both)
```

Moving it to `shared/` clears the finding. The message names the file, so the
fix does not need a design discussion.

**Data and behaviour do not come here.** When `payment` needs rows `invoice`
owns, the answer is not to move `InvoiceRepository` into `shared/` — that is
two modules sharing a table, and the checker cannot tell it from the coupling
it was meant to remove. It is a function in `modules/invoice/public.py` that
takes the caller's session and a `tenant_id` and returns DTOs, plus
`depends_on = ["invoice"]` under `[modules.payment]` in `contracts.toml`. For
anything else of another module the message says exactly that:

```
modules/payment/service.py:12: cross-module: module 'payment' imports modules.invoice.service; import modules.invoice.public instead
  (only public.py is another module's API -- call a function in modules/invoice/public.py that returns DTOs (add one if it is missing), and add "invoice" to depends_on under [modules.payment] in contracts.toml)
```

And raw SQL against `invoices` from inside `payment` is reported as
`cross-module-sql`: it is the same coupling with nothing to see it.
[Services, modules and layouts](modules.md#communication-between-modules) has
the full example and the reasons.

**The direction is one-way**, and that is checked too:

```
shared/enums.py:43: shared-direction: shared/ imports modules.invoice
  (the direction is one-way: modules use shared/, never the reverse,
   or the graph becomes a circle)
```

Without that second rule `shared/` becomes the place everything ends up, which
is the failure mode of every `utils` package ever written.

### Queries through a facade, effects through events

The three ways across a module boundary, and which one to use:

| You need to… | Use | Not |
| --- | --- | --- |
| read data another module owns | a function in its `public.py`, returning DTOs | its repository, its entity, SQL against its tables |
| react to something another module did | an event it publishes (`outbox.publish`) and you `@subscribe` to | a call back into it, or queuing its task by name |
| speak the same enum or type | `shared/` | a copy in each module |

The second row is where events come in, and it works on the default stack —
PostgreSQL and its queue, no broker. The receipts module declares the event it
publishes and publishes it in the transaction that did the categorising; the
module that cares subscribes in its own `tasks.py`:

```python
# modules/receipt/services/receipt_service.py
from jfastframework.events import Event

await outbox.publish(session, "receipts", Event(type="receipt.categorized", data={"id": receipt.id}))
```

```python
# modules/budget/tasks.py
from jfastframework.events import Event, subscribe
from jfastframework.tasks import TaskSession

@subscribe("receipt.categorized")
async def check_budget(event: Event, session: TaskSession) -> None:
    ...  # runs in the worker, as the publishing tenant, committed on return
```

```toml
# contracts.toml
[modules.receipt]
publishes = ["receipt.categorized"]
```

`outbox.publish` writes one job per subscriber into the queue through the same
session, so the reaction exists if and only if the categorising committed.
`budget` does **not** list `receipt` in `depends_on`: the publisher never names
its subscribers, so no edge points either way and the module graph stays free of
cycles. `jfast worker` runs the subscriber; see
[Queues and events](queues-and-events.md#events-between-modules).

Two things this replaces, both of which pass review and fail later:

- **Publishing with nobody listening.** An event no module subscribes to and no
  broker carries is refused in the request (`UndeliverableEvent`, a 500 that
  names the fix) instead of answering 201 and dying in the outbox.
- **Queuing the other module's task by name.** `Job(task="budget.check")` from
  `receipt` is a call into `budget` spelt as a string. `contracts check`
  reports it as `undeclared-dependency` and suggests the event.

A [channel](#channels) below does a similar job inside one process when the
message does not have to survive a crash.

### What belongs in shared/

| Belongs | Does not |
| --- | --- |
| Enums two modules both speak | Anything only one module uses |
| Value objects and shared types | Anything that touches the database |
| Pure functions | Anything that makes an HTTP call |

The database line is the one that matters, and the generated `contracts.toml`
enforces it by forbidding `sqlalchemy` in `shared/`. **Two modules sharing a
repository is two modules sharing a table**, and that is how a set of services
becomes a distributed monolith with extra latency.

### Why `str, Enum`

Both templates use it, and the reason is not style. A plain `Enum` serialises
as `Status.DRAFT` down some paths and `"DRAFT"` down others; inheriting from
`str` makes a member a string everywhere — in JSON, in SQL, and in a log line.

And **the value is the wire format**. It is stored in a column, serialised into
an API response and read by a frontend. Renaming a member is free; changing its
value is a data migration.

Turn the whole thing off if you disagree — one switch for every module rule,
facade and SQL checks included:

```toml
[rules.placement]
enabled = false
```

---

## Channels

The pattern this replaces is a file of string constants imported wherever
somebody publishes:

```python
VINCULACION_COMPLETADA = "eventos:vinculacion_completada"
```

That works, and fails in three specific ways: nothing checks the payload, the
transport is welded to the call site, and nobody can list the channels a system
uses.

```python
# channels.py
from jfastframework.channels import Channel

VINCULACION_COMPLETADA = Channel(
    "eventos:vinculacion_completada",
    description="A linkage finished; downstream balances may be stale.",
    required=("vinculacion_id", "rfc"),
)

LARAVEL_CHEQUES = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")
```

```python
await VINCULACION_COMPLETADA.publish({"vinculacion_id": 7, "rfc": "AAA010101AAA"})

@VINCULACION_COMPLETADA.on
async def recalculate(payload):
    ...
```

### The payload is validated where it is built

```
ChannelError: Payload for 'eventos:vinculacion_completada' is missing rfc.
```

That failure belongs in the service that built the message, not in a worker
three services away that read a key which is no longer there.

`required` is a set of key names rather than a model on purpose. These payloads
cross language boundaries — Laravel publishes to some of them — so the contract
that can be enforced on both sides is *these keys are present*, not *this is a
pydantic model*.

### Backends are per channel

Mixing is the normal case, not an edge case:

| Backend | Use it when | Understand that |
| --- | --- | --- |
| `memory` | Default. Inside one process. | Delivery is to this process only — correct for a modular monolith, wrong the moment there are two replicas. |
| `redis` | Something else, in another language, publishes or subscribes. | Nothing is retained. A subscriber that is not connected never sees the message. |
| `kafka` | A consumer that was down has to catch up. | Retained and replayable, and needs the `events` plugin. |

The default needs no infrastructure at all, so publishing an event is not a
decision you have to make on day one. Moving a channel to Redis later is one
keyword on the declaration.

`redis` is for *this changed, refresh*. For *do this work*, use the queue — it
retries, backs off and dead-letters, and pub/sub does none of those.

### Listing them

```bash
jfast describe --json | jq '.plugins[] | select(.name=="channels") | .channels'
```

Which is the third failure fixed: the channels a system speaks are in one file
and one command, rather than spread across whichever modules happen to publish.
