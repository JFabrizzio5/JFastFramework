# Queues and events

Two different things, deliberately two different plugins.

| | `queue` | `events` |
| --- | --- | --- |
| A message means | "do this" | "this happened" |
| Consumers | exactly one wins | every group gets a copy |
| After consuming | gone | still there, replayable |
| Failure handling | retry, then dead-letter | offsets, replay |
| Backends | PostgreSQL, Redis, RabbitMQ | Kafka |
| Use for | send the email, resize the image | tell other services an order was paid |

Using a queue for events means adding another queue every time a new service
starts caring. Using a stream for jobs means reimplementing retries and
dead-lettering on top of offsets. Pick by which row you are in.

---

## Background jobs

```toml
[plugins]
enabled = ["observability", "database", "queue"]

[plugin.queue]
backend = "postgres"     # or "redis", "rabbitmq"
max_attempts = 3
```

Register a task, enqueue from a route:

```python
tasks = request.app.state.jfast.require("tasks")

@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None:
    ...

queue = request.app.state.jfast.require("queue")
await queue.enqueue(Job(task="send_invoice_email", payload={"invoice_id": 7}))
```

Run a worker:

```python
from jfastframework.queues import Worker

worker = Worker(queue, tasks, concurrency=4)
await worker.run()
```

`GET /queue/stats` reports depths and registered task names.

### Delivery is at-least-once. Handlers must be idempotent.

A worker can do the work and die before acknowledging. Then the job is
redelivered and the work happens twice. No backend here promises
exactly-once, because none of them can.

Charging a card twice is a bug in the handler, not in the queue. Key the side
effect on something stable — the invoice id, an idempotency key — and check
before acting.

### A job runs as the tenant that queued it

A `Job` built inside a request takes that request's `tenant_id` and
`request_id` from context, and the worker puts both back while the handler
runs. The handler's log lines carry the request that caused them, and
`current_tenant_id()` inside it answers the same tenant:

```python
from jfastframework.plugins.builtin.observability import current_tenant_id

@tasks.task("recalculate_balance")
async def recalculate_balance(payload: dict) -> None:
    async with sessionmaker() as session:
        repository = AccountRepository(session, tenant_id=current_tenant_id())
        ...
```

Until `0.1.0a9` the fields existed and nothing filled them, so every job ran
with no tenant and a repository opened inside one read every tenant's rows.
A job queued outside a request -- a cron, a script -- still carries none unless
it is given one: `Job(task=..., tenant_id="acme")`.

### Enqueue in the request's transaction: the outbox

`queue.enqueue(job)` commits in a transaction of its own, apart from the
request's rows, so the two can disagree. Enable the `outbox` plugin and
enqueue through the request's session instead; the job then exists if and only
if the request committed:

```python
outbox = request.app.state.jfast.require("outbox")
await outbox.enqueue(session, Job(task="send_receipt", payload={"id": order.id}))
await outbox.publish(session, "orders", Event(type="order.placed", data={"id": order.id}))
```

On the PostgreSQL queue in the same database the job is inserted straight into
`jfast_jobs`. Anything else -- Redis, RabbitMQ, Kafka events -- goes through
`jfast_outbox` and a relay that runs in every process, claims rows with `FOR
UPDATE SKIP LOCKED`, retries with backoff, and sets a message aside as dead
after `max_attempts`. `/ready` turns degraded when a message is stuck.

The consumer's half is `claim_once`: record the message id in the same
transaction as the work, and a redelivery is skipped.

```python
from jfastframework.outbox import claim_once
from jfastframework.queues import current_job

@tasks.task("send_receipt")
async def send_receipt(payload: dict) -> None:
    async with sessionmaker() as session, session.begin():
        if not await claim_once(session, current_job().id, consumer="receipts"):
            return
        ...
```

### Choosing a backend

| | PostgreSQL (default) | Redis | RabbitMQ |
| --- | --- | --- | --- |
| Extra service | none | Redis | RabbitMQ |
| Enqueue in your transaction | **yes** | no | no |
| Latency | poll interval | microseconds | microseconds |
| Throughput ceiling | hundreds/sec | tens of thousands | very high |
| Routing, priorities, UI | no | no | yes |

