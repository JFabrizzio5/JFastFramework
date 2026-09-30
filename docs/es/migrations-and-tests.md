# Migraciones y tests

Las dos vienen cableadas en cada servicio generado. Ninguna es algo que tengas
que configurar.

---

## Migraciones (Alembic)

`jfast new service` escribe `alembic.ini`, `migrations/env.py`,
`migrations/script.py.mako` y `migrations/versions/` siempre que el plugin
`database` esté habilitado.

```bash
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
alembic downgrade -1
alembic history
```

Dos cosas que hace y que un `alembic init` de fábrica no hace:

**El DSN sale de la configuración de la aplicación.** `env.py` lee
`JFAST_DB_DSN` a través del mismo `DatabaseSettings` que usa el servicio, y
`alembic.ini` deliberadamente no tiene `sqlalchemy.url`. Una migración que
*puede* correr contra una base de datos distinta a la del servicio en algún
momento lo va a hacer, en el peor momento posible.

**Los modelos se importan automáticamente.** Autogenerate solo ve las tablas
cuyas clases fueron importadas. Un import olvidado produce una migración vacía,
y la tabla que falta se descubre en producción. `env.py` recorre `modules/` e
importa dos nombres por módulo, `models` y `storage`, así que no hay lista de
imports que mantener. Eso cubre los cuatro layouts, aunque no a los cuatro de la
misma manera:

| Layout | Qué registra la tabla |
| --- | --- |
| `layered` | `modules/<m>/models.py` |
| `modular` | `modules/<m>/models/`, cuyo `__init__.py` re-exporta la entidad |
| `screaming` | `modules/<m>/storage.py` |
| `hexagonal` | ninguno de los dos nombres existe. Importar `modules.<m>.models` igual corre primero `modules/<m>/__init__.py`, que importa `.adapters.http` y llega a `infrastructure/orm.py` |

El nombre ausente es esperado y se traga; un `ModuleNotFoundError` que nombre
otra cosa se vuelve a lanzar, porque un error de import dentro de un módulo no
puede convertirse en una migración vacía. Un servicio con un módulo de cada
layout deja las cuatro tablas en `Base.metadata` — pero hexagonal llega ahí por
el `__init__` del paquete, no por un nombre de archivo que `env.py` busque.

También habilita `compare_type` y `compare_server_default` — sin ellos
autogenerate se pierde en silencio los cambios de tipo de columna y los cambios
de default, las dos ediciones que la gente más suele dar por detectadas.

### Los nombres de constraints están fijados

`jfastframework.db.Base` define un `naming_convention`. Sin él PostgreSQL
inventa los nombres de las constraints y autogenerate produce diffs distintos
en máquinas distintas. Con él, una primary key siempre es `pk_<table>`, y una
foreign key siempre `fk_<table>_<column>_<referred>`.

Adoptarlo en una base de datos que ya tiene constraints con nombres automáticos
requiere una migración única. Hazla antes de que la flota crezca.

### Los timestamps llevan zona (breaking, requiere una migración única)

`TimestampMixin` mapeaba `created_at` / `updated_at` a
`TIMESTAMP WITHOUT TIME ZONE`. Los valores volvían como `2026-08-29T20:55:15`
— sin `Z`, sin offset — y cualquier cliente JavaScript los leía como hora
*local*, así que una fila escrita ahora se renderizaba con horas de diferencia
para quien no estuviera en UTC. El mixin ahora usa
`jfastframework.db.UTCDateTime`, que es `TIMESTAMPTZ` en PostgreSQL y adjunta
UTC a la salida en el resto de backends.

Cada tabla construida sobre el mixin hay que convertirla una vez.
`autogenerate` detecta el cambio de tipo (`compare_type` está activo) y escribe
un `ALTER COLUMN ... TYPE timestamptz` pelado, sin `USING`. Eso no falla:
convierte con el cast implícito, que lee cada valor guardado en el `TimeZone`
del *servidor*. En un servidor que no esté en UTC eso desplaza la tabla entera
y nadie se queja. Escríbelo a mano:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

