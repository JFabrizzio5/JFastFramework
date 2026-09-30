"""A language-model client with a spending cap, for any OpenAI-compatible API.

Every service that calls a model grows the same four things, usually after the
first surprise invoice: a hard budget, a ledger of what each call cost, prices
kept in one place, and retries that respect ``Retry-After``. This is those four
things, once.

    from jfastframework import get_context

    llm = get_context(request.app).require("llm")
    result = await llm.chat(
        [{"role": "user", "content": "Summarise this invoice"}],
        tenant_id=tenant, purpose="invoice-summary",
    )
    result.text, result.usage.usd

**The budget is checked before the request leaves.** A call reserves its
worst case -- the prompt it is about to send plus ``max_tokens`` of output --
against the cap, atomically, and settles to the real cost when the answer
arrives. Twenty concurrent calls cannot each see "under budget" and overshoot
together, which is what a check-then-call does. A call that would cross the cap
fails with :class:`BudgetExceededError` (a 503) and nothing is sent.

**The ledger never holds content.** Purpose, model, tokens, cost, latency and
tenant; never the prompt or the answer. Prompts carry customers' documents.

**OpenAI-compatible, not OpenAI-only.** ``base_url`` points it at Azure OpenAI,
Ollama (``http://localhost:11434/v1``), vLLM, LiteLLM or anything else that
speaks ``/chat/completions`` and ``/embeddings``.

This module has no FastAPI dependency: a worker, a script or a test uses it
exactly as a route does. The ``llm`` plugin builds one from ``[plugin.llm]``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from jfastframework import tracing
from jfastframework.errors import ServiceUnavailableError

logger = logging.getLogger("jfast.llm")

#: USD per million tokens, (input, output). Check the provider's pricing page
#: before trusting these; override or extend them in ``[plugin.llm.prices]``.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}

#: What an unknown model is assumed to cost: high, so a typo in the model name
#: exhausts the budget early instead of spending freely.
UNKNOWN_PRICE = (10.0, 30.0)

# Three characters per token -- fewer than real text averages, on purpose: it
# sizes a reservation, and a reservation that errs high is the safe one.  It is
# settled to the real usage the moment the answer arrives.
_CHARS_PER_TOKEN = 3
_IMAGE_TOKENS = 1500
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


class LLMError(ServiceUnavailableError):
    """The provider refused or failed. The message is the provider's, never the prompt."""


