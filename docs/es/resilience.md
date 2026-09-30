# Resiliencia

Qué hace un servicio cuando algo de lo que depende deja de responder: cuánto
espera, si reintenta, cuándo deja de preguntar, qué recibe el cliente y qué dice
`/ready`. Todo viene encendido por defecto. Esta página lista los números, las
razones detrás de ellos y los drills que los comprueban contra servidores reales.

Tres reglas atraviesan todos los plugins:

1. **Cada llamada externa tiene deadline, política de reintentos y breaker.**
   Una dependencia *caída* falla rápido por sí sola: la conexión se rechaza. La
   peligrosa es la *colgada*: acepta la conexión y nunca responde, que es como
   se ve un contenedor pausado, una VM congelada o una cola de accept llena. Sin
   deadline cada petición espera a que el sistema operativo se rinda, y sin
   breaker cada petición vuelve a pagar esa espera.
2. **Una caída responde 503, nunca 500 y nunca se cuelga.** Un 500 le dice a un
   cliente que se detenga y a quien lo lee que busque un bug; un 503 dice que la
   misma petición puede salir bien en un momento.
3. **`/ready` falla solo por lo que un reinicio o un cambio de ruta pueden
   arreglar.** Una dependencia compartida caída se reporta como *degradada*:
   sacar todas las réplicas de rotación por ella convierte una caída en dos.

## Defaults por dependencia

| Dependencia | Deadline | Reintentos | Breaker | Settings |
| --- | --- | --- | --- | --- |
| PostgreSQL, al conectar | 10 s (`connect_timeout`) | — | abre tras 2 conexiones fallidas, 5 s | `[plugin.database]` |
| PostgreSQL, revisión de conexión del pool | 2 s (`ping_timeout`) | una conexión nueva | — | `[plugin.database]` |
| PostgreSQL, sentencia | apagado (`command_timeout = 0`) | — | — | `[plugin.database]` |
| Redis (caché, rate limit, token store) | 1 s por comando, 2 s para conectar | — | abre tras 3 fallas, 5 s | `[plugin.cache]` |
| Proveedor de identidad (JWKS) | 5 s por descarga, completa | 2 intentos, solo 429/5xx/red | abre tras 3 refrescos fallidos, 30 s | `[plugin.auth] jwks_*` |
| S3 / MinIO | 5 s conectar, 30 s leer | 3 intentos ("standard" de botocore) | abre tras 5 fallas, 15 s | por disco |
| SMTP | 30 s por operación de socket | los de la cola (`max_attempts`) | — | `[plugin.mail]` |
| Proveedor de modelos | ver [LLM](llm.md) | | | `[plugin.llm]` |
| Servicios hermanos | ver [Cliente HTTP](http-client.md) | | | `[plugin.http]` |

Por qué estos números:

- **Conectar a PostgreSQL, 10 s.** El default de asyncpg es 60 s: dos request
  timeouts. Diez cubren TLS y SCRAM en un servidor ocupado (medido hasta 4.4 s
  en una laptop cargada) y todavía le dejan tiempo a la petición para responder
  503 por sí misma, bien dentro de los 30 s de `[app] request_timeout`.
- **Ping de PostgreSQL, 2 s.** El `pool_pre_ping` de SQLAlchemy no tiene
  deadline propio: una conexión del pool a una base que dejó de responder
  retenía su petición hasta que moría el socket; en la práctica, hasta el
  request timeout. El framework lo reemplaza con un `SELECT 1` acotado
  (protocolo simple, así funciona detrás de PgBouncer en modo transacción);
  pasados 2 s la conexión se termina y se intenta una nueva.
- **Sentencias de PostgreSQL, apagado.** Las migraciones y los reportes corren
  minutos legítimamente; una petición ya está acotada por `request_timeout` y un
  job por el `job_timeout` del worker. Configura `command_timeout` en un
  servicio cuyas consultas deban ser todas rápidas.
- **Redis, 1 s.** Redis responde en mucho menos de un milisegundo. El rate
  limiter y el token store están frente a cada petición, así que esto es también
  lo que esperan las primeras peticiones de una caída, por comando. Los comandos
  bloqueantes (`BLMOVE`, `BLPOP`, `XREAD`…) y pub/sub quedan fuera: esperar es
  su trabajo, y la cola y `channels` los leen del mismo cliente.
