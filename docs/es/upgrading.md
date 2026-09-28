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
