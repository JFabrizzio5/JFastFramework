# Elegir datastores

Cada datastore es un plugin. Habilita lo que el servicio necesita, y el
cliente, el health check y el contenedor llegan todos juntos.

```toml
[plugins]
enabled = ["observability", "database", "cache", "qdrant", "mongo"]
```

| Plugin | Store | Provee | Extra | Offset de puerto |
| --- | --- | --- | --- | --- |
| `database` | PostgreSQL (+pgvector) | `db.engine`, `db.sessionmaker`, `db.databases` | `[db]` | +1 (y +2, +5, +6 para más instancias) |
| `cache` | Redis | `cache`, `cache.client` | `[cache]` | +3 |
| `mongo` | MongoDB | `mongo.client`, `mongo.db` | `[mongo]` | +4 |
| `qdrant` | Qdrant | `qdrant.client` | `[qdrant]` | +7 (HTTP), +8 (gRPC) |

Correr más de uno es normal. Los datos relacionales con foreign keys van en
PostgreSQL; los historiales de chat y los payloads scrapeados están más
cómodos en Mongo. El error es adoptar un segundo store antes de que el primero
deje de alcanzar — cada uno es una cosa más que respaldar, monitorear y
restaurar a las 3am.

---

## Instancias de base de datos con nombre

El plugin `database` guardaba un solo DSN. Ese único campo es la razón de que
no hubiera réplica de lectura, ni base de datos por tenant, ni shard — no eran
cuatro features faltantes, era una estructura faltante: una base de datos que
el servicio pueda **nombrar**.

```toml
[plugin.database.connections.primary]
dsn_env = "JFAST_DB_DSN"

[plugin.database.connections.replica]
dsn_env = "JFAST_DB_REPLICA_DSN"
read_only = true
pool_size = 4
```

**Omitir `connections` es exactamente lo mismo, escrito más corto.** Sin ese
bloque hay una sola instancia llamada `default`, configurada por los campos de
arriba y leyendo `JFAST_DB_DSN`. Todos los proyectos que ya existen, todos los
templates generados y `ctx.require("db.engine")` siguen funcionando sin cambiar
nada.

| Setting | Por conexión | Valor por defecto |
| --- | --- | --- |
| `dsn` / `dsn_env` | sí | `JFAST_DB_DSN`, si no `JFAST_DB_<NAME>_DSN` |
| `read_only` | sí | `false` |
| `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`, `pool_pre_ping` | sí | el valor a nivel plugin |
| `include_infra`, `image`, `port_offset`, `database`, `user` | sí | el valor a nivel plugin |

`dsn_env` es el *nombre* de una variable, nunca un valor. Una conexión sin
`dsn` ni `dsn_env` lee `JFAST_DB_<NAME>_DSN`, así que una conexión `replica` no
necesita ninguna línea para encontrar `JFAST_DB_REPLICA_DSN`.

### Cuál es "la" base de datos

`db.engine` y `db.sessionmaker` siguen significando la instancia que se puede
escribir: la que se llama `default`, o `primary`, o la primera que no sea
`read_only`. Usa `default_connection` para decirlo explícitamente. Una
configuración donde todas las conexiones son `read_only` se rechaza al
arrancar — nada en ese servicio podría escribir, y enterarte en el primer
`POST` es enterarte tarde.

El mapa completo se publica como `db.databases`:

```python
databases = ctx.require("db.databases")
databases.names            # ("primary", "replica")
databases.engine("replica")
databases.sessionmaker()   # la instancia por defecto
```

`jfast describe --json` las lista, con la variable que lee cada una y nunca su
valor, más `max_connections` — la cantidad de conexiones al servidor que un
proceso puede abrir entre todas las instancias. Ese número es el que tiene que
entrar bajo el `max_connections` de PostgreSQL, y es el que la gente descubre
durante un incidente.

### Contenedores

Cada instancia con `include_infra` declara su propio contenedor, su propio
volumen y su propia variable de password:

```
postgres          8011:5432   POSTGRES_PASSWORD           postgres_data
postgres-replica  8012:5432   POSTGRES_REPLICA_PASSWORD   postgres_replica_data
```

