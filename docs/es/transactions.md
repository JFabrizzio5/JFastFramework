# Transacciones

Cuándo se confirman las escrituras de una request, en qué se convierte una
escritura que falla, y cómo se evita que dos requests que compiten por la misma
fila ganen las dos.

```python
from jfastframework.plugins.builtin.database import DbSession

@router.post("/invoices", status_code=201)
async def create_invoice(payload: InvoiceCreate, session: DbSession) -> InvoiceRead:
    invoice = await InvoiceRepository(session).create(**payload.model_dump())
    return InvoiceRead.model_validate(invoice, from_attributes=True)
```

Una request, una transacción. El repositorio hace flush; la sesión hace commit
cuando el endpoint regresa; una excepción en cualquier punto deshace toda la
request.

---

## El commit ocurre antes de la respuesta

`DbSession`, `ReadSession` y `TenantSession` son las dependencias de sesión con
`scope="function"` ya aplicado. El scope decide *cuándo* corre el commit, y el
valor por defecto es la respuesta equivocada.

FastAPI corre el código que sigue al `yield` de una dependencia **después de
enviar la respuesta**, salvo que la dependencia tenga scope de función. La
sesión hace commit en ese código. Hasta `0.1.0a8` eso significaba:

- **Un commit que fallaba ya se había contestado `201`.** Una constraint
  diferida, un fallo de serialización, una conexión que se cae en el momento
  equivocado: al cliente se le dijo que la fila existe, y no existe.
- **Un cliente podía leer su propia escritura antes de que existiera.** La
  respuesta salía, el cliente pedía la fila que acababa de crear, y el commit
  todavía no corría. Es la falla de lectura-después-de-escritura que el pin de
  réplica existe para evitar, reproducida sin ninguna réplica.

Con `scope="function"` el commit corre cuando el endpoint regresa, antes de
armar la respuesta: un commit fallido es el `500` que debe ser, y un `201`
significa que la fila está ahí.

**El plugin de base de datos se niega a arrancar** mientras alguna ruta llegue a
una dependencia de sesión con otro scope, y nombra cada ruta. Cualquiera de
estas dos formas sirve:

```python
async def create(session: DbSession): ...
async def create(session = Depends(session_dependency, scope="function")): ...
```

Una dependencia generadora propia que envuelva una sesión también tiene que
tener scope de función: FastAPI rechaza una dependencia con scope de request que
dependa de una con scope de función.

---

## En qué se convierte una escritura que falla

`BaseRepository` hace flush después de cada escritura, y el flush es donde la
base dice que no. Las excepciones del driver se vuelven el error que el cliente
debe ver:

| Qué pasó | Error | Status |
| --- | --- | --- |
| Se violó una constraint única o de exclusión | `ConflictError` | 409 |
| Una FK apunta a nada, o un borrado deja huérfano a un hijo | `ConflictError` | 409 |
| Otra request cambió la fila desde que esta la leyó | `ConflictError` | 409 |
| `update(expected_version=n)` y la fila ya pasó de `n` | `PreconditionFailedError` | 412 |

Después de cualquiera de ellos la transacción terminó -- PostgreSQL rechaza
toda sentencia hasta el rollback -- y la sesión de la request hace el rollback.

### Revisar-y-luego-insertar es una carrera; la constraint es la regla

```python
if await repository.by_name(payload.name) is not None:
    raise ConflictError(...)
return await repository.create(**payload.model_dump())
```

Dos requests pueden pasar la revisión. Los modelos generados llevan
`UniqueConstraint("tenant_id", "name", postgresql_nulls_not_distinct=True)`,
así que solo una pasa el insert y la otra recibe el mismo `409`. La revisión se
queda por su mensaje. `NULLS NOT DISTINCT` es lo que hace valer la constraint en
filas sin tenant; requiere PostgreSQL 15 o posterior.

---

## Actualizaciones perdidas: `VersionedMixin`

Dos personas abren la misma factura. Las dos la editan. Sin versión, quien
guarda segundo borra en silencio lo del primero. Con versión:

```python
from jfastframework.db import Base, TimestampMixin, VersionedMixin

class Invoice(Base, VersionedMixin, TimestampMixin):   # Versioned primero
    ...
```

Una sola columna da dos protecciones:

- **Entre requests.** El cliente lee `version: 3`, la manda de vuelta con la
  edición, y el servicio la pasa:
  `repository.update(invoice, expected_version=payload.version, **changes)`.
  Si alguien guardó en medio, la fila está en 4 y la respuesta es `412` con
  `current_version` en el cuerpo. El cliente vuelve a leer y decide.
- **Dentro de la carrera.** Dos requests que leen la versión 3 en el mismo
  instante pasan las dos esa revisión. El `UPDATE` de SQLAlchemy lleva
  `WHERE version = 3`; la segunda no encuentra fila y se vuelve un `409`.

`VersionedMixin` va **antes** de `TimestampMixin` en las bases. Los dos definen
`__mapper_args__` y gana el primero; este lleva también la configuración del de
timestamps. El otro orden perdería el versionado sin decir nada, así que lanza
`TypeError` al importar.