`AT TIME ZONE 'UTC'` es la parte que sostiene todo: declara que los valores
guardados siempre fueron UTC. Lo eran, porque `now()` escrito en una columna
`timestamp` guardaba el instante UTC con la zona quitada.

La forma con `USING` reescribe la tabla y sostiene un lock `ACCESS EXCLUSIVE`
mientras lo hace. Prográmalo como cualquier otra reescritura sobre una tabla
grande.

Las escrituras quedan más estrictas: un `datetime` naive lanza error en vez de
guardarse bajo una zona asumida. Usa `datetime.now(UTC)`.

### Lee la migración antes de aplicarla

Autogenerate es un borrador, no un plan:

- Un **rename** se renderiza como un drop más un add. En una tabla con filas,
  eso es pérdida de datos silenciosa. La revisión generada lo avisa, pero solo
  cuando de verdad contiene un drop y un add sobre la misma tabla — una
  advertencia en todas las revisiones es una que nadie lee. Usa
  `op.alter_column(..., new_column_name=...)` o copia los valores entre las dos
  operaciones; `migration check` lee las dos formas.
- Las **migraciones de datos** no se escriben en absoluto.
- Los renombres de índices y los cambios de miembros de un enum se pierden con
  frecuencia. Mira
  [Enums](datastores.md#enums-que-mitad-de-la-garantia-estas-comprando) para
  saber qué impone y qué no impone la columna en cada caso.
- Una **columna `NOT NULL` nueva** se repara sola cuando el modelo trae un
  `default=` escalar: la revisión agrega la columna con un `server_default`
  equivalente, rellena las filas y vuelve a quitar el default en la misma
  migración. Alembic solo mira `server_default`, así que sin esto emitía DDL
  que PostgreSQL rechaza de plano en cualquier tabla con filas. Un `default=`
  que no se puede traducir a SQL — un callable como `uuid4`, o directamente
  ningún default — se avisa en la revisión, porque no hay con qué rellenar.

### Léela con `jfast migration check`

```bash
jfast migration check              # cada revisión sin aplicar
jfast migration check --all        # también las aplicadas
jfast migration check --json       # para un agente, o para CI
jfast migration plan               # la próxima revisión riesgosa, y cómo reescribirla
jfast migration plan -r 7cd507730ac7   # esa, por el id que imprimió alembic
```

`check` parsea `migrations/versions/*.py` con `ast` y **nunca las importa**. Una
revisión importa los modelos del proyecto, y el entorno donde corre la CLI no
suele ser el entorno donde esos imports resuelven — un checker que solo funciona
cuando el proyecto ya importa no está disponible justo cuando hace falta.

#### Qué decide que una revisión esté "sin aplicar"

El head sale de `alembic_version` en la base; la cadena sale del `revision` y el
`down_revision` de cada archivo. Toda revisión que se alcance caminando
`down_revision` hacia atrás desde el head está aplicada, y el resto es lo que
reporta `check`. Valen las dos escrituras — `revision = "..."` y la anotada
`revision: str = "..."` que escribe `script.py.mako` — y ambas se comparan
contra el id que estampó **alembic**, no contra el nombre del archivo. Sin base
de datos no hay head, así que `check` reporta todas las revisiones y avisa
`scope: all`.

| Hallazgo | Severidad | Qué significa |
| --- | --- | --- |
| `migration-add-not-null` | critical | `add_column` con `nullable=False` y sin `server_default`. PostgreSQL lo rechaza de plano apenas la tabla tiene una fila |
| `migration-rename` | critical | Un `drop_column` sobre una tabla a la que la misma revisión le agrega columnas, sin nada en medio que copie los valores. Autogenerate renderiza un rename exactamente así, y los datos se van con el drop |
| `migration-timestamptz-no-using` | critical | `ALTER COLUMN ... TYPE timestamptz` sin `USING`. No falla; corre la columna en silencio. Mira arriba |
| `migration-drop-table` | critical | Se pierden todas las filas y `downgrade` recrea la tabla vacía, en el mejor caso. `TRUNCATE` cuenta |
| `migration-drop-column` | high | La columna y su contenido desaparecen; `downgrade` devuelve una columna vacía |
| `migration-type-change` | high, o medium con `postgresql_using` | Reescribe la tabla bajo `ACCESS EXCLUSIVE`: sin lecturas ni escrituras hasta que termina |
| `migration-set-not-null` | high | `alter_column(nullable=False)` escanea la tabla entera para validar, sosteniendo el lock |
| `migration-drop-constraint` | medium | La garantía deja de aplicarse de inmediato; volver a agregarla exige un escaneo de validación |
| `migration-index-lock` | medium | `create_index` sin `postgresql_concurrently=True` bloquea toda escritura mientras dura |
| `migration-no-downgrade` | low | No es un defecto. Pero `alembic downgrade -1` va a reportar éxito sin cambiar nada |
| `migration-raw-sql` | low | Una sentencia de `op.execute` que ningún check de acá lee. No es un veredicto — es la ausencia de uno |

Las severidades, la forma de `Finding` y `--fail-on` son las mismas que usa
`jfast analyze`. `--fail-on` vale `high` por defecto; un riesgo en ese nivel o
peor sale con **4** (`Code.MIGRATION`).

Cada hallazgo apunta a la línea de la operación de la que habla, y cada remedio
que `plan` imprime como Python es Python que puedes pegar: la suite corre
`ast.parse` sobre todos, y el remedio del rename se aplica contra un PostgreSQL
real y después se lee la fila.

#### Un rename que se lleva sus datos no se reporta

`migration-rename` se calla cuando entre el `add_column` y el `drop_column` hay
una sentencia que copia la columna vieja en la nueva:

```python
op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))
op.execute("UPDATE posts SET media_url = image_url")
op.drop_column("posts", "image_url")
```

La regla es angosta a propósito: un string SQL literal — `op.execute("...")` o
`op.execute(sa.text("..."))` — que nombre las dos columnas, asigne sobre la
nueva (`UPDATE ... SET <new> = ...`, o `INSERT INTO ... (<new>) ... SELECT ...`)
y esté **entre** las dos líneas. Una copia después del drop no cuenta; no puede,
para entonces la columna ya no existe. Un backfill armado en tiempo de ejecución,
o corrido desde otro script, acá no se ve y el hallazgo se mantiene — lo cual es
honesto, porque una revisión que no copia los valores es una revisión que los
pierde. No hay comentario de waiver: escribir el backfill *es* la forma de
callarlo.

`migration-drop-column` se sigue reportando en `high`. Tirar una columna que una
copia del servicio todavía en aire puede estar leyendo vale la pena frenarla,
sea rename o no.

#### Para qué se lee `op.execute`

El SQL arbitrario no se puede juzgar sin un parser de SQL. Se lee una lista
nombrada de formas — `DROP TABLE`, `TRUNCATE`, `ALTER TABLE ... DROP COLUMN`,
`... SET NOT NULL`, `CREATE INDEX` (sin `CONCURRENTLY`) y
`ALTER COLUMN ... TYPE` — y cada una produce el mismo hallazgo que produciría la
llamada `op.*` equivalente. Cualquier otra cosa, incluida una sentencia armada en
tiempo de ejecución, se reporta como `migration-raw-sql` en `low`: **no
revisada**, en vez de aprobada. Un `✓` sobre una sentencia que nadie parseó
afirma una revisión que no ocurrió.

También ausente a propósito: un `create_index` o un `alter_column` sobre una
tabla que la misma revisión crea — esa tabla está vacía por construcción, y
reportarla es el falso positivo que hace que se silencie el comando entero.
Ensanchar un `VARCHAR` tampoco se reporta: PostgreSQL toma un `varchar` más largo
como una edición de catálogo, no como una reescritura. Ese caso necesita
`existing_type=` en la llamada para ser reconocido — el largo viejo es lo único
que dice para qué lado va el cambio, y autogenerate siempre lo escribe.
`op.alter_column("widgets", "name", type_=sa.String(200))` por sí solo se
reporta en `high`, y angostar (`String(200)` → `String(50)`) siempre. Y un
cambio de tipo que
trae `postgresql_using` se reporta en `medium` y no en `high`: la reescritura y
su lock `ACCESS EXCLUSIVE` siguen igual y siguen valiendo la pena, pero quien la
escribió dejó la conversión escrita, así que no bloquea el `--fail-on high` por
defecto.

#### Cómo se relaciona con el hook de `env.py`

Son dos mitades del mismo problema, en momentos distintos. `migrations/env.py`
instala un hook `process_revision_directives` que repara una columna `NOT NULL`
con default escalar **mientras se genera la revisión** — el único caso en que la
corrección se deduce del modelo. `migration check` lee revisiones que **ya
existen**: escritas a mano, traídas de una rama, o generadas antes de que ese
hook existiera. Una revisión autogenerada por un servicio actual no debería
disparar nunca `migration-add-not-null`. Si lo hace, se escribió a mano o la
generó una versión vieja, y el hallazgo es correcto.

#### Los conteos de filas necesitan una base de datos

La línea `Reason` de `plan` da un conteo real cuando resuelve un DSN — `--dsn`,
después `JFAST_DB_DSN`, después `.env` en la raíz del proyecto:

```
Migration:  0004_add_status
File:       migrations/versions/0004_add_status.py
Risk:       CRITICAL
Reason:     status is NOT NULL and widgets has 812 rows
Database:   connected

  migration-add-not-null: widgets.status is NOT NULL with no server_default
  PostgreSQL rejects `ALTER TABLE ... ADD COLUMN ... NOT NULL` with no default
  the moment the table has a single row, so this passes on an empty database
  and stops the deploy on the one that matters. Add the column nullable,
  backfill it, then set NOT NULL.

Recommended:
  1. add the column nullable
  2. backfill it
  3. add the NOT NULL constraint
  4. `op.alter_column('widgets', 'status', nullable=False)` in a follow-up revision, once the backfill has committed
```

`Database:` es uno de `connected`, `unavailable` o `skipped` — el último es
`--no-db`. `Reason:` cierra la frase con el conteo real cuando la base
respondió: `has 812 rows`, `has 1 row`, `is empty on this database`, o
`does not exist on this database yet`.

Cuando no resuelve ninguno dice `has an unknown row count, treat as populated`.
Nunca reporta una tabla como vacía sin evidencia: un checker que asume el caso
seguro es un checker que se queda callado en producción. `--no-db` se saltea la
conexión por completo, que es lo que debería usar CI.

### SQL offline

```bash
alembic upgrade head --sql > migration.sql
```

Corre `env.py` sin conectarse, que es además la forma en que CI verifica el
cableado sin una base de datos.

---

## Tests (pytest)

`jfast new service` escribe `pytest.ini` y `conftest.py`. Los módulos generados
traen sus propios tests, que pasan de inmediato:

```bash
pytest                          # modules/ and tests/
pytest modules/invoice/tests    # one module
```

### Fixtures

`conftest.py` provee `app` y `client`, construidos con una lista **explícita**
de plugins:

```python
@pytest.fixture
def app():
    return build_test_app(plugins=["observability"], app_name="billing")
```

Aquí lo explícito le gana a lo implícito: un test que nombra sus plugins no se
puede romper porque alguien cambió un default en `jfast.toml`.

De `jfastframework.testing`:

| Helper | Qué hace |
| --- | --- |
| `build_test_app(...)` | App con una lista nombrada de plugins, sin archivo de config, sin env |
| `client_for(app)` | Cliente HTTP async con el lifespan realmente ejecutado |
| `NullPlugin` | Registra `register` / `startup` / `shutdown`, para tests de orden |
| `make_config(...)` | Un `JFastConfig` sin tocar el disco |

`client_for` importa más de lo que parece: los plugins que abren recursos en
`startup` necesitan que el lifespan corra, y un `TestClient` pelado se lo salta
en contextos async.

### Qué prueban realmente los tests generados

Prueban los campos declarados, no un marcador de posición: al crear se guarda
cada campo, cada llave `--unique` se rechaza si está ocupada y otra vez en una
edición que se mueve a ella (y no cuando una fila conserva la suya), un id que
no existe es un 404, una edición cambia solo lo enviado, un `null` explícito
para una columna NOT NULL es un 422, y un texto más largo que su columna se
rechaza antes del INSERT.

**Layouts layered y modular** — tests a nivel de servicio contra un
repositorio en memoria que guarda objetos reales de la entidad, así que el
verificador de tipos ve cada atributo que lee un test. Sin base de datos, sin
contenedores.

**Layout screaming** — dos archivos, separados a propósito:

- `test_<module>_domain.py` — la entidad y sus reglas. Sin base de datos, sin
  fakes, sin event loop. Si un test de aquí alguna vez necesita un fixture, una
  regla se escapó del dominio.
- `test_<module>_use_cases.py` — un repositorio falso y un event loop, nada
  más.

Los dos se marcan explícitamente con `pytest.mark.asyncio` en vez de depender
de `asyncio_mode = auto`, así pasan en un proyecto que no configuró
pytest-asyncio.

### Los controles del proyecto generado

Un servicio generado trae la configuración con la que corren sus controles,
así que `ruff check .` significa lo mismo en cualquier máquina y en cualquier
versión de ruff:

| Archivo | Qué fija |
| --- | --- |
| `ruff.toml` | el conjunto de reglas (`E F W I UP B SIM RUF ASYNC`), largo de línea 100, `Depends`/`Query`/... de FastAPI como llamadas inmutables para que B008 no marque el idioma propio del framework, y una sección de imports `framework` para `jfastframework` entre terceros y tu código |
| `mypy.ini` | `strict = True`; `migrations/versions/` excluido -- las revisiones se revisan como migraciones, no se tipan como código |
| `pytest.ini` | `asyncio_mode = auto`, `modules` y `tests` como rutas de test |

Recién generado, sin tocar nada, pasan los cuatro:

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy . && pytest
```

`scripts/smoke_generated_quality.sh` lo mantiene cierto en cada cambio del
framework: genera `jfast start` con un cliente y multitenant y un servicio con
todos los plugins que no necesitan un servidor propio, agrega un módulo por
layout con campos de ejemplo, con `--fields ... --unique ...` y con `--bare`,
más uno con todos los tipos de la gramática, y corre los cuatro controles en
cada proyecto -- además de `jfast check --ci`, y en el multitenant `jfast check
--multitenant-ready`, que no debe encontrar nada en código que escribió el
generador. `JFAST_SMOKE_GENERATED=1 pytest tests/test_smoke_generated_quality.py`
lo corre desde la suite; tarda como un minuto con la caché de mypy caliente.

### Tests de integración

Los tests que necesitan un PostgreSQL o un Redis real no se generan. Márcalos
con `@pytest.mark.integration` y mantenlos fuera de la suite rápida; un harness
de integración a nivel de framework es la fase 6 de PLAN.md.

---

## Qué debería correr CI

```bash
pytest
ruff check src tests
mypy src
jfast doctor
```

Además, para cualquier cosa que toque templates:

```bash
bash scripts/smoke.sh              # renders both layouts, runs their tests, checks alembic
bash scripts/smoke_workspace.sh    # workspace, gateway, frontends, patching
bash scripts/smoke_generated_quality.sh   # cada forma generada pasa ruff, format, mypy, pytest
bash scripts/smoke_upgrade.sh      # el proyecto de la versión anterior, actualizado a este checkout
```

Los templates son la parte que se rompe en silencio — renderizan bien y
producen código que no importa. `pytest` por sí solo no atrapa eso.
