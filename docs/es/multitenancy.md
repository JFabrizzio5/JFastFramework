# Multi-tenancy

Un solo deploy, muchos clientes, cada uno viendo solo sus propios datos.

El plugin responde una sola pregunta — **¿para qué tenant es este request?** —
y pone la respuesta en `request.state.tenant_id`, en cada línea de log, y en la
clase base de los repositorios. Lo que *no* hace es imponer el aislamiento. Esa
distinción importa y está explicada al final de esta página.

```toml
[plugins]
enabled = ["observability", "auth", "tenancy"]

[plugin.tenancy]
sources = ["token", "subdomain"]
base_domain = "app.example.com"
```

## Las fuentes son un orden de confianza

La lista se prueba en orden y gana la primera que acierta. Ese orden es el
diseño:

| Fuente | Controlado por | Confianza |
| --- | --- | --- |
| `token` | tu identity provider, criptográficamente | alta |
| `user` | el mismo token firmado: el usuario *es* el tenant | alta |
| `subdomain` | tu DNS y TLS | media |
| `path` | la URL | baja |
| `header` | quien haya mandado el request | **ninguna** |

`header` existe porque es realmente útil en desarrollo y en los tests. No está
en la lista por defecto, y activarlo en producción escribe un warning, porque
`X-Tenant-ID: acme` está a un `curl` de distancia de los datos de otro tenant.

Un claim firmado siempre le gana al hostname. Alguien que apunta `acme.` a tu
IP no se convirtió en Acme; alguien con un token que tu identity provider firmó
para Acme, sí.

## Cada cuenta es su propio tenant: la fuente `user`

La mayoría de los SaaS arrancan sin organizaciones: una persona se registra y
lo que sube es suyo. No hay un claim `tenant_id` que leer, e inventar una tabla
de tenants con una fila por usuario es ceremonia. La fuente `user` convierte el
id del usuario con sesión -- el `sub` del token -- en el tenant:

```toml
[plugins]
enabled = ["observability", "database", "auth", "accounts", "tenancy"]

[plugin.tenancy]
sources = ["token", "user"]
```