Los módulos `layered` generados ya lo traen: el modelo está versionado, `Read`
devuelve `version` y `Update` la acepta. Si un `PATCH` no la manda, se conserva
el comportamiento anterior: gana la última escritura.

---

## Valores que no caben en un solo `UPDATE`

Un saldo, un inventario, el siguiente folio: leer, calcular, escribir. Dos
requests que leen el mismo valor escriben cada una su resultado, y uno de los
dos cambios se pierde. La columna de versión lo convierte en un `409`; cuando lo
correcto es esperar, se bloquea.

**La fila**, cuando la hay:

```python
account = await repository.get_for_update(account_id)   # SELECT ... FOR UPDATE
account.balance += amount
```

**Una llave**, cuando lo que se protege no es una fila -- "una factura abierta
por cliente por mes":

```python
from jfastframework.db import advisory_lock

await advisory_lock(session, f"open-invoice:{customer_id}:{month}")
```

`pg_advisory_xact_lock` se libera en el commit o el rollback, así que no hay
unlock que olvidar ni bloqueo que sobreviva a una request caída. En SQLite, que
ya permite un solo escritor a la vez, no hace nada.

---

## Reintentar una transacción que la base abandonó

`40001` (fallo de serialización) y `40P01` (deadlock) significan que la base
deshizo la transacción para proteger a otra. El mismo trabajo, corrido otra vez,
normalmente funciona. `run_in_transaction` hace exactamente eso y nada más:

```python
from jfastframework.db import run_in_transaction

async def settle(session):
    ...

await run_in_transaction(sessionmaker, settle, attempts=3)
```

- Cada intento es una sesión y una transacción nuevas, y el trabajo corre desde
  su primera línea. Un reintento nunca retoma media unidad de trabajo.
- Solo se reintentan esos dos códigos. Una violación de constraint o un bug
  fallan al primer intento.
- Backoff con jitter completo, para que dos transacciones que se bloquearon
  entre sí no vuelvan a chocar con el mismo ritmo.

Para jobs y scripts. Una request ya tiene su transacción, y reintentar dentro de
ella repetiría solo la parte posterior al punto de reintento. Deja fuera de la
función reintentada los efectos externos a la base -- un correo, una llamada
HTTP --: ocurren una vez por intento.

---

## Un mensaje que se confirma con las filas: el outbox

Guardar un pedido y encolar su recibo son dos escrituras. Como dos
transacciones, cualquiera puede ocurrir sola: el job se encola y el pedido hace
rollback, o el pedido se confirma y el proceso muere antes de que exista el
job. El plugin `outbox` las vuelve una:

```python
from jfastframework.queues import Job

@router.post("/orders", status_code=201)
async def create(payload: OrderIn, request: Request, session: DbSession):
    order = await OrderRepository(session).create(**payload.model_dump())
    outbox = request.app.state.jfast.require("outbox")
    await outbox.enqueue(session, Job(task="send_receipt", payload={"id": order.id}))
    return order
```

`outbox.enqueue` y `outbox.publish` escriben a través de la sesión de la
request, así que el mensaje existe si y solo si existe el pedido. Con la cola de
PostgreSQL en la misma base, el job entra directo a ella; si no, un relay en
cada proceso mueve los mensajes confirmados a la cola o al bus de eventos, con
`FOR UPDATE SKIP LOCKED` para que dos relays nunca envíen el mismo mensaje. Ver
[Colas y eventos](queues-and-events.md) para la mitad del consumidor.

## Un POST reintentado: idempotency keys

Un cliente cuya conexión se cae después de enviar `POST /payments` no sabe si
llegó, así que reintenta -- y sin ayuda el servidor cobra dos veces. Con el
plugin `idempotency`, una ruta que pide la llave la registra en la misma
transacción que el pago:

```python
from jfastframework.idempotency import IdempotencyKey

@router.post("/payments", status_code=201)
async def pay(payload: PaymentIn, session: DbSession, key: IdempotencyKey): ...
```

| El cliente manda `Idempotency-Key: k` y | Recibe |
| --- | --- |
| `k` es nueva | La request corre; su respuesta se registra |
| la misma request otra vez | La respuesta registrada, con `Idempotent-Replayed: true` |
| otra request distinta con `k` | 422: una llave nombra una sola operación |
| la misma request mientras la primera sigue corriendo | 409 |

Si la primera request falla, la llave hace rollback con ella y el reintento
corre desde cero. Las llaves son por tenant y vencen tras `ttl_hours` (24).
`RequiredIdempotencyKey` rechaza una request que no la trae.

---

## Lo que esto no es

**No es un circuit breaker.** Un breaker deja de llamar a algo que está caído.
No hace nada por un trabajo que falló a la mitad; eso lo hacen la frontera de la
transacción, el outbox y la idempotency key. El cliente entre servicios con
timeouts, reintentos y breaker está en `PLAN-NEXT.md`.
