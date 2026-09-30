# El contrato de servicio

Un servicio JFast no es "un servicio escrito con JFast". Es un servicio que
cumple este contrato. Esa distinción es toda la razón por la que un servicio
en Go y uno en Python pueden estar detrás del mismo gateway, tomar puertos del
mismo workspace, y ser expuestos por el mismo Caddyfile — nada de lo cual sabe
ni le importa qué lenguaje los produjo.

Mantén esto estable. Los templates son implementación; esto es la interfaz.

---

## 1. Endpoints de sistema

| Endpoint | Debe | No debe |
| --- | --- | --- |
| `GET /health` | Devolver 200 mientras el proceso esté arriba | Sondear ninguna dependencia |
| `GET /ready` | Devolver 200 cuando sirve, 503 cuando una dependencia **crítica** está caída | Usarse como liveness probe |
| `GET /info` | Reportar versión e inventario | Existir cuando `JFAST_ENV=prod` |

`/health` y `/ready` están separados porque confundirlos causa reinicios en
cascada: la base parpadea, el liveness falla, el orquestador mata pods sanos, y
la estampida termina de matar la base.

Que falle una dependencia no crítica hace que `/ready` reporte `"degraded"` con
un 200. Una cache fría no debería sacar a un servicio de rotación.

```json
// GET /health
{"status": "ok", "service": "billing", "version": "0.1.0", "env": "local"}

// GET /ready
{"status": "degraded", "service": "billing",
 "checks": {"cache": {"healthy": false, "detail": "cold", "critical": false}}}
```

## 2. Correlación de requests

Lee `X-Request-ID` del request. **Reúsalo si viene**, genera uno si no.
Devuélvelo en la respuesta, adjúntalo a cada línea de log, y reenvíalo en las
llamadas salientes.

Generar un id nuevo en cada servicio en vez de reusar el del llamante es la
forma más común de que un trace distribuido quede inservible: cada hop arranca
un "trace" nuevo y nada se une.

Lo mismo vale para el trace context de W3C. Lee `traceparent` y `tracestate`;
descarta un `traceparent` inválido (versión `00`, hex en minúsculas, un trace
id de 32 hex y un parent id de 16 hex que no sean todo ceros, flags de 2 hex)
junto con su `tracestate`; loguea el trace id como `trace_id`; y manda el
contexto en las llamadas salientes. Un servicio que exporta spans (el plugin
`telemetry` de Python) manda su propio span como padre. Uno que no (el
scaffold de Go) pasa el del llamante **sin cambios** -- inventar un parent id
que nunca se exporta deja un hueco en el trace.

Lo que lleva una llamada saliente, en cualquier lenguaje: `X-Request-ID`,
`traceparent`, `tracestate`. No el tenant (el siguiente servicio lo resuelve
del token) y no `Authorization`, salvo que quien llama lo pida -- el
`forward_authorization = true` del cliente de Python.

## 3. Errores

Toda falla se serializa a RFC 7807 `application/problem+json`:

```json
{"type": "about:blank", "title": "Not Found", "status": 404,
 "detail": "invoice 7 not found", "instance": "/invoices/7",
 "request_id": "9f2c…"}
```

Las excepciones no manejadas incluidas. El `detail` de un error no manejado
debe ser genérico salvo que `JFAST_DEBUG=true` — filtrar internos a los
clientes es un hallazgo de information disclosure, y el default tiene que ser
el seguro.

## 4. Configuración

Desde el entorno, con estos nombres:

| Variable | Significado |
| --- | --- |
| `JFAST_APP_NAME` | Nombre del servicio, usado en logs y métricas |
| `JFAST_ENV` | `local` / `dev` / `staging` / `prod` |
| `JFAST_PORT` | Puerto HTTP — la base del bloque del servicio |
| `JFAST_DEBUG` | Si el detalle interno de los errores llega a los clientes |
| `JFAST_LOG_JSON_LOGS` | Logs estructurados encendidos o apagados |

