"""Idempotency keys: a retried POST returns the first answer instead of acting twice.

A client sends ``POST /payments``, the connection drops before the response
arrives, and the client retries. Without a key the server cannot tell a retry
from a second payment. With one, the first request records the key in the same
transaction as the payment, and every retry with that key gets the recorded
response back::

    from jfastframework.idempotency import IdempotencyKey

    @router.post("/payments", status_code=201)
    async def pay(payload: PaymentIn, session: DbSession, key: IdempotencyKey):
        ...

The rules, each for a reason:

* **The key is written in the request's transaction.** If the request rolls
  back, the key goes with it and a retry runs from scratch; if it commits, the
  key commits with the payment. The two cannot disagree.
* **Same key, different body: 422.** A key names one operation. Reusing it
  for another is a client bug, and replaying the first answer would hide it.
* **Same key while the first is still running: 409.** PostgreSQL makes the
  second insert wait for the first transaction, so the two never both run.
* **Keys are per tenant**, and expire after ``ttl_hours``.

The response is recorded after it has been sent, in its own short
transaction. A process that dies in that gap leaves the key "in progress" with
the work committed: retries get 409 rather than a second payment, which is the
side to fail on.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Table,
    delete,
    func,
    select,
    update,
)
from starlette.responses import Response

from jfastframework.db.base import UTCDateTime
from jfastframework.db.framework import (
    JSONType,
    dialect_of,
    framework_metadata,
    insert_ignoring_conflicts,
)
from jfastframework.errors import ConflictError, ValidationError
from jfastframework.plugins.builtin.database import DbSession

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "IDEMPOTENCY_TABLE",
    "IdempotencyKey",
    "IdempotencyRecorder",
    "IdempotentReplay",
    "RequiredIdempotencyKey",
    "idempotency",
]

logger = logging.getLogger("jfast.idempotency")

IDEMPOTENCY_TABLE = "jfast_idempotency"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
STATE_KEY = "jfast_idempotency"

idempotency = Table(
    IDEMPOTENCY_TABLE,
    framework_metadata,
    # '' rather than NULL for "no tenant": a NULL in a primary key is not
    # allowed, and a key must still be unique among untenanted requests.
    Column("tenant", String(255), nullable=False, default=""),
    Column("key", String(255), nullable=False),
    Column("method", String(10), nullable=False),
    Column("path", String(2048), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("response_status", Integer),
    Column("response_headers", JSONType),
    Column("response_body", LargeBinary),
    Column("replayable", Boolean, nullable=False, default=True),
    Column("created_at", UTCDateTime, nullable=False, server_default=func.now()),
    Column("completed_at", UTCDateTime),
    PrimaryKeyConstraint("tenant", "key"),
)
Index("ix_jfast_idempotency_created_at", idempotency.c.created_at)


class IdempotentReplay(Exception):
    """Raised by the dependency to answer with a recorded response."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        super().__init__("idempotent replay")
        self.status = status
        self.headers = headers
        self.body = body

    def response(self) -> Response:
        headers = dict(self.headers)
        headers["Idempotent-Replayed"] = "true"
        return Response(content=self.body, status_code=self.status, headers=headers)


def _settings(request: Request) -> Any:
    from jfastframework.app import get_context

    return get_context(request.app).require("idempotency.settings")


def _valid(key: str) -> bool:
    return 0 < len(key) <= 255 and all(33 <= ord(c) <= 126 for c in key)


async def _fingerprint(request: Request) -> str:
    digest = hashlib.sha256()
    digest.update(request.method.encode())
    digest.update(b"\n")
    digest.update(request.url.path.encode())
    digest.update(b"?")
    digest.update(request.url.query.encode())
    digest.update(b"\n")
    digest.update(await request.body())
    return digest.hexdigest()


