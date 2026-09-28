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
| `subdomain` | tu DNS y TLS | media |
| `path` | la URL | baja |
| `header` | quien haya mandado el request | **ninguna** |

`header` existe porque es realmente útil en desarrollo y en los tests. No está
en la lista por defecto, y activarlo en producción escribe un warning, porque
`X-Tenant-ID: acme` está a un `curl` de distancia de los datos de otro tenant.

Un claim firmado siempre le gana al hostname. Alguien que apunta `acme.` a tu
IP no se convirtió en Acme; alguien con un token que tu identity provider firmó
para Acme, sí.

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

## Lo que esto no es

El tenant resuelto llega a `BaseRepository`, así que una query que se olvida de
filtrar igual queda filtrada por el repositorio. **Eso es una convención, no
aislamiento.**

Cualquiera de estas cosas lo rompe: SQL crudo, un join a través de una tabla
sin scope, un bug en un método de repositorio, un background job que corre sin
request. La garantía que quieres es row-level security de PostgreSQL, donde la
base de datos rechaza la lectura sin importar lo que haya pedido la query. Eso
todavía no se genera — ver PLAN.md fase 2.

Hasta entonces, trata a tenancy como defensa en profundidad sobre queries
correctas, no como un reemplazo de ellas.

## Ver también

- [Autenticación](auth.md) — el claim `tenant_id`
- [Deploy](deploy.md) — Caddy como edge
