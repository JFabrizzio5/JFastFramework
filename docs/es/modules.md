# Servicios, módulos y layouts

Dos niveles de generación:

```bash
jfast new service billing            # a whole service
jfast new module invoice             # a domain module inside it
```

Los dos son composiciones, no plantillas fijas. Tú eliges la forma, módulo por
módulo, y la elección queda registrada.

---

## Servicios

```bash
jfast new service billing                            # JSON API
jfast new service storefront --kind web --port 8020  # server-rendered
jfast new service billing --agent-docs               # + AGENTS.md and skills
```

| Kind | Renderiza | Plugins habilitados | Extra necesario |
| --- | --- | --- | --- |
| `api` | JSON | observability, metrics, database | `[server,db,metrics]` |
| `web` | HTML | + web | `[server,db,metrics,web]` |
| `spa` | — | un proyecto de frontend | ninguno; es npm |
| `gateway` | — | gateway | `[server,gateway]` |

Un frontend es un servicio como cualquier otro. Loguea igual, reporta health
igual, se despliega igual y vive en el mismo bloque de puertos. La única
diferencia es lo que sale de los handlers.

Los dos kinds de backend se generan con `main.py`, `jfast.toml`, `.env.example`,
`conftest.py`, `requirements.txt`, `shared/`, `.gitignore` y un README. El kind
`web` agrega `templates/base.html`, `templates/index.html`, `static/app.css` y
un router `web.py` en la raíz.

