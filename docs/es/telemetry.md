# Telemetría: un request, seguido a todas partes

Los logs dicen qué hizo un servicio; las métricas, con qué frecuencia. Ninguno
sigue un request por la API, su SQL, una llamada a un modelo, un segundo servicio
y el job que encoló -- que es lo primero que hace falta cuando algo va lento en
producción. El plugin `telemetry` hace eso con trazas de OpenTelemetry, exportadas
por OTLP a cualquier cosa que lo hable: Jaeger, Grafana Tempo, Honeycomb, Datadog,
un OpenTelemetry Collector.

```toml
[plugins]
enabled = ["observability", "database", "http", "telemetry"]
```

```bash
# .env -- la única línea que lo enciende
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

```bash
pip install "jfastframework[telemetry]"
```

## Gratis hasta que se configura

Con el plugin habilitado y sin endpoint **no se instala nada**: ni middleware, ni
listener de SQL, ni tracer. Cada span del framework sigue siendo el no-op que es
sin el plugin, y una línea al arrancar lo dice:

```
telemetry: no OTEL_EXPORTER_OTLP_ENDPOINT, so no traces are recorded or exported
```

`/ready` reporta `telemetry` como `not exporting`, nunca como falla. OpenTelemetry
ni siquiera tiene que estar instalado hasta que hay endpoint; con endpoint y sin
el extra, el arranque se detiene con el `pip install` que hay que correr.

El endpoint se lee, en orden de precedencia, de `[plugin.telemetry]
traces_endpoint` / `endpoint` en `jfast.toml`, luego `JFAST_TELEMETRY_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (URL completa) y `OTEL_EXPORTER_OTLP_ENDPOINT`
(URL base; se le agrega `/v1/traces`) -- del entorno o del `.env`.

## Qué se traza

| Span | Tipo | Atributos |
| --- | --- | --- |
| `GET /users/{user_id}` -- cada request HTTP | server | `http.route` (la plantilla, no la ruta), `http.request.method`, `http.response.status_code`, `jfast.tenant_id`, `jfast.request_id`; excepciones como eventos |
| `SELECT notes` -- cada sentencia SQL | client | `db.system`, `db.name`, `db.operation`, `db.sql.table`, `db.rows_affected`; `error.type` si falla |
| `GET billing` -- cada llamada del cliente `http` | client | `jfast.upstream`, `server.address`, `http.response.status_code`, `http.request.resend_count` |
| `GET /billing` -- cada llamada que proxea el gateway | client | `jfast.gateway.prefix`, `server.address`, `http.response.status_code` |
| `llm.chat`, `llm.embed` | client | `llm.model`, `llm.purpose`, `jfast.tenant_id`, `llm.usage.input_tokens`, `llm.usage.output_tokens`, `llm.usd`, `llm.ms`, `llm.retries`, `llm.finish_reason` |
| `rag.ingest` | internal | `rag.document_id`, `rag.chunks`, `rag.embedded`, `rag.reused`, `rag.store` |
| `rag.search` | internal | `rag.limit`, `rag.hybrid`, `rag.filtered`, `rag.hits`, `rag.store` |
| jobs y handlers de eventos | consumer | los ponen los plugins de cola y eventos, dentro de la traza del request que los creó |

Cada span lleva el recurso del servicio: `service.name` (el `app_name`, salvo que
exista `OTEL_SERVICE_NAME`), `service.version` y `deployment.environment.name`.

`/health`, `/ready` y `/metrics` no se trazan (`exclude_paths`): los probes y los
scrapes llegan varias veces por segundo, para siempre, y nadie los lee.

## Qué nunca se traza

**Texto de prompts, documentos, respuestas, consultas de búsqueda, cuerpos de
request y de respuesta, parámetros de SQL.** La misma regla que el ledger de
`llm`, aplicada en tres lugares:

- Los puntos de llamada pasan solo ids, nombres, conteos y duraciones. `llm.chat`
  conoce los mensajes y no los pasa; `rag.search` conoce la consulta y no la pasa.
- El backend descarta todo atributo que no sea un escalar pequeño -- un dict nunca
  se convierte en texto -- y corta los textos a 256 caracteres.
- Los spans de SQL nunca leen los parámetros. El texto de la sentencia está
  apagado por defecto (`record_sql_statement = true` lo enciende): una sentencia
  escrita con literales en vez de parámetros los llevaría.

