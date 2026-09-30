# Probarlo en local

JFastFramework está en PyPI como pre-release. Con `pip install jfastframework`
alcanza para usarlo; instala desde un checkout cuando pienses cambiar el
framework mismo.

## De cero a un stack corriendo

```bash
pip install jfastframework
mkdir -p ~/projects/shop && cd ~/projects/shop
jfast start shop
```

No hace falta `--pre`. Pip rechaza un pre-release solo cuando hay una versión
estable que preferir; cuando todas las versiones publicadas de un paquete son
pre-releases -- como lo es cada `0.1.0aN` acá -- resuelve la más nueva.

Pedir `--pre` igual te cuesta algo, porque el flag no está acotado al paquete
que nombraste: mete *todas* las dependencias de la resolución en sus propios
pre-releases. Terminas con un FastAPI alpha y un pydantic alpha debajo de un
framework que no se probó contra ninguno de los dos, y la primera falla cae en
código ajeno.

## Desde un checkout, para cambiar el framework mismo

Un solo bloque. Pégalo en una shell de Linux o WSL:

```bash
git clone https://github.com/JFabrizzio5/JFastFramework ~/github/JFastFramework \
  && cd ~/github/JFastFramework \
  && python3 -m venv .venv \
  && ./.venv/bin/pip install -e ".[all,dev]" \
  && export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH" \
  && mkdir -p ~/projects/shop && cd ~/projects/shop \
  && jfast start shop
```

Eso te deja un monolito modular, un frontend en Vue, un archivo de compose con
un contenedor por datastore, un Caddyfile, y cada cadena de conexión ya
escrita. Después:

```bash
jfast workspace env      # generate the secrets and each service's .env
docker compose up -d     # the datastores
cd shop && ../.venv/bin/uvicorn main:app --reload --port 8010 --no-proxy-headers
```

`http://localhost:8010/docs` es la API, `/health` y `/ready` son las probes, y
`jfast workspace graph` imprime qué está conectado con qué.

**Windows:** corre todo dentro de WSL, no en PowerShell. Los scripts generados
son bash, y `npm` dentro de WSL resuelve al binario de Windows salvo que Node
esté instalado en la distribución.

---

## Una vez, para instalarlo

```bash
cd ~/github/JFastFramework
python3 -m venv .venv
./.venv/bin/pip install -e ".[all,dev]"
```

`-e` es una instalación editable: el comando `jfast` corre el código de este
checkout, así que editar el framework tiene efecto de inmediato, sin
reinstalar.

Ponlo en tu PATH para la sesión:

```bash
export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"
jfast version
```

O hazlo permanente:

```bash
echo 'export PATH="$HOME/github/JFastFramework/.venv/bin:$PATH"' >> ~/.bashrc
```

### Si `python3 -m venv` falla

Ubuntu saca `venv` del Python base. O lo instalas:

```bash
sudo apt install python3-venv
```

o metes pip en el venv sin él:

```bash
python3 -m venv --without-pip .venv
curl -sS https://bootstrap.pypa.io/get-pip.py | ./.venv/bin/python
```

---

## Un proyecto nuevo, por la vía rápida

```bash
mkdir ~/projects/shop && cd ~/projects/shop
jfast start shop
```

Eso es todo. Obtienes:

```
shop/                  FastAPI + PostgreSQL/pgvector + Redis + background jobs
  modules/item/        a starter module whose tests already pass
  contracts.toml       the rules this service holds itself to
  alembic.ini          migrations, reading the app's own DSN
shop-web/              Vue 3 + Vite + Tailwind, pointed at the backend
docker-compose.yml     derived from the enabled plugins
Caddyfile              one hostname in front of both
jfast.workspace.toml   ports, and what the frontend should call
```

`jfast start` no pregunta nada, así que dos decisiones son flags:

- **Clientes.** `--single-tenant` (el default) o `--multitenant`. Multitenant
  enciende `tenancy` (el tenant sale del token firmado o del usuario que inició
  sesión), `auth` y `accounts`, genera rutas que toman el tenant de
  `current_tenant`, y escribe en `shop/.env` un `JFAST_AUTH_SECRET` y la
  contraseña del primer admin. Un solo cliente es el default porque un servicio
  multitenant necesita un sistema de usuarios y alguien con sesión antes de que
  funcione su primer `curl` -- la forma correcta para un SaaS, demasiado para
  cualquier otra cosa. Todas las tablas conservan su columna `tenant_id` en
  ambos casos, así que cambiar después es un backfill y no una reescritura del
  esquema.
- **Trazas.** `telemetry` viene encendido y no exporta nada hasta que se define
  `OTEL_EXPORTER_OTLP_ENDPOINT`. `--no-telemetry` lo deja fuera.

### Correr el backend

```bash
cd shop
pip install -r requirements.txt
cp .env.example .env          # fill in POSTGRES_PASSWORD
pytest                        # the starter module's tests
jfast serve --port 8000
```

`jfast serve` en lugar de `uvicorn main:app` porque el manejo propio de
`X-Forwarded-For` de uvicorn viene activado y confía en loopback, que es
justamente lo que un servidor de desarrollo bindea. Reescribiría la dirección
del cliente antes de que `trusted_proxies` del framework llegara a ver un peer,
así que un rate limit probado acá pasaría por la razón equivocada. Arrancar
uvicorn a mano necesita el flag apagado:

```bash
uvicorn main:app --reload --port 8000 --no-proxy-headers
```

```bash
curl localhost:8000/health
curl localhost:8000/docs      # OpenAPI UI
```

Los endpoints que necesitan la base de datos solo funcionan cuando hay una
corriendo:

```bash
docker compose up -d shop-database
jfast exec -- alembic revision --autogenerate -m "initial"
jfast exec -- alembic upgrade head
```

`jfast exec --` corre un comando con el `.env` del servicio traducido para tu
máquina. En un workspace ese `.env` está escrito para compose -- la base es
`shop-database:5432` y la contraseña es `${SHOP_DATABASE_PASSWORD}`, que solo
compose rellena --, así que un `alembic` o un `pytest` a secas en el host falla
con un error de DNS. `jfast serve`, `jfast worker` y `jfast dev` lo traducen
solos; todo lo demás pasa por `jfast exec --`. Sin un `docker-compose.yml` junto
al servicio o un nivel arriba no cambia nada.

### Correr el frontend

```bash
cd ../shop-web
npm install
npm run dev                   # http://localhost:8010
```

La página de inicio llama al `/health` del backend al cargar, así que si el
cableado está mal lo ves de inmediato y no en tu primer feature de verdad.

### O todo en Docker

```bash
cd ~/projects/shop
docker compose up --build
```

---

## Un proyecto nuevo, eligiendo sobre la marcha

```bash
jfast init
```

Pregunta qué estás construyendo, qué datastores quieres, si la app atiende a
varios clientes, qué capacidades y en qué puerto -- y después genera
exactamente lo mismo que habrían generado los flags.

**"¿Esta app atiende a varios clientes (multitenant)?"** Una respuesta fija
todas las piezas que tienen que coincidir, así que no pueden contradecirse:

| | Sí | No |
| --- | --- | --- |
| `tenancy` | encendido, `sources = ["token", "user"]` | apagado |
| rutas generadas | `current_tenant` (401/403 sin tenant) | `require_auth` con auth, abiertas sin él |
| `[plugin.rag] tenant_scoped` | `true` | `false` |
| `[plugin.llm] tenant_budget_usd` | definido | ausente |
| premarcados | + `auth`, `accounts` | |
| columnas `tenant_id` | se quedan | se quedan |

**Las capacidades** salen del catálogo de plugins, así que un plugin nuevo del
framework no puede faltar en el menú. Los recomendados vienen premarcados,
con la etiqueta *recommended* y primero en la lista -- apretar Enter los
conserva:

