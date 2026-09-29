# Modelos de lenguaje

Chat, salida estructurada, visión y embeddings sobre cualquier API compatible
con OpenAI, detrás de un tope de gasto que se sostiene entre workers.

```toml
[plugins]
enabled = ["observability", "cache", "llm"]

[plugin.llm]
chat_model = "gpt-5.4-mini"
embedding_model = "text-embedding-3-small"
budget_usd = 20.0           # por budget_period, todo el servicio
budget_period = "month"     # "month" | "day" | "total"
tenant_budget_usd = 2.0     # por tenant dentro del periodo; 0 = sin tope por tenant
```

```bash
pip install "jfastframework[llm]"
JFAST_LLM_API_KEY=sk-...
```

La key nunca va en `jfast.toml`. Sin ella el servicio arranca igual: el plugin
aparece degradado en `/ready` -- no caído, el resto del servicio no necesita un
modelo -- y cada llamada responde un 503 que nombra la variable.

Todo servicio que llama a un modelo termina escribiendo las mismas cuatro
cosas, casi siempre después de la primera factura sorpresa: un tope duro, un
registro de cuánto costó cada llamada, los precios en un solo lugar y
reintentos que respetan `Retry-After`. Este plugin son esas cuatro cosas.

---

## Llamarlo

```python
from jfastframework import get_context

llm = get_context(request.app).require("llm")      # jfastframework.llm.LLMClient

resultado = await llm.chat(
    [{"role": "system", "content": "Resumes facturas en una línea."},
     {"role": "user", "content": texto}],
    tenant_id=tenant,
    purpose="resumen-factura",     # aparece en la bitácora
    max_tokens=400,
)
resultado.text, resultado.usage.usd, resultado.usage.input_tokens
```

`jfastframework.llm` no depende de FastAPI: un worker de la cola, un script y un
test usan el mismo cliente.

### Salida estructurada

```python
schema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proveedor", "total", "fecha"],
    "properties": {
        "proveedor": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "fecha": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    },
}
resultado = await llm.chat(mensajes, schema=schema, tenant_id=tenant, purpose="leer-ticket")
resultado.data["total"]
```

El schema se manda en modo estricto, así que la respuesta es JSON válido con
esa forma, ya parseado en `resultado.data`. Valida igual los *valores* en
código: un schema estricto garantiza un campo de fecha, no una fecha
plausible.

### Imágenes

```python
from jfastframework.llm import image_part, text_part

resultado = await llm.chat(
    [{"role": "user", "content": [
        text_part("Lee este ticket."),
        image_part(foto_bytes, "image/jpeg", detail="high"),
    ]}],
    schema=schema, tenant_id=tenant, purpose="leer-ticket",
)
```

Un PDF escaneado también son imágenes: renderiza sus páginas a PNG (PyMuPDF lo
hace en tres líneas) y manda esas.

### Embeddings

```python
vectores = await llm.embed(textos, tenant_id=tenant, purpose="indexar")
```

En lotes (`embedding_batch_size`, default 96), en el orden de entrada, cada
lote dentro del presupuesto. Con `[plugin.rag] embedder = "llm"` el
[plugin `rag`](rag.md) usa esto, así que indexar documentos gasta del mismo
presupuesto que platicar sobre ellos.

---

## El presupuesto

**Se revisa antes de mandar nada.** Una llamada reserva su peor caso -- el
prompt que está por mandar más `max_tokens` de salida -- contra el tope, de
forma atómica, y se ajusta al costo real cuando llega la respuesta. Una llamada
que falla libera su reserva.

Esa es la diferencia con "revisar el total y luego llamar": veinte requests
concurrentes ven cada uno "bajo el presupuesto" y se pasan juntos. Con una
reserva, el vigésimo primero se rechaza antes de salir.

```
BudgetExceededError: The AI spending cap for the service's month budget ($20.00)
would be exceeded. Nothing was sent.
```

Es un 503. Lo que el servicio ya guardó -- documentos, conversaciones, datos
extraídos -- sigue funcionando; solo se detienen las llamadas nuevas al modelo.