`contracts.toml` no está entre ellos. Sus paths de capa son los de un layout, y
un servicio no tiene layout hasta que tiene un módulo, así que llega con el
primero — ver [Layouts de módulo](#layouts-de-módulo) más abajo.

`--agent-docs` además escribe `AGENTS.md` y `.jfast/skills/` — ver
[Trabajar con agentes de IA](agents.md).

---

## Layouts de módulo

Cuatro, y cada módulo elige el suyo. De eso se trata un monolito modular: un
módulo de catálogo son cuatro archivos, y un módulo de órdenes que tiene que
ser testeable sin base de datos quiere ports y adapters. Forzar a los dos a la
misma forma deja a uno de ellos mal.

```bash
jfast new module invoice                        # asks
jfast new module invoice --layout hexagonal     # or say
```

Corre sin `--layout` en una terminal y pregunta:

```
Architecture for 'invoice'
  › modular      a folder per layer. Start here: it grows without being moved.
    layered      a file per layer. For a table with an API and little else.
    screaming    one file per use case. When the verbs matter more than the nouns.
    hexagonal    ports and adapters. When the domain must be testable with no database.

  choice [modular] ›
```

**Sin terminal no pregunta.** Una instalación por pipe, un script o un job de
CI reciben `modular` en vez de un prompt que nadie puede ver. Un wizard que
bloquea un pipeline es peor que un flag que nadie puso.

**El primer módulo también escribe `contracts.toml`**, con los paths de capa
del layout que elegiste. Es el momento honesto más temprano: `jfast new
service` no tiene módulo ni layout, y lo que adivinaba — layered, siempre — no
coincidía con ninguno de los archivos que generan los otros tres layouts, así
que sus contratos no aplicaban nada y reportaban un pase.

Un módulo posterior en un layout *distinto* no lo reescribe. Un contrato en
disco es un documento que alguien tuvo la oportunidad de editar, y sus paths de
capa son lo de menos de lo que lleva. Agrega tú los paths del segundo layout;
nada más lo va a hacer, y `contracts check` no puede ver el hueco mientras las
capas del primer layout sigan coincidiendo con sus propios módulos. Si el
servicio entero se muda a otro layout, el check sí lo reporta, como
`layer-unmatched`.

### `layered`

La forma conocida. Sirve cuando el módulo es sobre todo CRUD y lo interesante
son los datos, no las reglas.

```
modules/invoice/
├── router.py       HTTP in, response out
├── service.py      business rules
├── repository.py   queries
├── models.py       SQLAlchemy
├── schemas.py      Pydantic
├── enums.py
├── public.py       lo que otros módulos pueden llamar
├── README.md
└── tests/
```

### `modular`

Layered, una carpeta por responsabilidad. Los límites son idénticos — su
contrato es el de layered con otras rutas — así que lo único que compran las
carpetas es espacio: una responsabilidad puede crecer a varios archivos sin que
nadie tenga que decidir dónde va el nuevo.

```
modules/invoice/
├── api/routes.py
├── models/
│   ├── invoice_entity.py       SQLAlchemy
│   └── invoice_models.py       Pydantic
├── repositories/invoice_repository.py
├── services/invoice_service.py
├── validations/invoice_validation.py
├── enums.py
├── public.py                   lo que otros módulos pueden llamar
├── README.md
└── tests/
```

`validations/` es la única idea genuinamente nueva: **reglas de negocio que no
son forma de schema.** "Este nombre ya está tomado" necesita el resto de la
tabla; "no puedes desactivar el último activo" necesita la fila actual. Ninguna
de las dos es algo que Pydantic pueda expresar, y las dos van en algún lugar
donde quien lee las encuentre.

Cada carpeta tiene un `__init__.py` que re-exporta sus nombres públicos, así
quien llama importa desde el paquete en vez de meterse dentro de un archivo.

Úsalo cuando un módulo pase de cuatro archivos — no antes. Seis carpetas
alrededor de una entidad CRUD es ceremonia.

### `screaming`

El listado del directorio es la lista de features. Sirve cuando el módulo tiene
reglas reales que vale la pena proteger del framework, y cuando quieres que las
capacidades nuevas lleguen como archivos nuevos y no como métodos nuevos en una
clase que nadie puede navegar.

```
modules/invoice/
├── invoice.py           the domain: entity + rules, framework-free
├── use_cases/
│   ├── create_invoice.py
│   ├── list_invoices.py
│   ├── get_invoice.py
│   ├── update_invoice.py
│   └── delete_invoice.py
├── storage.py           SQLAlchemy model + repository, with to_domain()
├── http.py              router + wire schemas
├── public.py            lo que otros módulos pueden llamar
├── README.md
└── tests/
    ├── test_invoice_domain.py      no database, no fakes, no event loop
    └── test_invoice_use_cases.py   fake repository, nothing else
```

### `hexagonal`

Ports y adapters. El dominio declara un port abstracto, la infraestructura lo
implementa, y la capa de aplicación depende solo del port.

```
modules/invoice/
├── domain/
│   ├── entities.py      plain dataclasses, no ORM
│   ├── ports.py         the Protocol the application depends on
│   └── enums.py
├── application/use_cases.py
├── infrastructure/
│   ├── orm.py           SQLAlchemy model
│   └── repository.py    implements the port
├── adapters/http.py     the FastAPI router
├── public.py            lo que otros módulos pueden llamar
├── README.md
└── tests/
    ├── test_invoice_domain.py      no database, no FastAPI, milliseconds
    └── test_invoice_use_cases.py   an in-memory fake implementing the port
```

**La regla que hace que valga su costo:** `domain/` no puede importar nada. Ni
SQLAlchemy, ni FastAPI, ni la capa de aplicación. El contrato generado obliga
exactamente eso:

```toml
[layers.domain]
paths = ["modules/*/domain/*.py"]
may_import = []
forbid_packages = ["sqlalchemy", "fastapi", "starlette", "httpx", "redis"]
```

En el momento en que el dominio importa el ORM, lo que estabas comprando — un
dominio testeable en milisegundos sin base de datos — desaparece, y estás
pagando cuatro carpetas por nada. Así que se verifica en vez de recordarse.

Úsalo cuando las reglas son el producto, cuando más de un punto de entrada
maneja el mismo comportamiento, o cuando el dominio tiene que testearse
exhaustivamente y rápido. Es el layout más caro de todos. La mayoría de los
módulos no lo necesitan.

### Cómo elegir

| | Úsalo cuando |
| --- | --- |
| `modular` | Por defecto. Una carpeta por capa, así un módulo crece sin reorganizarse. |
| `layered` | Una tabla con una API y poco más: cinco archivos son todo el módulo. |
| `screaming` | Los verbos importan más que los sustantivos; las capacidades llegan como archivos. |
| `hexagonal` | El dominio debe ser testeable sin base de datos, o las reglas son el producto. |

No tienes que acertar el día uno. Los layouts son por módulo, así que el
siguiente puede ser distinto, y moverse entre ellos es un refactor dentro de
una sola carpeta.

### Qué garantiza cualquier layout

Los cuatro exportan los mismos tres nombres, que es lo que permite que todo lo
demás siga siendo agnóstico del layout:

| Export | Lo usa |
| --- | --- |
| `router` | `main.py`, insertado por el generador |
| `build_service(session, tenant_id)` | el overlay de HTMX, los workers, lo que sea |
| `CreatePayload` | el handler del formulario HTMX, que tiene que construir uno sin conocer el layout |

Esos son para la app. Lo que ven *los otros módulos* es un cuarto archivo,
`public.py`, y nada más — siguiente sección.

---

## Comunicación entre módulos

**Consultas por una fachada, efectos por eventos, nada por `shared/`.**

Tarde o temprano el módulo B necesita datos que son del módulo A. Hay cuatro
formas de conseguirlos, y solo una sobrevive al día en que A cambia:

| Forma | Lo que cuesta | `contracts check` |
| --- | --- | --- |
| Importar el servicio, el repositorio o la entidad de A | B depende de las tripas de A. Renombras un método en A y B se rompe; ninguno se puede volver servicio sin el otro. | `cross-module` |
| SQL crudo contra las tablas de A desde B | El mismo acoplamiento, sin nada que lo vea. No hay import que buscar; A renombra una columna y B falla en producción. | `cross-module-sql` |
| Mover el código a `shared/` | `shared/` se vuelve un segundo hogar para comportamiento. Un repositorio ahí son dos módulos compartiendo una tabla. | — (por eso la regla es explícita) |
| **Llamar una función del `public.py` de A** | A promete una función y un DTO; todo lo que está detrás sigue siendo de A para cambiarlo. | pasa, una vez declarado |

### La fachada

Todo módulo generado tiene `modules/<nombre>/public.py`, y es el único archivo
que otro módulo le puede importar. Digamos que el módulo `asesor` — un asesor
que responde preguntas sobre gastos — necesita el gasto por categoría de
`comprobante`.

El dueño expone una función y un DTO:

```python
# modules/comprobante/public.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .repositories import ComprobanteRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class GastoPorCategoria:
    categoria: str
    total_centavos: int
    comprobantes: int


async def gasto_por_categoria(
    session: AsyncSession, *, tenant_id: str, desde: date, hasta: date
) -> list[GastoPorCategoria]:
    repositorio = ComprobanteRepository(session, tenant_id=tenant_id)
    filas = await repositorio.gasto_por_categoria(desde=desde, hasta=hasta)
    return [GastoPorCategoria(categoria=c, total_centavos=t, comprobantes=n) for c, t, n in filas]
```

La consulta en sí vive en el repositorio de `comprobante`, junto a la tabla que
lee. Quien llama importa la fachada y nada más:

```python
# modules/asesor/services/asesor_service.py
from modules.comprobante.public import gasto_por_categoria

gastos = await gasto_por_categoria(session, tenant_id=tenant_id, desde=inicio, hasta=fin)
```

Y lo dice en `contracts.toml`:

```toml
[modules.asesor]
depends_on = ["comprobante"]
```

`jfast new module` agrega un `[modules.<nombre>] depends_on = []` vacío por cada
módulo que genera, así que agregar una arista siempre es una línea que un
revisor ve cambiar. El `public.py` generado trae un ejemplo —
`get_<nombre>(session, *, tenant_id, <nombre>_id) -> <Nombre>Summary | None` —
cableado a través del repositorio propio de ese layout.

### Por qué recibe la sesión y un tenant_id

- **La sesión de quien llama** mete la lectura en su transacción: ve las filas
  que quien llama escribió antes en el mismo request, y un request nunca tiene
  dos conexiones. Una fachada que abriera su propia sesión leería otro snapshot
  y, con carga, duplicaría el pool.
- **Un `tenant_id` explícito** porque a la fachada la llaman desde lugares sin
  request del cual inferirlo — un worker, una tarea programada, el servicio de
  otro módulo. Un tenant implícito es justo como un job termina leyendo las
  filas de todos los tenants ([Colas y eventos](queues-and-events.md) tiene la
  historia). En un servicio multitenant (`--access tenant`, lo que implica
  tenancy) es `tenant_id: str`: `None` construiría el repositorio sin filtro de
  tenant y la fachada respondería por todos. En un servicio de un solo tenant es
  `tenant_id: str | None`, porque las filas se escriben sin tenant y `None` es
  el único valor que las encuentra; `jfast check --multitenant-ready` lista esas
  firmas (`facade-tenant-optional`) para cambiarlas al encender tenancy.
- **DTOs, no entidades.** Una entidad del ORM arrastra su sesión y sus
  relaciones lazy a través de la frontera, y cada columna se vuelve parte de la
  API el día en que alguien la lee. Un DTO es una promesa que elegiste hacer.
  `public.py` tampoco puede importar FastAPI: tiene que funcionar donde no hay
  request.

### Los efectos van por eventos

Una fachada responde preguntas. Cuando `asesor` necesita *reaccionar* a algo
que hizo `comprobante` — se categorizó un comprobante, así que el consejo quedó
viejo — no lo llaman de vuelta. `comprobante` publica un evento en la misma
transacción que la escritura, por el outbox, y `asesor` se suscribe:

```python
# modules/comprobante/services/comprobante_service.py -- junto a la escritura
from jfastframework.events import Event

await outbox.publish(
    session, "comprobantes", Event(type="comprobante.categorized", data={"id": c.id}, key=str(c.id))
)
```

```python
# modules/asesor/tasks.py
from jfastframework.events import Event, subscribe
from jfastframework.tasks import TaskSession

@subscribe("comprobante.categorized")
async def refrescar_consejo(event: Event, session: TaskSession) -> None:
    ...
```

```toml
# contracts.toml
[modules.comprobante]
publishes = ["comprobante.categorized"]
```

`outbox.publish` encola un job por suscriptor en la misma transacción que la
escritura, y `jfast worker` lo corre con el tenant que publicó. No hay broker
de por medio: funciona en el stack por defecto con PostgreSQL. `comprobante`
nunca se entera de que `asesor` existe y `asesor` no declara `depends_on` por
esto — así que no hay arista en ningún sentido, y no hay ciclo. Un evento al que
nadie se suscribe se rechaza en la request, y `contracts check` reporta una
suscripción que nadie declara publicar. Las garantías de entrega están en
[Colas y eventos](queues-and-events.md#eventos-entre-modulos); `@on(topic)`
sobre Kafka es para *otros servicios*, no para módulos de este.

### La recompensa: extraer un módulo

El día en que `comprobante` se muda a su propio servicio
(`jfast new service comprobante`), el cambio de este lado es un archivo:
`public.py` conserva sus firmas y sus DTOs, y su cuerpo se vuelve una llamada
al nuevo servicio con el [plugin `http`](http-client.md). El argumento
`session` simplemente deja de usarse. La línea de import de `asesor` no cambia,
porque nunca supo de dónde venía la respuesta.

Eso solo funciona si `public.py` era la única entrada. Un módulo que además se
metía en el repositorio de `comprobante`, o consultaba sus tablas, hay que
encontrarlo y reescribirlo primero — que es justo lo que los checks de abajo
existen para evitar.

### Qué se verifica

| Regla | Salta cuando |
| --- | --- |
| `cross-module` | un módulo importa cualquier cosa de otro módulo que no sea `modules/<otro>/public.py` |
| `undeclared-dependency` | importa `modules.<otro>.public` sin `<otro>` en su `depends_on` |
| `module-cycle` | el grafo de `depends_on` declarados más los imports reales de fachadas tiene un ciclo |
| `public-leak` | `public.py` importa o reexporta una entidad del ORM, o importa `fastapi`/`starlette` |
| `cross-module-sql` | un string en un módulo tiene SQL que nombra una tabla de otro módulo |

Los mensajes, las exenciones y el único switch que las apaga están en
[Contratos](contracts.md#entre-modulos).

---

## El layout queda registrado

`jfast new module` guarda la elección en `jfast.toml`:

```toml
# How each module was generated, so later commands know where a
# new file belongs. Written by `jfast new module`.
[modules.invoice]
layout = "hexagonal"
ui = "api"

[modules.catalogue]
layout = "layered"
ui = "api"
```

Preguntar de nuevo cada vez termina dando una respuesta distinta, y adivinar a
partir de las carpetas en disco se rompe en cuanto alguien agrega una. El
runtime ignora la tabla — es contabilidad del CLI que vive en el archivo que ya
estaba ahí.

---

## Los módulos se registran solos

`main.py` viene con dos marcadores, y el generador inserta ahí:

```python
from modules.invoice import router as invoice_router
# [jfast:imports]

ROUTERS: list[APIRouter] = [
    invoice_router,
    # [jfast:routers]
]

app = create_app(routers=ROUTERS)
```

Idempotente: generar el mismo módulo dos veces no lo monta dos veces. **Deja
los marcadores.** Sin ellos el generador imprime qué pegar en vez de adivinar
un número de línea — no va a hacer fallar tu scaffold por eso, pero deja de
registrar por ti.

---

## UI: JSON, HTML o las dos

```bash
jfast new module invoice --ui api    # JSON only (default)
jfast new module invoice --ui htmx   # JSON plus server-rendered pages
```

`--ui htmx` es un *overlay*, compuesto encima de cualquier layout en vez de
duplicado por layout. Agrega:

```
modules/invoice/web.py           HTML router, mounted at /ui/invoices
templates/invoice/index.html     the page
templates/invoice/_rows.html     the table body fragment
templates/invoice/_row.html      one row
templates/base.html              only if the project has none
```

**La superficie HTML vive bajo `/ui/`.** El router JSON ya es dueño de
`/invoices` y declara ahí los mismos verbos, así que dos routers en un mismo
prefijo hacían que respondiera el que se registrara primero — navegar devolvía
JSON, y el formulario hacía POST contra el handler de la API. Prefijos
distintos hacen que ninguno pueda tapar al otro, sin importar el orden en
`main.py`.

| Path | Devuelve |
| --- | --- |
| `/invoices` | JSON |
| `/ui/invoices` | la página, o un fragmento para un `hx-get` |

El router JSON se queda. Un módulo sirve las dos superficies desde las mismas
reglas, que es el punto: las vistas HTML no son una segunda implementación.

### Renderizado parcial

HTMX manda `HX-Request: true` y espera un fragmento. `render()` resuelve las
dos cosas desde un solo handler:

```python
return render(request, "invoice/index.html", {"page": page},
              partial="invoice/_rows.html")
```

La navegación del browser recibe la página completa. `hx-get` recibe solo las
filas. Un handler, un contexto, sin markup duplicado — la página hace
`{% include %}` del mismo fragmento que devuelve.

### Errores

Con el plugin `web` habilitado, un `JFastError` lanzado durante un request de
HTMX vuelve como fragmento HTML en vez de `problem+json`. HTMX inserta el
cuerpo de la respuesta en el DOM, así que el JSON se le mostraría al usuario
como texto crudo. Los requests normales siguen recibiendo `problem+json`, lo
que mantiene honesto en ambas superficies a un servicio mixto de API y web.

---

## Campos: genera el módulo que querías

Un módulo generado sin campos trae un ejemplo -- `name`, `description`,
`is_active` -- que no sirve para ningún dominio real. El primer módulo hecho
sobre 0.1.0a10 pasó de 654 líneas generadas a 276 conservadas. Di qué guarda el
módulo y cada lugar donde estaba el ejemplo recibe los campos reales:

```bash
jfast new module presupuesto \
  --fields "cartera_id:int, mes:str(7), gasto:money, leida:bool=false, nota:text?" \
  --unique "cartera_id,mes"
```

Eso escribe la entidad (con su restricción única), los modelos de alta,
edición y lectura con los mismos límites, un finder en el repositorio por cada
llave única, la regla que convierte una llave ocupada en un 409 legible -- al
crear y en la edición que toca la llave --, el DTO de `public.py` con los
campos reales, y tests que los ejercitan todos. Sirve en los cuatro layouts.
Para un módulo cuyos campos aún no se conocen, o que solo guarda relaciones:

```bash
jfast new module alerta --bare      # la estructura, sin campos ni ejemplo
```

### La gramática

Un campo por coma; las comas dentro de paréntesis no separan.

```
campo := nombre ":" tipo ["?"] ["=" default]
```

| Tipo | Python | Columna | En la petición |
| --- | --- | --- | --- |
| `int` | `int` | `INTEGER` | |
| `bigint` | `int` | `BIGINT` | |
| `str(N)` | `str` | `VARCHAR(N)` | `max_length=N`; `min_length=1` salvo que sea nulable |
| `str` | `str` | `VARCHAR(255)` | como `str(255)` |
| `text` | `str` | `TEXT` | `min_length=1` salvo que sea nulable |
| `bool` | `bool` | `BOOLEAN` | |
| `float` | `float` | `FLOAT` | |
| `decimal(P,S)` | `Decimal` | `NUMERIC(P,S)` | `max_digits=P, decimal_places=S` |
| `money` | `int` | `BIGINT` | unidades menores enteras: 1050 es 10.50 |
| `date` | `date` | `DATE` | |
| `datetime` | `datetime` | `TIMESTAMPTZ` | `AwareDatetime`: una sin zona es un 422 |
| `json` | `dict[str, Any]` | `JSONB` (`JSON` fuera de PostgreSQL) | |
| `enum(a,b,...)` | un `StrEnum`, `<Modulo><Campo>` | `VARCHAR` + `CHECK (campo IN ('a', 'b'))` | el enum: cualquier otro valor es un 422 |

- `?` lo hace nulable, y opcional al crear.
- `=valor` es el default, escrito en la sintaxis del tipo: `=0`, `=false`,
  `=pendiente`, `="dos palabras"`, `=0.00`. `date`, `datetime` y `json` no
  aceptan default: un "ahora" por defecto es una decisión de zona horaria y va
  en el servicio.
- `--unique "a,b"` hace el par único por tenant; repítelo para más llaves. Cada
  restricción lleva nombre, porque dos restricciones que empiezan por
  `tenant_id` en la misma tabla compartirían el del naming convention.
- `money` es entero a propósito. Los floats no suman al centavo; `decimal(12,2)`
  es la alternativa cuando el monto tiene de verdad una escala fija.
- `enum(personal,empresa,otra)` escribe `class CarteraTipo(StrEnum)` en el
  archivo de enums del módulo (`domain/enums.py` en hexagonal), con la misma
  forma que escribe `jfast new enum`, y la usa en todos los lugares donde
  aparece el campo: la columna, los modelos de creación/actualización/lectura,
  la entidad de dominio y `public.py`. Los valores van en snake_case, al menos
  dos; el miembro es el valor en mayúsculas (`en_revision` es `EN_REVISION`). El
  default es uno de ellos (`=personal`), `?` lo hace opcional, y puede formar
  parte de una llave `--unique`.
  La columna es `Enum(native_enum=False)` de SQLAlchemy guardando el *valor*
  del miembro -- un `VARCHAR`, así que la fila se lee de vuelta como el enum y
  el SQLite en memoria de una prueba crea la misma tabla -- más un CHECK con
  nombre (`ck_<tabla>_<campo>`) construido desde el enum. No un `ENUM` nativo
  de PostgreSQL: autogenerate escribe el par como una columna `sa.Enum(...)` y
  un `sa.CheckConstraint`, donde `create_constraint=True` escribía el CHECK dos
  veces. Autogenerate no compara restricciones CHECK, así que un miembro nuevo
  necesita una migración que borre `ck_<tabla>_<campo>` y lo cree de nuevo (y
  que ensanche la columna si el valor nuevo es más largo que el más largo).

Cada error se rechaza antes de escribir un archivo, con el arreglo en el
mensaje: un tipo desconocido lista los que existen, `id` o `tenant_id` avisa que
ya están en toda entidad, `str(0)` apunta a `text`, y un nombre que taparía algo
que usa el código generado (`payload`, `json`, `model_...`) pide otro nombre.

`tenant_id` está en toda entidad generada, sean cuales sean los campos -- ver
[pasar a multitenant después](multitenancy.md#pasar-a-multitenant-despues):
es lo que convierte "tenemos un segundo cliente" en un backfill en vez de una
reescritura del esquema.

`--ui htmx` se rechaza con `--fields` o `--bare`: las páginas que dibuja son
para los campos de ejemplo. Genera el módulo de API y escribe las páginas para
los tuyos.

### Quién puede llamar a las rutas

Las rutas generadas leen al que llama según cómo esté configurado el servicio,
así que un módulo nunca queda más abierto que el servicio que lo contiene:

| `jfast.toml` habilita | Rutas | Tenant |
| --- | --- | --- |
| `tenancy` | `tenant_id: str = Depends(current_tenant)` en la factory | el del que llama; 401 sin sesión, 403 sin tenant |
| `auth` o `accounts` | `APIRouter(dependencies=[Depends(require_auth)])` | ninguno: un solo cliente |
| ninguno | abiertas | lo que haya resuelto la petición -- nada |

`--access open|auth|tenant` lo cambia para un módulo.

### Cada módulo sale formateado y tipado

Lo que escribe el generador pasa los controles del propio proyecto -- `ruff
check .`, `ruff format --check .`, `mypy .` (estricto) y `pytest` -- en todos
los layouts y todas las formas; los detalles están en [los controles del
proyecto generado](migrations-and-tests.md#los-controles-del-proyecto-generado).
Los archivos nuevos además pasan por el ruff del proyecto cuando está instalado
(viene en `requirements-dev.txt`), porque un nombre de módulo largo produce
líneas que ninguna plantilla puede partir de antemano. Sin ruff se escriben tal
como se renderizan, y `ruff format .` termina el trabajo.

Todo módulo trae además un `tasks.py` vacío: el lugar de sus `@task` y
`@subscribe`, que la API y `jfast worker` encuentran al arrancar. Ver [colas y
eventos](queues-and-events.md).

---

## Nombres de tabla

Los nombres de tabla se pluralizan: `order` → `orders`, `category` →
`categories`. No es cosmética — los sustantivos en singular chocan con palabras
reservadas de SQL mucho más seguido que los plurales (`order`, `user`,
`group`). Sobrescribe cuando adivine mal:

```bash
jfast new module order --table sales_orders
```

Un proyecto que nombra sus módulos en español recibe plurales en español.
Dilo una vez, en `jfast.toml`:

```toml
[scaffold]
language = "es"
```

y `camion` queda `camiones`, `sucursal` queda `sucursales`, `lapiz` queda
`lapices` y `lunes` se queda `lunes`. En un nombre compuesto se pluraliza el
sustantivo principal, como en español: `orden_compra` → `ordenes_compra`.
`--language en` o `--language es` sobrescribe al proyecto para un módulo; otro
valor se rechaza. Sin el ajuste se usan las reglas del inglés, como antes.

---

## Combinaciones

Cuatro layouts × dos UIs = ocho, a partir de seis árboles de plantillas.
Composición en vez de ocho copias que se van separando entre sí:

```bash
jfast new module invoice --layout modular --ui htmx
jfast new module ledger  --layout hexagonal
```

Cada combinación se ejercita en CI. `scripts/smoke_layouts.sh` genera los
cuatro layouts y verifica que cada uno renderiza, importa, monta sus rutas y
pasa su propio contrato; `scripts/smoke_htmx.sh` envía el formulario real en
cada uno; y `scripts/smoke_generated_quality.sh` corre ruff, ruff format, mypy
y pytest sobre cada layout con campos de ejemplo, con `--fields` y con `--bare`.

---

## Después de generar

```bash
jfast contracts check                       # before anything else
pytest modules/invoice/tests
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
```

O todo junto, con la base de datos arriba y la migración aplicada primero:

```bash
jfast dev
```

Ver [El ciclo local](dev.md).

Lee la migración generada antes de aplicarla. Autogenerate se pierde los
defaults del lado del servidor, los cambios de enum y los renombres de índices.

Los archivos generados llevan un sello `.jfast-template` que registra qué
plantilla y qué versión del framework los produjo. Ese sello es lo que un
futuro `jfast upgrade` usa para mostrar un diff en vez de una reescritura.
