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

## Nombres de tabla

Los nombres de tabla se pluralizan: `order` → `orders`, `category` →
`categories`. No es cosmética — los sustantivos en singular chocan con palabras
reservadas de SQL mucho más seguido que los plurales (`order`, `user`,
`group`). Sobrescribe cuando adivine mal:

```bash
jfast new module order --table sales_orders
```

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
cada uno.

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
