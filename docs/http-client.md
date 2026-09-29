# Calling other services

`jfastframework.http` is the client a service uses to call its siblings. It
exists so that each service does not write its own, and so that the four
things every hand-written one gets wrong are decided once: how long to wait,
what to retry, when to stop calling an upstream that is down, and how many
calls to let pile up behind a slow one.

```toml
[plugins]
enabled = ["observability", "http"]

[plugin.http.upstreams.billing]
base_url = "http://billing:8010"
read_timeout = 5.0
retries = 2

[plugin.http.upstreams.catalog]
base_url = "http://catalog:8020"
forward_authorization = true
```

```python
@router.get("/orders/{order_id}")
async def show(order_id: int, request: Request) -> dict:
    billing = request.app.state.jfast.require("http").client("billing")
    response = await billing.get(f"/invoices/{order_id}")
    if response.status_code == 404:
        raise NotFoundError("no invoice for this order")
    response.raise_for_status()
    return response.json()
```

Install it with `pip install "jfastframework[http]"`. It is httpx underneath,
and `response` is an `httpx.Response`.

---

## One client per upstream per process

`require("http")` returns a factory that holds one client for each configured
upstream, for the life of the process. Take the client from it rather than
building one per request: the circuit breaker, the bulkhead and the retry
budget are state, and they only protect anything if every call to that
upstream goes through the same one.

Outside a service -- a script, a test -- build one directly:

```python
from jfastframework.http import ServiceClient, Timeouts, Upstream

async with ServiceClient(Upstream(name="billing", base_url="http://localhost:8010",
                                  timeouts=Timeouts(total=10))) as billing:
    response = await billing.get("/invoices/7")
```

## Deadlines are mandatory

| Setting | Default | Bounds |
| --- | --- | --- |
| `connect_timeout` | 2 s | opening the TCP (and TLS) connection |
| `read_timeout` | 10 s | waiting for each chunk of the response |
| `write_timeout` | 10 s | sending each chunk of the request |
| `pool_timeout` | 2 s | waiting for a free connection from the pool |
| `total_timeout` | 30 s | the whole call: every attempt and every wait between them |

None of them can be turned off -- zero, negative and missing are refused at
start-up. An upstream that accepts the connection and never answers would
otherwise hold the calling request, its worker and its database connection
for as long as it liked. When `total_timeout` passes, the call raises
`UpstreamTimeoutError`.

## What is retried

A failed attempt is tried again only when all of these hold:

