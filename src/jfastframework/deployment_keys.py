"""The settings the environment owns: one table, read by the kernel and every tool.

``jfast.toml`` is the committed description of the service, and for its shape
-- which plugins, which tables, pool sizes, the layout of each module -- the
file wins over the environment. Some values are not the service's shape but a
property of *where it runs*: whether this is production, the SMTP server, the
database address, the LLM budget this deployment may spend, the domain tenants
live under. For those the process environment wins, and a value in the file is
only the default that applies when the environment is silent.

Before 0.1.0a12 only the second rule existed, so a deployment that set
``JFAST_MAIL_BACKEND=smtp`` over a file that said ``backend = "console"`` sent
its mail to stdout and nothing said so. The rule is declared here, once, as
data -- :data:`DEPLOYMENT_KEYS` -- and every place that has to honour or report
it reads the same table:

- :class:`jfastframework.settings.JFastConfig` leaves an owned key out of what
  it hands pydantic when the variable is set, so the setting reads the variable
  (``[app]`` in ``load``, ``[plugin.<name>]`` in ``plugin_config``);
- ``create_app`` logs one WARNING per key where the two disagree;
- ``jfast check`` and ``jfast ai context`` list those disagreements with the
  file and line;
- ``jfast upgrade --check`` lists, for a project written before 0.1.0a12, every
  owned key its file sets.

Only the *process* environment counts, never a ``.env`` file pydantic reads on
its own. The generated ``.env.example`` carries development values
(``JFAST_ENV=local`` among them), and a copied ``.env`` that could beat a
committed ``env = "prod"`` would turn production off without anyone deciding
it. Compose's ``env_file:`` and ``jfast serve``/``jfast dev`` do put ``.env``
into the process environment, and then it counts like any other variable.

Values are never printed as they are when they can hold a credential: a key
marked ``secret`` shows as ``'***'``, and any URL has its user and password
replaced -- ``redis://:hunter2@cache`` is ``redis://***@cache`` -- because a
cache URL is typed ``str`` and still carries a password.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEPLOYMENT_KEYS",
    "MASK",
    "DeploymentKey",
    "OwnedValue",
    "config_lines",
    "line_of",
    "mask",
    "masked",
    "owned_in_file",
    "process_environment",
    "same_value",
    "toml_spelling",
    "without_environment_owned",
]

#: The spelling of a masked value, everywhere one is printed.
MASK = "***"


@dataclass(frozen=True)
class DeploymentKey:
    """One setting the process environment wins over the file for.

    ``table`` is ``"app"`` for the kernel or a plugin's name for
    ``[plugin.<name>]``. ``key`` is dotted when it lives in a sub-table, and a
    ``*`` segment stands for any name the file gives it: ``upstreams.*.base_url``
    is every upstream's ``base_url``. ``variables`` are the names that set it,
    first the one to recommend; a ``{}`` in one is the ``*`` name upper-cased.
    """

    table: str
    key: str
    variables: tuple[str, ...]
    secret: bool = False

    @property
    def section(self) -> str:
        """How the file spells the table: ``[app]`` or ``[plugin.mail]``."""
        return "[app]" if self.table == "app" else f"[plugin.{self.table}]"

    @property
    def variable(self) -> str:
        return self.variables[0]

    @property
    def spelled(self) -> str:
        """``[plugin.mail] backend (JFAST_MAIL_BACKEND)``: the key and what sets it."""
        return f"{self.section} {self.key} ({self.variable})"


def _key(table: str, key: str, *variables: str, secret: bool = False) -> DeploymentKey:
    return DeploymentKey(table=table, key=key, variables=variables, secret=secret)


#: Every setting the environment owns, and nothing else. A key missing from here
#: keeps the file's precedence; see docs/deploy.md, "Which wins", for the same
#: table in prose and the reasons some candidates are not in it (a storage
#: disk's bucket or endpoint lives in a per-disk table no single variable
#: addresses, a gateway route's upstream is a list entry, and `[app] port` is
#: the port block every generated file is derived from).
#:
#: ``tests/test_deployment_keys.py`` checks each variable against the settings
#: class that reads it, so a renamed field cannot leave a stale row behind.
DEPLOYMENT_KEYS: tuple[DeploymentKey, ...] = (
    # -- the kernel ---------------------------------------------------------
    _key("app", "env", "JFAST_ENV"),
    _key("app", "debug", "JFAST_DEBUG"),
    _key("app", "cors_origins", "JFAST_CORS_ORIGINS"),
    _key("app", "cors_origin_regex", "JFAST_CORS_ORIGIN_REGEX"),
    _key("app", "trusted_hosts", "JFAST_TRUSTED_HOSTS"),
    _key("app", "trusted_proxies", "JFAST_TRUSTED_PROXIES"),
    _key("app", "root_path", "JFAST_ROOT_PATH"),
    # -- logs ---------------------------------------------------------------
    _key("observability", "level", "JFAST_LOG_LEVEL"),
    _key("observability", "json_logs", "JFAST_LOG_JSON_LOGS"),
    # -- mail ---------------------------------------------------------------
    _key("mail", "backend", "JFAST_MAIL_BACKEND"),
    _key("mail", "host", "JFAST_MAIL_HOST"),
    _key("mail", "port", "JFAST_MAIL_PORT"),
    _key("mail", "username", "JFAST_MAIL_USERNAME", secret=True),
    _key("mail", "password", "JFAST_MAIL_PASSWORD", secret=True),
    _key("mail", "from_email", "JFAST_MAIL_FROM_EMAIL"),
    _key("mail", "use_starttls", "JFAST_MAIL_USE_STARTTLS"),
    _key("mail", "use_ssl", "JFAST_MAIL_USE_SSL"),
    # -- notifications: the same decision as the mail backend ---------------
    _key("notifications", "backend", "JFAST_NOTIFICATIONS_BACKEND"),
    _key("notifications", "project_id", "JFAST_NOTIFICATIONS_PROJECT_ID"),
    _key("notifications", "credentials_json", "JFAST_NOTIFICATIONS_CREDENTIALS_JSON", secret=True),
    # -- llm ----------------------------------------------------------------
    _key("llm", "api_key", "JFAST_LLM_API_KEY", secret=True),
    _key("llm", "base_url", "JFAST_LLM_BASE_URL"),
    _key("llm", "chat_model", "JFAST_LLM_CHAT_MODEL"),
    _key("llm", "embedding_model", "JFAST_LLM_EMBEDDING_MODEL"),
    _key("llm", "budget_usd", "JFAST_LLM_BUDGET_USD"),
    _key("llm", "tenant_budget_usd", "JFAST_LLM_TENANT_BUDGET_USD"),
    # -- storage ------------------------------------------------------------
    _key("storage", "serve_local", "JFAST_STORAGE_SERVE_LOCAL"),
    _key("storage", "signing_key", "JFAST_STORAGE_SIGNING_KEY", secret=True),
    # -- auth, accounts, tenancy -------------------------------------------
    _key("auth", "issuer", "JFAST_AUTH_ISSUER"),
    _key("auth", "audience", "JFAST_AUTH_AUDIENCE"),
    _key("auth", "jwks_url", "JFAST_AUTH_JWKS_URL"),
    _key("auth", "secret", "JFAST_AUTH_SECRET", secret=True),
    _key("auth", "public_key", "JFAST_AUTH_PUBLIC_KEY", secret=True),
    _key("accounts", "frontend_url", "JFAST_ACCOUNTS_FRONTEND_URL"),
    _key(
        "accounts",
        "bootstrap_admin_password",
        "JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD",
        secret=True,
    ),
    _key("tenancy", "base_domain", "JFAST_TENANCY_BASE_DOMAIN"),
    # -- every address a plugin connects to --------------------------------
    _key("database", "dsn", "JFAST_DB_DSN", secret=True),
    _key("cache", "url", "JFAST_CACHE_URL", secret=True),
    _key("mongo", "dsn", "JFAST_MONGO_DSN", secret=True),
    _key("qdrant", "url", "JFAST_QDRANT_URL"),
    _key("qdrant", "api_key", "JFAST_QDRANT_API_KEY", secret=True),
    _key("queue", "rabbitmq_url", "JFAST_QUEUE_RABBITMQ_URL", secret=True),
    _key("events", "bootstrap_servers", "JFAST_EVENTS_BOOTSTRAP_SERVERS"),
    _key("rag", "ollama_url", "JFAST_RAG_OLLAMA_URL"),
    _key("sentry", "dsn", "JFAST_SENTRY_DSN", secret=True),
    _key("telemetry", "endpoint", "JFAST_TELEMETRY_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    _key(
        "telemetry",
        "traces_endpoint",
        "JFAST_TELEMETRY_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ),
    _key("telemetry", "headers", "JFAST_TELEMETRY_HEADERS", secret=True),
    _key("http", "upstreams.*.base_url", "JFAST_HTTP_UPSTREAMS__{}__BASE_URL"),
)


def process_environment(variable: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The variable from the process environment, as pydantic-settings reads it.

    Case-insensitive, like the settings themselves. Only the process
    environment -- see the module docstring for why a ``.env`` file does not
    count. An empty value counts as unset.
    """
    current = os.environ if environ is None else environ
    wanted = variable.upper()
    for key, value in current.items():
        if key.upper() == wanted and value.strip():
            return value
    return None


