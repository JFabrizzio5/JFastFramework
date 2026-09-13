# Trabajar con agentes de IA

La premisa de este framework es que un agente escribiendo código en tu
repositorio ya es normal, y que lo que lo hace sobrevivible no es un prompt
mejor — son **reglas que el agente no puede romper en silencio.**

Tres superficies, en orden creciente de cuánto ayudan:

| | Qué le da a un agente |
| --- | --- |
| `jfast ai context --json` | todo sobre este proyecto, en una sola llamada |
| `jfast next` | qué queda sin terminar, en el orden en que se puede hacer |
| `contracts.toml` | qué puede y qué no, verificado por CI |
| `AGENTS.md` + `.jfast/skills/` | cómo espera este proyecto que se trabaje |

---

## La superficie de agente generada

```bash
jfast new service billing --agent-docs
jfast init                              # asks
```

Escribe:

```
AGENTS.md                                   the rules, at the root where agents look
.jfast/skills/respect-contracts/SKILL.md    read the contract before writing
.jfast/skills/design-system/SKILL.md        only when there is a frontend
```

**Apagado por defecto.** Un proyecto al que nadie le apunta un agente no debe
archivos de agente, y cada archivo que se envía es un archivo que puede
desviarse del código que describe.

### Por qué `.jfast/skills/` y no un archivo grande

Porque el valor está en *no* cargarlo todo. Una skill declara para qué sirve y
cuándo saltarla:

```yaml
---
name: respect-contracts
description: Read this project's contract before writing code in it, and verify
  the code against it before calling the work done.
when_to_use: Always, before any code change in billing.
when_not_to_use: Answering a question about the project without changing it.
---
```

Un agente lee el front matter de cada una, elige la que corresponde a la tarea,
y carga solo esa. Un archivo monolítico gasta el presupuesto de contexto en
reglas que no aplican al cambio en curso — y duplica `AGENTS.md`, que entonces
se desincroniza.

### Para qué es cada archivo

**`AGENTS.md`** — las reglas que aplican a todo cambio: la forma a nivel de
servicio, las cinco reglas verificadas, qué no hacer, y los comandos. Corto a
propósito.

No dice nada sobre los archivos que hay *dentro* de un módulo, y es a
propósito. Se escribe al generar el servicio, antes de que exista un módulo, y
un mismo servicio puede tener módulos en los cuatro layouts. Nombrar
`router.py` ahí era una promesa que se cumplía en uno de los cuatro. En su
lugar nombra las dos cosas que se escriben junto al código y por eso siempre
son ciertas: `[modules.<name>]` en `jfast.toml` para el layout, y
`modules/<name>/README.md` para el mapa de archivos de ese layout.

**Una skill** — el procedimiento para un tipo de tarea: precondiciones, pasos
con los comandos exactos, cómo verificar, y los errores que la gente comete de
verdad.

---

## Las reglas de las que un agente no se puede desviar

No son consejos. `jfast contracts check` rompe el build:

```
modules/payment/<file>:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

`archivo:línea`, la regla, qué pasó, y **qué hacer al respecto**. Esa segunda
línea importa más de lo que parece: un agente al que le das una violación sin
remedio tiende a satisfacer al checker en vez de arreglar el diseño — borrando
el import, copiando el código, o apagando la regla. Nombrar el destino elimina
la ambigüedad.

La lista completa está en [Contratos](contracts.md). Las que más seguido
atrapan código generado:

| Regla | Por qué la pisa un agente |
| --- | --- |
| La capa HTTP no puede importar `sqlalchemy` | Consultar desde el handler es el camino más corto a un endpoint que anda |
| Los módulos no se importan entre sí | Reusar el modelo del vecino es más fácil que moverlo |
| `shared/` no puede importar un módulo | Arreglar lo anterior importando al revés |
| Nada bloqueante en `async def` | `time.sleep` y `requests` es lo que usan casi todos los ejemplos |

---

## Exime una línea, no borres la regla

```python
from modules.invoice.enums import Status  # contracts: allow migrating to shared
```

La exención va en una línea, con una razón, y `jfast contracts waivers` las
lista todas. Apagar la regla en `contracts.toml` la quita para todo el mundo,
en silencio, y la próxima violación no se reporta — que es cómo un contrato
deja de significar algo.

---

## Una sola llamada antes de la primera edición

```bash
jfast ai context --json
```

Todo sobre **este** proyecto en una sola respuesta: cada módulo y su forma, qué
importa realmente `main.py`, el grafo de imports entre módulos, el contrato, qué
dicen ahora mismo `analyze` y `contracts check`, qué queda sin terminar, y los
comandos que devuelven lo que se dejó afuera.

Es una composición, no una reimplementación — `inspect`, `analyze`, `graph`,
`contracts` y `next` en un solo payload, así que no puede contradecir al comando
que te dice que ejecutes. Nunca importa el proyecto, así que sigue respondiendo
en un servicio al que le faltan dependencias o cuyo código no parsea.

### Cuánto pesa

**No hay un número único, y publicar uno solo lo volvía equivocado para todo
proyecto que no fuera aquel donde se midió.** El payload son hechos sobre *tu*
servicio, así que escala con tu servicio. Medido sobre servicios generados —
`jfast new service shop --with database` y después `jfast new module` N veces:

| módulos | `--json` | `--json --brief` |
| --- | --- | --- |
| 1 | 8,298 | 2,789 |
| 3 | 9,896 | 4,073 |
| 5 | 11,508 | 5,369 |

Un servicio recién generado es el piso, porque todavía no hay nada mal en él. El
mismo servicio de cinco módulos después de trabajarlo un rato — dos módulos que
`main.py` nunca levantó, un archivo fuera de todo módulo, dos violaciones de
contrato — mide **14,837 bytes (~3,700 tokens) completo y 6,149 (~1,500) con
`--brief`**. Aproximadamente +800 bytes por módulo; el resto es `next` y `checks`
creciendo con lo que realmente está pendiente.

Dónde se va el payload completo en ese servicio de cinco módulos, en bytes:

```
next      3,052   contract  2,300   commands 1,576   checks  1,562
modules   1,499   omitted     921   project    135   plugins   130
```

Las cifras de tokens son bytes ÷ 4 — la estimación gruesa habitual, no un
tokenizer.

**`jfast ai context --size` es la respuesta para tu proyecto**, y es el comando
que ejecutas antes de decidir si vas a llamar al otro dentro de un bucle. La
tabla de arriba es una escala, no un presupuesto: no planifiques una ventana de
contexto contra ella.

### Qué deja afuera a propósito

La implementación obvia de "todo lo que un modelo necesita" concatena `docs/`.
Está mal por dos motivos distintos:

* **`docs/` no viaja en el wheel.** `pyproject.toml` publica
  `packages = ["src/jfastframework"]`, así que un proyecto creado por alguien que
  ejecutó `pip install jfastframework` no tiene nada de eso en disco. Un comando
  que lo lea funciona en este repositorio y en ningún otro lado.
* **Son 53 páginas, 555 KB, unos 139k tokens** — sesenta veces el tamaño de la
  respuesta, para un manual que no dice nada sobre *tus* módulos.

Así que el payload lleva hechos sobre el proyecto, y nombra cada hueco bajo
`omitted` junto con el comando que lo cierra:

| Qué se deja afuera | Cómo obtenerlo |
| --- | --- |
| La documentación | <https://jfabrizzio5.github.io/JFastFramework/latest/> |
| El contenido de los archivos | abre los que nombra — nunca cita código fuente |
| El listado de archivos por módulo | `jfast ai context --module <name>` |
| Settings resueltos, plugin graph vivo, tabla de rutas | `jfast describe --json` (importa la app) |
| Qué le hace cada revisión sin aplicar a una base con filas | `jfast migration check --json`, `jfast migration plan` |
| Findings o violaciones más allá de los primeros 20 | `jfast analyze --json`, `jfast contracts check --json` |
| Historia | `git log` |

Nombrar los huecos importa más de lo que parece. Un agente al que le pasas una
respuesta parcial sin costura la trata como completa, y después escribe código
contra un archivo que nunca vio.

### Acotar

```bash
jfast ai context --json --module invoice   # un módulo, con su lista de archivos
jfast ai context --json --brief            # la forma, sin el detalle
jfast ai context --size                    # cuánto cuestan los dos de arriba
```

`--module` acota los módulos, los findings y las violaciones. **No** acota
`next`, a propósito: preguntaste por un módulo, y el paso que te está bloqueando
puede estar en otro.

`--brief` descarta las reglas del contrato (conserva su alcance, los nombres de
layers y los invariants), las listas de findings y violaciones (conserva los
conteos), `shared/`, y la prosa de `commands` y `omitted`.

### Los comandos a los que apunta

Cada entrada de `commands` es un punto de entrada legible por máquina, con qué
devuelve y qué te ahorra leer:

```bash
jfast inspect --json           # modules, layouts, routes, wiring, tables
jfast analyze --json           # structural findings, each with a remedy
jfast graph --format json      # module-to-module import edges
jfast contracts show --json    # layers, forbidden calls, interfaces, invariants
jfast contracts check --json   # violations, with file, line and rule
jfast next --json              # what is unfinished, in order
jfast describe --json          # resolved settings and plugin graph
```

Todos ellos menos `describe` leen el sistema de archivos sin importar el
proyecto.

---

## `jfast next` — qué queda sin terminar

El mismo motor que `jfast analyze`, dado vuelta. `analyze` dice qué está mal;
`next` dice qué hacer al respecto, en el orden en que se puede hacer:

```
  next  shop

   1. module 'ghost' declares routes but main.py never imports it
      └─ edit main.py between the [jfast:imports] and [jfast:routers] markers
   2. 'helpers.py' belongs to no module                      move it into a module, or into shared/
   3. ghosts has no revision (this project has none)         alembic revision --autogenerate
   4. invoices has no revision (this project has none)       alembic revision --autogenerate
   5. contracts check fails (2 violations)                   jfast contracts check
   6. modules/order has no tests                             add modules/order/tests/
   7. contracts.toml still has its generated placeholders    edit contracts.toml
   8. modules/payment has no README                          add modules/payment/README.md

  wire -> shape -> persist -> verify -> cover -> document   (dependency order, not severity)
