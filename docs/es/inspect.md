# Leer un proyecto de vuelta

```bash
jfast inspect     # qué hay aquí
jfast analyze     # qué está mal
jfast graph       # qué depende de qué
jfast check       # todo lo anterior, más el resto, con un solo exit code
```

`check` es una línea de tu pipeline, no el pipeline: no corre **ningún linter,
ningún type checker ni ningún test** — ver [Qué NO chequea
`check`](#qué-no-chequea-check).

Tres comandos que responden lo que un mantenedor pregunta de verdad en el mes
seis, y que la CLI no sabía responder: era muy buena en los primeros diez
minutos de un proyecto y se quedaba muda después.

---

## El hueco que cierran

`jfast describe` responde "qué es este servicio" **construyendo la app**.
Resuelve el grafo de plugins, importa cada plugin y lee la configuración viva.
Esa respuesta es la autoritativa y tiene dos problemas.

No está disponible justo cuando la quieres. Una dependencia que falta, un error
de sintaxis, un refactor a medias: los momentos en que más necesitas saber qué
tienes enfrente son los momentos en que `describe` no puede correr.

Y no dice nada de los módulos. Genera dos y ninguno aparece en su salida:

```bash
jfast new module invoice && jfast new module order
jfast describe --json | grep -c invoice
0
```

El framework conocía el layout del módulo, las reglas de capas y el cableado, y
no tenía forma de contarte nada de eso. Así que cada agente que tocaba un
proyecto terminaba en `grep`, y cada persona también.

Estos tres leen el sistema de archivos. Un poco menos autoritativos, siempre
disponibles, y saben qué es un módulo.

---

## `jfast inspect`

```
  shop 0.1.0 (local)

  modules (2)
    invoice  layered/api   /invoices
    order    layered/api   /orders

  plugins      observability, metrics, database
  shared       2 files
  migrations   0
  contract     contracts.toml
  frontend     -

  ✓ nothing to report
```

Una pantalla. El comando para correr primero en un servicio desconocido, y el
que un agente debería correr antes de editar nada.

```bash
jfast inspect module order    # un módulo en detalle
jfast inspect --json          # lo mismo, como datos
jfast inspect --path ../shop  # desde afuera
```

`inspect module` agrega la lista de archivos, los paquetes que importa, las
tablas que declara, y si tiene tests y README.

**Nunca importa tu código.** Todo sale de parsear la fuente, así que funciona
en un proyecto cuyas dependencias no están instaladas y en uno que hoy no
arranca.

---

## `jfast analyze`

Todo lo estructuralmente mal, lo peor primero, cada cosa con el arreglo en su
propia línea:

```
  CRITICAL
    module-cycle: import cycle: invoice -> order -> invoice
      Modules in a cycle are one module with folders between them: neither can
      be extracted into a service, and a change to one breaks the other in a
      way no test covers. Keep the query in one direction, through the other
      module's public.py, and turn the other direction into an event.

  HIGH
    modules/ghost/  module-unregistered: module 'ghost' declares routes but
                    main.py never imports it
      The endpoints do not exist at runtime. Nothing fails: the tests in the
      module pass, the server starts, and the route 404s.
```

### Qué revisa

| Código | Severidad | Qué atrapa |
| --- | --- | --- |
| `module-cycle` | critical | Dos módulos que se importan entre sí. Ninguno se va a poder extraer nunca. |
| `module-unregistered` | high | Un módulo con rutas que `main.py` nunca importa. Los endpoints sencillamente no existen. |
| `route-conflict` | high | Dos routers en un mismo prefijo. Gana el que se registra primero; las rutas del otro quedan inalcanzables y FastAPI no avisa. |
| `shared-imports-module` | high | `shared/` volviendo hacia un módulo, lo que convierte el grafo de dependencias en un círculo. |
| `plugin-unknown` | high | Un plugin habilitado en `jfast.toml` que nada provee. La app se niega a arrancar. |
| `contract-governs-nothing` | high | Una capa de `contracts.toml` cuyos `paths` no matchean ningún archivo acá. Sus reglas no aplican a nada, y `contracts check` pasa sin hacer cumplir nada. |
| `module-no-migration` | medium | Un módulo cuya tabla ninguna revisión crea. Falla en la primera consulta y en ningún lado antes. |
| `cross-module-import` | medium | Un módulo importando a otro por fuera de su `public.py`. Importar `modules/<otro>/public.py` es como los módulos deben hablarse y no se reporta acá; `contracts check` decide si está declarado. |
| `code-outside-module` | low | Un `.py` en la raíz que no pertenece a nada. |

### Por qué la lista es corta

Cada check es decidible desde el texto de la fuente. Nada de esto adivina.

Es un límite deliberado y cuesta cobertura real: detección de N+1, código
muerto y dependencias sin usar son cosas que una pasada más difusa podría
reportar, y todas cosas en las que a veces se equivocaría. Un checker que
acierta nueve de cada diez veces queda silenciado después del segundo falso
positivo, y con él se van los hallazgos verdaderos. Diez checks que nadie
discute valen más que treinta que nadie corre.

`module-no-migration` es la forma que esa regla toma en la práctica. Solo se
dispara cuando **ya hay** revisiones y la tabla de ese módulo no está en
ninguna. Sin migraciones, el proyecto simplemente no llegó a ese paso, que es
como se ve uno recién generado, y recibir a un proyecto nuevo con un hallazgo es
la forma de enseñarle a la gente a ignorar la herramienta.

### Contra `contracts check`

El contrato aplica las reglas que declaraste **dentro** de un archivo: qué capa
puede importar qué, qué llamadas están prohibidas, qué no puede bloquear.
`analyze` reporta sobre la forma del proyecto **entre** archivos. Corre los
dos; fallan por razones distintas y con exit codes distintos.

`analyze` no corre `contracts check`, y `contract-governs-nothing` no es una
excepción a eso. Lee una sola cosa de `contracts.toml`: si las capas matchean
algún archivo. En qué layout están los archivos y si el contrato lo describe
son hechos sobre cómo están *acomodados* los archivos, decidibles sin abrir
ninguno: la pregunta propia de este comando. Lo que una capa permite adentro de
un archivo sigue siendo de `contracts check`, y reemitir sus hallazgos acá le
daría a un mismo reporte dos dueños, dos severidades y dos remedios, que es la
forma en que dos comandos terminan contradiciéndose.

Quedarse callado era la opción peor, y es lo que esto reemplaza. En un servicio
generado con el contrato layered y después llenado con módulos hexagonales,
`contracts check` fallaba con tres violaciones `layer-unmatched`, `jfast next`
reportaba *contracts check fails (3 violations)* — y `jfast analyze` imprimía
`✓ no findings` sobre el mismo directorio. El hallazgo se produce llamando a
`check_coverage`, la función del checker, así que los dos comandos reportan
ahora el mismo conteo y las mismas frases.

```bash
jfast analyze --fail-on critical   # solo lo peor
jfast analyze --fail-on never      # reporta, nunca falla
jfast analyze --json               # conteos y hallazgos, como datos
```

Por defecto es `--fail-on high`.

---

## `jfast graph`

```
  invoice
    └─ (no module dependencies)
  order
    └─ invoice
```

`jfast workspace graph` dibuja servicios. Este dibuja los módulos dentro de
uno, que es el grafo que decide si un módulo se va a poder extraer alguna vez:
un módulo que nadie importa es un servicio esperando a nacer, y un ciclo son
dos módulos que nunca van a ser ninguna de las dos cosas.

```bash
jfast graph --module billing        # solo sus aristas
jfast graph --format mermaid        # para pegar en un README
jfast graph --format dot            # para graphviz
jfast graph --format json
```

---

## `jfast check`

Todos los checks **estáticos** que tiene este framework, en un comando y un
solo exit code.

Todos los checks de esta página ya existían. Lo que no existía era una sola
cosa que correr, así que CI corría tres, un agente corría el que se acordaba, y
los dos que nadie cableó no corrían nunca.

No corre tu linter, tu type checker ni tus tests, y cada corrida lo dice en sus
últimas dos líneas. Leé [Qué NO chequea `check`](#qué-no-chequea-check) antes de
reemplazar algo con esto.

```
  shop

  ✓ config      pass   shop (local)
  ✓ plugins     pass   3 enabled, 20 installed
  ✗ analyze     fail   high 1
  ✗ contracts   fail   high 1
  ✓ migrations  pass   0 revisions read, no database
  ✓ deploy      pass   compose renders, 2 services

  ANALYZE  (validation failure, exit 1)
  HIGH
    modules/order/  module-unregistered: module 'order' declares routes but
                    main.py never imports it
      The endpoints do not exist at runtime. Nothing fails: the tests in the
      module pass, the server starts, and the route 404s.

  CONTRACTS  (contract violation, exit 5)
  HIGH
    modules/invoice/service.py:43  forbid-call: print() is not allowed here
      Use the structured logger; print output has no request id and no level.

  4 pass, 2 fail in 0.13s
  exit 5  (contract violation)

  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
```

```bash
jfast check                          # todo, salida para humanos
jfast check --json                   # todo, como datos
jfast check --ci                     # estricto: cualquier hallazgo falla, y también cualquier skip
jfast check --only contracts,analyze # un subconjunto
jfast check --fail-on critical       # el mismo umbral que `jfast analyze`
jfast check --multitenant-ready      # en lugar de la batería: qué rompería pasar a multitenant
jfast check --path ../shop           # desde afuera
```

### Contra `doctor` y `workspace validate`

`doctor` pregunta algo sobre **tu máquina**: si la configuración resuelve y si
cada plugin habilitado importa en este intérprete. Su respuesta cambia cuando
cambias de virtualenv y nunca cuando cambias una línea de código.

`check` pregunta algo sobre **el repositorio**: si lo que está commiteado es
consistente consigo mismo. Corre las dos preguntas de `doctor` como sus dos
primeros checks, porque una instalación rota vuelve mentira toda respuesta
posterior — pero `doctor` se queda, porque cuando estás depurando tu propia
laptop quieres la respuesta de dos segundos y no la batería entera.

`jfast workspace validate` lee un solo archivo, el grafo de recursos del
workspace, y su alcance es un workspace y no un servicio. `check` lo corre
dentro de `deploy`.

En CI va solo `check`.

### Los siete checks

Cada uno lleva el nombre del comando que ya lo corría. No hay un segundo
vocabulario, y el nombre es lo que recibe `--only`.

| Check | Qué corre | Exit code al fallar |
| --- | --- | --- |
| `config` | `JFastConfig.load`, la misma carga que hace `doctor` | `2` |
| `plugins` | `registry.discover()`, `registry.build()` y la lista de no importables | `3` |
| `analyze` | `jfast analyze`: la estructura entre archivos | `1` |
| `tenancy` | ajustes de tenant que se contradicen entre sí o con el código ([multitenancy](multitenancy.md#ajustes-que-se-contradicen)) | `1` |
| `contracts` | `jfast contracts check`: las reglas que declaraste | `5` |
| `migrations` | `jfast migration check`, en estático: sin base de datos | `4` |
| `deploy` | `workspace.validate()` y renderizar `docker-compose` | `1` |

Nada de esto levanta un contenedor, abre un socket ni habla con una base. Eso
es lo que hace al comando seguro en un hook de pre-commit, y también es su
límite: `deploy` prueba que el compose se puede generar y que es consistente
consigo mismo, no que las imágenes bajen.

### Qué NO chequea `check`

**`jfast check` no corre ningún linter, ningún formateador, ningún type checker
ni ningún test.** Nada en su salida cambia cuando cualquiera de ellos falla:

```bash
# un import sin usar, un archivo mal formateado, un str asignado a un int
# y un test que falla, todo junto
ruff check .            # 17 errors
ruff format --check .   # 4 files would be reformatted
mypy .                  # 2 errors
pytest                  # 1 failed

jfast check             # 6 pass — idéntico byte a byte al proyecto limpio
jfast check --ci        # exit 0
```

Y no es un bug que se arregle corriéndolos. `pytest` ejecuta tu código por un
tiempo no acotado contra lo que decida levantar un fixture, y `mypy` en un árbol
cuyas dependencias no están instaladas reporta imports faltantes que tu propia
configuración habría silenciado — cualquiera de los dos adentro de este comando
convierte un hook de pre-commit en un build, y un checker que fabrica hallazgos
queda silenciado en una semana. `check` responde si el **repositorio** es
consistente consigo mismo; esos cuatro responden si el **código** está bien. Dos
preguntas, dos comandos.

Así que el costo se paga a la vista. Cada corrida termina con los cuatro que te
deja a vos, pase o falle:

```
  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
```

y `--json` lleva lo mismo bajo `not_covered`, porque un script que lee
`"ok": true` no puede leer un pie de página:

```json
{
  "ok": true,
  "not_covered": [
    {"what": "lint", "catches": "unused imports, undefined names, unreachable code",
     "command": "ruff check ."},
    {"what": "formatting", "catches": "a diff nobody agreed to review",
     "command": "ruff format --check ."},
    {"what": "types", "catches": "a str where an int was declared", "command": "mypy ."},
    {"what": "tests", "catches": "whether any of it works", "command": "pytest"}
  ]
}
```

La línea que va en CI es la línea entera:

```bash
jfast check --ci && ruff check . && ruff format --check . && mypy . && pytest
```

### Un exit code para muchos checks

Seis checks, un número. Cuando fallan varios, el código sale del fallo que
invalida la mayor parte del reporte, no de la severidad más alta:

```
2 config  >  3 plugins  >  4 migrations  >  5 contracts  >  1 analyze/deploy
```

Un `jfast.toml` que no parsea vuelve conjetura a todas las demás respuestas, así
que gana. Un plugin que no importa sigue, por la misma razón. **Migrations le
gana a contracts** porque un esquema con el que el código no está de acuerdo
falla en producción en la primera query y es el resultado que no puedes dejar
pasar a un deploy; una violación de contrato, en cambio, se ve en el diff y
falla sin daño. Contract le gana a `analyze` porque un contrato es una regla que
alguien escribió, mientras que los hallazgos de `analyze` son la clase más
amplia y menos específica: van al final, para que cualquier código más
informativo le gane.

El número único es un resumen, nunca la respuesta completa. `--json` lleva
`codes` — el código de cada check que falló — para que un script no tenga que
deducir los otros cinco:

```json
{
  "ok": false,
  "exit_code": 5,
  "exit_meaning": "contract violation",
  "codes": [1, 5],
  "failed": ["analyze", "contracts"],
  "skipped": [],
  "complete": true
}
```

### Un skip no es un pass

Un check que no pudo correr se reporta como `skip`, con el motivo, en los dos
modos de salida:

```
  · contracts   skip   no contracts.toml in /tmp/shop (jfast contracts init)

  4 pass, 1 fail, 1 skip in 0.07s
  · skipped: contracts -- not a pass
```

En `--json` es `status: "skip"` con un `reason` no nulo, su nombre está en
`skipped` y **no** en `passed`, y `complete` es `false`. Una batería que
reporta verde en silencio sobre un check que no corrió es peor que no tener
batería: convierte "no miré" en "miré y estaba bien".

**Bajo `--ci`, un skip falla**, con exit `3` — error de entorno. Eso es lo que
significa un skip en CI: al runner le faltaba algo que el check necesitaba, y
el pipeline no está revisando lo que crees que revisa. En local un skip es
normal e informativo; en CI es un agujero.

`--ci --allow-skips` es la única forma de pasar, y es explícita a propósito: un
equipo que de verdad no puede proveer algo escribe el flag en el pipeline,
donde un revisor lo ve, en vez de no enterarse nunca del agujero.

Un skip solo decide el exit code cuando no falló nada más. Una corrida que
además viola su contrato sale con `5`, no con `3`: hay un defecto concreto que
reportar, y culpar al runner por él sería incorrecto.

### Cuánto cuesta

Sobre un servicio recién generado, los checks tardan **120–185 ms**, y el
proceso completo **0.65–0.9 s** de reloj: casi toda la diferencia es el arranque
y los imports del propio Python, que cualquier herramienta en un hook de
pre-commit paga igual.

Medido por check, sobre ese mismo servicio:

| Check | Costo |
| --- | --- |
| `config` | ~1 ms |
| `migrations` | ~3–12 ms |
| `contracts` | ~30 ms |
| `plugins` | ~45–85 ms |
| `analyze` | ~60–100 ms (incluye `plugins`) |
| `deploy` | ~60–90 ms (incluye `plugins`) |

`plugins` es el lento y arrastra al resto: `registry.discover()` importa cada
plugin instalado. Corre una vez por invocación y su resultado se le pasa a
`analyze` — que necesita el conjunto instalado para reportar `plugin-unknown` —
y a `deploy`, en vez de recalcularlo. Todo lo demás es parseo de AST, que escala
con la cantidad de archivos `.py` y no con el tamaño de las dependencias del
proyecto.

`--only contracts,migrations` es el subconjunto que evita el descubrimiento de
plugins por completo, en ~35–45 ms. Esa es la forma a poner en un hook de
pre-commit si la corrida completa alguna vez deja de sentirse instantánea, con
la batería completa igual en CI.

Una advertencia sobre `migrations`: sin base de datos lee **todas** las
revisiones, no solo las pendientes — cuáles están aplicadas es un dato que solo
tiene la base — y trata cada tabla como poblada, que es la lectura conservadora.
En un repositorio con historia larga de migraciones, `jfast migration check
--dsn ...` es la respuesta más acotada, y `--only` puede sacar este check de la
batería.

---

## Exit codes

Estándar en toda la CLI, documentados y con tests. Un script que tiene que leer
inglés para saber por qué falló un comando es un script que se rompe cuando
cambia la redacción.

| Código | Significa |
| --- | --- |
| `0` | éxito |
| `1` | fallo de validación: un check encontró algo mal |
| `2` | error de configuración: falta un archivo o no parsea |
| `3` | error de entorno: Docker, un contenedor, una base |
| `4` | riesgo o fallo de migración |
| `5` | violación de contrato |
| `6` | error de entrada del usuario |
| `7` | error de compatibilidad o versión |

Distinguir `1` de `5` es lo que le permite a CI decir *por qué* falló sin
parsear texto:

```bash
jfast analyze || case $? in
  1) echo "structure" ;;
  2) echo "config" ;;
esac
```

Son parte de la API pública. Un código nunca cambia de significado; un modo de
fallo nuevo recibe un número nuevo.

---

## Para un agente

```bash
jfast inspect --json          # what exists
jfast graph --format json     # what depends on what
jfast analyze --json          # what is wrong, with the fix in `why`
jfast contracts show --json   # the rules
jfast contracts check --json  # the violations
jfast check --json            # todos los checks estáticos, con un solo exit code
```

`jfast check --json` es la última llamada *de jfast* antes de reportar un cambio
como terminado: es la única que falla cuando algo no llegó a revisarse, así que
"pasó" no puede significar "no corrió". Lee `skipped` y `complete` antes de leer
`ok` — y lee `not_covered`, que nombra el linter, el formateador, el type checker
y los tests que no corrió. `"ok": true` no es "el cambio está terminado"; es "el
repositorio es consistente consigo mismo".

Cinco comandos, sin leer fuente, sin adivinar convenciones. El campo `why` de
cada hallazgo es la parte que importa: a un agente al que le das una violación
sin remedio tiende a satisfacer al checker en vez de arreglar el diseño.