Un password compartido haría que una filtración en cualquier lado fuera una
filtración en todos. Un servicio tiene diez puertos y los otros plugins ya
reclaman algunos (cache +3, mongo +4, qdrant +7 y +8, gRPC +9), así que las
bases de datos usan +1, +2, +5 y +6 — pide una quinta y el generador lo dice en
vez de chocar. Usa `include_infra = false` para una instancia administrada en
otro lado, que es el caso normal de una réplica en la nube.

Kubernetes no genera ninguna base de datos, a propósito (ver
[Kubernetes](kubernetes.md)), pero cada instancia enlazada llega al pod como su
propio `secretKeyRef`: `JFAST_DB_DSN` desde `db-dsn`, `JFAST_DB_REPLICA_DSN`
desde `db-replica-dsn`.

---

## Separación de lecturas y escrituras

```toml
[plugin.database]
read_write_split = true
```

Las lecturas van a una instancia `read_only`, las escrituras al primario:

```python
from jfastframework.plugins.builtin.database import DbSession, ReadSession

@router.get("/invoices")
async def list_invoices(session: ReadSession):
    ...

@router.post("/invoices")
async def create_invoice(session: DbSession):
    ...
```

`DbSession` y `ReadSession` son `session_dependency` y
`read_session_dependency` con `scope="function"` ya aplicado, que es lo que
hace que el commit ocurra antes de enviar la respuesta -- ver
[Transacciones](transactions.md). Sin réplica configurada, `ReadSession` va a
la misma base que `DbSession`. Úsala en todos lados desde el principio y la separación
llega después como un bloque de configuración.

### La parte que no es opcional: el pin

**Una réplica va atrasada.** Guardas una fila, rediriges, lees de la réplica, y
la fila todavía no está. Es un 404 intermitente que aparece bajo carga y nunca
se reproduce en una laptop, porque una laptop no tiene réplica. Ningún flag lo
arregla: una separación sin pin es un bug que ya publicaste, no una feature.

Por eso, después de una escritura, las lecturas de ese cliente van al
**primario** durante `pin_window` segundos (5 por defecto).

**Cómo viaja el pin.** Como un token que lleva el cliente — una cookie
`jfast_rw` y un header `X-JFast-Read-Pin` — no como una entrada en una tabla de
este proceso. Un redirect puede caer en cualquier réplica del servicio, y un
pin que el siguiente proceso no puede ver es un pin que silenciosamente no
está. Los navegadores llevan la cookie gratis; un cliente de API que no guarda
cookies devuelve el header.

**Por qué confiar en un valor que controla el cliente es seguro acá.** La única
dirección en la que el cliente puede empujar es *hacia el primario*, que nunca
está desactualizado. El costo de un valor falsificado es capacidad del
primario, así que el valor se recorta a `pin_window` desde ahora: nadie puede
quedarse pineado para siempre.

**Qué dispara el pin.** Un método no seguro (`POST`, `PUT`, `PATCH`, `DELETE`)
que respondió por debajo de 400. Esto se tiene que decidir *antes* de que el
handler retorne: una sesión hace commit durante el teardown de la dependencia,
que corre cuando los headers de la respuesta ya salieron, así que el commit no
puede ser lo que setea la cookie. Para la escritura rara detrás de un `GET` —
un upsert perezoso, un contador — dilo:

```python
from jfastframework.plugins.builtin.database import mark_write

@router.get("/reports/{id}")
async def report(request: Request, session: DbSession):
    await touch_last_seen(session, id)
    mark_write(request)
```

La aproximación cuesta que un `POST` que no escribió nada pinee una ventana de
lecturas. Eso es carga, no incorrección, y `pin_on_unsafe_methods = false` lo
apaga para un servicio que marca sus escrituras a mano.

**Por qué cinco segundos, y qué sería mejor.** Un standby sano en la misma red
está a milisegundos; cinco segundos igual cubren un pico de checkpoint o un WAL
sender trabado, y pinear a un cliente cinco segundos después de escribir es
despreciable en una carga dominada por lecturas. La respuesta exacta es basada
en LSN: registrar `pg_current_wal_lsn()` en la escritura, compararlo con el
`pg_last_wal_replay_lsn()` de la réplica, y dejar de pinear en cuanto la
réplica alcanzó. Eso cuesta un round trip por lectura y solo funciona contra un
standby físico real, así que es el camino de mejora y no el default.

