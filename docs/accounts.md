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
| POST | `/auth/login` | `{email, password}` → a token pair, or the second step (below) |
| POST | `/auth/login/mfa` | `{mfa_token, code}` → a token pair. With `mfa = true` |
| POST | `/auth/register` | Creates an account. Only with `allow_registration = true` |
| GET | `/auth/account` | The signed-in user, with roles, permissions, `email_verified`, `mfa_enabled` |
| POST | `/auth/password` | `{current_password, new_password}` |
| POST | `/auth/logout/all` | Ends every session of the caller, this one included |
| GET | `/auth/features` | What is on: registration, verification, reset, MFA -- for a frontend |
| POST | `/auth/verify` | `{token}` from the email. With `email_verification` on |
| POST | `/auth/verify/resend` | `{email}` → 202, always |
| POST | `/auth/password/forgot` | `{email}` → 202, always. With `password_reset = true` |
| POST | `/auth/password/reset` | `{token, new_password}` → 204, and every session ends |
| POST | `/auth/mfa/setup` | Starts enrolment: `{secret, otpauth_uri}` |
| POST | `/auth/mfa/confirm` | `{code}` turns it on and returns the recovery codes, once |
| POST | `/auth/mfa/disable` | `{password, code}` |
| POST | `/auth/mfa/recovery-codes` | `{password, code}` → a new set; the old ones stop working |

Errors are RFC 7807 as everywhere else, and the ones a frontend has to tell
apart carry a `code`: `email_not_verified`, `token_invalid`, `mfa_code_invalid`,
`mfa_token_invalid`, `password_invalid`, `mfa_required_by_role`.

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
| DELETE | `/accounts/users/{id}/mfa` | Turns a user's MFA off: lost phone *and* lost recovery codes |

`POST /accounts/users` takes `email_verified: true` when the administrator
vouches for the address; otherwise, with verification on, the user is sent the
link. Deactivating a user (`PATCH ... {is_active: false}`) ends their sessions
at once -- the access token included, not at its expiry.

---

## Email verification

```toml
[plugins]
enabled = ["observability", "database", "cache", "queue", "auth", "mail", "accounts"]

[plugin.accounts]
allow_registration = true
email_verification = "required"          # off | optional | required
frontend_url = "https://app.example.com" # where the link in the email points
```

`optional` sends the link at sign-up and the account works meanwhile;
`/auth/account` says `email_verified: false` until it is used. `required`
gives no session until then: registration answers **202** with no tokens, and
a sign-in with the right password answers **403** `email_not_verified` -- only
with the right password, so the answer tells nobody else the account exists.

The link is `frontend_url` + `verify_email_path` (`/verify-email`) +
`?token=...`. The frontend posts the token to `/auth/verify`. The token is 256
random bits, **stored only as its SHA-256**, good for
`verification_token_minutes` (a day), and single use: consuming it is one
conditional `UPDATE`, so two clicks on one link produce one success. A resend
leaves the first link working -- the first email is often the one opened.

Mail goes through the `mail` plugin, queued when `queue` is on. The three
messages -- `accounts/verify_email`, `accounts/reset_password`,
`accounts/already_registered` -- have plain built-in versions; drop
`<name>.html` (and `.txt`) into the mail `templates_dir` to replace one. They
get `app_name`, `link`, `email`, `display_name` and `expires_minutes`.

**Signing up with a taken address**, in `required` mode, answers the same 202
as a new one, and the owner of the address gets an email saying so, with a
reset link. The sign-up form never says "that email is taken". In `off` and
`optional`, registration signs the user in, so a taken address is a 409, as
before: there is no way to hand out a session and hide that.

Administrators and the bootstrap admin are verified when created (the
operator wrote the address), a provider-verified email counts, and completing
a password reset does too -- the link reached the inbox.

Enabling verification on a service with users: every existing account is
unverified. With `required`, they get the 403 and can ask for a link. To treat
the accounts that existed before as verified, run once:
`UPDATE jfast_users SET email_verified_at = created_at WHERE email_verified_at IS NULL`.

