# Contratos

`AGENTS.md` dice qué hacer. Un contrato dice qué está **permitido**, y algo lo
verifica.

Esa diferencia es todo el punto. Un agente que genera código a toda velocidad
se va a pasar de largo una sugerencia sin notarlo — no por mala fe, sino
porque nada lo frenó. Un contrato lo frena:

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'  (Data access. No business rules.)

1 violation(s). Fix them, or waive one inline with
    # contracts: allow <reason>
```

Salida distinta de cero. En CI, eso es un build roto.

---

## Las tres audiencias, un archivo

`contracts.toml` está en la raíz de un servicio, escrito por su **primer**
`jfast new module` con los paths de capa del layout de ese módulo.

| Audiencia | Lo lee como |
| --- | --- |
| El build | `jfast contracts check` — falla ante una violación |
| Un agente | `jfast contracts show --json` — antes de escribir una línea |
| Una persona | `CONTRACTS.md` — generado, para review |

Una fuente, tres representaciones, para que el documento y la regla aplicada
no puedan contradecirse.

No lo escribe `jfast new service`, y es a propósito. Un servicio se genera
antes de que exista un módulo, así que no hay layout para el cual escribir un
contrato — y lo que ahí se adivinaba era siempre el layered. En un servicio
hexagonal, modular o screaming sus globs no coincidían con ningún archivo en
disco, así que cada regla de capa se aplicaba a nada mientras
`contracts check` reportaba un pase.

Un servicio que todavía no tiene módulos y quiere las reglas de servicio
— llamadas prohibidas, seguridad del event loop — puede escribirlo con
`jfast contracts init`.

### Más de un layout en un servicio

Un servicio puede tener módulos de varios layouts; de eso se trata un monolito
modular. El contrato tiene los paths de capa de **uno** de ellos, el primero, y
un módulo en un segundo layout no coincide con ningún glob de capa — así que
solo lo alcanzan las reglas de servicio. Agrega tú sus paths a
`contracts.toml`. `contracts check` detecta el caso completo, donde *ningún*
archivo coincide con una capa, y no puede detectar el mixto, porque las capas
del primer layout siguen coincidiendo con sus propios módulos.

---

## Lo que declaras

### Alcance

```toml
[project]
name = "billing"
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."
```

`does_not_own` es la mitad más útil, y la que la gente omite. Casi todo el
código malo en un sistema que crece es un servicio expandiéndose en silencio
hacia algo que otro servicio ya posee — y nunca se ve mal desde adentro de ese
servicio.

### Capas

```toml
[layers.domain]
description = "Entities and their rules. Framework-free."
paths = ["modules/*/[!_]*.py"]
may_import = ["shared"]
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain", "shared"]
```

`may_import` nombra **otras capas**, no paquetes. Las capas son sobre lo que
discute un reviewer; los nombres de paquetes son lo que se le olvida.

Un archivo se clasifica por el patrón coincidente **más específico** — el de
menos comodines, no el de cadena más larga. Esa distinción es crítica:
`modules/*/[!_]*.py` es más largo que `modules/*/http.py`, y ordenar por
longitud clasificaría cada router como código de dominio para después rechazar
sus imports por una razón que nadie podría deducir.

#### `*` se detiene en `/`. `**` lo cruza.

Un glob de capa es una afirmación sobre *dónde en el árbol* está un archivo, así
que el separador es un límite real:

| Patrón | Matchea | No matchea |
| --- | --- | --- |
| `modules/*/repository.py` | `modules/invoice/repository.py` | `modules/invoice/infrastructure/repository.py` |
| `modules/**/repository.py` | los dos de arriba | — |
| `modules/*/[!_]*.py` | `modules/invoice/invoice.py` | `modules/invoice/tests/test_invoice.py`, `modules/invoice/__init__.py` |

`**/` también matchea *cero* directorios, así que `modules/**/http.py` cubre
`modules/http.py`.

Deliberadamente **no** es `fnmatch`, que traduce `*` a `.*` y lo deja atravesar
un separador. Con esa lectura, la capa `storage` del contrato layered reclamaba
el `modules/*/infrastructure/repository.py` de un proyecto hexagonal, contaba
como que gobernaba algo, y `layer-unmatched` — el hallazgo cuyo único trabajo es
detectar un contrato que no gobierna nada — se quedaba callado sobre un contrato
que casi no gobernaba nada. El catch-all de screaming era lo mismo un directorio
más adentro: `modules/*/[!_]*.py` se tragaba todos los `modules/*/tests/*.py`, y
los `forbid_packages` de la capa de dominio terminaban aplicándose a archivos de
test.

El matcheo es sensible a mayúsculas en toda plataforma, a propósito. `fnmatch`
normaliza mayúsculas en Windows, y una regla que responde distinto según el
sistema operativo no es una regla.

#### `shared` está en todas las listas, y en ninguna propia

Todo contrato generado declara una capa `shared` para `shared/*.py`, y todas
las demás capas pueden importarla — el dominio incluido. No es un aflojamiento:
es lo que hace legal el consejo de [shared/, enums y
channels](shared-and-events.md). `[rules.placement]` te dice que muevas a
`shared/` el enum que quiere un segundo módulo, y una capa que no podía
importar `shared/` no tenía forma de obedecer la instrucción que el propio
checker imprimía.

Sigue siendo seguro porque `shared` mantiene `may_import = []` y se prohíbe a
sí misma `sqlalchemy` y `fastapi`. Nada llega a una base de datos ni a un
router a través de un enum, y la dirección sigue siendo de una sola vía — lo
que `[rules.placement]` verifica desde el otro lado.

#### `public` es la puerta de cada módulo

Todo contrato generado declara además una capa `public` para
`modules/*/public.py`: la fachada que llaman los otros módulos. Puede importar
las capas de su propio módulo — una lectura puede ir directo a storage, una
escritura pasa por el servicio — y nada dentro del módulo puede importarla de
vuelta.

Desde qué capa de *otro* módulo se llama a una fachada no es una pregunta de
capas. Un import de `modules.<otro>.public` desde un módulo distinto se salta el
check de capas y lo gobiernan `[modules.*]` y `[rules.placement]`, abajo: las
reglas de capa describen el interior de un módulo, y la fachada es el borde de
otro. En el layout screaming, `public` es más específica que el comodín del
dominio `modules/*/[!_]*.py`, así que a la fachada no se le aplican las reglas
del dominio.

### Entre módulos

Consultas por una fachada, efectos por eventos, nada por `shared/`.
[Servicios, módulos y layouts](modules.md#comunicacion-entre-modulos) recorre un
ejemplo completo; esto es lo que el checker te exige.

```toml
[modules.asesor]
depends_on = ["comprobante"]    # asesor puede llamar a modules/comprobante/public.py
```

```python
# modules/comprobante/public.py
@dataclass(frozen=True, slots=True)
class GastoPorCategoria:
    categoria: str
    total_centavos: int

async def gasto_por_categoria(session, *, tenant_id: str, desde: date) -> list[GastoPorCategoria]: ...

# modules/asesor/services/asesor_service.py
from modules.comprobante.public import gasto_por_categoria
```

Un módulo sin bloque `[modules.<nombre>]` no depende de nada. `jfast new
module` agrega uno vacío por cada módulo que genera.

| Regla | Se reporta cuando | Qué dice |
| --- | --- | --- |
| `cross-module` | un módulo importa cualquier cosa de otro módulo que no sea su `public.py` | `module 'asesor' imports modules.comprobante.services; import modules.comprobante.public instead` — y, si ese archivo no existe, que lo crees con una función que devuelva DTOs |
| `undeclared-dependency` | importa `modules.<otro>.public`, o encola `Job(task="...")` de una task que `<otro>` declara con `@task`, sin `<otro>` en `depends_on` | `module 'asesor' calls modules.comprobante.public but does not declare 'comprobante' in depends_on` |
| `module-cycle` | el grafo de `depends_on` declarados más los imports reales de fachadas y las referencias a tasks tiene un ciclo | `module dependency cycle: asesor -> comprobante -> asesor`, una vez por ciclo |
| `unused-dependency` | una entrada de `depends_on` nombra un módulo que este nunca importa ni del que encola una task | `[modules.asesor] depends_on lists 'cartera', but module 'asesor' never calls modules.cartera.public or queues one of its tasks` -- reportado en la línea de `contracts.toml` |
| `public-leak` | `public.py` importa o reexporta una entidad del ORM (cualquier clase de ese módulo cuyo cuerpo asigna `__tablename__`), o importa `fastapi`/`starlette` | `modules/comprobante/public.py imports the ORM entity Comprobante` |
| `cross-module-sql` | un string en `modules/<aquí>/` tiene SQL que nombra una tabla de otro módulo | `module 'asesor' queries 'comprobantes' (module 'comprobante') with raw SQL` |
| `unknown-dependency` | un bloque `[modules.x]` o una entrada de `depends_on` nombra algo que no es un módulo en `modules/` -- casi siempre un typo | `[modules.asesor] depends_on names 'comprobantes', which is not a module under modules/` |
| `shared-direction` | `shared/` importa un módulo | `shared/ imports modules.invoice` |

Dos casos conservan el consejo de antes. Un módulo de **enums o tipos**
importado (`modules.x.enums`, `modules.x.domain.enums`, `modules.x.types`) es
vocabulario, así que `cross-module` sigue nombrando el archivo de `shared/` al
que moverlo. Cualquier otra cosa — un servicio, un repositorio, una entidad —
es comportamiento, y `shared/` es la respuesta equivocada: el mensaje apunta al
`public.py` del dueño.

Por qué existe cada regla:

- **La fachada, no `shared/`.** `shared/` es vocabulario: enums, tipos,
  funciones puras. El comportamiento que se muda ahí para esquivar
  `cross-module` es un repositorio que comparten dos módulos, o sea dos módulos
  compartiendo una tabla.
- **Nada de SQL crudo.** `text("SELECT ... FROM comprobantes")` dentro de
  `asesor` es el acoplamiento que habría sido un import, menos cualquier cosa
  que lo vea. La propiedad sale de `__tablename__`: una tabla declarada bajo
  `modules/comprobante/` es de `comprobante`, y solo se reportan las tablas de
  un módulo *distinto*. Se buscan en literales de string, incluidas las partes
  constantes de los f-strings; los docstrings se saltan.
- **`depends_on` se declara.** Así el grafo es una decisión revisada y no lo
  que sumen los imports, y un ciclo se ve en `contracts.toml` antes de
  construirse. Se rompe convirtiendo una dirección en evento — el módulo de
  abajo se suscribe en vez de que lo llamen de vuelta.
- **DTOs y nada de HTTP en `public.py`.** A la fachada la llaman workers y
  otros módulos, no solo un request; una entidad arrastra su sesión y todas sus
  columnas a través de la frontera.

Todas respetan la exención inline, y `[rules.placement] enabled = false` apaga
todas juntas -- incluidas las de eventos de abajo. El nombre del archivo de la
fachada es fijo: `public.py`. `jfast inspect` reporta `module-cycle` desde el
mismo grafo -- imports más `depends_on` declarados --, así que los dos comandos
no pueden discrepar sobre si un proyecto tiene un ciclo.

### Eventos y tasks

La otra forma de cruzar una frontera es un evento, y también es parte del
contrato. Un módulo declara los tipos de evento que publica; las suscripciones
y el dueño de cada task se leen del código:

```toml
[modules.comprobante]
depends_on = []
publishes = ["comprobante.registrado"]
```

```python
# modules/alerta/tasks.py
@subscribe("comprobante.registrado")
async def revisar_presupuesto(event: Event, session: TaskSession) -> None: ...
```

`alerta` **no** declara `depends_on = ["comprobante"]` por esto: un suscriptor
no depende de nada, y ese es el punto. Ver
[Colas y eventos](queues-and-events.md#eventos-entre-módulos) para cómo se
entrega el evento.

| Regla | Se reporta cuando | Arreglo |
| --- | --- | --- |
| `orphan-subscription` | un `@subscribe("<tipo>")` nombra un evento que ningún módulo declara en `publishes` | declararlo en el módulo que lo publica, o corregir el nombre. Un evento de otro servicio llega por Kafka: usa `@on(topic)` para él |
| `undeclared-event` | se construye `Event(type="<tipo>")` en un módulo cuyo bloque no lista el tipo en `publishes` | agregarlo a los `publishes` de ese módulo |
| `undeclared-dependency` | se encola `Job(task="alerta.revisar")` desde un módulo distinto del que la declara con `@task` | publicar un evento y suscribirse con `@subscribe` -- o declarar la dependencia |

Solo se leen literales de texto -- un tipo o nombre de task construido en tiempo
de ejecución no se adivina -- y se saltan los `tests/` de cada módulo. Encolar la
task de otro módulo por su nombre es el ciclo escondido que esto atrapa:
`comprobante` encolando `alerta.revisar` mientras `alerta` lee la fachada de
`comprobante` pasaba todos los checks antes, y ahora es `undeclared-dependency`,
y `module-cycle` una vez declarado.

`jfast contracts show --json` trae `events` (cada tipo con sus publicadores
declarados, los módulos que lo construyen y sus suscriptores) y `tasks` (cada
task, su dueño y quién la encola); `CONTRACTS.md` presenta ambos como tablas, y
`jfast ai context` agrega de cada módulo las funciones de su fachada,
`publishes`, `subscribes` y `tasks`.

### Llamadas prohibidas

```toml
[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py", "config/*.py", "migrations/env.py"]
why = "Configuration is typed. Add a field to a settings model so a bad value fails at boot."
```

`why` no es decoración. Es lo que imprime el checker, y la diferencia entre
alguien que arregla la causa y alguien que borra la línea.

Un patrón con puntos también atrapa el import pelado, así que
`from os import getenv` no se escapa. Un patrón sin puntos coincide exacto,
así que prohibir `print` no marca además `report.print()`.

### Estructura requerida

```toml
[[rules.require]]
path = "tests"
applies_to = "modules/*"
why = "A module with no tests is a module nobody can change safely."
```

### Seguridad del event loop

El bug más difícil de un servicio async es el que nunca lanza una excepción.
Una llamada bloqueante dentro de `async def` frena todas las demás requests de
ese worker mientras dura, y el síntoma llega como latencia en endpoints que no
tienen nada que ver con la causa. Nada en el traceback, nada en el log, y un
profile del endpoint lento apunta a código que es inocente.

Así que es una regla de contrato, verificada en cada build:

```toml
[rules.async_safety]
enabled = true
naive_datetime = true
allow_in = ["tests/*", "conftest.py", "scripts/*", "migrations/*"]
follow_local_helpers = true

[rules.async_safety.extra_blocking]
"myapp.legacy.render_pdf" = "await asyncio.to_thread(render_pdf, ...)"
```

| Clave | Default | Apaga |
| --- | --- | --- |
| `enabled` | `true` | toda la tabla: `async-blocking` **y** `naive-datetime` |
| `naive_datetime` | `true` | solo `naive-datetime`, dejando `async-blocking` encendida |
| `allow_in` | `["tests/*", "conftest.py", "scripts/*", "migrations/*"]` | el chequeo bajo esas rutas. Reemplaza la lista por defecto, nunca la extiende |
| `follow_local_helpers` | `true` | seguir un helper síncrono del mismo archivo hasta sus llamadores async |

Dos reglas comparten esta tabla, y solo una es sobre el event loop.
`naive-datetime` — ver [Zonas horarias](timezones.md#el-check-del-contrato) —
vive acá porque era la única tabla de reglas que tenía el modelo de contrato,
así que tiene su propio switch: si no, un proyecto que la silencia silenciaría
también el chequeo async, y la que no quería apagar es justo la que estaba
funcionando. `enabled = false` sigue apagando las dos, porque eso es lo que
"esta tabla está apagada" tiene que significar.

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
  (every other request on this worker waits. Use httpx.AsyncClient, already a dependency of gateway and auth)
blocking_demo.py:13: async-blocking: warm_cache() is synchronous and calls time.sleep(), which blocks the event loop
  (make the helper a coroutine, or offload it with asyncio.to_thread)
```

Tres cosas que encuentra y que un linter de propósito general no:

* **Clientes en `self`.** `self._s3 = boto3.client("s3")` en `__init__`, y
  después `self._s3.put_object(...)` en un método async cuatro pantallas más
  abajo. La llamada es un método sobre una instancia, invisible para una regla
  que lee una función a la vez.
* **Un salto de indirección.** La llamada bloqueante casi nunca está en el
  handler; está en el helper síncrono que el handler llama. Dentro de un
  archivo, ese helper se sigue hasta quienes lo llaman.
* **Tu propio código.** `extra_blocking` es donde el equipo anota las
  funciones que solo él conoce, con el reemplazo al que hay que ir.

Conoce `boto3`, `pymongo`, `psycopg2`, el cliente síncrono de `redis` y
`sqlite3` por nombre, más los casos de la biblioteca estándar: `time.sleep`,
`subprocess`, `requests`, I/O bloqueante de `pathlib`, `open()`, y
`asyncio.run` o `run_until_complete` dentro de una corrutina.

**Lo que no va a hacer**, por el mismo principio que el resto del checker:

* Un handler con `def` síncrono no se reporta. FastAPI lo corre en un
  threadpool; esa es una forma soportada de escribir una ruta, no un bug.
* El trabajo entregado a `asyncio.to_thread`, `run_in_executor`,
  `anyio.to_thread.run_sync` o `run_in_threadpool` es código correcto y se
  deja en paz -- incluida la clausura síncrona que le pasas, que es por lo que
  `storage/s3.py` no reporta nada.
* Una llamada que no puede resolver a través de los imports del archivo no se
  reporta. `self._client.ping()` podría ser cualquier cosa, y un checker que
  adivinara marcaría cada `ping` del codebase.
* Nada cruza el límite de un archivo. Resolver un nombre hasta su definición
  en otro módulo es trabajo de un type checker.

Si además usas ruff, activa su ruleset `ASYNC` -- cubre los casos de la
biblioteca estándar de forma independiente. Este framework lo hace, y prender
la regla encontró dos llamadas bloqueantes a `Path.is_dir()` en su propia
sonda de readiness.

Exime una cuando bloquear de verdad es lo correcto:

```python
time.sleep(0)  # contracts: allow one-off at startup, not per request
```

### Interfaces

```toml
[[provides]]
name = "invoices-api"
kind = "http"
path = "/invoices"
stability = "stable"      # experimental | stable | deprecated

[[consumes]]
name = "catalog"
via_env = "API_CATALOG_URL"
```

Anotado para que cambiar una interfaz `stable` sea una decisión visible en vez
de una sorpresa para quien haya dependido de ella.

### Invariantes

```toml
[invariants]
rules = [
  "Money is stored in minor units as an integer. Never a float.",
  "A job handler is idempotent: delivery is at-least-once.",
]
```

El checker no puede verificarlas. Están aquí justamente porque nada más las va
a atrapar — esta es la lista que un reviewer, o un agente, revisa a mano.

---

## Exenciones

```python
from sqlalchemy import text  # contracts: allow one-off reporting query, JF-412
```

La razón es obligatoria. Una exención es una decisión; `jfast contracts
waivers` lista cada una, porque las decisiones que nadie revisa son
exactamente cómo un contrato deja de significar algo.

---

## Por qué existe una regla: `contracts explain`

`contracts check` dice que se rompió una regla. No dice *por qué la regla está
ahí*, y un agente al que le llega una violación sin remedio tiende a satisfacer
al checker en vez de arreglar el diseño — borrando el import, copiando el
código al segundo módulo, o apagando la regla. `explain` cierra eso:

```bash
jfast contracts explain billing analytics          # may billing import analytics?
jfast contracts explain http sqlalchemy            # may the http layer import it?
jfast contracts explain --rule shared-direction    # what is that rule, and where
jfast contracts explain --file modules/billing/service.py
jfast contracts explain --json
jfast contracts explain                            # every rule that can fire here
```

```
may module 'asesor' import module 'comprobante'?  [FORBIDDEN]

  rule     cross-module  -- one module imported something of another module other than its
           public.py
  what     'asesor' does not declare 'comprobante' in depends_on, so it may not import it -- and
           even when it does, only through modules/comprobante/public.py
  declared contracts.toml:148
           [rules.placement]
  declared contracts.toml:219
           depends_on = []
  why      Placement: how modules talk to each other.
           Queries through a facade, effects through events, nothing through shared/.
           A module that needs another's data imports modules/<other>/public.py and nothing else
           of it: a few functions that take the caller's session and an explicit tenant_id and
           return DTOs. [...]
  instead  - to read data 'comprobante' owns: call a function in modules/comprobante/public.py
           that returns DTOs, and add 'comprobante' to depends_on under [modules.asesor] in
           contracts.toml
           - to react to something 'comprobante' did: subscribe to the event it publishes
           through the outbox, and depend on nothing
           - if it is an enum or a type both modules speak: move it to shared/enums.py and
           import it from both
           - if only 'asesor' needs it, it belongs in 'asesor'
           - waive this one line with # contracts: allow <reason> while the move is in flight

  waiver   # contracts: allow <reason> -- one line, and only the rule that fired on it
           jfast contracts waivers lists every one, so it stays a reviewable decision
           deleting the rule from contracts.toml removes it for every file and for everyone,
           silently, and nothing reports the next violation
```

Cuatro cosas, y la segunda es la que no daba nada más:

* **Qué regla** lo prohíbe, en el mismo vocabulario que imprime `check`.
* **Dónde está declarada** — `contracts.toml` y un número de línea, con la
  línea misma, así la afirmación se verifica en vez de creerse. Cada layout
  declara capas distintas en líneas distintas, y la respuesta sigue al contrato
  que tiene enfrente.
* **Por qué está la regla**, citado del comentario que su autor escribió arriba
  de esa declaración. Los contratos generados llevan una justificación arriba
  de cada regla; esto la lee en vez de inventar prosa. Donde no hay comentario,
  usa el `description` de la capa y el `why` de la regla.
* **Qué hacer en su lugar**, nombrando un destino —
  `modules/comprobante/public.py`, no "no hagas eso" — y **qué cuesta una exención**, sus dos mitades: la exención
  inline ocupa una línea y queda listada por `jfast contracts waivers`,
  mientras que editar `contracts.toml` saca la regla para todos, en silencio.

### Cuando no puede responder

Una respuesta que no puede derivar se reporta como `[UNKNOWN]` con lo que sí
sabe — las capas que declara el contrato, los módulos en disco — y el comando
sale con código distinto de cero, así un script distingue "no" de "no sé". Lo
mismo aplica a un archivo que ninguna capa reclama: eso no es un aprobado, es
un path que nadie incluyó.

`--json` lleva todo eso, más los `paths`, `may_import`, `forbid_packages` y
`description` de cada capa, y las violaciones vivas del archivo por el que
preguntas. Está pensado para ser lo bastante completo como para que un modelo
nunca tenga que abrir `contracts.toml` — porque un modelo que lo abre está a un
edit de borrar la regla.

`--contract PATH` apunta a un `contracts.toml` (o al directorio que lo
contiene) cuando el comando no corre desde adentro del servicio.

---

## Qué cambió estructuralmente: `contracts diff`

```bash
jfast contracts diff
jfast contracts diff --json
```

```
Architecture changes  (cuadra)

  + asesor -> comprobante         cross-module  modules/asesor/services/asesor_service.py:1
                                  reaches past modules/comprobante/public.py, the only file another module may import
  + reporte -> cartera            undeclared-dependency  modules/reporte/services/reporte_service.py:3
                                  calls modules/cartera/public.py without 'cartera' in depends_on
  - asesor -> cartera             declared in depends_on, and no import uses it  declared at contracts.toml:219
  - http -> shared                permitted, and no import uses it  declared at contracts.toml:37

Potential breaking change:
  asesor loses direct access to comprobante.ComprobanteService when that import goes -- expose
    what it needs from modules/comprobante/public.py as a function returning DTOs
```

**No es un diff de git, y el límite vale decirlo claro.** Nada de esto lee una
revisión anterior ni sabe cómo estaba el código ayer. Compara la arquitectura
que `contracts.toml` *permite* contra los imports que el código *hace*:

* `+` es una arista que el código tiene y el contrato no permite — el mismo
  hallazgo que reporta `contracts check`, dicho como cambio de arquitectura,
  con los símbolos que cruzan la arista.
* `-` es una arista que el contrato permite y ningún import usa: un permiso que
  se podría ajustar, no algo que se haya quitado. Entre módulos es una entrada
  de `depends_on` que ningún import de fachada usa.
* `~` es un permiso sobre una capa que no gobierna ningún archivo. No es un `-`
  más débil; mira abajo.
* **Potential breaking change** lista lo que cuesta hacer cumplir el contrato:
  qué nombre pierde el importador si esa arista se va, y de dónde sacarlo en su
  lugar — el `public.py` del dueño, o `shared/` si es un enum. Esa
  es la diferencia entre mover el código y borrar el import.

Solo cuentan los imports estáticos, y solo entre capas declaradas y directorios
bajo `modules/`. Es solo reporte — siempre sale con cero; al build lo hace
fallar `contracts check`.

### Cuando una capa no gobierna ningún archivo

`-` es una resta: todo lo que el contrato permite, menos todo lo que se observó
que el código hace. Significa "ningún import usa esto" solo mientras las capas
de la arista tengan archivos. Con un contrato cuyos globs no matchean nada — el
contrato layered sobre módulos hexagonales — ningún import de esas capas se
puede observar, así que **todos** los permisos que declaran caen en `-` de una
vez y el comando presenta una falla como una lista de oportunidades:

```
  - http -> shared                permitted, and no import uses it
  - http -> service               permitted, and no import uses it
  ... ocho más
```

Diez de esas, sobre un contrato que no hace cumplir nada, no son una invitación
a ajustar nada. Por eso `diff` nombra primero las capas vacías y marca sus
permisos con `~`:

```
Architecture changes  (billing)

  ! http, schemas, service govern no file in this project. Their rules apply to nothing, so what
    they permit is listed under `~` rather than as a permission you could tighten. `jfast
    contracts check` fails on this with layer-unmatched.

  - storage -> shared             permitted, and no import uses it  declared at contracts.toml:47
  ~ http -> service               'http' and 'service' govern no file  declared at contracts.toml:32
  ~ http -> shared                'http' governs no file  declared at contracts.toml:32
```

Las aristas entre capas que *sí* gobiernan archivos conservan su `-`: siguen
teniendo respuesta, y negarse a dibujar el reporte entero costaría un
diagnóstico que funciona para arreglar uno roto. Qué capas están vacías lo
decide la misma función que usa `check_coverage`, así que `diff` y `contracts
check` no pueden contradecirse en eso. En `--json` esto llega como `unsound` y
`ungoverned_layers`, aparte de `removed`.

El arreglo no está en este comando. Apunta el `paths` de la capa al layout que
los módulos realmente usan, o regenera el contrato para ese layout —
`jfast inspect` nombra el layout de cada módulo, y `jfast analyze` reporta el
mismo estado como `contract-governs-nothing`.

---

## Lo que el checker deliberadamente no hace

Es estático, basado en AST y conservador. Un checker que grita lobo consigue
un archivo de ignore en una semana, y ahí el contrato vuelve a ser decoración.

- **Los archivos que no coinciden con ninguna capa no se verifican por capa.**
  Un path se incluye *explícitamente*. Adivinar produciría ruido en cada
  script, migración y notebook.
- **Pero una capa que termina sin coincidir con nada sí se reporta.**
  `layer-unmatched`, y hace fallar el build. Una capa sin archivos es normal
  mientras el código no está escrito, así que solo es un hallazgo cuando el
  árbol *además* tiene archivos bajo los directorios que el contrato reclama y
  ninguna capa toma — que es exactamente cómo se ve, desde adentro, un contrato
  escrito para otro layout. Sin esto, un contrato que no aplica nada es
  indistinguible de código sin nada malo.
- **Solo se inspeccionan imports estáticos y llamadas directas.** `importlib`
  y las cadenas de `getattr` quedan fuera de alcance. Esto es una barandilla
  de diseño, no un sandbox.
- **`cross-module-sql` lee literales de string, no consultas.** Encuentra un
  nombre de tabla después de `FROM`, `JOIN`, `INTO`, `UPDATE` o `TABLE` en un
  string o en la parte constante de un f-string. Un nombre de tabla armado en
  tiempo de ejecución desde una variable no se ve, y tampoco una consulta de
  SQLAlchemy construida sobre el modelo de otro módulo — aunque importar ese
  modelo ya es `cross-module`.
- **El contrato se valida primero.** Dos capas reclamando un mismo path, o un
  `may_import` que nombra una capa que no existe, se reportan como errores del
  contrato — porque si no, los hallazgos son respuestas seguras a la pregunta
  equivocada.

---

## Usarlo con un agente

Pon esto en las instrucciones del agente, o apóyate en `AGENTS.md`, que ya lo
hace:

> Antes de escribir código aquí, corre `jfast contracts show --json`. Antes de
> dar el trabajo por terminado, corre `jfast contracts check`. Cuando reporte
> una violación, corre `jfast contracts explain --rule <rule> --json` antes de
> cambiar nada — y nunca edites `contracts.toml` para que un check pase.

El JSON lleva alcance, límites de capas, llamadas prohibidas, interfaces e
invariantes. Con eso alcanza para que un agente escriba código que encaja a la
primera, en vez de código que un reviewer tiene que devolver.

Y cuando igual se desvía, el check lo atrapa — que es la parte que hace a esto
distinto de escribir las mismas reglas en prosa y confiar.

---

## Comandos

```bash
jfast contracts init                    # defaults for your layout
jfast contracts init --layout screaming
jfast contracts check                   # non-zero exit on a violation
jfast contracts check --json
jfast contracts show --json             # what an agent reads first
jfast contracts render                  # CONTRACTS.md
jfast contracts waivers                 # every inline exception
jfast contracts explain <a> <b>         # why that import is refused, and what to do
jfast contracts explain --rule layer    # what a reported rule means, and where it lives
jfast contracts explain --rule orphan-subscription
jfast contracts diff                    # permitted architecture vs. the built one
```

Agrégalo al CI del servicio, al lado de los tests:

```yaml
- run: jfast contracts check
```

## Una advertencia que vale la pena decir

Un contrato atrapa el desvío estructural: una capa alcanzando para el lado
equivocado, una llamada prohibida, un directorio de tests que falta. No atrapa
un algoritmo equivocado, un mal nombre, o una regla implementada al revés.

Hace que el código generado sea *estructuralmente* limpio y hace explícitas
las reglas. No hace que el código sea correcto. El review sigue aplicando — el
contrato solo saca las discusiones que si no tendrías cada vez.
