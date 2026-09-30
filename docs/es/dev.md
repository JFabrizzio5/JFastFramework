# El ciclo local

```bash
jfast dev
```

Levanta los contenedores y espera a que estén sanos, aplica las migraciones, y
recién entonces arranca la API, el worker de la cola y el frontend. Un comando
para las cosas que uno hace cada mañana, en el orden que hace que las fallas aparezcan donde
corresponde.

---

## Qué hace, en orden

| Etapa | Qué corre | Si no puede |
| --- | --- | --- |
| 1. Infraestructura | `docker compose up -d <db> <cache>`, y espera health | dice por qué, y para |
| 2. Migraciones | `alembic upgrade head` | **para** |
| 3. API | `uvicorn main:app --reload --no-proxy-headers` | — |
| 4. Worker | `jfast worker`, cuando el plugin `queue` está activo | dice por qué, y sigue |
| 5. Frontend | `npm run dev` en el proyecto del frontend | dice por qué, y sigue |

El worker está porque la cola viene activada en todo servicio generado, y una
cola que nadie consume son jobs acumulándose en `jfast_jobs` -- y suscriptores
de eventos que nunca corren -- mientras la API responde 201. Levanta el mismo
`main:app`, así que corre los mismos `@task` y `@subscribe` de los módulos.
**No** recarga: reinicia `jfast dev` después de cambiar una task o un
suscriptor.

> `--no-proxy-headers` no es adorno. uvicorn trae su propio resolutor de
> cabeceras forwarded **encendido**, confiando en `127.0.0.1` -- que es
> justo lo que bindea `serve` -- y reescribe la dirección del cliente antes
> de que corra cualquier middleware, así que `trusted_proxies` nunca llega a
> decidir. `jfast dev` y `jfast serve` la pasan por ti.


Cada etapa se puede saltar, y cada salto se anuncia:

```bash
jfast dev --no-infra      # the containers are already up
jfast dev --no-migrate    # you are mid-migration and know it
jfast dev --no-web        # backend only
jfast dev --no-worker     # no queue worker (run `jfast worker` yourself)
jfast dev --port 9000     # override the port in jfast.toml
```

Sin Docker en la máquina, sin compose, sin `alembic.ini`, sin frontend: cada
caso degrada a una línea impresa y el resto igual corre. Un comando de
desarrollo que se niega a arrancar porque falta la mitad opcional es un comando
que la gente deja de usar.

---

## Por qué una migración fallida detiene todo

Es el único hard stop, y a propósito.

Un servidor arrancado contra un esquema atrasado no falla al arrancar. Falla
después, en una petición que no tiene nada que ver con la columna que falta, y
con un error que nombra una tabla en vez de la migración que nadie corrió. La
media hora que eso cuesta vale más que la comodidad de arrancar igual.

```
  ✗ alembic upgrade head failed:
    (psycopg.errors.UndefinedColumn) column "tenant_id" does not exist
```

Arréglalo, o pasa `--no-migrate` y hazte cargo.

---

## La traducción del `.env`

Esta es la parte que vale entender, porque es invisible cuando funciona.

El `.env` generado está escrito para **compose**, donde los servicios se
direccionan por nombre y compose interpola la contraseña:

```bash
JFAST_DB_DSN=postgresql+asyncpg://app:${SHOP_DATABASE_PASSWORD}@shop-database:5432/app
```

Las dos mitades son falsas para un proceso corriendo en tu máquina.
`shop-database` resuelve en la red de compose y en ningún otro lado, y nadie
interpola `${SHOP_DATABASE_PASSWORD}` para un `uvicorn` pelado — el DSN llegaría
a asyncpg con las llaves puestas.

Así que `jfast dev` reescribe las dos antes de correr nada:

```bash
JFAST_DB_DSN=postgresql+asyncpg://app:s3cr3t@localhost:9431/app
```

El puerto del host sale de lo que compose realmente publica, leído del archivo
de compose. **El mismo entorno va a Alembic y al servidor**, porque Alembic
corre primero — poner la traducción solo en el servidor hace que la migración
falle con un error de DNS nombrando un host que nunca iba a resolver ahí.

Lo que no puede resolver lo deja tal cual. Una suposición equivocada sería más
difícil de depurar que el valor original. Una variable que ya está puesta en tu
shell también se respeta, igual que pydantic-settings la prefiere sobre el
`.env`.

La traducción no es solo de `jfast dev`. `jfast serve`, `jfast worker` y
`jfast migration check`/`plan` la aplican cuando encuentran el archivo de compose de la misma forma (en el
directorio del servicio o un nivel arriba), y `jfast exec -- <comando>` corre
cualquier otra cosa con ella -- `jfast exec -- alembic revision --autogenerate
-m "add invoices"`, `jfast exec -- pytest`. Dentro de un contenedor no se
traduce nada: ahí los nombres de compose resuelven y `localhost` es el propio
contenedor. La imagen de producción de todos modos no tiene un compose que
encontrar (`.dockerignore` lo excluye, y el del workspace queda fuera del
contexto de build).

---

## Cómo se detiene

Ctrl-C detiene todo lo que arrancó. `SIGTERM` también — de `timeout`, de un
supervisor, de cerrar la terminal.

Lo segundo necesitó su propio handler, y vale saberlo si alguna vez tocas esto.
Los hijos se ponen a propósito en **su propio grupo de procesos**, para que un
Ctrl-C en la terminal no les llegue directo y el padre pueda bajarlos en orden.
Pero la disposición por defecto de `SIGTERM` mata al intérprete de una — sin
`finally`, sin limpieza — y los hijos, al estar en otro grupo, habrían
sobrevivido como huérfanos aguantando los puertos. El siguiente `jfast dev`
falla entonces con `address already in use` y una cacería confusa.

`scripts/smoke_dev.sh` lo verifica: arranca, manda `SIGTERM`, cuenta lo que
quedó.

---

## `dev` contra `serve`

| | `jfast serve` | `jfast dev` |
| --- | --- | --- |
| Backend | sí | sí |
| Contenedores | no | los levanta |
| Migraciones | no | las aplica |
| Worker de la cola | no (`jfast worker`) | lo arranca |
| Frontend | no | lo arranca |
| `.env` traducido para el host | sí | sí |

`serve` es la herramienta chica y se queda así: un proceso, sin efectos
secundarios, nada arrancado que después tengas que acordarte de bajar. Úsalo
cuando la base ya está corriendo y quieres un servidor y nada más.

---

## Todo en contenedores

```bash
(cd shop-web && npm install && npm run build)
docker compose up --build
```

Que es lo que corre en producción. El frontend es lo único que se construye en
el host: Caddy sirve `<frontend>/dist`, donde escribe `npm run build`, y no
corre ningún contenedor de Node. El archivo de compose que escribe `jfast start` está completo — el `.env` del
workspace con las contraseñas generadas y el `.env` de cada servicio se
escriben junto a él, así que esto funciona en un clon limpio sin pasos extra.

Usa `jfast dev` cuando quieres reload y un debugger. Usa compose cuando
quieres saber que funciona como se va a desplegar.