## Password reset

```toml
[plugin.accounts]
password_reset = true
frontend_url = "https://app.example.com"
```

`POST /auth/password/forgot {email}` answers **202, whatever the address** --
and at the same moment: the lookup, the token and the email all happen after
the response has been sent, so the response time says nothing about whether the
address has an account. The link is `reset_password_path` (`/reset-password`),
good for `reset_token_minutes` (30), single use, hashed at rest.

`POST /auth/password/reset {token, new_password}` checks the password policy
*before* spending the token (a password that is too short does not cost the
link), sets the password, clears a lockout, and **ends every session of the
account**: each refresh family recorded at sign-in is revoked in `auth`'s
store, which stops the access tokens that carry it too. It does not sign the
user in: that would skip the second factor.

At most one email per address per `email_cooldown_seconds` (60), however often
the form is sent.

## Two-factor authentication

```toml
[plugin.accounts]
mfa = true
mfa_required_roles = ["admin"]    # optional
```

```bash
JFAST_ENCRYPTION_KEYS=k1:...      # see encryption.md; required with mfa = true
```

TOTP (RFC 6238): six digits, thirty seconds, SHA-1 -- what every authenticator
app reads. Written against the standard library (`hmac`, `hashlib`,
`base64`), no new dependency, and tested against the RFC's own vectors.

**Enrolment** is two steps. `POST /auth/mfa/setup` (with the current password)
stores a new secret and returns it with an `otpauth://` URI; nothing is
guarded yet. `POST /auth/mfa/confirm {code}` turns it on with a code the app
computed, and returns ten **recovery codes** -- shown once, stored as SHA-256,
each good for one sign-in.

**Signing in** becomes two steps:

```
POST /auth/login       {email, password}   -> {"mfa_required": true, "mfa_token": "...", "expires_in": 300}
POST /auth/login/mfa   {mfa_token, code}   -> the token pair
```

`code` is the six digits or a recovery code. The MFA token is single use and
lasts `mfa_token_minutes` (5). A code one step either side of now is accepted
(a phone thirty seconds off still works); **the same code is never accepted
twice**, nor an older one -- the last accepted step is stored and moved forward
with a conditional `UPDATE`, so two requests racing with one code get one yes.
A wrong code counts against the MFA token (`mfa_max_attempts`, 5, then sign in
again) *and* against the account's lockout, so knowing the password buys no
extra guesses at the code: the failure count is cleared only when a session is
actually issued.

**Per role.** A user holding a role in `mfa_required_roles` and no MFA is sent
to enrol at sign-in -- `{"mfa_enrollment_required": true, "mfa_token": ...}`;
`/auth/mfa/setup` and `/confirm` accept that token in the body instead of a
session, and `confirm` then returns the session with the recovery codes. A
session that gains such a role ends at its next refresh, and MFA cannot be
turned off while the role is held.

The secret is the one credential that cannot be hashed -- the server computes
the code from it -- so it is **encrypted** with `JFAST_ENCRYPTION_KEYS`, bound
to its user. The plugin refuses to start with `mfa = true` and no key.

A provider sign-in (`auth` social login) of an account with MFA returns the
same `mfa_required` challenge instead of tokens: a provider is the first factor.

## Rate limits

With the `cache` plugin on, sign-in is rate limited without configuring
anything, in addition to the lockout:

| Bucket | Default | Applies to |
| --- | --- | --- |
| per address | 20 per 5 min (`login_limit_per_ip`) | login, the MFA step, verify, reset |
| per account | 10 per 5 min (`login_limit_per_account`) | login (keyed on the email typed), the MFA step |
| emails per address | 5 per 15 min (`email_limit_per_ip`) | register, resend, forgot |