async def _claim(request: Request, session: Any, *, required: bool) -> str | None:
    settings = _settings(request)
    key = request.headers.get(settings.header)
    if key is None:
        if required:
            raise ValidationError(f"this operation needs an {settings.header} header")
        return None
    key = key.strip()
    if not _valid(key):
        raise ValidationError(
            f"{settings.header} must be 1-255 printable characters with no spaces"
        )

    tenant = str(getattr(request.state, "tenant_id", None) or "")
    fingerprint = await _fingerprint(request)
    now = datetime.now(UTC)
    values = {
        "tenant": tenant,
        "key": key,
        "method": request.method,
        "path": request.url.path[:2048],
        "request_hash": fingerprint,
        "status": IN_PROGRESS,
        "replayable": True,
    }
    where = (idempotency.c.tenant == tenant) & (idempotency.c.key == key)

    for _ in range(2):
        inserted = await session.execute(
            insert_ignoring_conflicts(dialect_of(session), idempotency).values(**values)
        )
        if inserted.rowcount == 1:
            request.state.jfast_idempotency = (tenant, key)
            return key

        row = (await session.execute(select(idempotency).where(where))).mappings().first()
        if row is None:
            continue  # it expired or rolled back between the two statements
        created = row["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < now - timedelta(hours=settings.ttl_hours):
            await session.execute(delete(idempotency).where(where))
            continue
        if row["request_hash"] != fingerprint:
            raise ValidationError(
                f"{settings.header} {key!r} was already used for a different request"
            )
        if row["status"] != COMPLETED:
            raise ConflictError(f"a request with {settings.header} {key!r} is still in progress")
        if not row["replayable"]:
            raise ConflictError(
                f"the request with {settings.header} {key!r} completed, but its response "
                f"was too large to keep; read the resource instead"
            )
        raise IdempotentReplay(
            int(row["response_status"]),
            dict(row["response_headers"] or {}),
            bytes(row["response_body"] or b""),
        )
    raise ConflictError(f"could not record {settings.header} {key!r}; retry")


async def _optional_key(request: Request, session: DbSession) -> str | None:
    return await _claim(request, session, required=False)


async def _required_key(request: Request, session: DbSession) -> str | None:
    return await _claim(request, session, required=True)


#: The ``Idempotency-Key`` header, honoured when the client sends one.
IdempotencyKey = Annotated[str | None, Depends(_optional_key)]
#: The same, and a request without one is refused with 422.
RequiredIdempotencyKey = Annotated[str | None, Depends(_required_key)]


class IdempotencyRecorder:
    """ASGI middleware: stores the response of a request that claimed a key.

    Pure ASGI rather than ``BaseHTTPMiddleware``, so the body is captured as it
    streams out instead of being buffered a second time by Starlette.
    """

    def __init__(self, app: ASGIApp, *, engine: Any, max_body_bytes: int) -> None:
        self.app = app
        self._engine = engine
        self._limit = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        captured: dict[str, Any] = {"status": 0, "headers": {}, "body": bytearray(), "big": False}

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = {
                    k.decode("latin-1"): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                    if k.lower() in (b"content-type", b"location")
                }
            elif message["type"] == "http.response.body" and not captured["big"]:
                captured["body"] += message.get("body", b"")
                if len(captured["body"]) > self._limit:
                    captured["big"] = True
                    captured["body"] = bytearray()
            await send(message)

        await self.app(scope, receive, recording_send)

        claimed = scope.get("state", {}).get(STATE_KEY)
        if not claimed:
            return
        tenant, key = claimed
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(idempotency)
                    .where((idempotency.c.tenant == tenant) & (idempotency.c.key == key))
                    .values(
                        status=COMPLETED,
                        response_status=captured["status"],
                        response_headers=json.loads(json.dumps(captured["headers"])),
                        response_body=None if captured["big"] else bytes(captured["body"]),
                        replayable=not captured["big"],
                        completed_at=datetime.now(UTC),
                    )
                )
        except Exception:
            # The response has already gone out; all that is lost is the
            # replay, and a retry will get 409 until the key expires.
            logger.exception("could not record an idempotent response", extra={"key": key})


async def purge_expired(engine: Any, *, ttl: timedelta) -> int:
    cutoff = datetime.now(UTC) - ttl
    async with engine.begin() as conn:
        result = await conn.execute(delete(idempotency).where(idempotency.c.created_at < cutoff))
    return int(result.rowcount or 0)
