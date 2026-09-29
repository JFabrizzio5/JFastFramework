"""Error model shared by every JFast service.

All failures serialise to RFC 7807 ``application/problem+json`` so clients and
sibling services parse one shape, not one shape per team.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"


class JFastError(Exception):
    """Base class for framework and domain errors."""

    status_code: int = 500
    title: str = "Internal Server Error"
    type_: str = "about:blank"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": self.type_,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotFoundError(JFastError):
    status_code = 404
    title = "Not Found"


class ConflictError(JFastError):
    status_code = 409
    title = "Conflict"


class PreconditionFailedError(JFastError):
    """The client wrote against a version of the row that is no longer current.

    412 rather than 409: the request carried a precondition -- the version it
    read -- and that precondition is what failed. The client's remedy is to
    read again and decide, which is different from a conflict it can fix by
    changing the payload.
    """

    status_code = 412
    title = "Precondition Failed"


class ValidationError(JFastError):
    status_code = 422
    title = "Unprocessable Entity"


class UnauthorizedError(JFastError):
    status_code = 401
    title = "Unauthorized"


class ForbiddenError(JFastError):
    status_code = 403
    title = "Forbidden"


class ServiceUnavailableError(JFastError):
    status_code = 503
    title = "Service Unavailable"


class PluginError(JFastError):
    """Configuration-time failure in the plugin graph."""

    title = "Plugin Error"


def _problem_response(problem: dict[str, Any], request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        problem.setdefault("request_id", request_id)
    return JSONResponse(
        status_code=int(problem["status"]),
        content=problem,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def problem_response(exc: JFastError, request: Request) -> JSONResponse:
    """Render an error as problem+json, without an exception handler.

    Middleware runs *outside* the exception handlers, so an error raised there
    would escape as a 500 with a stack trace instead of the documented shape.
    Anything raising from middleware returns this instead.
    """
    return _problem_response(exc.to_problem(instance=str(request.url.path)), request)


def _serialisable(value: Any) -> Any:
    """Coerce anything json cannot encode into something it can.

    Only ever applied to error detail, where the alternative is worse than an
    imperfect rendering: an error response that raises while being written
    replaces the diagnosis with a stack trace from the JSON encoder, and the
    status the client sees is 500 rather than the 422 that was meant.

    Bytes decode when they are text and become a short marker when they are
    not, so a binary upload does not put a megabyte of latin-1 in a log line.
    """
    if isinstance(value, dict):
        return {str(key): _serialisable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialisable(item) for item in value]
    if isinstance(value, bytes | bytearray):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(value)} bytes>"
        return text if len(text) <= 512 else text[:512] + "..."
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def install_error_handlers(app: FastAPI, *, debug: bool = False) -> None:
    """Register the problem+json handlers on an app."""

    @app.exception_handler(JFastError)
    async def _jfast_error(request: Request, exc: JFastError) -> JSONResponse:
        return _problem_response(exc.to_problem(instance=str(request.url.path)), request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": exc.detail if isinstance(exc.detail, str) else "HTTP Error",
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": str(request.url.path),
        }
        return _problem_response(problem, request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": "Request validation failed",
            "instance": str(request.url.path),
            # Sanitised, because pydantic puts the offending value in `input`
            # and that value is whatever arrived. A form posted without a
            # content type puts the raw body there as `bytes`, which the JSON
            # encoder cannot represent -- so serialising the raw 422 would
            # raise inside the handler, and the client would get a 500 with a
            # traceback about json.dumps instead of the field name it needs.
            "errors": _serialisable(exc.errors()),
        }
        return _problem_response(problem, request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            # Never leak internals in production; debug builds get the message.
            "detail": str(exc) if debug else "An unexpected error occurred",
            "instance": str(request.url.path),
        }
        return _problem_response(problem, request)