A token bucket in Redis -- the `ratelimit` plugin's own limiter, on the cache's
connection; the `ratelimit` plugin itself need not be on. Over the limit is a
429 with `Retry-After`. The per-account bucket is keyed on what was typed, so it
answers the same for an address with no account. If Redis stops answering, it
fails open and the lockout still applies. `rate_limit = false` turns it off;
without `cache` it is off and says so at startup.

---

## What it defends against

**Guessing.** After `max_failed_logins` wrong passwords or codes (5) the
account locks for `lockout_minutes` (15). Counted per account, not per address,
so a guesser cannot get round it by changing IP; the rate limits above slow
down guessing across many accounts.

**Finding out who has an account.** A wrong password, an unknown email and a
locked account all answer the same 401 with the same message -- and take the
same time, because an unknown email is still checked against a decoy hash.
"Send me a link" (reset, resend) answers 202 before looking the address up,
and sign-up with verification required answers the same for a taken address.

**A permission that outlives its removal.** `accounts` registers `auth`'s
`on_refresh` hook, so every refresh re-reads the user's roles. Take a
permission away, or deactivate the user, and it applies at their next refresh
-- deactivation ends the session there -- instead of at the end of a
thirty-day refresh token. The access token already issued keeps working until
it expires (`access_lifetime_minutes`, 15), which is the reason to keep that
short -- except on deactivation, a password reset and `/auth/logout/all`, which
revoke every session at once, access tokens included.

**A stolen table of tokens.** Verification and reset tokens and recovery codes
are stored as SHA-256; the TOTP secret is encrypted. A copy of the database
resets nobody's password. None of them is ever logged.

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
| `email_verification` | `off` | `off`, `optional` or `required`. Needs `mail` |
| `verification_token_minutes` | `1440` | |
| `password_reset` | `false` | Needs `mail` |
| `reset_token_minutes` | `30` | |
| `frontend_url` | `""` | Required when either of the two above is on (`JFAST_ACCOUNTS_FRONTEND_URL`) |
| `verify_email_path` / `reset_password_path` / `login_path` | `/verify-email` / `/reset-password` / `/login` | The frontend routes the links point at |
| `email_cooldown_seconds` | `60` | One email of a kind per address per this long |
| `mfa` | `false` | Needs `JFAST_ENCRYPTION_KEYS` |
| `mfa_required_roles` | `[]` | Needs `mfa = true` |
| `mfa_issuer` | the app name | What the authenticator app shows |
| `mfa_token_minutes` / `mfa_max_attempts` | `5` / `5` | The second step of a sign-in |
| `recovery_codes` | `10` | |
| `rate_limit` | `true` | Only acts with the `cache` plugin |
| `login_limit_per_ip` / `login_limit_per_account` / `login_window_seconds` | `20` / `10` / `300` | |
| `email_limit_per_ip` / `email_window_seconds` | `5` / `900` | |

Every combination that cannot work stops the service at startup, naming the
fix: verification or reset without `mail` or without `frontend_url`,
`mfa_required_roles` without `mfa`, `mfa` without an encryption key.

The tables (`jfast_users`, `jfast_roles`, `jfast_role_permissions`,
`jfast_user_roles`, `jfast_account_tokens`, `jfast_recovery_codes`,
`jfast_user_sessions`) are the plugin's: created at startup and skipped by the
service's Alembic autogenerate. A `jfast_users` created by an earlier release
gains its new columns at startup too (all nullable, so the `ALTER` is instant):
no migration to write.

## What is not here

- **Wildcard permissions.** `invoices:*` is not a thing; list what a role may do.
- **Passkeys (WebAuthn), SMS or email codes.** The second factor is TOTP.
- **A QR code.** `/auth/mfa/setup` returns the `otpauth://` URI; drawing it is
  the frontend's choice (the generated one shows the link and the key).
- **Changing the password does not end other sessions.** A reset does, and so
  does `/auth/logout/all`.
- **The MFA step after a provider sign-in, in the generated frontend.** The
  backend returns the challenge; the frontend's provider callback does not
  handle it yet.