```

Esta es la respuesta a "el agente se saltó un paso".

### El orden es el punto

Los pasos se ordenan por **etapa**, y una etapa es precondición de las que vienen
después — nunca por qué tan grave es el finding:

| Etapa | Nada posterior vale la pena hasta que |
| --- | --- |
| `boot` | el servicio arranque — un plugin habilitado que nadie provee lo impide |
| `scaffold` | exista un módulo que cablear, migrar o testear |
| `wire` | `main.py` lo importe: los tests de un módulo sin cablear pasan mientras sus rutas dan 404 |
| `shape` | el código haya dejado de moverse entre archivos |
| `persist` | existan las tablas que quedaron tras acomodar el código |
| `verify` | el contrato se haya verificado contra dónde terminaron los archivos |
| `cover` | el código bajo el test esté cableado, ubicado y respaldado por una tabla |
| `document` | — al final: describe lo que las etapas de arriba dejaron resuelto |

La severidad daría un orden distinto y peor. En el listado de arriba,
`'helpers.py' belongs to no module` es `low` y `ghosts has no revision` es
`medium`, y aun así el archivo suelto va primero: moverlo cambia qué tablas
declara el proyecto, así que una revisión generada antes de moverlo es una que
regeneras después. Testear un módulo sin cablear es el mismo error en versión más
ruidosa — el test pasa, y el endpoint da 404.

`jfast next --json` lleva el `stage` y su `rank` numérico en cada paso, más un
bloque `stages` que explicita de qué es precondición cada uno.

### Un hecho, un paso

Un contrato escrito para otro layout lo reportan dos comandos a la vez:
`analyze` emite un `contract-governs-nothing` por capa vacía, y `contracts
check` emite un `layer-unmatched` por esas mismas capas. Cuatro capas vacías
llegaban entonces como cuatro pasos más un resumen
`contracts check fails (4 violations)` de esos mismos cuatro — cinco líneas
sobre un archivo, archivadas bajo `shape` como si algo tuviera que moverse, y
cada una con `jfast analyze  # contract-governs-nothing`, que reimprime lo que
ya estás mirando.

