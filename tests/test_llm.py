"""The llm client: budget, ledger, retries and parsing, with no network.

httpx.MockTransport plays the provider. Every test that says "nothing was
sent" checks the transport's request count, not just the exception.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from jfastframework.errors import PluginError
from jfastframework.llm import BudgetExceededError, LLMClient, LLMError, MemoryLedger, image_part
from jfastframework.plugins.builtin.llm import LLMPlugin
from jfastframework.testing import build_test_app


class Provider:
    """A scripted OpenAI-compatible endpoint."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def chat_ok(text: str = "hola", prompt: int = 1000, completion: int = 200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-5.4-mini",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        },
    )


def client(provider: Provider, **kwargs: object) -> LLMClient:
    defaults: dict[str, object] = {
        "api_key": "sk-test",
        "transport": httpx.MockTransport(provider),
        "clock": lambda: datetime(2026, 9, 29, tzinfo=UTC),
    }
    return LLMClient(**(defaults | kwargs))  # type: ignore[arg-type]


async def test_chat_returns_text_and_charges_the_real_usage() -> None:
    provider = Provider(chat_ok(prompt=1000, completion=200))
    llm = client(provider, budget_usd=1.0)
    result = await llm.chat([{"role": "user", "content": "hola"}], tenant_id="acme", purpose="test")

    assert result.text == "hola"
    # gpt-5.4-mini: 0.75 in / 4.50 out per 1M tokens.
    expected = (1000 * 0.75 + 200 * 4.50) / 1_000_000
    assert result.usage.usd == pytest.approx(expected)
    spend = await llm.spend("acme")
    assert spend["spent_usd"] == pytest.approx(expected)
    assert spend["tenant_spent_usd"] == pytest.approx(expected)
    assert spend["recent"][0]["purpose"] == "test"
    # The ledger holds numbers, never the conversation.
    assert "hola" not in json.dumps(spend["recent"])
    assert provider.requests[0].headers["authorization"] == "Bearer sk-test"


async def test_a_call_that_would_cross_the_cap_is_never_sent() -> None:
    provider = Provider(chat_ok())
    # max_tokens=4000 of gpt-5.4-mini output alone is $0.018: above a $0.01 cap.
    llm = client(provider, budget_usd=0.01)
    with pytest.raises(BudgetExceededError):
        await llm.chat([{"role": "user", "content": "x"}], max_tokens=4000)
    assert provider.requests == []
    assert (await llm.spend())["spent_usd"] == 0


async def test_the_tenant_cap_is_separate_from_the_service_cap() -> None:
    provider = Provider(chat_ok(prompt=10, completion=10))
    llm = client(provider, budget_usd=10.0, tenant_budget_usd=0.005)
    await llm.chat([{"role": "user", "content": "x"}], tenant_id="acme", max_tokens=500)
    with pytest.raises(BudgetExceededError, match="tenant"):
        await llm.chat([{"role": "user", "content": "x"}], tenant_id="acme", max_tokens=1200)
    # Another tenant is not affected, and the refused reservation left no trace.
    await llm.chat([{"role": "user", "content": "x"}], tenant_id="globex", max_tokens=500)
    assert len(provider.requests) == 2
    assert (await llm.spend("acme"))["tenant_spent_usd"] == pytest.approx(
        (10 * 0.75 + 10 * 4.5) / 1e6, abs=1e-6
    )


async def test_a_failed_call_releases_its_reservation() -> None:
    provider = Provider(httpx.Response(400, json={"error": {"message": "bad model"}}))
    llm = client(provider, budget_usd=1.0)
    with pytest.raises(LLMError, match="bad model"):
        await llm.chat([{"role": "user", "content": "secret prompt"}])
    assert (await llm.spend())["spent_usd"] == pytest.approx(0)


async def test_the_error_message_never_repeats_the_prompt() -> None:
    provider = Provider(httpx.Response(500, text="upstream exploded"))
    llm = client(provider, max_retries=0)
    with pytest.raises(LLMError) as caught:
        await llm.chat([{"role": "user", "content": "customer contract text"}])
    assert "customer contract" not in str(caught.value)


