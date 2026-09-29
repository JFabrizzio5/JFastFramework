# shared/, enums y channels

Dos features, una misma idea: **acoplamiento fácil de agregar, invisible una
vez agregado, y caro de sacar después de que una segunda persona copió el
patrón.** Por eso los dos se verifican en vez de acordarse.

---

## Dónde va un enum

```bash
jfast new enum InvoiceStatus --module invoice
jfast new enum Currency --shared
```

Si lo corres sin flag te pregunta, porque la ubicación *es* la decisión:

```
  Will more than one module use it?  yes puts it in shared/, no puts it in one module
```

No hace falta que aciertes el primer día. Arranca un enum en el módulo que lo
necesita, y el check te avisa el día en que un segundo lo quiere.

### La regla

**`shared/` es vocabulario, no comportamiento.** Un enum, un tipo o una función
pura que hablan dos módulos se muda acá, y el check nombra el archivo cuando un
módulo importa uno de otro:

```
modules/payment/service.py:41: cross-module: module 'payment' imports modules.invoice.enums, private to module 'invoice'
  (an enum or type two modules both speak is vocabulary: move it to shared/enums.py and import it from both)
```

Moverlo a `shared/` limpia el finding. El mensaje nombra el archivo, así que el
arreglo no necesita una discusión de diseño.

**Los datos y el comportamiento no vienen acá.** Cuando `payment` necesita
filas que son de `invoice`, la respuesta no es mover `InvoiceRepository` a
`shared/` — eso son dos módulos compartiendo una tabla, y el checker no lo
puede distinguir del acoplamiento que se quería quitar. Es una función en
`modules/invoice/public.py` que recibe la sesión de quien llama y un
`tenant_id` y devuelve DTOs, más `depends_on = ["invoice"]` bajo
`[modules.payment]` en `contracts.toml`. Para cualquier otra cosa de otro
módulo el mensaje dice exactamente eso:

```
modules/payment/service.py:12: cross-module: module 'payment' imports modules.invoice.service; import modules.invoice.public instead
  (only public.py is another module's API -- call a function in modules/invoice/public.py that returns DTOs (add one if it is missing), and add "invoice" to depends_on under [modules.payment] in contracts.toml)
```