1. **The request is safe to repeat.** GET, HEAD, PUT, DELETE and OPTIONS are
   idempotent. POST and PATCH are not, unless the request carries an
   `Idempotency-Key` -- pass `idempotency_key=` and the client sets the header,
   which is also what lets an upstream using the
   [`idempotency` plugin](transactions.md#a-retried-post-idempotency-keys) answer the retry with the first
   response instead of acting twice. `retry=False` forbids a retry;
   `retry=True` vouches for a call the rules would not retry.
2. **The failure is one another attempt can fix:** a connection error, a
   timeout, a dropped connection, or a 429, 502, 503 or 504. A 500 is not
   retried: it is the upstream answering with a bug, and the same request gets
   the same bug. A 4xx is the caller's mistake.
3. **There is budget and time.** See below.

Between attempts the client waits an exponential backoff with **full jitter**
-- a uniform draw between zero and `backoff_base × 2^(n-1)`, capped at
`backoff_max`. Without jitter every client that failed together retries
together, and the upstream meets the same spike on every round. When the
upstream sends `Retry-After` (seconds or an HTTP date), the client waits
exactly that instead; if it asks for longer than `max_retry_after` (30 s), the
response goes back to the caller rather than being waited for.

No retry is started that the total deadline cannot finish: the last response
is returned instead.

**What comes back.** A response the upstream sent is returned as it is once
retrying stops -- a 404, a 500, the last 503 -- and the caller decides what it
means. Only a call that got no response at all raises.

## The retry budget

Three attempts per call means that when an upstream falls over, every caller
triples its traffic at the moment it can least take it. The budget caps
retries at `retry_budget_ratio` (0.2) of the requests made in the last ten
seconds, plus `retry_budget_min_per_second` (1) so a quiet client can still
retry at all. Past that, the failure is returned instead of retried. With a
ratio of 0.1, twenty calls to an upstream answering only 503 make at most
twenty-three requests in the test suite; without the budget they would make
sixty.

## The circuit breaker

Each upstream has a breaker, per process:

- **Closed** -- calls go through. It opens after `breaker_failures` (5)
  failures in a row, or when `breaker_failure_rate` (half) of the calls in the
  last `breaker_window` (30 s) failed, once there were at least
  `breaker_minimum_calls` (20).
- **Open** -- calls raise `CircuitOpenError` at once, and **nothing is sent**.
  The upstream gets `breaker_cool_down` (15 s) to recover instead of a queue of
  retries.
- **Half-open** -- after the cool-down, one call goes through as a probe while
  the rest keep failing fast. If it succeeds the breaker closes; if it fails it
  opens for another full cool-down.

Failures, for the breaker, are what says the upstream is not there: transport
errors, timeouts, 502, 503 and 504. A 500 proves the upstream is up; a 429 is
it pacing us, and an open breaker would turn that into an outage.

Per process means each worker opens on its own evidence: a recovering
upstream sees up to one probe per worker. A breaker shared across processes
would need a shared store on the path of every call.

## The bulkhead

At most `max_concurrent` (50) calls to one upstream are in flight from one
process; a call that cannot get a slot within `bulkhead_wait` (0.5 s) raises
`BulkheadFullError`. Without it, one slow upstream takes every worker down
with it -- requests that never touch it queue behind the ones waiting on it.
A call turned away by the bulkhead does not count against the breaker: the
upstream did not fail it.

## Errors

Every error a call raises is a 503 `ServiceUnavailableError`, so a route
that lets one escape answers `application/problem+json` naming the upstream,
never a 500 with a traceback:

| Error | Raised when |
| --- | --- |
| `CircuitOpenError` | the breaker is open or its probe is out; carries `retry_after` |
| `BulkheadFullError` | the upstream's slots are full |
| `UpstreamTimeoutError` | the total deadline passed |
| `UpstreamUnreachableError` | every attempt failed at the transport |

All four subclass `UpstreamError`, which carries `upstream`. Catch it where
the caller has something better to say -- a cached value, a degraded page.

## What travels with the call

- **`X-Request-ID`** of the request being served, so one grep follows a
  request across services. Inside a queue job it is the job's, which the
  worker restores from the request that queued it; in a script, none is
  invented.
- **The caller's bearer token**, only to upstreams with
  `forward_authorization = true`. A token is issued for an audience; sending
  it to a service outside that audience hands the caller's identity to it,
  which is why it is off by default. Only `Bearer` credentials are forwarded,
  never Basic, and redirects are not followed, so a token cannot be carried
  to another host by a `302`.
- **`headers`** from the upstream's configuration, and then the call's own
  `headers=`, which win over everything above.

## Configuration

Every key of `[plugin.http.upstreams.<name>]`:

| Key | Default |
| --- | --- |
| `base_url` | required |
| `connect_timeout`, `read_timeout`, `write_timeout`, `pool_timeout` | 2, 10, 10, 2 s |
| `total_timeout` | 30 s |
| `retries` | 2 (after the first attempt) |
| `backoff_base`, `backoff_max` | 0.1 s, 2 s |
| `max_retry_after` | 30 s |
| `retry_budget_ratio`, `retry_budget_min_per_second` | 0.2, 1 |
| `breaker_failures`, `breaker_failure_rate`, `breaker_minimum_calls` | 5, 0.5, 20 |
| `breaker_window`, `breaker_cool_down` | 30 s, 15 s |
| `max_concurrent`, `bulkhead_wait` | 50, 0.5 s |
| `forward_authorization` | `false` |
| `headers` | `{}` |

A misspelt key is refused at start-up rather than ignored. The base URL can
come from the environment instead of the file --
`JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL=http://billing.internal:8010` -- and,
as for every plugin, a value in `jfast.toml` wins over the environment, so
leave `base_url` out of the file for an upstream whose address differs per
environment.

## Health

`/ready` lists every upstream's breaker under the `http` check. An open or
half-open breaker makes readiness `degraded`, never `unavailable`: this
service is still up, and taking it out of rotation because a dependency is
down turns one outage into two.

## What is verified, and what is not

**Tested without a network** (`tests/test_http_client.py`, against
`httpx.MockTransport`, a clock the test moves and recorded sleeps): which
methods and failures are retried and which are not, the idempotency key,
full-jitter bounds, `Retry-After` in both forms and its ceiling, the total
deadline, the budget under an outage, every breaker transition including the
single probe, a cancelled probe and the failure rate, the bulkhead, header
propagation, the plugin's configuration and its `/ready` report, and that the
plugin imports without httpx.

**Not tested:** a real upstream over a real socket, TLS, and connection-pool
behaviour under load. **Not supported:** streaming. Request bodies are bytes,
text, JSON or form data, and responses are read whole before they are
returned.
