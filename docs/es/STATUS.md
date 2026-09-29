# Madurez por subsistema

Un solo número de versión no puede describir este repositorio con honestidad.
El kernel está testeado, tipado y ejercitado por CI en cada push; el cliente de
Kafka nunca habló con un broker. Llamar "0.7.0" a los dos no le decía nada al
lector, y por eso el paquete vuelve a empezar en `0.1.0a1` y por eso la madurez
se registra aquí, por subsistema.

## Niveles

| Nivel | Significa |
| --- | --- |
| `beta` | Ejercitado por CI contra la dependencia real. La API todavía puede cambiar, el comportamiento se conoce. |
| `alpha` | Testeado en aislamiento, usado por el autor, no probado contra tráfico de producción. |
| `experimental` | Publicado para recibir feedback. Espera que la API se mueva. |
| `unverified` | Escrito contra APIs documentadas. **Nunca ejecutado contra la dependencia real.** |
| `broken` | Un defecto conocido, nombrado abajo. Todavía no construyas sobre esto. |

**Criterio de promoción:** un subsistema no sube de nivel porque se sienta más
terminado. Sube cuando un job de CI lo ejercita contra la dependencia real —
un contenedor, un broker, un cluster. Es la misma regla que dejó salir el
scaffold de Go y dejó afuera el de Angular.

## Kernel

| Subsistema | Nivel | Notas |
| --- | --- | --- |
| `create_app`, registro de plugins, `AppContext` | `beta` | Suite de tests completa, `mypy --strict`, descubrimiento por entry-point y detección de ciclos cubiertos. |
| Settings y carga de `jfast.toml` | `beta` | Tipado, sobreescribible por entorno. CORS, trusted hosts, límite de tamaño de body y timeout de request son settings; todos apagados salvo que se configuren. |
| Modelo de errores RFC 7807 | `beta` | Handlers para errores de dominio, HTTP, validación y no manejados. |
| `/health`, `/ready`, `/info` | `beta` | El readiness corre cada check en paralelo bajo un timeout, y reporta `timeout` aparte de `fail`. `/docs` y `/openapi.json` se cierran en producción junto con `/info`. |
| Fixtures de test | `beta` | `build_test_app`, `client_for`, `NullPlugin`. |

## Plugins

