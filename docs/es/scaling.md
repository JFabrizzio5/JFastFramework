# Escalar

Cada afirmación de capacidad en esta página es un número de una corrida, con
la máquina, los tamaños y el método al lado. Donde un número sería una
suposición, la página lo dice en vez de darlo.

**Dónde se midió:** laptop Apple M5 (10 núcleos, 16 GB), macOS, Python
3.12.14, FastAPI 0.142.1. PostgreSQL 16.15 con pgvector 0.8.6 y Redis 7 en
Docker Desktop (una VM de 10 CPUs y 8 GB, compartida con unos 25 contenedores
más). En la misma máquina corrían otras suites de pruebas (carga promedio de
4 a 12), y por eso los números del propio framework son tiempo de CPU y no
tiempo de reloj. Tu hardware dará otros números absolutos; lo que se traslada
son las proporciones y las formas.

## Lo que cuesta una petición, y el presupuesto que lo mantiene

`scripts/bench_overhead.py` llama directamente al callable ASGI de cada app
-- sin socket, sin servidor -- y mide el **tiempo de CPU del proceso** por
petición del mismo `GET /ping` en cuatro apps:

| Escenario | us de CPU / petición | x FastAPI |
| --- | --- | --- |
| FastAPI solo | 20.6 | 1.00 |
| FastAPI + JWT y tenant a mano (dependencia `async def`) | 70.5 | 3.26 |
| JFast, plugins por defecto (observability, metrics), logs en `WARNING` | 53.2 | 2.48 |
| JFast + auth + tenancy + métricas, logs en `WARNING` | 114.3 | 5.43 |

(9 rondas de 5,000 peticiones; los escenarios se turnan en tramos de 200
peticiones; los microsegundos son la mediana de las medias por ronda, la
proporción es la mediana de las proporciones por tramo.)

Por qué en proceso y por qué tiempo de CPU: `ab` contra uvicorn mide también
sockets y parseo HTTP, y en un runner compartido esa parte se mueve decenas de
por ciento entre dos corridas del mismo commit. Un presupuesto que falla por
ruido acaba apagado. El tiempo de CPU ignora los minutos en que otro proceso
tuvo el núcleo, y aun así cuenta un salto al threadpool -- una de las dos
regresiones para las que existe el presupuesto. Por qué tramos: Apple silicon
mueve un proceso entre núcleos rápidos y lentos; una proporción dentro de 200
peticiones compara lo mismo con lo mismo. Tres corridas del chequeo con la
máquina cargada dieron proporciones a menos de 1.5 % entre sí.

**El presupuesto.** `tests/test_performance_budget.py` hace la misma medición
y falla si una proporción presupuestada (`jfast_defaults`,
`jfast_auth_tenancy_metrics`) crece más de 20 % sobre
`tests/performance_baseline.json`. También demuestra que muerde: volver a
poner un `BaseHTTPMiddleware` -- la regresión de 0.1.0a10 -- lo hace fallar.
Está apagado por defecto, porque mide:

```bash
JFAST_PERF_BUDGET=1 pytest tests/test_performance_budget.py
```

La línea base guarda una entrada por plataforma (`darwin-arm64`,
`linux-x86_64`...) porque las proporciones cambian entre familias de CPU; una
plataforma sin entrada se salta y dice cómo registrar una. Para aceptar un
cambio que cuesta más a propósito, vuelve a registrarla y commitea el archivo
con la razón en el mensaje:

```bash
python scripts/bench_overhead.py --write-baseline tests/performance_baseline.json
```

En CI la comparación robusta es contra la rama base en el mismo runner: corre
el script en el commit base con `--json > base.json` y luego la prueba con
`JFAST_PERF_BASELINE=base.json`. Un archivo con un resultado suelto se acepta
como línea base justo para esto.

**Las rutas del framework van al final.** Starlette prueba las rutas en orden,
y `/health`, `/ready`, `/info`, `/metrics` y la documentación se registraban
primero, así que cada petición a una ruta de la aplicación fallaba contra cada
una antes de llegar a la suya. Ahora pasan detrás de las rutas de la
aplicación al final del arranque. Medido con el mismo método, dos copias de la
app por defecto que solo difieren en el orden: **de 2 a 4 us de CPU ahorrados
por petición a la aplicación** (de unos 43). No cambia quién responde: cada
ruta movida se prueba con su propio path, y cuando una ruta de la aplicación
la reclamaría -- un comodín `/{slug}`, o un `PUT /{slug}` que convertiría
`POST /health` en su propio 405 -- la ruta del framework vuelve delante de
esa ruta. Las rutas con parámetros nunca se mueven, porque ninguna prueba
puede demostrar que moverlas es inocuo. `tests/test_route_order.py` sostiene
todo esto.