Todo lo que sigue funciona sin cambios: `BaseRepository` filtra por él, está en
cada línea de log, el [row-level security](#aislamiento-que-impone-la-base-row-level-security)
lo lee, el [store de `rag`](rag.md) se limita a él y el [presupuesto de `llm`](llm.md)
se lo cobra.

Pon `user` **después** de `token`. El día que un usuario entra a una
organización y su token empieza a traer un claim `tenant_id`, el claim gana y
pasa a los datos de la organización sin cambiar código. En el orden inverso se
quedaría en su espacio personal para siempre.

El id de usuario lo elige el identity provider, no tú -- un UUID, un id
hexadecimal, `auth0|abc123` --, así que se valida con menos rigidez que el slug
de un subdominio, pero igual rechaza cualquier cosa que se pueda leer como ruta
o como SQL. Antes de iniciar sesión no hay usuario, así que el login, el
registro y los health checks no resuelven tenant, que es lo correcto.

## Leer el tenant en una ruta

```python
from fastapi import Depends
from jfastframework.plugins.builtin.tenancy import current_tenant

@router.get("/invoices")
async def invoices(tenant: str = Depends(current_tenant), session: DbSession = ...):
    return await InvoiceService(InvoiceRepository(session, tenant_id=tenant)).list()
```

`current_tenant` regresa lo que el plugin resolvió. Sin nadie con sesión
responde **401** -- un token vencido tiene que hacer que el cliente refresque, y
los clientes refrescan ante un 401, no ante un 403 --, y con una sesión que las
fuentes no pudieron limitar a un tenant, **403**. No lee nada más -- ni un header, ni un campo del body.
En un servicio sin el plugin tenancy cae al claim `tenant_id` del token, así que
un servicio que solo usa `auth` sigue funcionando.

Prefiérelo a `getattr(request.state, "tenant_id", None)`: un `None` que llega a
un repositorio significa "sin filtro de tenant", y la dependencia lo convierte
en 403 antes de que llegue.

## Subdominios

`acme.app.example.com` con `base_domain = "app.example.com"` resuelve a
`acme`. Las reglas, todas deliberadas:

- solo la etiqueta más a la izquierda, y solo una — `a.b.app.example.com` es un
  error, no un tenant llamado `a.b`;
- el dominio base pelado no es un tenant;
- `www`, `api`, `app`, `admin`, `static`, `cdn`, `mail` y compañía están
  reservados y nunca resuelven a un tenant;
- la etiqueta tiene que cumplir `[a-z0-9][a-z0-9-]{0,62}` — el slug termina en
  hostnames, campos de log y parámetros SQL, y tiene que ser seguro en los tres.

`base_domain` es obligatorio cuando `subdomain` es una fuente. Sin eso,
cualquier hostname parece un tenant, así que el plugin se niega a arrancar en
vez de resolver cualquier cosa.

### Caddy

```bash
jfast workspace caddy --hostname app.example.com --production --wildcard-tenants
```

Eso emite un bloque de sitio `app.example.com, *.app.example.com` con TLS
on-demand, más el endpoint `ask` global que lo controla:

```
{
	on_demand_tls {
		ask http://api:8000/internal/tenant-exists
		interval 2m
		burst 5
	}
}
```

**El endpoint `ask` no es opcional.** Caddy no puede obtener un certificado
wildcard a partir de un *match* wildcard, así que emite uno por hostname la
primera vez que lo ve. Sin `ask`, cualquiera que apunte un registro DNS a tu
servidor puede hacer que pidas certificados para él hasta que Let's Encrypt le
aplique rate-limit a tu dominio.

Lo implementas tú. Responde `200` si el tenant existe y cualquier otra cosa si
no:

```python
@router.get("/internal/tenant-exists")
async def tenant_exists(domain: str) -> Response:
    slug = domain.split(".", 1)[0]
    if await tenants.exists(slug):
        return Response(status_code=200)
    return Response(status_code=404)
```

Mantenlo fuera del router público, y hazlo barato — corre en cada hostname
nuevo que ve Caddy, incluidos los que te están sondeando.

También necesitas un registro DNS wildcard (`*.app.example.com`) apuntando a la
misma dirección.

## Exigir un tenant

```toml
[plugin.tenancy]
require_tenant = true
```

Cualquier request que no resuelva a ningún tenant recibe un `403` en
problem+json. Los health checks, las métricas, `/docs` y `/openapi.json` quedan
exentos — un readiness probe no tiene tenant y no debe fallar.

## Una base de datos por tenant

Una columna por fila es lo predeterminado y la respuesta correcta para casi
todos los servicios. Una base de datos por tenant es la respuesta cuando el
aislamiento tiene que ser físico: un cliente regulado, un restore que no puede
tocar a nadie más, un tenant con un volumen de datos propio.

```toml
[plugin.database]
tenant_dsn_env_template = "JFAST_DB_DSN_{tenant}"
tenant_dsn_template = "postgresql+asyncpg://app:pw@db:5432/{tenant}"
tenant_max_engines = 25
tenant_pool_size = 2
tenant_max_overflow = 2
```

```python
from jfastframework.plugins.builtin.database import TenantSession

@router.get("/invoices")
async def list_invoices(session: TenantSession):
    ...
```

El tenant sale de `request.state.tenant_id`, así que esto necesita el plugin
`tenancy`. Un request que no resuelve a ningún tenant lanza un error en lugar
de adivinar a qué base de datos apuntar.

### La trampa: explosión de pools

Un dict de engines indexado por tenant es la implementación obvia, y tumba a
PostgreSQL. 200 tenants con `pool_size = 10` son 2000 conexiones contra un
servidor cuyo `max_connections` por defecto es 100. Nada en ese código se ve
mal; simplemente se queda sin un recurso que nadie contó.

Por eso el mapa es un **LRU acotado** y la cota es un número que puedes leer:

```
tenant_max_engines × (tenant_pool_size + tenant_max_overflow) = 25 × 4 = 100
```

`jfast describe --json` lo imprime como `tenant_max_connections`. Dimensiónalo
contra el `max_connections` de tu servidor, dividido entre la cantidad de
procesos — un contenedor con cuatro workers de uvicorn abre cuatro de estos
mapas, no uno.

Los pools por tenant son chicos a propósito. Un tenant es una porción de tu
tráfico, no todo, y diez conexiones ociosas por tenant es donde la aritmética
se rompe.

### Qué pasa cuando un engine se desaloja a mitad de un request

El desalojo nunca cierra un engine que un request todavía está usando. La
entrada desalojada sale del mapa de inmediato — así nada nuevo la toma — y se
cierra cuando se libera el último lease. Cerrarla en el momento del desalojo
cortaría la conexión debajo de una query en curso, que aparece como un
`InterfaceError` aleatorio justo en los tenants más activos.

Dos consecuencias que conviene conocer:

- **Se desaloja el engine menos usado recientemente que no tenga requests
  activos.** Un tenant ocupado nunca es la víctima de uno tranquilo que llega.
- **Un mapa lleno de engines ocupados rechaza.** Cuando todos los engines están
  en uso y llega un tenant nuevo, se lanza `TenantPoolExhausted` en vez de
  abrir el engine número `max_engines + 1`. Pasarse de la cota bajo carga es la
  tormenta de conexiones que la cota existe para evitar, y un 503 se recupera
  de una forma en que una base de datos caída no. Si lo ves,
  `tenant_max_engines` está por debajo de tu conjunto de tenants concurrentes.

Desalojar un tenant a mano — tras un cambio de plan, una migración, una baja —
usa el mismo mecanismo:

```python
databases = ctx.require("db.databases")
await databases.tenants.evict("acme")
```

### Resolver el DSN

Primero se intenta `tenant_dsn_env_template` (`JFAST_DB_DSN_ACME`), después
`tenant_dsn_template`. Ninguno le sirve a un servicio que guarda sus tenants en
una tabla de control, así que puedes pasar un resolver:

```python
databases.tenants.set_resolver(lambda tenant: catalogue[tenant])
```

Un tenant que no se puede resolver lanza un `PluginError` que nombra ambos
settings, no un `KeyError` desde adentro de un pool.

## El orden, y por qué funciona la fuente `token`

El middleware corre **en la capa más interna**, después de auth. Esto no es
accesorio: el `add_middleware` de Starlette pone el middleware en la capa más
externa, lo que correría tenancy *antes* de auth y dejaría el claim firmado
ilegible, porque a esa altura todavía no existe ningún principal. El plugin en
cambio lo agrega al final, así que toda fuente — incluido el token — está
disponible cuando resuelve.

## Aislamiento que impone la base: row-level security

El tenant resuelto llega a `BaseRepository`, así que una query que se olvida de
filtrar igual queda filtrada por el repositorio. Eso es una convención: SQL
crudo, un join a través de una tabla sin scope o un bug en un método de
repositorio leen las filas de todos los tenants. Row-level security mueve la
regla a PostgreSQL, donde una query que olvida el filtro no recibe filas en vez
de recibir las de otro.

**1. Pon cada tabla de tenant bajo una política**, en una migración:

```python
from jfastframework.db.rls import enable_tenant_rls, disable_tenant_rls

def upgrade() -> None:
    enable_tenant_rls(op, "invoices")

def downgrade() -> None:
    disable_tenant_rls(op, "invoices")
```

**2. Actívalo**, y cada transacción le dice a PostgreSQL su tenant -- el de la
request, o el de un job de la cola, que el worker restaura:

```toml
[plugin.database]
rls = true
```

Es `set_config('jfast.tenant_id', ..., true)` al empezar cada transacción: local
a la transacción, así que una conexión del pool nunca lleva un tenant a la
siguiente request, y funciona detrás de PgBouncer en modo transacción --
verificado, no supuesto: ver [Detrás de PgBouncer](#detrás-de-pgbouncer).

**3. Conéctate con un rol al que apliquen las políticas.** Un superusuario, o un
rol con `BYPASSRLS`, ignora toda política -- y el compose generado se conecta
como el superusuario de la base. Dale al servicio un rol propio:

```sql
CREATE ROLE app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app;
```

Corre las migraciones como el dueño y el servicio como `app`. Con `rls = true`
el plugin de base de datos revisa el rol al arrancar: en producción no arranca
con un superusuario o un rol `BYPASSRLS`; en cualquier otro entorno avisa.

Lo que eso consigue, cada punto verificado contra PostgreSQL en
`tests/test_rls.py`:

| | |
| --- | --- |
| `SELECT * FROM invoices`, sin ningún `WHERE` | solo las filas de este tenant |
| Una transacción sin tenant | ninguna fila, y ninguna escritura |
| `INSERT` con el id de otro tenant | lo rechaza la política |
| La siguiente transacción en la misma conexión | empieza sin tenant |

El trabajo que por definición abarca a todos los tenants -- un reporte, una
corrección de datos -- lo dice: crea la política de esa tabla con
`allow_bypass=True` y corre el trabajo dentro de `with bypass_rls():`. Las tablas
sin esa opción siguen cerradas incluso ahí.

El filtro del repositorio se queda: es lo que hace que las queries usen el
índice, y es la primera línea. Row-level security es la que aguanta cuando la
primera falla.

### Más que el tenant: `transaction_setting`

A veces el tenant no es toda la regla. Dentro de un tenant, un usuario puede ver
solo algunas de sus empresas, sucursales o almacenes -- y ese valor tiene que
llegar a PostgreSQL igual que el tenant, por transacción, o una conexión del pool
lleva las empresas de un usuario al request de otro.

Registra una función que regrese el valor para el request o job actual, y cada
sesión con tenant lo pone junto al tenant:

```python
from jfastframework.auth import current_principal
from jfastframework.db.rls import transaction_setting

@transaction_setting("app.companies")
def companies() -> str | None:
    principal = current_principal()
    if principal is None:
        return None
    return "{" + ",".join(principal.claims.get("companies", [])) + "}"
```

Después escribe tú la policy, con los valores que necesita, en una migración:

```python
from jfastframework.db.rls import disable_rls_policy, enable_rls_policy

def upgrade() -> None:
    enable_rls_policy(
        op,
        "invoices",
        predicate="tenant_id = current_setting('jfast.tenant_id', true) "
        "AND company = ANY(current_setting('app.companies', true)::text[])",
    )

def downgrade() -> None:
    disable_rls_policy(op, "invoices")
```

Las reglas que respeta:

- **El nombre se valida.** `prefijo.nombre`, en minúsculas, porque va dentro de
  `set_config`. `jfast.tenant_id` y `jfast.rls_bypass` son del framework y se
  rechazan.
- **`None` significa cero filas.** El valor queda sin poner en esa transacción,
  la policy lee NULL y nada coincide -- igual que una transacción sin tenant no
  ve nada.
- **Regístralo al importar**, junto a las policies que lo leen. El registro es
  de todo el proceso.
- **El predicado es SQL que escribe el autor de la migración**, sin escapar:
  nunca lo armes con datos del request. Los valores le llegan por
  `current_setting`, que sí va escapado.

`tests/test_rls.py` corre esta forma contra PostgreSQL: un tenant con dos
empresas, un usuario que ve una, luego las dos, luego ninguna.

Esto es lo que puede usar un servicio que abría su propia sesión solo para poner
un segundo valor -- ve [`@transactional`](transactions.md) para los que todavía
necesitan la suya.

## Las capas, y qué atrapa cada una

El aislamiento no es una función. Es una pila, y cada capa está ahí por el bug
que se le escapó a la de arriba:

| Capa | Quién lo hace | Qué atrapa |
| --- | --- | --- |
| Resolución | este plugin, primero de fuentes firmadas | un tenant elegido por quien mandó el request |
| La ruta | `Depends(current_tenant)` | un handler que corre sin tenant |
| El repositorio | `BaseRepository(tenant_id=...)` | una consulta que olvidó `WHERE tenant_id` |
| La base de datos | row-level security, `rls = true` | SQL crudo, un join mal hecho, un bug en un repositorio |
| Entre módulos | el `public.py` de cada módulo recibe `tenant_id` explícito ([módulos](modules.md)) | un módulo leyendo tablas de otro con su propia idea del tenant |
| Trabajo en segundo plano | los jobs llevan el tenant; el worker lo restaura | un job que corre como "nadie" y lo ve todo |
| Recuperación | el store de `rag` rechaza llamadas sin tenant ([RAG](rag.md)) | una búsqueda en los documentos de todos los clientes |
| Gasto | `tenant_budget_usd` en el [plugin `llm`](llm.md) | un tenant gastándose el presupuesto de IA de todos |
| Configuración | el check `tenancy` de `jfast check` | dos de los ajustes de arriba contradiciéndose |
| El cambio | `jfast check --multitenant-ready` | código que todavía supone un solo cliente, antes de que llegue el segundo |

Actívalas de arriba hacia abajo. Las tres primeras no cuestan nada y vienen con
el framework; row-level security es un setting, una migración y un rol de base
de datos; el resto se activa en cuanto el plugin lo está.

Lo que ninguna atrapa: **un id de tenant equivocado pero bien formado.** Si tu
propio código asigna un usuario a la organización equivocada, cada capa va a
hacer cumplir fielmente la respuesta equivocada. Ese mapeo -- dónde vive, quién
lo puede cambiar -- merece la revisión más cuidadosa del servicio.

## Pasar a multitenant después

La mayoría de los servicios empieza con un cliente, y está bien: tenancy que no
necesitas es configuración que tienes que mantener consistente. Lo que abarata
el cambio posterior se decide el primer día y no cuesta nada: **conserva la
columna `tenant_id`**. Toda entidad generada lleva `TenantMixin` (un
`tenant_id` nullable) y llaves únicas que consideran el tenant aunque el
servicio sea de un solo cliente, así que pasar a multitenant es rellenar datos,
no reescribir el esquema.

Lo demás son tres herramientas, en el orden en que se usan.

### Ajustes que se contradicen

El aislamiento se configura pieza por pieza, y cada pieza es válida por sí
sola. Las fallas están entre ellas, y ninguna impide que el servicio arranque.
El check `tenancy` de `jfast check` las lee juntas:

| Código | Severidad | Qué está mal | El arreglo que nombra |
| --- | --- | --- | --- |
| `tenancy-rag-scoped-without-tenancy` | medium | `[plugin.rag] tenant_scoped` es true (el default) y el plugin tenancy está apagado: toda llamada a rag necesita un tenant que nada resuelve | `tenant_scoped = false` para un cliente; `jfast tenancy enable` para varios |
| `tenancy-budget-without-tenancy` | medium | `tenant_budget_usd > 0` sin tenancy: solo aplica el tope global | quitarlo y dimensionar `budget_usd`, o activar tenancy |
| `tenancy-rls-without-tenancy` | high (medium si un claim `tenant_id` de auth lo puede poner) | `rls = true` y nada resuelve un tenant: toda tabla con política se lee vacía | `rls = false`, o activar tenancy |
| `tenancy-policies-without-rls` | high | una revisión llama `enable_tenant_rls` y `rls = false`: ninguna sesión pone el tenant, así que esas tablas se leen vacías para el rol del propio servicio | `rls = true`, o quitar la política |
| `tenancy-current-tenant-without-source` | high | el código depende de `current_tenant` y no hay ni plugin tenancy ni claim de auth: todo request recibe 401/403 | `require_auth` para un cliente; activar tenancy para varios |
| `tenancy-source-unresolvable` | high | una fuente que aquí nunca puede responder: `subdomain` sin `base_domain`, `token`/`user` sin el plugin auth | poner el dominio base, activar auth, o cambiar las fuentes |

Lee los ajustes de los plugins ya construidos cuando el grafo resuelve, así que
lo que se juzga es un override del entorno (`JFAST_RAG_TENANT_SCOPED=false`).

### Qué rompería el cambio: `jfast check --multitenant-ready`

Un servicio de un solo cliente tiene razón en suponer un solo cliente, y lo
hace en lugares que nada marca. Esto los lista con archivo y línea, como `jfast
upgrade --check` lista lo que rompe una actualización:

```
  shop: what a switch to multitenant would break
  tenant tables: customers, invoices

  ✗ modules/invoice/api/routes.py:26  factory-without-tenant  [high]
      get_service() opens a database session with no tenant dependency;
      used by create_invoice(), delete_invoice(), get_invoice(),
      list_invoice() and 1 more
      → fix
        The generated factory reads `getattr(request.state,
        "tenant_id", None)`, and `None` builds a repository with no
        tenant filter. Take the tenant as a dependency ...
```

| Regla | Severidad | Busca |
| --- | --- | --- |
| `tenant-none-literal` | high | una llamada que pasa `tenant_id=None`: un repositorio, una fachada, `rag`, `llm` |
| `facade-tenant-optional` | high | una fachada de módulo (`modules/<nombre>/public.py`) cuyo `tenant_id` admite None (`str \| None`, `Optional[str]`, `= None`): una variable que resulta ser None lee todos los tenants, y ningún literal marca la llamada. Las fachadas generadas para un servicio de un solo tenant aparecen aquí a propósito -- cámbialas a `tenant_id: str` al encender tenancy |
| `route-without-tenant` | high | una ruta que abre una sesión de base de datos y no tiene dependencia de tenant |
| `factory-without-tenant` | high | lo mismo en una dependencia (`get_service`), reportada una vez con las rutas que la usan |
| `raw-sql-without-tenant` | high | un string SQL que nombra una tabla de tenant y nunca `tenant_id` |
| `storage-key-without-tenant` | high | `storage.put(f"invoices/{id}.pdf", ...)`: una llave armada sin el tenant |
| `cache-key-without-tenant` | high | `cache.get(f"report:{month}")`: una llave armada sin el tenant |
| `rag-unscoped` | high | `[plugin.rag] tenant_scoped = false` |
| `scheduled-job-without-tenant` | medium | una tarea programada con `every=`/`cron=` (o `tasks.schedule`) que arma un `Job` o un `Event` sin `tenant_id`, o recibe un `TaskSession`: un tick corre sin tenant, así que su sesión ve todas las filas hoy y ninguna con RLS |
| `llm-call-without-tenant` | medium | `llm.chat(...)` sin `tenant_id`: no aplica ningún presupuesto por tenant |

**Son heurísticas**, leídas del código sin importarlo, y están hechas para
callarse cuando no pueden decidir en vez de adivinar:

- Una dependencia de tenant es `current_tenant`, `TenantSession`, o un alias
  `Annotated` de cualquiera de las dos declarado en el proyecto. Una
  dependencia se sigue hacia funciones del mismo archivo o del mismo
  `modules/<nombre>/`; una importada de otro lado no se sigue.
- Una llamada a storage o cache se reconoce por el nombre de quien la recibe
  (`storage`, `disk`, `cache`), y su llave tiene que carecer *visiblemente* del
  tenant: un literal, un f-string, un `.format()` o una variable local
  asignada con uno. Una llave que llega como parámetro no se juzga: quien la
  armó no está a la vista.
- SQL crudo es un literal (o f-string, o `+` de ellos) con
  `SELECT`/`INSERT`/`UPDATE`/`DELETE` y una tabla de tenant después de `FROM`,
  `JOIN`, `UPDATE` o `INTO`, y sin `tenant` en ningún lado. Los docstrings son
  prosa y se saltan.
- Las tablas de tenant son los modelos cuya cadena de bases incluye
  `TenantMixin` o que declaran `tenant_id`.
- No se leen los tests ni `migrations/`: un test que pasa `tenant_id=None`
  ejercita a propósito el comportamiento de un solo cliente, y una revisión es
  historia.

Lo que se reporta y es deliberado se exime en línea con el comentario que
`jfast contracts check` ya respeta, en la línea del hallazgo o en una línea de
comentario justo arriba:

```python
rates = await cache.get("fx:usd")  # contracts: allow exchange rates are global
```

Un hallazgo eximido se lista como eximido, con su razón, para que la decisión
siga siendo revisable. `--json` trae `findings`, `waived`, `tenant_tables` y la
tabla `rules`. Sale con 1 mientras quede algo por arreglar, 0 cuando no.

Es un flag de `jfast check` y no un check de su batería porque sus hallazgos
son sobre una hipótesis: un servicio correcto de un solo cliente fallaría
`jfast check` para siempre. El flag reemplaza la batería por este reporte.

### El cambio: `jfast tenancy enable`

```bash
jfast tenancy enable --tenant acme --dry-run   # imprime todo, no escribe nada
jfast tenancy enable --tenant acme             # escribe la revisión y jfast.toml
alembic upgrade head                           # el paso que cambia datos
```

Escribe **una revisión de Alembic**, sobre el head actual, con una sentencia
por tabla que quien revisa puede tachar:

1. todo `tenant_id` NULL pasa a ser `--tenant`, el cliente atendido hasta ahora;
2. con `--not-null`, la columna pasa a NOT NULL (decláralo también en los
   modelos, o el siguiente autogenerate lo revierte);
3. `enable_tenant_rls` en cada tabla, **después** de su relleno, porque
   `FORCE ROW LEVEL SECURITY` aplica también al rol de la migración y una
   migración no pone tenant;
4. la tabla de chunks de RAG, cuando `[plugin.rag]` guarda los chunks en
   pgvector: su `tenant_id` NULL re-asignado al mismo tenant y la misma
   política aplicada, dentro de un bloque `DO` que primero revisa que la tabla
   exista (el plugin rag la crea al arrancar, así que un servicio migrado puede
   no tenerla todavía).

Y edita `jfast.toml` en su lugar, conservando los comentarios: `tenancy` en
`[plugins].enabled`, `[plugin.tenancy] sources = ["token", "user"]` (o
`--sources`, con `--base-domain` para `subdomain`), `[plugin.database] rls =
true`, `[plugin.rag] tenant_scoped = true`.

Luego imprime lo que no puede hacer, en orden: aplicar la revisión como dueño
de las tablas; crear el rol al que aplican las políticas (el SQL de arriba);
decidir quién ve las filas rellenadas; arreglar lo que el reporte todavía
encuentra. Se niega, con el arreglo en el mensaje, cuando todavía no hay
revisiones, cuando las revisiones tienen varios heads (`alembic merge heads`),
cuando ya existe una revisión del cambio, y cuando las fuentes elegidas nunca
podrían resolver (`token`/`user` sin auth).

**Elegir `--tenant`.** Las filas existentes le pertenecen, así que un request
las ve solo cuando resuelve a ese tenant: un token cuyo claim `tenant_id` lo
diga, o, con la fuente `user`, el usuario cuyo id es. Si hasta ahora la app era
de una sola persona, el id de usuario de esa persona es el valor correcto.

El downgrade quita las políticas y deja el tenant rellenado en su lugar: un
servicio de un solo cliente lee esas filas sin filtro de cualquier forma, y
adivinar cuáles eran NULL antes sería un segundo cambio de datos, silencioso.

### Row-level security es la red de seguridad

El reporte es una lista de heurísticas y algo se le va a escapar. El cambio
activa row-level security para que lo que se escape falle *cerrado*: una query
cruda que nadie pasó al tenant no devuelve filas en vez de devolver las de otro
cliente, y una escritura para el tenant equivocado la rechaza la base. Un bug
visible, no una fuga. `tests/test_tenancy_enable_pg.py` lo prueba sobre un
servicio generado, con un rol sin SUPERUSER ni BYPASSRLS: después de `jfast
tenancy enable` y `alembic upgrade head`, un segundo tenant lee cero filas de
cada tabla -- chunks incluidos -- sin ningún `WHERE`, su `UPDATE` y su `DELETE`
no tocan nada, y un `INSERT` con el id del primer tenant falla en la política.

### Detrás de PgBouncer

El pooling por transacción le da una conexión del servidor a muchos clientes,
una transacción a la vez. Lo que `tests/test_rls_pgbouncer.py` verifica ahí
(PgBouncer 1.25, `pool_mode = transaction`, `default_pool_size = 1`, así que
toda transacción cae en el mismo backend):

- **El tenant es local a la transacción.** Después de una transacción para
  `acme`, la transacción del siguiente cliente en la misma conexión del
  servidor lee `current_setting('jfast.tenant_id', true)` vacío y no ve filas.
- **Tenants intercalados nunca se ven.** Cincuenta transacciones concurrentes,
  dos tenants, un backend: cada una ve solo sus filas, y una transacción sin
  tenant después no ve ninguna.
- **Row-level security no le pide nada al pooler.** Ni `SET` ni
  `server_reset_query`: `set_config(..., true)` termina con la transacción.

**asyncpg necesita un ajuste.** asyncpg cachea prepared statements por conexión
del cliente; detrás de un pool por transacción la siguiente transacción puede
correr en otra conexión del servidor, donde el statement nunca se preparó:

```
prepared statement "__asyncpg_stmt_7__" does not exist
```

Medido: con `max_prepared_statements = 0` y más de una conexión del servidor
en el pool, ocho workers concurrentes fallan antes de veinte transacciones con
los defaults de asyncpg. El arreglo es un ajuste:

```toml
[plugin.database]
pgbouncer = true   # por conexión: [plugin.database.connections.x] pgbouncer = true
```

Pone `statement_cache_size = 0` (asyncpg), `prepared_statement_cache_size = 0`
(SQLAlchemy) y un nombre único por statement, para que dos procesos que
comparten una conexión del servidor tampoco choquen en `__asyncpg_stmt_1__`. En
PgBouncer 1.21+ con `max_prepared_statements > 0` (200 por defecto en la 1.25
que corrimos) los defaults de asyncpg también funcionaron en la misma prueba,
porque PgBouncer lleva los statements por su cuenta; ahí el ajuste no estorba, y
es el correcto donde ese seguimiento está apagado.

**Parámetros de arranque.** El plugin de base de datos fija `timezone` como
parámetro de arranque, y PgBouncer lo reenvía de forma nativa. El
`default_transaction_read_only` de una réplica no es uno que PgBouncer conozca:
la conexión se rechaza con `unsupported startup parameter` salvo que PgBouncer
(1.20+) tenga `track_extra_parameters = default_transaction_read_only` --
verificado: dos clientes, uno de solo lectura, alternando en un backend, cada
uno vio su propio valor. No lo pongas en `ignore_startup_parameters`: eso quita
la protección de la réplica sin avisar.

CI, para GitHub Actions (las dos URLs que lee el test):

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    env: { POSTGRES_USER: jfast, POSTGRES_PASSWORD: jfast, POSTGRES_DB: jfast }
    ports: ["5499:5432"]
    options: >-
      --health-cmd "pg_isready -U jfast" --health-interval 5s --health-retries 10
  pgbouncer:
    image: edoburu/pgbouncer:latest   # fija el tag que verificaste
    env:
      DB_HOST: postgres
      DB_PORT: "5432"
      DB_NAME: jfast
      DB_USER: jfast_bouncer          # lo crea el test, NOSUPERUSER NOBYPASSRLS
      DB_PASSWORD: jfast_bouncer
      AUTH_TYPE: scram-sha-256
      POOL_MODE: transaction
      DEFAULT_POOL_SIZE: "1"          # toda transacción en un backend
      MAX_PREPARED_STATEMENTS: "0"    # el caso estricto
    ports: ["6435:5432"]
env:
  JFAST_TEST_PG_URL: postgresql+asyncpg://jfast:jfast@localhost:5499
  JFAST_TEST_PGBOUNCER_URL: postgresql+asyncpg://jfast_bouncer:jfast_bouncer@localhost:6435/jfast
```

Las mismas variables de entorno se probaron contra un contenedor local. La
imagen de PgBouncer se conecta a PostgreSQL cuando entra el primer cliente, que
es después de que el test creó el rol, así que no hace falta ordenar los dos
servicios. `JFAST_TEST_PGBOUNCER_MULTI_URL` -- una base de PgBouncer con varias
conexiones del servidor y `max_prepared_statements = 0` -- corre además el test
que reproduce la falla de asyncpg y muestra que el ajuste la arregla.

## Ver también

- [Autenticación](auth.md) — el claim `tenant_id`
- [Cuentas](accounts.md) — usuarios y registro, con la fuente `user`
- [RAG y búsqueda vectorial](rag.md) — recuperación limitada al tenant
- [Modelos de lenguaje](llm.md) — presupuestos por tenant
- [Deploy](deploy.md) — Caddy como edge
