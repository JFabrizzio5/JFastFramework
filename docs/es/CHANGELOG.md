# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versionado: [SemVer](https://semver.org/) con identificadores de pre-release
según [PEP 440](https://peps.python.org/pep-0440/). Mientras la API esté en
pre-alpha, los servicios fijan la versión exacta (`jfastframework==0.1.0a5`);
un pin de release compatible (`~=`) empieza a tener sentido en 0.2.

## Renumeración

Las entradas de abajo estaban numeradas originalmente de `0.1.0` a `0.7.0`. Esa
numeración exageraba la madurez del código. En ese momento no se había publicado nada; el formato
del archivo de workspace está por cambiar; el backend de cola de Redis no
implementa el visibility timeout que documenta su propio contrato; los backends
de RabbitMQ y Kafka nunca se corrieron contra un broker real.

Por eso el paquete vuelve a empezar en `0.1.0a1`. La historia se conserva
textual como log de desarrollo -- registra qué se construyó y cuándo -- pero
esos números nunca fueron releases y nunca fueron instalables.
`pip install jfastframework` no resuelve un pre-release sin `--pre`, así que la
herramienta de packaging impone la advertencia en vez de una frase en un README.

La madurez a nivel de subsistema vive en [STATUS.md](STATUS.md), que es el
archivo para leer antes de depender de cualquier parte de esto.

## [Unreleased]

## [0.1.0a12] - 2026-09-30

Solo correcciones. Cinco defectos que se encontraron en la primera hora de
construir un SaaS nuevo desde cero sobre 0.1.0a11, y que la suite no vio porque
cada uno necesitaba el camino de un usuario nuevo: un campo único opcional, una
imagen de producción, el frontend generado en un navegador, una subida.

### Corregido

- **La imagen generada no podía escribir en el storage local, así que se
  detenía al arrancar.** Corre como `appuser`, pero `WORKDIR` creó `/app` como
  root y `--chown` solo alcanzó a los archivos copiados. El Dockerfile ahora crea
  `/app/storage` y los discos por defecto a nombre de `appuser` (un volumen
  montado encima empieza con permisos de escritura), y un disco que aun así no
  puede crear su raíz dice qué línea agregar. Lo encontró el paso nuevo del
  smoke de compose, que corre `jfast add storage` en un proyecto generado y sube
  un archivo a la imagen construida. `jfast upgrade --check`:
  `image-cannot-write-local-storage`.
- **`--unique` sobre un campo opcional permitía una sola fila sin valor.** La
  llave generada era `NULLS NOT DISTINCT` y la regla buscaba `None`, así que el
  segundo comprobante sin UUID respondía 409. Una llave con un campo `?` ahora
  es un índice único parcial (`WHERE campo IS NOT NULL`), la regla ignora el
  valor vacío y las pruebas generadas lo cubren. Verificado en PostgreSQL. Las
  tablas ya creadas conservan su restricción: `jfast upgrade --check` las lista
  (`unique-key-on-optional-field`) con la migración que hay que escribir.
- **Al extra `storage` le faltaba `python-multipart`.** FastAPI no importa una
  ruta con `UploadFile` sin él; en desarrollo funcionaba porque el extra `dev`
  lo trae, y la imagen de producción construida desde `requirements.txt` no
  arrancaba. El smoke de compose ahora sube un archivo a esa imagen.
- **El frontend generado no podía llamar a su API en desarrollo.** Dos orígenes
  (:8610 y :8600) y ningún `cors_origins`: el navegador bloqueaba la primera
  petición. `jfast start`, `jfast new service` y `jfast workspace env` agregan
  los orígenes de desarrollo de los frontends a `[app] cors_origins` de la API,
  sin quitar nunca uno que alguien configuró.
- **El cliente de API generado convertía las subidas en JSON.** Forzaba
  `Content-Type: application/json` y axios serializaba un `FormData` como
  `{"archivo":{}}`. Se quitó; axios manda los objetos como JSON por sí solo.
- **Un handler de `@task` o `@subscribe` no podía llegar a `llm`, `storage` ni
  al outbox.** Recibía el payload y una `TaskSession`, nada más, y los docs solo
  mostraban `request.app.state.jfast.require(...)` -- que un worker no tiene --,
  así que cada proyecto guardaba su propia copia global del contexto. Un
  parámetro anotado `TaskContext` (de `jfastframework.tasks`; es `AppContext`, y
  esa anotación también sirve) ahora recibe el contexto de la app que está
  corriendo, igual en `jfast worker` que en la API. Verificado con un proceso
  real de `jfast worker` que corre una task y un suscriptor que le piden
  providers.
- **`jfast dev` anunciaba el frontend en :5173 mientras Vite corría en 8610.**
  El script `dev` generado fija el puerto del workspace y `jfast dev` imprimía el
  default de Vite; `--web-port 8610` además corría `vite --port 8610 --port
  8610`. La URL anunciada ahora se lee del frontend (su script `dev`, luego
  `vite.config`, luego 5173), y `--port` solo se pasa cuando cambia algo.

## [0.1.0a11] - 2026-09-30

Sale de construir dos servicios reales sobre 0.1.0a10, Cuadra y Dictamen, y de
lo que tuvieron que rodear. El peor hallazgo rompía una regla que este proyecto
no había escrito: los contratos le decían a un módulo que publicara un evento
en vez de llamar a otro módulo, y en el stack por defecto ese evento no llegaba
a ningún lado -- `outbox.publish` respondía 201 y la fila se reintentaba hasta
morir. Así que este release trata sobre todo de que el camino recomendado sea
el que funciona: eventos que llegan a un suscriptor sin broker, un worker que
existe, cuentas con las que un SaaS puede abrir, un paso de un cliente a
varios que es un comando y no una cacería, un deadline en cada llamada a algo
que se puede colgar, y código generado que pasa sus propios controles.

Para cada cambio incompatible de abajo, corre `jfast upgrade --check`: lista
los que aplican a tu proyecto, con archivo y línea, y el arreglo.

### Cambios incompatibles

- **Un header `X-Tenant-ID` suelto ya no es un tenant (seguridad).** El
  middleware de observability lo copiaba a `request.state.tenant_id` cuando
  nada más había resuelto uno, y `current_tenant`, la sesión con RLS y cada
  `Job` o `Event` creado en la petición confiaban en ese valor: con `auth`
  activo y `tenancy` apagado, una petición anónima con `X-Tenant-ID: victim` se
  atendía como el tenant `victim` (un usuario con sesión no podía cambiarse: el
  token ganaba). El header ahora es solo un campo del log, `tenant_claimed`. Un
  servicio detrás de un gateway confiable que lo pone declara `header` en
  `[plugin.tenancy] sources`. Encontrado al replicar tenancy en el scaffold de
  Go. `jfast upgrade --check`: `tenant-header-not-a-tenant`.

- **`outbox.publish` rechaza un evento que nadie va a recibir.** Sin ningún
  módulo con `@subscribe` al tipo del evento y sin bus configurado, lanza
  `UndeliverableEvent` -- un 500 cuyo detalle dice cómo arreglarlo, sin
  escribir nada -- en vez de responder 201 y reintentar la fila hasta que
  muera. Declara un suscriptor (abajo) o habilita `events`. Falla en la
  petición, no al arrancar: nada compara los `publishes` declarados contra los
  suscriptores al inicio.
- **Un `Outbox(...)` construido a mano necesita `events=<bus>`** para escribir
  filas de evento.
- **`contracts check` tiene reglas nuevas.** `orphan-subscription` (un
  suscriptor a un evento que nadie publica), `undeclared-event` (un módulo que
  publica un tipo que su `[modules.x] publishes` no lista) y
  `unused-dependency` (un `depends_on` que nada usa, reportado en su línea de
  `contracts.toml` y eximible ahí). `undeclared-dependency` ahora cubre
  `Job(task="<task de otro módulo>")`, que además cuenta como arista para
  `module-cycle`: el acoplamiento escondido por el que pasaba el parche de
  Cuadra. Los proyectos existentes pueden fallar hasta declarar `publishes` y
  quitar entradas de `depends_on` que ya no se usan. Los contratos generados
  antes de este release no tienen el bloque `[layers.tasks]`, y en el layout
  screaming un `tasks.py` que importa `use_cases` se reporta hasta agregarlo.
- **`Worker.run()` espera como máximo `drain_timeout` (25 s) al detenerse**, y
  luego devuelve a la cola los jobs que siguen corriendo sin gastar un
  intento. Pasa `drain_timeout=` para jobs que necesiten más al apagar.
- **`POST /auth/login` puede responder un reto de MFA** --
  `{mfa_required | mfa_enrollment_required, mfa_token, expires_in}` -- en vez
  de un par de tokens cuando `mfa = true`. Con `email_verification =
  "required"`, `POST /auth/register` responde 202 sin tokens, y los usuarios
  existentes quedan sin verificar hasta que los marques (`UPDATE jfast_users
  SET email_verified_at = created_at WHERE email_verified_at IS NULL`).
- **Los frontends generados son privados por defecto** (`PUBLIC_BY_DEFAULT =
  false`) cuando algún backend del workspace habilita `accounts`, y piden
  `/auth/account` después del sign-in en vez de leer al usuario de la
  respuesta del login. Los dos `LoginView` de nexora se reemplazan por un
  `LoginView` compartido dentro de un `AuthShell` por look.
- **El arranque rechaza configuraciones inválidas** en los settings de
  `database`, `cache`, `storage`, `mail`, `auth` y `queue`, una prueba por
  regla. Entre ellas: `pool_size = 0` (SQLAlchemy lo lee como ilimitado), un
  `session_timezone` desconocido, una plantilla de DSN por tenant sin
  `{tenant}`, un `visibility` de storage que no es `public` ni `private` (un
  error de dedo volvía privado un disco sin avisar), `access_key` sin
  `secret_key`, un nombre de cola de PostgreSQL que no es identificador SQL
  (se interpola en el SQL), el modo `public_key` con `issue_tokens`,
  algoritmos que PyJWT no conoce, un `from_email` sin `@`; en producción, un
  `jwks_url` http, un secreto HMAC o llave de firma de menos de 32 bytes y el
  `guest` por defecto de RabbitMQ.
- **Los comandos de Redis vencen a 1 s** (`[plugin.cache] command_timeout`).
  Súbelo para scripts Lua largos o `SCAN`. Los comandos bloqueantes y pub/sub
  quedan fuera.
- **Una base caída responde 503, no 500 ni se cuelga.** Las fallas al
  conectar, las conexiones perdidas y un pool lleno lanzan
  `DatabaseUnavailableError`; cualquier otro error de base sigue siendo 500.
- **La revocación de tokens falla abierta por defecto.** Una caída de Redis
  respondía 500 en toda petición autenticada; ahora el token se acepta sin
  revisar la revocación, con un aviso espaciado y `/ready` degradado.
  `revocation_fail_open = false` responde 503. Es un compromiso, no un
  arreglo gratis: durante la caída un token revocado sigue sirviendo hasta que
  vence.
- **`jfast check --ci` falla en un servicio con `rag` activo, `tenancy`
  apagado y `tenant_scoped` distinto de false**
  (`tenancy-rag-scoped-without-tenancy`, media). `jfast check` a secas sigue
  saliendo con 0. Pon `tenant_scoped = false`, o corre `jfast tenancy enable`.
- **Los módulos hexagonales ya no importan FastAPI desde el init del
  paquete.** La plantilla hexagonal de 0.1.0a10 importaba `CreatePayload`
  desde el adaptador HTTP al inicio de `__init__.py`, así que importar el
  dominio arrastraba FastAPI y el test generado del propio módulo fallaba. El
  import se difiere con `__getattr__`; los módulos generados por 0.1.0a10
  necesitan el mismo cambio (`hexagonal-eager-create-payload`).

### Agregado -- eventos y trabajo en segundo plano

- **Eventos de dominio locales y durables.** `@subscribe("<tipo>")` en
  `modules/<nombre>/tasks.py`; `outbox.publish` encola un job por suscriptor
  en la transacción que publica, así que un evento existe si y solo si sus
  filas se confirmaron. Los ids de job son deterministas por evento y
  suscriptor, así que publicar dos veces encola a cada suscriptor una vez. Los
  suscriptores se emparejan por tipo de evento, nunca por tópico; la entrega
  es al menos una vez, y un suscriptor que recibe una `TaskSession` reclama el
  evento en su propia transacción -- un efecto por suscriptor. Con un bus
  configurado el evento también va a Kafka.
- **`@task` declarado en el módulo dueño** (`jfastframework.tasks`),
  descubierto desde `modules/<nombre>/tasks.py` igual por la API que por el
  worker; un `tasks.py` roto detiene el arranque. **`TaskSession`** es una
  sesión en la base primaria con el tenant del job (respeta RLS), con commit
  al regresar y rollback ante error. **`idempotent_on=`** reclama una llave
  con `claim_once` en la transacción del handler.
- **`jfast worker`** arranca el lifespan de la app y corre las tasks y
  suscripciones de cada módulo. SIGTERM deja de reclamar y drena durante
  `--grace` segundos (25); lo que queda se libera sin gastar un intento; una
  segunda señal libera de inmediato. **`jfast dev` lo arranca** cuando la cola
  está activa (`--no-worker`). Los compose generados (servicio y workspace)
  traen un servicio `<svc>-worker` y Kubernetes un Deployment `<svc>-worker`.
- **`jfast jobs dead` y `jfast jobs retry <id...> | --all`** para las colas
  de PostgreSQL y Redis. RabbitMQ recibe un mensaje que apunta a su consola de
  administración.
- **Eventos en el contrato.** `[modules.x] publishes`; las suscripciones, el
  dueño de cada task y las referencias `Job(task=...)` se leen del código.
  `contracts show --json`, `CONTRACTS.md` y `jfast ai context` listan
  publicadores, suscriptores, tasks y las funciones de la fachada de cada
  módulo.
- **`Job.trace` y `Event.trace`** llevan el contexto de traza W3C junto al
  tenant y al request id; los spans de jobs y handlers de eventos son spans
  consumer dentro de la traza de la petición que los creó.

### Agregado -- telemetría

- **Plugin `telemetry`** (`pip install "jfastframework[telemetry]"`): trazas
  de OpenTelemetry por OTLP a Jaeger, Tempo, Honeycomb, Datadog o un
  Collector. Spans para cada petición (plantilla de ruta, nunca el path
  crudo), cada sentencia SQL (nunca los parámetros; el texto solo con
  `record_sql_statement = true`), cada llamada del cliente `http` y del
  gateway, `llm.chat`/`llm.embed` con tokens y costo, `rag.ingest`/`rag.search`
  con conteos, jobs y handlers de eventos. **Nunca texto de prompts,
  documentos, respuestas, consultas ni bodies**, impuesto en los puntos de
  llamada y otra vez en el backend, que descarta todo atributo que no sea un
  escalar pequeño.
- **Gratis hasta configurarlo.** Sin `OTEL_EXPORTER_OTLP_ENDPOINT` no se
  instala nada -- ni middleware, ni listener de SQL, ni tracer -- y
  OpenTelemetry ni siquiera tiene que estar instalado. Medido en proceso: 41
  us por petición sin el plugin, 41 con él y sin endpoint, 66 exportando a
  memoria. `/ready` reporta el exportador y nunca es crítico.
- **Una traza a través de servicios.** El cliente `http` manda `traceparent`
  y `tracestate` en cada llamada dentro de un span client propio; el gateway
  reemplaza el header de quien llama por el de su propio span, y lo pasa
  intacto cuando la telemetría está apagada; las peticiones entrantes
  continúan la traza de quien llama. `sample_ratio` decide en el borde y cada
  servicio detrás lo sigue.
- **`include_infra = true`** agrega un OpenTelemetry Collector y Jaeger al
  compose generado -- un par para todo un workspace.
- `tracing.span(...)` y `tracing.inject()` para spans propios y clientes
  crudos, gratis con la telemetría apagada; el provider se registra como el
  global de OpenTelemetry cuando nadie registró uno antes.

### Agregado -- cuentas

- **Verificación de email** (`email_verification = "off" | "optional" |
  "required"`) y **recuperación de contraseña por correo**, que termina todas
  las sesiones, tokens de acceso incluidos. Los tokens se guardan como hash
  SHA-256, de un solo uso y con vencimiento. "Olvidé" y "reenviar" responden
  202 antes de buscar la dirección, y en modo `required` registrarse con una
  dirección ocupada recibe la misma respuesta que una nueva.
- **MFA TOTP** (RFC 6238, solo biblioteca estándar) con códigos de
  recuperación y protección contra repetición, obligatorio por rol
  (`mfa_required_roles`) al entrar, al refrescar y al desactivar; el secreto se
  cifra en reposo. Un sign-in social de una cuenta con MFA también recibe el
  reto. Los códigos incorrectos cuentan para los intentos del token de MFA y
  para el bloqueo de la cuenta.
- **Sign-in con límite por defecto** cuando `cache` está activo: token
  buckets por IP, por cuenta y para peticiones de correo, 429 con
  `Retry-After`.
- `POST /auth/logout/all`, `GET /auth/features` y un reset de MFA por un
  admin (`DELETE /accounts/users/{id}/mfa`). Desactivar a un usuario termina
  sus sesiones de inmediato. Los errores que un frontend debe distinguir
  llevan un `code`.
- **Las tablas del framework reciben columnas nuevas al arrancar**
  (`ensure_columns`), así que una tabla `jfast_users` de 0.1.0a10 se
  actualiza en su lugar.
- **Los frontends Vue y React generados hablan con accounts:** sign-in con el
  paso de MFA, registro, verificación de email, olvido y reset de contraseña,
  inscripción de MFA y una página de Seguridad; `VITE_API_TIMEOUT` (60 s por
  defecto, para llamadas de IA). Sin ningún backend con `accounts`, el
  frontend queda público y sin esas páginas.
- El servicio no arranca con verificación o reset activos y sin el plugin
  `mail` o sin `frontend_url`, con `mfa_required_roles` y `mfa` apagado, ni con
  `mfa` activo y sin `JFAST_ENCRYPTION_KEYS`.

### Agregado -- un cliente hoy, varios mañana

- **`jfast check` suma un séptimo check, `tenancy`**: ajustes de tenant que se
  contradicen entre sí o con el código -- rag con scope sin tenancy,
  presupuesto por tenant sin tenant, `rls = true` sin nada que ponga el
  tenant, políticas en revisiones con `rls = false`, `current_tenant` sin
  fuente, fuentes que nunca resuelven. Cada hallazgo nombra el arreglo.
- **`jfast check --multitenant-ready`**: qué rompería pasar a varios
  clientes, con archivo y línea -- `tenant_id=None`, rutas y factories de
  servicio sin dependencia de tenant, SQL crudo, llaves de storage y cache,
  RAG sin scope, tareas programadas, llamadas a LLM. Heurísticas,
  documentadas con sus puntos ciegos; una regla que no puede decidir se calla.
  Se eximen con `# contracts: allow <razón>`; `--json`.
- **`jfast tenancy enable --tenant <id>`**: una revisión de Alembic revisable
  (relleno, `--not-null` opcional, RLS por tenant, fragmentos de RAG
  re-asignados) más la edición de `jfast.toml` conservando sus comentarios, y
  luego los pasos manuales que quedan. `--dry-run` no escribe nada.
  Verificado de punta a punta en un servicio generado: con un rol sin
  `BYPASSRLS`, PostgreSQL no le muestra a un segundo tenant ninguna fila del
  primero y rechaza sus escrituras.
- **`[plugin.database] pgbouncer = true`** para pooling por transacción: sin
  caché de statements de asyncpg ni de SQLAlchemy, nombres únicos; los
  engines por tenant lo heredan. RLS detrás de PgBouncer 1.25 en modo
  transacción está verificado por `tests/test_rls_pgbouncer.py`: el tenant no
  sobrevive a la siguiente transacción de otro cliente en el mismo backend, y
  cincuenta transacciones intercaladas de dos tenants en un backend nunca se
  ven entre sí.
- **`jfast init` pregunta si la app sirve a varios clientes**, y la respuesta
  ajusta cada pieza de forma consistente: tenancy y sus fuentes, auth,
  `rag.tenant_scoped`, `llm.tenant_budget_usd` y rutas generadas sobre
  `current_tenant`. `jfast start` es de un solo tenant por defecto;
  `--multitenant` genera `JFAST_AUTH_SECRET` y una contraseña del primer admin
  en `.env` para que `jfast check --ci` pase. Las columnas `tenant_id` se
  quedan en ambos casos.

### Agregado -- deadlines, breakers y simulacros de falla

- **Toda llamada externa tiene deadline y breaker por defecto**, cada uno un
  setting con su razón en el código. PostgreSQL: 10 s para conectar (el de
  asyncpg es 60), un ping del pool acotado a 2 s que reemplaza al de
  SQLAlchemy (que no tenía deadline y colgaba peticiones), un breaker de
  conexión. Redis: 2 s para conectar, 1 s por comando, un breaker. JWKS: 5 s
  para el intento completo, un reintento solo ante 429, 5xx y errores de red,
  un breaker, y quienes esperaban detrás de una descarga fallida comparten su
  falla. S3/MinIO: timeouts, reintentos y breaker configurables por disco; un
  404 nunca cuenta.
- **`/ready` nunca falla por un plugin no crítico.** Auth queda degradado, no
  no disponible, mientras sirve claves JWKS cacheadas; un object store queda
  degradado; una falla del disco local es crítica.
- **`get_or_set` falla abierto en una sola ida y vuelta** con Redis caído.
- **El correo rechazado con 5xx, demasiado grande o malformado va a dead
  letters de inmediato;** los timeouts y los 4xx conservan sus reintentos.
- **Simulacros de falla**, `tests/test_failure_drills.py`: PostgreSQL y Redis
  pausados (`docker pause`: el socket sigue abierto y nadie contesta) y el
  proveedor de identidad colgado. Cada uno revisa el status dentro de su
  deadline, qué nombra `/ready` y la recuperación sin reiniciar. Los tiempos
  están en `docs/resilience.md`, nueva.

### Agregado -- código generado que pasa sus propios controles

- **Los servicios generados pasan ruff, ruff format, `mypy --strict` y
  pytest** en cada layout y forma: cada uno trae un `ruff.toml` que nombra sus
  reglas (con `Depends` y compañía de FastAPI permitidos como llamadas por
  defecto) y un `mypy.ini` estricto. `scripts/smoke_generated_quality.sh`
  genera `jfast start` de un tenant y multitenant y `jfast new service` con 19
  plugins, un módulo por layout en cada forma, y los revisa todos.
- **`jfast new module --fields "..." --unique "..." --bare`** en los cuatro
  layouts: int, bigint, `str(N)`, text, bool, float, `decimal(P,S)`, money
  (unidades menores enteras), date, datetime con zona horaria y json; `?` para
  nullable, `=valor` para un default. Entidad, modelos con los mismos límites,
  finders del repositorio, unicidad al crear y al actualizar, reglas de
  dominio, puerto y adaptador, DTO de `public.py`, README y tests salen de los
  campos. `--access open|auth|tenant` elige cómo se protegen las rutas; su
  default sigue a `jfast.toml`. `--ui htmx` se rechaza junto con `--fields` o
  `--bare`.
- **`jfast init` lista los plugins desde el catálogo**, así que uno nuevo ya
  no se puede quedar fuera; `telemetry` y `queue` vienen recomendados y
  premarcados. `jfast start --no-telemetry`.
- **`jfast add <plugin>` y `jfast remove <plugin>`**: `add` lo habilita junto
  con lo que requiere, fija el extra, agrega su bloque de settings e imprime
  las variables de entorno, conservando los comentarios; `remove` se niega
  mientras otro plugin lo requiera.
- **Los módulos traen `tasks.py`** y los contratos generados una capa `tasks`.
- **`scripts/smoke_upgrade.sh`**: el release anterior desde PyPI, un proyecto
  generado con él, el wheel de este checkout encima, y exactamente los códigos
  de `scripts/smoke_upgrade.expected` desde `jfast upgrade --check`.
- **`JFAST_TEST_WINDOWS_CLOCK=1`** redondea `time.monotonic` a 1/64 s, como
  Windows, para toda la suite. Contra el código de JWKS anterior al arreglo
  falló 16 de 30 corridas; con él activo, las suites de concurrencia pasaron
  10 de 10.

### Agregado -- servicios en Go en un workspace de Python

"Contrato sí, framework no": el scaffold de Go recibe lo que un servicio en Go
necesita para convivir con los de Python -- sigue siendo solo biblioteca
estándar, todo middleware de `net/http` plano que envuelve tal cual un engine
de Gin, Echo o Chi.

- **Los servicios en Go propagan el trace context.** `traceparent`/`tracestate`
  se validan (W3C versión 00; uno inválido se descarta con su state), se
  loguean como `trace_id` y se pasan sin cambios, junto con `X-Request-ID`,
  con `jfast.Propagate` / `jfast.PropagatingTransport`. No se crean spans; el
  README muestra `otelhttp` como decisión del usuario.
- **Los servicios en Go verifican los JWT del workspace y resuelven el
  tenant**, con los nombres de variables y las reglas de los plugins de
  Python: `JFAST_AUTH_*` en modo `secret` (HS256/384/512) o `public_key`
  (RS256/384/512, ES256/384), algoritmos fijados por configuración,
  `exp`/`iat`/`sub` obligatorios, `nbf`, `iss` y `aud` verificados con el
  mismo leeway de 30 s, refresh tokens rechazados como bearer, y los mismos
  rechazos al arrancar; fuentes `JFAST_TENANCY_*` en el mismo orden de
  confianza. `RequireAuth`, `RequireScopes`, `RequireRoles` (401/403) y
  `RequireTenant` (401 sin sesión, 403 sin tenant), con `ClaimsFrom(ctx)` y
  `TenantFrom(ctx)`; el tenant va en el access log. Apagado salvo que se
  configure. El modo `jwks` no arranca y nombra una librería. **La revocación
  no se consulta**: un access token revocado sirve contra un servicio en Go
  hasta que vence (15 minutos por default); el servicio lo dice en cada
  arranque.
- El módulo de ejemplo separa su store por tenant, y funciona igual con auth
  y tenancy apagados.
- **El formato de la cola de PostgreSQL está documentado para otros
  lenguajes** (`docs/es/service-contract.md`, "Consumir la cola desde otro
  lenguaje"): las columnas y estados de `jfast_jobs`, las sentencias de
  claim, ack, reintento, muerte y release, el sobre del evento, y lo que un
  consumidor debe hacer para ser seguro. Un formato documentado, no un
  cliente soportado: no hay worker de Go.
- `tests/test_go_service.py`: los tokens que emite el propio `TokenIssuer`
  del plugin auth (HS256 y RS256) los acepta el middleware del servicio
  generado bajo un toolchain de Go real y resuelven el mismo tenant que
  resuelve una app JFast; refresh y vencidos reciben 401 de los dos lados; un
  `traceparent` llega sin cambios a la llamada saliente del servicio en Go.
  `scripts/smoke_go.sh` además corre el binario con tokens emitidos por
  Python (aislamiento por tenant, `trace_id` y `tenant_id` en el log, `jwks`
  rechazado) y revisa `gofmt`.

### Agregado -- CI

- El job `go` también corre la prueba entre lenguajes de arriba.
- Jobs para los controles de proyectos generados, el smoke de actualización,
  las suites de concurrencia con un reloj de grano Windows, RLS detrás de un
  PgBouncer fijado (`edoburu/pgbouncer:v1.25.2-p0`), y spans exportados por
  OTLP y leídos de vuelta desde Jaeger (`scripts/smoke_telemetry.sh`); el
  presupuesto de rendimiento contra la base del cambio en el mismo runner; el
  disco S3 contra MinIO; la suite de varias réplicas. Los
  simulacros de falla corren al final del job principal, pausando su propio
  PostgreSQL y Redis. Las suites de eventos locales, el worker, dead letters,
  flujos de cuentas, spans de SQL y el paso a multitenant están en la lista
  que tumba el build si se saltan.

### Cambiado

- `Event` se movió a `jfastframework.events` (se sigue pudiendo importar
  desde `plugins.builtin.events`) y hereda `tenant_id` y `request_id` del
  contexto, como `Job`. Los handlers `@on` de Kafka corren con el tenant, el
  request id y la traza del evento.
- `module-cycle` de `jfast inspect` usa los `depends_on` declarados además de
  los imports, como `contracts check`; no coincidían.
- La cola de PostgreSQL agrega una columna `trace` al arrancar (`ADD COLUMN IF
  NOT EXISTS`; el rol de la app necesita `ALTER` sobre `jfast_jobs`) y guarda
  el error de un job fallido.
- El contador de fallos de accounts solo se limpia al emitir una sesión. Una
  contraseña correcta lo reiniciaba, así que quien tuviera la contraseña
  recibía una tanda nueva de intentos de MFA en cada sign-in.
- El timeout de axios del frontend generado es de 60 s (`VITE_API_TIMEOUT`).
- `--with accounts` también habilita `auth`; el generador enciende lo que
  requiera un plugin elegido.
- `jfast new enum` escribe `StrEnum`. Reescribir el `.env` de un servicio
  conserva las llaves que no son del grafo del workspace.
- Más silencioso por defecto: el aviso de "no signing key" de storage solo
  aparece en producción (`temporary_url()` sigue fallando con el arreglo), y
  el aviso del token store en memoria de auth es info fuera de producción.
- `docs/modules.md` ya no recomienda `outbox.publish` más `@on` de Kafka entre
  módulos -- el callejón sin salida -- sino `publishes` más `@subscribe`.

### Corregido

- **El arranque de cada proceso tomaba locks exclusivos y `/ready` oscilaba.**
  La imagen generada corre un proceso de uvicorn por CPU, y cada uno ejecuta el
  arranque de sus plugins. La cola corría `ALTER TABLE jfast_jobs ADD COLUMN IF
  NOT EXISTS trace` en cada arranque, y pgvector sus sentencias de upgrade: las
  dos toman un lock ACCESS EXCLUSIVE antes de notar que no hay nada que hacer,
  así que un proceso que arrancaba quedaba en fila detrás de cualquier lectura
  abierta, y cada consulta -- incluido el `/ready` de otro proceso -- detrás de
  él (el smoke de compose vio 200 y luego 503). El trabajo de esquema ahora
  consulta primero el catálogo y solo ejecuta lo que falta, bajo un advisory
  lock de transacción, lo que también arregla que `CREATE TABLE IF NOT EXISTS`
  concurrente fallara en una base vacía cuando arrancan varios procesos a la
  vez. `tests/test_startup_ddl.py`.
- **Las escrituras del store de pgvector recorrían la tabla completa.**
  Buscaban las filas de un documento con `tenant_id IS NOT DISTINCT FROM`, que
  ningún btree sirve: borrar un documento tomaba 19.2 ms en vez de 0.03 ms con
  300k fragmentos, y crecía con la tabla. Ahora usan `tenant_id = :tenant` (o
  `IS NULL` en un store sin tenant); `tests/test_rag_scale.py` se lo pregunta
  al planner.

- **`/ready` del outbox decía `ok` con mensajes fallando.** Queda degradado
  desde el primer intento fallido y cita la última razón de muerte; la línea
  de log del relay trae la causa; una fila imposible de entregar muere en su
  primer intento con su razón.
- **Los contenedores del compose de workspace leían la dirección del host.**
  Un servicio con Kafka, RabbitMQ o MinIO no recibía `client_env` en `jfast
  workspace compose`, caía a su `.env` y marcaba a `localhost` -- a sí mismo.
  Regenera el compose.
- **`Job(task="alerta.x")` desde otro módulo pasaba `contracts check`** cuando
  la task estaba registrada en un `worker.py` en la raíz (la forma de
  0.1.0a10), porque ningún `@task` la declaraba. Ahora el prefijo de módulo
  del nombre de la task nombra a su dueño. Encontrado al migrar Cuadra.
- **`--multitenant-ready` reportaba SQL crudo cuyo filtro se interpola después
  de `WHERE`/`AND`** -- un helper del repositorio que sí filtra por tenant.
  Eso no se puede decidir desde el código, así que la regla se calla. Cuadra
  tenía ocho hallazgos, todos falsos.
- **La telemetría propia de FastAPI 0.142 corría junto al plugin.** Al ver
  `OTEL_EXPORTER_OTLP_ENDPOINT` configuraba un segundo provider global
  (`unknown_service`), duplicaba cada span de servidor y habría exportado logs
  con mensajes de excepción y valores de entrada rechazados. `create_app` la
  apaga siempre que FastAPI tenga el interruptor; los spans propios de FastAPI
  no se registran, a propósito. Encontrado al trazar Cuadra hacia Jaeger.
- **`jfast add` reinstalaba el release anterior encima del que corría.** Un
  `requirements.txt` que seguía fijando 0.1.0a10 hacía que `jfast add
  telemetry` corriera `pip install -r` y pusiera a10 -- que no trae ese extra
  -- encima de a11. Ahora pip se omite, con el comando a correr, cuando el pin
  difiere de la versión en ejecución o la instalación es editable. Encontrado
  al migrar Cuadra.
- **`listing(limit=1500)` del disco S3 regresaba 1.000.** Ahora sigue el
  continuation token.
- La documentación en español enlazaba tres anclas con acento que el sitio
  quita; arreglado, junto con la de la nueva página de telemetría.

### Rendimiento y escala, medidos

Cada número de aquí está en `docs/scaling.md`, nueva, con la máquina donde se
midió (una laptop Apple M5 con otras suites corriendo) y el script que lo
produce.

- **Un presupuesto de rendimiento.** `scripts/bench_overhead.py` maneja cada
  app en proceso y mide el tiempo de CPU del proceso por petición como
  proporción contra FastAPI solo en la misma corrida -- los defaults de JFast
  2.48x, auth + tenancy + metrics 5.43x -- y `tests/test_performance_budget.py`
  (`JFAST_PERF_BUDGET=1`) falla si una proporción crece más de 20 % sobre la
  línea base. Una segunda prueba demuestra que muerde: un `BaseHTTPMiddleware`
  de vuelta, la regresión de 0.1.0a10, lo tumba.
- **`jfast bench <url>`**: una prueba de carga por escalones armada desde el
  OpenAPI del servicio. Reporta req/s, p50/p95/p99 y errores por escalón,
  dónde se rompe el servicio (`--max-p99-ms`, `--max-error-rate`) y dónde deja
  de crecer el throughput, y cualquier check de `/ready` que se degradó;
  `--k6` exporta el escenario, `--json` y `--fail-on-break` son para CI. Su
  propio generador llega a 3.000-3.600 req/s; arriba de eso usa `ab` o k6.
- **Las rutas del framework se prueban después de las de la aplicación.**
  `/health`, `/ready`, `/info`, `/metrics` y la documentación pasan detrás de
  las rutas de la app al arrancar, lo que ahorra 2-4 us de CPU por petición de
  la app (medido; los "~8 us" del plan no). Una ruta de la app que reclamaría
  uno de sus paths, completo o con un 405, sigue sin hacerlo: cada uno se
  prueba y vuelve delante de ella. `app.routes` y `/openapi.json` listan
  primero los paths de la aplicación.
- **El disco S3 está verificado contra MinIO**
  (`tests/test_storage_minio.py`): put, get, stat, listado, URLs firmadas
  descargadas y luego vencidas, subidas prefirmadas, un stream multipart de
  tres partes y su aborto, salud.
- **Garantías con varias réplicas, demostradas.** Dos apps y dos workers
  contra un PostgreSQL y un Redis (`tests/test_multi_replica.py`): 400
  mensajes del outbox reenviados y consumidos una vez cada uno; los ticks del
  scheduler encolados una vez en ambos stores; un tope LLM de $1.00 entre las
  dos réplicas deja pasar exactamente 10 de 60 llamadas concurrentes de $0.10;
  un logout en una réplica se rechaza en la otra, y una carrera de refresh
  entre ellas rota una sola vez.
- **`jfastframework.db.rollups.MonthlyRollup`**: totales mensuales por tenant
  precalculados, refrescados un bucket a la vez desde sus filas, así que un
  evento procesado dos veces no hace daño; serializado con un advisory lock.
  Sobre 3M de filas, el panel de seis agregados de un tenant de 990k filas
  pasó de 455 ms a 3.25 ms en p50.
- **RAG con 300k fragmentos entre 1.000 tenants** (`scripts/bench_rag.py`):
  búsqueda vectorial p50 2.54 ms, p99 6.92 ms; híbrida p50 2.96 ms. A ese
  tamaño el planner sirve las consultas por tenant con el btree del tenant y
  un orden exacto, así que el índice HNSW de 586 MB no sirve ninguna; con
  cuatro tenants grandes sí, y el recall@10 es 0.918 con el `ef_search` por
  defecto (0.950 con 200).

### Sin hacer, y con nombre

- **Sin correr de verdad:** el camino de Kafka contra un broker; eventos
  locales sobre la cola de Redis o RabbitMQ (probados con una cola que
  graba); los manifiestos de Kubernetes del worker (parseados, nunca
  aplicados); `jfast dev` lanzando el worker (simulado); un simulacro de falla
  de RabbitMQ; S3 o MinIO (solo dobles); SMTP (la separación 5xx/4xx se prueba
  contra excepciones de `smtplib`); un backend de trazas hospedado o TLS hacia
  el collector; las páginas de cuenta generadas en un navegador; `jfast init`
  interactivo de punta a punta; `--not-null` aplicado contra PostgreSQL;
  réplicas de lectura detrás de PgBouncer bajo carga.
- **Límites:** un worker haciendo long-poll contra un Redis pausado espera en
  el socket (BLMOVE y pub/sub no tienen deadline por comando); un proveedor de
  identidad caído sin ninguna clave cacheada sigue respondiendo 401, no 503;
  `TaskSession` siempre usa la base primaria, no las de cada tenant; el worker
  no tiene endpoint de salud y no se recarga bajo `jfast dev`; RabbitMQ no
  tiene `jfast jobs`; el callback de login social del frontend no maneja el
  reto de MFA; no hay código QR para inscribir MFA; cambiar la contraseña no
  termina las otras sesiones (un reset sí); el `tasks.py` generado es solo un
  docstring.
- **Diez detectores de `jfast upgrade --check` tienen bugs conocidos**, cada
  uno fijado por un xfail estricto en `tests/test_upgrade_detectors.py` (por
  ejemplo, `async-dependencies` marca cualquier llamada llamada
  `current_tenant`, y `SKIP_DIRS` se compara contra rutas absolutas, así que
  un proyecto dentro de una carpeta llamada `build` no se lee).
- **El smoke de actualización falla hasta que `upgrades.py` nombre
  `hexagonal-eager-create-payload`**, a propósito.
- Los tiempos de los simulacros se midieron en una sola laptop cargada; un
  runner de CI más lento no está probado. Los controles del generador solo se
  corrieron con Python 3.12 en local.
- **Escala, todavía sin medir:** RAG con 1M de fragmentos (todos los
  embeddings fueron sintéticos); la tabla de `ab` en `docs/deploy.md` (no se
  volvió a medir en una máquina cargada); una línea base de Linux para el
  presupuesto, que CI mide contra la rama base en su lugar -- un paso que aún
  no corre en CI; el plugin de telemetría como escenario del presupuesto;
  `jfast bench` nombrando la dependencia saturada más allá de `/ready`, y un
  escenario con modelo simulado.
- **Encontrado, sin arreglar:** la respuesta problem+json de un 405 pierde el
  header `Allow`.
- **El job de MinIO en CI corre un fork de la comunidad** (`pgsty/minio`):
  MinIO dejó de publicar imágenes, y la suite solo se verificó en local contra
  la última oficial, `RELEASE.2025-09-07T16-13-09Z`.
- Trabajo largo de IA por la cola por defecto, recetas probadas y las tablas
  con RLS en `jfast ai context` no se empezaron.
- **Go, sin verificar:** un servicio en Go detrás del gateway o en `jfast
  workspace compose` con auth encendido (los valores le llegan solo por su
  propio `.env`, igual que a uno de Python); ES256/ES384 contra tokens
  emitidos por Python (solo emitidos por Go); tokens de un proveedor de
  identidad real; un consumidor en Go de la tabla de la cola (solo
  documentado); el envoltorio de Gin/Echo/Chi que muestran los docs (no se
  compila aquí, porque el scaffold no tiene dependencias con qué probarlo). La
  prueba entre lenguajes y el smoke extendido corrieron en local en
  `golang:1.23`, todavía no en CI.

## [0.1.0a10] - 2026-09-29

Tres cosas que un proyecto deja atrás en su primer mes de uso real: una
recuperación que mezclaba tenants, módulos que se hablaban por SQL crudo porque
los contratos prohibían cualquier otra forma, y un modelo de tenants sin lugar
para "cada cuenta es la suya".

### Cambios incompatibles

- **El store de rag se niega a trabajar sin tenant.** La identidad de un
  fragmento era `(document_id, chunk_index)`: dos tenants con un `contrato-1`
  se pisaban, y `search(tenant_id=None)` buscaba en todos. La identidad ahora
  es `(tenant_id, document_id, chunk_index)`, cada sentencia filtra por tenant,
  y un store limitado por tenant -- el default -- lanza `TenantRequiredError`
  (403). `tenant_scoped = false` para un servicio de un solo tenant.
  `ensure_schema` actualiza en su lugar una tabla pgvector de 0.1.0a9 sin
  perder filas.
- **El router de rag viene apagado y exige autenticación al encenderlo.** No
  revisaba token y tomaba el tenant del body. Ahora `mount_router = true` exige
  el plugin `auth`, una sesión iniciada, `read_scopes`/`write_scopes` opcionales,
  y toma el tenant del plugin tenancy o del token.
- **Los ids de puntos de Qdrant incluyen el tenant.** Las colecciones escritas
  por 0.1.0a9 hay que volver a ingerirlas.
- **Los módulos se hablan por `modules/<nombre>/public.py`, declarado en
  `[modules.<nombre>] depends_on`.** Un módulo no podía importar a otro, y el
  consejo era mover la cosa a `shared/` -- correcto para un enum, incorrecto
  para comportamiento --, así que en la práctica los módulos leían las tablas
  de otros con SQL crudo que ningún chequeo veía. La fachada regresa DTOs y
  recibe la sesión de quien llama y un `tenant_id` explícito. `contracts check`
  suma `undeclared-dependency`, `module-cycle`, `public-leak` (una entidad ORM
  o FastAPI cruzando la fachada), `cross-module-sql` (un string con SQL contra
  la tabla de otro módulo) y `unknown-dependency`; `cross-module` ahora también
  atrapa imports relativos entre módulos y cada nombre de `import a, b`, y solo
  sugiere `shared/` para enums y tipos. `jfast upgrade --check` lista las
  líneas que cada regla reporta en un proyecto. Un contrato screaming de
  0.1.0a9 necesita el nuevo bloque `[layers.public]`.
- **`require_auth`, `optional_auth`, `current_tenant` y `tenant_zone` son
  `async def`**, igual que las dependencias que regresan
  `require_scopes`/`require_roles`. Con `Depends(...)` no cambia nada; una
  llamada directa ahora regresa una corrutina. `principal_of(request)` es la
  forma síncrona de leer al usuario. `jfast upgrade --check` lista cada llamada
  directa.
- **Las métricas se etiquetan por plantilla de ruta.** El middleware leía la
  ruta antes de que corriera el router, no encontraba ninguna y etiquetaba por
  la ruta cruda: una serie por id (`/users/41`, `/users/42`...), un registro que
  crecía sin límite ante un escáner. `endpoint` ahora es `/users/{user_id}` (con
  el prefijo de routers incluidos y montajes), `<unmatched>` cuando ninguna ruta
  coincidió, y `http_requests_in_progress` lleva solo `method`.
- **Protocolo `VectorStore`:** `delete_document` y `search` reciben
  `tenant_id`; nuevos `existing_hashes`, `sync_document` y `supports_hybrid`.
  Un store propio necesita esos métodos.

### Corregido

- **JWKS de una sola descarga en Windows.** Cincuenta peticiones que llegan
  con la caché vacía deben causar una sola descarga de las llaves del
  proveedor de identidad. Quien esperaba decidía "alguien ya descargó"
  comparando dos lecturas de `time.monotonic()`, que en Windows avanza en
  saltos de ~15.6 ms: una descarga que terminaba en el mismo tic en que empezó
  parecía no haber ocurrido, y el siguiente volvía a descargar (la CI de
  Windows vio 2 descargas; con el reloj congelado son 50). Un contador de
  generaciones que sube con cada descarga exitosa reemplaza al reloj, y una
  prueba con el reloj congelado reproduce la falla en cualquier sistema.

### Rendimiento

Medido con `ab` contra un worker de uvicorn (tabla en `docs/deploy.md#rendimiento`):
un servicio con auth, tenancy, métricas y logs pasó de **2,411 a 8,581
peticiones por segundo** en el mismo endpoint; FastAPI con JWT y tenant hechos a
mano da 9,494. JFast con sus plugins por defecto pasó de 4,228 a 12,443.

- **Todos los middlewares son ASGI puro.** Observability, métricas, auth,
  tenancy y el pin de lectura/escritura eran `BaseHTTPMiddleware`, que corre la
  app en un task group y pasa la respuesta por un canal en memoria: unos 75 us
  de CPU por petición cada uno. Mismo comportamiento, con un `send` envuelto.
- **Las dependencias del framework y las fábricas `get_service` generadas son
  `async def`.** FastAPI corre una dependencia `def` en su threadpool; ese
  salto costaba 75-85 us por petición, más que todos los middlewares juntos.
- `tests/test_performance_guards.py` falla si vuelve un `BaseHTTPMiddleware` o
  una dependencia síncrona del framework.
- `docs/deploy.md` suma una sección de Rendimiento con las cifras, lo que
  cuesta el resto y las reglas para que tu propio código siga rápido.

### Agregado

- **`public.py` en cada módulo generado**, en los cuatro layouts, con un DTO
  y un `get_<nombre>(session, *, tenant_id, <nombre>_id)` cableado al
  repositorio de ese layout; `jfast new module` además agrega
  `[modules.<nombre>] depends_on = []` a `contracts.toml`. `contracts show
  --json`, `CONTRACTS.md`, `contracts explain` y `contracts diff` conocen los
  módulos y sus dependencias. `AGENTS.md` y la skill `respect-contracts` le
  dicen a un agente: los datos de otro módulo por su fachada, las reacciones
  por el outbox, nunca SQL crudo sobre sus tablas, `shared/` solo para
  vocabulario.
- **Servicio `rag`** (`ctx.require("rag")`, `jfastframework.rag.RagService`):
  `ingest`, `search`, `delete`, todos limitados por tenant, y `format_context`
  para extractos numerados y citables. Sin dependencia de FastAPI, así que un
  worker de la cola ingiere igual que una ruta.
- **Re-ingerir solo embebe lo que cambió.** Cada fragmento lleva un hash de su
  texto y de su embedder; los que no cambiaron conservan su vector, un
  documento más corto pierde su cola, todo en una transacción. Un cambio de
  modelo vuelve a embeber en vez de mezclar espacios vectoriales.
- **HNSW en vez de IVFFlat** en pgvector. IVFFlat construido sobre una tabla
  vacía no aprendía nada y, con `probes = 1`, regresaba uno o dos hits donde
  había ocho relevantes. `hnsw.iterative_scan` en pgvector 0.8+, para que un
  filtro selectivo no deje el resultado vacío.
- **Búsqueda híbrida** en pgvector: una columna `tsvector` generada con índice
  GIN y reciprocal rank fusion con el ranking vectorial. `text_search_config`
  elige el diccionario (`spanish` hace que "entrega" encuentre "entregará").
- **Filtros:** `document_ids`, `where` (coincidencia exacta en metadata, con
  índice GIN), `min_score`.
- **Fragmentación por estructura** (`chunk_strategy = "recursive"`, el
  default): encabezados, párrafos, líneas, oraciones, palabras; un encabezado
  siempre abre fragmento. `fixed` conserva las ventanas de 0.1.0a9.
- **Row-level security en la tabla de fragmentos**: cada transacción del store
  fija `jfast.tenant_id`, así que `enable_tenant_rls(op, "rag_chunks")`
  funciona. Verificado con un rol que no es superusuario.
- **`schema_sql()`** en `jfastframework.vectors.pgvector`: las sentencias que
  corre `ensure_schema`, para una migración de Alembic con `auto_migrate =
  false`.
- **Plugin `llm` y `jfastframework.llm.LLMClient`**: chat, schemas JSON
  estrictos, imágenes y embeddings sobre cualquier API compatible con OpenAI
  (OpenAI, Azure, Ollama, vLLM, LiteLLM). Un tope de gasto por servicio y por
  tenant, por mes, día o total, **reservado de forma atómica antes de cada
  llamada y ajustado al costo real** -- las llamadas concurrentes no pueden ver
  todas "bajo el presupuesto" y pasarse juntas. La bitácora en Redis registra
  propósito, modelo, tokens, costo y latencia, nunca el prompt. Reintentos en
  408/409/429/5xx respetando `Retry-After`. Precios en una tabla, reemplazables
  en `[plugin.llm.prices]`; los modelos desconocidos se cobran caro.
  `[plugin.rag] embedder = "llm"` hace que indexar gaste del mismo presupuesto.
- **Fuente de tenancy `user`**: el id del usuario con sesión es el tenant, para
  el SaaS donde cada cuenta es dueña de sus datos. Después de `token`, así que
  unirse a una organización mueve al usuario a ella sin cambiar código.
- **Dependencia `current_tenant`** para rutas: el tenant resuelto; 401 sin
  sesión (para que un token vencido se refresque), 403 con sesión pero sin
  tenant. Nunca un header ni un campo del body.
- **Docs:** `modules.md`, `contracts.md` y `shared-and-events.md` explican la
  comunicación entre módulos con un ejemplo completo. `docs/rag.md` y
  `docs/llm.md`, nuevas, en inglés y español;
  `multitenancy.md` suma la fuente `user`, `current_tenant` y una tabla de cada
  capa de aislamiento y qué atrapa.
- El PostgreSQL de la CI es `pgvector/pgvector:pg16`, así que la suite de rag
  corre ahí.

### Cambiado

- `rag` pasa a `alpha` en STATUS.md; `llm` entra en `alpha`.

## [0.1.0a9] - 2026-09-28

Un 201 tiene que significar que la fila existe.

La sesión de la request confirmaba al desmontar la dependencia, y FastAPI corre
el desmontaje de una dependencia con `yield` después de enviar la respuesta. Así
que un commit fallido ya se había contestado como éxito, y un cliente que leía
su propia escritura enseguida podía llegar antes que el commit. Todos los
módulos generados conectaban la sesión de esa forma.

> Las entradas de `0.1.0a6` a `0.1.0a8` solo están en inglés, en el
> [CHANGELOG del repositorio](https://github.com/JFabrizzio5/JFastFramework/blob/main/CHANGELOG.md).

### Incompatible

- **El plugin de base de datos no arranca mientras la sesión de alguna ruta
  confirme después de la respuesta.** `DbSession`, `ReadSession` y
  `TenantSession` son las tres dependencias de sesión con `scope="function"`, que
  confirma cuando el endpoint regresa y antes de que exista la respuesta; un
  commit fallido ahora es un 500. `Depends(session_dependency, scope="function")`
  también sirve. El rechazo nombra cada ruta, y `jfast upgrade --check` lista
  las líneas antes de que lo haga el arranque.
- **Piso de FastAPI 0.121**, la primera versión con `Depends(..., scope=...)`.
  **Piso de SQLAlchemy 2.0.16** por `postgresql_nulls_not_distinct`.

### Agregado

- **Las violaciones de constraint son 409.** `BaseRepository` convierte una
  violación única, de FK o de exclusión en `ConflictError`, en vez de una
  excepción del driver que salía como 500.
- **`VersionedMixin` y `PreconditionFailedError`.** Una columna `version` que
  SQLAlchemy revisa en cada `UPDATE`, para que el segundo de dos guardados
  concurrentes falle con 409 en vez de reemplazar al primero en silencio; y
  `update(expected_version=n)`, un 412 cuando la fila ya pasó la versión que
  leyó el cliente. Se niega a ir después de `TimestampMixin`, donde se perdería
  sin aviso.
- **`get_for_update`, `advisory_lock`, `run_in_transaction`.** Bloqueo de fila
  para leer-modificar-escribir; bloqueo por llave durante la transacción, para
  reglas que una constraint no expresa; y un ejecutor que reintenta la unidad de
  trabajo completa ante fallo de serialización o deadlock, y ante nada más.
- **`docs/transactions.md`**, en inglés y español.

### Agregado -- el resto de la historia de transacciones

- **Plugin `outbox`.** `outbox.enqueue(session, job)` y
  `outbox.publish(session, topic, event)` escriben a través de la sesión de la
  request, así que un mensaje existe si y solo si se confirmaron las filas de
  las que habla. Con la cola de PostgreSQL en la misma base el job entra directo
  a `jfast_jobs`; lo demás pasa por `jfast_outbox` y un relay que corre en cada
  proceso con `FOR UPDATE SKIP LOCKED`, hace backoff y aparta un mensaje como
  muerto tras `max_attempts`. `claim_once(session, id)` es la mitad del
  consumidor, y `current_job()` le da a un handler el id de su job para
  deduplicar.
- **Plugin `idempotency`.** `IdempotencyKey` / `RequiredIdempotencyKey`: la
  llave se registra en la transacción de la request, un reintento repite la
  respuesta guardada con `Idempotent-Replayed: true`, otro cuerpo con la misma
  llave es 422, y un duplicado concurrente espera al primer insert y recibe 409
  o la repetición. Por tenant, y vence tras `ttl_hours`.
- **Row-level security.** `enable_tenant_rls(op, table)` en una migración y
  `[plugin.database] rls = true`: cada transacción fija su tenant con un
  `set_config` local a la transacción, una query sin tenant no ve filas, y
  PostgreSQL rechaza una escritura para otro tenant. `bypass_rls()` para trabajo
  entre tenants, en las tablas que lo permiten. En producción no arranca con RLS
  activo bajo un rol superusuario o `BYPASSRLS`, que ignoran toda política.
- **Plugin `accounts`.** El store de usuarios que `auth` deja fuera: usuarios,
  login con contraseña argon2id, bloqueo tras fallos repetidos, roles y permisos
  que viajan como scopes del token (`require_permission`), administración por
  tenant bajo `/accounts`, un administrador inicial, y los hooks `on_refresh` y
  `on_identity` de `auth` registrados por ti -- así un permiso retirado o una
  cuenta desactivada terminan en el siguiente refresh, y una identidad de
  proveedor se enlaza a una cuenta solo por un email verificado.

### Cambiado

- **Módulos generados.** Todos los layouts dependen de `DbSession`. `limit` va
  de 1 a 200 y `offset` desde 0: `?limit=-1` era un 500 y `?limit=10000000` un
  volcado de la tabla. El `name` que el servicio revisa por duplicados ahora es
  `UniqueConstraint("tenant_id", "name", postgresql_nulls_not_distinct=True)`
  -- dos requests podían pasar la revisión. Los módulos `layered` tienen
  versión: `Read` devuelve `version`, `Update` la acepta y la lista manda
  `X-Total-Count`.

### Corregido

- **Los jobs corrían sin tenant.** `Job.tenant_id` y `request_id` existían y
  nadie los llenaba, así que todo handler corría sin alcance y sus repositorios
  leían las filas de todos los tenants. Un job creado dentro de una request toma
  ambos del contexto, y el worker los restaura alrededor del handler.
- **`jfast check` acepta `TenantSession`** como apertura de la base de un tenant;
  solo buscaba `tenant_session_dependency`.
- **Documentación que se contradecía.** El README decía `0.1.0a5`; STATUS decía
  que instalar requiere `--pre`, y no es así mientras solo existan pre-releases;
  STATUS decía que no había techos de versión, que `0.1.0a8` agregó; la CI decía
  que el paquete no estaba en PyPI.

### Corregido -- tablas del framework y autogenerate

- **`alembic revision --autogenerate` proponía borrar `jfast_jobs`.** La cola
  crea su tabla al arrancar y no está entre los modelos del servicio, así que
  autogenerate la leía como una tabla que el servicio había borrado. Toda tabla
  del framework es `jfast_*` ahora, y el `env.py` generado pasa `include_name`
  para saltarlas. `jfast upgrade --check` nombra un `env.py` que no lo tiene.

### Cambiado -- el CLI

- **`cli/main.py` pasó de 2,786 líneas a 90.** Los comandos se movieron a
  `cli/commands/` por responsabilidad, con `register(app)` como los módulos
  `migrations` y `check` que ya existían; los helpers de generación compartidos
  viven en `cli/generate.py` para que ningún módulo de comandos importe a otro.
  Los nombres de comandos, opciones, textos de ayuda y orden no cambian -- la
  salida de `--help` de la raíz y de los 52 subcomandos es idéntica antes y
  después.

### Agregado -- lo que necesitó un servicio real

Salió de reconstruir un servicio de facturación (E-Cont) sobre el framework:
cada una de estas piezas la había escrito él mismo, y cada una la tenía mal en
el mismo lugar.

- **`@transactional`, para dependencias de sesión propias.** La revisión al
  arrancar solo conocía las tres dependencias de sesión del framework, así que
  una dependencia `yield` del servicio -- usada en 120 rutas en esa
  reconstrucción -- hacía commit después de la respuesta y nada lo decía.
  Marcada, queda sujeta a la misma regla: una ruta que dependa de ella sin
  `scope="function"` impide que el servicio arranque. Las dependencias
  generadoras sin marcar que llaman `.commit(` después de su `yield` aparecen
  en una advertencia al arrancar.
- **Row-level security con más que el tenant.**
  `@transaction_setting("app.companies")` registra un valor que cada
  transacción con tenant pone junto al tenant, y
  `enable_rls_policy(op, table, predicate=...)` escribe una policy que lo lee.
  Los nombres se validan, los del framework se rechazan, y `None` deja el valor
  sin poner -- cero filas.
- **Subidas que no caben en memoria.** `Disk.put_stream(key, chunks)` en los
  drivers local y S3 -- un archivo temporal que se renombra al final, o un
  multipart que se aborta si falla -- y `guard_stream`, que corre el paso
  `validate` del disco sobre los primeros 64 KiB y aplica `max_bytes` conforme
  llegan los pedazos.
- **XML, JSON, CSV y texto se reconocen parseándolos.** No tienen bytes
  mágicos, así que `validate.allow` no podía nombrarlos. Un XML con `DOCTYPE` o
  `ENTITY` se rechaza antes de parsearlo, y una raíz SVG o HTML no se acepta
  como `application/xml`.
- **`jfastframework.encryption`.** AES-256-GCM para valores que el servicio
  tiene que leer de vuelta, en una columna (`EncryptedString(context=...)`) o a
  mano (`SecretBox`). El contexto se autentica, así que un valor copiado a otra
  fila no se descifra; las llaves vienen de `JFAST_ENCRYPTION_KEYS` y rotan sin
  migración. Extra `encryption`.
- **Nombres de tabla en español.** `[scaffold] language = "es"` en
  `jfast.toml`, o `--language es`: `camion` → `camiones`, `orden_compra` →
  `ordenes_compra`.

### Agregado -- tareas recurrentes, retrasos en RabbitMQ, llamar a otros servicios

- **Tareas recurrentes.** `@tasks.task(name, every=... | cron=..., timezone=...)`
  y `tasks.schedule(...)`, que corre un loop de scheduler dentro del servicio
  con `[plugin.queue] scheduler = true`. Es seguro en cada réplica y worker
  porque cada tick se reclama primero -- en `jfast_schedule_ticks`
  (PostgreSQL, en la misma transacción que el job con la cola de PostgreSQL) o
  con `SET NX` de Redis -- y el job de cada tick tiene un id determinista.
  Después de una caída se dispara una vez el tick perdido más reciente, nunca
  una ráfaga. El cron es un parser propio de cinco campos con la semántica de
  días de Vixie y reglas explícitas para el horario de verano.
- **La cola de RabbitMQ respeta `Job(available_at=...)` y el backoff de
  reintentos**, con una cascada binaria de colas de TTL fijo y sin plugin del
  broker, así que un retraso largo ya no bloquea uno corto detrás. Antes
  publicaba todos los jobs de inmediato.
- **`jfastframework.http` y el plugin `http`.** Un cliente para servicios
  hermanos con timeouts obligatorios y un deadline total; reintentos solo para
  peticiones idempotentes o con `Idempotency-Key`, con backoff full-jitter,
  `Retry-After` y presupuesto de reintentos; circuit breaker y bulkhead por
  upstream que fallan rápido con un problem 503; propagación de `X-Request-ID`
  y, si se activa, del token bearer. Extra `http`.

### Corregido -- RabbitMQ

- **`stats()` siempre reportaba cero.** El canal robusto de aio-pika devuelve
  el objeto de cola que guardó al declararla, con su conteo original; ahora se
  pregunta al canal crudo.

### Agregado -- los frontends vienen en looks, y Nexora es el de por defecto

- **`--template nexora|classic`** en `jfast new service --kind spa`,
  `jfast start` y `jfast init` (que pregunta). `nexora` es el nuevo default: el
  sistema de diseño liquid-glass en el que está hecho el sitio de docs --
  paneles y sidebar de vidrio, una barra superior tipo isla, claro y oscuro,
  una pantalla de login y un pequeño dashboard con valores reales (`/health`, su
  tiempo de respuesta, las vistas registradas). Un listón WebGL (three.js, en
  su propio chunk perezoso, apagado con movimiento reducido y sin WebGL, en
  pausa en pestañas ocultas). `classic` es exactamente el frontend anterior.
- **Selector de color.** Seis colores predefinidos y uno libre, junto al botón
  de tema y en el login. Toda la paleta sale de ese color, con tonos de texto
  que alcanzan contraste 4.5:1; la elección se guarda por app y se aplica antes
  del primer pintado. `VITE_ACCENT` fija el color por defecto del proyecto.
- **La marca es el nombre del proyecto** (`VITE_APP_NAME`), o `jfastframework`
  si está vacío.
- **Fondo 3D, 2D o ninguno**, en el mismo popover: el listón, su cuadro fijo
  sin descargar three.js, o el color liso de la página. Se recuerda por app y
  se aplica antes del primer pintado; `VITE_BACKGROUND` fija el default del
  proyecto.
- **El botón de menú pliega el sidebar en pantalla ancha**, y se queda plegado
  al recargar. Antes se veía ahí y no hacía nada: la regla que lo ocultaba
  perdía contra `.nx-round-btn`, y su click solo movía el drawer del teléfono.
- **JFast Suite como referencia.** Con `--agent-docs`, un frontend nexora
  recibe la skill `nexora-reference`: una copia de las páginas de la suite
  (cerca de 1 MB, las imágenes en WebP) y cómo traer un patrón de ahí al
  proyecto. Los templates ahora pueden llevar archivos que no son `.j2`; se
  copian byte por byte.
- **El look queda registrado** en `.jfast-template`, y `jfast new view` dibuja
  las páginas nuevas en él (`--template` lo sobrescribe). Un look desconocido,
  o un look para un servicio sin frontend, se rechaza antes de escribir nada.
- **Los agentes hacen lo que pidió el usuario.** La skill de diseño generada,
  `AGENTS.md` y la documentación de frontend dicen que el look que pide el
  usuario gana sobre el de por defecto; antes la skill regresaba cualquier
  petición al diseño por defecto.

### Cambiado -- modular por defecto

- **`jfast new module` sin `--layout` genera un módulo `modular`**, y el prompt
  interactivo, `jfast start` y `contracts init` también empiezan ahí. Antes era
  `layered`. Los módulos existentes conservan el layout que tienen registrado en
  `jfast.toml`; `--layout layered` sigue generando la forma anterior. Las guías
  para agentes les dicen que usen `modular` salvo que el usuario pida otro.

### Sitio de documentación

- **Las páginas en español llevan a páginas en español.** Todo enlace del menú
  lateral, del paginador y de los botones de la landing en una página en
  español apuntaba a la página en inglés un directorio arriba, así que a las
  traducciones solo se llegaba con el botón de idioma. `es/docs.html` cargaba
  una hoja de estilos que no existe y decía `lang="en"`. Nada de eso se vio
  porque `docs-site/check.py` solo revisaba el primer nivel; ahora revisa
  `es/`, rechaza un enlace que salga del español al inglés salvo el propio
  botón de idioma, y revisa `<html lang>`. Contra el build anterior reporta
  1,197 problemas.
- **La versión es el release.** Las páginas imprimían el directorio donde se
  publican, así que todos los pies decían "JFastFramework latest". Ahora
  imprimen la versión del paquete, leída del código, y el selector dice
  `latest · 0.1.0a9`. Las cifras de la landing -- plugins, arquitecturas,
  funciones de test -- se cuentan al construir en vez de escribirse en el
  texto, que llevaba un mes diciendo 17 plugins y 509 tests.
- **Textos del sitio en los dos idiomas.** El paginador, el botón de copiar, el
  pie, la portada de la documentación, la etiqueta de costo y la del botón de
  tema estaban en inglés en las páginas en español.
- **Rubí líquido.** Oscuro por defecto, paneles de vidrio, barra de navegación
  flotante y, en la landing, una cinta de vidrio dibujada con three.js --
  fijada y con hash desde cdnjs, con un brillo fijo cuando no puede correr. El
  logo va a la derecha del hero, en su propio panel de vidrio. Ver
  `docs-site/assets/BRAND.md`.

### Sin hacer, y nombrado

Tracing distribuido, y, en `accounts`, verificación de email, recuperación de
contraseña y MFA. Los dos en `PLAN-NEXT.md`.

## [0.1.0a8] - 2026-09-03

> Traducción parcial: de 0.1.0a6 a 0.1.0a8 solo esta parte está en español. El registro completo está en el [CHANGELOG en inglés](../../CHANGELOG.md).

### Agregado

Cinco comandos que llevan a la CLI más allá de los primeros diez minutos de un
proyecto. Cada uno responde algo que el framework ya podía responder y no
respondía.

- **`jfast check`** — todos los checks que existen, una pantalla, un exit code.
  Ya existían todos; lo que no existía era una sola cosa que correr, así que CI
  corría tres y los dos que nadie cableó no corrían nunca. La precedencia de
  exit codes va por **cuánto del reporte invalida el fallo**, no por severidad:
  un `jfast.toml` que no parsea vuelve conjetura todo lo demás. `--json` lleva
  el código de *cada* check que falló, porque un solo número nunca es la
  respuesta completa. Bajo `--ci` un **skip falla** — en CI un skip significa
  que al runner le faltaba algo, y una batería que reporta verde sobre lo que no
  ejecutó es peor que no tenerla.

- **`jfast migration check` / `plan`** — lee las revisiones antes de correrlas:
  `NOT NULL` sobre tabla poblada, un rename renderizado como drop más add, un
  índice construido reteniendo un lock de escritura, un cambio de tipo sin
  `USING`. Verificado mirando fallar `alembic upgrade head` contra PostgreSQL
  real y prediciéndolo. Los conteos de filas salen de `EXISTS ... LIMIT 1` y
  `pg_class.reltuples`, nunca de un `count(*)`, y sin base de datos reporta
  "desconocido, trátalo como poblado" en vez de asumir vacío.

- **`jfast contracts explain`** — por qué existe una regla, dónde está
  declarada, y qué hacer en su lugar. `contracts check` te dice que una regla se
  rompió; a un agente con una violación sin remedio le sale más barato
  satisfacer al checker que arreglar el diseño, borrando el import o apagando la
  regla. La respuesta cita la línea de tu `contracts.toml` y el comentario que
  escribió su autor, no prosa inventada. `contracts diff` compara la
  arquitectura que el contrato permite contra los imports que el código tiene
  — **no** es un diff de git, y la doc lo dice sin rodeos.

- **`jfast ai context --json` y `jfast next`** — todo lo que un modelo necesita
  de un proyecto en una llamada. El tamaño **depende del proyecto y no hay un
  número único**: un servicio generado mide 8,3 KB con un módulo y 11,5 KB con
  cinco, y un servicio de cinco módulos con hallazgos y violaciones de contrato
  reales mide 14,8 KB (`--brief` va de 2,8 KB a 6,1 KB en ese mismo rango).
  **`jfast ai context --size` imprime la cifra de tu proyecto** — esa es la que
  hay que usar para planificar. Para escala: enviar `docs/` en su lugar habrían
  sido 555.859 bytes. Lo que deja fuera a propósito queda listado en un campo
  `omitted` con el comando que lo recupera, `jfast migration check` incluido.
  `next` ordena los pasos por **dependencia,
  no por severidad** — un módulo sin registrar va antes que sus tests faltantes,
  porque testear un módulo no cableado no prueba nada — y en un proyecto limpio
  dice qué revisó en vez de inventar trabajo.

- **`jfast upgrade --check`** — qué rompe al pasar a una versión más nueva,
  **filtrado a lo que aplica a este proyecto**: lee tus modelos, tu
  `contracts.toml` y la configuración de tus plugins, y reporta solo los cambios
  que pueden afectarte. Un aviso que no aplica es como la gente aprende a
  saltarse la salida. El manifiesto son datos en el paquete y no un parseo del
  changelog, que es prosa, no viaja en el wheel, y se rompe en silencio si
  alguien reescribe un encabezado. `--apply` está rechazado, no stubbeado:
  reescribir el proyecto de alguien necesita una vuelta atrás que esto no tiene.

## [0.1.0a5] - 2026-08-30

Veinte hallazgos de un segundo reporte externo, este contra `0.1.0a4`. Nueve se
verificaron a mano antes de tocar nada, dos resultaron peores de lo reportado, y
dos se rechazaron — uno de ellos rechazado como defecto y resuelto como problema
de nombre.

El eje es más angosto que el de la versión anterior. `0.1.0a4` construyó siete
comandos de diagnóstico en un día y **tres mienten**: `migration check` aprobaba
tres sentencias peligrosas, `contracts check` daba verde sobre un contrato que
no gobernaba un solo archivo, y `analyze` no reportaba nada en ese mismo
proyecto mientras `jfast next` decía que el check fallaba. Un diagnóstico que
nadie cree se ignora; un diagnóstico que la gente cree y que miente sale más
caro que el defecto que venía a encontrar, porque es la razón por la que dejaron
de leer la migración a mano.

De ahí salieron dos reglas, y las dos viajan como test y no como nota:

1. Ningún diagnóstico se da por bueno sin un caso donde debe fallar y falla.
2. El test afirma la mitad que puede romperse, **por la ruta que toma un
   usuario**: no el string anunciado cuando lo que falla es el mapping de
   puertos, no `contracts init` cuando la gente corre `jfast new service`, no el
   middleware aislado cuando corre detrás de uvicorn.

1.287 tests pasan y 7 se saltan: cinco quieren un Redis real, dos son un mismo
comando alcanzable por dos caminos a propósito.

### Rompe

- **El `*` de un glob de capa se detiene en `/`.** Los paths de capa pasaban por
  `fnmatch`, que traduce `*` a `.*` y cruza separadores de directorio, así que
  `modules/*/repository.py` también reclamaba
  `modules/billing/infrastructure/repository.py` — una capa podía parecer que
  gobierna un árbol para el que nadie la escribió, y `layer-unmatched`, el
  hallazgo que existe para atrapar un contrato que no gobierna nada, no saltaba
  nunca. El matcheo además es case-sensitive en toda plataforma: `fnmatch`
  normaliza mayúsculas en Windows, así que un mismo contrato pasaba en una
  laptop y fallaba en CI.

  Un archivo que ayer matcheaba una capa y hoy no matchea ninguna no está
  gobernado por nada, `forbid_packages` incluido. Donde el alcance era
  intencional, ensanchá el patrón:

  ```diff
  -paths = ["modules/*/repository.py"]
  +paths = ["modules/**/repository.py"]
  ```

  `**` cruza directorios a propósito y lo dice, y `**/` matchea también *cero*
  segmentos, así que `modules/**/http.py` sigue cubriendo `modules/http.py`.
  `jfast upgrade --check` lista bajo `layer-globs-narrowed` los archivos que
  cambiaron de mano, calculados contra tu árbol y no deducidos de los patrones:
  un contrato escrito todo con `**` no cambia y no recibe reporte.

- **`datetime.now()` sin zona es una violación de contrato.** La regla nueva
  `naive-datetime` viaja en `[rules.async_safety]`, que todo contrato existente
  ya tiene prendida, así que llega sin que nadie opte por ella y un build que
  ayer pasaba hoy falla nombrando una regla que el proyecto nunca vio.
  `datetime.utcnow()` y `datetime.utcfromtimestamp()` son las otras dos: naive a
  pesar del nombre, y deprecadas desde 3.12.

  ```diff
  -created = datetime.now()
  +from jfastframework.time import now
  +created = now()
  ```

  `datetime.now(UTC)` vale igual: la regla es sobre el argumento que falta, no
  sobre qué módulo usás. Un splat `*args` o `**kwargs` nunca se reporta, porque
  la sintaxis no puede decir si la zona va ahí adentro. Para postergar la regla
  entera, una línea en `contracts.toml`:

  ```toml
  [rules.async_safety]
  naive_datetime = false
  ```

  Eso deja prendida la mitad de async-blocking, que es la que ya tenías.

- **Toda sesión de base de datos queda fijada a UTC.** `[plugin.database]
  session_timezone` vale `"UTC"` por defecto y viaja como parámetro de arranque
  de asyncpg, así que `date_trunc('day', ...)`, `CURRENT_DATE`, `now()::date` y
  cualquier `AT TIME ZONE` sin zona explícita dejan de leer el `TimeZone` del
  servidor. En un servidor configurado con otra cosa, esas consultas devuelven
  filas distintas a las de ayer — **que es justamente el punto**, pero es un
  cambio de respuesta, no de código, y donde se nota es en un reporte diario.
  Para conservar el comportamiento anterior, decilo:

  ```toml
  [plugin.database]
  session_timezone = ""   # no toca el TimeZone del servidor
  ```

  Va en el paquete de arranque y no como un `SET` después de conectar, a
  propósito: una conexión pooleada se toma a mitad de su vida, así que un
  `DISCARD ALL`, un `RESET ALL` o un reset de servidor de pgbouncer deshace una
  sentencia que corrió una vez y la sesión vuelve a la zona del servidor sin
  dejar nada en los logs.

- **`jfast new service` ya no escribe `contracts.toml`.** No tenía forma de
  saber el layout — un servicio se genera antes de que exista un módulo — así
  que escribía el contrato layered en servicios hexagonal, modular y screaming,
  donde no matcheaba ningún archivo y no imponía nada mientras `contracts check`
  salía 0. Ahora el primer `jfast new module --layout X` escribe el contrato de
  X, y nunca reemplaza uno que ya esté en disco. Pasá
  `jfast new service --layout X` cuando el layout ya esté decidido.

  Para proyectos generados antes de esto, la consecuencia es una falla nueva:
  `contracts check` reporta `layer-unmatched` (exit 5) para una capa que no
  matchea ningún archivo mientras archivos que debería haber reclamado quedan
  sin dueño. Apuntá la capa a las carpetas que tus módulos usan de verdad —

  ```diff
  [layers.storage]
  -paths = ["modules/*/repository.py"]
  +paths = ["modules/*/infrastructure/*.py"]
  ```

  — y `jfast inspect` te dice el layout de cada módulo.
  `jfast contracts init --layout X --force` también lo arregla y sobrescribe el
  archivo entero, con todas las capas, reglas y waivers que el proyecto agregó,
  así que acá es el último recurso y no el primero.

- **`TokenStore.rotate_refresh` devuelve un resultado, no un bool**, y recibe un
  keyword `grace`. Un bool no podía distinguir un cliente reintentando de un
  token robado siendo reusado — los dos son "este ya se usó" — y solo el store
  puede responder las dos cosas juntas. Todo test de veracidad sobre el valor
  viejo también da verdadero para `"replayed"`, que es el único resultado que
  tiene que terminar la family:

  ```diff
  -async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
  +async def rotate_refresh(
  +    self, token_id: str, *, family: str, ttl: int, grace: int = 0
  +) -> RefreshOutcome:
  ```

  `RefreshOutcome` es `Literal["rotated", "raced", "replayed"]` en
  `jfastframework.auth.store`, donde `MemoryTokenStore` y `RedisTokenStore` son
  los ejemplos completos. Un store que no puede honrar una ventana de gracia
  devuelve `"replayed"` donde antes devolvía `False`. Solo afecta a proyectos
  que escribieron su propio store; los que vienen incluidos los lee el framework
  correctamente.

- **Un hook `on_refresh` que devuelve `None` revoca la family de la sesión.**
  Antes rechazaba esa petición y dejaba el access token que el cliente ya tenía
  en la mano válido por toda su vida, así que un usuario baneado seguía
  trabajando hasta quince minutos más. El hook además corre *antes* de consumir
  el refresh token: si el hook falla, el token sigue usable, así que el reintento
  natural del cliente es un reintento y no un replay que cuesta la family por
  toda la vida del refresh.

- **`Page.total` es `int | None`.** Esto salió en `0.1.0a4` y quedó anotado ahí
  como arreglo, que era la sección equivocada: los modos que se saltan el
  `COUNT` nunca supieron un total, y reportar uno igual era un número sobre el
  que nadie podía actuar. Llega a los clientes y no solo al código que
  type-checkea: el JSON de todo endpoint paginado que este framework genera
  puede traer `"total": null`, y un modelo de respuesta que declara `total: int`
  falla la validación justo en la página que lo produce.

  ```diff
  class PageResponse(BaseModel):
  -    total: int
  +    total: int | None
  ```

  `has_more` responde "¿hay página siguiente?" sin necesidad del total.
  `jfast upgrade --check` lista los call sites bajo
  `pagination-total-optional`.

- **Un servicio con `storage` prendido recibe 25 MiB y 120 s desde el kernel.**
  El par elevado lo escribía el scaffold en `jfast.toml` y no vivía en ningún
  otro lado, así que un proyecto que habilitó `storage` un año después de
  `jfast new service` corría con 2 MiB y 30 s mientras `upgrade --check` le
  prometía 25 MiB. La regla vive en `JFastSettings` ahora, lo que vuelve cierto
  el manifiesto por construcción. Un valor explícito en `[app]` — incluido un
  `0` explícito — sigue ganando, así que escribí uno para conservar un límite
  más chico.

- **Los compose generados no fijan `container_name`.** Es global al daemon, así
  que una segunda copia del mismo workspace no podía levantar al lado de la
  primera. Ahora compose deriva el nombre del contenedor del proyecto, lo que
  rompe cualquier script que nombrara uno directo:

  ```diff
  -docker exec -it shop_postgres psql -U shop
  +docker compose exec postgres psql -U shop
  ```

### Agregado

- **Un subsistema de zonas horarias, porque "guardá UTC" era la mitad del
  problema.** `0.1.0a4` volvió aware lo que se *guarda*. Calcular con esos
  valores seguía siendo una propiedad de dónde corre el contenedor:
  `date_trunc('day', created_at)` responde distinto en dos réplicas cuyos
  servidores tienen distinto `TimeZone`, a partir de filas idénticas byte a
  byte, y nada falla.

  `jfastframework.time` responde la mitad que es una pregunta de negocio.
  `now()` es el único reloj que lee el framework y siempre devuelve UTC aware;
  `today(tz)` y `day_bounds(day, tz)` deciden a qué día *local* pertenece un
  instante UTC. `day_bounds` devuelve un rango semiabierto `[start, end)` en UTC
  — nunca `BETWEEN`, cuyo límite superior cerrado o cuenta la medianoche dos
  veces o pierde el último microsegundo según la precisión de la columna — y
  resuelve los dos bordes con `fold=0`, que es lo que lo hace correcto tanto en
  el día local que no tiene medianoche (America/Santiago arranca el DST a las
  00:00) como en el que tiene dos. `in_zone()` es presentación y nada más;
  `parse()` rechaza un string sin offset salvo que quien llama diga en qué zona
  lo escribieron.

  Tres zonas, separadas porque son tres preguntas: el almacenamiento es UTC y no
  se configura; `[app] timezone` es la zona **de negocio**, lo que significa
  "hoy" para un reporte, un período de facturación o una cuota diaria;
  `[plugin.tenancy.timezones]` la pisa por tenant, que es el caso que existe
  cuando un mismo deploy atiende varios países, con una dependencia
  `tenant_zone` que cae en la zona de negocio y nunca en la del servidor. Cada
  nombre se valida al arrancar, así que un typo detiene el servicio en vez de
  correr los reportes de un tenant un día entero, una semana después.

  `"UTC"` resuelve a `datetime.UTC` y no lee ningún archivo: `zoneinfo` lee
  `/usr/share/zoneinfo` también para esa clave, y un contenedor slim sin tzdata
  falla con cualquier nombre. Nombrar una zona real en una imagen así falla al
  arrancar, con un mensaje que dice qué paquete falta.

  `jfast doctor` reporta `db_timezone` como par — en qué zona calcula un cliente
  sin fijar, y en cuál calcula este servicio — leído por dos conexiones, porque
  un parámetro de arranque *se convierte* en el valor de reset de la sesión y
  `pg_settings.reset_val` diría `UTC` sobre un servidor configurado con
  cualquier cosa. Que el servidor no esté en UTC no es una falla de este
  servicio y aun así merece una línea: cualquier otro cliente de esa base, psql
  o una herramienta de BI o una migración corrida a mano, está reportando otro
  día. Ver [docs/timezones.md](timezones.md).

- **`jfast check` dice qué no revisa.** Con un import sin usar, un archivo mal
  formateado, un `str` asignado a un `int` y un test fallando, todo a la vez, su
  salida era byte a byte idéntica a la del proyecto limpio y salía 0, `--ci`
  incluido. Nada del reporte era falso. Lo falso era la impresión que dejaba el
  nombre, y un equipo que actúa sobre esa impresión borra su propio script de
  verificación y pierde cuatro compuertas en un commit.

  Sigue sin correr ninguna de las cuatro, a propósito: `pytest` ejecuta tu
  código por un tiempo no acotado y `mypy` en un árbol sin dependencias
  instaladas fabrica hallazgos, y cualquiera de las dos adentro convierte un
  hook de pre-commit en un build. Así que la solución es nombrar, no agregar.
  Toda corrida termina con

  ```
  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
  ```

  pase o falle, y `--json` lleva esas mismas cuatro bajo `not_covered`, cada una
  con lo que atrapa y el comando que la atrapa.

- **`contracts check` imprime cuántos archivos gobierna cada capa**, en el orden
  en que el contrato las declara, en cada corrida:

  ```
  layers
    domain             4 files
    application        2 files
    infrastructure     3 files
    adapters           2 files
    shared             2 files
  ```

  Una capa en 0 es el hallazgo, y es el que este checker reportaba tarde:
  `layer-unmatched` salta solo cuando los archivos que esa capa debería haber
  reclamado *además* quedan sin dueño, y una capa que pasa de 40 archivos a 3 en
  un refactor no levanta nada. Se cuenta a través de `layer_for` y no globeando
  crudo, así que una capa cuyos matches se los lleva todos un patrón más
  específico aparece con el cero que le corresponde.

  Un hecho, un dueño, entre cuatro comandos: `analyze` reporta
  `contract-governs-nothing` llamando a `check_coverage` en vez de
  reimplementarlo — antes imprimía "sin hallazgos" en un proyecto cuyo contrato
  no gobernaba nada, mientras `jfast next` decía que el check fallaba —
  `contracts diff` lista lo que permite una capa vacía bajo `~` con el motivo,
  en vez de vender una caída como diez oportunidades de apretar el contrato, y
  `next` junta el par en un solo paso en lugar de cinco líneas sobre una sola
  cosa que hacer.

- **`ratelimit`, `channels` y `websocket` están en el menú de plugins.** Los
  tres viajaban como entry points y no aparecían en ningún catálogo, así que la
  única forma de encontrarlos era leer `pyproject.toml`. `tenancy` era el caso
  espejo: visible en el catálogo y ausente de todo `jfast.toml` generado, porque
  el filtro que arma ese menú exigía un extra y `tenancy` no necesita ninguno.
  Los plugins sin extra ahora imprimen `no extra needed` en su fila. Dos tests
  atan las dos listas hechas a mano, en los dos sentidos — una entrada de
  catálogo sin entry point ofrece una instalación que no puede funcionar.

- **`[plugin.auth] refresh_grace_seconds`**, 10 por defecto. Dos pestañas del
  mismo navegador refrescan en el mismo instante y una pierde; sin ventana, el
  intento del perdedor se lee como robo y revoca la sesión que las dos
  compartían. La petición perdedora igual se rechaza — hay un solo refresh token
  vivo y lo tiene el ganador — pero la family sobrevive. `0` restaura la
  detección estricta de reuso, y más que un round trip de request solo le da más
  tiempo a un token robado.

- **Pantalla de login en los dos frontends generados.** `LoginView`, un guard de
  rutas público por defecto con la única línea a cambiar marcada y explicada, y
  un manejador de 401 en `services/api.js` que refresca **una sola vez** para
  toda una ráfaga: cuatro paneles cargando juntos producen cuatro 401, y cuatro
  refresh presentan el mismo token rotado cuatro veces, cosa que un backend que
  trata el replay como robo responde revocando la sesión.

- **`jfast new service --layout`**, para un servicio cuyo layout ya está
  decidido, y `followSystem()` en el composable de tema de ambos frontends.

### Arreglado — lo silencioso

- **uvicorn resolvía la dirección del cliente antes de que el framework la
  viera.** En todo el fuente de `0.1.0a4` no hay un solo `proxy_headers`, y
  uvicorn trae su propio manejo de `X-Forwarded-For` **prendido**, confiando en
  loopback, corriendo antes que cualquier middleware de aplicación. `jfast
  serve` bindea `127.0.0.1`, que es exactamente el peer al que uvicorn le cree —
  así que `trusted_proxies` se validaba en local y salía correcto por el motivo
  equivocado, mientras que en un pod cualquier sidecar podía elegir su propia
  dirección de cliente. Un `X-Forwarded-Proto` falsificado además volteaba
  `scope["scheme"]`, que es la compuerta con la que se decide HSTS.

  `jfast serve`, `jfast dev` y el entrypoint del Dockerfile generado pasan
  `--no-proxy-headers` ahora: un solo resolvedor en el proceso, y es el que lee
  `jfast.toml`. Un peer que llega ya sustituido se detecta en vez de creerse —
  un proxy agrega la dirección *desde la que* recibió la conexión, nunca la
  propia, así que un peer de transporte legítimo no aparece en la cadena que
  está retransmitiendo — y esa petición se trata como si no tuviera cliente:
  un único balde del que un atacante no puede rotar, con el scheme degradado
  cuando el header pudo haberlo escrito. Los tests de las dos mitades corren
  contra un uvicorn real sobre un socket real, arrancado como viene de fábrica.

- **La paginación por cursor se detenía en el primer NULL y decía que había
  terminado.** Peor de lo que decía el reporte: no es "la página termina antes",
  es una página vacía con `has_more=False`, y toda fila desde ese cursor en
  adelante inalcanzable desde cualquier cursor — 180 de 200 en SQLite, donde los
  NULL van primero, 40 de 200 en PostgreSQL, donde van últimos.
  `last_message_at` y `edited_at` son justo las columnas por las que ordena un
  feed.

  Todo ordenamiento que arma el repositorio escribe ahora `NULLS LAST` en las
  columnas nullables, porque el default no es uno solo — PostgreSQL pone los
  NULL últimos ascendente y primeros descendente, SQLite los pone primeros en
  los dos casos — y la comparación de keyset está escrita contra eso: `IS NULL`
  para un empate, y un paso más allá de un valor no nulo admite el bloque de
  NULL que lo sigue. El costo se dice en vez de esconderse: una columna de orden
  nullable degrada el index range scan a un index scan con filtro, medido en 840
  buffers contra 4 con columnas NOT NULL, una página en la profundidad 100k
  sobre 200k filas, contra 1.049 de la misma página por `OFFSET`. Sigue siendo
  la más barata de las tres formas de paginar; ya no es plana.

- **Tres parsers no podían leer la propia plantilla de este framework.**
  `cli/migrations.py` y `upgrades.py` filtraban `ast.Assign`, y el
  `script.py.mako` generado emite `revision: str = ...` y
  `down_revision: str | None = ...`, que son `ast.AnnAssign`. El mismo punto
  ciego le escondía `__tablename__: str = "users"` — estilo SQLAlchemy 2.0, que
  es como está escrito el resto de un modelo generado — a `project.py`, así que
  `module-no-migration` se callaba sobre una tabla que ninguna revisión crea. El
  test que lo cubre parsea una revisión **generada por la plantilla**, no una
  escrita a mano.

- **`jfast upgrade --check` entregaba `ALTER TABLE` para tablas sin
  timestamps.** Reportaba uno por cada `__tablename__` en cualquier archivo que
  *importara* `TimestampMixin`, y un archivo de modelos suele tener tanto las
  tablas que lo mezclan como las tablas de proyección que no. Un `ALTER` que
  nombra `created_at` en una tabla que no lo tiene aborta la revisión — después
  de que cada sentencia anterior ya tomó ACCESS EXCLUSIVE y reescribió su propia
  tabla. Ahora se resuelve por clase, recorriendo la cerradura de bases, así que
  un modelo que llega al mixin por una base declarada en `shared/` aparece y una
  base `__abstract__` se saltea.

- **El script de consola `jfast` no podía cargar los plugins del propio
  proyecto.** `[plugins.paths]` nombra módulos que viven en el proyecto y no en
  un wheel, y `python -m jfastframework` pone el directorio del proyecto en
  `sys.path` mientras que el script de consola no — así que un mismo
  `jfast.toml` pasaba con una forma de invocar y reportaba su propio plugin como
  faltante con la otra. Una sola inserción, en `registry.discover`, y una ruta
  con puntos que no importa queda registrada como rota en vez de reventar el
  discovery. `check`, `analyze` y `doctor` ven ahora un único conjunto de
  plugins.

- **Kafka anunciaba un puerto que compose no publicaba.** `host_port` movía solo
  la dirección anunciada; el mapping seguía saliendo de
  `base_port + port_offset`. Un cliente hacía bootstrap, reconectaba a la
  dirección anunciada y se colgaba. Ahora `InfraService.host_port` alimenta las
  dos cosas desde un solo campo, y el test afirma también el mapping de `ports:`
  — el viejo comprobaba únicamente el string anunciado, así que pasó todo el
  tiempo que el defecto estuvo presente.

- **`jfast deploy compose` tiraba las subidas de un servicio en el siguiente
  build.** El volumen que conserva los discos de storage locales lo escribía
  solo el generador de workspace, así que el mismo servicio desplegado por el
  otro comando conservaba las filas y perdía los archivos a los que apuntan.
  Ahora los dos generadores comparten una función, como ya hacían para la forma
  de un contenedor.

### Arreglado — corrección

- `jfast migration check` lee `op.execute`. Las formas de sentencia cuyo riesgo
  deciden las primeras palabras clave se analizan, y una que no matchea ninguna
  se reporta como no leída en vez de pasar en silencio.
- Un drop más add con un backfill en el medio ya no se reporta como pérdida de
  datos. Copiar los valores es lo que convierte al par en un rename, y marcarlo
  igual es el falso positivo que deja a `--fail-on never` como la única forma de
  pasar una migración correcta por CI.
- El banner de rename que trae una revisión generada dice que habla por el
  momento en que se escribió el archivo y que `jfast migration check` relee el
  archivo: cuando el comando se calla y el banner sigue ahí, el viejo es el
  banner. Nada actualiza un comentario, y un STOP que nadie borra es un STOP que
  nadie lee.
- Cada remedio entre backticks que imprime `migration check` está afirmado como
  Python que alguien puede pegar. Los fragmentos de SQL y de shell en las mismas
  frases no lo son, y no se marcan como si lo fueran.
- `useTheme().resolved` es un `computed` a nivel de módulo en los dos frontends.
  Era un `ref` por cada llamada, así que solo se refrescaba el componente que
  manejó el click, bajo un comentario que afirmaba la propiedad que no entregaba.
  Hay también un `followSystem()`: `toggleTheme` solo puede fijar claro u
  oscuro, así que sin él el primer click descartaba para siempre uno de los tres
  estados.
- El `alembic.ini` generado usa `version_path_separator = newline`. `os` está
  deprecado y avisa en cada comando de alembic.
- `contracts explain` documenta `naive-datetime` y apunta a
  `[rules.async_safety]`, así que una violación de la regla nueva llega con un
  remedio y no con un nombre de regla y un número de línea.
- El `jfast.toml` generado documenta `timezone`, `[plugin.database]
  session_timezone` y `[plugin.tenancy.timezones]` donde se configura cada uno,
  y `AGENTS.md` ya no afirma que todo servicio nace con un contrato.

## [0.1.0a4] - 2026-08-30

Veinte defectos encontrados construyendo una aplicación real contra `0.1.0a3`,
más una auditoría de seguridad y una revisión de la CLI. Casi todos **fallaban
en silencio**: un consumer suscrito a nada, un token refrescado que autenticaba
sin permisos, un compose que se regeneraba idéntico ignorando un plugin, un
contrato que prohibía el patrón que su propia documentación prescribe.

### Rompe

- **Los timestamps son timezone-aware.** `TimestampMixin` mapeaba a `TIMESTAMP
  WITHOUT TIME ZONE`, así que `created_at` serializaba como
  `2026-08-29T20:55:15` sin offset y todo cliente JavaScript lo leía como hora
  local — un post escrito ahora se veía como "en 6 horas" al este de UTC. Las
  tablas existentes necesitan una migración, y **la cláusula `USING` es carga
  estructural**:

  ```sql
  ALTER TABLE invoices
      ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
      ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
  ```

  El autogenerate de Alembic escribe la forma pelada, sin `USING`. **Eso no
  falla**: convierte por el cast implícito, leyendo cada valor guardado en el
  `TimeZone` del *servidor*, y desplaza la tabla entera en silencio en cualquier
  servidor que no esté en UTC.

- **Los refresh tokens emitidos por `0.1.0a3` y anteriores se rechazan** con
  401. Cada sesión activa vuelve a autenticarse una vez. Esas sesiones ya
  estaban rotas: rotarlas devolvía un token sin scopes.

- **`/auth/logout` termina solo la sesión que llama.** Antes revocaba todas las
  sesiones de esa persona. No hay reemplazo de "cerrar sesión en todos lados" en
  esta versión — un cursor a nivel de sujeto necesita cambiar `TokenStore` y va
  aparte.

- **Los contratos generados dejan que cada capa importe `shared/`.**
  `contracts.toml` es del proyecto una vez generado, así que hay que agregar
  `"shared"` a mano en el `may_import` de cada capa. `contracts init --force`
  escribe los defaults corregidos y **sobrescribe el archivo entero**,
  descartando las líneas específicas del proyecto, que son la parte que vale.

- **`max_body_bytes` y `request_timeout` ahora tienen valor** (2 MiB, 30 s; 25
  MiB y 120 s en un servicio con storage). Los dos eran `None`. `None` y `0`
  siguen significando sin límite.

- **`contracts check` sale con `5`, `doctor` con `2` o `3`.** Los exit codes son
  estándar y están documentados.

- **Los access tokens llevan un claim `fam`.** Consecuencia: revocar una sesión
  ahora invalida de inmediato sus access tokens pendientes.

### Arreglado — lo silencioso

- **El consumer de events se suscribía a nada.** `startup()` salía con un
  `return` pelado cuando no había handlers: sin log, sin warning. El consumer
  arrancaba, se unía al grupo, y no recibía nada nunca — porque la ventana de
  registro era de una línea, entre que el plugin provee el bus y que startup lo
  lee. Los handlers se declaran en import time y se drenan al registrar, la
  misma forma que `Channel` ya usaba, y las dos ramas logean.

- **Un token refrescado no tenía scopes ni roles.** Más hondo de lo que parecía:
  el refresh token nunca los llevó, así que reenviar lo que `rotate` recibía no
  habría reenviado nada. Ahora lleva el permiso bajo `grt`, un claim propio del
  emisor — nunca el claim de scopes configurado, así que `verify()` no puede
  leerlo de vuelta como autorización.

- **Un logout dejaba el siguiente login muerto hasta 30 días.** La family del
  refresh era el subject, y `revoke_family` la negaba por la vida completa del
  refresh — así que el *siguiente* login nacía dentro de una family revocada.
  Access tokens de 15 minutos y ningún refresh que funcionara. Las families son
  aleatorias por sesión.

- **Un refresh token se aceptaba como bearer token.** Misma clave, mismo emisor,
  y ni `require_auth` ni el middleware revisaban `typ`, así que un token de 30
  días abría toda ruta protegida solo con `require_auth`. Contenido hasta ahora
  únicamente porque no llevaba scopes — por eso el permiso va en su propio
  claim.

- **`jfast workspace compose` ignoraba cualquier plugin que declarara
  infraestructura.** Salida byte a byte idéntica con el plugin prendido o
  apagado. Dos generadores leían dos fuentes de verdad, y el que usan los
  proyectos reales no podía expresar un plugin. Ahora hay uno solo; un plugin
  que no puede representar es un warning, nunca silencio.

- **Seguir la documentación violaba el contrato generado.** Todo servicio trae
  `shared/enums.py` y el `may_import` de cada capa omitía `shared`, así que
  mover código adonde los docs, la regla de placement y el propio mensaje del
  checker te dicen que lo muevas fallaba el check.

- **`jfast start` escribía una contraseña de base en un archivo que llamaba
  gitignored.** La regla vivía en el cuerpo del comando `workspace init`, no en
  `Workspace.save()`.

- **La primera migración de todo servicio generado no podía correr.**
  Introducido por el cambio de timestamps de esta misma versión y atrapado antes
  de publicar: Alembic renderiza un tipo propio por su ruta con puntos y no
  emite el import, y la revisión *parsea* porque el nombre solo se evalúa dentro
  de `upgrade()`. Moría en `alembic upgrade head` con un `NameError` en una
  línea que se veía perfecta.

### Arreglado — corrección

- `NOT NULL` con un default escalar en el modelo ahora emite `server_default` y
  lo vuelve a quitar, así que un add sobre tabla poblada aplica y el siguiente
  autogenerate sale vacío.
- El aviso de rename es condicional. Estaba en el docstring de *cada* revisión,
  o sea que era tapiz — quien reportó la pérdida de datos lo tenía enfrente.
- `paginate` ya no fuerza un `COUNT` exacto; `paginate_keyset` evita el barrido
  por offset (44.925 buffers en el offset 10.000 contra 588 en la primera).
- `public_base_url` funciona en discos locales. Se aceptaba, se pasaba solo a
  S3, y se descartaba en silencio para local — y a propósito **no** aplica a
  `temporary_url`, porque un CDN delante de una URL firmada sirve el objeto
  después de que la firma expira.
- `jfast new enum` pone el archivo en la capa que usa el layout. En un módulo
  hexagonal caía fuera del layout, donde ningún glob del contrato lo alcanzaba.
- Kafka anuncia dos listeners, así que un proceso en el host alcanza el broker.
- El compose emite `shm_size` en PostgreSQL, un volumen para los discos de
  storage, y una cantidad de workers derivada de la cuota de cgroup del
  contenedor en vez de los cores del host.

### Agregado

- **`jfast inspect`, `jfast analyze`, `jfast graph`.** La CLI no sabía decirte
  qué había en tu propio proyecto: `describe` construye la app para responder,
  lo que falla justo cuando lo necesitas, y no dice nada de módulos. Estos leen
  el sistema de archivos y nunca importan código del proyecto.
- **`ratelimit`** — token bucket en un script Lua, así que leer/decidir/escribir
  es indivisible. Falla abierto, en voz alta, y reporta `/ready` degradado. El
  README del gateway prometía este plugin desde hacía un año.
- **`websocket`** — registro de conexiones, handshake autenticado por
  `Sec-WebSocket-Protocol` (nunca el query string), buffers de envío acotados,
  heartbeats, y entrega entre workers sobre el backplane de Redis que ya
  existía.
- **Conexiones de base con nombre**, split lectura/escritura con fijado al
  primary tras escribir, y engines por tenant detrás de un LRU acotado que nunca
  desecha un engine que una petición tiene en la mano.
- **Un pipeline de subida** en cada disco, con un paso `validate` que detecta el
  tipo desde los bytes, y un paso opcional `optimise-image` detrás de
  `jfastframework[images]`.
- **Cabeceras de seguridad** — CSP derivada de si el esquema está expuesto, HSTS
  condicionada a producción *y* HTTPS, `frame-ancestors`, y resolución de
  proxies de confianza para que `X-Forwarded-For` no sea un bypass.
- **`get_or_set`** con protección de estampida y contadores de aciertos. El
  cache no tenía ni un test.
- **Exit codes estándar** en `jfastframework.cli.exits`.
- **Un switch de tema** en los dos frontends generados: tres estados, sin flash
  al recargar, y un drawer móvil donde antes un teléfono no tenía navegación.
- **El CI levanta PostgreSQL y Redis**, y falla si los tests que los necesitan
  se saltan. Un skip es silencioso, que es como se publica una garantía de
  concurrencia sin haberla observado nunca.
- **Un test que afirma que la documentación para agentes es cierta.** Para cada
  uno de los cuatro layouts extrae cada ruta que nombran `AGENTS.md` y las
  skills, y afirma que existe. `AGENTS.md` venía describiendo un árbol `layered`
  a todos los proyectos, así que en un módulo hexagonal los cuatro archivos que
  nombraba estaban ausentes.

### Agregado

- **`jfast inspect`, `jfast analyze` y `jfast graph`.** La CLI no sabía decirte
  qué había en tu propio proyecto. `jfast describe` responde "qué es este
  servicio" construyendo la app --no disponible cuando falta una dependencia o
  el código no importa-- y no dice absolutamente nada de los módulos: genera dos
  y ninguno aparece en su salida. Estos tres leen el sistema de archivos y nunca
  importan código del proyecto, así que funcionan en un proyecto que hoy está
  roto.

  `inspect` es una pantalla: módulos, cómo está formado cada uno, qué sirve, si
  está cableado en la app. `inspect module <name>` va más hondo. `analyze`
  reporta ocho problemas estructurales, lo peor primero, cada uno con el arreglo
  en su propia línea: ciclos de import, un módulo que `main.py` nunca registra,
  dos routers reclamando un prefijo, `shared/` importando un módulo, un plugin
  habilitado que nada provee. `graph` dibuja el grafo de dependencias entre
  módulos como texto, mermaid, dot o JSON.

  Cada check es decidible desde la fuente. Nada adivina: un checker que acierta
  nueve de cada diez veces queda silenciado después del segundo falso positivo,
  y con él se van los hallazgos verdaderos. `module-no-migration` solo se
  dispara cuando *ya hay* revisiones, así que un proyecto recién generado no
  reporta nada.

- **Exit codes estándar**, en `jfastframework.cli.exits`, documentados en
  [docs/inspect.md](inspect.md) y cubiertos por tests. `1` validación, `2`
  configuración, `3` entorno, `4` migración, `5` contrato, `6` uso, `7`
  compatibilidad. CI ya puede decir *por qué* falló sin parsear inglés.

- **Un switch de tema en los dos frontends generados.** No había ninguno: las
  clases `dark:` estaban por todos lados y nada definía el tema nunca, así que
  la app seguía al sistema operativo y quien quería el otro no tenía forma de
  decirlo. `src/style.css` redefine la variante `dark` para leer primero un
  `data-theme` explícito y caer al `prefers-color-scheme`; `ThemeToggle` va en
  el header; la elección se guarda por servicio y se aplica con doce líneas
  inline en `index.html` **antes del primer pintado**, así que no hay flash al
  recargar.

- **Un drawer móvil.** El sidebar era `hidden md:block` y el header móvil no
  tenía más que el nombre del servicio, o sea que en un teléfono la app generada
  no tenía navegación en absoluto.

### Cambiado

- **Los templates de frontend se pintan con tokens semánticos.** El layout usaba
  `slate-*` y los componentes `zinc-*` --dos rampas neutras distintas, una con
  tinte azul y la otra no-- así que una card nunca terminaba de combinar con la
  página sobre la que estaba, en ninguno de los dos temas. Ese desajuste es la
  mayor parte de lo que significa "el template se ve soso": ningún componente
  tiene nada mal, los grises simplemente no se ponen de acuerdo.

  `bg-surface`, `bg-panel`, `bg-elevated`, `border-line`, `text-ink`,
  `text-ink-soft` y `text-ink-faint` ahora salen de `@theme inline`, así que las
  utilidades emiten `var(--ui-*)` y un cambio de variable da vuelta la interfaz.
  **Ningún componente lleva ya una clase `dark:`**, salvo los cuatro tonos de
  estado de `BaseBadge` y `ToastHost` donde el color es el significado.
  `color-scheme` se define al lado, así que las barras de scroll y los date
  pickers también siguen el tema.

- Viaja la rampa completa de la marca (`50` a `900`) en vez de seis pasos
  sueltos. Un paso que falta no es un error: `bg-brand-200` compilaba a nada y
  el elemento perdía el color en silencio.

- `contracts check` sale con `5` en vez de `1`; `doctor` sale con `2` si falla
  la configuración y `3` si falla el entorno. **Breaking** para cualquier cosa
  que asierte sobre el número, que es justo por qué pasa ahora y no después
  de 1.0.

### Arreglado

- **La regla de que un router no puede importar SQLAlchemy estaba documentada
  pero nunca se aplicaba.** `docs/agents.md` la pone primera en la tabla de
  reglas que atrapan código generado, y ningún `contracts.toml` generado la
  traía: la capa más externa declaraba `may_import` --que restringe otras
  *capas*, no paquetes-- y ningún `forbid_packages`. El mismo hueco estaba en
  los cuatro layouts, `adapters` en hexagonal incluido. Un servicio recién
  generado con `from sqlalchemy import select` en su router pasaba
  `jfast contracts check` con exit code 0.

  `forbid_packages = ["sqlalchemy"]` ahora viaja en `layers.http`
  (`layers.adapters` en hexagonal) en todas las plantillas de contrato. Los
  cuatro layouts se siguen generando y pasando su propio contrato; el import
  plantado ahora falla con
  `layer-package: 'http' must not import 'sqlalchemy'` y exit code 1.

  Los proyectos existentes no cambian: `contracts.toml` es tuyo una vez
  generado. Agrega la línea para adoptar la regla.

### Agregado

- [PLAN-CLI.md](PLAN-CLI.md) -- una propuesta para que la CLI sea una
  herramienta de ciclo de vida y no un generador: `adopt`, `analyze`,
  `inspect`, `migration check`, `extract`, exit codes estándar y `--json` en
  todos lados. Auditada contra la superficie de comandos actual antes de
  escribirla, que es como apareció el bug de contratos de arriba. Nada de ahí
  está aceptado todavía.

## [0.1.0a3] - 2026-08-29

### Documentación

- **Una landing separada del wiki.** La portada se renderizaba con el mismo
  marco que cualquier página de documentación -- una barra lateral de
  diecinueve enlaces, un selector de versión, un paginador -- así que llegar al
  proyecto era llegar ya metido en el manual. `index.html` ahora es su propia
  página y `docs.html` es el inicio de la documentación.
- **El README dice para qué sirve esto.** Abría con una categoría ("un
  framework FastAPI basado en plugins") y se metía derecho en un recorrido de
  quince secciones de features, escrito para alguien que ya decidió. Ahora
  arranca con para quién es, **para quién no es**, la forma de lo que se
  genera, y cuatro situaciones concretas para las que fue construido. La mitad
  de referencia queda igual; el problema nunca fue que existiera.
- **Una edición en español.** Cada página tiene una URL en español bajo `/es/`,
  y 24 de 26 están realmente traducidas -- el resto renderiza el fuente en
  inglés bajo un aviso que lo dice, en vez de servir inglés en silencio o dejar
  enlaces muertos en una barra lateral traducida. Una traducción vive en
  `docs/es/<name>.md` y reemplaza a la inglesa; agregar una página es soltar un
  archivo ahí.
- **`jfast dev` y la superficie de agentes están documentados**
  (`docs/dev.md`, `docs/agents.md`), junto con los cuatro layouts de módulo,
  los componentes base, los stores y `python -m jfastframework`. Diez cosas
  habían salido sin documentar; un chequeo sobre la documentación ahora no
  reporta ninguna.
- **Un switch claro/oscuro**, en lugar de "lo que diga el sistema operativo".
  Tres estados en vez de dos: una elección explícita, o seguir al sistema.
- **La barra lateral distingue etiquetas de enlaces.** Los títulos de grupo y
  las entradas eran ambos gris apagado en una sola columna, así que un título
  se leía como un enlace más chico. Ahora los títulos van en color de acento y
  monoespaciados con una línea después, y cada entrada lleva un ícono.
- **El idioma que elige quien lee se recuerda**, y se aplica **solo en la raíz
  del sitio**. Redirigir enlaces profundos significaría que una URL compartida
  deja a alguien en un lugar al que no hizo clic, y un crawler rebotado en cada
  página que pide no indexa nada.

### Corregido

- **`jfast start` escribía un archivo de compose que no podía arrancar.** Su
  propio panel de próximos pasos decía correr `docker compose up --build`, y
  ese comando fallaba: sin `.env` de workspace, compose se negaba a interpolar
  `${SHOP_DATABASE_PASSWORD}` en vez de darle un valor por defecto, y sin
  `.env` por servicio, que el archivo de compose lista como `env_file` y trata
  como error cuando falta. Ahora los dos se escriben junto al archivo de
  compose, así que ambos concuerdan por construcción y no por instrucción.
- **El formulario HTMX nunca enviaba.** `--ui htmx` montaba su router HTML en
  el mismo prefijo que el de JSON, declarando ambos los mismos verbos, así que
  respondía el que se registrara primero: navegar devolvía JSON y el
  formulario hacía POST contra el handler de la API. La superficie HTML ahora
  vive bajo `/ui/<table>`. También pasaba un dict pelado a un servicio que lee
  `payload.name`, y extendía un `base.html` que solo se entrega con
  `--kind web`. Tres fallas separadas en un mismo camino, ninguna de las cuales
  podían ver la generación, el import, el montaje ni `contracts check` -- solo
  enviar el formulario las encuentra, que es lo que ahora hace
  `scripts/smoke_htmx.sh` en los cuatro layouts.
- **Un error de validación cuyo input eran bytes devolvía 500, no 422.**
  pydantic pone el valor ofensor en el campo `input` del error, y un formulario
  posteado sin content type deja ahí el body crudo -- que el encoder de JSON no
  puede representar, así que serializar el 422 explotaba dentro del handler.
  Quien llamaba recibía un stack trace sobre `json.dumps` en vez del nombre del
  campo, y un status que invita a reintentar una request que nunca puede
  funcionar.

### Agregado

- **Cuatro layouts de módulo, y un prompt que pregunta cuál.** `layered`,
  `modular`, `screaming` y `hexagonal`. Un monolito modular no debería forzar a
  un catálogo y a un módulo de órdenes a la misma forma; el punto del límite es
  que cada lado pueda diferir. `jfast new module` pregunta cuando se omite
  `--layout`, y cae a `layered` sin preguntar cuando no hay terminal, así que
  un script o un job de CI no se cuelga en un prompt que nadie puede ver.
- **`jfast.toml` recuerda qué layout usó cada módulo.** Preguntar de nuevo cada
  vez termina dando una respuesta distinta, y adivinar por las carpetas en
  disco se rompe en el momento en que alguien agrega una. Se escribe donde vive
  el resto de la configuración del servicio, y el runtime lo ignora.
- **Un contrato por layout.** `contracts_modular` y `contracts_hexagonal`
  coinciden con las carpetas que sus layouts realmente crean; sin ellos
  `jfast contracts init --layout hexagonal` apuntaba a una plantilla que no
  existía. El hexagonal es el interesante: `domain/` no puede importar nada
  -- ni el ORM, ni FastAPI -- porque un dominio que importa SQLAlchemy ya dejó
  de pagar por el layout.
- **Los módulos se registran solos en `main.py`.** El frontend viene parcheando
  sus propias rutas y su menú desde el principio; el backend imprimía dos
  líneas y las dejaba para pegar a mano, así que un módulo generado quedaba
  inerte hasta que alguien lo hacía. Un módulo que no está montado se ve
  exactamente igual que un módulo que no funciona. No es fatal, por diseño: un
  `main.py` editado a mano que perdió sus marcadores recibe las líneas para
  pegar en vez de un scaffold deshecho.
- **`jfast dev`.** Contenedores arriba y esperados, migraciones aplicadas, y
  después la API y el frontend juntos. Cada etapa degrada y lo dice; el único
  hard stop es una migración que falla, porque un servidor sobre un esquema
  atrasado falla después, en una request que no tiene nada que ver con la
  columna que falta. También traduce el `.env` generado para un proceso en el
  host -- los hostnames de contenedor pasan a `localhost:<published port>` y
  `${...}` se interpola -- porque ninguna de las dos cosas es cierta fuera de
  la red de compose. Ctrl-C y SIGTERM se llevan a los hijos con ellos, lo que
  necesitó su propio handler: la disposición por defecto de SIGTERM mata al
  intérprete de una, y los hijos, al estar en sus propios grupos de proceso,
  habrían sobrevivido reteniendo los puertos.
- **`python -m jfastframework`.** Para cuando el console script no es
  alcanzable: una instalación de Windows donde `Scripts/` no está en el PATH,
  un virtualenv que nadie activó. El único module path que funcionaba antes
  imprimía un `RuntimeWarning` en cada invocación.
- **Componentes base y toasts, en ambos frontends.** `BaseButton`,
  `BaseInput`, `BaseModal`, `BaseBadge`, `SkeletonLoader`, `EmptyState` y un
  host de toasts, espejados entre Vue y React para que los dos sean el mismo
  producto. El loading es un esqueleto con la forma de lo que viene y no la
  palabra "Cargando", y el vacío dice qué estaría ahí y ofrece la acción que
  crea el primero.
- **Stores de Pinia y Zustand, conectados.** Pinia era una dependencia que
  ningún archivo generado importaba; React no tenía librería de estado en
  absoluto. Auth y notificaciones ahora vienen como stores, así que un toast
  levantado dentro de un servicio y uno levantado en un componente caen en la
  misma lista.
- **El spinner tiene quien lo llame.** `ui.working()` estaba escrito, era
  ASCII-safe, y no se invocaba desde ningún lado -- la función existía, la
  feature no.

### Cambiado

- **El color de marca es carmesí**, en lugar del azul placeholder, igualando el
  sitio de documentación y la terminal.

<!-- earlier in this cycle -->


### Corregido

- **`jfast init` y `jfast start` crasheaban en una consola de Windows.** No era
  un carácter roto -- era un `UnicodeEncodeError` levantado por `sys.stdout` a
  mitad de escribir un proyecto, así que el comando moría con un traceback
  habiendo creado ya la mitad. cp1252 no tiene `U+2713`; cp850 no tiene ni ese
  ni `U+203A`; los caracteres de bloque del banner no están en ninguno de los
  dos. Cada símbolo ahora se resuelve a través de `cli/glyphs.py` contra la
  codificación que la consola realmente reporta, y cae a ASCII -- paneles,
  guías de árbol y spinner incluidos. El chequeo es un `str.encode` real y no
  una lista de code pages conocidas, porque las terminales mienten sobre sí
  mismas y `PYTHONIOENCODING` pisa todo eso.
- **Tres hojas de estilo generadas apuntaban a un archivo que nunca se
  generaba.** `frontend_vue`, `frontend_react` y `service_web` le decían a
  quien leía que consultara `.jfast/skills/design-system/SKILL.md`, y ningún
  proyecto generado lo contenía. Un puntero a la nada es peor que ningún
  puntero: le cuesta el viaje a quien lee, y le enseña a un agente que las
  instrucciones de este proyecto no son confiables. La referencia ahora es
  condicional a que la skill se escriba, y un test recorre cada archivo
  generado para mantener a los dos en sincronía.

### Agregado

- **Un árbol en vez de un muro.** Un scaffold imprime cuarenta y pico de rutas,
  y en una columna plana la única línea que vale la pena leer -- un archivo que
  se dejó intacto porque ya existía -- se ve exactamente igual que las treinta
  y nueve que sí se escribieron. Cada comando que genera pasa por un único
  reporter, así que la forma de la salida se decide una vez y no por comando.
- **La superficie de agentes, opt-in: `--agent-docs`, o una pregunta en
  `jfast init`.** Un `AGENTS.md` y una skill bajo `.jfast/skills/`, reusando el
  layout que el repo del framework ya usa en vez de inventar un segundo lugar
  donde buscar convenciones. Un frontend además recibe la skill de diseño, que
  es lo que hace verdadera la referencia a la hoja de estilo de arriba. Apagado
  por defecto, porque un proyecto al que nadie le apunta un agente no le debe
  archivos de agente -- y cada archivo entregado es un archivo que puede
  desactualizarse.


## [0.1.0a2] - 2026-08-28

### Agregado

- **Una capa `shared/`, y un chequeo que dice cuándo usarla.** Dos módulos que
  se importan entre sí son un módulo con una carpeta en el medio: ninguno se
  puede extraer a un servicio después, y un cambio en uno rompe al otro de una
  forma que ningún test cubre. `jfast contracts check` ahora reporta el
  cross-import **y nombra el archivo al que mover el código**, así que el
  arreglo no necesita una discusión de diseño. La dirección se impone en los
  dos sentidos: los módulos importan `shared/`, `shared/` no importa ningún
  módulo -- sin esa segunda regla `shared/` se convierte en el lugar donde todo
  termina cayendo, que es el modo de falla de todo paquete `utils` jamás
  escrito.
- **`jfast new enum`**, que pregunta dónde va cuando no lo dices. La ubicación
  es la decisión; el archivo no. Empieza uno en el módulo que lo necesita y el
  chequeo te avisa el día que un segundo módulo lo quiera, así que nadie tiene
  que predecirlo. Los módulos generados y `shared/` traen ambos un `enums.py`
  usando `str, Enum`, porque un Enum pelado serializa como `Status.DRAFT` por
  algunos caminos y `"DRAFT"` por otros.
- **Canales declarados (plugin `channels`).** Reemplaza a un archivo de
  constantes string, que falla de tres formas: nada chequea el payload, el
  transporte queda soldado al call site, y nadie puede listar los canales que
  usa un sistema. Un `Channel` valida su payload **donde se construye el
  mensaje** y no en un worker a tres servicios de distancia, y carga su propio
  backend -- memoria por defecto y sin necesitar infraestructura, redis para un
  canal que algo en otro lenguaje también habla, kafka cuando un consumer que
  estuvo caído tiene que ponerse al día. Mezclarlos es el caso normal.

- **`jfast serve`.** Corre un servicio localmente, y se niega a arrancar cuando
  no hay un `jfast.toml` en el directorio -- que es el caso que antes booteaba
  en silencio con los defaults del framework, sin base de datos y sin quejarse.
  Se ata a loopback y no a `0.0.0.0`, porque un servidor de desarrollo no
  debería estar en la red salvo que lo digas.
- **Plugin `mail`.** Plantillas, tres backends, y **encolado por defecto**: un
  servidor de correo lento o que rechaza un rato no debería volverse la
  latencia o el error de la request que lo disparó. `send_now()` es la
  escapatoria síncrona y se lee como tal. El backend por defecto es `console`,
  así que nadie le manda un mail a un cliente desde una laptop por accidente y
  no hacen falta credenciales para desarrollar. En producción con el backend
  smtp se niega a arrancar sin credenciales en vez de fallar en el primer
  envío.
- **`jfast add` y un catálogo de capacidades.** Excel y armado de PDF, HTML a
  PDF, XML grande, dataframes, visión, validación, locale, reintentos. Nada se
  instala por defecto: un servicio que sirve JSON no debería cargar numpy, y el
  grafo de plugins deja de describir al servicio en el momento en que lo hace.
  En un workspace con varios backends pregunta cuál, porque agregar una
  dependencia pesada al servicio equivocado es invisible hasta que se construye
  la imagen.
- **`jfastframework.exports.pdf`**, para armar muchos documentos. `merge()`
  **reporta lo que no pudo incluir** -- la implementación obvia loguea un
  warning y devuelve un paquete que se ve completo, lo que para un paquete
  fiscal o legal es peor que un error. También hace el merge en lotes, porque
  `PdfWriter.append()` retiene todas las páginas hasta `write()` y si no la
  memoria pico crece con el trabajo entero.
- **`jfastframework.exports.excel`**, usando el modo write-only de openpyxl
  para que un cursor se pueda transmitir a un archivo sin retener entero a
  ninguno de los dos.
- **El instalador se renderiza con `rich`** -- que viene con Typer, así que no
  hay dependencia nueva. Un banner, opciones tabuladas, un resumen antes de que
  se escriba nada, y los próximos pasos con lo que hace cada comando al lado.

### Corregido

Seis defectos que salieron en `0.1.0a1`. Juntos significaban que un servicio
generado no se podía instalar, no se podía construir en una imagen, no podía
responder un GET, y no podía responder un PATCH. Cada uno está ahora cubierto
por un test, y por `scripts/smoke_docker.sh`, que construye la imagen generada y
la corre contra un PostgreSQL real -- el chequeo cuya ausencia dejó pasar a los
seis.

- **Toda ruta que tomaba una sesión de base de datos respondía 422.**
  `session_dependency(request: Any)`: FastAPI decide qué *es* un parámetro de
  dependencia a partir de su anotación, y de `Any` concluyó lo único que
  quedaba -- un query parameter obligatorio. Reproducido desde el schema de
  OpenAPI (`name='request' in='query' required=True`), no inferido. Ahora está
  anotado `Request`.
- **Todo update devolvía 500 una vez que se serializaba un timestamp.**
  `TimestampMixin.updated_at` carga `onupdate`, que SQLAlchemy expira en el
  flush; la lectura siguiente -- Pydantic construyendo la respuesta -- intentó
  IO dentro de una corutina y levantó `MissingGreenlet`. El mixin ahora pide
  `eager_defaults`, así que PostgreSQL devuelve el valor con `RETURNING` en la
  misma sentencia. Un `session.refresh()` también habría funcionado, al costo
  de un SELECT en cada escritura, incluidas las escrituras que nunca leen un
  timestamp.
- **El Dockerfile generado no podía construir.** `COPY pyproject.toml ./`
  nombraba un archivo que el generador nunca escribe, y COPY falla cuando su
  origen no está. Ahora usa glob, como siempre lo hizo la línea
  `requirements.txt*` justo debajo.
- **Un contenedor contra una base vacía respondía 500 a todo.** Nada corría las
  migraciones. La imagen ahora tiene un entrypoint que corre
  `alembic upgrade head` y después hace `exec` de uvicorn: `set -e` detiene el
  contenedor ante una migración fallida en vez de servir un esquema a medio
  migrar, y `exec` mantiene a uvicorn como PID 1 para que reciba SIGTERM.
  `create_all` se descartó como arreglo -- construye un esquema del que Alembic
  no sabe nada, y la primera migración real después diverge en silencio.
- **La búsqueda del workspace subía hasta la raíz del sistema de archivos.**
  Correr `jfast start` una vez en un directorio home dejaba ahí un archivo de
  workspace, y todo proyecto por debajo se sumaba: un archivo de compose, un
  espacio de puertos, servicios sin relación registrándose entre sí, y nada
  fallando. La búsqueda ahora se detiene en el directorio home y en un `.git`,
  porque la raíz de un repositorio es donde termina un proyecto.
- **Un servicio arrancado desde el directorio equivocado booteaba mal
  configurado en silencio.** `session_dependency` metía la mano directo en
  `request.app.state.jfast` y levantaba `KeyError: 'jfast'` en cualquier app
  que este framework no hubiera construido. Ahora pasa por `get_context()`, que
  lo dice.

### Agregado

- **`scripts/smoke_docker.sh`**, con gate en CI. Construye la imagen que el
  generador escribe, la corre contra un PostgreSQL real, y afirma tres cosas:
  que una migración fallida detiene el contenedor con el error propio de la
  base, que una exitosa deja atrás una tabla `alembic_version`, y que `/ready`
  reporta la base sana desde adentro del contenedor.

- **Todo servicio generado traía un `requirements.txt` que pip no podía
  satisfacer.** La plantilla llevaba un literal `jfastframework[...]~=0.7`, que
  sobrevivió a la renumeración a `0.1.0a1`, así que
  `pip install -r requirements.txt` en un proyecto scaffoldeado fallaba con *No
  matching distribution found*. El pin ahora se deriva de la versión propia del
  framework con `framework_pin()`.

  Un pre-release se fija **exacto**, porque `~=0.1` tampoco matchea con
  `0.1.0a1`: una cláusula de release compatible se normaliza a
  `>= 0.1, == 0.*` y `0.1.0a1` ordena por debajo de `0.1.0`, así que queda
  fuera de rango incluso con `--pre`. Cuando el framework llegue a un release
  final el pin pasa a ser `~=major.minor` por sí solo.

  Ahora falla un test si alguna plantilla de requirements vuelve a hardcodear
  una versión. La resolución en sí deliberadamente no se chequea en CI: al
  momento del release la versión que se está fijando todavía no está publicada,
  así que ese chequeo fallaría justo en el commit que es correcto.

### Agregado

- **El sitio de documentación tiene la identidad propia del proyecto.** Un
  monograma (`mark.svg`) y un favicon en negro y carmesí, en lugar del
  placeholder de letras-en-una-caja y el favicon emoji.
  `docs-site/assets/BRAND.md` dice dónde va el búho; el sitio cae al monograma
  cuando no está, así que un binario faltante no puede romper el build.
- **La barra lateral está agrupada** en Start here, Build, Run, Guard y
  Project. Veintidós enlaces planos son una lista que nadie escanea.
- **Un índice "on this page"** en cualquier página con tres o más secciones,
  enlaces anterior/siguiente en orden de lectura, y un botón de copiar en cada
  bloque de código.
- **Una página de Madurez**, que renderiza `STATUS.md`. Es la página más útil
  del sitio para cualquiera que esté decidiendo si depender de una parte de
  esto.
- **`docs/local-setup.md` abre con un solo bloque de copiar y pegar** que va de
  cero a un stack corriendo. Se ejecuta de punta a punta antes de entregar, no
  se escribe de memoria.

- **El grafo de recursos.** Los datastores son instancias con nombre que el
  workspace posee (`[[workspace.resources]]`), y un servicio se ata a una bajo
  una variable (`uses = [{ resource = "core-db", as = "JFAST_DB_DSN" }]`). Dos
  bases de datos del mismo tipo, y un cache compartido por dos servicios, ahora
  son ambos expresables; antes no lo era ninguno.
- **El DSN se genera.** `jfast workspace env` escribe el `.env` de cada
  servicio desde sus bindings. El archivo de compose antes emitía un contenedor
  de datastore y le dejaba el connection string a una persona, que es de donde
  venía la desincronización.
- **Una contraseña por recurso**, generada en un `.env` de workspace ignorado
  por git y nunca sobrescrita una vez fijada. Reemplaza al único
  `POSTGRES_PASSWORD` de todo el workspace, donde una filtración en cualquier
  lado era una filtración en todos.
- `jfast workspace resource`, `jfast link`, `jfast unlink`,
  `jfast workspace validate`, `jfast workspace migrate-resources` y
  `jfast workspace graph` (mermaid o dot, con las aristas etiquetadas con la
  variable).
- Atar dos recursos a una variable se rechaza, y `validate` reporta un puerto
  reclamado dos veces, un binding a un recurso que no existe, y un recurso que
  nadie usa.

### Corregido

- **El hero del sitio publicitaba `pip install jfastframework`,** que no
  resuelve porque no se publicó nada. Ahora muestra el clone-and-install que sí
  funciona hoy, y dice por qué todavía no está en PyPI.
- Un em dash mal codificado en el texto del hero, que venía renderizando como
  `â` desde que se escribió la página.

### Corregido

- **Todo script de smoke reportaba éxito cuando fallaba.** `trap 'rm -rf
  "${WORK}"' EXIT` termina con un `rm` exitoso, y bash le pasa al script el
  status del trap -- así que una aserción fallida salía con 0 y CI se ponía en
  verde. Los nueve ahora preservan el código de falla.

- **La cola de Redis no tenía visibility timeout.** `visibility_timeout` se
  guardaba y nunca se leía, y la recuperación solo drenaba la lista de
  procesamiento del propio worker -- bajo una clave que incluía `id(self)`, una
  dirección de memoria. Un worker que moría volvía bajo otro nombre y nunca
  recuperaba sus propios jobs en vuelo, así que la garantía que `queues.base`
  documenta para todo backend aquí no existía. Los workers ahora se registran en
  un hash con un heartbeat sobre la hora del servidor, y cualquier worker
  devuelve los jobs de un consumer cuyo heartbeat quedó viejo. `close()`
  entrega el trabajo de vuelta de inmediato, así que un deploy rolling no deja
  jobs estacionados hasta que expire el timeout.
- **`/ready` corría sus chequeos en serie y sin timeout.** Una dependencia
  colgada a nivel TCP mantenía la probe abierta hasta que el socket se rendía.
  Los chequeos ahora corren concurrentemente bajo `readiness_timeout` (2s por
  defecto), y un timeout se reporta como `timeout` y no como `fail` -- uno
  significa que la dependencia dijo que no, el otro que nunca respondió.
- **`BaseRepository.paginate()` no emitía `ORDER BY`.** Las páginas no eran
  estables: una fila podía aparecer dos veces mientras otra nunca se devolvía.
  El orden ahora cae por defecto en la clave primaria y se puede sobrescribir
  por repositorio.
- **El filtro de tenant fallaba abierto.** Un repositorio al que se le daba un
  `tenant_id` para un modelo sin esa columna devolvía en silencio las filas de
  todos los tenants. Ahora levanta en la construcción; un modelo genuinamente
  global declara `tenant_scoped = False`.

### Agregado

- **Protecciones de borde en el kernel**, todas apagadas salvo que se
  configuren: CORS, `TrustedHostMiddleware`, un límite de tamaño de body que
  responde 413, y un timeout de request que responde 504. Caddy cubre estas
  cuando está adelante; `jfast deploy function` pone un servicio en Lambda sin
  nada adelante. Orígenes CORS con comodín combinados con credenciales se
  rechaza en el boot, porque los browsers rechazan ese par y si no fallaría en
  silencio.
- `safe_identifier()` valida cualquier nombre de tabla interpolado en SQL, en
  el punto en que entra, así que la interpolación que sigue es demostrablemente
  segura.
- `pip-audit` y `bandit` corren en CI como gates duros. Cada hallazgo existente
  está exonerado explícitamente con su razón, o corregido.
- Tests para la cola de Redis contra un doble en memoria de los comandos que
  emite, y para el repositorio contra SQLite. 380 tests en total.

### Cambiado

- `/docs` y `/openapi.json` se cierran cuando `env = prod` salvo que se seteen
  explícitamente. `/info` ya lo hacía; los tres son ahora una sola regla.
- La query de claim de PostgreSQL se movió a una constante `CLAIM_SQL` con
  nombre, construida una vez por llamada en vez de ensamblada inline.
- `RabbitMQQueue` ya no toma `visibility_timeout`. El broker reentrega los
  mensajes sin acknowledge cuando se cierra un canal, así que el parámetro
  nunca hizo nada, y uno que no hace nada es una promesa que quien llama se
  cree.


### Agregado

- **Regla de contrato `async-blocking`.** `jfast contracts check` ahora reporta
  llamadas que frenan el event loop desde adentro de un `async def`: los casos
  de la librería estándar, los clientes síncronos que este framework trae
  (boto3, pymongo, psycopg2, redis sync, sqlite3), un cliente bloqueante
  guardado en `self`, y un salto hacia un helper síncrono definido en el mismo
  archivo. El offloading correcto vía `asyncio.to_thread` y compañía se
  reconoce y se deja en paz. Configurable bajo `[rules.async_safety]`;
  exonerable inline.

### Cambiado

- El conjunto de reglas `ASYNC` de Ruff está habilitado para el framework.
  `ASYNC109` se ignora con una razón: quiere un cancel scope en vez de un
  parámetro `timeout`, y `dequeue(timeout=...)` mapea a una primitiva del
  broker.

### Corregido

- Plugin `web`: la probe de readiness corría dos `Path.is_dir()` bloqueantes en
  el event loop, una vez por probe por réplica. Ahora está offloadeado.


## [0.7.0] - 2026-08-28

Archivos, tenants, y los tres servicios de nube que una app desplegada termina
buscando.

### Agregado

**Plugin `storage`**
- Discos con nombre y una visibilidad, modelados sobre los de Laravel: el
  código escribe a `storage.disk("private")` y dónde vive eso es
  configuración.
- Drivers local y S3/MinIO detrás de un único protocolo `StorageBackend`. Un
  disco local escribe atómicamente (archivo temporal + `replace`) para que
  quien lee nunca vea un objeto parcial.
- Un disco privado **se niega** a producir una URL permanente.
  `temporary_url()` firma la clave *y* la expiración con HMAC, comparadas en
  tiempo constante — firmar solo una de las dos convierte a un único enlace
  válido en una llave a todo el disco.
- Toda clave se valida antes de llegar a un filesystem o a un bucket:
  traversal, rutas absolutas, backslashes y bytes nulos rechazados, `..`
  resuelto primero. Los discos locales re-chequean después de resolver, porque
  un symlink adentro de la raíz igual puede apuntar afuera.
- Las descargas van con `Content-Disposition: attachment` + `nosniff`. Un
  `.html` o `.svg` subido y servido inline corre el script de quien lo subió en
  tu origen.
- Los enlaces expirados y los falsificados devuelven el mismo 403 con el mismo
  mensaje.
- MinIO en el archivo de compose generado en el offset de puerto `+6`, opt-in.

**Plugin `tenancy`**
- Resuelve el tenant desde un claim del token, un subdominio, un prefijo de
  ruta o un header, en ese **orden de confianza**. `header` no está en la lista
  por defecto y avisa en producción: `X-Tenant-ID: acme` está a un curl de
  distancia de los datos de otro tenant.
- El parseo de subdominio rechaza hosts de múltiples labels, el dominio base
  pelado, y una lista reservada (`www`, `api`, `admin`, …). `base_domain` es
  obligatorio, o si no cada hostname parece un tenant.
- `require_tenant` devuelve problem+json 403, con health, métricas y docs
  exentos para que las probes sigan pasando.
- `jfast workspace caddy --wildcard-tenants` emite un bloque de sitio con
  comodín con TLS on-demand **y** el endpoint `ask` que lo controla. Sin `ask`,
  cualquiera que apunte DNS hacia ti quema tu rate limit de certificados.

**Login social (`auth`)**
- Presets de Google, Microsoft y GitHub; cualquier otro proveedor por sus
  endpoints.
- `/auth/{provider}/start` y `/auth/{provider}/callback`, con el state y el
  nonce viajando en una cookie httponly, samesite=lax y ambos verificados a la
  vuelta.
- Los ID tokens se verifican por audiencia y emisor. Sin el chequeo de
  audiencia, un token emitido para la app de Google de cualquier otro loguea
  aquí.
- `@auth.on_identity` es donde una identidad verificada se vuelve tu usuario.
  Que falte es un 500, no un 200 alegre.
- `OIDCIdentity.federated_id` está calificado por proveedor, porque los ids de
  subject son únicos por proveedor y no globalmente.

**Secrets**
- `load_secrets()` puebla `os.environ` desde AWS Secrets Manager o Google
  Secret Manager antes de `create_app()`. Un valor de entorno existente gana
  salvo que se sobrescriba; solo se loguean nombres, nunca valores; el JSON
  anidado se rechaza en vez de recibir un nombre aplanado impredecible.

**Serverless**
- `jfast deploy function <name> --target aws|gcp` escribe el handler, el
  Dockerfile y un script de deploy — y no los corre.
- Ambos targets corren la misma app ASGI que corre el contenedor. Privados por
  defecto en las dos nubes; `--public` opta por lo contrario y avisa.

**Plugin `notifications`**
- Firebase Cloud Messaging sobre la API HTTP v1, con un backend `console` que
  loguea en vez de enviar para desarrollo y tests.
- Los tokens de dispositivo no registrados se reportan de vuelta para poder
  borrarlos.
- No está verificado contra un proyecto FCM real en CI; la construcción del
  payload sí.

### Corregido

- **El plugin `observability` ya no pisa un tenant resuelto.** Confiaba en
  `X-Tenant-ID` incondicionalmente y aplastaba `request.state.tenant_id` al
  pasar, así que un tenant resuelto desde un claim firmado quedaba reemplazado
  por `None` antes de que corriera el handler. Ahora llena el hueco solo cuando
  nada más resolvió uno.
- **`tenancy` corre lo más adentro posible.** `add_middleware` pone el
  middleware *más afuera*, lo que corría tenancy antes que auth y dejaba a la
  fuente `token` firmada permanentemente ilegible. Ahora se appendea.
- **`secrets.parse` rechaza un array JSON** en vez de caer al parser
  `KEY=value` y cargar nada en silencio.

### Agregado (interno)

- `errors.problem_response()` para middleware, que corre por fuera de los
  exception handlers de FastAPI y si no expondría un 500 con un stack trace.

## [0.6.0] - 2026-08-27

Autenticación JWT, y manifiestos de Kubernetes derivados del contrato del
servicio.

### Agregado

**Plugin `auth`**
- Verificación en tres modos: `jwks` (traer las claves públicas del emisor — el
  default, y el único sensato entre servicios), `public_key` (un PEM fijado),
  `secret` (HMAC, para un servicio único).
- `require_auth`, `require_scopes(...)`, `require_roles(...)`, `optional_auth`
  como dependencias de FastAPI. 401 para "quién eres", 403 para "no puedes".
- Cliente JWKS con caché, rotación ante un `kid` desconocido, y un rate limit
  en el refresh para que no se puedan usar `kid`s falsificados para martillar
  al proveedor de identidad. Las claves cacheadas siguen funcionando durante
  una caída de JWKS; `/ready` reporta la antigüedad.
- Emisión de tokens para un servicio que tiene su propio login, con **rotación
  de refresh y detección de reuso**: un refresh token reproducido revoca toda
  la familia de la sesión.
- Revocación: `POST /auth/logout` deniega el `jti` y su familia de refresh,
  respaldado por Redis cuando el plugin `cache` está activo. El fallback en
  memoria se reporta a sí mismo como no compartido en vez de fingir.
- `GET /auth/me` devuelve identidad y permisos — nunca el token, nunca los
  claims crudos.

**Decisiones de seguridad, cada una con un test**
- Los algoritmos se fijan por configuración y se pasan explícitamente al
  decoder, así que `alg: none` y la confusión RS256→HS256 se rechazan las dos.
  Configurar algoritmos simétricos y asimétricos juntos se rechaza en el
  arranque: esa combinación *es* el ataque.
- `aud` e `iss` se verifican — apagados por defecto en la mayoría de las
  librerías, y sin ellos un token para un servicio hermano se acepta aquí.
- El margen de expiración es de 30 segundos, no de minutos.
- Las razones de rechazo van al log; el cliente recibe un 401 pelado.
- **`tenant_id` ahora viene de un claim firmado**, no del falsificable header
  `X-Tenant-ID`. Esta es la razón principal de seguridad para habilitar auth.

**Kubernetes**
- `jfast workspace k8s` — un árbol de kustomize: Deployment, Service,
  ConfigMap, HPA y PodDisruptionBudget por servicio, un Ingress, overlays
  `dev`/`prod`.
- `jfast init` pregunta si lo necesitas.
- Las liveness probes van a `/health`, las de readiness a `/ready` — el
  contrato de dos endpoints es lo que evita que un parpadeo de la base de datos
  reinicie todos los pods sanos. Una startup probe permite 150s para un primer
  boot lento.
- No-root, sistema de archivos raíz de solo lectura, capabilities descartadas,
  `maxUnavailable: 0`, y un PDB para que un drenaje de nodo no pueda llevarse
  todas las réplicas.
- El Ingress sirve `/api`, la misma forma que el Caddyfile generado, así que el
  build del frontend es idéntico local y en el cluster.

### No se genera, deliberadamente
- **Bases de datos.** Un StatefulSet para PostgreSQL salido de un scaffolder es
  la forma en que la gente pierde datos. Los manifiestos leen un DSN desde un
  Secret.
- **Secretos reales.** `*-secrets.example.yaml` tiene placeholders.
- **Un endpoint de login.** Chequear una contraseña contra tu tabla de usuarios
  es trabajo de la aplicación; `auth.issuer` se provee para tu propia ruta.
- **NetworkPolicies, ServiceMonitors, Jobs de migración, Helm.** Cada uno
  necesita una decisión sobre tu sistema que un generador no debería adivinar.

### Notas
- Los manifiestos se validan como YAML y se afirman estructuralmente en
  `tests/test_kubernetes.py`. **No** han sido aplicados a un cluster real en
  CI. Trata el primer `kubectl apply` como el test.

## [0.5.0] - 2026-08-27

Contratos por proyecto, y un quickstart que CI realmente ejecuta.

### Agregado

**Contratos**
- `contracts.toml` en cada servicio generado: alcance (`owns` /
  `does_not_own`), límites de capa, llamadas prohibidas, estructura requerida,
  interfaces declaradas, e invariantes que ningún checker puede verificar.
- `jfast contracts init | check | show --json | render | waivers`. `check` sale
  con código distinto de cero, así que rompe un build en vez de imprimir
  consejos.
- Un checker estático, basado en AST: límites de capa (imports relativos y
  absolutos), paquetes prohibidos por capa, llamadas prohibidas con la razón
  adjunta, y archivos requeridos por módulo.
- Exoneraciones inline — `# contracts: allow <reason>` — con la razón
  obligatoria y `jfast contracts waivers` listando todas.
- El contrato se valida antes que el código: dos capas reclamando una misma
  ruta, o un `may_import` nombrando una capa que no existe, se reportan como
  errores de contrato en vez de producir respuestas confiadas a la pregunta
  equivocada.
- `CONTRACTS.md` se genera desde el mismo archivo, así que el documento y la
  regla que se aplica no pueden estar en desacuerdo.
- `.jfast/skills/respect-contracts/SKILL.md`, y `AGENTS.md` ahora abre con
  `jfast contracts show --json`.

**Primeros pasos**
- `docs/local-setup.md` — instalar desde un checkout, generar un proyecto, el
  ciclo que realmente usas, y los modos de falla que vale la pena conocer.
- `scripts/smoke_docs.sh` corre esos comandos **exactamente como están
  documentados**, en CI. Documentación que nunca se ejecutó es una suposición.

### Corregido
- **Un servicio generado con `queue` habilitado no podía arrancar sin
  PostgreSQL.** `setup()` levantaba en el arranque, así que el proceso entraba
  en crash-loop con un traceback de asyncpg en vez de servir. Ahora arranca,
  loguea la razón, y se reporta a sí mismo como no listo — un orquestador
  maneja "no listo" con elegancia y maneja un crash loop paginando a alguien.
  El mismo arreglo para el `auto_migrate` de `rag`.
- Los defaults del contrato layered reclamaban `modules/*/schemas.py` para dos
  capas. Lo agarró un servicio recién generado fallando su propio contrato, que
  es exactamente el chequeo que debería agarrarlo.
- El matcheo de capas ordenaba los patrones por longitud de string, así que el
  catch-all del layout screaming `modules/*/[!_]*.py` le ganaba a
  `modules/*/http.py` y clasificaba todo router como código de dominio. Ahora
  ordena por especificidad — menos comodines.

## [0.4.0] - 2026-08-27

Servicios políglotas, colas y eventos, arranque de un solo comando, Caddy en el
borde, y un sitio de documentación — más verificación real de build para todo lo
que hasta ahora solo se había verificado con grep.

### Agregado

**`jfast start`**
- Un comando para el default opinado: un monolito modular en Python con
  PostgreSQL + pgvector, Redis, jobs en background y un módulo inicial, un
  frontend Vue, un Caddyfile y un archivo de compose de workspace.
- Un monolito en vez de tres servicios a propósito: partirlo después es una
  jugada, despartirlo es una reescritura.

**Servicios políglotas**
- `docs/service-contract.md` — el contrato que satisface todo servicio JFast
  sin importar el lenguaje: `/health`, `/ready`, `X-Request-ID`, problem+json,
  configuración `JFAST_*`, bloques de diez puertos, logs JSON en stdout.
- `jfast new service --language go` — un servicio en Go con **cero
  dependencias de terceros**, que implementa el contrato en unas 300 líneas
  vendoreadas. CI corre `go vet`, `go test`, `go build`, arranca el binario y le
  hace curl.
- `jfastframework/languages.py` — el registro de lenguajes. Solo necesitas el
  toolchain de los lenguajes que realmente uses.

**gRPC**
- `--grpc` genera el contrato `.proto` (health, problem, un servicio de
  dominio) y reserva el offset de puerto +9. **Solo el contrato:** no se
  generan stubs y no se cablea ningún servidor, porque fijar una versión de
  `protoc` adentro de un scaffolder hace que los stubs generados no coincidan
  con lo que sea que tenga CI. Ver `proto/README.md`.

**Colas**
- Plugin `queue` con un protocolo `QueueBackend` y tres backends: PostgreSQL
  (`FOR UPDATE SKIP LOCKED`, encolado transaccional), Redis (`BLMOVE` a una
  lista de procesamiento por worker), RabbitMQ (dead-letter exchange con un TTL
  para las demoras).
- `TaskRegistry` y `Worker`: backoff exponencial acotado, dead-lettering,
  timeouts de job, drenaje de lo que está en vuelo al apagar, despertar
  inmediato al detener.
- `GET /queue/stats`.

**Eventos**
- Plugin `events` — publish/subscribe de Kafka con claves de partición,
  offsets commiteados después de manejar, e infraestructura en modo KRaft (sin
  ZooKeeper).

**Borde y deploy del workspace**
- `jfast workspace compose` — un archivo de compose para cada servicio, sea
  cual sea su lenguaje, con datastores por servicio.
- `jfast workspace caddy` — un Caddyfile que pone al workspace detrás de un
  solo hostname. Los backends viven bajo `/api` con o sin gateway, así que el
  build de producción del frontend sigue funcionando el día que aparezca uno.
- Los frontends ganaron `.env.production` con `VITE_API_URL=/api` — relativo,
  así que no hay CORS ni un rebuild por entorno.

**Sitio de documentación**
- `docs-site/build.py` renderiza el markdown propio del repositorio a un sitio
  estático y versionado; `docs-site/check.py` valida enlaces, anclas, assets,
  tokens de tema y artefactos de plantilla sin renderizar.
- `.github/workflows/pages.yml` lo publica, reconstruyendo cada versión minor
  publicada desde su propio tag para que las versiones viejas sigan
  funcionando.

### Corregido
- **Los routers generados de Vue y React no eran JavaScript válido.** El
  comentario marcador `/*nuevaRuta*/` estaba adentro de un comentario de bloque
  `/* … */`, cuyo `*/` interno cerraba el comentario antes de tiempo. Todo
  chequeo basado en grep pasaba — el marcador *estaba* ahí — y solo
  `vite build` lo agarró. Ambos frontends ahora instalan y construyen en CI.
- **El worker hacía busy-wait con cualquier backend no bloqueante.** PostgreSQL
  hace polling y devuelve al instante cuando la cola está vacía, así que el
  loop nunca cedía: quemaba un core y mataba de hambre al event loop, lo que
  significaba que los handlers HTTP del mismo proceso dejaban de responder
  mientras "el worker está corriendo".
- El frontend de `jfast start` llamaba a un puerto de dev que Caddy servía bajo
  otra ruta. Ahora los dos coinciden en `/api`.

### Cambiado
- `PLUGIN_CATALOG` ganó `queue` y `events`; `--with queue` arrastra un backend
  que el servicio realmente puede alcanzar, igual que hace `rag`.
- `SERVICE_KINDS` y el instalador ofrecen Go.
- CI ganó tres jobs: Go (`setup-go`), frontend (`setup-node`) y el sitio de
  docs. El job de frontend existe por el bug del router de arriba.

### No hecho, deliberadamente
- **Angular** no se genera. Un `angular.json` hecho a mano que nunca corrió
  bajo `ng serve` parece terminado y falla de una forma difícil de atribuir.
- **React Native** no se genera, por la misma razón.
- **RabbitMQ y Kafka** están escritos contra APIs documentadas pero no se
  probaron de ida y vuelta contra brokers reales en CI.
- **Las extensiones de Laravel y .NET** no están empezadas. El contrato del
  servicio es el punto de extensión; un lenguaje necesita un `LanguageSpec`, un
  árbol de plantillas y un job de CI que construya lo que genera.

## [0.3.0] - 2026-08-27

Workspaces multi-servicio, un API gateway, generación de frontend, y el setup de
migraciones/testing que el release anterior solo *documentaba*.

### Agregado

**Workspaces**
- `jfast.workspace.toml` y `jfastframework.workspace`: los servicios se
  registran solos, toman el siguiente bloque libre de diez puertos, y el
  archivo registra a qué debería llamar el frontend.
- `jfast workspace init | list | gateway | env`.

**API gateway**
- Plugin `gateway`: reverse proxy basado en prefijos con stripping de headers
  hop-by-hop, propagación de `X-Request-ID`, y 502/504 como problem+json.
- Se genera automáticamente en cuanto un workspace tiene más de un backend. Con
  un solo backend deliberadamente no se genera.
- No es un catch-all: solo los prefijos configurados se proxean, así que el
  gateway conserva sus propios `/health`, `/ready` y `/metrics`. La readiness
  no sondea a los upstreams, así que un reinicio no tumba a todo el sistema.

**Frontends**
- `jfast new service <name> --kind spa --frontend vue|react` — proyecto Vite +
  Tailwind v4 con una página de inicio funcional que llama al `/health` del
  backend al cargar.
- `jfast new view <Name>` — la estructura
  `Modulo<Name>/{Components,Pages,Routes,Services}`, registrada en el router y
  en la barra lateral en comentarios marcadores.
- `jfastframework.cli.patcher`: parcheo idempotente, ruidoso, que preserva los
  marcadores. Re-correr el generador no duplica; un marcador faltante levanta
  con la ruta en vez de no hacer nada en silencio.
- El framework se autodetecta desde el proyecto, así que no hay que repetir
  `--frontend`.

**Migraciones y tests en los servicios generados**
- `alembic.ini`, `migrations/env.py`, `script.py.mako` y `versions/`. `env.py`
  lee el `JFAST_DB_DSN` propio de la app y auto-importa los modelos de cada
  módulo, así que el autogenerate no puede emitir una migración vacía en
  silencio. `compare_type` y `compare_server_default` están activos.
- `pytest.ini` y un `conftest.py` con fixtures `app` / `client`.

**Selección de datastores desde la terminal**
- `jfast init` — instalador interactivo: tipo, frontend, datastores, puerto.
- `jfast new service --with database,cache,qdrant,rag` — la lista de plugins,
  los bloques `[plugin.*]`, las claves del `.env` y los extras fijados se
  derivan todos de ahí.
- El store de `rag` se infiere de los datastores elegidos, así que
  `--with qdrant,rag` no puede generar un servicio configurado para pgvector.

### Cambiado
- **Breaking:** `jfast new service --port` ahora cae por defecto al siguiente
  bloque libre del workspace en vez de 8000.
- `SERVICE_KINDS` ganó `spa` y `gateway`.
- Las plantillas `.vue`, `.jsx` y `.tsx` se renderizan por el entorno Jinja de
  corchetes, así que la interpolación de Vue y las llaves de JSX sobreviven al
  scaffolding.

### Corregido
- **CI:** `mypy --strict` fallaba porque `redis.asyncio.from_url` no está
  tipado en algunos releases de redis y sí anotado en otros — una corrida
  estricta que pasaba localmente y fallaba en CI por nada que hubiéramos
  escrito. Los paquetes opcionales de terceros ahora son
  `follow_imports = "skip"`, que es honesto sobre tipos que ni controlamos ni
  podemos dar por seguros en toda la matriz de soporte.
- **CI:** `scripts/smoke.sh` hardcodeaba `.venv/bin/python`, que no existe en un
  job de CI. Ahora cae al `PATH`.
- Se eliminó el marcador muerto de dependencia `tomli` (`requires-python` ya es
  `>=3.11`).

## [0.2.0] - 2026-08-27

Los datastores pasaron a ser una elección, y un frontend pasó a ser un servicio.

### Agregado

**Datastores**
- Protocolo `VectorStore` en `jfastframework.vectors`, con `Chunk` y
  `SearchHit` como vocabulario compartido. Cada store normaliza su score a
  similitud coseno en [0, 1].
- Plugin `qdrant` — cliente, health check, contenedor con puertos HTTP y gRPC.
- Plugin `mongo` — cliente Motor y handle de base de datos.
- `rag` ahora elige su store desde la configuración: `pgvector`, `qdrant`, o
  una ruta con puntos a tu propia clase. Lo mismo para el embedder.
- `InfraService.extra_ports` para contenedores que exponen más de un puerto.

**Frontends renderizados en el servidor**
- Plugin `web` — plantillas Jinja2, archivos estáticos, y `render()` con
  renderizado parcial de HTMX: una navegación del browser recibe la página, un
  `hx-get` recibe el fragmento, desde un solo handler.
- Manejo de errores consciente de HTMX: un `JFastError` levantado durante una
  request HTMX devuelve un fragmento HTML en vez de `problem+json`, que HTMX si
  no metería en el DOM como texto crudo.

**Generador**
- `jfast new service <name> [--kind api|web]` — scaffoldea un servicio entero.
- `jfast new module <name> [--layout layered|screaming] [--ui api|htmx]`.
- Layout `module_screaming`: dominio libre de framework, un archivo por caso de
  uso, almacenamiento y HTTP en los bordes, tests de dominio separados de los
  tests de casos de uso.
- Overlay `ui_htmx` — compuesto sobre cualquiera de los dos layouts en vez de
  duplicado, así que tres árboles de plantillas cubren las cuatro
  combinaciones.
- Dos entornos Jinja en el scaffolder: las plantillas `.html.j2` usan `[[ ]]`
  para los valores de scaffolding para que el `{{ }}` de runtime que el browser
  necesita sobreviva.
- Los nombres de tabla se pluralizan, lo que además esquiva las palabras
  reservadas de SQL con las que los sustantivos en singular no dejan de chocar
  (`order`, `user`, `group`). Se sobrescribe con `--table`.

**Docs**
- `docs/modules.md`, `docs/datastores.md`.

### Cambiado
- **Breaking:** `jfast new <name>` ahora es `jfast new module <name>`.
- **Breaking:** `[plugin.rag] table` se renombró a `collection` — nombra una
  colección de Qdrant tan seguido como una tabla de PostgreSQL ahora.
- `rag` ya no requiere `database` de forma dura. Declara `after` y valida el
  store con el que realmente fue configurado, nombrando el plugin faltante.
- Las plantillas de módulo se movieron a `module_layered/`; ambos layouts ahora
  exportan `build_service(session, tenant_id)`, la costura que consume el
  overlay de HTMX.
- mypy ya no fija `python_version`; chequea contra el intérprete sobre el que
  corre, que CI varía a lo largo de la matriz de soporte.

### Corregido
- `chunk_text` emitía una última astilla ya contenida en el chunk anterior cada
  vez que el texto no dividía parejo — una llamada de embedding desperdiciada y
  un duplicado en cada conjunto de resultados.

## [0.1.0] - 2026-08-27

Primer alpha. Kernel y plugins incorporados.

### Agregado
- `create_app()` con resolución, registro y orquestación del lifespan de los
  plugins
- Configuración tipada: `JFastSettings`, `JFastConfig`, `jfast.toml` + env
- `AppContext` con indirección `provide` / `require` entre plugins
- Contrato de plugin: `PluginMeta`, `PluginSettings`, hooks de ciclo de vida,
  `infra()`, `describe()`
- Registry: descubrimiento por entry-point, listas de permitidos/denegados,
  orden de dependencias, detección de ciclos, detección de proveedores
  duplicados
- Modelo de error RFC 7807 `application/problem+json`
- Endpoints de sistema `/health`, `/ready`, `/info`
- Plugins incorporados: `observability`, `metrics`, `sentry`, `database`,
  `cache`, `rag`
- `jfastframework.db`: `Base` declarativa con una convención fija de nombres de
  constraints, `TimestampMixin`, `TenantMixin`, `BaseRepository` genérico
- Generación de deploy: `docker-compose` y `Dockerfile` derivados de las
  declaraciones `infra()` del grafo de plugins
- CLI `jfast`: `new`, `describe`, `doctor`, `plugins list`, `deploy`
- Plantilla de módulo Jinja2, fixtures de `jfastframework.testing`
- Superficie de agentes: `AGENTS.md`, `.jfast/skills/` con cuatro skills
  iniciales

### Notas
- La multi-tenencia es una convención que impone `BaseRepository`, no una
  garantía. La seguridad a nivel de fila es fase 2. No la describas como
  aislamiento hasta entonces.
- El prototipo v0 se conserva bajo `legacy/` como referencia.