Ahora es un solo paso, en `verify`, y nombra el comando que lo resuelve:

```
   2. contracts.toml governs nothing: 4 layers match no file here
      └─ jfast contracts init --layout hexagonal --force
```

El layout sale de lo que `jfast.toml` registra para los módulos. Si no están
todos en el mismo layout, el hueco queda como `<layout>`: `--force` pisa el
contrato y una adivinanza no vale ese riesgo. Y una violación que *no* sea
`layer-unmatched` sigue teniendo su propio paso `contracts check fails (n)` —
sacar el duplicado no puede sacar el resto.

### Cuando no queda nada

```
  ✓ shop: nothing outstanding

  3 modules, every one registered, tested and documented.
  2 revisions cover every declared table.
  contracts.toml passes, and its placeholders are filled in.
```

Enunciado como la lista de lo que se verificó y no como felicitación, porque una
herramienta que siempre encuentra algo que hacer entrena a la gente a ignorarla,
y una que dice "todo bien" sin decir qué miró no es mejor.

`jfast next` siempre sale con 0. Es una pregunta, no una compuerta — la compuerta
es `jfast analyze --fail-on high`.

### Sobre un servicio recién generado

No se queda callado, y todo lo que dice es cierto. `jfast new service` escribe
tres modelos con `__tablename__` y ninguna revisión, y un `contracts.toml` cuyos
`owns` y `does_not_own` siguen en `TODO`:

```
   1. invoices has no revision (this project has none)   alembic revision --autogenerate
   2. orders has no revision (this project has none)     alembic revision --autogenerate
   3. payments has no revision (this project has none)   alembic revision --autogenerate
   4. contracts.toml still has its generated placeholders: owns, does_not_own, invariants
      └─ edit contracts.toml
```

**No** afirma que los módulos estén sin cablear o sin tests — el generador los
cableó y los testeó, y decir lo contrario es la falla que hace que la gente deje
de leer la salida.

Este es además el único punto donde `next` reporta algo que `analyze` no. `analyze`
se queda callado sobre migraciones cuando un proyecto no tiene ninguna, para no
recibir a un servicio nuevo con un finding. `next` existe para nombrar el paso
siguiente al que acabas de dar, y en ese proyecto el paso es la primera revisión.

---

## Lo que esto no resuelve

Un agente que sigue todas las reglas igual puede construir lo equivocado. Los
contratos restringen *estructura*, no intención: nada aquí se da cuenta de que
la funcionalidad no era la que pediste, de que el test afirma el bug, o de que
una regla que escribiste en enero está mal en junio.

Lo que sí compras es más angosto y vale igual: el código no se degrada mientras
no estás mirando, y un review puede ser sobre si la funcionalidad está bien en
vez de sobre dónde quedó el archivo.

---

## La superficie de agente del propio framework

Este repositorio lo practica: `AGENTS.md` en la raíz y siete skills bajo
`.jfast/skills/`, cubriendo creación de módulos, autoría de plugins, contratos,
el frontend y el sistema de diseño. [Skills para agentes](skills.md) cubre cómo
escribir una.
