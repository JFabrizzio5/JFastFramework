# Arquitectura

Cada decisión aquí tiene un costo. Este documento nombra las dos caras.

---

## 1. Una librería versionada, no un generador de código

**Decisión.** El runtime vive en `jfastframework` y se importa. El generador
emite solo el código que es genuinamente de cada servicio.

**Por qué.** La generación anterior escribía su runtime como literales de
string dentro de funciones generadoras. Cada servicio generado era un fork
congelado en el momento de la generación; un arreglo en el cliente HTTP
compartido significaba parchear N repositorios a mano, y las plantillas que
viven dentro de literales de string no pasan por un linter, no se ven en un
diff y no se pueden testear.

**Costo.** Los servicios ya no pueden editar la capa compartida libremente. Eso
era una funcionalidad accidental: cuando algo no encajaba, los equipos
parcheaban local. Si los puntos de extensión no son suficientemente buenos, los
forks vuelven, solo que ahora escondidos. Ese es el riesgo del que el contrato
de plugins se tiene que ganar la salida.

---

## 2. Todo lo que está arriba del kernel es un plugin

**Decisión.** Monitoreo, base de datos, cache, RAG y error tracking son
plugins. El kernel los resuelve, los ordena, los arranca y los detiene. No sabe
nada más.

**Por qué.** "Monitoreo por defecto, removible a pedido" solo es cierto si el
monitoreo no es un caso especial. Los built-in se registran por el mismo grupo
de entry points `jfastframework.plugins` que usan los terceros, así que un
plugin de terceros puede reemplazar a uno built-in reclamando la misma provider
key.

**Costo.** Indirección. Leer `create_app` no te dice qué hace la app: para eso
corres `jfast describe`. Ese es el trade que el diseño acepta, y la razón por
la que las herramientas de introspección son parte del kernel y no un add-on.

---

## 3. Los plugins se comunican por keys de string

**Decisión.** `ctx.provide("db.engine", engine)` / `ctx.require("db.engine")`.
Los plugins nunca se importan entre sí.

**Por qué.** Los imports directos harían rígido el grafo: cambiar el cache de
Redis por uno en memoria tocaría a todos los consumidores.

**Costo.** No hay chequeo estático de tipos cruzando la frontera. Mitigaciones:
`require(key, expected=Type)` chequea en runtime, `meta.provides` se declara
por adelantado, los reclamos duplicados fallan en build time, y un provider que
falta levanta un error que nombra lo que *sí* está disponible.

---

## 4. Los plugins declaran su infraestructura

**Decisión.** `Plugin.infra()` devuelve los contenedores que el plugin
necesita. `jfast deploy compose` los compone en un archivo de compose.

**Por qué.** Los archivos de compose mantenidos a mano se desvían de la app. Si
apagas el plugin de cache, el contenedor de Redis debería desaparecer, no
quedarse seis meses hasta que alguien lo note.

**Costo.** Ahora los plugins saben algo del deploy, que estrictamente no es
asunto suyo. La alternativa —un manifiesto de infra aparte— se desvía, que es
exactamente la falla que este diseño quiere eliminar. Aceptado a propósito.

**Convención.** Un servicio es dueño de un bloque de diez puertos que empieza
en su puerto base; cada plugin declara un offset dentro del bloque. Heredado
del esquema de CometaX, menos el IAM central como dependencia dura.

---

## 5. Dos endpoints de health, no uno

**Decisión.** `/health` es liveness y nunca sondea dependencias. `/ready` agrega
el health check de cada plugin y devuelve 503 cuando falla uno **crítico**.

**Por qué.** Mezclarlos causa reinicios en cascada: la base de datos parpadea,
la probe de liveness falla, el orquestador mata pods sanos, y la estampida
termina de voltear a la base de datos.

**Detalle.** `HealthReport.critical=False` significa degradado, no caído. Un
cache frío no debería sacar al servicio de rotación.

---

## 6. RFC 7807 para todos los errores

**Decisión.** Todas las fallas se serializan a `application/problem+json`.

**Por qué.** Una sola forma de error en todos los servicios. Los clientes y los
servicios hermanos parsean una vez.

**Detalle.** El handler catch-all devuelve el mensaje de la excepción solo
cuando `debug` está prendido. Filtrar stack traces y rutas internas a clientes
de producción es un hallazgo de information disclosure, y el default tiene que
ser el seguro.

---

## 7. Convención de nombres de constraints fijada en `Base`

**Decisión.** `MetaData(naming_convention=...)` en `jfastframework.db.base`.

**Por qué.** Sin ella, PostgreSQL inventa los nombres de los constraints y el
`--autogenerate` de Alembic produce diffs distintos en máquinas distintas. Con
decenas de servicios corriendo autogenerate, eso es un desorden de migraciones
permanente y de bajo grado.

**Costo.** Las bases de datos existentes con constraints auto-nombrados
necesitan una migración única para adoptarla. Hazlo antes de que la flota
crezca, no después.

---

## 8. Las plantillas son archivos, no strings

**Decisión.** Plantillas Jinja2 bajo `templates/`, renderizadas por
`Scaffolder`.

**Por qué.** Las plantillas que son archivos pasan por un linter, se ven en un
diff durante el review, y se testean renderizándolas.

**Detalle.** Cada árbol generado lleva un sello `.jfast-template` que registra
la versión del framework y el contexto de render. Ese sello es lo que le va a
permitir a un futuro `jfast upgrade` mostrar un diff en vez de una reescritura
(fase 3).

---

## 9. Precedencia de configuración

```
CLI overrides  >  environment  >  jfast.toml  >  field defaults
```

Los secretos van en el entorno como `SecretStr`, nunca en `jfast.toml`: ese
archivo se commitea. `SecretStr` además mantiene los DSN fuera de la salida de
`jfast describe` y del endpoint `/info`.

---

## 10. Las dependencias opcionales fallan tarde, no en el discovery

**Decisión.** Un plugin cuyo extra no está instalado se salta durante el
discovery y queda registrado en `discover.broken`. Solo levanta error si el
servicio realmente lo habilita, y ahí el error nombra la falla del import.

**Por qué.** Instalar `jfastframework` sin `[rag]` no debe romper
`jfast --help`. Pero habilitar `rag` sin `[rag]` debe fallar fuerte, con la
razón.

---

## Ciclo de vida de una request

```
request
  → RequestContextMiddleware      assign/propagate X-Request-ID, bind contextvars
  → PrometheusMiddleware          RED metrics, labelled by route template
  → route handler
      → DbSession                     sesión; confirma antes de la respuesta
      → Service                       domain rules
      → Repository                    data access, tenant-filtered
  → response                      X-Request-ID echoed back
  ← exception                     → problem+json, request_id attached
```

## Ciclo de vida del arranque

```
create_app()
  1. load jfast.toml + env            → JFastConfig
  2. discover plugins                 → entry points + dotted paths
  3. select and order                 → allow/deny, requires, cycles, conflicts
  4. instantiate                      → each plugin gets its [plugin.<name>] block
  5. install error handlers
  6. plugin.register(ctx)             → routers, middleware, providers. No I/O.
  7. mount system + app routers

lifespan startup
  8. plugin.startup(ctx)              → in order. Pools, connections, DDL.
lifespan shutdown
  9. plugin.shutdown(ctx)             → reverse order. One failure does not
                                         block the rest.
```