@dataclass(frozen=True)
class OwnedValue:
    """One owned key the file writes, and what the environment says about it."""

    spec: DeploymentKey
    #: The concrete dotted key: ``upstreams.billing.base_url``, never a ``*``.
    key: str
    value: Any
    variable: str
    #: The variable's value when the process environment sets it.
    environment: str | None = None

    @property
    def where(self) -> str:
        return f"{self.spec.section} {self.key}"

    @property
    def overridden(self) -> bool:
        """The environment sets it, so the file's value is not the one that runs."""
        return self.environment is not None

    @property
    def disagrees(self) -> bool:
        """Overridden with a value that is not the file's."""
        return self.environment is not None and not same_value(self.value, self.environment)

    @property
    def file_shown(self) -> str:
        return mask(self.value, secret=self.spec.secret)

    @property
    def environment_shown(self) -> str:
        return mask(self.environment, secret=self.spec.secret)

    def sentence(self) -> str:
        """The boot WARNING, masked."""
        return (
            f"{self.where} = {self.file_shown} in jfast.toml is overridden by "
            f"{self.variable}={self.environment_shown} from the environment"
        )

    def describe(self) -> dict[str, Any]:
        """Safe to print or hand to an agent: every value masked."""
        return {
            "key": self.where,
            "variable": self.variable,
            "file": self.file_shown,
            "environment": self.environment_shown if self.environment is not None else None,
            "disagrees": self.disagrees,
        }