## Prueba de carga de un servicio en marcha: `jfast bench`

```bash
jfast bench http://localhost:8000
jfast bench http://localhost:8000 -r "GET /invoices" -r "GET /invoices/42" \
    -c 1,8,32,128 -d 15 --token "$TOKEN"
jfast bench http://localhost:8000 --k6 load.js --json --fail-on-break
```

Lee el `/openapi.json` del servicio y carga cada `GET` cuyos parámetros puede
llenar con un ejemplo, un default o un enum; `--route` elige rutas en su
lugar (una plantilla del esquema o un path literal). Las escrituras entran con
`--method POST` solo cuando el esquema trae un cuerpo de ejemplo -- los
cuerpos inventados miden los 422. Cada escalón mantiene N clientes concurrentes
durante `--duration` segundos y reporta:

```text
clients     req/s   p50 ms   p95 ms   p99 ms  errors  non-2xx  ready
      1     1,720      0.5      1.1      1.8    0.0%        0  ok
      8     2,160      3.0      7.2     13.9    0.0%        0  ok
     32     2,806     10.3     18.2     31.2    0.0%        0  ok
     64     3,035     18.9     33.1     50.9    0.0%        0  ok

Did not break up to 64 clients (p99 <= 500 ms, errors <= 1.0%).
Throughput stops growing past 32 clients: size replicas from there.
The load generator used a whole core in some steps: ...
```

- **Dónde se rompe:** el primer escalón cuyo p99 pasa de `--max-p99-ms` (500)
  o cuya tasa de errores pasa de `--max-error-rate` (1 %). Errores son 5xx,
  429 y fallas de transporte; un 401 o un 404 es culpa del escenario y se
  cuenta como non-2xx.
- **Dónde se satura:** el escalón después del cual más clientes compraron
  menos de 10 % más throughput. Ese es el número para dimensionar réplicas.
- **Qué dependencia cedió:** después de cada escalón se lee `/ready`, y cada
  chequeo que no esté `ok` se imprime junto al escalón.
- `--k6 load.js` escribe el mismo escenario como script de k6 (`ramping-vus`,
  una etapa por escalón, los umbrales como thresholds de k6); el token se lee
  de la variable `TOKEN` de k6, nunca se escribe en el archivo.

**Su límite, medido:** un proceso de Python genera de 3,000 a 3,600 peticiones
por segundo (la corrida de arriba, contra un `/ping` de FastAPI solo, donde
`ab` hizo 21,000 contra el mismo servidor). El reporte lo dice cuando el CPU
del propio generador es el cuello de botella; para más carga, exporta a k6.
Cada cliente simulado tiene su propio cliente httpx: un pool compartido generó
430 req/s con 32 clientes contra los 1,900 de un solo cliente, y eso es
contención en el pool, no el servicio.

## Varias réplicas

`tests/test_multi_replica.py` corre dos réplicas -- cada una con su engine,
su conexión a Redis, su relay, scheduler, cliente LLM o app, sin compartir
nada en memoria -- contra un PostgreSQL y un Redis:

| Qué | Demostrado | Cómo |
| --- | --- | --- |
| Relay del outbox | 400 mensajes escritos por las dos réplicas, dos relays y dos workers: cada uno reenviado una vez (los conteos de los relays suman 400, nada queda pendiente) y consumido una vez. Los dos relays y los dos workers participaron. | `FOR UPDATE SKIP LOCKED` sobre las filas del outbox |
| Scheduler | 39 ticks de un horario por minuto, las dos réplicas pasando en los mismos instantes: exactamente un encolado por tick, con el tick store de SQL y con el de Redis (la cola de Redis no deduplica ids de job, así que un doble encolado se vería como un job 40) | un reclamo por tick en un store compartido |
| Presupuesto LLM | 60 llamadas concurrentes de $0.10 entre las dos réplicas contra un tope de $1.00: exactamente 10 enviadas, 50 rechazadas, el ledger termina en $1.00. Con tope por tenant de $0.30: exactamente 3 por tenant. | `RedisLedger`: reserva atómica, reversa si se rechaza, liquida al costo real |
| Revocación de tokens | Un logout en la réplica A se rechaza en la réplica B, token de acceso y de refresh. El mismo refresh token enviado a las dos a la vez rota una sola vez; la perdedora recibe 401 y el par nuevo de la ganadora funciona en ambas. | `RedisTokenStore` y su script de rotación |

Lo que **no** se promete, y por eso no se prueba como si lo fuera: la entrega
sigue siendo al menos una vez (un relay puede publicar y morir antes de
marcar la fila), así que los consumidores deduplican con `claim_once`; un
scheduler que muere entre reclamar un tick y encolarlo pierde ese tick cuando
el store de reclamos y la cola son sistemas distintos; el token store y el
ledger en memoria son por proceso a propósito, y los health checks lo dicen.

