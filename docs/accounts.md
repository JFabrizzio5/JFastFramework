# Accounts

Users, password login, roles and permissions -- the user store the `auth`
plugin deliberately leaves out, as a plugin you turn on.

```toml
[plugins]
enabled = ["observability", "database", "cache", "auth", "accounts"]

[plugin.auth]
mode = "secret"
algorithms = ["HS256"]
issue_tokens = true

[plugin.accounts]
bootstrap_admin_email = "admin@example.com"
```

```bash
pip install "jfastframework[accounts]"
JFAST_AUTH_SECRET=...                           # at least 32 bytes
JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD=...     # first start only
```

On the first start the plugin creates its tables, a role called `admin` with
the permission `accounts:admin`, and the administrator. Remove the password
from the environment afterwards: the user exists, and the variable is only
read when it does not.

---

## The model

| Thing | What it is |
| --- | --- |
| Permission | A string: `invoices:write`, `reports:read`. Yours to name. |
| Role | A named set of permissions: `billing` = `invoices:read` + `invoices:write`. |
| User | An email, a password (or a provider identity), and roles. |

A signed-in user's permissions travel in the access token as scopes, so the
check on a route is one dependency and no database query:

```python
from fastapi import Depends
from jfastframework.accounts import require_permission
from jfastframework.auth import Principal

@router.post("/invoices")
async def create(caller: Principal = Depends(require_permission("invoices:write"))):
    ...
```

`require_permission` is `require_scopes` under the name that says what it is
for here. No token is 401; a token without the permission is 403.

---

## Endpoints

Under `prefix` (default `/auth`), next to `auth`'s own `/auth/refresh` and
`/auth/logout`:

| Method | Path | Does |
| --- | --- | --- |
| POST | `/auth/login` | `{email, password}` → a token pair |
| POST | `/auth/register` | Creates an account and signs in. Only with `allow_registration = true` |
| GET | `/auth/account` | The signed-in user, with roles and permissions |
| POST | `/auth/password` | `{current_password, new_password}` |

Under `admin_prefix` (default `/accounts`), for holders of `accounts:admin`:

| Method | Path | Does |
| --- | --- | --- |
| GET | `/accounts/users` | Users, paginated |
| POST | `/accounts/users` | `{email, password?, display_name?, roles}` |
| PATCH | `/accounts/users/{id}` | `{is_active?, display_name?, roles?}` |
| GET | `/accounts/roles` | Roles and their permissions |
| POST | `/accounts/roles` | `{name, description?, permissions}` |
| PATCH | `/accounts/roles/{id}` | `{description?, permissions?}` |
| DELETE | `/accounts/roles/{id}` | Removes the role from everyone who had it |

---

## What it defends against

**Guessing.** After `max_failed_logins` wrong passwords (5) the account locks
for `lockout_minutes` (15). Counted per account, not per address, so a guesser
cannot get round it by changing IP. Put the `ratelimit` plugin in front of
`/auth/login` as well, to slow down guessing across many accounts.

**Finding out who has an account.** A wrong password, an unknown email and a
locked account all answer the same 401 with the same message -- and take the
same time, because an unknown email is still checked against a decoy hash.

**A permission that outlives its removal.** `accounts` registers `auth`'s
`on_refresh` hook, so every refresh re-reads the user's roles. Take a
permission away, or deactivate the user, and it applies at their next refresh
-- deactivation ends the session there -- instead of at the end of a
thirty-day refresh token. The access token already issued keeps working until
it expires (`access_lifetime_minutes`, 15), which is the reason to keep that
short.

**A stolen password table.** Passwords are hashed with argon2id, off the event
loop, and rehashed at the next login when the parameters change.

**Taking over an account through a provider.** With social login configured in
`auth`, a provider identity is linked to an existing account only through a
*verified* email. An unverified one would let anybody who can type an address
into a provider sign in as its owner.

---

## Tenants

Every user and role belongs to a tenant, or to none. Login, registration and
social sign-in read the tenant from the request -- the `tenancy` plugin's
subdomain or path source; the token source cannot, because there is no token
yet. The same email in two tenants is two people, and an administrator sees
and changes only the users and roles of their own tenant.

**A SaaS where each account is its own tenant** needs no tenant table at all:
add the tenancy plugin with `sources = ["token", "user"]` and the signed-in
user's id becomes the tenant for everything they create. The users themselves
stay in no tenant, which is right: the account is the boundary. See
[the `user` source](multitenancy.md#every-account-is-its-own-tenant-the-user-source).

---

## Settings

| Setting | Default | |
| --- | --- | --- |
| `prefix` / `admin_prefix` | `/auth` / `/accounts` | |
| `allow_registration` | `false` | Also governs whether social login may create users |
| `default_roles` | `[]` | Roles a self-registered user starts with |
| `min_password_length` | `10` | |
| `max_failed_logins` / `lockout_minutes` | `5` / `15` | |
| `admin_permission` | `accounts:admin` | |
| `bootstrap_admin_email` | `""` | Password from `JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD` |
| `social_login` | `true` | Only acts when `auth` has providers |

The tables (`jfast_users`, `jfast_roles`, `jfast_role_permissions`,
`jfast_user_roles`) are the plugin's: created at startup and skipped by the
service's Alembic autogenerate.

## What is not here

- **Email verification and password reset by email.** Both need to send mail
  and hold a token; the `mail` plugin can, and the flow is not built yet.
- **Multi-factor authentication.**
- **Wildcard permissions.** `invoices:*` is not a thing; list what a role may do.