def _expand(spec: DeploymentKey, table: Mapping[str, Any]) -> Iterator[list[str]]:
    """Each concrete path *spec* names in *table*: ``*`` becomes every name there."""
    parts = spec.key.split(".")

    def walk(node: Any, index: int, path: list[str]) -> Iterator[list[str]]:
        if not isinstance(node, Mapping):
            return
        part = parts[index]
        candidates = list(node) if part == "*" else [part]
        for candidate in candidates:
            if candidate not in node:
                continue
            if index == len(parts) - 1:
                yield [*path, candidate]
            else:
                yield from walk(node[candidate], index + 1, [*path, candidate])

    yield from walk(table, 0, [])


def _variables_for(spec: DeploymentKey, names: list[str]) -> list[str]:
    return [variable.format(*(name.upper() for name in names)) for variable in spec.variables]


def _environment_value(
    spec: DeploymentKey, path: list[str], environ: Mapping[str, str] | None
) -> tuple[str, str | None]:
    """The variable the environment set this path with, or the canonical one and None."""
    names = [
        part for part, pattern in zip(path, spec.key.split("."), strict=True) if pattern == "*"
    ]
    variables = _variables_for(spec, names)
    for variable in variables:
        value = process_environment(variable, environ)
        if value is not None:
            return variable, value
    return variables[0], None


def _get(table: Mapping[str, Any], path: list[str]) -> Any:
    node: Any = table
    for part in path:
        node = node[part]
    return node


def _table_of(raw: Mapping[str, Any], table: str) -> Mapping[str, Any]:
    found = raw.get("app", {}) if table == "app" else raw.get("plugin", {}).get(table, {})
    return found if isinstance(found, Mapping) else {}


def owned_in_file(
    raw: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    *,
    tables: set[str] | frozenset[str] | None = None,
) -> list[OwnedValue]:
    """Every owned key *raw* sets, in table order, with the environment's value.

    ``tables`` narrows to ``"app"`` and the plugins named -- the ones that run.
    """
    found: list[OwnedValue] = []
    for spec in DEPLOYMENT_KEYS:
        if tables is not None and spec.table not in tables:
            continue
        table = _table_of(raw, spec.table)
        for path in _expand(spec, table):
            variable, value = _environment_value(spec, path, environ)
            found.append(
                OwnedValue(
                    spec=spec,
                    key=".".join(path),
                    value=_get(table, path),
                    variable=variable,
                    environment=value,
                )
            )
    return found


