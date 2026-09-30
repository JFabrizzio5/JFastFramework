"""JWT authentication: verification, scopes, rotation, revocation.

    from jfastframework.auth import Principal, require_scopes

    @router.post("/invoices")
    async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
        ...

The plugin verifies tokens and can mint them. It does not know who your users
are; the ``accounts`` plugin does -- users, password login and roles -- and
calls ``auth.issuer`` for you. A service with its own user store calls
``auth.issuer`` from its own login route instead.
"""

from typing import TYPE_CHECKING

from jfastframework.auth.jwks import JWKSClient, JWKSError
from jfastframework.auth.principal import Grant, Principal, current_principal
from jfastframework.auth.store import MemoryTokenStore, RedisTokenStore, TokenStore
from jfastframework.auth.tokens import (
    SUPPORTED_ALGORITHMS,
    TokenClaims,
    TokenError,
    issue,
    verify,
)

if TYPE_CHECKING:
    # What the type checker sees in place of ``__getattr__`` below, which it can
    # only type as ``object``: under mypy --strict the documented
    # ``Depends(require_auth)`` was an error in every project that followed
    # the docs. Never executed, so the lazy import stays lazy.
    from jfastframework.plugins.builtin.auth import (
        optional_auth,
        principal_of,
        require_auth,
        require_roles,
        require_scopes,
    )

__all__ = [
    "SUPPORTED_ALGORITHMS",
    "Grant",
    "JWKSClient",
    "JWKSError",
    "MemoryTokenStore",
    "Principal",
    "RedisTokenStore",
    "TokenClaims",
    "TokenError",
    "TokenStore",
    "current_principal",
    "issue",
    "optional_auth",
    "principal_of",
    "require_auth",
    "require_roles",
    "require_scopes",
    "verify",
]


def __getattr__(name: str) -> object:
    """Expose the FastAPI dependencies without importing the plugin eagerly.

    ``jfastframework.auth`` must stay importable in a process that has no
    FastAPI app — a worker, a script, a test of the token functions alone.
    The dependencies live in the plugin because they read ``request.state``.
    """
    if name in ("require_auth", "require_scopes", "require_roles", "optional_auth", "principal_of"):
        from jfastframework.plugins.builtin import auth as plugin

        return getattr(plugin, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
