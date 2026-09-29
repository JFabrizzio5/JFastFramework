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
siguiente request, y funciona detrás de PgBouncer en modo transacción.

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

Actívalas de arriba hacia abajo. Las tres primeras no cuestan nada y vienen con
el framework; row-level security es un setting, una migración y un rol de base
de datos; el resto se activa en cuanto el plugin lo está.

Lo que ninguna atrapa: **un id de tenant equivocado pero bien formado.** Si tu
propio código asigna un usuario a la organización equivocada, cada capa va a
hacer cumplir fielmente la respuesta equivocada. Ese mapeo -- dónde vive, quién
lo puede cambiar -- merece la revisión más cuidadosa del servicio.

## Ver también

- [Autenticación](auth.md) — el claim `tenant_id`
- [Cuentas](accounts.md) — usuarios y registro, con la fuente `user`
- [RAG y búsqueda vectorial](rag.md) — recuperación limitada al tenant
- [Modelos de lenguaje](llm.md) — presupuestos por tenant
- [Deploy](deploy.md) — Caddy como edge
