# JFastFramework

**Genera el backend que nadie quiere escribir dos veces, y evita que se
degrade mientras un equipo — o un agente — trabaja sobre él.**

Un comando te da un servicio FastAPI con PostgreSQL, Redis, jobs en segundo
plano, un frontend Vue y un reverse proxy, cableados entre sí y corriendo bajo
Compose. Lo que escribes tú es la parte que solo tú conoces: las reglas de tu
negocio. Lo que la mantiene coherente después es un contrato que CI hace
cumplir.

Estado: alpha, en PyPI -- la versión actual es la que muestra PyPI; un número
escrito aquí se queda viejo. La madurez se rastrea por subsistema en vez
de con un solo número de versión: [STATUS.md](STATUS.md) dice qué está probado
contra infraestructura real, qué no está verificado y qué se sabe roto. Léelo
antes de depender de cualquier parte.

---

**[jfastframework en PyPI](https://pypi.org/project/jfastframework/)** &middot; **[Documentación](https://jfabrizzio5.github.io/JFastFramework/latest/)**

---

## Un comando

```bash
pip install jfastframework
jfast start shop
```

```
shop/                 FastAPI · PostgreSQL · pgvector · Redis · jobs
shop-web/             Vue 3 · Vite · Tailwind
docker-compose.yml    one container per resource
Caddyfile             one hostname, TLS, static assets
.env                  every DSN, generated from the bindings
```

```bash
jfast dev             # containers up, migrations applied, API and frontend running
```

¿Prefieres elegir? `jfast init` pregunta. ¿Prefieres flags? Cada elección es
una.

---

## Para quién es esto

| Úsalo cuando | Porque |
| --- | --- |
| Arrancas backends seguido — trabajo de agencia, herramientas internas, un producto con varios servicios | La plomería se genera y se versiona, así que el décimo cuesta lo que costó el primero |
| Un equipo chico va a mantener lo que escribas | Los límites entre capas los verifica CI, así que una review es sobre la feature y no sobre dónde quedó el archivo |
| Estás dirigiendo agentes de IA por un codebase | Cada regla es legible por máquina y se hace cumplir, así que un agente que se desvía rompe el build en vez de mergear |
| Quieres empezar como monolito y separar después | Las fronteras entre módulos se verifican, así que las costuras siguen siendo reales; cuando a uno le queda chico el resto, `jfast new service` levanta su deploy y recablea el workspace |

### Cuándo no usarlo

Acá importan más las respuestas honestas que una feature más.

| No lo uses cuando | Usa en su lugar |
| --- | --- |
| Estás escribiendo un endpoint, o un script con una UI web encima | FastAPI pelado. Esto es mucha estructura para un solo archivo |
| El equipo ya tiene un framework propio y convenciones que funcionan | El tuyo. Acá el valor son las opiniones, y ya tienes algunas |
| Necesitas el admin de Django, su ecosistema de ORM o su auth de fábrica | Django. Esto no intenta ser eso |
| Estás en un stack síncrono y no quieres async | Flask, o FastAPI sin esto |
| Necesitas hoy aislamiento multi-tenant de nivel producción | Todavía no — aquí el tenancy es una convención, no row-level security. [STATUS.md](STATUS.md) es explícito al respecto |

---

## Qué obtienes en realidad

```
shop/                       one service
├── main.py                 routers register themselves here
├── jfast.toml              which plugins are on; which layout each module uses
├── contracts.toml          the rules, checked by CI
├── modules/
│   └── invoice/            one business capability
│       ├── router.py           HTTP in, response out
│       ├── service.py          the rules — no SQL, no Request
│       ├── repository.py       queries — no HTTP concepts
│       ├── schemas.py
│       └── tests/
├── shared/                 what two modules both need
└── migrations/
```

El framework es una **librería que tu servicio importa**, no código copiado
dentro. Arreglas algo en el framework y todos los servicios lo reciben en el
siguiente bump de versión. Lo que se genera es solo la parte que es
genuinamente tuya.

---

## Los casos de uso para los que fue construido

Estas son las formas que le calzan. Si la tuya no es una de ellas, la tabla de
arriba es la guía honesta.

**Una agencia que arranca un proyecto de cliente cada pocas semanas.** El
octavo backend no debería costar lo que costó el primero. `jfast start` produce
el mismo stack siempre, así que el desarrollador que lo tome en un año
encuentra el layout que ya conoce, y un arreglo del framework llega a los ocho
por un bump de versión en vez de por ocho parches.

**Una herramienta interna que va a sobrevivir a su autor.** El contrato es la
entrega: `jfast contracts show --json` declara qué posee el servicio, qué no, y
qué límites se hacen cumplir. La siguiente persona no tiene que inferir la
arquitectura leyendo el código.

**Un producto que empieza como un servicio y todavía no sabe dónde están sus
costuras.** Arrancas como monolito modular con límites de módulo reales —
verificados, para que no se erosionen en silencio — y promueves un módulo a su
propio servicio cuando la carga o el equipo lo pidan. Separar después es una
jugada; volver a unir es un rewrite.

**Un codebase donde los agentes escriben la mayor parte del código.** Las
reglas son ejecutables. Un agente que consulta la base de datos desde un
router, importa un módulo dentro de otro, o bloquea el event loop falla en
`jfast contracts check` con el archivo, la línea y el arreglo. Esa es la
diferencia entre un agente que supervisas y uno que puedes dejar corriendo.

---

## La arquitectura que obtienes

**Un monolito modular, con las costuras visibles.** Un deployable, varios
módulos, y límites que se hacen cumplir en vez de acordarse:

- Un módulo nunca importa otro módulo. Lo que dos módulos necesitan se mueve a
  `shared/`, y el checker nombra el archivo al que hay que moverlo.
- `shared/` nunca importa un módulo. Sin esa segunda regla, `shared/` se
  convierte en donde termina todo.
- Nada en `shared/` toca la base de datos. Dos módulos compartiendo un
  repository son dos módulos compartiendo una tabla.

**Cada módulo elige su propia forma.** Un catálogo son cuatro archivos; un
módulo de órdenes que tiene que ser testeable sin base de datos quiere puertos
y adaptadores. Cuatro layouts — `layered`, `modular`, `screaming`,
`hexagonal` — elegidos por módulo, y recordados en `jfast.toml` para que los
comandos posteriores sepan dónde va un archivo nuevo.
[docs/modules.md](docs/modules.md) tiene la pregunta que te dice cuál elegir.

**Y una salida.** Cuando a un módulo le queda chico el monolito,
`jfast new service billing` levanta su servicio y el workspace recablea los
puertos, los DSN y el gateway a su alrededor. **Mover el código del módulo sigue
siendo tarea tuya** — nada aquí lo extrae por ti. Lo que la herramienta te quita
es el trabajo de infraestructura, que es la parte que suele frenar a la gente.
No tienes que adivinar las costuras el día uno, que es justamente la razón para
empezar como monolito.

---

## Qué problema resuelve esto

Los generadores de scaffolding copian código. Cada servicio generado se
convierte en un fork congelado en el momento de la generación: arreglas un bug
en el cliente HTTP compartido y lo parchas a mano en doce lugares.

JFast separa las dos preocupaciones que los generadores confunden:

| | |
| --- | --- |
| **Runtime** (`jfastframework`) | Una librería versionada que tus servicios **importan**. Arréglala una vez, sube el pin. |
| **Generador** (CLI `jfast`) | Emite solo el código que es genuinamente tuyo. |

La segunda mitad es donde la mayoría de las herramientas se detienen: **la
estructura generada se degrada salvo que algo la sostenga.** Por eso el
generador también emite un `contracts.toml`, y `jfast contracts check` rompe el
build cuando el código se desvía de él.

Todo lo que está por encima del kernel es un plugin.

```toml
[plugins]
enabled = ["observability", "metrics", "database", "cache", "queue"]
disabled = ["sentry"]
```

Borra `"cache"` y el cliente de Redis, su health check y su contenedor en el
compose generado desaparecen todos juntos. La infraestructura se deriva del
grafo de plugins, así que no puede desviarse de lo que la app realmente carga.

## Plugins

| Plugin | Hace | Extra |
| --- | --- | --- |
| `observability` | Logs JSON, correlación por request-id y tenant | — |
| `metrics` | Métricas RED de Prometheus, `/metrics` | `metrics` |
| `database` | SQLAlchemy async, sesiones, cableado de Alembic | `db` |
| `cache` | Caché Redis, pub/sub | `cache` |
| `queue` | Jobs en segundo plano sobre PostgreSQL, Redis o RabbitMQ | `queue` |
| `events` | Publicación/suscripción con Kafka | `kafka` |
| `channels` | Canales pub/sub declarados sobre memoria, Redis o Kafka | — |
| `mongo` | MongoDB vía Motor | `mongo` |
| `qdrant` | Base de datos vectorial Qdrant | `qdrant` |
| `rag` | Recuperación sobre pgvector o Qdrant | `rag` |
| `web` | Renderizado parcial con Jinja2 + HTMX | `web` |
| `gateway` | Reverse proxy basado en prefijos | `gateway` |
| `auth` | Verificación JWT, scopes, revocación, login social | `auth` |
| `ratelimit` | Token bucket en Redis, por subject, tenant o IP | `cache` |
| `websocket` | WebSockets autenticados con backplane Redis y registro de conexiones | `server` |
| `storage` | Archivos en discos locales, S3 o MinIO | `storage` |
| `tenancy` | Tenant desde un claim del token, subdominio o path | — |
| `notifications` | Push por Firebase Cloud Messaging | `fcm` |
| `mail` | Email con templates, encolado por defecto | `mail` |
| `sentry` | Reporte de errores y performance | `sentry` |

Los plugins de terceros se registran por el mismo entry-point group, así que
nada de aquí es privilegiado.

---

## No solo Python

Un servicio JFast no es "un servicio escrito con JFast" — es un servicio que
cumple [el contrato](docs/service-contract.md): `/health`, `/ready`,
`X-Request-ID`, `problem+json`, config `JFAST_*`, un bloque de diez puertos,
logs JSON en stdout.

```bash
jfast new service edge --language go --grpc
```

Ese servicio Go tiene **cero dependencias de terceros** e implementa el
contrato en ~300 líneas vendorizadas. El gateway rutea hacia él sin saber que
es Go; el workspace le asigna sus puertos; Caddy lo pone al frente junto con
todo lo demás — porque todos hablan con el contrato, no con el lenguaje.

CI corre `go vet`, `go test`, `go build`, arranca el binario y le hace curl. Un
scaffold que nadie corrió es un pasivo que parece una feature.

`--grpc` genera el contrato `.proto`. **No** genera stubs ni cablea un
servidor — mira [proto/README.md](src/jfastframework/templates/proto/proto/README.md.j2)
para saber por qué, y para los comandos con los que hacerlo tú mismo.

---

## Colas y eventos

Dos cosas distintas, dos plugins:

```toml
[plugin.queue]
backend = "postgres"    # or "redis", "rabbitmq"
```

```python
@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None: ...

await queue.enqueue(Job(task="send_invoice_email", payload={"id": 7}))
```

Empieza con PostgreSQL: encolar comparte la transacción que produjo el trabajo,
así que un rollback se lleva el job con él. Redis compra latencia, RabbitMQ
compra ruteo. [Cuál elegir, y por qué](docs/queues-and-events.md).

La entrega es at-least-once — los handlers tienen que ser idempotentes. Los
reintentos están acotados y el backoff tiene tope; los jobs agotados van a la
dead letter en vez de quedar en loop.

Los eventos son la otra mitad: `events` es Kafka, para "esto pasó" en vez de
"haz esto".

---

## Frontends

**Renderizado en servidor**, sin paso de build:

```bash
jfast new service storefront --kind web
jfast new module product --ui htmx
```

**SPA**, Vue 3 o React con Vite y Tailwind v4:

```bash
jfast new service admin --kind spa --frontend vue
cd admin && jfast new view Facturas
```

`jfast new view` crea `src/ModuloFacturas/{Components,Pages,Routes,Services}` y
lo registra en el router y en el sidebar en sus comentarios marcadores — de
forma idempotente, fallando ruidosamente si un marcador ya no está.

Ambos frontends se instalan y se buildean en CI. Ese job existe por un bug
real: el comentario marcador quedó dentro de un comentario de bloque, cuyo `*/`
interno lo cerró antes de tiempo y dejó el router sintácticamente inválido.
Todos los greps pasaron. Solo `vite build` lo agarró.

Angular y React Native **no** se generan. [Por qué](docs/frontend.md#angular).

---

## Workspaces, gateway, Caddy

```bash
jfast workspace init cometax
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag     # a gateway appears here
jfast workspace compose && jfast workspace caddy
```

Los servicios se registran solos y toman el siguiente bloque libre de diez
puertos. Al **segundo** backend se genera un gateway automáticamente — un solo
backend deliberadamente no lo recibe, porque agregaría un salto y una
superficie de caída a cambio de nada.

Caddy es el borde (TLS, HTTP/3, compresión, la SPA buildeada); el gateway es el
proxy de aplicación detrás de él. Los backends viven bajo `/api` en cualquier
caso, así que el build de producción del frontend sobrevive a la aparición de
un gateway.

[docs/workspaces.md](docs/workspaces.md) · [docs/deploy.md](docs/deploy.md)

---

## Migraciones y tests, ya cableados

Cada servicio generado trae `alembic.ini`, `migrations/env.py`, `pytest.ini` y
`conftest.py`. `env.py` lee el mismo `JFAST_DB_DSN` que lee la app — una
migración no puede correr contra una base distinta a la del servicio — e
importa automáticamente los modelos de cada módulo, así que autogenerate nunca
emite en silencio una migración vacía. [Detalles y las
trampas](docs/migrations-and-tests.md).

---

## Autenticación

```toml
[plugin.auth]
mode = "jwks"                    # jwks | public_key | secret
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

Verificación, scopes y roles, rotación de claves JWKS, rotación de refresh con
detección de reuso, y revocación compartida entre réplicas a través de Redis.

Los defaults rechazan los ataques que no parecen fallas: `alg: none`, confusión
RS256→HS256 (configurar las dos familias de algoritmos a la vez se rechaza al
arrancar — esa combinación *es* el ataque), tokens emitidos para un servicio
hermano, y un clock skew generoso. Las razones del rechazo van al log; el
cliente recibe un 401 pelado.

**El tenancy deja de ser falsificable.** Sin auth, `tenant_id` viene del header
`X-Tenant-ID` — que puede poner cualquiera con curl. Con auth, viene de un
claim firmado.

No hay `/auth/login`: verificar una contraseña contra tu tabla de usuarios es
trabajo de tu aplicación. `auth.issuer` se provee para tu propia ruta.
[docs/auth.md](docs/auth.md).

## Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` pregunta si lo necesitas. Obtienes un árbol de kustomize:
Deployment, Service, ConfigMap, HPA y PodDisruptionBudget por servicio, un solo
Ingress sirviendo `/api` — la misma forma pública que el Caddyfile generado — y
overlays `dev`/`prod`.

Liveness sondea `/health`, readiness sondea `/ready`. Ese contrato de dos
endpoints es lo que evita que un parpadeo de la base de datos reinicie de golpe
todos los pods sanos.

**Las bases de datos no se generan.** Un StatefulSet de PostgreSQL salido de un
scaffolder es la manera en que la gente pierde datos. Los manifiestos leen un
DSN desde un Secret. [docs/kubernetes.md](docs/kubernetes.md).

## Contratos: reglas de las que un agente no puede desviarse

`AGENTS.md` dice qué hacer. Un **contrato** dice qué está permitido, y algo lo
verifica — que es la diferencia entre una regla y una sugerencia.

Cada servicio generado trae un `contracts.toml` que es tuyo:

```toml
[project]
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py"]
why = "Configuration is typed. Add a field to a settings model."
```

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'
  (Data access. No business rules.)
```

El mismo archivo activa la verificación del bug que nunca lanza excepción: una
llamada bloqueante dentro de un `async def`, que frena todas las demás requests
del worker y aparece como latencia en un lugar completamente distinto.

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
```

Exit distinto de cero — en CI, un build roto. Un agente generando código a toda
velocidad se desvía de la prosa; no se desvía de un check que falla.

Tres audiencias, un archivo: el build lo lee por `check`, un agente por `jfast
contracts show --json`, un revisor por el `CONTRACTS.md` generado. Los waivers
son inline y exigen una razón. [docs/contracts.md](docs/contracts.md).

## Por qué los agentes de IA son audiencia de primera clase

```bash
jfast contracts show --json  # the rules THIS project holds itself to
jfast describe --json        # settings schema, plugin graph, providers, infra
jfast workspace list --json  # services, ports, API base URL, needs_gateway
jfast doctor
```

Nada de grep. Además `.jfast/skills/` — una carpeta por tarea con un `SKILL.md`
que declara cuándo usarla y los pasos exactos — y [AGENTS.md](AGENTS.md), las
reglas que un agente tiene que seguir aquí.

---

## Documentación

El sitio se construye desde estos mismos archivos: **<https://jfabrizzio5.github.io/JFastFramework/>**

| Documento | Contenido |
| --- | --- |
| [docs/local-setup.md](docs/local-setup.md) | Instalar desde un checkout y hacer tu primer proyecto |
| [docs/dev.md](docs/dev.md) | El ciclo local: contenedores, migraciones, API y frontend en un comando |
| [docs/agents.md](docs/agents.md) | Trabajar con agentes de IA: qué se hace cumplir, y qué no |
| [docs/contracts.md](docs/contracts.md) | Reglas por proyecto, verificadas |
| [docs/auth.md](docs/auth.md) | JWT: modos, los ataques rechazados, revocación, login con Google |
| [docs/ratelimit.md](docs/ratelimit.md) | Un token bucket que no se filtra bajo carga |
| [docs/websockets.md](docs/websockets.md) | Sockets entre workers, y qué no se entrega |
| [docs/upgrading.md](docs/upgrading.md) | Qué rompe al subir de versión, filtrado a lo que aplica a tu proyecto |
| [docs/storage.md](docs/storage.md) | Discos, URLs firmadas, S3 y MinIO |
| [docs/multitenancy.md](docs/multitenancy.md) | Subdominios, orden de confianza, qué no es |
| [docs/cloud.md](docs/cloud.md) | Gestores de secretos, funciones serverless, push |
| [docs/kubernetes.md](docs/kubernetes.md) | Manifiestos, probes, qué no se genera |
| [docs/service-contract.md](docs/service-contract.md) | Qué tiene que hacer todo servicio, en cualquier lenguaje |
| [docs/modules.md](docs/modules.md) | Layouts de módulo, HTMX, tipos de servicio |
| [docs/datastores.md](docs/datastores.md) | PostgreSQL, Redis, Mongo, Qdrant |
| [docs/queues-and-events.md](docs/queues-and-events.md) | Jobs, streams, backends |
| [docs/frontend.md](docs/frontend.md) | HTMX, Vue, React, el generador de vistas |
| [docs/workspaces.md](docs/workspaces.md) | Muchos servicios, el gateway |
| [docs/migrations-and-tests.md](docs/migrations-and-tests.md) | Alembic, pytest |
| [docs/plugins.md](docs/plugins.md) | Escribir un plugin |
| [docs/deploy.md](docs/deploy.md) | Compose, Caddy, Dockerfile |
| [docs/skills.md](docs/skills.md) | Escribir una skill |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Decisiones y sus costos |
| [PLAN.md](PLAN.md) | Hecho, parcial, sin empezar |

## Verificar

```bash
pytest                             # 397 framework tests
ruff check src tests docs-site && ruff format --check src tests docs-site
mypy src                           # strict

bash scripts/smoke.sh              # both module layouts, HTMX, alembic, a booting service
bash scripts/smoke_contracts.sh    # a generated service passes its own contract
bash scripts/smoke_auth_k8s.sh     # auth guards routes; manifests parse
bash scripts/smoke_workspace.sh    # workspace, gateway, view patching
bash scripts/smoke_start.sh        # the default stack, end to end
bash scripts/smoke_docs.sh         # the quickstart, run exactly as written
bash scripts/smoke_go.sh           # go vet, test, build, run, curl   (needs go)
bash scripts/smoke_frontend.sh     # npm install + vite build         (needs node)
python docs-site/build.py --version latest --output site/latest
python docs-site/check.py site/latest
```

## Qué no está verificado

Dicho sin rodeos, porque un framework que exagera su cobertura es peor que uno
que admite el hueco:

- **RabbitMQ y Kafka** están escritos contra APIs documentadas pero nunca se
  probaron de ida y vuelta contra brokers reales en CI.
- **Multi-tenancy** es una convención que hace cumplir `BaseRepository`, no una
  garantía de aislamiento. Row-level security es fase 2.
- **RAG** trocea a ancho fijo y sin reranking.
- **Angular, React Native, Laravel, .NET** no se generan en absoluto.

## Licencia

MIT.