Una sola convención de `.env` cubre todos los lenguajes. No inventes nombres
por lenguaje.

## 5. Puertos

Un servicio es dueño de **diez puertos consecutivos** desde su base. Los
plugins reclaman offsets dentro de ese bloque:

| Offset | Uso |
| --- | --- |
| +0 | HTTP |
| +1 | PostgreSQL |
| +2 | Kafka / PgBouncer |
| +3 | Redis |
| +4 | MongoDB |
| +5 / +6 | Prometheus / Grafana, RabbitMQ |
| +7 / +8 | Qdrant HTTP / gRPC |
| +9 | El gRPC propio del servicio |

El workspace asigna los bloques. No elijas un puerto a mano sin haber revisado
qué hay ya dentro de uno.

## 6. Logs

Un objeto JSON por línea, en stdout, llevando al menos `service`, `env`,
`level`, `message` y — dentro de un request — `request_id`.

No un archivo, no un socket. El runtime recolecta stdout; un servicio que
maneja sus propios archivos de log pelea con lo que ya los esté recolectando.

La línea de access log es `"message": "request"` con `http_method`,
`http_path`, `http_status`, `duration_ms` y -- cuando el request los tiene --
`trace_id` y `tenant_id`.

## 7. Identidad y tenant (cuando el servicio autentica)

Opcional, y apagado por default. Un servicio que autentica verifica los JWT
del workspace con las reglas del plugin `auth` de Python y lee las mismas
variables, así que un token se comporta igual contra cualquier servicio:

| Variable | Significado | Default |
| --- | --- | --- |
| `JFAST_AUTH_MODE` | `secret` (HMAC), `public_key` (PEM fijo) o `jwks` | -- |
| `JFAST_AUTH_ALGORITHMS` | Algoritmos permitidos, p. ej. `["HS256"]`. Nunca se leen del token; `none` nunca; HMAC y RSA/EC nunca mezclados | `["RS256"]` |
| `JFAST_AUTH_SECRET` / `JFAST_AUTH_PUBLIC_KEY` / `JFAST_AUTH_JWKS_URL` | La llave del modo | -- |
| `JFAST_AUTH_ISSUER` / `JFAST_AUTH_AUDIENCE` | Se verifican, y entonces son obligatorios, cuando están | vacío |
| `JFAST_AUTH_LEEWAY` | Desfase de reloj permitido en `exp`, `nbf`, `iat`, en segundos | `30` |
| `JFAST_AUTH_TENANT_CLAIM` / `_SCOPE_CLAIM` / `_ROLES_CLAIM` | Dónde los guarda el token | `tenant_id` / `scope` / `roles` |
| `JFAST_TENANCY_SOURCES` | De dónde sale el tenant, en orden de confianza: `token`, `user`, `subdomain`, `path`, `header` | `["token", "subdomain"]` |
| `JFAST_TENANCY_BASE_DOMAIN` | Lo necesita la fuente `subdomain` | vacío |
| `JFAST_TENANCY_REQUIRE_TENANT` | Rechazar con 403 todo request que no resuelva tenant, fuera de `/health`, `/ready` y similares | `false` |

Las reglas: `exp`, `iat` y `sub` son obligatorios; un token con
`typ: refresh` no sirve como bearer; un token malo en una ruta abierta se
ignora, no se rechaza. Responde **401** cuando no hay un llamante verificado y
**403** cuando lo hay pero le falta el scope, el rol o el tenant -- un cliente
refresca su sesión ante un 401 y se rinde ante un 403. El tenant sale de un
claim firmado antes que de cualquier cosa que el request pueda elegir;
`X-Tenant-ID` solo es fuente cuando `header` está en la lista.