- **JWKS, 5 s, 30 s abierto.** Servir claves cacheadas treinta segundos no hace
  daño —eran válidas hace un momento— y son treinta segundos de peticiones que
  no esperan cada una a un issuer caído.
- **S3, 5 s / 30 s / 3 intentos.** El read timeout es el hueco entre bytes, no
  la transferencia completa. El breaker evita que una caída de S3 retenga un
  hilo del worker por upload durante tres timeouts.
- **SMTP, sin breaker.** El correo se manda desde la cola: un servidor de correo
  caído cuesta reintentos de un job, no latencia de una petición.

## Qué falla abierto, qué falla cerrado

| Cuando esto cae | Qué pasa | Status | `/ready` |
| --- | --- | --- | --- |
| PostgreSQL | la ruta falla | **503** `Database Unavailable` | **unavailable** (503) |
| Pool de PostgreSQL agotado | la ruta falla tras `pool_timeout` | **503** | sin cambio |
| Redis, `cache.get_or_set` | corre el loader, no se cachea nada | 200 | degradado |
| Redis, `cache.get`/`set`/`delete` | lanza (un `redis.ConnectionError`, también 503 si escapa) | 503 | degradado |
| Redis, rate limit (`fail_open = true`, default) | la petición no se limita, se registra un aviso | 200 | degradado |
| Redis, rate limit (`fail_open = false`) | la petición se rechaza | **429** con `Retry-After` | degradado |
| Redis, revocación (`revocation_fail_open = true`, default) | el token se acepta sin la verificación | 200 | degradado |
| Redis, revocación (`revocation_fail_open = false`) | toda petición autenticada se rechaza | **503** | degradado |
| Proveedor de identidad, claves en caché | los tokens verifican con las claves cacheadas | 200 | degradado |
| Proveedor de identidad, nada en caché | el token no se verifica | **401** | **unavailable** |
| Bucket de S3 | la llamada de storage falla | **503** con el breaker abierto | degradado |
| Disco local que falta o es de solo lectura | la llamada de storage falla | 500 | **unavailable** |
| SMTP, pasajero (timeout, 4xx) | el job se reintenta con backoff | — | degradado |
| SMTP, permanente (5xx, muy grande, header inválido) | el job va a dead letters de inmediato | — | degradado |

Una falla permanente de SMTP no se reintenta porque reintentar no cambia la
respuesta, y con un destinatario rechazado insistirle al servidor es como un
dominio emisor termina marcado. El dead letter guarda el mensaje para
reenviarlo cuando se arregle la causa.

## Qué reporta `/ready`

`/ready` corre la revisión de cada plugin en paralelo bajo
`[app] readiness_timeout` (2 s). Una regla decide la respuesta: una falla tumba
el readiness (**503**, `"status": "unavailable"`) solo cuando el plugin está
declarado crítico **y** el reporte dice crítico. Todo lo demás que no está sano
es **degradado** y responde **200**, así el orquestador mantiene la réplica en
rotación.

| Plugin | ¿Crítico? |
| --- | --- |
| `database` | sí |
| `auth` | sí cuando no se puede traer ninguna clave; degradado mientras sirve claves cacheadas o con el store de revocación caído |
| `storage` | un disco local: sí; un object store: degradado |
| `cache`, `ratelimit`, `mail`, `channels`, `websocket`, `http` | nunca |

Un plugin declarado no crítico ya no puede tumbar el readiness por una rama de
su revisión que olvidó `critical=False`, ni por una revisión que lanza una
excepción. Cada entrada nombra su plugin, así la dependencia está en el cuerpo:

```json
{"status": "degraded", "checks": {"cache": {"healthy": false, "status": "fail",
 "detail": "redis unreachable: calls to 'redis' are suspended ...", "critical": false,
 "meta": {"breaker": {"state": "open", "retry_after": 3.2}}}}}
```

Una revisión que no responde a tiempo es `"status": "timeout"`, distinto de
`"fail"`: uno es una dependencia que dijo que no, el otro una que no respondió.

## Los drills

`tests/test_failure_drills.py` le quita cada dependencia a un servicio corriendo
y se la devuelve. En cada una comprueba que la ruta responde el status
documentado dentro de su deadline, que `/ready` nombra la dependencia, y que la
siguiente petición después de recuperarse sale bien: la misma app y los mismos
pools, sin reinicio.