- `telemetry` -- las trazas no cuestan nada hasta que hay un endpoint, y son lo
  primero que hace falta cuando algo va lento en producción, cuando encenderlas
  ya no ayuda.
- `queue` -- todo lo que tarda más que una petición va fuera de ella; el
  backend es el PostgreSQL que el servicio ya tiene, y `jfast worker` la consume.
- `auth` y `accounts`, cuando la respuesta de arriba es sí: un tenant sale de
  alguien que inició sesión.

Un plugin que el framework instalado no trae se omite con una nota en vez de
escribirse en un `jfast.toml` que no arrancaría.

Lo mismo, con flags:

```bash
jfast new service billing --with database,cache,queue
jfast new service edge --language go
jfast new service admin --kind spa --frontend vue
jfast new service saas --with database,queue,telemetry --multitenant
```

Un frontend dibuja inicio de sesión, registro y la página de seguridad, y deja
sus rutas privadas por defecto, solo cuando algún backend del workspace
habilita `accounts`; si no, queda público y sin páginas de cuenta.

### Agregar y quitar un plugin después

```bash
jfast add telemetry          # [plugins].enabled, el extra, su bloque de settings
jfast add accounts           # también habilita auth (y database), que requiere
jfast remove telemetry       # se niega mientras otro plugin habilitado lo requiera
```

`jfast add` edita `[plugins].enabled` en `jfast.toml` -- conservando todos los
comentarios --, fija el extra del plugin en `requirements.txt`, agrega el bloque
de settings con el que se habría generado un servicio nuevo, e imprime lo que
falta: las variables que lee y el paso siguiente (`alembic upgrade head`,
`jfast worker`). `jfast remove` quita el extra cuando ningún otro plugin
habilitado lo usa y deja el bloque `[plugin.<nombre>]` para el día que vuelva.
`jfast add` sin argumento lista plugins y capacidades juntos.

---

## El ciclo que vas a usar de verdad

```bash
jfast dev                         # containers, migrations, API and frontend
jfast new module invoice          # asks which architecture; registers itself
jfast exec -- pytest modules/invoice/tests
jfast contracts check             # layer boundaries, forbidden calls
jfast exec -- alembic revision --autogenerate -m "add invoices"
```

El módulo se monta solo: el generador inserta el import y el router en
`main.py` en los marcadores que dejó ahí. Ve [El ciclo local](dev.md) para
saber qué hace `jfast dev` en cada etapa y qué se salta cuando falta algo.

---

## Si `jfast` no está en tu PATH

```bash
python -m jfastframework --help
python -m jfastframework start shop
```

Idéntico al script `jfast`. Útil en una instalación de Windows donde
`Scripts/` no está en el PATH, en un virtualenv que nadie activó, o en un paso
de CI que preferiría no adivinar dónde puso pip el binario.

---

## Una nota sobre las terminales de Windows

La CLI resuelve cada símbolo que imprime contra la codificación que tu consola
reporta de verdad, y cae a ASCII cuando un glifo no entra:

```
+---------------------------------+
|  jfastframework                 |
|  the opinionated default stack  |
+---------------------------------+
  + jfast.workspace.toml        workspace
```

Esto no es cosmético. Una consola de Windows es `cp1252` o `cp850` mucho más
seguido que UTF-8, y ninguna de las dos tiene `✓` ni los caracteres de dibujo
de cajas — escribir uno no imprime un placeholder, levanta
`UnicodeEncodeError` a mitad de la escritura. Antes de que existiera el
fallback, `jfast start` moría con un traceback **después** de crear medio
proyecto.

Nada que configurar. Si quieres la versión dibujada en una terminal que la
soporta, define `PYTHONIOENCODING=utf-8`.

---

## Trabajar sobre el framework mismo

