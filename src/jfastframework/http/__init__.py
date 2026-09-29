"""Calling sibling services: deadlines, retries, circuit breakers, bulkheads.

    from jfastframework.http import ServiceClient, Upstream

    billing = ServiceClient(Upstream(name="billing", base_url="http://billing:8010"))
    response = await billing.get("/invoices/7")

In a service, configure upstreams under ``[plugin.http.upstreams.<name>]`` and
take the shared client from the context instead -- the circuit breaker and the
bulkhead are per upstream and per process, so two clients for one upstream
would each think it was healthy::

    billing = request.app.state.jfast.require("http").client("billing")

The client itself needs ``pip install jfastframework[http]``. The error types,
the policies and the context variable import without it, which is what lets
the ``http`` plugin load for ``jfast plugins list`` on a machine without httpx.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jfastframework.http.errors import (
    BulkheadFullError,
    CircuitOpenError,
    UpstreamError,
    UpstreamTimeoutError,
    UpstreamUnreachableError,
)
from jfastframework.http.resilience import (
    BreakerPolicy,
    Bulkhead,
    CircuitBreaker,
    CircuitState,
    RetryBudget,
    RetryPolicy,
)

if TYPE_CHECKING:
    from jfastframework.http.client import HttpClients, ServiceClient, Timeouts, Upstream

__all__ = [
    "BreakerPolicy",
    "Bulkhead",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "HttpClients",
    "RetryBudget",
    "RetryPolicy",
    "ServiceClient",
    "Timeouts",
    "Upstream",
    "UpstreamError",
    "UpstreamTimeoutError",
    "UpstreamUnreachableError",
]

_NEEDS_HTTPX = {"HttpClients", "ServiceClient", "Timeouts", "Upstream"}


def __getattr__(name: str) -> Any:
    # The client imports httpx; everything else here must not.
    if name in _NEEDS_HTTPX:
        from jfastframework.http import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