| Plugin | Nivel | Notas |
| --- | --- | --- |
| `observability` | `beta` | Logs JSON, correlación por request-id y por tenant. Cero dependencias. ASGI puro desde 0.1.0a10 (era `BaseHTTPMiddleware`, ~75 us por petición). |
| `metrics` | `beta` | Labels por plantilla de ruta, así los parámetros de path no pueden explotar la cardinalidad -- dicho aquí desde el inicio y cierto solo desde 0.1.0a10: el middleware leía la ruta antes de que corriera el router y etiquetaba por la ruta cruda. Ahora probado con parámetros de path, routers incluidos, montajes y rutas sin match en `tests/test_performance_guards.py`. ASGI puro desde 0.1.0a10. |
| `contracts` (checker) | `beta` | Corre en CI contra un servicio generado; una violación rompe el build. Capas, llamadas e imports prohibidos, estructura requerida, y bloqueo del event loop. |
| `database` | `alpha` | Conexiones con nombre, split read/write con pinning al primario, paginación keyset y sin COUNT, engines por tenant detrás de un LRU acotado, y un filtro de tenant que lanza en vez de fallar abierto. El pinning y los pools se ejercitan contra un PostgreSQL real, con la réplica simulada como una segunda base deliberadamente atrasada — eso reproduce el lag, que es lo que rompe read-after-write, y **no** replicación por streaming ni failover. Nunca se testeó un standby físico. Hasta `0.1.0a8` la sesión de la request confirmaba después de enviar la respuesta, así que un commit fallido contestaba 2xx; ahora confirma antes, y el plugin no arranca con una ruta que no lo haga -- incluida una dependencia de sesión propia del servicio, una vez marcada `@transactional`; las que no están marcadas y hacen commit después de su `yield` aparecen en una advertencia. Las violaciones de constraint y las versiones viejas son 409/412, y las carreras -- veinte inserts concurrentes contra una constraint única, un contador con `FOR UPDATE` y con advisory lock, un fallo de serialización real reintentado hasta tener éxito -- corren contra PostgreSQL en `tests/test_transactions.py`. |
| `cache` | `alpha` | Fachada de Redis y health check. No se corre contra un Redis real en CI. |
| `auth` | `alpha` | Verificación, rotación de JWKS, detección de reuso de refresh y los defaults de ataque rechazado están testeados. Hasta `0.1.0a4` la detección de reuso se testeaba bajo el supuesto de una sola sesión — el test afirmaba `is_family_revoked(subject)`, que es el bug escrito como expectativa — así que un logout revocaba todas las sesiones de esa persona y envenenaba su siguiente login. Ahora las familias son por sesión, y los casos de dos sesiones están cubiertos. Sin PKCE, sin mTLS, sin proveedor de identidad real en CI. |
| `accounts` | `alpha` | Usuarios, login con contraseña argon2id, bloqueo tras fallos repetidos, roles y permisos que viajan como scopes del token, administración por tenant y `on_refresh` para que un permiso retirado o una cuenta desactivada terminen en el siguiente refresh. Probado por HTTP sobre SQLite y PostgreSQL en `tests/test_accounts.py`. Todavía sin verificación de email, recuperación de contraseña ni MFA. |
| `tenancy` | `alpha` | El orden de resolución es correcto y está testeado. Por sí solo el filtro del repositorio es una **convención**: un `session.execute` crudo se lo salta. Con `[plugin.database] rls = true` y `enable_tenant_rls` en una migración lo impone el row-level security de PostgreSQL: cada transacción fija su tenant con un `set_config` local a la transacción, y una query sin tenant no ve filas. Verificado contra PostgreSQL con un rol que no es superusuario en `tests/test_rls.py`, incluido que el tenant no sobrevive a la siguiente transacción en una conexión del pool. Producción rechaza RLS con un rol superusuario o `BYPASSRLS`, que ignoran toda política. Las policies con más que el tenant -- `transaction_setting` más `enable_rls_policy` -- se verifican igual, con un tenant, sus empresas y un usuario que ve algunas. Todavía no se probó detrás de PgBouncer. La fuente `user` (cada cuenta con sesión es su propio tenant) y `current_tenant` están cubiertas en `tests/test_tenancy.py`. |
| `storage` (disco local) | `alpha` | Validación de claves, chequeo de symlinks, escrituras atómicas, URLs firmadas, y un pipeline de upload cuyo paso `validate` olfatea el content type de los bytes en vez del nombre de archivo — XML, JSON, CSV y texto parseándolos, con `DOCTYPE`/`ENTITY` rechazados — todo testeado. `put_stream` y `guard_stream` escriben subidas que no caben en memoria, validadas sobre los primeros 64 KiB y con tope conforme llegan. La resolución de claves independiente del disco y el copy-on-read vienen apagadas; el ledger que se entrega es en memoria y está documentado como solo-desarrollo. |
| `storage` (S3 / MinIO) | `unverified` | Nunca se corrió contra un endpoint real de S3 o MinIO. El `put_stream` multipart, incluido el abort si falla, solo se prueba contra un doble en memoria. |
| `encryption` | `alpha` | AES-256-GCM con contexto autenticado y rotación de llaves, en una columna o a mano. Se prueban el viaje de ida y vuelta, la alteración, un valor movido a otro contexto, la rotación y el tipo de columna; guardar la llave le toca al servicio, por el entorno o `load_secrets`. |
| `web` (Jinja + HTMX) | `alpha` | Renderizado y con smoke test; sin test a nivel de browser. |
| `gateway` | `alpha` | Ruteo por prefijo, stripping de headers, errores problem+json. Un upstream por prefijo: sin pool y sin balanceo de carga. El rate limiting es el plugin `ratelimit` de abajo; el README prometió uno durante un año antes de que existiera. |
| `ratelimit` | `alpha` | Token bucket en un script Lua, así el read/decide/write es indivisible; la afirmación de concurrencia se verificó primero contra un read-then-write ingenuo en Python, donde los 20 requests pasaron un límite de 5. Corre contra un Redis real. Falla abierto a propósito, logueando y reportando `/ready` degradado en vez de convertir una caída del caché en una caída total. Se aplica como dependencia de FastAPI, así que un request que no matchea ninguna ruta no se limita — eso es trabajo del edge. No pasa a `beta` hasta que CI lo haya corrido. |
| `queue` (worker, todo backend) | `alpha` | Un job toma el tenant y el request id de la request que lo encoló, y el worker los restaura alrededor del handler; hasta `0.1.0a8` nadie los llenaba, así que todo job corría sin tenant. `enqueue` sigue confirmando en su propia transacción, aparte de las filas de la request. Cubierto por `tests/test_transactions.py`. |
| `queue` (PostgreSQL) | `alpha` | `FOR UPDATE SKIP LOCKED`, reclamo correcto de jobs de un worker muerto. No se corre contra un PostgreSQL real en CI. |
| `queue` (Redis) | `alpha` | Visibility timeout implementado: los workers mandan heartbeat a un registro con tiempo de servidor y cualquier worker reclama los jobs en vuelo de un par muerto. Testeado contra un doble en memoria de los comandos de Redis, todavía no contra un servidor real. |
| `queue` (RabbitMQ) | `alpha` | Los retrasos y el backoff de reintentos los sostiene el broker en una cascada de colas con TTL fijo, así que un retraso largo no puede bloquear uno corto. Verificado contra rabbitmq:3.13 en `tests/test_rabbitmq_queue.py`: retraso, orden de vencimiento, head-of-line, backoff de reintentos, dead-lettering, retrasos más largos que la cascada, y un worker reintentando un handler que falla. La CI exige que no se salte, pero **ninguna corrida de CI lo ha ejecutado todavía**. No probado en cluster, tras un reinicio del broker, ni con quorum queues; si el broker se cae durante un salto entre niveles puede perder ese mensaje. |
| `queue` scheduler | `alpha` | Corre en cada réplica; cada tick se reclama en PostgreSQL (`jfast_schedule_ticks`) o en Redis, y el id del job es determinista. Verificado contra PostgreSQL 16 real (20 reclamos concurrentes quedan en uno, reclamo y job se confirman juntos, dos apps corriendo encolan cada tick una vez, poda) y Redis 7 real en `tests/test_scheduler.py`; cron y horario de verano cubiertos por 77 tests unitarios. Un proceso que muere entre reclamar y encolar pierde ese tick cuando el almacén y la cola son sistemas distintos. Ninguna corrida de CI ha ejecutado los tests contra servidores reales todavía. |
| `http` (cliente entre servicios) | `alpha` | Timeouts, reintentos, presupuesto de reintentos, circuit breaker, bulkhead y propagación de headers probados contra `httpx.MockTransport` con reloj falso. **Nunca se ha enviado por un socket real**; sin TLS, sin prueba de carga; no soporta streaming. Los breakers son por proceso. |
| `outbox` | `alpha` | Los mensajes escritos por la sesión de la request se confirman con sus filas o no se confirman; con la cola de PostgreSQL en la misma base el job entra directo a `jfast_jobs`. El relay toma filas con `FOR UPDATE SKIP LOCKED`, hace backoff y aparta un mensaje como muerto tras `max_attempts`; `claim_once` deduplica del lado del consumidor. Dos relays compitiendo por 60 filas envían cada una exactamente una vez contra PostgreSQL en `tests/test_outbox.py`. El relay no se ha corrido contra Redis, RabbitMQ ni Kafka reales. |
| `idempotency` | `alpha` | `Idempotency-Key` registrada en la transacción de la request: un reintento repite la respuesta guardada, otro cuerpo con la misma llave es 422, un duplicado concurrente espera al primer insert y recibe 409 o la repetición. Cuatro requests simultáneas con una llave escriben una sola fila contra PostgreSQL en `tests/test_idempotency.py`. La respuesta se registra después de enviarse, así que un proceso que muere en medio deja la llave en progreso hasta que vence. |
| `events` (Kafka) | `unverified` | Nunca se corrió contra un broker. Sin outbox, así que una fila commiteada puede perder su evento. |
| `mongo` | `alpha` | Cliente y handle nada más. Sin contrato de documento, sin migraciones, sin paridad de repositorio con SQL. |
| `qdrant` | `alpha` | Cliente, health check, contenedor. |
| `rag` | `alpha` | Identidad por tenant, tenant obligatorio en cada llamada, HNSW, búsqueda híbrida con reciprocal rank fusion, fragmentación por estructura y re-ingesta que solo embebe los fragmentos que cambiaron; `schema_sql()` para una migración en vez de DDL al arrancar. El store de pgvector está verificado contra PostgreSQL 16 + pgvector 0.8 en `tests/test_rag_pgvector.py`, incluida la actualización en su lugar de una tabla de 0.1.0a9 y row-level security con un rol que no es superusuario. El store de Qdrant **no se corre contra un servidor**: sigue el mismo protocolo y pasa el chequeo de tipos, nada más. Sin reranking, sin OCR, sin búsqueda híbrida en Qdrant. |
| `llm` | `alpha` | Chat compatible con OpenAI, salida JSON estricta, imágenes y embeddings. El presupuesto reserva el peor caso de forma atómica antes de cada llamada y se ajusta al costo real; una llamada que falla libera la reserva. Probado con `httpx.MockTransport` en `tests/test_llm.py`; el ledger de Redis es `INCRBYFLOAT` y no se ha corrido contra un Redis real en CI. **Nunca se ha llamado a un proveedor real desde CI.** Sin streaming. |
| `notifications` (FCM) | `unverified` | La construcción del payload está testeada; nunca se hizo una entrega desde CI. |
| `channels` | `alpha` | Pub/sub declarado. El backend de memoria está cubierto por tests; los backends de redis y kafka no se corren contra un servidor real en CI. |
| `mail` | `alpha` | Plantillas, encolado y el backend de consola están testeados. Nunca se envió un mensaje a través de un servidor SMTP real desde CI. |
| `websocket` | `alpha` | Handshake, registro, backpressure, heartbeat y expiración de token están cubiertos en aislamiento. La entrega cross-worker se afirma contra un `redis:7-alpine` real — dos instancias de la app, un socket en cada una, exactamente una vez — y el autor le hizo mutation testing a esa afirmación, encontrando que pasaba con un bug de doble entrega inyectado antes de arreglarlo. CI ahora levanta Redis y falla si el test se saltea, pero **ninguna corrida de CI lo ejecutó todavía**, así que esto queda en `alpha` hasta que alguna lo haga. Sin cliente de browser, sin proxy, sin load test, y dos workers en un proceso en vez de dos procesos. |
| `sentry` | `alpha` | Apagado por defecto. |