**Start with PostgreSQL.** The transactional property is worth more than the
latency for most work: `INSERT` the order and enqueue "charge the card" in one
transaction, and a rollback takes the job with it. With Redis you can commit
the row, crash before the `LPUSH`, and the job simply never exists.

Move to Redis when the poll latency actually matters, and to RabbitMQ when you
need routing, priorities or an operator UI. Be able to name the number you hit.

### How each backend stays honest

**PostgreSQL** claims with `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent
workers take different rows instead of blocking. A partial index covers exactly
the claim predicate, so accumulating dead jobs do not slow the queue down.

**Redis** uses `BLMOVE` into a per-worker processing list. A worker that dies
leaves its job visible for recovery; a naive `BRPOP` queue drops it. On startup
and shutdown the worker returns anything left in its own processing list.

**RabbitMQ** holds delayed jobs and retry backoff in the broker, with no
plugin; see [Delays on RabbitMQ](#delays-on-rabbitmq). Sleeping in the worker
instead would hold a connection and lose the delay on restart.

### Delays on RabbitMQ

`Job(available_at=...)` and every retry go through the same path. The obvious
design -- one delay queue, a TTL on each message -- is wrong: RabbitMQ only
expires the message at the **head** of a queue, so a job delayed ten minutes
holds up a job delayed one second that was published after it. A periodic task
that re-enqueues itself with a delay stops being periodic.

The backend declares a cascade instead. Level `n` is a queue whose TTL is
`2**n` × 100 ms *for every message in it*, so messages expire in the order they
arrived and nothing waits behind a longer one. The delay is written in binary
into the routing key and published to the top level; each level's topic
exchange puts the message in its queue when its bit is 1 and passes it down
when it is 0, and an expired message is dead-lettered to the level below. The
time spent in the cascade is the sum of the levels whose bit is set.

- **Precision:** rounded up to 100 ms. A job can start up to 100 ms late and
  never early.
- **Range:** 25 levels hold about 38 days. A longer delay passes through at
  the maximum carrying its due time in a header, and the worker that receives
  it early sends it round again -- the only step that compares clocks.
- **Topology:** for a queue named `jfast.jobs` the broker holds
  `jfast.jobs.delay.0` to `jfast.jobs.delay.24`, each an exchange and a queue.
  They are declared at setup; `GET /queue/stats` reports their total as
  `delayed`.
- **What it does not survive:** dead-lettering from one level to the next is
  not covered by publisher confirms, so a broker that crashes during the hop
  can lose that message.

The unit and the level count are broker topology: a queue declared with one
TTL refuses to be redeclared with another. Changing either needs a new queue
name.

### Retries

Exponential backoff, capped at five minutes, bounded by `max_attempts`. A job
that exhausts them goes to the dead-letter queue.

Both bounds matter. Uncapped backoff schedules the last retry days out, which
looks like the job vanished. Unbounded retries let one poison message occupy a
worker forever.

An **unknown task** is dead-lettered immediately, without retrying: no future
deploy makes it deliverable, and retrying hides the real problem behind a
growing queue.

### Task names are a wire contract

Jobs queued by yesterday's deploy are still in the queue when today's rolls
out. Rename a task and those jobs become undeliverable. Add the new name,
keep the old one until the queue has drained, then remove it.

### Recurring tasks

Declare the schedule where the task is declared, and turn the scheduler on:

```toml
[plugin.queue]
scheduler = true
```

```python
from datetime import timedelta

@tasks.task("refresh_rates", every=timedelta(minutes=5))
async def refresh_rates(payload: dict) -> None: ...

@tasks.task("nightly_report", cron="0 3 * * *", timezone="America/Mexico_City")
async def nightly_report(payload: dict) -> None:
    day = payload["scheduled_for"]   # the tick's time, ISO-8601 UTC
    ...