class BudgetExceededError(ServiceUnavailableError):
    """The spending cap would be crossed. Raised before anything is sent."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    usd: float
    model: str
    ms: int


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: Usage
    #: The parsed JSON when a ``schema`` was given; None otherwise.
    data: Any = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class Ledger(Protocol):
    """Where spend is counted. Redis in production, memory in tests."""

    async def add(self, key: str, amount: float) -> float: ...

    async def get(self, key: str) -> float: ...

    async def record(self, entry: dict[str, Any]) -> None: ...

    async def recent(self, tenant_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class MemoryLedger:
    """Per-process ledger. Fine for tests and one-worker development only:
    with several workers each one has its own budget."""

    def __init__(self) -> None:
        self._totals: dict[str, float] = {}
        self._entries: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def add(self, key: str, amount: float) -> float:
        async with self._lock:
            self._totals[key] = self._totals.get(key, 0.0) + amount
            return self._totals[key]

    async def get(self, key: str) -> float:
        return self._totals.get(key, 0.0)

    async def record(self, entry: dict[str, Any]) -> None:
        self._entries.insert(0, entry)
        del self._entries[500:]

    async def recent(self, tenant_id: str | None, limit: int) -> list[dict[str, Any]]:
        rows = [e for e in self._entries if tenant_id is None or e.get("tenant_id") == tenant_id]
        return rows[:limit]


class RedisLedger:
    """Shared across workers and replicas: the cap holds for the whole service."""

    def __init__(self, redis: Any, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix

    async def add(self, key: str, amount: float) -> float:
        return float(await self._redis.incrbyfloat(self._prefix + key, amount))

    async def get(self, key: str) -> float:
        return float(await self._redis.get(self._prefix + key) or 0)

    async def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry)
        pipe = self._redis.pipeline()
        for name in ("log", f"log:{entry.get('tenant_id') or '-'}"):
            pipe.lpush(self._prefix + name, line)
            pipe.ltrim(self._prefix + name, 0, 499)
        await pipe.execute()

    async def recent(self, tenant_id: str | None, limit: int) -> list[dict[str, Any]]:
        name = "log" if tenant_id is None else f"log:{tenant_id}"
        return [json.loads(x) for x in await self._redis.lrange(self._prefix + name, 0, limit - 1)]


def image_part(data: bytes, mime: str, *, detail: str = "auto") -> dict[str, Any]:
    """A message content part carrying an image, for vision-capable models."""
    encoded = base64.b64encode(data).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": detail},
    }


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _period_key(period: str, now: datetime) -> str:
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "month":
        return now.strftime("%Y-%m")
    return "total"


def _estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content) // _CHARS_PER_TOKEN + 8
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    total += len(str(part.get("text", ""))) // _CHARS_PER_TOKEN
                else:
                    total += _IMAGE_TOKENS
    return total


class LLMClient:
    """One configured connection to a model provider.

    ``budget_usd`` caps the whole service per ``budget_period`` ("total",
    "month" or "day"); ``tenant_budget_usd`` caps each tenant within it.
    ``0`` disables a cap -- do that knowingly.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        chat_model: str = "gpt-5.4-mini",
        embedding_model: str = "text-embedding-3-small",
        embedding_dimensions: int | None = None,
        budget_usd: float = 10.0,
        tenant_budget_usd: float = 0.0,
        budget_period: str = "month",
        prices: Mapping[str, Sequence[float]] | None = None,
        timeout_s: float = 90.0,
        max_retries: int = 2,
        embedding_batch_size: int = 96,
        ledger: Ledger | None = None,
        transport: Any = None,
        clock: Any = None,
    ) -> None:
        if budget_period not in ("total", "month", "day"):
            raise ValueError("budget_period must be 'total', 'month' or 'day'")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.budget_usd = budget_usd
        self.tenant_budget_usd = tenant_budget_usd
        self.budget_period = budget_period
        self.prices: dict[str, tuple[float, float]] = dict(DEFAULT_PRICES)
        for model, pair in (prices or {}).items():
            self.prices[model] = (float(pair[0]), float(pair[1]) if len(pair) > 1 else 0.0)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.embedding_batch_size = embedding_batch_size
        self.ledger: Ledger = ledger or MemoryLedger()
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._warned_unknown: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # -- money -----------------------------------------------------------

    def price(self, model: str) -> tuple[float, float]:
        if model in self.prices:
            return self.prices[model]
        # Dated snapshots ("gpt-4.1-mini-2025-04-14") cost what their family costs.
        for known in sorted(self.prices, key=len, reverse=True):
            if model.startswith(known):
                return self.prices[known]
        if model not in self._warned_unknown:
            self._warned_unknown.add(model)
            logger.warning("no price for model %r; assuming %s per 1M tokens", model, UNKNOWN_PRICE)
        return UNKNOWN_PRICE

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.price(model)
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000

    def _keys(self, tenant_id: str | None) -> list[tuple[str, float]]:
        period = _period_key(self.budget_period, self._clock())
        keys = [(f"spend:{period}", self.budget_usd)]
        if tenant_id:
            keys.append((f"spend:{period}:{tenant_id}", self.tenant_budget_usd))
        return keys

    async def _reserve(self, tenant_id: str | None, amount: float) -> list[str]:
        """Add ``amount`` to every counter, or to none of them."""
        taken: list[str] = []
        for key, cap in self._keys(tenant_id):
            total = await self.ledger.add(key, amount)
            taken.append(key)
            if cap > 0 and total > cap:
                for done in taken:
                    await self.ledger.add(done, -amount)
                scope = "this tenant's" if key.count(":") > 1 else "the service's"
                raise BudgetExceededError(
                    f"The AI spending cap for {scope} {self.budget_period} budget "
                    f"(${cap:.2f}) would be exceeded. Nothing was sent."
                )
        return taken

    async def _settle(self, keys: list[str], reserved: float, actual: float) -> None:
        if keys and actual != reserved:
            for key in keys:
                await self.ledger.add(key, actual - reserved)

    async def spend(self, tenant_id: str | None = None, *, recent: int = 20) -> dict[str, Any]:
        """What has been spent this period, against which caps."""
        keys = self._keys(tenant_id)
        service = await self.ledger.get(keys[0][0])
        tenant = await self.ledger.get(keys[1][0]) if len(keys) > 1 else None
        return {
            "configured": self.configured,
            "period": self.budget_period,
            "spent_usd": round(service, 6),
            "budget_usd": self.budget_usd,
            "remaining_usd": None
            if self.budget_usd <= 0
            else round(max(self.budget_usd - service, 0), 6),
            "tenant_spent_usd": None if tenant is None else round(tenant, 6),
            "tenant_budget_usd": self.tenant_budget_usd or None,
            "chat_model": self.chat_model,
            "embedding_model": self.embedding_model,
            # lrange(0, -1) is "everything", so 0 must not reach the ledger.
            "recent": await self.ledger.recent(tenant_id, recent) if recent > 0 else [],
        }

    # -- transport -------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        if not self.api_key:
            raise LLMError("No API key configured for the llm plugin (JFAST_LLM_API_KEY).")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        attempt = 0
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_s, transport=self._transport
        ) as http:
            while True:
                try:
                    response = await http.post(path, json=body, headers=headers)
                except httpx.TransportError as exc:
                    if attempt >= self.max_retries:
                        raise LLMError(f"Model provider unreachable: {type(exc).__name__}") from exc
                    await asyncio.sleep(min(2**attempt, 8))
                    attempt += 1
                    tracing.annotate(**{"llm.retries": attempt})
                    continue
                if response.status_code < 400:
                    data: dict[str, Any] = response.json()
                    return data
                if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after", "")
                    delay = (
                        float(retry_after)
                        if retry_after.replace(".", "", 1).isdigit()
                        else 2**attempt
                    )
                    await asyncio.sleep(min(delay, 20))
                    attempt += 1
                    tracing.annotate(**{"llm.retries": attempt})
                    continue
                # The provider's message only: the request body is the prompt.
                try:
                    detail = str(response.json().get("error", {}).get("message", ""))
                except ValueError:
                    detail = response.text[:200]
                raise LLMError(f"Model provider answered {response.status_code}: {detail[:300]}")

    # -- calls -----------------------------------------------------------

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tenant_id: str | None = None,
        purpose: str = "chat",
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        schema_name: str = "answer",
        max_tokens: int = 2000,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> ChatResult:
        """One chat completion. With ``schema``, strict JSON output, parsed into ``data``."""
        model = model or self.chat_model
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_completion_tokens": max_tokens,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            body["temperature"] = temperature
        body.update(extra or {})

        # Model, purpose, tenant and what the call cost -- the ledger's fields,
        # and like the ledger never the messages or the answer.
        with tracing.span(
            "llm.chat",
            **{
                "span.kind": "client",
                "llm.model": model,
                "llm.purpose": purpose,
                "jfast.tenant_id": tenant_id,
                "llm.max_tokens": max_tokens,
                "llm.structured": schema is not None,
            },
        ):
            reserved = self.cost(model, _estimate_tokens(messages), max_tokens)
            keys = await self._reserve(tenant_id, reserved)
            started = time.monotonic()
            try:
                data = await self._post("/chat/completions", body)
            except BaseException:
                await self._settle(keys, reserved, 0.0)
                raise
            usage = data.get("usage") or {}
            tokens_in, tokens_out = (
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            )
            actual = self.cost(model, tokens_in, tokens_out)
            await self._settle(keys, reserved, actual)
            result_usage = Usage(
                tokens_in,
                tokens_out,
                round(actual, 6),
                str(data.get("model") or model),
                int((time.monotonic() - started) * 1000),
            )
            await self._log(purpose, tenant_id, result_usage)
            _annotate_usage(result_usage)

            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")
            tracing.annotate(**{"llm.finish_reason": finish})
            if finish == "length":
                raise LLMError(
                    "The model's answer was cut off by max_tokens; raise it or shorten the prompt."
                )
            parsed = None
            if schema is not None:
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    raise LLMError("The model did not return valid JSON for the schema.") from exc
            return ChatResult(
                text=text, usage=result_usage, data=parsed, finish_reason=finish, raw=data
            )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        tenant_id: str | None = None,
        purpose: str = "embed",
        model: str | None = None,
    ) -> list[list[float]]:
        """Embeddings in input order, batched, each batch within the budget."""
        model = model or self.embedding_model
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.embedding_batch_size):
            batch = list(texts[start : start + self.embedding_batch_size])
            body: dict[str, Any] = {"model": model, "input": batch}
            if self.embedding_dimensions:
                body["dimensions"] = self.embedding_dimensions
            with tracing.span(
                "llm.embed",
                **{
                    "span.kind": "client",
                    "llm.model": model,
                    "llm.purpose": purpose,
                    "jfast.tenant_id": tenant_id,
                    "llm.inputs": len(batch),
                },
            ):
                reserved = self.cost(
                    model, sum(len(t) for t in batch) // _CHARS_PER_TOKEN + len(batch), 0
                )
                keys = await self._reserve(tenant_id, reserved)
                started = time.monotonic()
                try:
                    data = await self._post("/embeddings", body)
                except BaseException:
                    await self._settle(keys, reserved, 0.0)
                    raise
                tokens = int((data.get("usage") or {}).get("prompt_tokens", 0))
                actual = self.cost(model, tokens, 0)
                await self._settle(keys, reserved, actual)
                usage = Usage(
                    tokens, 0, round(actual, 6), model, int((time.monotonic() - started) * 1000)
                )
                await self._log(purpose, tenant_id, usage)
                _annotate_usage(usage)
            vectors.extend(
                item["embedding"] for item in sorted(data["data"], key=lambda d: d["index"])
            )
        return vectors

    async def _log(self, purpose: str, tenant_id: str | None, usage: Usage) -> None:
        entry = {
            "at": self._clock().isoformat(timespec="seconds"),
            "purpose": purpose,
            "tenant_id": tenant_id,
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "usd": usage.usd,
            "ms": usage.ms,
        }
        await self.ledger.record(entry)
        logger.info("llm call", extra={k: v for k, v in entry.items() if k != "at"})


def _annotate_usage(usage: Usage) -> None:
    tracing.annotate(
        **{
            "llm.response.model": usage.model,
            "llm.usage.input_tokens": usage.input_tokens,
            "llm.usage.output_tokens": usage.output_tokens,
            "llm.usd": usage.usd,
            "llm.ms": usage.ms,
        }
    )


class LLMEmbedder:
    """The ``rag`` embedder backed by this client: budgeted and logged.

    ``[plugin.rag] embedder = "llm"`` builds one from the ``llm`` plugin.
    """

    def __init__(self, client: LLMClient, *, dimensions: int) -> None:
        self._client = client
        self.dimensions = dimensions
        self.model_id = f"{client.embedding_model}:{dimensions}"

    async def embed(self, texts: list[str], *, tenant_id: str | None = None) -> list[list[float]]:
        return await self._client.embed(texts, tenant_id=tenant_id, purpose="rag-embed")


__all__ = [
    "DEFAULT_PRICES",
    "BudgetExceededError",
    "ChatResult",
    "LLMClient",
    "LLMEmbedder",
    "LLMError",
    "Ledger",
    "MemoryLedger",
    "RedisLedger",
    "Usage",
    "image_part",
    "text_part",
]
