"""Verifying and issuing JWTs.

The verification path is where the security actually lives, so the choices
are spelled out rather than left to library defaults:

**Algorithms are pinned by configuration, never read from the token.** The
classic JWT attack is algorithm confusion: a service that trusts the token's
own ``alg`` header can be handed an HS256 token signed with the RSA *public*
key it publishes, and it will happily verify it. Passing an explicit allow-list
to ``jwt.decode`` is what prevents that, and it also rules out ``alg: none``.

**Audience and issuer are verified.** Both are off by default in most
libraries. Without them, a valid token minted for a different service of the
same issuer is accepted here -- which is how one compromised low-value service
becomes access to a high-value one.

**Expiry has a small leeway, not a large one.** Clocks drift by seconds. A
five-minute leeway is five extra minutes of life for a stolen token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jfastframework.auth.principal import Principal

# Algorithms this framework will verify. `none` is absent on purpose and must
# never be added: it makes every unsigned token valid.
SUPPORTED_ALGORITHMS = frozenset(
    {"HS256", "HS384", "HS512", "RS256", "RS384", "RS512", "ES256", "ES384"}
)
SYMMETRIC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


class TokenError(Exception):
    """The token could not be verified. The reason is safe to log, not to return."""


@dataclass(frozen=True)
class TokenClaims:
    """Where to find things in this issuer's tokens."""

    scopes: str = "scope"
    roles: str = "roles"
    tenant: str = "tenant_id"


def _as_set(value: Any) -> frozenset[str]:
    """Claims arrive as a space-delimited string or a list, depending on issuer."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.split() if part)
    if isinstance(value, list | tuple | set):
        return frozenset(str(item) for item in value)
    return frozenset()


def verify(
    token: str,
    *,
    key: Any,
    algorithms: list[str],
    audience: str | None = None,
    issuer: str | None = None,
    leeway: int = 30,
    claims: TokenClaims | None = None,
    require_subject: bool = True,
) -> Principal:
    """Verify a token and return the caller it identifies.

    Raises ``TokenError`` for anything that fails, with a reason that belongs
    in the log. What reaches the client is a plain 401: telling an attacker
    *why* their token was rejected is free reconnaissance.
    """
    import jwt

    unsupported = set(algorithms) - SUPPORTED_ALGORITHMS
    if unsupported:
        raise TokenError(f"unsupported algorithm(s) configured: {', '.join(sorted(unsupported))}")
    if not algorithms:
        raise TokenError("no algorithms configured; every token would be rejected")

    names = claims or TokenClaims()
    options = {
        "require": ["exp", "iat", *(["sub"] if require_subject else [])],
        "verify_aud": audience is not None,
        "verify_iss": issuer is not None,
        "verify_signature": True,
        "verify_exp": True,
        "verify_nbf": True,
    }

    try:
        payload = jwt.decode(
            token,
            key,
            # Pinned. Never `jwt.get_unverified_header(token)["alg"]`.
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenError("token was issued for a different audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenError("token was issued by an unexpected issuer") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenError(f"token is missing a required claim: {exc}") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"token is invalid: {exc}") from exc

    expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC) if payload.get("exp") else None
    return Principal(
        subject=str(payload.get("sub", "")),
        scopes=_as_set(payload.get(names.scopes)),
        roles=_as_set(payload.get(names.roles)),
        # From a signed claim, never from a header.
        tenant_id=payload.get(names.tenant),
        token_id=payload.get("jti"),
        issuer=payload.get("iss"),
        expires_at=expires_at,
        claims=payload,
    )


def issue(
    subject: str,
    *,
    key: Any,
    algorithm: str = "HS256",
    lifetime: timedelta,
    audience: str | None = None,
    issuer: str | None = None,
    scopes: list[str] | None = None,
    roles: list[str] | None = None,
    tenant_id: str | None = None,
    token_type: str = "access",
    extra: dict[str, Any] | None = None,
    claims: TokenClaims | None = None,
) -> tuple[str, str, datetime]:
    """Mint a token. Returns ``(encoded, jti, expires_at)``.

    Every token gets a ``jti``: without one, revoking anything at all means
    rotating the signing key, which ends every session of every user at once.
    Signing out one *person* everywhere is not offered here: it needs a
    subject-level cursor the ``TokenStore`` has no room for yet.

    ``token_type`` is checked on the way back in. Without it a refresh token is
    a perfectly valid access token, and its whole point is that it is longer
    lived.
    """
    import jwt

    if algorithm not in SUPPORTED_ALGORITHMS:
        raise TokenError(f"unsupported algorithm {algorithm!r}")

    now = datetime.now(UTC)
    token_id = uuid.uuid4().hex
    names = claims or TokenClaims()

    payload: dict[str, Any] = {
        "sub": subject,
        "jti": token_id,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "typ": token_type,
    }
    if audience:
        payload["aud"] = audience
    if issuer:
        payload["iss"] = issuer
    if scopes:
        payload[names.scopes] = " ".join(scopes)
    if roles:
        payload[names.roles] = list(roles)
    if tenant_id:
        payload[names.tenant] = tenant_id
    if extra:
        # Reserved claims are the framework's; an override here would let a
        # caller widen its own token.
        reserved = set(payload) & set(extra)
        if reserved:
            raise TokenError(f"cannot override reserved claim(s): {', '.join(sorted(reserved))}")
        payload.update(extra)

    encoded = jwt.encode(payload, key, algorithm=algorithm)
    return encoded, token_id, datetime.fromtimestamp(payload["exp"], tz=UTC)


def token_type_of(principal: Principal) -> str:
    return str(principal.claims.get("typ", "access"))
