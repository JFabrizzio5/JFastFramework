"""The sibling-service client: what it retries, what it refuses to, and when it stops calling.

No network: every upstream is an ``httpx.MockTransport``, time for the breaker
and the budget is a number the test moves, and backoff sleeps are recorded
rather than slept.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request

from jfastframework.errors import PluginError
from jfastframework.http import (
    BreakerPolicy,
    Bulkhead,
    BulkheadFullError,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryBudget,
    RetryPolicy,
    ServiceClient,
    Timeouts,
    Upstream,
    UpstreamTimeoutError,
    UpstreamUnreachableError,
)
from jfastframework.http.client import HttpClients, retry_after_seconds
from jfastframework.http.context import inbound_authorization
from jfastframework.plugins.builtin.http import HttpPlugin
from jfastframework.plugins.builtin.observability import request_id_var
from jfastframework.testing import build_test_app, client_for


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Upstreamish:
    """A scripted upstream: answers from a list, records what it received."""

    def __init__(self, *answers: int | Exception | Callable[[httpx.Request], Any]) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            result = answer(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result  # type: ignore[no-any-return]
        return httpx.Response(answer, json={"status": answer})

    @property
    def calls(self) -> int:
        return len(self.requests)


def make_client(
    upstream: Upstreamish,
    *,
    clock: Clock | None = None,
    sleeps: list[float] | None = None,
    **options: Any,
) -> ServiceClient:
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    options.setdefault("retry", RetryPolicy(attempts=3, backoff_base=0.1, backoff_max=2.0))
    return ServiceClient(
        Upstream(name="billing", base_url="http://billing", **options),
        transport=httpx.MockTransport(upstream),
        clock=clock or Clock(),
        sleep=sleep,
        rng=random.Random(7),
    )


# -- what is retried ---------------------------------------------------------


async def test_a_get_is_retried_through_a_503() -> None:
    upstream = Upstreamish(503, 200)
    response = await make_client(upstream).get("/invoices/7")
    assert response.status_code == 200
    assert upstream.calls == 2


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "OPTIONS"])
async def test_idempotent_methods_are_retried(method: str) -> None:
    upstream = Upstreamish(502, 504, 200)
    response = await make_client(upstream).request(method, "/x")
    assert response.status_code == 200
    assert upstream.calls == 3


@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_a_non_idempotent_call_is_not_retried(method: str) -> None:
    upstream = Upstreamish(503, 200)
    response = await make_client(upstream).request(method, "/charges", json={"amount": 5})
    assert response.status_code == 503
    assert upstream.calls == 1


async def test_an_idempotency_key_makes_a_post_retryable_and_is_sent() -> None:
    upstream = Upstreamish(503, 201)
    response = await make_client(upstream).post(
        "/charges", json={"amount": 5}, idempotency_key="charge-41"
    )
    assert response.status_code == 201
    assert [r.headers["Idempotency-Key"] for r in upstream.requests] == ["charge-41"] * 2
    assert upstream.requests[0].content == upstream.requests[1].content


async def test_the_caller_can_forbid_or_vouch_for_a_retry() -> None:
    forbidden = Upstreamish(503, 200)
    assert (await make_client(forbidden).get("/x", retry=False)).status_code == 503
    assert forbidden.calls == 1

    vouched = Upstreamish(503, 200)
    assert (await make_client(vouched).post("/x", retry=True)).status_code == 200
    assert vouched.calls == 2


@pytest.mark.parametrize("status", [400, 404, 409, 422, 500, 501])
async def test_an_answer_that_would_come_back_the_same_is_returned_not_retried(
    status: int,
) -> None:
    upstream = Upstreamish(status, 200)
    assert (await make_client(upstream).get("/x")).status_code == status
    assert upstream.calls == 1


async def test_a_transport_error_on_a_get_is_retried() -> None:
    upstream = Upstreamish(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), 200)
    assert (await make_client(upstream).get("/x")).status_code == 200
    assert upstream.calls == 3


async def test_a_post_that_got_no_answer_raises_without_retrying() -> None:
    upstream = Upstreamish(httpx.ReadTimeout("slow"), 200)
    with pytest.raises(UpstreamUnreachableError) as caught:
        await make_client(upstream).post("/charges")
    assert upstream.calls == 1
    assert caught.value.status_code == 503
    assert caught.value.to_problem()["upstream"] == "billing"


async def test_retries_stop_at_the_attempt_limit() -> None:
    upstream = Upstreamish(httpx.ConnectError("refused"))
    with pytest.raises(UpstreamUnreachableError, match="after 3 attempt"):
        await make_client(upstream).get("/x")
    assert upstream.calls == 3

    exhausted = Upstreamish(503)
    assert (await make_client(exhausted).get("/x")).status_code == 503
    assert exhausted.calls == 3


# -- how long between tries --------------------------------------------------


async def test_backoff_is_full_jitter_under_a_growing_cap() -> None:
    sleeps: list[float] = []
    upstream = Upstreamish(503)
    client = make_client(
        upstream,
        sleeps=sleeps,
        retry=RetryPolicy(attempts=6, backoff_base=0.1, backoff_max=0.5),
        retry_budget_min_per_second=10,
        breaker=BreakerPolicy(failure_threshold=100),
    )
    await client.get("/x")
    caps = [0.1, 0.2, 0.4, 0.5, 0.5]
    assert len(sleeps) == len(caps)
    assert all(0 <= slept <= cap for slept, cap in zip(sleeps, caps, strict=True))
    # Jitter: not every client waits the cap.
    assert len(set(sleeps)) == len(sleeps)


async def test_a_429_waits_exactly_what_retry_after_asks() -> None:
    sleeps: list[float] = []
    upstream = Upstreamish(
        lambda _: httpx.Response(429, headers={"Retry-After": "2"}),
        200,
    )
    assert (await make_client(upstream, sleeps=sleeps).get("/x")).status_code == 200
    assert sleeps == [2.0]


async def test_retry_after_as_a_date() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    stamp = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert retry_after_seconds(stamp, now=now) == pytest.approx(90)
    assert retry_after_seconds("0", now=now) == 0
    assert retry_after_seconds("soon", now=now) is None
    assert retry_after_seconds(None) is None


async def test_a_retry_after_longer_than_we_wait_returns_the_429() -> None:
    upstream = Upstreamish(lambda _: httpx.Response(429, headers={"Retry-After": "120"}), 200)
    response = await make_client(upstream).get("/x")
    assert response.status_code == 429
    assert upstream.calls == 1


# -- the deadline ------------------------------------------------------------


async def test_the_total_deadline_covers_the_whole_call() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    client = make_client(Upstreamish(hang), timeouts=Timeouts(total=0.2))
    started = asyncio.get_running_loop().time()
    with pytest.raises(UpstreamTimeoutError) as caught:
        await client.get("/slow")
    assert asyncio.get_running_loop().time() - started < 1.0
    assert caught.value.status_code == 503


async def test_no_retry_is_started_that_the_deadline_cannot_finish() -> None:
    sleeps: list[float] = []
    upstream = Upstreamish(lambda _: httpx.Response(503, headers={"Retry-After": "5"}), 200)
    client = make_client(upstream, sleeps=sleeps, timeouts=Timeouts(total=2))
    assert (await client.get("/x")).status_code == 503
    assert sleeps == []


@pytest.mark.parametrize("name", ["connect", "read", "write", "pool", "total"])
def test_every_timeout_is_mandatory(name: str) -> None:
    for bad in (0, -1, None):
        with pytest.raises(ValueError, match=name):
            Timeouts(**{name: bad})  # type: ignore[arg-type]


# -- the retry budget --------------------------------------------------------


def test_the_budget_allows_a_share_of_recent_requests() -> None:
    clock = Clock()
    budget = RetryBudget(ratio=0.5, min_per_second=0, window=10, clock=clock)
    for _ in range(10):
        budget.record_request()
    assert [budget.try_spend() for _ in range(6)] == [True] * 5 + [False]

    # Ten seconds on, those requests have left the window and so has the room.
    clock.advance(11)
    assert not budget.try_spend()
    budget.record_request()
    budget.record_request()
    assert budget.try_spend()


def test_the_budget_keeps_a_floor_for_a_quiet_client() -> None:
    budget = RetryBudget(ratio=0, min_per_second=0.5, window=10, clock=Clock())
    assert [budget.try_spend() for _ in range(6)] == [True] * 5 + [False]


async def test_during_an_outage_retries_add_a_tenth_not_double() -> None:
    upstream = Upstreamish(503)
    client = make_client(
        upstream,
        retry_budget_ratio=0.1,
        retry_budget_min_per_second=0,
        breaker=BreakerPolicy(failure_threshold=1000, minimum_calls=1000),
    )
    for _ in range(20):
        await client.get("/x")
    # Twenty calls with three attempts each would be sixty requests.
    assert 20 <= upstream.calls <= 20 + 3


# -- the circuit breaker -----------------------------------------------------


def breaker_client(upstream: Upstreamish, clock: Clock, **policy: Any) -> ServiceClient:
    policy.setdefault("failure_threshold", 3)
    policy.setdefault("cool_down", 10)
    return make_client(
        upstream, clock=clock, retry=RetryPolicy(attempts=1), breaker=BreakerPolicy(**policy)
    )


async def test_consecutive_failures_open_the_circuit_and_calls_stop_reaching_the_network() -> None:
    clock = Clock()
    upstream = Upstreamish(503)
    client = breaker_client(upstream, clock)
    for _ in range(3):
        assert (await client.get("/x")).status_code == 503
    assert client.breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError) as caught:
        await client.get("/x")
    assert upstream.calls == 3
    problem = caught.value.to_problem()
    assert caught.value.status_code == 503
    assert problem["upstream"] == "billing"
    assert problem["retry_after"] == 10


async def test_a_success_resets_the_consecutive_count() -> None:
    clock = Clock()
    upstream = Upstreamish(503, 503, 200, 503, 503, 200)
    client = breaker_client(upstream, clock)
    for _ in range(6):
        await client.get("/x")
    assert client.breaker.state is CircuitState.CLOSED


async def test_after_the_cool_down_one_probe_decides() -> None:
    clock = Clock()
    upstream = Upstreamish(503, 503, 503, 200)
    client = breaker_client(upstream, clock)
    for _ in range(3):
        await client.get("/x")

    clock.advance(9.9)
    assert client.breaker.state is CircuitState.OPEN
    clock.advance(0.1)
    assert client.breaker.state is CircuitState.HALF_OPEN

    assert (await client.get("/x")).status_code == 200
    assert client.breaker.state is CircuitState.CLOSED


async def test_a_failed_probe_opens_it_again_for_a_full_cool_down() -> None:
    clock = Clock()
    upstream = Upstreamish(httpx.ConnectError("refused"))
    client = breaker_client(upstream, clock)
    for _ in range(3):
        with pytest.raises(UpstreamUnreachableError):
            await client.get("/x")
    clock.advance(10)
    with pytest.raises(UpstreamUnreachableError):
        await client.get("/x")
    assert client.breaker.state is CircuitState.OPEN
    clock.advance(9)
    with pytest.raises(CircuitOpenError):
        await client.get("/x")
    assert upstream.calls == 4


async def test_while_the_probe_is_out_everyone_else_fails_fast() -> None:
    clock = Clock()
    release = asyncio.Event()

    async def slow_ok(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200)

    upstream = Upstreamish(503, 503, 503, slow_ok)
    client = breaker_client(upstream, clock)
    for _ in range(3):
        await client.get("/x")
    clock.advance(10)

    probe = asyncio.create_task(client.get("/x"))
    await asyncio.sleep(0.01)
    with pytest.raises(CircuitOpenError):
        await client.get("/x")
    release.set()
    assert (await probe).status_code == 200
    assert client.breaker.state is CircuitState.CLOSED


async def test_a_cancelled_probe_frees_the_slot_for_the_next_one() -> None:
    clock = Clock()
    breaker = CircuitBreaker(
        "billing", BreakerPolicy(failure_threshold=1, cool_down=5), clock=clock
    )
    breaker.record(breaker.acquire(), failed=True)
    clock.advance(5)
    probe = breaker.acquire()
    with pytest.raises(CircuitOpenError):
        breaker.acquire()
    breaker.release(probe)
    assert breaker.acquire().probe


def test_a_permit_from_before_a_transition_does_not_count_after_it() -> None:
    clock = Clock()
    breaker = CircuitBreaker(
        "billing", BreakerPolicy(failure_threshold=2, cool_down=5), clock=clock
    )
    stale = breaker.acquire()
    breaker.record(breaker.acquire(), failed=True)
    breaker.record(breaker.acquire(), failed=True)
    assert breaker.state is CircuitState.OPEN
    clock.advance(5)
    breaker.record(stale, failed=False)
    # The stale success did not close it; a real probe still decides.
    assert breaker.state is CircuitState.HALF_OPEN


async def test_a_failure_rate_opens_it_without_a_streak() -> None:
    clock = Clock()
    upstream = Upstreamish(*([503, 200] * 10))
    client = breaker_client(
        upstream, clock, failure_threshold=100, failure_rate=0.5, minimum_calls=10, window=30
    )
    for _ in range(9):
        await client.get("/x")
    assert client.breaker.state is CircuitState.CLOSED  # under minimum_calls
    await client.get("/x")
    assert client.breaker.state is CircuitState.OPEN


async def test_failures_outside_the_window_are_forgotten() -> None:
    clock = Clock()
    upstream = Upstreamish(503)
    client = breaker_client(
        upstream, clock, failure_threshold=100, failure_rate=0.5, minimum_calls=4, window=10
    )
    for _ in range(3):
        await client.get("/x")
    clock.advance(11)
    upstream.answers = [200]
    await client.get("/x")
    assert client.breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("status", [500, 429, 404])
async def test_an_upstream_that_answers_does_not_open_the_circuit(status: int) -> None:
    clock = Clock()
    upstream = Upstreamish(status)
    client = breaker_client(upstream, clock)
    for _ in range(10):
        await client.get("/x")
    assert client.breaker.state is CircuitState.CLOSED


# -- the bulkhead ------------------------------------------------------------


async def test_the_bulkhead_turns_away_the_call_past_its_limit() -> None:
    release = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200)

    upstream = Upstreamish(held)
    client = make_client(upstream, max_concurrent=2, bulkhead_wait=0)
    in_flight = [asyncio.create_task(client.get("/x")) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert client.bulkhead.in_flight == 2

    with pytest.raises(BulkheadFullError) as caught:
        await client.get("/x")
    assert caught.value.status_code == 503
    assert upstream.calls == 2
    # Turned away by us, not failed by them: the breaker has nothing to count.
    assert client.breaker.state is CircuitState.CLOSED

    release.set()
    assert [r.status_code for r in await asyncio.gather(*in_flight)] == [200, 200]
    assert client.bulkhead.in_flight == 0


async def test_a_call_may_wait_briefly_for_a_slot() -> None:
    bulkhead = Bulkhead("billing", max_concurrent=1, max_wait=1.0)
    order: list[str] = []

    async def hold(name: str, seconds: float) -> None:
        async with bulkhead.slot():
            order.append(name)
            await asyncio.sleep(seconds)

    await asyncio.gather(hold("first", 0.05), hold("second", 0))
    assert order == ["first", "second"]

    impatient = Bulkhead("billing", max_concurrent=1, max_wait=0.02)
    holder = asyncio.create_task(hold_on(impatient, 0.2))
    await asyncio.sleep(0.01)
    with pytest.raises(BulkheadFullError):
        await hold_on(impatient, 0)
    await holder


async def hold_on(bulkhead: Bulkhead, seconds: float) -> None:
    async with bulkhead.slot():
        await asyncio.sleep(seconds)


# -- headers -----------------------------------------------------------------


async def test_the_request_id_travels_with_the_call() -> None:
    upstream = Upstreamish(200)
    token = request_id_var.set("req-123")
    try:
        await make_client(upstream).get("/x")
    finally:
        request_id_var.reset(token)
    assert upstream.requests[0].headers["X-Request-ID"] == "req-123"


async def test_outside_a_request_no_request_id_is_invented() -> None:
    upstream = Upstreamish(200)
    await make_client(upstream).get("/x")
    assert "X-Request-ID" not in upstream.requests[0].headers


async def test_the_bearer_token_is_forwarded_only_to_an_upstream_that_asked() -> None:
    token = inbound_authorization.set("Bearer caller-token")
    try:
        forwarding, keeping = Upstreamish(200), Upstreamish(200)
        await make_client(forwarding, forward_authorization=True).get("/x")
        await make_client(keeping).get("/x")
    finally:
        inbound_authorization.reset(token)
    assert forwarding.requests[0].headers["Authorization"] == "Bearer caller-token"
    assert "Authorization" not in keeping.requests[0].headers


async def test_the_caller_s_headers_win_over_propagated_and_static_ones() -> None:
    upstream = Upstreamish(200)
    client = make_client(upstream, headers={"X-Service": "orders", "X-Env": "test"})
    token = request_id_var.set("req-1")
    try:
        await client.get("/x", headers={"X-Request-ID": "chosen", "X-Env": "override"})
    finally:
        request_id_var.reset(token)
    headers = upstream.requests[0].headers
    assert (headers["X-Service"], headers["X-Env"], headers["X-Request-ID"]) == (
        "orders",
        "override",
        "chosen",
    )


# -- the plugin --------------------------------------------------------------


def _router() -> APIRouter:
    router = APIRouter()

    @router.get("/proxy")
    async def proxy(request: Request) -> dict[str, Any]:
        billing = request.app.state.jfast.require("http").client("billing")
        response = await billing.get("/echo")
        return {"status": response.status_code, **(response.json() if response.content else {})}

    return router


def _plugin_app(**upstream: Any) -> Any:
    upstream.setdefault("base_url", "http://billing:8010")
    return build_test_app(
        plugins=["observability", "http"],
        extra_plugins=[HttpPlugin],
        routers=[_router()],
        raw={"plugin": {"http": {"upstreams": {"billing": upstream}}}},
    )


def _echo(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "request_id": request.headers.get("X-Request-ID"),
            "authorization": request.headers.get("Authorization"),
            "url": str(request.url),
        },
    )


def _mock(app: Any, handler: Any) -> ServiceClient:
    client: ServiceClient = app.state.jfast.require("http").client("billing")
    client._transport = httpx.MockTransport(handler)
    return client


async def test_through_the_plugin_the_request_id_and_token_reach_the_upstream() -> None:
    app = _plugin_app(forward_authorization=True)
    _mock(app, _echo)
    async with client_for(app) as client:
        bearer = await client.get(
            "/proxy", headers={"X-Request-ID": "abc", "Authorization": "Bearer t0k"}
        )
        basic = await client.get("/proxy", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert bearer.json() == {
        "status": 200,
        "request_id": "abc",
        "authorization": "Bearer t0k",
        "url": "http://billing:8010/echo",
    }
    # A password is never passed along, whatever the upstream asked for.
    assert basic.json()["authorization"] is None


async def test_an_open_circuit_is_a_503_problem_and_degrades_readiness() -> None:
    app = _plugin_app(breaker_failures=1, retries=0)
    _mock(app, lambda request: httpx.Response(503))
    async with client_for(app) as client:
        first = await client.get("/proxy")
        tripped = await client.get("/proxy")
        ready = await client.get("/ready")
    # The upstream's 503 is an answer, returned to the route as it is...
    assert first.json() == {"status": 503}
    # ...and the open circuit is not: no call is made, and the route's
    # unhandled error is a 503 problem naming the upstream.
    assert tripped.status_code == 503
    assert tripped.headers["content-type"].startswith("application/problem+json")
    assert tripped.json()["upstream"] == "billing"
    assert ready.status_code == 200
    check = ready.json()["checks"]["http"]
    assert check["status"] == "fail" and check["critical"] is False
    assert check["meta"]["breakers"]["billing"]["state"] == "open"


async def test_readiness_lists_closed_breakers_when_all_is_well() -> None:
    app = _plugin_app()
    async with client_for(app) as client:
        check = (await client.get("/ready")).json()["checks"]["http"]
    assert check["status"] == "ok"
    assert check["meta"]["breakers"] == {"billing": {"state": "closed"}}


def test_the_settings_become_the_policies() -> None:
    app = _plugin_app(
        read_timeout=3, total_timeout=9, retries=4, breaker_failures=7, max_concurrent=12
    )
    upstream = app.state.jfast.require("http").client("billing").upstream
    assert (upstream.timeouts.read, upstream.timeouts.total) == (3, 9)
    assert upstream.retry.attempts == 5
    assert upstream.breaker.failure_threshold == 7
    assert upstream.max_concurrent == 12


def test_a_misspelt_setting_is_refused_not_ignored() -> None:
    with pytest.raises(Exception, match="read_timout"):
        _plugin_app(read_timout=60)


def test_a_zero_timeout_is_refused_in_configuration() -> None:
    with pytest.raises(Exception, match="read_timeout"):
        _plugin_app(read_timeout=0)


def test_the_base_url_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL", "http://billing.internal:9000")
    app = build_test_app(
        plugins=["http"],
        extra_plugins=[HttpPlugin],
        raw={"plugin": {"http": {"upstreams": {"billing": {"retries": 1}}}}},
    )
    upstream = app.state.jfast.require("http").client("billing").upstream
    assert upstream.base_url == "http://billing.internal:9000"
    assert upstream.retry.attempts == 2


def test_asking_for_an_unconfigured_upstream_names_the_configured_ones() -> None:
    clients = HttpClients({"billing": Upstream(name="billing", base_url="http://b")})
    with pytest.raises(PluginError, match=r"no upstream named 'catalog'; configured: billing"):
        clients.client("catalog")


def test_the_plugin_imports_without_the_client() -> None:
    """`jfast plugins list` loads every plugin; httpx is only needed to call."""
    import importlib
    import sys

    touched = [
        name
        for name in sys.modules
        if name.startswith("httpx")
        or name in ("jfastframework.plugins.builtin.http", "jfastframework.http.client")
    ]
    saved = {name: sys.modules[name] for name in touched}
    for name in saved:
        if name.startswith("httpx"):
            sys.modules[name] = None  # type: ignore[assignment]
        else:
            del sys.modules[name]
    try:
        module = importlib.import_module("jfastframework.plugins.builtin.http")
        assert module.HttpPlugin.meta.name == "http"
        with pytest.raises(ImportError):
            importlib.import_module("jfastframework.http.client")
    finally:
        sys.modules.pop("jfastframework.plugins.builtin.http", None)
        sys.modules.pop("jfastframework.http.client", None)
        sys.modules.update(saved)