**Archivos:** un disco local es por réplica. Antes de una segunda réplica,
mueve a S3 los discos que importan -- ve la siguiente sección.

## Archivos en S3, verificados contra MinIO

`tests/test_storage_minio.py` corre el disco S3 contra una API S3 real. Se
salta sin estas variables:

```bash
docker run -d -p 9010:9000 -e MINIO_ROOT_USER=jfastminio \
    -e MINIO_ROOT_PASSWORD=jfastminio-secret minio/minio server /data
JFAST_TEST_S3_URL=http://localhost:9010 JFAST_TEST_S3_ACCESS_KEY=jfastminio \
JFAST_TEST_S3_SECRET_KEY=jfastminio-secret pytest tests/test_storage_minio.py
```

Cubre: put, get, stat (content type, ETag, metadatos), exists, delete (y su
"¿estaba ahí?"), listado por prefijo y límite, `temporary_url` descargada con
httpx y luego rechazada al expirar, la URL sin firma rechazada, `upload_url`
aceptando un PUT directo, `put_stream` como multipart real de tres partes (su
ETag termina en `-3`), un stream corto como un solo PUT, un stream que falla a
la mitad y uno cancelado, ambos abortados sin dejar partes, y el health check.
Encontró un bug, corregido: S3 responde a lo más 1,000 llaves por petición, y
`listing(limit=1500)` devolvía 1,000 sin avisar. Ahora sigue el continuation
token. Nunca apuntes estas pruebas a S3 real.

## RAG a escala

`scripts/bench_rag.py` usa `PgVectorStore`, el store del plugin `rag`, con
embeddings sintéticos (unos pocos centroides de tema por tenant, más ruido;
vectores uniformes al azar serían el peor caso de HNSW y ningún corpus se ve
así). 384 dimensiones, 20 chunks por documento, 8 documentos escritos a la
vez, `hnsw.ef_search = 100` salvo que se diga otra cosa. No se corrió 1M de
chunks: es la meta del plan, pero 300,000 ya tomaron 6.5 minutos y 1.5 GB
aquí.

**300,000 chunks, 1,000 tenants (300 cada uno):**

| | |
| --- | --- |
| Ingesta, índice HNSW presente | 1,424 chunks/s |
| Ingesta, sin índice HNSW | 3,036 chunks/s |
| Construcción del HNSW, 300,000 chunks | 257.6 s (serial, `maintenance_work_mem` 1 GB) |
| Tabla + TOAST / índices / total | 866 MB / 683 MB (HNSW 586, GIN de texto 62) / 1,548 MB |

| Búsqueda, 10 resultados | p50 | p95 | p99 | consultas/s con 16 a la vez |
| --- | --- | --- | --- | --- |
| Vector (filtro de tenant siempre activo) | 2.5 ms | 5.1 ms | 6.9 ms | 1,037 |
| Vector + filtro de metadatos | 6.5 ms | 9.0 ms | 16.5 ms | 365 |
| Híbrida (vector + texto completo, fusionadas) | 3.0 ms | 4.4 ms | 6.4 ms | 1,349 |

Recall@10 contra un escaneo exacto: **1.0** -- porque con 300 chunks por
tenant el planner nunca usa el índice HNSW. Lee las filas del tenant por el
btree `(tenant_id, document_id)` y las ordena exacto. El índice HNSW de 586 MB
cuesta memoria y parte a la mitad la tasa de ingesta sin servir estas
consultas.

**100,000 chunks, 4 tenants (25,000 cada uno)** -- el planner recorre el grafo
HNSW y filtra por tenant (el iterative scan de pgvector):

| `ef_search` | Vector p50 / p99 | + metadatos p50 / p99 | Híbrida p50 / p99 | Recall@10 |
| --- | --- | --- | --- | --- |
| 100 (default) | 3.1 / 11.0 ms | 20.4 / 66.1 ms | 4.1 / 13.1 ms | 0.918 |
| 200 | 3.3 / 7.1 ms | 19.0 / 47.9 ms | 5.6 / 12.4 ms | 0.950 |
| 400 | 3.7 / 6.4 ms | 30.4 / 84.5 ms | 9.6 / 67.8 ms | 0.966 |

Ingesta 2,180 chunks/s con el índice, 3,599 sin él; construcción 36.9 s; 522
MB en total. Lo que significa para un despliegue:

- **La búsqueda híbrida cuesta poco** junto a la vectorial aquí: de 0.4 a 1 ms
  en p50.
- **Un filtro de tenant sobre un índice HNSW compartido pierde recall** en
  cuanto los tenants son lo bastante grandes para que el planner lo use: falta
  8 % del top 10 verdadero con el `ef_search` por defecto. Sube
  `hnsw_ef_search` para tenants grandes, o dales su propio índice parcial o
  partición.