# A task declared elsewhere, or one task on a second schedule:
tasks.schedule("purge_sessions", cron="@hourly", payload={"older_than_days": 30})
tasks.schedule("report", cron="0 8 * * mon", name="report-weekly", payload={"span": "week"})
```

Each tick becomes an ordinary job on the queue, so retries, dead-lettering and
at-least-once delivery are the queue's. A handler that must not run twice for
one tick deduplicates on `current_job().id` with `claim_once`: the id is
derived from the schedule's name and the tick's time, and is the same in every
replica.

**Run it everywhere.** The scheduler is meant to run in every replica and
every worker process, with no leader. Before enqueueing a tick, each process
claims it in a store they all share, and only the claim that lands enqueues:

| Store (`scheduler_store`) | Chosen by `auto` when | The claim |
| --- | --- | --- |
| `database` | the `database` plugin is on | a row in `jfast_schedule_ticks`, primary key `(name, fire_at)`, `INSERT … ON CONFLICT DO NOTHING` |
| `cache` | only the `cache` plugin is on | `SET NX` on a key per tick, expiring after two periods (at least ten minutes) |
| `memory` | neither | per process: every process fires every tick. Production refuses to start with it |

With the PostgreSQL queue on the same database, the claim and the job commit
in one transaction. With any other combination they are two writes: a failed
enqueue releases the claim so the next pass takes the tick, but a process
killed between the two loses that tick. The job id is the second line of
defence -- the PostgreSQL queue inserts a duplicate id once.

**When it fires.**

- **Intervals** count from the Unix epoch in UTC, so `every=timedelta(hours=1)`
  fires on the hour and every replica agrees on when that is without talking
  to the others. For "every day at 03:00 local time", use cron.
- **Cron** takes the five standard fields -- `*`, lists, ranges, steps, month
  and weekday names -- plus `@hourly`, `@daily`, `@weekly`, `@monthly`,
  `@yearly`. Day-of-month and day-of-week are an OR when both are restricted,
  as in Vixie cron. Quartz's `?`, `L`, `W` and `#` are refused.
- **Time zones:** cron is read in UTC unless the schedule names a zoneinfo
  zone. Across a DST jump, a wall-clock time that does not exist fires once,
  moved forward by the jump (02:30 becomes 03:30); a time that happens twice
  fires on its first occurrence, unless the hour field is `*`, in which case
  the job keeps running every real hour.
- **Missed ticks:** after downtime -- a restart, a deploy, an event loop
  blocked for an hour -- the most recent missed tick fires **once**. The ones
  before it are skipped, never replayed as a burst. `catch_up=False` skips it
  too. A schedule with no claim on record is new and starts with its next
  tick.
- **Names are contracts**, like task names: ticks are claimed under the
  schedule's name, so renaming a schedule makes it new, and a new schedule
  does not catch up.

`/ready` reports the scheduler under the queue check: the store, each
schedule and its next tick, and the last error. A stopped or failing
scheduler turns readiness `degraded`, not `unavailable` -- recurring work is
late, and the requests the replica serves are not.

---

## Events

```toml
[plugins]
enabled = ["observability", "events"]

[plugin.events]
bootstrap_servers = "localhost:9092"
consumer_group = "billing"
```

```python
from jfastframework.plugins.builtin.events import Event, on

# Declared at import time, bound by the plugin before the consumer starts.
@on("orders")
async def on_order(event: Event) -> None:
    if event.type == "order.paid":
        ...

# Publishing needs the running bus, so it happens inside a request or a task.
events = request.app.state.jfast.require("events")

await events.publish("orders", Event(
    type="order.paid",
    data={"order_id": 7, "amount": "42.00"},
    key="order-7",          # partition key: order events stay ordered
))
```

### Handlers are declared, not registered on a live bus

The consumer subscribes to the topics it knows about when it joins its group,
and it joins at startup. `bus.on(...)` still works, but only from the sliver of
time after the plugin registers and before it starts — a handler added by a
request handler or a FastAPI startup hook arrives too late and is never called.
Use the module-level `on`, which the plugin drains at register and again at
startup.

