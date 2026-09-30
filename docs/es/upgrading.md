# Actualizar un proyecto

```bash
jfast upgrade --check          # qué se rompe, para ESTE proyecto
jfast upgrade --check --json   # lo mismo, para un agente o un paso de CI
```

Compara la versión del framework que el proyecto fija contra la instalada, y
reporta los cambios que hay en medio **que este proyecto puede sentir de
verdad**.

---

## Lo que no es

No es el changelog. `CHANGELOG.md` dice qué cambió; quien hace la actualización
necesita saber qué cambia *para él*, y veinte notas de release sin forma de
saber cuáles tres aplican es una lista que nadie lee dos veces.

Así que el comando no parsea el changelog — ni podría aunque quisiera. Los
cambios que rompen están declarados como datos dentro del paquete, en
`jfastframework/upgrades.py`, cada uno con un `detect` que inspecciona tu
proyecto en disco. Tres razones por las que ese archivo es la fuente y la prosa
no:

- **La prosa no es un formato de datos.** `### Breaking` es un encabezado hoy.
  Renómbralo a `### Breaking changes` y el parser reporta que nada se rompió — y
  lo reporta con toda confianza.
- **El changelog no se publica.** El wheel contiene
  `packages = ["src/jfastframework"]` y nada más, así que un framework instalado
  no tiene changelog que leer.
- **Una nota de release no es un hallazgo.** "Los timestamps son timezone-aware"
  es una frase. `ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz
  USING created_at AT TIME ZONE 'UTC'` es algo que puedes ejecutar.

---

## La regla que lo hace valer la pena

**Un cambio que este proyecto no puede sufrir no se imprime.**

No es cortesía, es todo el diseño. Una advertencia que no aplica le enseña al
lector que la salida es relleno, y la siguiente advertencia — la que importaba —
se salta junto con ella.

Por eso cada entrada lleva un `detect` que devuelve la evidencia encontrada en
*tu* árbol:

| Cambio | Se reporta solo cuando |
| --- | --- |
| `timestamps-timezone-aware` | una clase de modelo lleva `TimestampMixin` |
| `contracts-shared-import` | una capa de `contracts.toml` omite `"shared"` |
| `contracts-layout-mismatch` | los `paths` de una capa no matchean el layout de ningún módulo |
| `refresh-tokens-rejected` | `auth` está habilitado **y** `issue_tokens = true` |
| `logout-ends-one-session` | igual |
| `token-store-rotate-refresh` | alguna clase tuya define `rotate_refresh` |
| `pagination-total-optional` | algún archivo llama a `paginate` o `paginate_keyset` |
| `access-token-fam-claim` | `auth` está habilitado **y** `issue_tokens = true` |
| `refresh-grace-seconds` | igual, **y** `refresh_grace_seconds` está sin fijar |
| `request-limit-defaults` | `jfast.toml` no fija el límite por su cuenta |
| `cli-exit-codes` | siempre — ver abajo |

Un servicio que solo *valida* tokens ajenos no se ve afectado por ningún cambio
en los endpoints que los emiten, que es el caso de la mayoría de los servicios
con `auth` encendido. Nunca se le informa de ellos.

`cli-exit-codes` es la excepción, y es honesta al respecto: nada en un proyecto
dice si su pipeline se bifurca según un exit code, así que la entrada se marca
como informativa y enuncia el cambio sin condiciones en vez de adivinar.

### El mixin se resuelve por clase, no por archivo

Los nombres de tabla del remedio de `timestamps-timezone-aware` salen del
`__tablename__` de cada modelo, leído con `ast` y sin importar el módulo. Valen
tanto `__tablename__ = "invoices"` como la forma anotada
`__tablename__: str = "invoices"`; una clase que calcula su nombre en tiempo de
ejecución se reporta como *carries `TimestampMixin`, declares no
`__tablename__`* en vez de quedar afuera, y una clase marcada
`__abstract__ = True` no es dueña de ninguna tabla y se salta.

Qué clases llevan el mixin se decide **por clase**. Un archivo de modelos
suele tener tanto las tablas que mezclan los timestamps como tablas de
proyección o de vista que no, y un `ALTER` que nombra `created_at` sobre una
tabla que no lo tiene no es una advertencia:

```
ERROR:  column "created_at" does not exist
```

