# Authentication

JWT verification, scopes, key rotation and revocation.

```toml
[plugins]
enabled = ["observability", "cache", "auth"]

[plugin.auth]
mode = "jwks"
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
from fastapi import Depends
from jfastframework.auth import Principal, require_scopes

@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

---

## What this does and does not do

It **verifies** tokens, and it can **mint** them. It has **no login endpoint**,
because checking a password against your user table is your application's job.
`auth.issuer` is provided for your own login route:

```python
issuer = request.app.state.jfast.require("auth.issuer")
pair = await issuer.issue_pair(user.id, scopes=user.scopes, tenant_id=user.tenant_id)
```

A framework that shipped a `/auth/login` would have to invent a user model,
a password hashing policy and a lockout strategy — and you would fight all
three.

---

## Choosing a mode

| Mode | Key | Use when |
| --- | --- | --- |
| `jwks` | fetched from the issuer | more than one service. **The default.** |
| `public_key` | a pinned PEM | one issuer, no network dependency wanted |
| `secret` | shared HMAC secret | a single service that also mints |

**`secret` does not belong between services.** Everything that can verify an
HMAC token can also mint one. A read-only reporting service holding that secret
can forge an admin token for the billing service. `jwks` and `public_key` split
that: the issuer holds the private key, everyone else holds a public one.

---

## The attacks the defaults refuse

These are checked in `tests/test_auth.py`, one test each.

**Algorithm confusion.** A service that trusts the token's own `alg` header can
be handed an HS256 token signed with the RSA *public key it publishes* as the
HMAC secret — and will verify it. The algorithms come from configuration and
are passed explicitly to the decoder. Configuring both families at once is
refused outright:

```
auth.algorithms mixes symmetric and asymmetric algorithms (HS256, RS256).
Allowing both lets a token signed with the public key as an HMAC secret verify.
Pick one family.
```

**`alg: none`.** Ruled out by the same allow-list. It is never in
`SUPPORTED_ALGORITHMS` and must not be added.

**Cross-service token reuse.** `aud` and `iss` are verified. Both are off by
default in most libraries, and without them a valid token for a *different*
service of the same issuer is accepted here — which is how a compromised
low-value service becomes access to a high-value one. Leaving `audience` empty
logs a warning at startup rather than silently accepting everything.

**A generous clock skew.** The leeway is 30 seconds. Five minutes of leeway is
five extra minutes of life for a stolen token.

**JWKS refresh amplification.** An unknown `kid` triggers a refresh — that is
how rotation is picked up — but at most once a minute. Without the floor, a
stream of forged `kid`s becomes a denial-of-service against your identity
provider.

**Leaking why a token failed.** The reason goes to the log; the client gets a
plain 401. Telling an attacker *which* check failed is free reconnaissance.

---

## Tenancy stops being forgeable

Before this plugin, `tenant_id` comes from the `X-Tenant-ID` header —
convenient in development, and settable by anyone with curl. With auth enabled
it comes from a **signed claim**, and the header is ignored.

That is the main security reason to turn this on, more than the login form.

---

## 401 versus 403

- **401** — I do not know who you are. No token, or an invalid one.
- **403** — I do, and you may not. Authenticated, missing a scope or role.

Collapsing them makes every permissions bug guesswork. `require_scopes` names
the missing scopes in the response, because that one is not a secret from a
caller who is already authenticated.

```python
require_auth                       # any verified caller
require_scopes("a", "b")           # all of these scopes
require_roles("admin", "owner")    # any one of these roles
optional_auth                      # Principal | None, for mixed routes
```

They are `async def` and meant for `Depends(...)`. That is not style: FastAPI
runs a plain `def` dependency in its threadpool, and the hop cost 75-85 us per
request -- more than the whole middleware stack ([Performance](deploy.md#performance)).
In plain code, outside a dependency, use the synchronous `principal_of(request)`,
which raises the same 401, or read `request.state.principal`.

---

## Revocation

JWTs are stateless, which is the point and also the problem: a token is valid
until it expires and "log out" has nothing to act on. The answer is short
access-token lifetimes plus a small amount of state.

`POST /auth/logout` revokes the caller's `jti` **and the session family that
token carries** — otherwise the refresh token issued alongside it quietly mints
a new session.

The family comes from the access token's own `fam` claim, so a logout ends that
session and nothing else: the same person's other devices keep working, and so
does their next login. A token minted elsewhere — an external IdP in `jwks`
mode — carries no `fam`, and there revoking the `jti` is all a logout can
honestly do.

Revocation entries carry the token's own remaining lifetime as a TTL: past
expiry the signature check rejects it anyway, so keeping the entry longer only
grows the store forever.

With the `cache` plugin enabled the store is Redis and a logout applies to
every replica. Without it the store is a dict, and `/ready` says so:

```
in-memory token store: revocation does not survive a restart or reach other replicas
```

Non-critical — the service still authenticates — but visible, rather than
discovered from a support ticket.

When Redis stops answering, the revocation lookup fails **open** by default:
the token is accepted without the check, a warning is logged (at most every ten
seconds) and `/ready` reports auth degraded. It is the trade the rate limiter
makes, bounded by the access-token lifetime. Where a logout that takes a few
minutes to bite is worse than an outage, close it:

```toml
[plugin.auth]
revocation_fail_open = false   # every authenticated request answers 503 until Redis is back
```

Either way the lookup is bounded: the cache client gives each command one
second and stops calling a Redis that is not answering (see
[Resilience](resilience.md)).

---

## Refresh rotation, with reuse detection

Every refresh returns a new refresh token and invalidates the one presented.
Presenting a used one means either a client retry or a replayed stolen token,
and from the server those are indistinguishable — so the whole **family** is
revoked and the user logs in again.

Losing one session is a far smaller cost than not noticing a theft. The one
exception is the grace window below, and it exists because one case *is*
distinguishable.

**A family is one session, not one person.** `issue_pair` mints a random family
per login, and both tokens of the pair carry it as `fam`. Keying it on the
subject instead would make one revocation reach every device that person has —
and the next login too, for the whole refresh lifetime.

```python
pair = await issuer.issue_pair("user-1", scopes=["invoices:read"], tenant_id="acme")
# POST /auth/refresh {"refresh_token": ...} -> a new pair
```

**The new access token keeps the rights the old one had.** The refresh token
carries the grant it was issued with under `grt`, a claim of this issuer's own —
never the configured scope claim, so `verify()` cannot read it back as
authorization. A refresh token *carries* a grant; it does not *hold* one, and
`principal.scopes` on one is empty.

That containment is why a refresh token is also refused as a bearer token. It
verifies like any other — same key, same issuer, same audience — so without an
explicit `typ` check a 30-day token would open a session anywhere an access
token would.

To re-read the caller's rights at every refresh instead of carrying them
forward — a permission taken away today should not survive in a token minted
yesterday — register a hook:

```python
from jfastframework.auth import Grant

