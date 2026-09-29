"""Language models behind a spending cap.

    [plugins]
    enabled = ["observability", "cache", "llm"]

    [plugin.llm]
    chat_model = "gpt-5.4-mini"
    embedding_model = "text-embedding-3-small"
    budget_usd = 20.0          # per budget_period, for the whole service
    budget_period = "month"    # "month" | "day" | "total"
    tenant_budget_usd = 2.0    # per tenant, within the same period; 0 = none

    [plugin.llm.prices]        # USD per 1M tokens: [input, output]
    "my-finetune" = [1.2, 4.8]

The key comes from ``JFAST_LLM_API_KEY`` and never from ``jfast.toml``. Without
one the service still starts -- ``/ready`` reports the plugin as degraded, not
failed -- and every call raises a 503 that says which variable is missing.

The spend is counted in Redis when the ``cache`` plugin is enabled, so the cap
holds across workers and replicas. Without it the plugin counts in memory and
says so at startup: that is a per-process budget, fine on a laptop and wrong
anywhere with more than one worker.

Provides ``llm`` (a :class:`jfastframework.llm.LLMClient`). The ``rag`` plugin
uses it for embeddings with ``[plugin.rag] embedder = "llm"``, so ingesting
documents spends from the same budget as chatting about them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError
from jfastframework.llm import LLMClient, MemoryLedger, RedisLedger
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class LLMSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_LLM_", env_file=".env", extra="ignore")

    api_key: SecretStr | None = None
    base_url: str = "https://api.openai.com/v1"
    chat_model: str = "gpt-5.4-mini"
    embedding_model: str = "text-embedding-3-small"
    # Matryoshka models (text-embedding-3-*) can return fewer dimensions for
    # less storage; None asks for the model's native size.
    embedding_dimensions: int | None = None
    budget_usd: float = Field(default=10.0, ge=0)
    tenant_budget_usd: float = Field(default=0.0, ge=0)
    budget_period: str = "month"
    prices: dict[str, list[float]] = Field(default_factory=dict)
    timeout_s: float = 90.0
    max_retries: int = Field(default=2, ge=0, le=6)
    embedding_batch_size: int = Field(default=96, ge=1, le=2048)
    # Namespaced by service, so two services sharing a Redis keep two budgets.
    ledger_prefix: str = ""


class LLMPlugin(Plugin):
    meta = PluginMeta(
        name="llm",
        version="0.1.0",
        description="Chat, vision and embeddings over an OpenAI-compatible API, capped.",
        after=("observability", "cache"),
        provides=("llm",),
        default_enabled=False,
        extra="jfastframework[llm]",
    )
    Settings = LLMSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: LLMClient | None = None

    def register(self, ctx: AppContext) -> None:
        settings: LLMSettings = self.settings
        if settings.budget_period not in ("month", "day", "total"):
            raise PluginError(
                f"[plugin.llm] budget_period = {settings.budget_period!r}; "
                'use "month", "day" or "total".'
            )
        for model, pair in settings.prices.items():
            if not 1 <= len(pair) <= 2:
                raise PluginError(
                    f"[plugin.llm.prices] {model!r} must be [input, output] USD per 1M tokens."
                )

        if ctx.has("cache.client"):
            prefix = settings.ledger_prefix or f"jfast:llm:{ctx.settings.app_name}:"
            ledger: Any = RedisLedger(ctx.require("cache.client"), prefix=prefix)
        else:
            ledger = MemoryLedger()
            ctx.logger.warning(
                "llm is counting spend in memory: each worker has its own budget. "
                "Enable the cache plugin so the cap holds for the whole service."
            )

        if settings.budget_usd == 0:
            ctx.logger.warning("llm has no spending cap ([plugin.llm] budget_usd = 0).")

        self._client = LLMClient(
            api_key=settings.api_key.get_secret_value() if settings.api_key else None,
            base_url=settings.base_url,
            chat_model=settings.chat_model,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            budget_usd=settings.budget_usd,
            tenant_budget_usd=settings.tenant_budget_usd,
            budget_period=settings.budget_period,
            prices=settings.prices,
            timeout_s=settings.timeout_s,
            max_retries=settings.max_retries,
            embedding_batch_size=settings.embedding_batch_size,
            ledger=ledger,
        )
        ctx.provide("llm", self._client)

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._client is None:
            return HealthReport.fail("llm client not initialised")
        if not self._client.configured:
            # Not critical: the rest of the service works without a model.
            return HealthReport(healthy=False, detail="no JFAST_LLM_API_KEY", critical=False)
        spend = await self._client.spend(recent=0)
        cap = f"${spend['budget_usd']:.2f}" if spend["budget_usd"] else "no cap"
        return HealthReport.ok(
            f"{self._client.chat_model}, ${spend['spent_usd']:.4f} of {cap} this {spend['period']}",
            spent_usd=spend["spent_usd"],
            budget_usd=spend["budget_usd"],
        )