PostgreSQL y Redis se *pausan* (`docker pause`), no se detienen: el socket sigue
abierto y nada responde, que es el caso difícil. Pausar un servidor rompe
cualquier otra suite que lo use, así que estos corren solo cuando se les dice
qué contenedor es suyo:

```bash
JFAST_TEST_PG_URL=postgresql+asyncpg://jfast:jfast@localhost:5499 \
JFAST_DRILL_PG_CONTAINER=jfast-pg \
JFAST_TEST_REDIS_URL=redis://localhost:6379/0 \
JFAST_DRILL_REDIS_CONTAINER=jfast-redis \
pytest -q -s tests/test_failure_drills.py
```

El drill del proveedor de identidad no necesita contenedor —el issuer es un
transport mock de httpx que responde, se cuelga o falla a la orden— y corre en
todas las suites. Los 429/500 del proveedor de modelos los cubren las pruebas
de LLM.

### Medido

Defaults del framework, PostgreSQL 16 y Redis 7 bajo Docker Desktop en macOS,
en una laptop con mucha carga. Tus números serán menores; la forma no.

| Drill | Paso | Tiempo | Status |
| --- | --- | --- | --- |
| PostgreSQL pausado | petición con conexión del pool (ping 2 s + conectar 10 s) | 12.0 s | 503 |
| | `/ready` | 2.0 s | 503, `database` fallando |
| | petición sin conexión en el pool (conectar 10 s) | 10.0 s | 503 |
| | petición con el breaker de conexión abierto | < 0.01 s | 503 |
| | `/ready` con el breaker abierto | < 0.01 s | 503 |
| | primera petición tras despausar (después del cool-down de 5 s) | 0.04 s | 200 |
| Redis pausado | primera petición (rate limit 1 s + lectura de caché 1 s) | 2.0 s | 200 |
| | segunda petición (la tercera falla abre el breaker) | 1.0 s | 200 |
| | todas las siguientes | < 0.01 s | 200 |
| | `/ready` | < 0.01 s | 200 `degraded`, `cache` y `ratelimit` fallando |
| | rate limit con `fail_open = false` | 1.0 s | 429 |
| | primera petición tras despausar (después del cool-down de 5 s) | 0.02 s | 200 |
| Proveedor de identidad colgado | tres primeras peticiones tras expirar las claves (`jwks_timeout = 0.5`) | 1.0–1.2 s | 200 |
| | todas las siguientes (breaker abierto) | < 0.01 s | 200 |
| | `/ready` | < 0.01 s | 200 `degraded`, `auth` nombra el refresco fallido |
| | primera petición cuando vuelve el issuer (después del cool-down) | 0.01 s | 200 |

Una caída de PostgreSQL cuesta a lo más dos peticiones de diez a doce segundos
por proceso antes de que el breaker de conexión responda las demás al instante;
las conexiones del pool que quedaron malas se reemplazan al recuperarse, sin
reinicio.

## El arranque falla con una mala configuración

Un valor que solo fallaría en la primera consulta, envío o petición impide el
arranque, con un mensaje que nombra el setting y el arreglo. Entre ellos: un
`session_timezone` desconocido, `pool_size = 0` (que SQLAlchemy lee como
*ilimitado*), una plantilla de DSN por tenant sin `{tenant}`, una URL de caché
que no es Redis, una `visibility` de storage que no es ni `public` ni
`private`, un disco S3 con access key y sin secret, un puerto SMTP fuera de
rango, `use_ssl` junto con `use_starttls`, un algoritmo JWT que PyJWT no
conoce, el modo `public_key` pidiendo emitir tokens, un nombre de tabla de cola
que no es un identificador —y, en producción, un `jwks_url` en http plano, un
secreto HMAC o una clave de firma de storage de menos de 32 bytes, un remitente
SMTP que no es una dirección, y el default `guest@localhost` de RabbitMQ.
`tests/test_boot_validation.py` tiene una prueba por regla.

## Lo que esto no cubre

- **Sentencias largas en una base sana**: `command_timeout` viene apagado; las
  acotan el request timeout y el job timeout.
- **Lecturas bloqueantes de Redis y pub/sub** no tienen deadline por comando; un
  worker haciendo long-poll a un Redis pausado espera en el socket.
- **Los breakers son por proceso.** Cada worker abre el suyo con su propia
  evidencia, así que una dependencia que se recupera ve hasta una prueba por
  proceso.
- **El drill del broker** (RabbitMQ pausado) todavía no está escrito.