def without_environment_owned(
    table_name: str, table: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """*table* with every owned key the process environment sets left out.

    Left out rather than replaced, so the settings class reads the variable
    and validates it itself: ``JFAST_ENV=production`` still fails the boot, as
    it would with no ``env`` in the file at all. A copy; *table* is untouched.
    """
    result: dict[str, Any] = _deep_copy(table)
    for spec in DEPLOYMENT_KEYS:
        if spec.table != table_name:
            continue
        for path in list(_expand(spec, result)):
            _, value = _environment_value(spec, path, environ)
            if value is None:
                continue
            parent: Any = result
            for part in path[:-1]:
                parent = parent[part]
            parent.pop(path[-1], None)
    return result


def _deep_copy(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {key: _deep_copy(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_deep_copy(value) for value in node]
    return node


# ---------------------------------------------------------------------------
# Comparing and printing
# ---------------------------------------------------------------------------

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def same_value(file_value: Any, text: str) -> bool:
    """Whether the variable says what the file says, read the way pydantic reads it.

    Loose on purpose: ``JFAST_DEBUG=false`` agrees with ``debug = false`` and
    ``JFAST_LLM_BUDGET_USD=10`` with ``budget_usd = 10.0``. A list or table is
    JSON in a variable, as pydantic-settings parses it. When in doubt they
    disagree -- a WARNING too many beats a silent override.
    """
    stripped = text.strip()
    if isinstance(file_value, bool):
        lowered = stripped.lower()
        return (lowered in _TRUE and file_value) or (lowered in _FALSE and not file_value)
    if isinstance(file_value, int | float):
        try:
            return float(stripped) == float(file_value)
        except ValueError:
            return False
    if isinstance(file_value, list | dict):
        try:
            return bool(json.loads(stripped) == file_value)
        except ValueError:
            return False
    return str(file_value) == text


_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")


def _mask_urls(text: str) -> str:
    return _USERINFO.sub(lambda match: f"{match['scheme']}{MASK}@", text)


def masked(value: Any, *, secret: bool) -> Any:
    """*value* with every credential replaced, still a value rather than text.

    A URL keeps its scheme, host and path -- the part that tells a reader which
    database this is -- and loses its user and password (and, for a secret,
    its query string). A secret that is not a URL loses everything. Anything
    else is returned as it is.
    """
    if isinstance(value, str) and _USERINFO.search(value):
        hidden = _mask_urls(value)
        if secret and "?" in hidden:
            hidden = hidden.split("?", 1)[0] + "?" + MASK
        return hidden
    if secret and value is not None:
        return MASK
    return value


def mask(value: Any, *, secret: bool) -> str:
    """:func:`masked`, as the repr a log line prints."""
    return repr(masked(value, secret=secret))


def toml_spelling(value: Any, *, secret: bool) -> str:
    """:func:`masked`, spelled the way ``jfast.toml`` writes it: ``"local"``, ``true``."""
    hidden = masked(value, secret=secret)
    if isinstance(hidden, bool):
        return "true" if hidden else "false"
    if isinstance(hidden, int | float):
        return str(hidden)
    return json.dumps(hidden, default=str)


# ---------------------------------------------------------------------------
# Where in the file
# ---------------------------------------------------------------------------

_HEADER = re.compile(r"^\[(?!\[)\s*([^\]]+?)\s*\](?:\s*#.*)?$")
_ARRAY_HEADER = re.compile(r"^\[\[")
_KEY_PART = r"""(?:[A-Za-z0-9_\-]+|"[^"]*"|'[^']*')"""
_ASSIGNMENT = re.compile(rf"^({_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*=")


def _split_dotted(text: str) -> list[str]:
    parts = re.findall(r"\"[^\"]*\"|'[^']*'|[A-Za-z0-9_\-]+", text)
    return [part[1:-1] if part[:1] in "\"'" else part for part in parts]


def config_lines(text: str) -> dict[str, int]:
    """The line each assignment in *text* starts on, by its full dotted key.

    ``[plugin.mail]`` + ``backend = ...`` is ``plugin.mail.backend``. Enough
    TOML for pointing at a line, not a parser: a key inside an inline table or
    an array of tables is found by the line of the key that holds it (see
    :func:`line_of`).
    """
    lines: dict[str, int] = {}
    prefix: list[str] = []
    in_array = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _ARRAY_HEADER.match(stripped):
            in_array = True
            continue
        header = _HEADER.match(stripped)
        if header:
            in_array = False
            prefix = _split_dotted(header.group(1))
            continue
        if in_array:
            continue
        assignment = _ASSIGNMENT.match(stripped)
        if assignment:
            lines.setdefault(".".join([*prefix, *_split_dotted(assignment.group(1))]), number)
    return lines


def line_of(lines: Mapping[str, int], owned: OwnedValue) -> int | None:
    """The line *owned* is written on, or that of the inline table holding it."""
    base = ["app"] if owned.spec.table == "app" else ["plugin", owned.spec.table]
    path = [*base, *owned.key.split(".")]
    while len(path) > len(base):
        found = lines.get(".".join(path))
        if found is not None:
            return found
        path.pop()
    return None
