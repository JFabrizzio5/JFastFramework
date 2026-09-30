# Telemetry: one request, followed everywhere it goes

Logs tell you what one service did; metrics tell you how often. Neither follows
one request through the API, its SQL, a model call, a second service and the job
it queued -- which is the first thing you need when something is slow in
production. The `telemetry` plugin does that with OpenTelemetry traces, exported
over OTLP to anything that speaks it: Jaeger, Grafana Tempo, Honeycomb, Datadog,
an OpenTelemetry Collector.

```toml
[plugins]
enabled = ["observability", "database", "http", "telemetry"]
```

```bash
# .env -- the only line that turns it on
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

```bash
pip install "jfastframework[telemetry]"
```

## Free until configured

With the plugin enabled and no endpoint, **nothing is installed**: no
middleware, no SQL listener, no tracer. Every span call in the framework stays
the no-op it is without the plugin, and one line at startup says so:

```
telemetry: no OTEL_EXPORTER_OTLP_ENDPOINT, so no traces are recorded or exported
```

`/ready` reports `telemetry` as `not exporting`, never as a failure. OpenTelemetry
does not even have to be installed until an endpoint is set; with one set and the
extra missing, startup stops with the `pip install` to run.

The endpoint is read, in order of precedence, from `[plugin.telemetry]
traces_endpoint` / `endpoint` in `jfast.toml`, then `JFAST_TELEMETRY_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (a full URL) and `OTEL_EXPORTER_OTLP_ENDPOINT`
(a base URL; `/v1/traces` is appended) -- from the environment or from `.env`.

## What is traced

| Span | Kind | Attributes |
| --- | --- | --- |
| `GET /users/{user_id}` -- every HTTP request | server | `http.route` (the template, not the path), `http.request.method`, `http.response.status_code`, `jfast.tenant_id`, `jfast.request_id`; exceptions as events |
| `SELECT notes` -- every SQL statement | client | `db.system`, `db.name`, `db.operation`, `db.sql.table`, `db.rows_affected`; `error.type` on failure |
| `GET billing` -- every `http` client call | client | `jfast.upstream`, `server.address`, `http.response.status_code`, `http.request.resend_count` |
| `GET /billing` -- every call the gateway proxies | client | `jfast.gateway.prefix`, `server.address`, `http.response.status_code` |
| `llm.chat`, `llm.embed` | client | `llm.model`, `llm.purpose`, `jfast.tenant_id`, `llm.usage.input_tokens`, `llm.usage.output_tokens`, `llm.usd`, `llm.ms`, `llm.retries`, `llm.finish_reason` |
| `rag.ingest` | internal | `rag.document_id`, `rag.chunks`, `rag.embedded`, `rag.reused`, `rag.store` |
| `rag.search` | internal | `rag.limit`, `rag.hybrid`, `rag.filtered`, `rag.hits`, `rag.store` |
| jobs and event handlers | consumer | set by the queue and events plugins, inside the trace of the request that created them |

Every span carries the service's resource: `service.name` (the `app_name`, unless
`OTEL_SERVICE_NAME` is set), `service.version` and `deployment.environment.name`.

`/health`, `/ready` and `/metrics` are not traced (`exclude_paths`): probes and
scrapes arrive several times a second, forever, and nobody reads them.

## What is never traced

**Prompt text, documents, answers, search queries, request and response bodies,
bound SQL parameters.** The same rule as the `llm` ledger, enforced in three
places:

- The call sites pass ids, names, counts and durations only. `llm.chat` knows the
  messages and does not pass them; `rag.search` knows the query and does not.
- The backend drops any attribute that is not a small scalar -- a dict is never
  turned into a string -- and cuts strings at 256 characters.
- SQL spans never read bound parameters. The statement text is off by default
  (`record_sql_statement = true` turns it on): a statement written with literals
  in it instead of parameters would carry them.

The raw request path is not recorded either, only the route template: a path can
hold an email or an id per request, and the request id already leads to the log
line that has it.

Exceptions are recorded with their type, message and stack trace, as every
OpenTelemetry integration does. The framework's own errors say what failed, not
what was sent (`LLMError` carries the provider's message, never the prompt); a
message your code puts in an exception is yours to keep clean.

## Across services, jobs and events

One trace covers every hop because the W3C `traceparent` travels with the work:

- **Incoming requests** continue the caller's trace: the server span's parent is
  the `traceparent` header when there is one.
- **The `http` client** sends the current span's `traceparent` and `tracestate`
  on every call, inside a client span of its own, so the next service's server
  span is that call's child. See [http-client.md](http-client.md#what-travels-with-the-call).
- **The gateway** replaces the client's `traceparent` with its own span's, one
  hop deeper in the same trace. With telemetry off it relays the client's header
  untouched, so the upstream can still continue the caller's trace.
- **Jobs and events** carry the trace context of the request that created them,
  next to its tenant and request id, and the worker runs the handler inside it:
  the job's spans join the request's trace instead of starting an orphan one.

A client that is not the framework's -- a raw `httpx` call -- has to send the
header itself:

```python
from jfastframework import tracing

await httpx_client.get(url, headers=tracing.inject())
```

And work worth seeing in your own code gets a span the same way the framework's
does, free when telemetry is off:

```python
with tracing.span("invoice.render", invoice_id=invoice.id, pages=len(pages)):
    pdf = render(invoice)
```

The provider is also set as OpenTelemetry's global one (`set_global_provider`,
only when nothing set one first), so `opentelemetry.trace.get_tracer(__name__)` in
your code or a third-party instrumentation joins the same traces.