Y el SQL crudo contra `invoices` desde dentro de `payment` se reporta como
`cross-module-sql`: es el mismo acoplamiento sin nada que lo vea.
[Servicios, módulos y layouts](modules.md#comunicacion-entre-modulos) tiene el
ejemplo completo y las razones.

**La dirección es de una sola vía**, y eso también se verifica:

```
shared/enums.py:43: shared-direction: shared/ imports modules.invoice
  (the direction is one-way: modules use shared/, never the reverse,
   or the graph becomes a circle)
```

Sin esa segunda regla `shared/` se vuelve el lugar donde termina todo, que es
el modo de falla de todo paquete `utils` jamás escrito.

### Consultas por una fachada, efectos por eventos

Las tres formas de cruzar la frontera de un módulo, y cuál usar:

| Necesitas… | Usa | No |
| --- | --- | --- |
| leer datos que son de otro módulo | una función en su `public.py`, que devuelve DTOs | su repositorio, su entidad, SQL contra sus tablas |
| reaccionar a algo que hizo otro módulo | un evento que publica por el outbox | una llamada de vuelta hacia él |
| hablar el mismo enum o tipo | `shared/` | una copia en cada módulo |

La segunda fila es donde entran los eventos y los channels. Un módulo que tiene
que enterarse de que un comprobante se categorizó no le pide al módulo de
comprobantes que lo llame; el de comprobantes publica `receipt.categorized` en
la misma transacción que categorizó — `outbox.publish(session, topic,
Event(...))`, ver [Colas y eventos](queues-and-events.md) — y quien le importe
se suscribe. El que publica nunca nombra a sus suscriptores, así que la
dependencia apunta en un solo sentido y el grafo de módulos queda sin ciclos. Un
[channel](#channels), más abajo, hace el mismo trabajo dentro de un proceso
cuando el evento no tiene que sobrevivir a una caída.

### Qué pertenece a shared/

| Pertenece | No pertenece |
| --- | --- |
| Enums que hablan dos módulos | Cualquier cosa que use un solo módulo |
| Value objects y tipos compartidos | Cualquier cosa que toque la base de datos |
| Funciones puras | Cualquier cosa que haga una llamada HTTP |

La línea de la base de datos es la que importa, y el `contracts.toml` generado
la hace cumplir prohibiendo `sqlalchemy` en `shared/`. **Dos módulos
compartiendo un repositorio son dos módulos compartiendo una tabla**, y así es
como un conjunto de servicios se convierte en un monolito distribuido con
latencia de más.

### Por qué `str, Enum`

Los dos templates lo usan, y la razón no es estética. Un `Enum` pelado
serializa como `Status.DRAFT` por algunos caminos y como `"DRAFT"` por otros;
heredar de `str` hace que un miembro sea un string en todos lados — en JSON, en
SQL y en una línea de log.

Y **el valor es el formato de cable**. Se guarda en una columna, se serializa
en una respuesta de API y lo lee un frontend. Renombrar un miembro es gratis;
cambiar su valor es una migración de datos.

Apaga todo esto si no estás de acuerdo — un solo switch para todas las reglas
de módulos, fachada y SQL incluidos:

```toml
[rules.placement]
enabled = false
```

---

## Channels

El patrón que esto reemplaza es un archivo de constantes string importado en
todos lados donde alguien publica:

```python
VINCULACION_COMPLETADA = "eventos:vinculacion_completada"
```

Eso funciona, y falla de tres formas concretas: nada verifica el payload, el
transporte queda soldado al call site, y nadie puede listar los canales que usa
un sistema.

```python
# channels.py
from jfastframework.channels import Channel

VINCULACION_COMPLETADA = Channel(
    "eventos:vinculacion_completada",
    description="A linkage finished; downstream balances may be stale.",
    required=("vinculacion_id", "rfc"),
)

LARAVEL_CHEQUES = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")
```

```python
await VINCULACION_COMPLETADA.publish({"vinculacion_id": 7, "rfc": "AAA010101AAA"})

@VINCULACION_COMPLETADA.on
async def recalculate(payload):
    ...
```

### El payload se valida donde se construye

```
ChannelError: Payload for 'eventos:vinculacion_completada' is missing rfc.
```

Esa falla pertenece al servicio que construyó el mensaje, no a un worker a tres
servicios de distancia que leyó una clave que ya no está.

`required` es un conjunto de nombres de claves y no un modelo, a propósito.
Estos payloads cruzan fronteras de lenguaje — Laravel publica en algunos de
ellos — así que el contrato que se puede hacer cumplir de los dos lados es
*estas claves están presentes*, no *esto es un modelo de pydantic*.

### Los backends son por canal

Mezclar es el caso normal, no un caso borde:

| Backend | Úsalo cuando | Ten en cuenta que |
| --- | --- | --- |
| `memory` | Por defecto. Dentro de un proceso. | La entrega es solo a este proceso — correcto para un monolito modular, incorrecto en el momento en que hay dos réplicas. |
| `redis` | Algo más, en otro lenguaje, publica o se suscribe. | No se retiene nada. Un suscriptor que no está conectado nunca ve el mensaje. |
| `kafka` | Un consumidor que estuvo caído tiene que ponerse al día. | Retenido y reproducible, y necesita el plugin `events`. |

El default no necesita infraestructura alguna, así que publicar un evento no es
una decisión que tengas que tomar el primer día. Mover un canal a Redis después
es una palabra clave en la declaración.

`redis` es para *esto cambió, refresca*. Para *haz este trabajo*, usa la queue
— reintenta, hace backoff y manda a dead-letter, y pub/sub no hace nada de eso.

### Listarlos

```bash
jfast describe --json | jq '.plugins[] | select(.name=="channels") | .channels'
```

Que es la tercera falla arreglada: los canales que habla un sistema están en un
archivo y un comando, en vez de repartidos por los módulos que resulte que
publican.