```bash
cd ~/github/JFastFramework
pytest                                  # unit tests, ~1s
ruff check src tests docs-site
mypy src

bash scripts/smoke.sh                   # both module layouts, HTMX, alembic
bash scripts/smoke_contracts.sh         # contracts catch what they should
bash scripts/smoke_workspace.sh         # workspace, gateway, view patching
bash scripts/smoke_start.sh             # the default stack end to end
bash scripts/smoke_go.sh                # needs go on PATH
bash scripts/smoke_frontend.sh          # needs npm on PATH
```

Cada script de smoke genera un proyecto en un directorio temporal, lo corre y
lo borra. Son los únicos chequeos que atrapan un template que renderiza limpio
y produce código que no funciona — `pytest` solo no puede.

### Toolchains opcionales

Las suites de Go y de frontend se saltan solas cuando falta su toolchain. Para
correrlas localmente sin tocar tu sistema:

```bash
mkdir -p ~/.jfast-toolchains && cd ~/.jfast-toolchains
curl -sSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz | tar xz
curl -sSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz | tar xJ
mv node-v22.12.0-linux-x64 node
export PATH="$HOME/.jfast-toolchains/go/bin:$HOME/.jfast-toolchains/node/bin:$PATH"
```

Borra el directorio para deshacerlo. No se instala nada a nivel del sistema.

---

## Problemas comunes

**`jfast: command not found`** — el venv no está en el PATH. O lo exportas
como arriba, o lo llamas directo:
`~/github/JFastFramework/.venv/bin/jfast`.

**`No jfast.toml found`** — `jfast describe`, `doctor` y `deploy` corren
dentro de un directorio de servicio. `jfast start`, `init` y `workspace`
corren por encima.

**`Port 8011 is already taken by 'billing'`** — el workspace asigna bloques de
diez puertos. Deja que elija él (`jfast new service …` sin `--port`) en vez de
elegir a mano.

**Alembic no puede llegar a la base de datos** — `migrations/env.py` lee
`JFAST_DB_DSN` del `.env`, deliberadamente el mismo valor que usa la app.
Levanta primero el contenedor: `docker compose up -d <service>-database`. Si el
error es `nodename nor servname provided` o `Name or service not known` y nombra
`<workspace>-database`, el `.env` es el de compose: córrelo como `jfast exec --
alembic ...`.

**Windows** — corre todo esto dentro de WSL. Los Dockerfiles generados, los
archivos de compose y los shell scripts asumen una shell POSIX.

**`413 Payload Too Large` en un request que antes funcionaba** — el límite de
body viene encendido, en 2 MiB, y el request timeout en 30 segundos. Habilitar
el plugin `storage` sube el par a 25 MiB y 120 segundos. Eso lo resuelve el
kernel, no el scaffold: `effective_max_body_bytes` y
`effective_request_timeout` leen la lista de plugins, así que un servicio que
habilita `storage` un año después de haber sido generado recibe el mismo par
sin regenerar nada. Escribir el campo en `jfast.toml` siempre le gana a la
subida — incluido `max_body_bytes = 0`, que quita el límite por completo, y
`request_timeout = 0`, que quita el timeout. `jfast new service --with storage`
escribe los dos con los valores subidos, para que el número se vea en el
archivo; bájalos ahí a lo que el servicio realmente acepta. Un
`504 Gateway Timeout` en un endpoint lento es la misma historia con
`request_timeout`.

**El navegador bloqueó un script o una hoja de estilos** — la
Content-Security-Policy viene encendida. Permite lo que cargan las páginas del
propio framework y nada más, así que el primer asset de terceros que agregues
a un template va a ser rechazado. Agrega su origen a `csp` en `jfast.toml`;
[deploy.md](deploy.md) tiene la política completa y el camino a una más
estricta.

**`http://localhost` de pronto redirige a HTTPS** — eso es HSTS, y no salió de
aquí: HSTS queda apagado fuera de producción y se omite en cualquier request
que no haya llegado por HTTPS. Lo mandó otra cosa que corriste en `localhost`,
y el navegador lo recuerda por host durante todo el `max-age`. Límpialo en
`chrome://net-internals/#hsts` (o el equivalente de Firefox) — limpiar el
caché del sitio no sirve.