async def test_429_is_retried_after_the_advertised_delay() -> None:
    provider = Provider(httpx.Response(429, headers={"retry-after": "0"}), chat_ok())
    llm = client(provider, max_retries=2)
    result = await llm.chat([{"role": "user", "content": "x"}])
    assert result.text == "hola"
    assert len(provider.requests) == 2


async def test_schema_output_is_parsed_and_sent_strict() -> None:
    provider = Provider(chat_ok(text='{"total": 12.5}'))
    llm = client(provider)
    schema = {
        "type": "object",
        "properties": {"total": {"type": "number"}},
        "required": ["total"],
        "additionalProperties": False,
    }
    result = await llm.chat([{"role": "user", "content": "x"}], schema=schema)
    assert result.data == {"total": 12.5}
    assert provider.bodies[0]["response_format"]["json_schema"]["strict"] is True


async def test_a_truncated_answer_is_an_error_not_half_an_answer() -> None:
    truncated = httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"tot'}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    with pytest.raises(LLMError, match="cut off"):
        await client(Provider(truncated)).chat([{"role": "user", "content": "x"}])


async def test_images_are_sent_as_content_parts() -> None:
    provider = Provider(chat_ok())
    llm = client(provider)
    await llm.chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read"},
                    image_part(b"\xff\xd8\xff", "image/jpeg"),
                ],
            }
        ]
    )
    part = provider.bodies[0]["messages"][0]["content"][1]
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_embeddings_come_back_in_input_order_across_batches() -> None:
    def embeddings(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        # Reversed on purpose: the client must sort by index.
        data = [
            {"index": i, "embedding": [float(len(t))]} for i, t in reversed(list(enumerate(texts)))
        ]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 5}})

    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return embeddings(request)

    llm = LLMClient(
        api_key="k",
        transport=httpx.MockTransport(transport),
        embedding_batch_size=2,
        embedding_dimensions=256,
    )
    vectors = await llm.embed(["a", "bb", "ccc"])
    assert vectors == [[1.0], [2.0], [3.0]]
    assert len(calls) == 2
    assert json.loads(calls[0].content)["dimensions"] == 256


async def test_an_unknown_model_is_priced_high() -> None:
    provider = Provider(chat_ok(prompt=1000, completion=0))
    llm = client(provider)
    result = await llm.chat(
        [{"role": "user", "content": "x"}], model="mystery-model", max_tokens=10
    )
    assert result.usage.usd == pytest.approx(1000 * 10.0 / 1e6)


async def test_dated_snapshots_cost_what_their_family_costs() -> None:
    llm = client(Provider(chat_ok()))
    assert llm.price("gpt-4.1-mini-2025-04-14") == llm.price("gpt-4.1-mini")


async def test_the_monthly_budget_resets_with_the_month() -> None:
    ledger = MemoryLedger()
    now = {"t": datetime(2026, 9, 30, tzinfo=UTC)}
    provider = Provider(chat_ok(prompt=10, completion=10))
    llm = LLMClient(
        api_key="k",
        transport=httpx.MockTransport(provider),
        ledger=ledger,
        budget_usd=0.01,
        clock=lambda: now["t"],
    )
    await llm.chat([{"role": "user", "content": "x"}], max_tokens=100)
    assert (await llm.spend())["spent_usd"] > 0
    now["t"] = datetime(2026, 10, 1, tzinfo=UTC)
    assert (await llm.spend())["spent_usd"] == 0


async def test_without_a_key_nothing_is_sent_and_the_message_names_the_variable() -> None:
    provider = Provider(chat_ok())
    llm = client(provider, api_key=None)
    with pytest.raises(LLMError, match="JFAST_LLM_API_KEY"):
        await llm.chat([{"role": "user", "content": "x"}])
    assert provider.requests == []
    assert (await llm.spend())["spent_usd"] == pytest.approx(0)


def test_plugin_rejects_an_unknown_budget_period() -> None:
    app = build_test_app()
    with pytest.raises(PluginError, match="budget_period"):
        LLMPlugin({"budget_period": "week"}).register(app.state.jfast)


async def test_plugin_without_a_key_is_degraded_not_failed() -> None:
    app = build_test_app()
    ctx = app.state.jfast
    plugin = LLMPlugin({})
    plugin.register(ctx)
    report = await plugin.health(ctx)
    assert report.healthy is False and report.critical is False
    assert ctx.has("llm")