With `consume = true` and nothing declared, the consumer would join the group
and receive nothing, forever. The plugin logs a warning and does not start it.

### Partition keys are not optional

Kafka orders messages **within a partition**, not within a topic. Publish
events about one order without a key and two consumers can process
`order.paid` before `order.created`. Use the aggregate id as the key.

### Offsets are committed after handling

`enable_auto_commit` is off. The consumer commits after the handler returns, so
a crash mid-handler redelivers rather than skips — at-least-once again. A
handler that raises does not commit, so a poison message blocks its partition.
That is visible and fixable; silently skipping it is neither.

### Events are past tense and immutable

`order.paid`, not `pay_order`. An event says something happened; a command asks
for something to happen, and a command belongs in a queue. Once published, an
event is history: correct it with a new event, never by rewriting the old one.

---

## Infrastructure

Enabled plugins contribute their containers to the generated compose file:

```bash
jfast deploy compose --stdout        # one service
jfast workspace compose              # the whole workspace
```

RabbitMQ lands at offset +6, Kafka at +2 (KRaft mode — no ZooKeeper, one
container instead of two). The `postgres` and `redis` queue backends add no
container: they reuse the one their own plugin already declares.

### Reaching the broker from the host

Kafka is the one container whose address is not just a port mapping. A client
bootstraps once, then reconnects to the address the broker advertises, so a
broker that only advertises its compose hostname lets a process on the host
connect and then hang. The container therefore runs two listeners:

| Listener | Container port | Address | Who uses it |
| --- | --- | --- | --- |
| `INTERNAL` | 9092 | `kafka:9092` | other compose services |
| `EXTERNAL` | 9094 | `localhost:<host_port>` | `jfast dev`, psql-style local tools |

`<host_port>` defaults to `base + 2`, which is what compose publishes and what
the generated `jfast.toml` already sets as `bootstrap_servers`. Two settings
adjust this when the defaults do not fit:

```toml
[plugin.events]
host_port = 19092         # the published port, if it is not base + 2
advertised_host = "localhost"
image = "bitnamilegacy/kafka:3.9"
```

`host_port` is one setting for two things: it is the port compose publishes
*and* the port the broker advertises. It cannot move one without the other,
which is the point of it — `ports: - "8702:9094"` against
`EXTERNAL://localhost:19092` is a client that bootstraps, reconnects to the
advertised address and hangs, which is the failure the setting was added to
prevent. It exists at all because `infra()` is called without an application
context, so a service on a non-default base port has to say so.

`image` exists because broker images move: Bitnami relocated its catalogue to
`bitnamilegacy/` in 2025 and the previous `bitnami/kafka:3.9` tag stopped
resolving.

---

## What is verified, and what is not

**Tested in CI:** the `Job` model, backoff and its cap, exhaustion,
dead-lettering, unknown-task handling, job timeouts, worker draining on
shutdown, and that an idle worker yields instead of busy-waiting — against an
in-memory backend implementing the same protocol.

**Against a real RabbitMQ 3.13** (`tests/test_rabbitmq_queue.py`, which CI
refuses to let skip): a delayed job waits for its time, a long delay does not
hold up a short one behind it, delayed jobs arrive in due order, a retry waits
out its backoff in the broker, an exhausted job is dead-lettered, a delay
longer than the cascade goes round again, and a worker retries a failing
handler through all of it.

**Against a real PostgreSQL and Redis** (`tests/test_scheduler.py`, also
refused a skip in CI): twenty concurrent claims of one tick from two engines
land once, the claim and the job commit in one transaction, a duplicate job id
is one row, pruning keeps each schedule's latest claim, two running services
with the scheduler on enqueue each tick once, and the Redis store claims once
and never moves its latest tick backwards. The cron parser, catch-up and the
two-replica cases run everywhere against a fake clock.

**Not tested:** the Redis and Kafka backends against real servers. The
PostgreSQL queue meets a real server only in the outbox suite, which enqueues
through a request's session and claims what it wrote. RabbitMQ has not been tested in a cluster, under a broker
restart, or with quorum queues.
