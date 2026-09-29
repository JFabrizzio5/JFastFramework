# Language models

Chat, structured output, vision and embeddings over any OpenAI-compatible API,
behind a spending cap that holds across workers.

```toml
[plugins]
enabled = ["observability", "cache", "llm"]

[plugin.llm]
chat_model = "gpt-5.4-mini"
embedding_model = "text-embedding-3-small"
budget_usd = 20.0           # per budget_period, the whole service
budget_period = "month"     # "month" | "day" | "total"
tenant_budget_usd = 2.0     # per tenant within the period; 0 = no per-tenant cap
```

```bash
pip install "jfastframework[llm]"
JFAST_LLM_API_KEY=sk-...
```

The key never goes in `jfast.toml`. Without it the service still starts: the
plugin shows as degraded in `/ready` -- not failed, the rest of the service does
not need a model -- and every call raises a 503 naming the variable.

Every service that calls a model ends up writing the same four things, usually
after the first surprise invoice: a hard budget, a record of what each call
cost, prices in one place, and retries that respect `Retry-After`. This plugin
is those four things.

---

## Calling it

```python
from jfastframework import get_context

llm = get_context(request.app).require("llm")      # jfastframework.llm.LLMClient

result = await llm.chat(
    [{"role": "system", "content": "You summarise invoices in one line."},
     {"role": "user", "content": text}],
    tenant_id=tenant,
    purpose="invoice-summary",     # shows up in the ledger
    max_tokens=400,
)
result.text, result.usage.usd, result.usage.input_tokens
```

`jfastframework.llm` has no FastAPI dependency: a queue worker, a script and a
test use the same client.

### Structured output

```python
schema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["supplier", "total", "date"],
    "properties": {
        "supplier": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    },
}
result = await llm.chat(messages, schema=schema, tenant_id=tenant, purpose="read-receipt")
result.data["total"]
```

The schema is sent strict, so the answer is valid JSON of that shape, parsed
into `result.data`. Validate the *values* in code anyway: a strict schema
guarantees a date field, not a plausible date.

### Images

```python
from jfastframework.llm import image_part, text_part

result = await llm.chat(
    [{"role": "user", "content": [
        text_part("Read this receipt."),
        image_part(photo_bytes, "image/jpeg", detail="high"),
    ]}],
    schema=schema, tenant_id=tenant, purpose="read-receipt",
)
```

A scanned PDF is images too: render its pages to PNG (PyMuPDF does it in three
lines) and send those.

### Embeddings

```python
vectors = await llm.embed(texts, tenant_id=tenant, purpose="index")
```

Batched (`embedding_batch_size`, default 96), returned in input order, each
batch inside the budget. With `[plugin.rag] embedder = "llm"` the
[`rag` plugin](rag.md) uses this, so indexing documents spends from the same
budget as talking about them.

---

## The budget

**Checked before anything is sent.** A call reserves its worst case -- the
prompt it is about to send plus `max_tokens` of output -- against the cap,
atomically, and settles to the real cost when the answer arrives. A failed call
releases its reservation.

That is the difference from "check the total, then call": twenty concurrent
requests each see "under budget" and overshoot together. With a reservation
the twenty-first is refused before it leaves.

```
BudgetExceededError: The AI spending cap for the service's month budget ($20.00)
would be exceeded. Nothing was sent.
```

It is a 503. What the service already stored -- documents, conversations,
extracted data -- keeps working; only new model calls stop.

| Setting | Default | |
| --- | --- | --- |
| `budget_usd` | `10.0` | the whole service, per period; `0` disables it (logged) |
| `tenant_budget_usd` | `0` | each tenant, within the same period |
| `budget_period` | `month` | `month`, `day` or `total`, in UTC |

`max_tokens` is part of the reservation, so asking for 16,000 tokens you will
not use holds budget you do not spend until the answer settles it. Ask for what
the task needs.

### Where the money is counted

In Redis when the `cache` plugin is enabled: the cap holds across workers and
replicas. Without `cache` the plugin counts in memory and warns at startup --
each worker then has its own budget, which is fine on a laptop and wrong
anywhere else.

```python
await llm.spend(tenant)
# {"period": "month", "spent_usd": 3.41, "budget_usd": 20.0, "remaining_usd": 16.59,
#  "tenant_spent_usd": 0.12, "tenant_budget_usd": 2.0,
#  "recent": [{"purpose": "read-receipt", "model": "gpt-5.4-mini",
#              "input_tokens": 1830, "output_tokens": 212, "usd": 0.0023, "ms": 4210, ...}]}
```

The ledger records purpose, model, tokens, cost, latency and tenant. **Never
the prompt or the answer**: prompts carry customers' documents, and a log is
the least protected place in most systems. Errors follow the same rule -- an
`LLMError` carries the provider's message, never the request body.

### Prices

`jfastframework.llm.DEFAULT_PRICES` lists USD per million tokens for the common
OpenAI models. Check them against the provider's pricing page; add or override
yours:

```toml
[plugin.llm.prices]
"gpt-5.4-mini" = [0.75, 4.50]
"my-finetune" = [1.20, 4.80]
```

A model with no price is charged as expensive (`$10 / $30` per million) and
logged once. A typo in a model name then exhausts the budget early instead of
spending freely. Dated snapshots (`gpt-4.1-mini-2025-04-14`) cost what their
family costs.

---

## Other providers

Anything that speaks `/chat/completions` and `/embeddings`:

```toml
[plugin.llm]
base_url = "http://localhost:11434/v1"   # Ollama
chat_model = "llama3.1"

[plugin.llm.prices]
"llama3.1" = [0, 0]                      # local: free, but still logged
```

Azure OpenAI, vLLM and LiteLLM work the same way. Features the provider lacks
-- strict JSON schemas are the usual one -- fail with the provider's own error.

## Retries

408, 409, 429 and 5xx are retried up to `max_retries` (default 2), waiting what
`Retry-After` says (at most 20 s) or backing off. A retried call is still one
reservation. Anything else fails at once: a 400 is not going to become a 200.

## Testing without the network

```python
import httpx
from jfastframework.llm import LLMClient

def provider(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })

llm = LLMClient(api_key="test", transport=httpx.MockTransport(provider))
```

`tests/test_llm.py` does this for every budget and retry case.

## What is not here

- **Streaming.** A chat UI that shows tokens as they arrive needs it; the
  budget settlement has to move to the end of the stream. Planned.
- **Tool calling helpers.** Pass `tools` through `extra={...}` and handle the
  loop yourself.
- **Non-OpenAI wire formats.** Anthropic's Messages API and Gemini need their
  own adapter, or a gateway like LiteLLM in front.

## See also

- [RAG and vector search](rag.md)
- [Multi-tenancy](multitenancy.md) -- the tenant a call is charged to
