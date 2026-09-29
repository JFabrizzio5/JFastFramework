"""The caller's bearer token, for upstreams configured to forward it.

Set by the ``http`` plugin's middleware for the length of a request, and only
when some upstream has ``forward_authorization = true``. It is never read for
an upstream that has not asked for it: a token sent to a service that was not
its audience is a token leaked.
"""

from __future__ import annotations

from contextvars import ContextVar

#: The inbound ``Authorization`` header, when it carries a bearer token.
inbound_authorization: ContextVar[str | None] = ContextVar(
    "jfast_inbound_authorization", default=None
)