## Generador y deployment

| Área | Nivel | Notas |
| --- | --- | --- |
| `jfast new module` / `new service` (Python) | `beta` | Todos los layouts (`modular` por defecto), nombres de tabla en español o inglés, el overlay de HTMX, y el cableado de Alembic y pytest se renderizan y corren en CI. |
| Scaffolds de Vue y React | `alpha` | `npm install` más `vite build` corren en CI, que es lo que detectó el bug del marker del router. Los dos looks -- `nexora` (el de por defecto) y `classic` -- se compilan en los dos frameworks en `smoke_frontend.sh`, y el look queda registrado en `.jfast-template` y lo sigue `jfast new view`. Las pantallas de Nexora y el selector de color se revisaron a mano solo en Chrome: ni Safari ni Firefox, y el respaldo sin WebGL no se ha visto. Sin test en runtime. |
| Scaffold de servicio en Go | `alpha` | `go vet`, `go test`, `go build`, y después se arranca el binario y se le hace curl. |
| gRPC | `experimental` | El contrato `.proto` se genera y el puerto queda reservado. Sin stubs, sin cableado de servidor. |
| Workspaces y asignación de puertos | `beta` | Los recursos son instancias con nombre y los servicios se enlazan a ellas bajo una variable; todo el flujo se ejercita en CI. Un archivo 0.1 todavía carga y `migrate-resources` lo convierte. |
| `deploy compose` / `dockerfile` | `beta` | Derivados del grafo de plugins. Un contenedor por recurso con los connection strings al lado, y la imagen generada se buildea y se corre contra un PostgreSQL real en CI. |
| `workspace k8s` | `unverified` | Los manifiestos se afirman estructuralmente en los tests. Nunca se aplicaron a un cluster, ni siquiera a kind. |
| `deploy function` (Lambda / Cloud Run) | `unverified` | Escribe scripts en vez de correrlos; nunca se aplicó contra una cuenta real. |
| `load_secrets` (AWS / GCP) | `unverified` | Nunca se corrió contra un secret manager real. |