| Setting | Default | |
| --- | --- | --- |
| `budget_usd` | `10.0` | todo el servicio, por periodo; `0` lo desactiva (queda en el log) |
| `tenant_budget_usd` | `0` | cada tenant, dentro del mismo periodo |
| `budget_period` | `month` | `month`, `day` o `total`, en UTC |

`max_tokens` es parte de la reserva, así que pedir 16,000 tokens que no vas a
usar aparta presupuesto que no gastas hasta que la respuesta lo ajusta. Pide lo
que la tarea necesita.

### Dónde se cuenta el dinero

En Redis cuando el plugin `cache` está activo: el tope se sostiene entre
workers y réplicas. Sin `cache` el plugin cuenta en memoria y avisa al
arrancar -- cada worker tiene entonces su propio presupuesto, que está bien en
una laptop y mal en cualquier otro lado.

```python
await llm.spend(tenant)
# {"period": "month", "spent_usd": 3.41, "budget_usd": 20.0, "remaining_usd": 16.59,
#  "tenant_spent_usd": 0.12, "tenant_budget_usd": 2.0,
#  "recent": [{"purpose": "leer-ticket", "model": "gpt-5.4-mini",
#              "input_tokens": 1830, "output_tokens": 212, "usd": 0.0023, "ms": 4210, ...}]}
```

La bitácora registra propósito, modelo, tokens, costo, latencia y tenant.
**Nunca el prompt ni la respuesta**: los prompts llevan documentos de clientes,
y un log es el lugar menos protegido de casi cualquier sistema. Los errores
siguen la misma regla -- un `LLMError` lleva el mensaje del proveedor, nunca el
cuerpo del request.

### Precios

`jfastframework.llm.DEFAULT_PRICES` trae USD por millón de tokens para los
modelos comunes de OpenAI. Compáralos con la página de precios del proveedor;
agrega o reemplaza los tuyos:

```toml
[plugin.llm.prices]
"gpt-5.4-mini" = [0.75, 4.50]
"mi-finetune" = [1.20, 4.80]
```

Un modelo sin precio se cobra como caro (`$10 / $30` por millón) y se avisa una
vez en el log. Así un typo en el nombre del modelo agota el presupuesto pronto
en vez de gastar sin freno. Los snapshots con fecha
(`gpt-4.1-mini-2025-04-14`) cuestan lo que cuesta su familia.

---

## Otros proveedores

Cualquiera que hable `/chat/completions` y `/embeddings`:

```toml
[plugin.llm]
base_url = "http://localhost:11434/v1"   # Ollama
chat_model = "llama3.1"

[plugin.llm.prices]
"llama3.1" = [0, 0]                      # local: gratis, pero igual queda en la bitácora
```

Azure OpenAI, vLLM y LiteLLM funcionan igual. Lo que el proveedor no soporte
-- los schemas JSON estrictos son lo típico -- falla con el error del propio
proveedor.

## Reintentos

408, 409, 429 y 5xx se reintentan hasta `max_retries` veces (default 2),
esperando lo que diga `Retry-After` (máximo 20 s) o con backoff. Una llamada
reintentada sigue siendo una sola reserva. Todo lo demás falla de inmediato: un
400 no se va a volver 200.

## Probar sin red

```python
import httpx
from jfastframework.llm import LLMClient

def proveedor(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })

llm = LLMClient(api_key="test", transport=httpx.MockTransport(proveedor))
```

`tests/test_llm.py` lo hace para cada caso de presupuesto y reintento.

## Lo que no está aquí

- **Streaming.** Una interfaz de chat que muestre los tokens conforme llegan lo
  necesita; el ajuste del presupuesto tiene que pasar al final del stream.
  Planeado.
- **Ayudas para tool calling.** Pasa `tools` con `extra={...}` y maneja el ciclo
  tú.
- **Formatos que no son de OpenAI.** La Messages API de Anthropic y Gemini
  necesitan su propio adaptador, o un gateway como LiteLLM enfrente.

## Ver también

- [RAG y búsqueda vectorial](rag.md)
- [Multi-tenancy](multitenancy.md) -- el tenant al que se cobra una llamada
