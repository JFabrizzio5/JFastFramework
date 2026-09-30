"""Run the suite on the clock Windows gives asyncio, on any OS.

Opt-in: ``JFAST_TEST_WINDOWS_CLOCK=1 pytest ...``. Unset, this module does
nothing at all.

Why it exists: the JWKS single-flight bug (fixed in 5b0ef2d) passed on macOS
and Linux for months and failed on the Windows runner. On Windows before
Python 3.13, ``time.monotonic()`` is ``GetTickCount64()`` and moves in
15.625 ms steps (1/64 s). That does two things a fine-grained clock hides:

- Two readings a few milliseconds apart are *equal*, so code that compares
  them to decide "did anything happen since" decides "no".
- asyncio reads ``time.get_clock_info("monotonic").resolution`` when a loop
  is created (``BaseEventLoop._clock_resolution``) and, on every iteration,
  runs every timer due before ``now + resolution``. A ``sleep(0.01)`` can
  therefore return *before* the clock has moved, and timers fire up to one
  step early and in batches.

Both are reproduced here: ``time.monotonic`` and ``time.monotonic_ns`` are
floored to the step, and ``get_clock_info("monotonic")`` reports it, so every
loop created afterwards behaves like one on Windows.

What is deliberately left alone:

- ``time.perf_counter`` -- on Windows it is ``QueryPerformanceCounter``, well
  under a microsecond on every Python version. Coarsening it would emulate a
  platform nobody runs, and would make the latency numbers the metrics and
  observability plugins record lie.
- ``time.time`` and ``datetime.now`` -- also coarse on Windows before 3.13,
  but ``datetime.now`` reads the C clock directly and cannot be swapped from
  Python, so patching only half of wall time would test a mixture that does
  not exist anywhere. The queue backends schedule on wall time; this plugin
  does not cover them.

What it cannot reach: a function bound at import time -- ``from time import
monotonic``, or ``clock=time.monotonic`` as a default argument, which is how
``jfastframework.http`` takes its clock -- keeps whichever clock existed when
its module was imported. The patch lands in ``pytest_configure``, before test
collection imports anything, so today every such binding in ``src/`` sees the
coarse clock; a module imported earlier (by a plugin, or ``-p``) would not.

``JFAST_TEST_WINDOWS_CLOCK_STEP`` overrides the step in seconds, for
experiments -- a larger step makes the same races easier to hit.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

ENABLE_VAR = "JFAST_TEST_WINDOWS_CLOCK"
STEP_VAR = "JFAST_TEST_WINDOWS_CLOCK_STEP"

#: GetTickCount64's step: the default timer interrupt of 64 Hz.
WINDOWS_TICK_SECONDS = 1 / 64


@dataclass
class _Originals:
    monotonic: Callable[[], float]
    monotonic_ns: Callable[[], int]
    get_clock_info: Callable[[str], Any]


_originals: _Originals | None = None
_step_seconds: float = WINDOWS_TICK_SECONDS


def enabled() -> bool:
    """Whether the emulation is on for this run. Tests may use it to xfail."""
    return os.environ.get(ENABLE_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def active() -> bool:
    """Whether the clock is currently patched (between configure and unconfigure)."""
    return _originals is not None


def _step_from_env() -> float:
    raw = os.environ.get(STEP_VAR, "").strip()
    if not raw:
        return WINDOWS_TICK_SECONDS
    try:
        step = float(raw)
    except ValueError:
        step = 0.0
    if not 0 < step < float("inf"):
        raise pytest.UsageError(
            f"{STEP_VAR}={raw!r} is not a step: set it to a positive number of seconds, "
            f"e.g. {STEP_VAR}=0.015625 (Windows' tick) or unset it for the default."
        )
    return step


def _install(step: float) -> None:
    global _originals, _step_seconds

    real = _Originals(
        monotonic=time.monotonic,
        monotonic_ns=time.monotonic_ns,
        get_clock_info=time.get_clock_info,
    )
    # Integer nanoseconds, so both functions floor to the same edges and a
    # float step such as 1/64 does not drift across a long uptime.
    step_ns = max(1, round(step * 1_000_000_000))
    real_ns = real.monotonic_ns
    real_info = real.get_clock_info

    def monotonic_ns() -> int:
        now = real_ns()
        return now - now % step_ns

    def monotonic() -> float:
        return monotonic_ns() / 1_000_000_000

    def get_clock_info(name: str, /) -> Any:
        info = real_info(name)
        if name != "monotonic":
            return info
        # The same shape the real function returns: asyncio reads only
        # `.resolution`, but anything that logs the whole thing should see
        # that the clock under it is not the host's.
        return SimpleNamespace(
            implementation=(
                f"jfast windows_clock emulation ({step_ns / 1_000_000:g} ms steps) "
                f"over {info.implementation}"
            ),
            monotonic=True,
            adjustable=False,
            resolution=step_ns / 1_000_000_000,
        )

    time.monotonic = monotonic
    time.monotonic_ns = monotonic_ns
    time.get_clock_info = get_clock_info
    _originals = real
    _step_seconds = step_ns / 1_000_000_000


def _uninstall() -> None:
    global _originals
    if _originals is None:
        return
    time.monotonic = _originals.monotonic
    time.monotonic_ns = _originals.monotonic_ns
    time.get_clock_info = _originals.get_clock_info
    _originals = None


# tryfirst: the patch must be in place before any other plugin's configure
# hook could create a loop. pytest-asyncio creates its loops (function,
# module and session scoped alike) in fixture setup, long after this, and
# they read the clock resolution at creation -- which is the whole point.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if enabled():
        _install(_step_from_env())


# trylast: restore only after every other plugin has torn down, so nothing
# that closes a loop at unconfigure sees the clock change under it.
@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    _uninstall()


def pytest_report_header(config: pytest.Config) -> str | None:
    if not active():
        return None
    return (
        f"windows clock: time.monotonic floored to {_step_seconds * 1000:g} ms steps "
        f"and reported to asyncio as its resolution ({ENABLE_VAR}=1)"
    )