## Directamente ausente

Nombrado aquí para que nadie tenga que hacer grep para enterarse:

- **Sin tracing distribuido.** Solo logs y métricas; `request_id` te da grep, no spans.
- **Sin diagramas de esquema ni de módulos.** `jfast workspace graph` dibuja los servicios y los recursos; el esquema de base de datos y el grafo de imports de módulos, no.
- **Sin casos de uso declarados.** Lo que hace un servicio no está escrito en ningún lugar que un build pueda chequear.
- **Sin SSE.** Los websockets existen (ver `websocket` arriba); el helper de
  server-sent-events, que es lo que la mayoría de las features de una sola vía
  debería usar en su lugar, no.
- **Sin lock de dependencias, y con pisos que nadie prueba.** Los cuatro paquetes del camino de la request tienen techo, así que una versión mayor no llega sin aviso. Los pisos están declarados pero la CI instala lo más nuevo de todo, así que un piso que dejó de alcanzar pasa desapercibido -- `0.1.0a9` encontró que el de FastAPI estaba mal por esto. No hay registro de contra qué versiones se probó cada commit.
- **Publicado, solo pre-releases.** `pip install jfastframework` resuelve la última alpha, porque pip toma un pre-release cuando no existe una versión final. La primera versión final cambia eso: desde ahí una alpha necesitará `--pre`.

[PLAN-NEXT.md](PLAN-NEXT.md) es el plan ordenado para cerrar todo eso.
