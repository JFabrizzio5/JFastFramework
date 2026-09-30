# Llamar a otros servicios

`jfastframework.http` es el cliente con el que un servicio llama a sus
hermanos. Existe para que cada servicio no escriba el suyo, y para que las
cuatro cosas que todo cliente hecho a mano resuelve mal se decidan una sola
vez: cuánto esperar, qué reintentar, cuándo dejar de llamar a un upstream que
está caído, y cuántas llamadas dejar que se apilen detrás de uno lento.

```toml
[plugins]
enabled = ["observability", "http"]

[plugin.http.upstreams.billing]
base_url = "http://billing:8010"
read_timeout = 5.0
retries = 2

[plugin.http.upstreams.catalog]
base_url = "http://catalog:8020"
forward_authorization = true
```

```python
@router.get("/orders/{order_id}")
async def show(order_id: int, request: Request) -> dict:
    billing = request.app.state.jfast.require("http").client("billing")
    response = await billing.get(f"/invoices/{order_id}")
    if response.status_code == 404:
        raise NotFoundError("no invoice for this order")
    response.raise_for_status()
    return response.json()
```

Se instala con `pip install "jfastframework[http]"`. Por debajo es httpx, y
`response` es un `httpx.Response`.

---

## Un cliente por upstream por proceso

`require("http")` devuelve una fábrica que guarda un cliente por cada upstream
configurado, durante toda la vida del proceso. Toma el cliente de ahí en vez de
construir uno por request: el circuit breaker, el bulkhead y el presupuesto de
reintentos son estado, y solo protegen algo si todas las llamadas a ese
upstream pasan por el mismo.

Fuera de un servicio -- un script, una prueba -- construye uno directamente:

```python
from jfastframework.http import ServiceClient, Timeouts, Upstream

async with ServiceClient(Upstream(name="billing", base_url="http://localhost:8010",
                                  timeouts=Timeouts(total=10))) as billing:
    response = await billing.get("/invoices/7")
```

## Los plazos son obligatorios

| Ajuste | Default | Acota |
| --- | --- | --- |
| `connect_timeout` | 2 s | abrir la conexión TCP (y TLS) |
| `read_timeout` | 10 s | esperar cada trozo de la respuesta |
| `write_timeout` | 10 s | enviar cada trozo del request |
| `pool_timeout` | 2 s | esperar una conexión libre del pool |
| `total_timeout` | 30 s | la llamada completa: cada intento y cada espera entre ellos |

Ninguno se puede apagar -- cero, negativo y ausente se rechazan al arrancar. Un
upstream que acepta la conexión y nunca contesta retendría, si no, al request
que llama, a su worker y a su conexión de base de datos todo el tiempo que
quisiera. Cuando pasa `total_timeout`, la llamada lanza `UpstreamTimeoutError`.

## Qué se reintenta

Un intento fallido se repite solo cuando se cumplen todas estas condiciones:

