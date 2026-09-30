"""Suite-wide plugins.

``windows_clock`` is opt-in (``JFAST_TEST_WINDOWS_CLOCK=1``) and a no-op
otherwise; see its docstring for what it emulates and why.
"""

# By module name, not ``tests.windows_clock``: ``tests`` is not a package, and
# pytest's default import mode puts this directory on ``sys.path`` before it
# imports this file, so the sibling module is importable as a top-level name.
pytest_plugins = ["windows_clock"]
