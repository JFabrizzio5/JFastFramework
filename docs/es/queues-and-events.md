# Colas y eventos

Dos cosas distintas, a propósito en dos plugins distintos.

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

Registra una task y encólala desde una ruta:

```python
tasks = request.app.state.jfast.require("tasks")

@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None:
    ...

queue = request.app.state.jfast.require("queue")
await queue.enqueue(Job(task="send_invoice_email", payload={"invoice_id": 7}))
```

Corre un worker:

```python
from jfastframework.queues import Worker

worker = Worker(queue, tasks, concurrency=4)
await worker.run()
```

`GET /queue/stats` reporta las profundidades y los nombres de tasks
registradas.

### La entrega es at-least-once. Los handlers tienen que ser idempotentes.

Un worker puede hacer el trabajo y morirse antes de confirmarlo. Entonces el
job se reentrega y el trabajo pasa dos veces. Ningún backend de aquí promete
exactly-once, porque ninguno puede.

Cobrar una tarjeta dos veces es un bug del handler, no de la cola. Ata el
efecto secundario a algo estable — el id de la factura, una idempotency key — y
verifica antes de actuar.

### Un job corre como el tenant que lo encoló

Un `Job` creado dentro de una request toma de contexto su `tenant_id` y su
`request_id`, y el worker los restaura mientras corre el handler. Los logs del
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
`max_attempts`. `/ready` se marca degradado cuando un mensaje se atora.

La mitad del consumidor es `claim_once`: registra el id del mensaje en la misma
transacción que el trabajo, y una reentrega se salta.

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

---

## Eventos

```toml
[plugins]
enabled = ["observability", "events"]

[plugin.events]
bootstrap_servers = "localhost:9092"
consumer_group = "billing"
```

```python
events = request.app.state.jfast.require("events")

await events.publish("orders", Event(
    type="order.paid",
    data={"order_id": 7, "amount": "42.00"},
    key="order-7",          # partition key: order events stay ordered
))

@events.on("orders")
async def on_order(event: Event) -> None:
    if event.type == "order.paid":
        ...
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
protocolo.

**Contra un RabbitMQ 3.13 real** (`tests/test_rabbitmq_queue.py`, que CI no
deja saltar): un job diferido espera su hora, un retraso largo no detiene a uno
corto detrás de él, los jobs diferidos llegan en el orden en que vencen, un
reintento espera su backoff en el broker, un job agotado va a dead-letter, un
retraso más largo que la cascada da otra vuelta, y un worker reintenta un
handler que falla a través de todo eso.

**Sin probar:** los backends de Redis y Kafka contra servidores reales. La cola
de PostgreSQL solo toca un servidor real en la suite del outbox, que encola a
través de la sesión de un request y reclama lo que escribió. RabbitMQ no se ha probado en clúster, con un
reinicio del broker ni con quorum queues.