## Jaeger locally

```toml
[plugin.telemetry]
include_infra = true
```

`jfast deploy compose` then adds an OpenTelemetry Collector and Jaeger, and
points the API at the collector (`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`
on the compose network). In a workspace every service names the same two
containers, so there is **one collector and one Jaeger for the whole workspace**,
and each service's container gets the same variable. The collector's
configuration is inline in its command -- OTLP in, a batch, OTLP out to Jaeger --
so there is no file to keep next to the compose file.

| | Host port |
| --- | --- |
| Jaeger UI | `http://localhost:16686` (`jaeger_ui_host_port`) |
| Collector, OTLP/HTTP | `http://localhost:4318` (`collector_host_port`) |

A service running outside compose (`jfast dev`) exports to the published
collector with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in its `.env`.

## Sampling

```toml
[plugin.telemetry]
sample_ratio = 0.1
```

Keeps one new trace in ten, decided from the trace id. A request that arrives with
a sampled `traceparent` is always kept and one that arrives unsampled is always
dropped, so a trace is never recorded in one service and missing in the next:
decide at the edge (the gateway or the first service), and everything behind it
follows. An unsampled request still carries its `traceparent` onward.

## Configuration

```toml
[plugin.telemetry]
# endpoint = "http://collector:4318"    # prefer OTEL_EXPORTER_OTLP_ENDPOINT
sample_ratio = 1.0
sql = true                               # one span per SQL statement
record_sql_statement = false
exclude_paths = ["/health", "/ready", "/metrics"]
export_timeout = 10.0                    # seconds per export
shutdown_timeout = 5.0                   # seconds to flush at shutdown
exporter = "otlp"                        # "memory" for tests, "console" to print
include_infra = false
```

An API key for a hosted backend goes in the environment, never in `jfast.toml`:
`JFAST_TELEMETRY_HEADERS='{"x-honeycomb-team": "..."}'`, or the standard
`OTEL_EXPORTER_OTLP_HEADERS`.

Spans are exported in batches from a background thread. At shutdown the last
batch is flushed, off the event loop and within `shutdown_timeout`, so a
collector that is down delays shutdown by at most that much.

## Health

`/ready` reports the exporter, never as critical -- a collector being down must
not take the service out of rotation:

- `not exporting: set OTEL_EXPORTER_OTLP_ENDPOINT ...` when there is no endpoint.
- `exporting to http://collector:4318/v1/traces` with the spans exported and the
  failed batches so far.
- `span export failing (...); spans are being dropped` after a failed batch, until
  one succeeds. The endpoint shown has its user-info and query stripped.

## Failures never reach the request

Telemetry that breaks a request is worse than none. Starting, ending or
annotating a span, extracting a header, recording an SQL statement: every one of
them is guarded, and a failure is a debug log line and an untraced request.
An exporter that raises or a collector that refuses is counted for `/ready` and
logged by the exporter; the request never sees it. `tests/test_telemetry.py`
replaces the tracer and the backend with objects that raise on every attribute
and checks the request is still served.

## Testing your own spans

```toml
# tests' configuration
[plugin.telemetry]
exporter = "memory"
```

```python
spans = app.state.jfast.require("telemetry").exporter.get_finished_spans()
assert any(span.name == "invoice.render" for span in spans)
```

The memory exporter records synchronously, so a span is there the moment it
ends.

## What it costs

Measured in-process for 0.1.0a11: a `GET /users/{user_id}` driven straight through
the ASGI app (no sockets, no HTTP client), `observability` at `WARNING`, median of
seven rounds of 5,000 requests, three runs, Apple M-series laptop:

| | us per request | over no plugin |
| --- | --- | --- |
| No `telemetry` plugin | 41 | -- |
| `telemetry` enabled, no endpoint | 41 | within run-to-run noise (±5) |
| `telemetry` exporting (in-memory exporter) | 66 | +19 to +29 (median +24) |

With no endpoint the cost is nothing measurable -- there is nothing installed to
cost anything. Exporting, it is the price of one server span per request plus its
attributes; against an endpoint that queries PostgreSQL (1-5 ms) or calls a model
it does not register, and `sample_ratio` lowers it for the requests not kept. The
real OTLP exporter adds the batch thread's serialisation and HTTP, which runs off
the request path.

## What is verified, and what is not

**Tested** (`tests/test_telemetry.py`, in-memory exporter): no endpoint installs
nothing and says so; server spans with route template, tenant, request id and
status; an incoming `traceparent` continued; probes excluded; exceptions and 5xx
answers recorded; sampling, including a sampled parent kept at ratio 0; SQL spans
against PostgreSQL as children of the request, without parameters, with the
statement only when asked, failures marked, the listener removed at shutdown;
two apps -- A calling B through the `http` client over an ASGI transport -- in one
trace, B's server span the child of A's client span; the gateway forwarding its
own span and, with telemetry off, relaying the caller's header; `llm` and `rag`
spans with their counts and cost and no prompt, document or answer text in any
attribute or event; a broken tracer, a broken backend and a failing exporter
never breaking a request; the compose output.

**Checked by hand, not in CI** (2026-09-30): the generated compose pair --
`otel/opentelemetry-collector:0.136.0` with its inline configuration and
`jaegertracing/jaeger:2.10.0` -- started, a service exported over OTLP/HTTP to the
collector, and the traces were read back from Jaeger's API under the service's
name with their route-template span names. **Not tested:** a hosted backend
(Honeycomb, Tempo, Datadog), and TLS to the collector.
