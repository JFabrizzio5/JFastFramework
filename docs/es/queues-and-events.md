# Colas y eventos

Dos cosas distintas, a propósito en dos plugins distintos -- y, entre los
módulos de un mismo servicio, una tercera construida sobre la primera: los
[eventos de dominio locales](#eventos-entre-modulos), que no necesitan broker.

| | `queue` | `events` |
| --- | --- | --- |
| Un mensaje significa | "haz esto" | "esto pasó" |
| Consumidores | gana exactamente uno | cada grupo recibe una copia |
| Después de consumir | desaparece | sigue ahí, se puede reproducir |
| Manejo de fallas | reintentos, luego dead-letter | offsets, replay |
| Backends | PostgreSQL, Redis, RabbitMQ | Kafka |
| Sirve para | mandar el correo, redimensionar la imagen | avisarle a otros servicios que una orden se pagó |

Usar una cola para eventos significa agregar otra cola cada vez que un servicio
nuevo empieza a interesarse. Usar un stream para jobs significa reimplementar
reintentos y dead-lettering encima de los offsets. Elige según la fila en la
que estás.

---

## Jobs en background

```toml
[plugins]
enabled = ["observability", "database", "queue"]

[plugin.queue]
backend = "postgres"     # or "redis", "rabbitmq"
max_attempts = 3
```

Declara una task en el módulo dueño de ella, en `modules/<nombre>/tasks.py`:

```python
# modules/invoice/tasks.py
from jfastframework.tasks import TaskSession, task

@task("invoice.send_email", idempotent_on=lambda payload: payload["invoice_id"])
async def send_email(payload: dict, session: TaskSession) -> None:
    ...  # commit cuando regresa, rollback si lanza
```

Encólala con la sesión de la request, para que exista si y solo si la request
hace commit (ver [el outbox](#encolar-dentro-de-la-transacción-de-la-request-el-outbox)):

```python
outbox = request.app.state.jfast.require("outbox")
await outbox.enqueue(session, Job(task="invoice.send_email", payload={"invoice_id": 7}))
```

Y corre el worker junto a la API:

```bash
jfast worker                    # --concurrency 4, --grace 25
```

`GET /queue/stats` reporta las profundidades y los nombres de tasks
registradas, a cualquiera que pregunte: está encendido en desarrollo y cerrado
en producción, salvo que `[plugin.queue] expose_stats = true` diga otra cosa.

### Las tasks viven en su módulo

Cada `modules/<nombre>/tasks.py` (o paquete `tasks/`) se importa al construir
la app -- en la API y en `jfast worker` por igual, así que los dos ven las
mismas declaraciones `@task` y `@subscribe`. No se busca en ningún otro lado:
una task declarada en otro archivo solo corre si algo la importa antes. Un
`tasks.py` que no importa detiene el arranque, porque la alternativa es una
task que nunca corre y nadie se entera.

Prefija el nombre con el módulo (`invoice.send_email`). Es el contrato de wire,
y es como `contracts check` sabe qué módulo es dueño de la task: un módulo que
encola la task de otro por su nombre sin declarar la dependencia se reporta,
junto con el evento que debería publicar en su lugar.

`@tasks.task(...)` sobre el registro (`request.app.state.jfast.require("tasks")`)
sigue funcionando; el `@task` a nivel de módulo es el que usan los generadores
y la documentación.

### Una sesión de task: tenant, commit y rollback resueltos

Un parámetro anotado `TaskSession` recibe una sesión sobre la base primaria,
abierta con el tenant del job -- con `[plugin.database] rls = true` cada
transacción queda acotada a él, igual que la de una request. Se hace commit
cuando el handler regresa y rollback cuando lanza, y entonces el job se
reintenta. Una task que la declara en un servicio sin el plugin `database`
detiene el arranque con el arreglo en el mensaje.

### Idempotencia sin pensarlo

`idempotent_on` saca una llave del payload y la reclama en el inbox
(`jfast_inbox`) **dentro de la transacción del handler**: si el trabajo hace
commit, el reclamo también, y toda reentrega posterior se salta; si hace
rollback, el reclamo también, y el reintento corre. El plugin de cola crea la
tabla del inbox cuando alguna task la necesita.

### Correr el worker

`jfast worker` levanta la misma app que `jfast serve` -- `main:app`, los mismos
plugins y el mismo lifespan --, registra las tasks y suscriptores de cada módulo
y consume la cola. `jfast dev` lo arranca junto a la API cuando el plugin de
cola está activo (`--no-worker` para dejarlo fuera; no recarga, así que
reinicia `jfast dev` después de cambiar una task). `jfast deploy compose`,
`jfast workspace compose` y los manifiestos de Kubernetes agregan un servicio
worker junto a cada servicio con cola: la misma imagen, `jfast worker`, sin
puertos ni probes HTTP.

**Apagado ordenado.** Con SIGTERM el worker deja de reclamar, les da a los jobs
que ya corren `--grace` segundos (25 por defecto) para terminar, y luego
cancela y **libera** el resto: de vuelta a la cola de inmediato, sin gastar un
intento, para que el siguiente worker los tome ya y no al vencer el visibility
timeout -- y un deploy no acerca un job a la dead-letter queue. Una segunda
señal deja de esperar. Mantén `--grace` por debajo del plazo de kill del
orquestador: el compose generado le da al worker un `stop_grace_period` de 30 s,
y el Deployment de Kubernetes un `terminationGracePeriodSeconds` de 30.

### Dead letters

```bash
jfast jobs dead                 # los más recientes primero, con el error que mató a cada uno
jfast jobs dead --json
jfast jobs retry <id> [<id>...] # de vuelta a la cola con los intentos en cero
jfast jobs retry --all
```

El worker registra por qué falló cada intento (`ValueError: card declined`),
así que la lista dice qué arreglar antes de reintentar. Implementado para las
colas de PostgreSQL y Redis; en RabbitMQ las dead letters están en la cola
`<nombre>.dead`, y el comando indica usar la UI de administración. El conteo de
muertos está en el check de cola de `/ready`, que se marca `degraded` mientras
sea mayor que cero.

### La entrega es at-least-once. Los handlers tienen que ser idempotentes.

Un worker puede hacer el trabajo y morirse antes de confirmarlo. Entonces el
job se reentrega y el trabajo pasa dos veces. Ningún backend de aquí promete
exactly-once, porque ninguno puede.

Cobrar una tarjeta dos veces es un bug del handler, no de la cola. Ata el
efecto secundario a algo estable — el id de la factura, una idempotency key — y
verifica antes de actuar.

### Un job corre como el tenant que lo encoló

Un `Job` creado dentro de una request toma de contexto su `tenant_id`, su
`request_id` y el contexto de traza, y el worker los restaura mientras corre el
handler -- sus spans se unen a la traza de la request cuando el plugin
`telemetry` está activo. Los logs del
handler llevan la request que lo originó, y `current_tenant_id()` dentro de él
responde el mismo tenant:

```python
from jfastframework.plugins.builtin.observability import current_tenant_id

@tasks.task("recalculate_balance")
async def recalculate_balance(payload: dict) -> None:
    async with sessionmaker() as session:
        repository = AccountRepository(session, tenant_id=current_tenant_id())
        ...
```

Hasta `0.1.0a9` los campos existían pero nadie los llenaba, así que todo job
corría sin tenant y un repositorio abierto dentro de él leía las filas de
todos los tenants. Un job encolado fuera de una request -- un cron, un script --
sigue sin tenant a menos que se lo den: `Job(task=..., tenant_id="acme")`.

### Encolar dentro de la transacción de la request: el outbox

`queue.enqueue(job)` confirma en una transacción propia, aparte de las filas de
la request, así que pueden no coincidir. Activa el plugin `outbox` y encola a
través de la sesión de la request; entonces el job existe si y solo si la
request se confirmó:

```python
outbox = request.app.state.jfast.require("outbox")
await outbox.enqueue(session, Job(task="send_receipt", payload={"id": order.id}))
await outbox.publish(session, "orders", Event(type="order.placed", data={"id": order.id}))
```

Con la cola de PostgreSQL en la misma base, el job se inserta directo en
`jfast_jobs`. Lo demás -- Redis, RabbitMQ, eventos de Kafka -- pasa por
`jfast_outbox` y un relay que corre en cada proceso, toma filas con `FOR UPDATE
SKIP LOCKED`, reintenta con backoff y aparta un mensaje como muerto tras
`max_attempts`. Una fila que ningún reintento puede entregar con esta
configuración -- un evento sin bus, un job sin cola -- muere en el primer
intento, con la razón. Cada envío fallido se registra con su causa en el
mensaje, y `/ready` se marca `degraded` desde el primer intento fallido, no solo
cuando un mensaje ya murió o envejeció.

Lo que `publish` hace con el evento está [más abajo](#eventos-entre-modulos).

La mitad del consumidor es `claim_once`: registra el id del mensaje en la misma
transacción que el trabajo, y una reentrega se salta. `idempotent_on` y la
`TaskSession` de un suscriptor lo hacen por ti.

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

### Elegir un backend

| | PostgreSQL (por defecto) | Redis | RabbitMQ |
| --- | --- | --- | --- |
| Servicio extra | ninguno | Redis | RabbitMQ |
| Encolar dentro de tu transacción | **sí** | no | no |
| Latencia | intervalo de poll | microsegundos | microsegundos |
| Techo de throughput | cientos/seg | decenas de miles | muy alto |
| Routing, prioridades, UI | no | no | sí |

**Empieza con PostgreSQL.** La propiedad transaccional vale más que la latencia
para la mayoría del trabajo: haces el `INSERT` de la orden y encolas "cobrar la
tarjeta" en una sola transacción, y un rollback se lleva el job con él. Con
Redis puedes commitear la fila, crashear antes del `LPUSH`, y el job
simplemente nunca existe.

Pasa a Redis cuando la latencia del poll realmente importe, y a RabbitMQ cuando
necesites routing, prioridades o una UI de operador. Debes poder nombrar el
número con el que te topaste.

### Cómo se mantiene honesto cada backend

**PostgreSQL** reclama con `SELECT … FOR UPDATE SKIP LOCKED`, así workers
concurrentes toman filas distintas en vez de bloquearse. Un índice parcial
cubre exactamente el predicado del claim, así los jobs muertos que se acumulan
no frenan la cola.

**Redis** usa `BLMOVE` hacia una lista de procesamiento por worker. Un worker
que muere deja su job visible para recuperarlo; una cola ingenua con `BRPOP` lo
pierde. Al arrancar y al apagarse, el worker devuelve lo que haya quedado en su
propia lista de procesamiento.

**RabbitMQ** sostiene en el broker los jobs diferidos y el backoff de los
reintentos, sin plugin; ver [Retrasos en RabbitMQ](#retrasos-en-rabbitmq).
Dormir dentro del worker, en cambio, mantendría una conexión ocupada y perdería
el retraso al reiniciar.

### Retrasos en RabbitMQ

`Job(available_at=...)` y cada reintento van por el mismo camino. El diseño
obvio -- una cola de retraso, un TTL por mensaje -- está mal: RabbitMQ solo
expira el mensaje que está a la **cabeza** de la cola, así que un job diferido
diez minutos detiene a uno diferido un segundo que se publicó después. Una
tarea periódica que se vuelve a encolar con retraso deja de ser periódica.

En su lugar el backend declara una cascada. El nivel `n` es una cola cuyo TTL
es `2**n` × 100 ms *para todos sus mensajes*, así que expiran en el orden en
que llegaron y nada espera detrás de uno más largo. El retraso se escribe en
binario en la routing key y se publica en el nivel más alto; el topic exchange
de cada nivel mete el mensaje en su cola cuando su bit es 1 y lo pasa al nivel
de abajo cuando es 0, y un mensaje expirado va por dead-letter al nivel
inferior. El tiempo en la cascada es la suma de los niveles cuyo bit está
encendido.

- **Precisión:** redondeada hacia arriba a 100 ms. Un job puede empezar hasta
  100 ms tarde y nunca antes.
- **Alcance:** 25 niveles sostienen unos 38 días. Un retraso mayor pasa por la
  cascada al máximo llevando su hora debida en un header, y el worker que lo
  recibe antes de tiempo lo manda otra vuelta -- el único paso que compara
  relojes.
- **Topología:** para una cola llamada `jfast.jobs` el broker tiene
  `jfast.jobs.delay.0` a `jfast.jobs.delay.24`, cada uno un exchange y una
  cola. Se declaran en el setup; `GET /queue/stats` reporta su total como
  `delayed`.
- **Lo que no sobrevive:** el dead-lettering de un nivel al siguiente no está
  cubierto por publisher confirms, así que un broker que se cae durante el
  salto puede perder ese mensaje.

La unidad y el número de niveles son topología del broker: una cola declarada
con un TTL se niega a redeclararse con otro. Cambiar cualquiera de los dos
requiere otro nombre de cola.

### Reintentos

Backoff exponencial, con tope de cinco minutos, acotado por `max_attempts`. Un
job que los agota va a la dead-letter queue.

Los dos límites importan. Un backoff sin tope agenda el último reintento a días
de distancia, y eso parece que el job se desvaneció. Reintentos sin límite
dejan que un solo mensaje envenenado ocupe un worker para siempre.

Una **task desconocida** se manda a dead-letter de inmediato, sin reintentar:
ningún deploy futuro la vuelve entregable, y reintentar esconde el problema
real detrás de una cola que crece.

### Los nombres de tasks son un contrato de wire

Los jobs encolados por el deploy de ayer siguen en la cola cuando sale el de
hoy. Renombra una task y esos jobs quedan sin poder entregarse. Agrega el
nombre nuevo, mantén el viejo hasta que la cola se drene, y recién entonces
quítalo.

### Tareas recurrentes

Declara el horario donde declaras la task, y enciende el scheduler:

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
    day = payload["scheduled_for"]   # la hora del tick, ISO-8601 en UTC
    ...

# Una task declarada en otro lado, o una task con un segundo horario:
tasks.schedule("purge_sessions", cron="@hourly", payload={"older_than_days": 30})
tasks.schedule("report", cron="0 8 * * mon", name="report-weekly", payload={"span": "week"})
```

Cada tick se vuelve un job normal en la cola, así que los reintentos, el
dead-lettering y la entrega at-least-once son los de la cola. Un handler que no
debe correr dos veces para un mismo tick deduplica sobre `current_job().id` con
`claim_once`: el id se deriva del nombre del horario y la hora del tick, y es
el mismo en todas las réplicas.

**Córrelo en todos lados.** El scheduler está hecho para correr en cada réplica
y en cada proceso worker, sin líder. Antes de encolar un tick, cada proceso lo
reclama en un almacén que todos comparten, y solo el reclamo que entra encola:

| Almacén (`scheduler_store`) | `auto` lo elige cuando | El reclamo |
| --- | --- | --- |
| `database` | el plugin `database` está encendido | una fila en `jfast_schedule_ticks`, llave primaria `(name, fire_at)`, `INSERT … ON CONFLICT DO NOTHING` |
| `cache` | solo el plugin `cache` está encendido | `SET NX` sobre una llave por tick, que expira tras dos periodos (mínimo diez minutos) |
| `memory` | ninguno de los dos | por proceso: cada proceso dispara cada tick. Producción se niega a arrancar con él |

Con la cola de PostgreSQL en la misma base, el reclamo y el job se commitean en
una sola transacción. Con cualquier otra combinación son dos escrituras: un
encolado que falla libera el reclamo para que la siguiente pasada tome el tick,
pero un proceso que muere entre las dos pierde ese tick. El id del job es la
segunda línea de defensa: la cola de PostgreSQL inserta una sola vez un id
duplicado.

**Cuándo dispara.**

- **Intervalos:** se cuentan desde el epoch de Unix en UTC, así que
  `every=timedelta(hours=1)` dispara en punto y todas las réplicas coinciden en
  cuándo es eso sin hablar entre ellas. Para "todos los días a las 03:00 hora
  local", usa cron.
- **Cron:** acepta los cinco campos estándar -- `*`, listas, rangos, pasos,
  nombres de mes y de día -- más `@hourly`, `@daily`, `@weekly`, `@monthly`,
  `@yearly`. Día del mes y día de la semana son un OR cuando ambos están
  restringidos, como en Vixie cron. Se rechazan `?`, `L`, `W` y `#` de Quartz.
- **Zonas horarias:** cron se lee en UTC salvo que el horario nombre una zona
  de zoneinfo. En un cambio de horario, una hora de reloj que no existe dispara
  una vez, movida hacia adelante lo que salta el reloj (02:30 se vuelve 03:30);
  una hora que ocurre dos veces dispara en la primera, salvo que el campo de
  hora sea `*`, en cuyo caso el job sigue corriendo cada hora real.
- **Ticks perdidos:** después de una caída -- un reinicio, un deploy, un event
  loop bloqueado una hora -- el tick perdido más reciente dispara **una vez**.
  Los anteriores se saltan, nunca se repiten en ráfaga. `catch_up=False` salta
  también ese. Un horario sin ningún reclamo registrado es nuevo y empieza con
  su siguiente tick.
- **Los nombres son contratos**, igual que los de las tasks: los ticks se
  reclaman bajo el nombre del horario, así que renombrarlo lo vuelve nuevo, y
  un horario nuevo no se pone al día.

`/ready` reporta el scheduler dentro del check de la cola: el almacén, cada
horario y su siguiente tick, y el último error. Un scheduler detenido o que
falla deja la disponibilidad en `degraded`, no en `unavailable`: el trabajo
recurrente va atrasado, y los requests que atiende la réplica no.

---

## Eventos entre módulos

Dos módulos de un servicio reaccionan uno al otro con **eventos de dominio
locales**, sobre la cola que ya existe. Sin broker, y sin dependencia entre los
dos:

```python
# modules/receipt/services/receipt_service.py -- el que publica
from jfastframework.events import Event

await outbox.publish(session, "receipts", Event(type="receipt.registered", data={"id": r.id}))
```

```python
# modules/alert/tasks.py -- un suscriptor
from jfastframework.events import Event, subscribe
from jfastframework.tasks import TaskSession

@subscribe("receipt.registered")
async def check_budget(event: Event, session: TaskSession) -> None:
    ...
```

```toml
# contracts.toml
[modules.receipt]
publishes = ["receipt.registered"]
```

**Qué hace `publish`.** Busca los suscriptores del **tipo** del evento en este
servicio y escribe un job por suscriptor con la sesión de la request -- en la
cola de PostgreSQL, directo en `jfast_jobs` dentro de la misma transacción; en
Redis o RabbitMQ, a través del relay del outbox. Los jobs existen si y solo si
la request hace commit, y publicar dos veces el mismo evento encola a cada
suscriptor una sola vez (el id del job se deriva del id del evento y del
suscriptor). Cuando el plugin `events` (Kafka) está activo, el evento **además**
va a `topic` para otros servicios; los suscriptores locales los sigue sirviendo
la cola, así que prender Kafka no cambia nada dentro del servicio.

**Se empata por tipo, nunca por topic.** El tipo es el hecho de dominio y es lo
que declara `contracts.toml`; el topic es un detalle de particionado de Kafka
que un módulo del mismo servicio no tiene por qué conocer.

**El worker corre al suscriptor** con el `Event` reconstruido, el tenant y el
request id de la request que publicó restaurados, y su traza adjunta. El nombre
de la task es `<tipo>-><módulo>.<función>` y es parte del contrato de wire como
cualquier nombre de task: pasa `@subscribe(..., name=...)` para conservarlo si
renombras la función.

**At-least-once, deduplicado por ti.** Un suscriptor que recibe una
`TaskSession` reclama el id del evento en el inbox dentro de su propia
transacción, así que una reentrega después del commit se salta: un efecto por
evento por suscriptor. Un suscriptor sin sesión tiene que ser idempotente por
su cuenta.

**Que nadie escuche es un error.** Publicar un evento al que ningún módulo de
este servicio se suscribe, sin bus configurado, lanza `UndeliverableEvent` en la
request -- un 500 cuyo detalle dice cómo arreglarlo -- en vez de responder 201 y
dejar una fila que se reintenta hasta morir. Suscriptores sin el plugin `queue`
activo se rechazan igual.

**El contrato lo sabe.** `jfast contracts check` reporta un `@subscribe` a un
evento que ningún módulo declara en `publishes` (`orphan-subscription`), un
evento construido en un módulo que no lo declara (`undeclared-event`), y un
módulo que encola por nombre la task de otro sin `depends_on`
(`undeclared-dependency`, con este patrón como arreglo). `jfast contracts show
--json`, `CONTRACTS.md` y `jfast ai context` listan quién publica y quién
escucha. Ver [Contratos](contracts.md#eventos-y-tasks).

---

## Eventos entre servicios (Kafka)

El plugin `events` es para que *otros servicios* escuchen a este. Dentro de un
servicio, usa [eventos locales](#eventos-entre-modulos). Un handler `@on` corre
con el tenant, el request id y la traza de la request que publicó restaurados,
igual que un suscriptor.

```toml
[plugins]
enabled = ["observability", "events"]

[plugin.events]
bootstrap_servers = "localhost:9092"
consumer_group = "billing"
```

```python
from jfastframework.events import Event
from jfastframework.plugins.builtin.events import on

# Declarado al importar; el plugin lo registra antes de que arranque el consumidor.
@on("orders")
async def on_order(event: Event) -> None:
    if event.type == "order.paid":
        ...

# Publicar necesita el bus corriendo, así que ocurre dentro de una request o una task.
events = request.app.state.jfast.require("events")

await events.publish("orders", Event(
    type="order.paid",
    data={"order_id": 7, "amount": "42.00"},
    key="order-7",          # partition key: order events stay ordered
))
```

### Las partition keys no son opcionales

Kafka ordena los mensajes **dentro de una partición**, no dentro de un topic.
Publica eventos sobre una misma orden sin key y dos consumidores pueden
procesar `order.paid` antes que `order.created`. Usa el id del agregado como
key.

### Los offsets se commitean después de manejar el evento

`enable_auto_commit` está apagado. El consumidor commitea después de que el
handler retorna, así un crash a mitad del handler reentrega en vez de saltarse
— at-least-once otra vez. Un handler que lanza una excepción no commitea, así
que un mensaje envenenado bloquea su partición. Eso es visible y arreglable;
saltárselo en silencio no es ninguna de las dos cosas.

### Los eventos son en pasado e inmutables

`order.paid`, no `pay_order`. Un evento dice que algo pasó; un comando pide que
algo pase, y un comando va en una cola. Una vez publicado, un evento es
historia: corrígelo con un evento nuevo, nunca reescribiendo el viejo.

---

## Infraestructura

Los plugins habilitados aportan sus contenedores al archivo de compose
generado:

```bash
jfast deploy compose --stdout        # one service
jfast workspace compose              # the whole workspace
```

RabbitMQ cae en el offset +6, Kafka en el +2 (modo KRaft — sin ZooKeeper, un
contenedor en vez de dos). Los backends de cola `postgres` y `redis` no agregan
contenedor: reusan el que ya declara su propio plugin.

### Llegar al broker desde el host

Kafka es el único contenedor cuya dirección no es solo un mapeo de puertos. Un
cliente hace bootstrap una vez y después se reconecta a la dirección que el
broker anuncia, así que un broker que solo anuncia su hostname de compose deja
que un proceso del host conecte y después se cuelgue. Por eso el contenedor
corre dos listeners:

| Listener | Puerto del contenedor | Dirección | Quién lo usa |
| --- | --- | --- | --- |
| `INTERNAL` | 9092 | `kafka:9092` | otros servicios de compose |
| `EXTERNAL` | 9094 | `localhost:<host_port>` | `jfast dev`, herramientas locales |

`<host_port>` es por defecto `base + 2`, que es lo que compose publica y lo que
el `jfast.toml` generado ya deja en `bootstrap_servers`. Dos settings ajustan
esto cuando los defaults no alcanzan:

```toml
[plugin.events]
host_port = 19092         # the published port, if it is not base + 2
advertised_host = "localhost"
image = "bitnamilegacy/kafka:3.9"
```

`host_port` es un solo setting para dos cosas: es el puerto que compose publica
*y* el puerto que el broker anuncia. No puede mover uno sin el otro, que es
justamente el punto — `ports: - "8702:9094"` contra
`EXTERNAL://localhost:19092` es un cliente que hace bootstrap, se reconecta a
la dirección anunciada y se cuelga, que es la falla que el setting existe para
prevenir. Existe, para empezar, porque `infra()` se llama sin contexto de
aplicación, así que un servicio en un base port que no es el default tiene que
decirlo.

`image` existe porque las imágenes de broker se mueven: Bitnami reubicó su
catálogo en `bitnamilegacy/` en 2025 y el tag anterior `bitnami/kafka:3.9` dejó
de resolver.

---

## Qué está verificado y qué no

**Probado en CI:** el modelo `Job`, el backoff y su tope, el agotamiento de
intentos, el dead-lettering, el manejo de tasks desconocidas, los timeouts de
jobs, el drenado del worker al apagarse, y que un worker ocioso ceda en vez de
hacer busy-waiting — contra un backend en memoria que implementa el mismo
protocolo. También liberar lo que no termina, no reclamar mientras se apaga, y
la traza adjunta a cada job.

**Eventos locales contra un PostgreSQL real** (`tests/test_local_events.py`):
un job por suscriptor en la transacción que publica y ninguno tras un rollback,
el mismo evento publicado dos veces encolando a cada suscriptor una vez, el
tenant, el request id y la traza restaurados en el worker, el efecto de un
suscriptor con sesión ocurriendo una vez a pesar de una reentrega, una
`TaskSession` con commit al regresar y rollback al fallar, `idempotent_on`
saltando un duplicado, un evento que nadie recibe respondido con 500 sin
escribir nada, y `/ready` degradado por una fila del outbox que falla.

**El proceso worker** (`tests/test_worker_cli.py`): un `jfast worker` real
contra PostgreSQL, que recibe SIGTERM con un job corto y uno largo corriendo --
el corto termina, el largo vuelve a la cola con su intento devuelto, y el
proceso sale dentro del periodo de gracia; `jfast jobs dead` y `jfast jobs
retry` contra la misma cola. Las dead letters y la liberación también se corren
contra un Redis real (`tests/test_dead_letters.py`).

**Contra un RabbitMQ 3.13 real** (`tests/test_rabbitmq_queue.py`, que CI no
deja saltar): un job diferido espera su hora, un retraso largo no detiene a uno
corto detrás de él, los jobs diferidos llegan en el orden en que vencen, un
reintento espera su backoff en el broker, un job agotado va a dead-letter, un
retraso más largo que la cascada da otra vuelta, y un worker reintenta un
handler que falla a través de todo eso.

**Contra PostgreSQL y Redis reales** (`tests/test_scheduler.py`, que CI
tampoco deja saltar): veinte reclamos concurrentes de un tick desde dos engines
entran una vez, el reclamo y el job se commitean en una transacción, un id de
job duplicado es una fila, la poda conserva el último reclamo de cada horario,
dos servicios corriendo con el scheduler encendido encolan cada tick una vez, y
el almacén de Redis reclama una vez y nunca mueve hacia atrás su último tick.
El parser de cron, el catch-up y los casos de dos réplicas corren en todos
lados con un reloj falso.

**Sin probar:** el backend de Kafka contra un broker real; la cola de Redis
contra un servidor real más allá de dead letters y liberación; los eventos
locales sobre la cola de Redis o RabbitMQ (viajan por el relay del outbox, que
se prueba con una cola que graba). RabbitMQ no tiene soporte de `jfast jobs` y
no se ha probado en clúster, con un reinicio del broker ni con quorum queues.