La ruta cruda tampoco se registra, solo la plantilla: una ruta puede traer un
correo o un id por request, y el request id ya lleva a la línea de log que la tiene.

Las excepciones se registran con tipo, mensaje y stack trace, como en toda
integración de OpenTelemetry. Los errores del framework dicen qué falló, no qué se
mandó (`LLMError` lleva el mensaje del proveedor, nunca el prompt); el mensaje que
tu código pone en una excepción es tuyo de mantener limpio.

## Entre servicios, jobs y eventos

Una sola traza cubre cada salto porque el `traceparent` de W3C viaja con el trabajo:

- **Los requests entrantes** continúan la traza de quien llama: el padre del span
  de servidor es el header `traceparent` cuando viene.
- **El cliente `http`** manda el `traceparent` y el `tracestate` del span actual en
  cada llamada, dentro de un span de cliente propio, así que el span de servidor
  del siguiente servicio es hijo de esa llamada. Ver
  [http-client.md](http-client.md#que-viaja-con-la-llamada).
- **El gateway** reemplaza el `traceparent` del cliente por el de su propio span,
  un salto más adentro de la misma traza. Con telemetry apagado reenvía intacto el
  del cliente, así que el upstream todavía puede continuar esa traza.
- **Los jobs y los eventos** llevan el contexto de traza del request que los creó,
  junto a su tenant y su request id, y el worker corre el handler dentro de él:
  los spans del job se unen a la traza del request en vez de empezar una huérfana.

Un cliente que no es el del framework -- una llamada directa con `httpx` -- tiene
que mandar el header él mismo:

```python
from jfastframework import tracing

await httpx_client.get(url, headers=tracing.inject())
```

Y el trabajo que vale la pena ver en tu código lleva un span igual que el del
framework, gratis cuando telemetry está apagado:

```python
with tracing.span("invoice.render", invoice_id=invoice.id, pages=len(pages)):
    pdf = render(invoice)
```

El provider además queda como el global de OpenTelemetry (`set_global_provider`,
solo si nadie puso uno antes), así que `opentelemetry.trace.get_tracer(__name__)`
en tu código o una instrumentación de terceros se une a las mismas trazas.

## Jaeger en local

```toml
[plugin.telemetry]
include_infra = true
```

`jfast deploy compose` agrega entonces un OpenTelemetry Collector y Jaeger, y apunta
la API al collector (`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` en la
red de compose). En un workspace todos los servicios nombran los mismos dos
contenedores, así que hay **un collector y un Jaeger para todo el workspace**, y el
contenedor de cada servicio recibe la misma variable. La configuración del
collector va en línea en su comando -- OTLP de entrada, un batch, OTLP de salida a
Jaeger --, así que no hay archivo que mantener junto al compose.

| | Puerto en el host |
| --- | --- |
| UI de Jaeger | `http://localhost:16686` (`jaeger_ui_host_port`) |
| Collector, OTLP/HTTP | `http://localhost:4318` (`collector_host_port`) |

Un servicio que corre fuera de compose (`jfast dev`) exporta al collector
publicado con `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` en su `.env`.

## Muestreo

```toml
[plugin.telemetry]
sample_ratio = 0.1
```

Conserva una traza nueva de cada diez, decidido por el trace id. Un request que
llega con un `traceparent` muestreado siempre se conserva y uno que llega sin
muestrear siempre se descarta, así que una traza nunca queda registrada en un
servicio y ausente en el siguiente: se decide en el borde (el gateway o el primer
servicio) y todo lo que está detrás lo sigue. Un request no muestreado igual lleva
su `traceparent` hacia adelante.

## Configuración

```toml
[plugin.telemetry]
# endpoint = "http://collector:4318"    # mejor OTEL_EXPORTER_OTLP_ENDPOINT
sample_ratio = 1.0
sql = true                               # un span por sentencia SQL
record_sql_statement = false
exclude_paths = ["/health", "/ready", "/metrics"]
export_timeout = 10.0                    # segundos por exportación
shutdown_timeout = 5.0                   # segundos para vaciar al apagar
exporter = "otlp"                        # "memory" para pruebas, "console" para imprimir
include_infra = false
```

La API key de un backend hospedado va en el entorno, nunca en `jfast.toml`:
`JFAST_TELEMETRY_HEADERS='{"x-honeycomb-team": "..."}'`, o el estándar
`OTEL_EXPORTER_OTLP_HEADERS`.

Los spans se exportan en lotes desde un hilo en segundo plano. Al apagar se vacía
el último lote, fuera del event loop y dentro de `shutdown_timeout`, así que un
collector caído retrasa el apagado a lo mucho eso.

## Salud

`/ready` reporta el exportador, nunca como crítico -- un collector caído no debe
sacar al servicio de rotación:

- `not exporting: set OTEL_EXPORTER_OTLP_ENDPOINT ...` cuando no hay endpoint.
- `exporting to http://collector:4318/v1/traces` con los spans exportados y los
  lotes fallidos hasta ahora.
- `span export failing (...); spans are being dropped` después de un lote fallido,
  hasta que uno salga bien. Al endpoint mostrado se le quitan user-info y query.

## Las fallas nunca llegan al request

Telemetría que rompe un request es peor que ninguna. Iniciar, terminar o anotar un
span, extraer un header, registrar una sentencia SQL: cada paso está protegido, y
una falla es una línea de log en debug y un request sin trazar. Un exportador que
lanza o un collector que rechaza se cuenta para `/ready` y lo registra el propio
exportador; el request nunca lo ve. `tests/test_telemetry.py` reemplaza el tracer
y el backend por objetos que fallan en cada atributo y comprueba que el request se
atiende igual.

## Probar tus propios spans

```toml
# configuración de pruebas
[plugin.telemetry]
exporter = "memory"
```

```python
spans = app.state.jfast.require("telemetry").exporter.get_finished_spans()
assert any(span.name == "invoice.render" for span in spans)
```

El exportador en memoria registra de forma síncrona: el span está ahí en cuanto
termina.

## Cuánto cuesta

Medido en proceso para 0.1.0a11: un `GET /users/{user_id}` llamado directo por la
app ASGI (sin sockets ni cliente HTTP), `observability` en `WARNING`, mediana de
siete rondas de 5,000 requests, tres corridas, laptop Apple serie M:

| | us por request | sobre sin plugin |
| --- | --- | --- |
| Sin plugin `telemetry` | 41 | -- |
| `telemetry` habilitado, sin endpoint | 41 | dentro del ruido entre corridas (±5) |
| `telemetry` exportando (exportador en memoria) | 66 | +19 a +29 (mediana +24) |

Sin endpoint el costo no es medible -- no hay nada instalado que cueste. Exportando,
es el precio de un span de servidor por request y sus atributos; frente a un
endpoint que consulta PostgreSQL (1-5 ms) o llama a un modelo no se nota, y
`sample_ratio` lo baja para los requests que no se conservan. El exportador OTLP
real agrega la serialización y el HTTP del hilo de lotes, que corre fuera del
camino del request.

## Qué está verificado y qué no

**Probado** (`tests/test_telemetry.py`, exportador en memoria): sin endpoint no se
instala nada y se avisa; spans de servidor con plantilla de ruta, tenant, request
id y status; un `traceparent` entrante continuado; probes excluidos; excepciones y
respuestas 5xx registradas; muestreo, incluido un padre muestreado conservado con
ratio 0; spans de SQL contra PostgreSQL como hijos del request, sin parámetros, con
la sentencia solo si se pide, fallas marcadas, el listener retirado al apagar; dos
apps -- A llamando a B con el cliente `http` por transporte ASGI -- en una sola
traza, con el span de servidor de B hijo del span de cliente de A; el gateway
reenviando su propio span y, con telemetry apagado, el header de quien llama;
spans de `llm` y `rag` con sus conteos y costo y sin texto de prompt, documento o
respuesta en ningún atributo ni evento; un tracer roto, un backend roto y un
exportador que falla sin romper nunca un request; la salida de compose.

**Revisado a mano, no en CI** (2026-09-30): el par generado para compose --
`otel/opentelemetry-collector:0.136.0` con su configuración en línea y
`jaegertracing/jaeger:2.10.0` -- arrancó, un servicio exportó por OTLP/HTTP al
collector, y las trazas se leyeron de vuelta en la API de Jaeger bajo el nombre del
servicio, con la plantilla de ruta como nombre de span. **No probado:** un backend
hospedado (Honeycomb, Tempo, Datadog) ni TLS hacia el collector.