**Las escrituras no pueden llegar a una réplica.** Dos guardas, porque una de
ellas no puede ver SQL crudo. Una sesión de lectura se niega a terminar con
cambios ORM pendientes (`ReadOnlySessionError`), y toda conexión asyncpg
`read_only` setea `default_transaction_read_only = on`, así que un `INSERT`
colado por `session.execute(text(...))` lo rechaza PostgreSQL mismo.

### Sharding no es esto

Las instancias con nombre son la base *sobre* la que se construirá un mapa de
shards — resolución de claves, migraciones por shard y queries entre shards son
un trabajo aparte, y nada de eso era expresable mientras el plugin tenía un
solo DSN. No está construido.

Una base de datos por tenant sí: ver [Multi-tenancy](multitenancy.md).

---

## Paginación

`BaseRepository` pagina de tres formas, y la elección es sobre lo que cuesta la
respuesta, no sobre estilo.

| Llamada | Costo | Qué te da |
| --- | --- | --- |
| `paginate()` | un `COUNT` + un `LIMIT`/`OFFSET` | `total` exacto, acceso aleatorio |
| `paginate(with_total=False)` | un `LIMIT limit + 1` | `has_more`, acceso aleatorio, sin `COUNT` |
| `paginate_keyset(after=...)` | un range scan | `has_more` + `next_cursor`, costo plano a cualquier profundidad |

`OFFSET n` obliga a la base a recorrer y descartar n filas antes de devolver
nada, así que la página 200 cuesta 200 páginas de trabajo. Una página keyset es
un range scan desde un punto conocido y cuesta lo mismo donde sea que caiga. El
canje es el acceso aleatorio: hay una página siguiente, no una página 40.

```python
class MessageRepository(BaseRepository[Message]):
    model = Message
    order_by = ("-edited_at",)


page = await repository.paginate_keyset(limit=50)
while page.has_more:
    page = await repository.paginate_keyset(limit=50, after=page.next_cursor)
```

### Columnas de orden que aceptan NULL

`edited_at`, `last_message_at`, `archived_at` — las columnas por las que ordena
un feed suelen ser justo las que están en NULL hasta que algo pasa. Eso está
soportado, y conviene saber qué hace el framework al respecto, porque la
versión ingenua de la paginación keyset **pierde filas sin decirlo**.

Chocan dos hechos. NULL compara UNKNOWN contra todo, incluso contra sí mismo,
así que un cursor que lleva un NULL no matchea ninguna fila: la página
siguiente vuelve vacía, `has_more` es `False`, y el recorrido informa que la
tabla se terminó. Y los backends no coinciden en dónde va el bloque de NULL —
PostgreSQL los ordena al final ascendente y al principio descendente, SQLite
los pone al principio en ambas direcciones — así que el mismo código pierde un
conjunto distinto de filas en cada uno. En una tabla de 200 filas con 40
claves de orden en NULL, eso eran 180 filas inalcanzables en SQLite y 40 en
PostgreSQL, sin error en ninguno de los dos casos.

Por eso todo orden que arma el repositorio fija el bloque de NULL al final:

```sql
ORDER BY messages.edited_at DESC NULLS LAST, messages.id DESC
```

y toda comparación de keyset está escrita contra esa fijación — un empate sobre
un NULL es `IS NULL`, y un paso más allá de un valor real también admite el
bloque de NULL que viene detrás. Un cursor cuyo primer valor es `None` es un
cursor legítimo que apunta a ese bloque, así que **la codificación por la que
pases un cursor para meterlo en una URL tiene que sobrevivir a un `None`**;
JSON lo hace, un `",".join(...)` ingenuo no.

Dos consecuencias que vale la pena decir:

- **`NULLS LAST` es de PostgreSQL, SQLite ≥ 3.30 y Oracle.** MySQL, MariaDB y
  SQL Server rechazan la sintaxis directamente. JFastFramework apunta a
  PostgreSQL y se prueba contra SQLite, así que ambos están cubiertos; un
  tercer backend no es un cambio de configuración acá.
- **Solo las columnas nullable reciben la cláusula.** Una columna de orden
  `NOT NULL` conserva el `ORDER BY c DESC` simple, porque `DESC NULLS LAST` no
  lo puede resolver un índice btree descendente común — PostgreSQL lo crea por
  defecto como `NULLS FIRST` — y compraría un sort a cambio de una garantía que
  la columna ya da.