@auth.on_refresh
async def rights(principal):
    user = await users.get(principal.subject)
    return Grant(scopes=tuple(user.scopes)) if user.active else None
```

Returning `None` **revokes the session family**, not merely this request. The
access token already in the client's hands carries the same `fam`, so it stops
verifying at once instead of running out its remaining lifetime — a decline that
only refused the next refresh would leave a banned user working for another
fifteen minutes.

**The hook runs before the presented token is consumed.** A hook reads your
database, and a database that blinks for one request must not cost the session:
with the token already spent, the client's natural retry looks like a replay and
takes the whole family with it. Nothing is consumed when the hook raises, so
that retry is just a retry, and the 500 the caller sees is honest and safe to
repeat.

The price of that ordering is one hook call for a genuinely replayed token
before the consume refuses it. Once only — that consume revokes the family, and
the check at the top of `rotate` turns every later attempt away first.

A refresh token minted before `grt` existed (`0.1.0a3` and earlier) is refused
with a 401 rather than rotated into an access token with no scopes at all: the
403 that would follow lands nowhere near the cause.

### Two tabs are not a theft

Two simultaneous refreshes of the same token returned `[200, 401]` **and ended
the session**: the loser's attempt tripped reuse detection, so the winner's
brand-new pair was revoked on arrival. A browser with two tabs does exactly
this.

For `refresh_grace_seconds` after a rotation, the token it replaced is refused
without revoking anything:

```toml
[plugin.auth]
refresh_grace_seconds = 10   # 0 for strict reuse detection
```

The loser still gets a 401 — there is one live refresh token and the winner has
it — but the session survives and the winner's pair works.

**This narrows reuse detection, and that is the trade.** A stolen refresh token
replayed *inside* the window is not detected as a replay. It gains the thief
nothing directly: the grace declines to revoke, it does not mint, and the reply
is the same 401. What it costs is the certainty that a reused token is always
noticed, in exchange for a normal browser no longer ending its own session.
Longer than a request round trip buys nothing; `0` restores the strict rule.

Outside the window nothing has changed — a replay revokes the family for the
full refresh lifetime.

The window is enforced by the store, not by the caller. `RedisTokenStore` runs
the consume and the "just rotated" mark as **one Lua script**, because a
`DELETE` followed by a second question has a gap in it: the loser can read the
mark before the winner has written it and report its own race as a theft. The
mark is written only by the request that won the `DELETE`, so a replay can read
it but never create or extend it, and the window closes on schedule however
often the token is presented.

`TokenStore.rotate_refresh` therefore answers `rotated` / `raced` / `replayed`
rather than a bool. A custom store must implement all three; returning
`replayed` where it means `raced` is the old behaviour, which is a working
default rather than a silent hole.

---

## Key rotation

With `mode = "jwks"` rotation is a publish, not a redeploy. The issuer adds a
new key to its JWKS document and starts signing with it; services fetch the new
key on the first token carrying an unknown `kid`.

Keep the old key published until every token signed with it has expired.

If the JWKS endpoint is unreachable, cached keys keep working — a JWKS outage
must not take every service down — and `/ready` reports the staleness as
*degraded*, not unavailable: every replica still verifies what it verified a
minute ago. With no key fetched at all it is critical, because nothing can be
verified.

Reaching the cached keys must not cost a timeout first, so the fetch has a
deadline, one retry for what looks transient, and a breaker:

```toml
[plugin.auth]
jwks_timeout = 5.0             # seconds for one fetch, the whole of it
jwks_attempts = 2              # the first try included; only 429/5xx/network errors retry
jwks_breaker_failures = 3      # failed refreshes in a row that stop calling the issuer...
jwks_breaker_cool_down = 30.0  # ...for this long, while cached keys are served
```

Measured with an issuer that hangs: the first three requests after the cache
expires wait about a second each (two tries of 0.5 s in the drill), and every
request after that answers in under a millisecond until the issuer is back.
Callers that queue behind a fetch that failed take its failure instead of
asking again, so an outage costs the issuer one request, not one per caller.

---

## Sign in with Google

```toml
[plugin.auth.providers.google]
client_id = "...apps.googleusercontent.com"
client_secret = "${GOOGLE_CLIENT_SECRET}"
redirect_uri = "https://app.example.com/auth/google/callback"
```

`google`, `microsoft` and `github` need nothing else. Any other name must also
give `issuer`, `jwks_uri`, `authorization_endpoint` and `token_endpoint`.

Two routes appear:

| Route | Does |
| --- | --- |
| `GET /auth/google/start` | redirects to Google, sets a state/nonce cookie |
| `GET /auth/google/callback` | verifies everything, then calls your handler |

The handler is yours, because only you know what a user is here:

```python
auth = app.state.jfast.require("auth")