1. **Repetir el request es seguro.** GET, HEAD, PUT, DELETE y OPTIONS son
   idempotentes. POST y PATCH no, salvo que el request lleve un
   `Idempotency-Key` -- pasa `idempotency_key=` y el cliente pone el header,
   que además es lo que permite a un upstream con el
   [plugin `idempotency`](transactions.md#un-post-reintentado-idempotency-keys)
   contestar el reintento con la primera respuesta en vez de actuar dos veces.
   `retry=False` prohíbe un reintento; `retry=True` responde por una llamada
   que las reglas no reintentarían.
2. **La falla es de las que otro intento puede arreglar:** un error de
   conexión, un timeout, una conexión cortada, o un 429, 502, 503 o 504. Un 500
   no se reintenta: es el upstream contestando con un bug, y el mismo request
   obtiene el mismo bug. Un 4xx es error de quien llama.
3. **Hay presupuesto y tiempo.** Ver abajo.

Entre intentos el cliente espera un backoff exponencial con **full jitter**: un
valor uniforme entre cero y `backoff_base × 2^(n-1)`, con tope en
`backoff_max`. Sin jitter, todos los clientes que fallaron juntos reintentan
juntos, y el upstream recibe el mismo pico en cada ronda. Cuando el upstream
manda `Retry-After` (segundos o una fecha HTTP), el cliente espera exactamente
eso; si pide más que `max_retry_after` (30 s), la respuesta regresa a quien
llama en vez de esperarla.

No se empieza un reintento que el plazo total no alcanza a terminar: se
devuelve la última respuesta.

**Qué regresa.** Una respuesta que el upstream mandó se devuelve tal cual
cuando se deja de reintentar -- un 404, un 500, el último 503 -- y quien llama
decide qué significa. Solo una llamada que no obtuvo ninguna respuesta lanza
una excepción.

## El presupuesto de reintentos

Tres intentos por llamada significan que cuando un upstream se cae, cada
cliente triplica su tráfico justo cuando menos lo aguanta. El presupuesto
limita los reintentos a `retry_budget_ratio` (0.2) de los requests hechos en
los últimos diez segundos, más `retry_budget_min_per_second` (1) para que un
cliente con poco tráfico pueda reintentar. Pasado eso, la falla se devuelve en
vez de reintentarse. Con una proporción de 0.1, veinte llamadas a un upstream
que solo contesta 503 hacen como máximo veintitrés requests en la suite de
pruebas; sin presupuesto serían sesenta.

## El circuit breaker

Cada upstream tiene un breaker, por proceso:

- **Cerrado** -- las llamadas pasan. Se abre tras `breaker_failures` (5) fallas
  seguidas, o cuando falló `breaker_failure_rate` (la mitad) de las llamadas de
  los últimos `breaker_window` (30 s), siempre que hubiera al menos
  `breaker_minimum_calls` (20).
- **Abierto** -- las llamadas lanzan `CircuitOpenError` de inmediato y **no se
  envía nada**. El upstream tiene `breaker_cool_down` (15 s) para recuperarse
  en vez de una cola de reintentos.
- **Medio abierto** -- pasado el enfriamiento, una llamada pasa como sonda
  mientras las demás siguen fallando rápido. Si sale bien el breaker se cierra;
  si falla, se abre por otro enfriamiento completo.

Para el breaker, una falla es lo que dice que el upstream no está: errores de
transporte, timeouts, 502, 503 y 504. Un 500 prueba que el upstream está
arriba; un 429 es el upstream pidiéndonos calma, y un breaker abierto
convertiría eso en una caída.

Por proceso significa que cada worker se abre con su propia evidencia: un
upstream que se recupera ve hasta una sonda por worker. Un breaker compartido
entre procesos necesitaría un almacén compartido en el camino de cada llamada.

## El bulkhead

Como máximo `max_concurrent` (50) llamadas a un upstream están en vuelo desde
un proceso; una llamada que no consigue lugar en `bulkhead_wait` (0.5 s) lanza
`BulkheadFullError`. Sin esto, un upstream lento arrastra a todos los workers:
requests que nunca lo tocan hacen fila detrás de los que lo esperan. Una
llamada rechazada por el bulkhead no cuenta contra el breaker: el upstream no
la falló.

## Errores

Todo error que lanza una llamada es un `ServiceUnavailableError` 503, así que
una ruta que lo deja escapar contesta `application/problem+json` nombrando al
upstream, nunca un 500 con traceback:

| Error | Se lanza cuando |
| --- | --- |
| `CircuitOpenError` | el breaker está abierto o su sonda está en vuelo; lleva `retry_after` |
| `BulkheadFullError` | los lugares del upstream están llenos |
| `UpstreamTimeoutError` | pasó el plazo total |
| `UpstreamUnreachableError` | todos los intentos fallaron en el transporte |

Los cuatro heredan de `UpstreamError`, que lleva `upstream`. Atrápalo donde
quien llama tenga algo mejor que decir: un valor en caché, una página
degradada.

## Qué viaja con la llamada

- **`X-Request-ID`** del request que se está atendiendo, para que un solo grep
  siga un request entre servicios. Dentro de un job de la cola es el del job,
  que el worker restaura desde el request que lo encoló; en un script no se
  inventa ninguno.
- **`traceparent` y `tracestate` de W3C**, cuando el plugin `telemetry` está
  exportando. Cada llamada es un span de cliente (`GET billing`, con el
  upstream, el status y el número de reintentos), y el header lleva ese span,
  así que el span de servidor del upstream es su hijo y una sola traza cubre
  los dos servicios. Los reintentos comparten el span. La ruta no se registra
  -- puede traer un id por llamada; el span del upstream nombra la plantilla de
  la ruta. Con telemetry apagado no se agrega nada. El gateway hace lo mismo
  con lo que proxea. Ver [telemetry.md](telemetry.md).
- **El bearer token de quien llama**, solo a upstreams con
  `forward_authorization = true`. Un token se emite para una audiencia;
  mandarlo a un servicio fuera de ella le entrega la identidad de quien llama,
  y por eso está apagado por defecto. Solo se reenvían credenciales `Bearer`,
  nunca Basic, y las redirecciones no se siguen, así que un `302` no puede
  llevarse el token a otro host.
- **`headers`** de la configuración del upstream, y después los `headers=` de
  la propia llamada, que ganan sobre todo lo anterior.

## Configuración

Todas las llaves de `[plugin.http.upstreams.<name>]`:

| Llave | Default |
| --- | --- |
| `base_url` | obligatoria |
| `connect_timeout`, `read_timeout`, `write_timeout`, `pool_timeout` | 2, 10, 10, 2 s |
| `total_timeout` | 30 s |
| `retries` | 2 (después del primer intento) |
| `backoff_base`, `backoff_max` | 0.1 s, 2 s |
| `max_retry_after` | 30 s |
| `retry_budget_ratio`, `retry_budget_min_per_second` | 0.2, 1 |
| `breaker_failures`, `breaker_failure_rate`, `breaker_minimum_calls` | 5, 0.5, 20 |
| `breaker_window`, `breaker_cool_down` | 30 s, 15 s |
| `max_concurrent`, `bulkhead_wait` | 50, 0.5 s |
| `forward_authorization` | `false` |
| `headers` | `{}` |

Una llave mal escrita se rechaza al arrancar en vez de ignorarse. La URL base
puede venir del entorno en vez del archivo --
`JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL=http://billing.internal:8010` -- y,
como en todos los plugins, un valor en `jfast.toml` gana sobre el entorno, así
que deja `base_url` fuera del archivo para un upstream cuya dirección cambia
según el entorno. (Solo `[app] env` y `debug` van al revés; ver
[deploy](deploy.md#quien-gana-jfasttoml-o-el-entorno).)

## Salud

`/ready` lista el breaker de cada upstream dentro del check `http`. Un breaker
abierto o medio abierto deja la disponibilidad en `degraded`, nunca en
`unavailable`: este servicio sigue arriba, y sacarlo de rotación porque una
dependencia está caída convierte una caída en dos.

## Qué está verificado y qué no

**Probado sin red** (`tests/test_http_client.py`, contra `httpx.MockTransport`,
un reloj que la prueba mueve y esperas registradas): qué métodos y qué fallas
se reintentan y cuáles no, la idempotency key, los límites del full jitter,
`Retry-After` en sus dos formas y su tope, el plazo total, el presupuesto
durante una caída, cada transición del breaker incluida la sonda única, una
sonda cancelada y la tasa de fallas, el bulkhead, la propagación de headers, la
configuración del plugin y su reporte en `/ready`, y que el plugin se importa
sin httpx.

**Sin probar:** un upstream real sobre un socket real, TLS, y el
comportamiento del pool de conexiones bajo carga. **Sin soporte:** streaming.
Los cuerpos de request son bytes, texto, JSON o datos de formulario, y las
respuestas se leen completas antes de devolverse.
