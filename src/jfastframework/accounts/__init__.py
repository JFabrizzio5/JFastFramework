"""Users, login, roles and permissions, on top of the ``auth`` plugin.

    [plugins]
    enabled = ["database", "cache", "auth", "accounts"]

    [plugin.auth]
    mode = "secret"
    issue_tokens = true

``auth`` verifies and mints tokens; this is the user store it deliberately does
not have. A permission is a string (``invoices:write``), a role is a set of
them, and a user's permissions travel in the token as scopes::

    from jfastframework.accounts import require_permission

    @router.post("/invoices")
    async def create(caller: Principal = Depends(require_permission("invoices:write"))):
        ...

See ``docs/accounts.md``.
"""

from jfastframework.accounts.service import AccountsService, LoginResult, User, normalize_email

__all__ = [
    "AccountsService",
    "LoginResult",
    "User",
    "normalize_email",
    "require_permission",
]


def require_permission(*permissions: str):  # type: ignore[no-untyped-def]
    """Require every listed permission, or 403. ``require_scopes`` by its other name.

    Permissions are the scopes the accounts plugin puts in each token, so the
    two are the same check; this spelling reads as what it means on a route
    guarded by roles an administrator manages.
    """
    from jfastframework.plugins.builtin.auth import require_scopes

    return require_scopes(*permissions)