@auth.on_identity
async def sign_in(identity, request):
    user = await users.upsert_federated(identity.federated_id, identity.email)
    return auth.issuer.issue(subject=str(user.id), scopes=user.scopes)
```

Without a registered handler the callback returns a 500 saying so. A verified
user and nowhere to put them is a configuration error, and a cheerful 200 would
hide it.

### Why you mint your own token

A Google ID token says "Google believes this is person@example.com". It does
not say what they may do in your system, it expires on Google's schedule, and
you cannot revoke it. Exchanging it for your own token is what puts scopes,
your tenant and your revocation back under your control.

### What is checked, and what each check stops

| Check | Without it |
| --- | --- |
| `aud == client_id` | a token minted for *anyone else's* Google app logs in here |
| `iss == provider` | a token from a different issuer entirely is accepted |
| `state` matches the cookie | login CSRF: a code obtained in the attacker's browser, replayed |
| `nonce` inside the token | a captured ID token replayed into a fresh login |
| signature, via the provider's JWKS | the usual |

The state cookie is `httponly` (script cannot read it), `samesite=lax` (it
survives Google's top-level redirect back but not a cross-site POST) and
`secure` outside development.

### Two traps

**Never match an existing account on an unverified email.** `email_verified` is
carried on `OIDCIdentity` for exactly this. A provider that lets a user set any
email without proving it, matched against your user table, is account takeover
in one step.

**Store `identity.federated_id`, not `identity.subject`.** Subject ids are
unique per provider, not globally. GitHub user `42` and Google user `42` are
different people, and the bare subject cannot tell you which.

GitHub is OAuth2, not OIDC: there is no ID token, so `verify_id_token()`
refuses it and the userinfo call is the only way to learn who signed in. It is
listed for completeness; that call is yours to make.

Install: `pip install jfastframework[oidc]`.

---

## Checklist before production

- [ ] `mode = "jwks"` or `public_key`, not `secret`, if more than one service
- [ ] `audience` set to this service, `issuer` set to your identity provider
- [ ] `cache` enabled, so revocation is shared across replicas
- [ ] Access-token lifetime in minutes, not hours
- [ ] `JFAST_AUTH_SECRET` (if used) is 32+ random bytes, from a secret manager
- [ ] Tokens never logged. `Principal.describe()` is the safe shape

## What is not here

- **A user store, password login, roles.** Not in this plugin, on purpose: it
  verifies and mints tokens and nothing else. The [accounts](accounts.md)
  plugin is the user store, and registers this plugin's `on_refresh` and
  `on_identity` hooks for you. MFA is in neither.
- **PKCE.** The authorization-code flow here is the confidential-client one,
  run from your backend with a client secret. A public client (a mobile app
  talking to Google directly) needs PKCE, which is not implemented.
- **mTLS or service-to-service identity.** Machine tokens work today; SPIFFE
  is not implemented.
- **Per-tenant key isolation.** One issuer, one key set.