La alternativa que se consideró era rechazar la consulta: lanzar cuando una
columna de orden acepta NULL. Es un cambio más chico y convierte la pérdida de
datos en un error ruidoso, pero rechaza "las ediciones más nuevas primero", que
no es un error que nadie esté cometiendo. Devolver un subconjunto en silencio
nunca fue una opción.

### Qué cuesta la corrección acá

`paginate_keyset` tiene costo plano — el mismo trabajo en la página 2 y en la
2.000 — **mientras las columnas de orden sean `NOT NULL`**. Una columna nullable
renuncia a eso. El predicado gana un disyunto `OR c IS NULL`, y PostgreSQL no
puede convertir una disyunción en un rango de índice: degrada el range scan a un
index scan con filtro y vuelve a leer desde el principio del índice.

Medido sobre 200k filas, con un índice en `(edited_at, id)`, una página de 50
filas a profundidad 100k:

| Columna de orden | Plan | Buffers |
| --- | --- | --- |
| `NOT NULL` | `Index Cond: ROW(edited_at, id) > ROW(...)` | 4 |
| nullable, cursor dentro del bloque de NULL | `Index Cond: edited_at IS NULL AND id > ...` | 5 |
| nullable, cursor sobre un valor real | `Filter: ... OR edited_at IS NULL`, 80k filas descartadas | 840 |
| la misma página por `OFFSET` | index scan, 100k filas descartadas | 1049 |

Así que sigue siendo la más barata de las tres y ya no es plana. Si eso importa
más que la comodidad, haz la columna `NOT NULL` con un centinela
(`edited_at DEFAULT created_at`) y el range scan vuelve. Partir el scan en el
rango sin NULL más el bloque terminal de NULL lo recuperaría sin el centinela;
eso no está construido.

### Los empates no son el mismo problema

`paginate_keyset` agrega la clave primaria al orden, así que el orden es total
y ninguna fila puede quedar a caballo entre dos páginas. `paginate` no lo hace:
ordena solo por `order_by`, así que las filas que empatan en la clave de orden
vuelven en la secuencia que haya elegido el planner, y el bloque de NULL es un
empate grande. La pertenencia sigue siendo correcta — cada fila está en
exactamente una página — pero si necesitas que la secuencia dentro de un grupo
empatado sea estable entre requests, pon tú una columna única en `order_by`.

---

## Cache

`get_or_set` es el camino de lectura. Todo lo demás en la fachada es una
primitiva a la que recurres cuando read-through no es la forma correcta.

```python
cache = ctx.require("cache")

report = await cache.get_or_set(
    f"report:{tenant_id}",
    lambda: build_report(tenant_id),   # cualquier corrutina sin argumentos
    ttl=300,
)
```

Hace tres cosas que `get` + `set` a mano no hacen.

**Sobrevive a que el cache esté caído.** Cualquier falla del backend dentro de
`get_or_set` degrada a llamar al loader. Un reinicio de Redis le cuesta a esas
requests una recomputación, no un 500. Esto es lo que vuelve cierto el
`health_critical=False` del plugin en vez de aspiracional — y es cierto *solo
en este camino*:

| Llamada | Redis inalcanzable |
| --- | --- |
| `get_or_set(...)` | devuelve el valor del loader |
| `get` / `set` / `delete` / `exists` / `publish` | lanza excepción |

Las primitivas lanzan a propósito. Un servicio que no distingue "no hay nada
cacheado" de "Redis desapareció" sirve respuestas viejas para siempre y nadie
se entera. Si las llamas directo en un camino de request, el `try/except` es
tuyo.

Los errores que lanza el *loader* siempre se propagan. Degradar ante una caída
del cache es el objetivo; degradar ante una query rota es cómo un servicio
devuelve respuestas incorrectas en silencio.

**Colapsa los misses concurrentes.** Cuando una key caliente expira bajo
carga, el read-through ingenuo manda todas las requests en vuelo a la base de
datos al mismo tiempo. Acá el primer caller que falla toma un lock corto en
Redis y recomputa; los demás consultan su resultado hasta `stampede_wait` y
después se rinden y cargan por su cuenta.