Eso aborta la revisión ahí mismo — después de que cada `ALTER` anterior ya tomó
`ACCESS EXCLUSIVE` y reescribió su propia tabla. Las clases base se siguen por
nombre en todo el proyecto, así que un modelo que llega al mixin a través de
una base declarada en `shared/` también se encuentra.

---

## Qué reporta

```
  0.1.0a3 → 0.1.0a4   (pinned in requirements.txt)

  ✗ timestamps-timezone-aware  [breaking, 0.1.0a4]
      TimestampMixin columns are timezone-aware. Existing tables need a migration.

      created_at and updated_at mapped to TIMESTAMP WITHOUT TIME ZONE,
      so a row serialised as 2026-08-29T20:55:15 with no offset and
      every JavaScript client read it as local time.

      in this project:
        modules/invoice/models.py  ->  invoices
          ALTER TABLE invoices
              ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
              ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';

      → fix
        Write the statements above into an Alembic revision by hand.
        The USING clause is load-bearing and autogenerate omits it.

  ✗ contracts-shared-import  [breaking, 0.1.0a4]
      ...
      in this project:
        [layers.http]  may_import = ["service", "schemas", "storage"]  ->  add "shared"

  7 apply here, 5 breaking.
```

### La cláusula `USING` no es decoración

El autogenerate de Alembic escribe la forma pelada:

```sql
ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz;
```

Eso no falla. Convierte a través del cast implícito, que lee cada valor
almacenado en el `TimeZone` **del servidor**, y desplaza en silencio la tabla
entera en cualquier servidor que no esté en UTC. La migración que imprime este
comando es la que no lo hace:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

---

## De dónde sale la versión del proyecto

Dos lugares la dicen, y se consultan en este orden:

1. `requirements.txt` — la línea `jfastframework[...]==X`. Esta gana, porque es
   sobre la que actúa `pip`: un proyecto actualizado editando esa línea y
   reinstalando está en la versión nueva, sin importar qué recuerde el resto del
   disco.
2. `.jfast-template` — el sello que deja cada scaffold, con la versión del
   framework que generó el árbol. El respaldo para un proyecto sin archivo de
   requirements.

Que no exista ninguno de los dos es un `2` (error de configuración), no un éxito
silencioso: el comando se niega a reportar sobre una versión que tuvo que
adivinar.

La comparación respeta PEP 440, así que `0.1.0a10` es más nueva que `0.1.0a9` —
que no es lo que dice la comparación de cadenas. `packaging` **no** es una
dependencia de este framework, ni directa ni transitiva, así que el orden está
implementado a mano en `upgrades.parse_version` en vez de importarse; la suite
de tests lo fija contra el `packaging` real, que sí está instalado para
desarrollo.

---

## Lo que cambia en `0.1.0a11`

Nada en esta versión impide arrancar a un servicio correcto de `0.1.0a10`. Lo
que se rompe es comportamiento que antes fallaba en silencio y ahora falla en
voz alta, más algunos ajustes que los plugins rechazan al arrancar. La
migración real de un servicio de cuatro módulos (un SaaS de comprobantes sobre
PostgreSQL, Redis, accounts y una cola) fue así, y es el orden a seguir.

**1. Mueve el pin y luego lee el reporte.** Edita `requirements.txt` a
`0.1.0a11` *después* de correr `jfast upgrade --check` con el pin viejo: el
reporte lee la versión de esa línea. Con los extras nuevos instalados, corre
`jfast check`: su séptimo check, `tenancy`, es nuevo.

**2. Cambia las tareas encoladas por nombre por un evento.** `contracts check`
ahora ve `Job(task="alerta.revisar_presupuesto")` en otro módulo como una
llamada a él (`undeclared-dependency`, y `module-cycle` si el otro lee de
vuelta). El handler puede vivir en un `worker.py` en la raíz: el prefijo
`<módulo>.` del nombre dice de quién es la tarea.

```python
# antes -- modules/comprobante/services/comprobante_service.py
await outbox.enqueue(session, Job(task="alerta.revisar_presupuesto", payload=...))

# después
await outbox.publish(session, "comprobantes", Event(type="comprobante.registrado", data=...))
```

```python
# modules/alerta/tasks.py
@subscribe("comprobante.registrado")
async def revisar_presupuesto(event: Event, session: TaskSession) -> None:
    ...  # corre como el tenant que publicó; commit al volver
```

