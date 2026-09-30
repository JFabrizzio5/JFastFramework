"""Trace context that crosses boundaries: jobs, events, HTTP calls.

The contract every part of the framework codes against, whether or not
OpenTelemetry is installed. Without the ``telemetry`` plugin every function
here is a no-op that costs a function call; with it, the plugin installs a
real backend through :func:`set_backend` and the same call sites start
producing spans and carrying W3C ``traceparent``/``tracestate``.

Three uses, and the code that owns each:

* **Carry** -- a ``Job`` or an ``Event`` built inside a request stores
  ``inject()`` next to its tenant and request id; the worker runs the handler
  inside ``attach(carrier)``, so its spans join the request's trace instead of
  starting an orphan one.
* **Propagate** -- the ``http`` client adds ``inject()`` to outgoing headers and
  the gateway forwards them, so one trace spans every service.
* **Measure** -- ``with span("llm.chat", model=...):`` around work worth seeing.

Rules every backend keeps: attributes are small scalars (ids, names, counts,
durations), **never prompt text, documents, answers or request bodies**; and
nothing here raises -- telemetry that breaks a request is worse than none.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from typing import Any, Protocol

__all__ = [
    "TracingBackend",
    "attach",
    "enabled",
    "inject",
    "reset_backend",
    "set_backend",
    "span",
]


class TracingBackend(Protocol):
    def inject(self) -> dict[str, str]:
        """The current trace context as W3C headers; {} outside any trace."""
        ...

    def attach(self, carrier: Mapping[str, str]) -> AbstractContextManager[None]:
        """Make ``carrier`` the parent of whatever runs inside the block."""
        ...

    def span(self, name: str, attributes: Mapping[str, Any]) -> AbstractContextManager[None]:
        """A span around the block. Exceptions are recorded and re-raised."""
        ...


class _NoOp:
    def inject(self) -> dict[str, str]:
        return {}

    def attach(self, carrier: Mapping[str, str]) -> AbstractContextManager[None]:
        return nullcontext()

    def span(self, name: str, attributes: Mapping[str, Any]) -> AbstractContextManager[None]:
        return nullcontext()


_NOOP = _NoOp()
_backend: TracingBackend = _NOOP


def set_backend(backend: TracingBackend) -> None:
    """Called by the telemetry plugin at startup. Process-wide."""
    global _backend
    _backend = backend


def reset_backend() -> None:
    """Back to the no-op backend. For tests and plugin shutdown."""
    global _backend
    _backend = _NOOP


def enabled() -> bool:
    return _backend is not _NOOP


def inject() -> dict[str, str]:
    try:
        return dict(_backend.inject())
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        return {}


@contextmanager
def attach(carrier: Mapping[str, str] | None) -> Iterator[None]:
    if not carrier:
        yield
        return
    try:
        manager = _backend.attach(carrier)
    except Exception:  # noqa: BLE001
        manager = nullcontext()
    with manager:
        yield


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    try:
        manager = _backend.span(name, attributes)
    except Exception:  # noqa: BLE001
        manager = nullcontext()
    with manager:
        yield


def annotate(**attributes: Any) -> None:
    """Add attributes to the current span, for values known only at its end.

    Framework-internal, and deliberately not in ``__all__``: the token counts
    of a model call or the status of an upstream answer arrive after
    :func:`span` opened, and this is how the call site records them. A backend
    without an ``annotate`` method ignores it; without a backend it returns at
    once. The same rule as everywhere here: small scalars, never content.
    """
    backend = _backend
    if backend is _NOOP:
        return
    # Telemetry must never break the caller -- not even a backend whose
    # attribute lookup itself fails.
    with suppress(Exception):
        record = getattr(backend, "annotate", None)
        if record is not None:
            record(attributes)