En Python la configuración de los plugins también puede venir de `jfast.toml`
(`[plugin.auth]`, `[plugin.tenancy]`), que le gana al entorno -- salvo
`JFAST_ENV` y `JFAST_DEBUG`, que le ganan a `[app] env` y `debug` cuando están
puestas, porque describen el despliegue ([deploy](deploy.md#quien-gana-jfasttoml-o-el-entorno)). Un servicio en
otro lenguaje solo tiene el entorno, así que escribe en su `.env` los valores
que debe compartir -- modo, algoritmos, secreto o llave pública, issuer,
audience, fuentes de tenancy. Nada los copia entre servicios por ti: un
servicio de Python obtiene su secreto igual, de su propio `.env`.

El scaffold de Go enciende cada parte solo con el entorno, porque no tiene
lista de plugins: auth cuando `JFAST_AUTH_MODE` está puesto, o cuando está
exactamente una de las variables de llave (la llave nombra el modo, y los
algoritmos toman `HS256` para un secreto, `RS256` si no); tenancy cuando
`JFAST_TENANCY_SOURCES` está puesto. Una configuración que no puede hacer
cumplir -- `jwks`, familias de algoritmos mezcladas, un secreto corto en
producción, un PEM usado como secreto HMAC -- lo detiene al arrancar en vez de
dejarlo responder requests sin verificar.

---

## Lenguajes

| | Python | Go |
| --- | --- | --- |
| Toolchain | `python3` | `go` |
| Implementación del contrato | `jfastframework` (importado) | `internal/jfast/` (vendored, solo biblioteca estándar, ~1.250 líneas de código) |
| Trace context | se propaga; spans con el plugin `telemetry` | pasa sin cambios; sin spans (agrega `otelhttp` tú) |
| Auth (sección 7) | plugin `auth`: `jwks`, `public_key`, `secret`; emite tokens | solo verifica: `public_key`, `secret`; `jwks` no arranca |
| Revocación de tokens | se consulta en Redis en cada request | **no se consulta**: un access token revocado sirve hasta que vence (15 min por default) |
| Tenancy (sección 7) | plugin `tenancy` | mismas fuentes y 401/403; sin zona horaria por tenant |
| Sistema de plugins | sí | no |
| Generador de módulos | sí | un módulo de ejemplo |
| Migraciones | Alembic | las pones tú |
| Worker de colas | sí | no -- el formato de la tabla está documentado abajo |
| Kinds | `api`, `web`, `gateway` | `api` |

```bash
jfast new service billing                     # Python
jfast new service edge --language go          # Go
jfast new service edge --language go --grpc   # + the .proto contract
```

Solo necesitas el toolchain de los lenguajes que realmente uses. Un equipo que
trabaja solo en Python nunca instala Go.

### Go: contrato sí, framework no

El scaffold de Go trae lo que un servicio en Go necesita para ser buen
ciudadano de un workspace cuyos otros servicios son Python, y nada más: los
endpoints, el request id, el trace context, la forma de los errores, los logs,
los tokens y el tenant. El router, el ORM y los workers son tuyos -- Gin,
Echo, Chi, `pgx`, lo que el equipo ya conozca.

Todo en `internal/jfast/` es middleware de `net/http` plano
(`func(http.Handler) http.Handler`) sin dependencias de terceros. Un engine de
Gin, Echo o Chi es un `http.Handler`, así que `jfast.Chain(engine, ...)` lo
envuelve tal cual; los handlers leen `jfast.ClaimsFrom(ctx)` y
`jfast.TenantFrom(ctx)`. `main.go` arma, de afuera hacia adentro:
`RequestID`, `AccessLog`, `Trace`, `Recover`, `Authenticate`,
`ResolveTenant`; cada ruta opta por `RequireAuth`, `RequireScopes`,
`RequireRoles` y `RequireTenant`. Las llamadas salientes van por
`jfast.PropagatingTransport` o `jfast.Propagate`.

Lo que CI prueba: el servicio generado pasa `gofmt`, `go vet` y sus propios
tests (trace context, tokens HS/RS/ES, `alg: none`, confusión de algoritmos,
issuer y audience, orden de tenant, 401/403); el binario arranca y responde;
los tokens que emite el propio issuer del plugin auth de Python los acepta y
resuelve el mismo tenant que resuelve una app JFast; un `traceparent` llega
sin cambios a su llamada saliente; y el modo `jwks` no arranca.

### Por qué Go hace vendoring del contrato en vez de importar un módulo compartido

Con dos servicios, `internal/jfast/` son unos pocos archivos que lees de una
sentada. Con diez, extráelo a su propio módulo de Go e impórtalo. Extraerlo el día uno
te compra un problema de versionado antes de que haya algo que versionar.

### Dónde Go se gana el sueldo

Un hot path, un handler de conexiones de larga vida, un binario que quieres
que pese 12MB y arranque en 5ms. No "porque es más rápido" — el sistema de
plugins, las migraciones y el generador de módulos valen más que los
milisegundos en la mayoría de los servicios.

---

## Consumir la cola desde otro lenguaje

La cola de PostgreSQL es una tabla, así que un servicio en cualquier lenguaje
puede leerla. Esto es un **formato documentado, no un cliente soportado**:
JFast no trae worker de Go (ni de otro lenguaje), y nada aquí tiene más
promesa de compatibilidad que esta página y el changelog. Lee
`jfastframework/queues/postgres.py` de la versión que corres.

**Dale al otro lenguaje su propia tabla.** Un worker de Python reclama todas
las filas de la tabla que drena y manda a dead letters, al primer intento,
cualquier task para la que no tiene handler (`UnknownTask`). Un consumidor en
Go que comparte `jfast_jobs` con un worker de Python pierde esos jobs. Crea en
el lado que produce un segundo `PostgresQueue(engine, table="edge_jobs")` para
los jobs que son del servicio en Go.

### La tabla

Lo que crea `PostgresQueue.setup()` (nombre por default `jfast_jobs`, se
cambia con `[plugin.queue] name`):

| Columna | Tipo | Significado |
| --- | --- | --- |
| `id` | `TEXT` primary key | Id del job; 32 caracteres hex por default. Los inserts son `ON CONFLICT (id) DO NOTHING`, así que un id se encola una vez. |
| `task` | `TEXT` | El nombre del handler. |
| `payload` | `JSONB`, default `{}` | El argumento del handler. |
| `attempts` | `INTEGER`, default 0 | Lo incrementa cada claim. |
| `max_attempts` | `INTEGER`, default 3 | Pasado ese número, el job está muerto. |
| `available_at` | `TIMESTAMPTZ` | No antes de este instante: retrasos y backoff de reintentos. |
| `locked_until` | `TIMESTAMPTZ`, null | El lease de un job en ejecución. |
| `request_id` | `TEXT`, null | El request que lo encoló. |
| `tenant_id` | `TEXT`, null | El tenant con el que corre. |
| `status` | `TEXT` | `pending`, `running` o `dead`. Un job terminado se borra, no se marca. |
| `last_error` | `TEXT`, null | Por qué falló el último intento, hasta 2.000 caracteres. |
| `created_at` | `TIMESTAMPTZ` | Momento del encolado. |
| `trace` | `JSONB`, null | Carrier W3C del código que lo encoló: `{"traceparent": ..., "tracestate": ...}`, o null sin telemetría. |

### El ciclo de vida, como lo corre el worker de Python

- **Claim** atómico, una fila a la vez, con `FOR UPDATE SKIP LOCKED`:

  ```sql
  UPDATE edge_jobs SET status = 'running', attempts = attempts + 1,
         locked_until = NOW() + make_interval(secs => $1)   -- el visibility timeout, 300 s por default
  WHERE id = (
      SELECT id FROM edge_jobs
      WHERE available_at <= NOW()
        AND (status = 'pending' OR (status = 'running' AND locked_until < NOW()))
      ORDER BY available_at
      FOR UPDATE SKIP LOCKED
      LIMIT 1)
  RETURNING id, task, payload, attempts, max_attempts, request_id, tenant_id, trace;
  ```

  La segunda rama del `WHERE` es la que devuelve un job cuyo worker murió.
  Nada extiende el lease: un handler que corre más que el visibility timeout
  se vuelve a reclamar mientras sigue corriendo.
- **Ack**: `DELETE FROM edge_jobs WHERE id = $1`.
- **Reintento**: `UPDATE ... SET status = 'pending', locked_until = NULL,
  available_at = NOW() + make_interval(secs => $delay), last_error = $error`,
  con `delay = min(2 * 2^(attempts - 1), 300)` segundos.
- **Muerto** cuando `attempts >= max_attempts`, o de inmediato para un task
  que nadie maneja: `SET status = 'dead', locked_until = NULL,
  last_error = $error`. `jfast jobs dead` los lista y `jfast jobs retry` los
  regresa a `pending` con `attempts = 0`.
- **Release** al apagarse: `SET status = 'pending', locked_until = NULL,
  available_at = NOW(), attempts = GREATEST(attempts - 1, 0) WHERE id = $1
  AND status = 'running'` -- un job detenido por un deploy no falló.

### Eventos en la cola

El job de un suscriptor es `task = <task del suscriptor>` con
`payload = {"topic": ..., "event": <Event.to_dict()>}`, y su id se deriva del
id del evento y del task, así que publicar dos veces lo encola una. El sobre
del evento:

```json
{"id": "9f2c…", "type": "comprobante.registrado", "source": "billing",
 "occurred_at": "2026-09-30T12:00:00+00:00",
 "request_id": "…", "tenant_id": "acme",
 "trace": {"traceparent": "00-…-…-01"}, "key": null,
 "data": {"id": 42}}
```

`trace` es `{}` cuando quien publicó no tenía telemetría. El mismo sobre es el
que va a Kafka cuando hay un event bus configurado.

### Lo que un consumidor debe hacer para ser seguro

1. **Reclamar de forma atómica**, con la sentencia de arriba o su
   equivalente. Dos consumidores que leen y luego actualizan compiten y corren
   el job dos veces.
2. **Restaurar el tenant** desde `tenant_id` antes de tocar datos (en el
   scaffold de Go, `jfast.WithTenant(ctx, tenantID)`), y loguear `request_id`
   y el trace id de `trace` para que el job se una al request que lo encoló.
   Un job que corre sin tenant lee o escribe las filas de todos los tenants.
3. **Ser idempotente.** La entrega es al menos una vez: un consumidor puede
   morir entre confirmar su trabajo y borrar la fila. O registra
   `(consumer, message_id)` en `jfast_inbox` dentro de la transacción que hace
   el trabajo -- `INSERT ... ON CONFLICT DO NOTHING`, y salta el job si no se
   insertó ninguna fila, que es lo que hace `claim_once` en Python -- o
   deduplica por el id del job en tu propia tabla.
4. **Terminar dentro del visibility timeout**, o partir el trabajo.
5. **Acotar los reintentos** con `max_attempts` y mandar el resto a dead
   letters, para que un job venenoso no ocupe a un consumidor para siempre.

---

## Agregar un lenguaje

1. Implementa las secciones 1 a 6 de arriba, y la 7 si el servicio autentica.
2. Agrega un `LanguageSpec` a `jfastframework/languages.py`.
3. Agrega un árbol de templates `service_<lang>/`.
4. Agrega un job de CI que **buildee y corra** el servicio generado. Un
   scaffold que nadie corrió es un pasivo que parece un feature.

El paso 4 no es opcional. El soporte de Go en este repo existe porque CI
compila el servicio generado, corre sus tests, arranca el binario y le hace
curl — no porque el template se vea bien.

## Lo que deliberadamente no está en el contrato

- **Una librería cliente compartida.** Los servicios hablan HTTP o gRPC. Un
  cliente compartido es un deploy compartido.
- **Un ORM o formato de serialización común** más allá de JSON en el cable.
- **Un vendor de tracing obligatorio.** `X-Request-ID` y pasar `traceparent`
  son el piso; exportar spans es el plugin `telemetry` en Python y tu propio
  `otelhttp` en Go, no un mandato.