- **Construye el índice después de una carga masiva**, no antes: la ingesta es
  de 1.6 a 2.1 veces más rápida sin él. Una construcción en paralelo guarda el
  grafo en memoria compartida, que en un contenedor es `/dev/shm`; el compose
  generado pone `shm_size: 1gb`, y un `maintenance_work_mem` por encima hace
  fallar la construcción con "could not resize shared memory segment".
- **Corregido en 0.1.0a11:** las escrituras del store buscaban las filas de un
  documento con `tenant_id IS NOT DISTINCT FROM`, que ningún btree sirve. Con
  300,000 chunks el DELETE de un documento tomaba 19.2 ms en vez de 0.03 ms, y
  crecía con la tabla. Ahora comparan `tenant_id = :tenant`, y
  `tests/test_rag_scale.py` le pregunta al planner si el índice las sirve.

## Agregados que siguen rápidos con datos

Un tablero que suma los gastos de un tenant por categoría del mes, de los
últimos doce meses y del año corre esos agregados en cada petición.
`jfastframework.db.rollups.MonthlyRollup` los guarda en una tabla propia, una
fila por tenant, mes local y grupo:

```python
from sqlalchemy import Integer, Numeric, String, func
from jfastframework.db.rollups import MonthlyRollup, rollup_table
from jfastframework.time import today

expenses_monthly = rollup_table(
    "expenses_monthly", Base.metadata,
    group_by={"category": String(64)},
    measures={"total": Numeric(14, 2), "count": Integer},
)
rollup = MonthlyRollup(
    source=Expense.__table__, target=expenses_monthly,
    at=Expense.paid_at, tenant=Expense.tenant_id,
    group_by=[Expense.category],
    measures={"total": func.sum(Expense.amount), "count": func.count()},
    where=Expense.status != "cancelled",
    zone="America/Mexico_City",
)

# En el handler del evento que publica la escritura -- o después de la
# escritura, en su propia transacción:
await rollup.refresh_at(session, tenant_id=event.tenant_id, moment=paid_at)

# Cada noche, desde una tarea programada, para sanar lo que se saltó eventos:
await rollup.rebuild(session, tenant_id=tenant, since=date(2024, 1, 1), until=today())
```

**Recalcula el bucket desde sus filas** en vez de sumar un delta: los eventos
y los jobs llegan al menos una vez, y `total = total + monto` duplica la
segunda vez que uno se entrega, mientras que un recálculo queda bien las veces
que corra. Dos refrescos del mismo bucket se serializan con un advisory lock
(sin él, diez refrescos concurrentes fallaron con violación de unicidad --
probado). Los meses son meses locales en la zona que le des, semiabiertos en
UTC.

Medido con `scripts/bench_aggregates.py`: 3,000,000 filas de gastos en 36
meses y 1,000 tenants, uno de ellos con 990,000 filas; los seis agregados de
un tablero de gastos, con un índice `(tenant_id, paid_at)` en la tabla origen;
30 repeticiones.

| Tablero de seis agregados | p50 | p95 |
| --- | --- | --- |
| Tenant grande (990,000 filas), al vuelo | 455 ms | 803 ms |
| Tenant grande, desde el rollup | 3.3 ms | 6.0 ms |
| Tenant típico (2,012 filas), al vuelo | 4.1 ms | 5.7 ms |
| Tenant típico, desde el rollup | 2.8 ms | 4.2 ms |

Lo que cuesta mantenerlo: refrescar un bucket, lo que paga una escritura, 8.1
ms (p95 11.8) para el mes del tenant grande, de unas 27,000 filas, y 1.5 ms
para el típico; reconstruir 37 meses, 1.1 s y 0.06 s. La tabla del rollup mide
0.2 MB junto a los 367 MB de la tabla origen.

O sea: con unos miles de filas por tenant el índice basta y un rollup ahorra
un milisegundo. Es para el tenant que creció, y para las vistas de
administración que agregan entre tenants -- las que van bien en la demo y no
con el cliente que más paga.

## Reproducirlo todo

```bash
python scripts/bench_overhead.py                     # el costo por petición
python scripts/bench_overhead.py --route-order       # lo que ahorra el orden de rutas
JFAST_PERF_BUDGET=1 pytest tests/test_performance_budget.py
pytest tests/test_multi_replica.py                   # necesita JFAST_TEST_PG_URL, JFAST_TEST_REDIS_URL
pytest tests/test_storage_minio.py                   # necesita JFAST_TEST_S3_*
python scripts/bench_rag.py --pg "$JFAST_TEST_PG_URL" --chunks 300000 --tenants 1000
python scripts/bench_aggregates.py --pg "$JFAST_TEST_PG_URL" --rows 3000000
```