El trade, dicho sin vueltas: el lock cuesta un round trip extra en cada miss, y
un caller que pierde la carrera espera hasta `stampede_wait` antes de recurrir
al loader. Ese límite es lo que lo hace seguro — un loader trabado cuesta
trabajo duplicado, nunca una fila de requests trabadas.

La alternativa, recomputar temprano antes de que expire el TTL, se ahorra el
round trip pero necesita envolver cada valor en un sobre que cargue su expiry
lógico. Eso cambia lo que se guarda, y este Redis normalmente se comparte con
algo que no es un servicio JFast. Mantener los valores cacheados como
documentos JSON planos valía más que el round trip.

```toml
[plugin.cache]
stampede_wait = 2.0      # 0 apaga el lock por completo
stampede_lock_ttl = 10   # techo de cuánto puede retenerlo un caller
```

**Está contado.** Con el plugin `metrics` habilitado, el cache registra cuatro
counters en el registry compartido y `/metrics` los sirve junto con todo lo
demás:

| Counter | Significado |
| --- | --- |
| `cache_hits_total` | lecturas servidas desde el cache |
| `cache_misses_total` | lecturas que no encontraron nada guardado |
| `cache_errors_total` | operaciones que el backend rechazó |
| `cache_stampede_suppressed_total` | ejecuciones del loader evitadas esperando a otro caller |

`metrics` es una dependencia blanda (`after`, no `requires`): un servicio que
quiere un cache no debería tener que cargar `prometheus-client` para tenerlo.
Deshabilita `metrics` y los counters pasan a ser no-ops en silencio.

### TTL

`ttl=None` significa "usa `default_ttl`", así que no puede significar además
"nunca expira". `ttl=0` es esa vía de escape:

```python
await cache.set("feature-flags", flags, ttl=0)   # hasta que algo lo borre
```

Un `ttl` negativo lanza `ValueError` en el punto de llamada y no en el round
trip, para que el mensaje pueda nombrar la key.

### Dos cosas que parecen bugs y no lo son

**`publish` no aplica el prefijo de keys.** Todos los demás métodos
namespacean su key; los nombres de canal son un contrato con quien más esté en
este Redis — muchas veces una app Laravel que jamás oyó hablar de nuestro
prefijo. Renombrar el canal en silencio rompería justamente la interoperación
para la que existe el Redis compartido. Namespacear canales es tarea del
plugin `channels`, bajo su propio setting `prefix`.

**`get` devuelve el string crudo cuando el valor no es JSON.** Misma razón:
este servicio no es el único que escribe. Una key que escribió otro sistema
vale la pena devolverla como string, no vale la pena lanzar una excepción.

---

## Búsqueda vectorial: pgvector o Qdrant

El plugin `rag` habla contra un protocolo `VectorStore`, así que el backend es
una línea de config.

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"       # the default
collection = "rag_chunks"
dimensions = 768
```

Cambiar a Qdrant:

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
```

No cambia nada más. Los mismos endpoints, la misma forma de `SearchHit`, los
mismos scores — cada store normaliza a similitud coseno en [0, 1], así que
quien llama nunca tiene que saber si el backend devolvió una distancia o una
similitud.

### Cuál

| | pgvector | Qdrant |
| --- | --- | --- |
| Costo operativo | ninguno — es la base de datos que ya corres | un segundo servicio que correr y respaldar |
| Escala | cómodo hasta unos pocos millones de chunks | mucho más allá de eso |
| Filtrado | tenant id, y el SQL que escribas | filtros ricos sobre el payload, indexados |
| Cuantización | no | sí |

**Empieza con pgvector.** Pasa a Qdrant cuando choques con un muro específico
que puedas nombrar — complejidad de los filtros, tiempo de build del índice,
memoria. "Quizá escale mejor" no es ese muro.

### Elegir mal falla ruidosamente

Elegir `pgvector` sin el plugin `database`, o `qdrant` sin el plugin `qdrant`,
revienta al arrancar y con el arreglo en el mensaje:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

No en la primera búsqueda, en producción, un viernes.

---

## Un store propio

Implementa el protocolo — `ensure_schema`, `upsert`, `search`,
`delete_document`, `health` — y apunta la config ahí. La clase se construye
con el `AppContext`, así que puede sacar del registro de providers lo que
necesite:

```python
from jfastframework.vectors import Chunk, SearchHit


class WeaviateStore:
    def __init__(self, ctx):
        self._client = ctx.require("weaviate.client")

    async def ensure_schema(self) -> None: ...
    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int: ...
    async def search(self, embedding, *, limit=5, tenant_id=None) -> list[SearchHit]: ...
    async def delete_document(self, document_id: str) -> None: ...
    async def health(self) -> tuple[bool, str]: ...
```

```toml
[plugin.rag]
store = "myapp.stores:WeaviateStore"
```

La misma escotilla de escape existe para los embedders
(`embedder = "myapp:OpenAIEmbedder"` — cualquier cosa con `dimensions` y un
`embed` async).

---

## Dimensiones del embedding

El error que cuesta una tarde: `dimensions` tiene que coincidir con el modelo
de embedding. `nomic-embed-text` es 768; `mxbai-embed-large` es 1024. Si no
coinciden falla al insertar, y cambiarlo después significa volver a embeber
cada documento que ya ingestaste. Decídelo antes del primer ingest.

---

## Enums: qué mitad de la garantía estás comprando

`Enum` parece una decisión y son dos. Las dos respuestas importan, y los
defaults no te dan el par que la mayoría asume.

```python
class Status(enum.Enum):
    pending = "pending"
    done = "done"


status: Mapped[Status] = mapped_column(Enum(Status, native_enum=False))
```

Esa columna es un `VARCHAR` común. **No se crea ningún `CHECK`.**
`create_constraint` viene en `False` desde SQLAlchemy 1.4, y `native_enum=False`
solo apaga el tipo `ENUM` de PostgreSQL — no pone nada en su lugar. Nada fuera
de la aplicación frena un valor inválido:

```sql
-- with native_enum=False and nothing else
CREATE TABLE t (s VARCHAR(7))
```

Las tres opciones, y lo que cuesta cada una:

| | Almacenamiento | Se puede escribir un valor inválido | Agregar un miembro |
|---|---|---|---|
| `Enum(Status)` (default) | tipo nativo `status_enum` | no — lo rechaza el servidor | `ALTER TYPE ... ADD VALUE`, una migración |
| `Enum(Status, native_enum=False)` | `VARCHAR(n)` | **sí** — desde cualquier cliente que no sea el ORM | nada; deployas el código |
| `Enum(Status, native_enum=False, create_constraint=True)` | `VARCHAR(n)` + `CHECK` | no | una migración que reescribe el `CHECK` |

```sql
-- with create_constraint=True
CREATE TABLE t (s VARCHAR(7), CONSTRAINT status_enum CHECK (s IN ('pending', 'done')))
```

**Usa `native_enum=False` sin constraint** para un conjunto que todavía se
mueve — estados, tipos, cualquier cosa que cambie por una decisión de producto.
Agregar un miembro es un deploy de código y nada más, que es toda la razón para
resignar el tipo nativo. A cambio, acepta que una sesión de psql, un `COPY`
masivo u otro servicio sobre la misma base pueden escribir `"pendign"` y la
columna lo va a tomar. Valida en el borde, donde llega el valor, y trata los
valores desconocidos como un caso real al leer — un `ValueError` saliendo de
`Status(row.status)` es un 500 que parece un bug en el lugar equivocado.

**Agrega `create_constraint=True`** cuando algo que no es este servicio escribe
la tabla, o cuando un valor incorrecto es un problema de corrección y no de
presentación. Pagas una migración por miembro, igual que con el tipo nativo —
pero un `CHECK` es más barato de cambiar que un `ENUM` de PostgreSQL, que ni
siquiera puede eliminar un valor.

Ten en cuenta que autogenerate no detecta de forma confiable los cambios de
miembros de un enum, en ninguna de las dos direcciones. Elijas la que elijas,
esa migración se escribe a mano.

---

## Deploy

Los plugins de datastore habilitados aportan sus contenedores al archivo de
compose generado, automáticamente:

```bash
jfast deploy compose --stdout
```

Deshabilita `cache` y el contenedor de Redis desaparece de la siguiente
generación. Ese es el punto de derivar la infraestructura del grafo de plugins
en vez de mantenerla en paralelo.

Los secretos se quedan en el entorno. El compose generado los referencia con
la forma fail-fast de compose, así una contraseña faltante detiene el stack en
vez de arrancar PostgreSQL abierto de par en par:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```
