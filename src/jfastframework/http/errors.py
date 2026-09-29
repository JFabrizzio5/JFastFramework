"""What a call to a sibling service raises when it gets no answer to return.

All of them are 503s: a route that lets one escape answers ``Service
Unavailable`` as problem+json, with the upstream named, rather than a 500 with
a traceback. A response the upstream *did* send -- a 404, a 500, a 503 after
the retries ran out -- is returned, not raised; the caller decides what it
means.
"""

from __future__ import annotations

from typing import Any

from jfastframework.errors import ServiceUnavailableError


class UpstreamError(ServiceUnavailableError):
    """A sibling service could not be reached or did not answer in time."""

    title = "Upstream Unavailable"

    def __init__(self, detail: str, *, upstream: str, **extra: Any) -> None:
        super().__init__(detail, upstream=upstream, **extra)
        self.upstream = upstream


class CircuitOpenError(UpstreamError):
    """The breaker for this upstream is open: failed fast, nothing was sent."""

    title = "Upstream Circuit Open"

    def __init__(self, *, upstream: str, retry_after: float) -> None:
        super().__init__(
            f"calls to {upstream!r} are suspended after repeated failures; "
            f"the next probe is in {retry_after:.1f}s",
            upstream=upstream,
            retry_after=round(retry_after, 3),
        )
        self.retry_after = retry_after


class BulkheadFullError(UpstreamError):
    """Too many calls to this upstream are already in flight from this process."""

    title = "Upstream Bulkhead Full"


class UpstreamTimeoutError(UpstreamError):
    """The call's total deadline passed, retries included."""

    title = "Upstream Timeout"


class UpstreamUnreachableError(UpstreamError):
    """Every attempt failed at the transport: refused, reset, or timed out."""

    title = "Upstream Unreachable"