```toml
# contracts.toml
[modules.comprobante]
publishes = ["comprobante.registrado"]
```

Sin suscriptor y sin bus de eventos, `outbox.publish` ahora lanza
`UndeliverableEvent` (un 500 que dice cómo arreglarlo) en vez de responder 201
y reintentar la fila hasta que muriera. `publish-without-receiver` encuentra
esas llamadas.

**3. Borra `worker.py`; corre `jfast worker`.** Los handlers van en
`modules/<nombre>/tasks.py` (`@task`, `@subscribe`), un `TaskSession` reemplaza
la sesión, el commit y la revisión de tenant escritos a mano, y `jfast worker`
drena al recibir SIGTERM. `jfast dev` lo arranca. Regenera los despliegues
(`jfast deploy compose`, `jfast workspace compose`, `jfast workspace k8s`):
ganan un servicio worker. Copia el bloque `[layers.tasks]` a `contracts.toml`
para que los tasks tengan capa (`contracts-tasks-layer`).

**4. Revisa los defaults que cambiaron.** Cuatro vienen encendidos y se
reportan como comportamiento: límites de inicio de sesión con `cache`
(`accounts-sign-in-rate-limit`), revocación de tokens que falla abierta cuando
Redis está caído (`revocation-fail-open`), un deadline de 1 s por comando de
Redis (`redis-command-timeout`) y errores de conexión a la base que responden
503 en vez de 500 (`database-unavailable-503`). Cada nota dice cómo conservar
el comportamiento anterior.

**5. Ajustes rechazados al arrancar.** `settings-refused-at-boot` lista los
valores que los plugins ya rechazan: `pool_size = 0`, un `session_timezone` que
no es IANA, una `visibility` de storage que no es public ni private, un
`access_key` sin su secreto, y más. Solo lee `jfast.toml`; los valores del
entorno se revisan al arrancar el servicio.

**6. Clientes de accounts.** Con `email_verification = "required"`, los
usuarios existentes quedan sin verificar hasta correr el `UPDATE` de la nota.
Con `mfa = true`, `/auth/login` puede responder un reto en vez de tokens. Un
frontend generado que lee el usuario de la respuesta del login tiene que llamar
`/auth/account` después de iniciar sesión; `jfast upgrade --check` lo encuentra
junto al servicio.

**`jfast add` y tu pin.** `jfast add <plugin>` edita `requirements.txt` y corre
pip, excepto cuando el pin no es la versión que estás usando o la instalación
es editable. Entonces imprime el comando en su lugar: instalar el pin viejo
reemplazaría el framework con el que estás trabajando.

## El que detiene un arranque en `0.1.0a9`

### Una sesión que confirma después de la respuesta

```
PluginError: these routes open a database session that would commit after the
response is sent ...
  POST /invoices -> session_dependency
```

Antes: un commit fallido -- una constraint diferida, un fallo de serialización,
una conexión que se cae en el momento equivocado -- ya se había contestado
`201`, y un cliente que leía su propia escritura enseguida podía llegar antes
que el commit. FastAPI corre el desmontaje de una dependencia con `yield`
después de la respuesta salvo que tenga scope de función, y la sesión confirma
en su desmontaje. Todo módulo que `jfast new module` generó antes de `0.1.0a9`
la conecta así.

La corrección es una línea por dependencia:

```python
# antes
def get_service(request: Request, session=Depends(session_dependency)) -> Service: ...

# después
from jfastframework.plugins.builtin.database import DbSession

def get_service(request: Request, session: DbSession) -> Service: ...
```

`ReadSession` reemplaza a `read_session_dependency` y `TenantSession` a
`tenant_session_dependency`. `Depends(session_dependency, scope="function")` es
lo mismo escrito completo. Una dependencia generadora propia que envuelva una
sesión también debe tener scope de función -- FastAPI rechaza el otro orden.

`jfast upgrade --check` lista cada línea como `session-commits-after-response`.
El framework ahora necesita FastAPI 0.121 o posterior; `pip install -U` lo
resuelve. [Transacciones](transactions.md) explica el resto.

## Exit codes

| Código | Significado |
| --- | --- |
| `0` | nada entre esas versiones afecta a este proyecto |
| `2` | no hay `jfast.toml`, o no hay pin contra el cual comparar |
| `6` | se pasó `--apply` |
| `7` | algo aplica, o el proyecto fija una versión más nueva que la instalada |

`7` es `COMPATIBILITY`: el proyecto y el framework instalado no coinciden. Pon
un deploy detrás de eso.

---

## `--apply` no existe

No es "todavía no en este build": no está planeado para este release, a
propósito.

Reescribir automáticamente los modelos, el contrato y la configuración de
alguien necesita una historia de rollback: un árbol limpio del cual partir, un
diff que revisar, una forma de volver cuando la reescritura está mal. Nada de
eso existe aquí, y una edición automática equivocada cuesta más que la manual
que ahorró. Pasar `--apply` lo dice y sale con `6`.

El reporte nombra el archivo y la línea de cada hallazgo. Haz las ediciones.

---

## Agregar un cambio al manifiesto

Cuando un release rompa algo, agrega un `Change` a `CHANGES` en
`jfastframework/upgrades.py`:

```python
Change(
    version="0.1.0a5",
    kind="breaking",              # breaking | deprecated | behaviour
    code="stable-identifier",     # what --json emits; never reuse one
    summary="One line. What broke.",
    detail="Why it broke and what the silent failure looked like.",
    detect=_something_on_disk,    # None only when nothing can decide it
    remedy="What to do, concretely.",
)
```

`detect` recibe un `jfastframework.project.Project` y devuelve las cadenas de
evidencia — nombres de tablas, nombres de capas, los valores que un setting está
por adquirir. Una lista vacía significa que el proyecto no está afectado y la
entrada no se imprime.

`Project` se lee del sistema de archivos y nunca importa el código del proyecto,
así que el reporte funciona sobre un servicio roto, a medio migrar o sin sus
dependencias instaladas. Lleva los nombres de los plugins pero no su
configuración; lee `jfast.toml` directamente cuando un cambio dependa de ella,
como hacen las entradas de `auth`.

Escribe la entrada con `detect=None` solo cuando nada en disco pueda decidir la
pregunta, y dilo en `detail`. Es la diferencia entre una nota informativa honesta
y una advertencia que la gente aprende a ignorar.

Después dale dos tests en `tests/test_upgrade_detectors.py` -- un proyecto que
marca y el más parecido que no debe marcar -- y regístralos en `AFFECTED` y
`CLEAN`. El último test de ese archivo los compara con `CHANGES`, así que una
entrada sin ellos rompe la suite.

## El smoke de actualización

`scripts/smoke_upgrade.sh` recorre el camino que recorre un usuario, en cada
cambio:

1. un virtualenv con la versión **anterior** desde PyPI;
2. un proyecto generado con ella -- un módulo en cada layout, y los plugins
   `auth`, `tenancy`, `metrics`, `rag` y `queue`;
3. el wheel de este checkout instalado encima;
4. `jfast upgrade --check --json`, cuyos códigos de cambio deben ser
   **exactamente** los de `scripts/smoke_upgrade.expected` -- uno que falta
   significa que un detector dejó de ver su caso, uno de más que empezó a ver
   uno que no está;
5. el remedio de cada código, escrito en el smoke como lo describe el `remedy`
   de la entrada;
6. la app importada y `GET /health` respondido en el mismo proceso;
7. el `pytest` del propio proyecto y `jfast check --ci`.

Lo que mantiene honesto es lo que promete esta página: que `upgrade --check`
nombra todo lo que un proyecto tiene que cambiar. Una rotura que no nombra
aparece en el paso 6 o 7 sin nada en el paso 4 -- así encontró que la plantilla
hexagonal de 0.1.0a10 no pasa su propio test (el init del paquete importa
FastAPI a través de `CreatePayload`).

```bash
PY=python3.12 scripts/smoke_upgrade.sh              # anterior = esta versión menos una
PREVIOUS=0.1.0a9 scripts/smoke_upgrade.sh           # o nómbrala
KEEP_WORK=1 scripts/smoke_upgrade.sh                # deja el proyecto para revisarlo
```

Un `Change` nuevo que aplica al proyecto generado necesita su código en el
archivo esperado y un `case` en `remedy()` del smoke; el smoke falla y lo dice
hasta que estén los dos.
