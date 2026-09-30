"""The settings the environment owns: the table, the precedence, the masking.

F12, second half: 0.1.0a12 already let JFAST_ENV and JFAST_DEBUG beat
`[app] env` and `debug`, but a deployment that set JFAST_MAIL_BACKEND=smtp,
JFAST_LLM_BUDGET_USD or JFAST_STORAGE_SERVE_LOCAL=false over a file that set
them was still silently ignored. The rule is now one table,
`deployment_keys.DEPLOYMENT_KEYS`, and these tests hold the kernel, the plugins
and the tools to it.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from pydantic import AliasChoices

from jfastframework.deployment_keys import (
    DEPLOYMENT_KEYS,
    DeploymentKey,
    config_lines,
    line_of,
    mask,
    owned_in_file,
    same_value,
    without_environment_owned,
)
from jfastframework.settings import JFastConfig, JFastSettings

#: Where each table's settings class lives. A plugin added to the table must be
#: added here, which is the point: the test below then checks its variables.
SETTINGS_CLASSES = {
    "observability": ("observability", "ObservabilitySettings"),
    "mail": ("mail", "MailSettings"),
    "notifications": ("notifications", "NotificationSettings"),
    "llm": ("llm", "LLMSettings"),
    "storage": ("storage", "StorageSettings"),
    "auth": ("auth", "AuthSettings"),
    "accounts": ("accounts", "AccountsSettings"),
    "tenancy": ("tenancy", "TenancySettings"),
    "database": ("database", "DatabaseSettings"),
    "cache": ("cache", "CacheSettings"),
    "mongo": ("mongo", "MongoSettings"),
    "qdrant": ("qdrant", "QdrantSettings"),
    "queue": ("queue", "QueueSettings"),
    "events": ("events", "EventsSettings"),
    "rag": ("rag", "RagSettings"),
    "sentry": ("sentry", "SentrySettings"),
    "telemetry": ("telemetry", "TelemetrySettings"),
    "http": ("http", "HttpSettings"),
}


def _settings_class(table: str) -> type:
    if table == "app":
        return JFastSettings
    module, name = SETTINGS_CLASSES[table]
    return getattr(importlib.import_module(f"jfastframework.plugins.builtin.{module}"), name)


def _variables_that_set(cls: type, key: str) -> set[str]:
    """What pydantic-settings reads for *key*, upper-cased."""
    field = cls.model_fields[key]  # type: ignore[attr-defined]
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        return {str(choice).upper() for choice in alias.choices}
    prefix = str(cls.model_config.get("env_prefix", ""))  # type: ignore[attr-defined]
    return {f"{prefix}{key}".upper()}


@pytest.mark.parametrize("spec", DEPLOYMENT_KEYS, ids=lambda spec: f"{spec.table}.{spec.key}")
def test_every_row_names_a_field_and_the_variable_that_really_sets_it(
    spec: DeploymentKey,
) -> None:
    cls = _settings_class(spec.table)
    if "*" in spec.key:
        # upstreams.*.base_url: a dict of models, set per name through the
        # nested delimiter.
        head, _, leaf = spec.key.partition(".*.")
        assert cls.model_config.get("env_nested_delimiter") == "__"  # type: ignore[attr-defined]
        assert head in cls.model_fields  # type: ignore[attr-defined]
        inner = cls.model_fields[head].annotation.__args__[1]  # type: ignore[attr-defined]
        assert leaf in inner.model_fields
        prefix = str(cls.model_config.get("env_prefix", ""))  # type: ignore[attr-defined]
        assert spec.variable == f"{prefix}{head}__{{}}__{leaf}".upper()
        return
    assert spec.key in cls.model_fields, f"{cls.__name__} has no field {spec.key!r}"  # type: ignore[attr-defined]
    assert set(spec.variables) == _variables_that_set(cls, spec.key)


def test_the_table_has_no_duplicate_rows() -> None:
    rows = [(spec.table, spec.key) for spec in DEPLOYMENT_KEYS]
    assert len(rows) == len(set(rows))


def test_every_credential_is_marked_secret() -> None:
    # A SecretStr field that is not marked would print its value in a WARNING.
    from pydantic import SecretStr

    for spec in DEPLOYMENT_KEYS:
        if "*" in spec.key:
            continue
        annotation = str(_settings_class(spec.table).model_fields[spec.key].annotation)
        if "SecretStr" in annotation or annotation == str(SecretStr):
            assert spec.secret, f"{spec.spelled} holds a SecretStr and is not marked secret"


# ---------------------------------------------------------------------------
# Precedence: the plugins, through plugin_config
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    for spec in DEPLOYMENT_KEYS:
        for variable in spec.variables:
            if "{}" not in variable:
                monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("JFAST_MAIL_TIMEOUT", raising=False)
    monkeypatch.delenv("JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL", raising=False)
    # pydantic-settings reads .env from the working directory.
    monkeypatch.chdir(tmp_path)
    return monkeypatch


F12 = """
[app]
name = "mesa"

[plugin.mail]
backend = "console"
host = "localhost"
password = "file-password"
timeout = 12.0

[plugin.llm]
budget_usd = 10.0

[plugin.storage]
serve_local = true

[plugin.database]
dsn = "postgresql+asyncpg://app:filepass@localhost:5432/app"
pool_size = 7

[plugin.http.upstreams.billing]
base_url = "http://localhost:8010"
retries = 4
"""


def _load(tmp_path: Path, body: str = F12) -> JFastConfig:
    path = tmp_path / "jfast.toml"
    path.write_text(body, encoding="utf-8")
    return JFastConfig.load(config_path=path)


def test_the_f12_variables_now_win_over_the_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    from jfastframework.plugins.builtin.llm import LLMSettings
    from jfastframework.plugins.builtin.mail import MailSettings
    from jfastframework.plugins.builtin.storage import StorageSettings

    clean_env.setenv("JFAST_MAIL_BACKEND", "smtp")
    clean_env.setenv("JFAST_MAIL_HOST", "smtp.example.com")
    clean_env.setenv("JFAST_LLM_BUDGET_USD", "0.5")
    clean_env.setenv("JFAST_STORAGE_SERVE_LOCAL", "false")
    config = _load(tmp_path)

    mail = MailSettings(**config.plugin_config("mail"))
    assert (mail.backend, mail.host) == ("smtp", "smtp.example.com")
    assert LLMSettings(**config.plugin_config("llm")).budget_usd == 0.5
    assert StorageSettings(**config.plugin_config("storage")).serve_local is False


def test_the_file_still_decides_when_the_environment_is_silent(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    from jfastframework.plugins.builtin.mail import MailSettings

    config = _load(tmp_path)
    assert MailSettings(**config.plugin_config("mail")).backend == "console"
    assert config.overridden == ()


def test_a_key_the_environment_does_not_own_still_loses_to_the_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    from jfastframework.plugins.builtin.mail import MailSettings

    clean_env.setenv("JFAST_MAIL_TIMEOUT", "99")
    assert MailSettings(**_load(tmp_path).plugin_config("mail")).timeout == 12.0


def test_a_dotenv_file_does_not_beat_the_file_for_a_plugin_either(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """Only the process environment owns a key, as for `[app] env`."""
    from jfastframework.plugins.builtin.mail import MailSettings

    (tmp_path / ".env").write_text("JFAST_MAIL_BACKEND=smtp\n", encoding="utf-8")
    assert MailSettings(**_load(tmp_path).plugin_config("mail")).backend == "console"


def test_an_upstream_base_url_is_owned_per_upstream(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    from jfastframework.plugins.builtin.http import HttpSettings

    clean_env.setenv("JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL", "http://billing.internal:8010")
    upstream = HttpSettings(**_load(tmp_path).plugin_config("http")).upstreams["billing"]
    # The environment's address, the file's retries: only the owned key moved.
    assert (upstream.base_url, upstream.retries) == ("http://billing.internal:8010", 4)


def test_either_telemetry_variable_owns_the_endpoint(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    table = without_environment_owned("telemetry", {"endpoint": "http://localhost:4318", "sql": 1})
    assert table == {"sql": 1}


def test_plugin_config_never_changes_the_raw_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL", "http://billing.internal:8010")
    config = _load(tmp_path)
    config.plugin_config("http")
    assert config.raw["plugin"]["http"]["upstreams"]["billing"]["base_url"] == (
        "http://localhost:8010"
    )


# ---------------------------------------------------------------------------
# The WARNING: one per disagreement, masked
# ---------------------------------------------------------------------------


def test_every_disagreement_is_one_sentence_and_no_secret_is_in_any(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_MAIL_BACKEND", "smtp")
    clean_env.setenv("JFAST_MAIL_PASSWORD", "env-password")
    clean_env.setenv("JFAST_DB_DSN", "postgresql+asyncpg://app:envpass@db:5432/app")
    config = _load(tmp_path)

    assert config.overridden == (
        "[plugin.mail] backend = 'console' in jfast.toml is overridden by "
        "JFAST_MAIL_BACKEND='smtp' from the environment",
        "[plugin.mail] password = '***' in jfast.toml is overridden by "
        "JFAST_MAIL_PASSWORD='***' from the environment",
        "[plugin.database] dsn = 'postgresql+asyncpg://***@localhost:5432/app' in jfast.toml "
        "is overridden by JFAST_DB_DSN='postgresql+asyncpg://***@db:5432/app' from the "
        "environment",
    )
    text = "\n".join(config.overridden)
    for secret in ("file-password", "env-password", "filepass", "envpass"):
        assert secret not in text


@pytest.mark.parametrize(
    ("file_value", "variable"),
    [(10.0, "10"), (10.0, "10.00"), (True, "true"), (False, "0"), (["a"], '["a"]'), ("x", "x")],
)
def test_a_variable_that_says_what_the_file_says_is_not_a_disagreement(
    file_value: object, variable: str
) -> None:
    assert same_value(file_value, variable)


@pytest.mark.parametrize(
    ("file_value", "variable"),
    [(10.0, "0.5"), (True, "false"), (True, "maybe"), (["a"], "a"), ("x", "X"), (5, "five")],
)
def test_a_variable_that_says_something_else_is(file_value: object, variable: str) -> None:
    assert not same_value(file_value, variable)


def test_the_boot_warning_names_a_plugin_key_and_masks_it(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jfastframework import create_app

    clean_env.setenv("JFAST_LOG_JSON_LOGS", "true")
    clean_env.setenv("JFAST_CACHE_URL", "redis://:hunter2@cache:6379/0")
    body = (
        '[app]\nname = "m"\n\n[plugins]\nenabled = ["observability"]\n\n'
        "[plugin.observability]\njson_logs = false\n\n"
        '[plugin.cache]\nurl = "redis://localhost:6379/0"\n'
    )
    path = tmp_path / "jfast.toml"
    path.write_text(body, encoding="utf-8")
    create_app(config_path=path)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    warnings = [line["message"] for line in lines if line["level"] == "WARNING"]
    assert any("json_logs = False" in w and "JFAST_LOG_JSON_LOGS='true'" in w for w in warnings)
    assert any("JFAST_CACHE_URL='redis://***@cache:6379/0'" in w for w in warnings)
    assert "hunter2" not in "\n".join(warnings)


# ---------------------------------------------------------------------------
# Masking and locating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "secret", "shown"),
    [
        ("postgresql://app:pw@db:5432/app", True, "'postgresql://***@db:5432/app'"),
        ("redis://:pw@cache:6379/0", False, "'redis://***@cache:6379/0'"),
        ("https://key@o1.ingest.sentry.io/2", True, "'https://***@o1.ingest.sentry.io/2'"),
        ("amqp://u:p@mq/?password=x", True, "'amqp://***@mq/?***'"),
        ("sk-live-123", True, "'***'"),
        ({"x-api-key": "k"}, True, "'***'"),
        ("smtp", False, "'smtp'"),
        (True, False, "True"),
    ],
)
def test_masking(value: object, secret: bool, shown: str) -> None:
    assert mask(value, secret=secret) == shown


def test_lines_are_found_in_tables_subtables_dotted_and_inline_keys() -> None:
    text = (
        "[app]\n"  # 1
        'env = "local"\n'  # 2
        "\n"  # 3
        "[plugin.mail]\n"  # 4
        "# backend = 'x'\n"  # 5
        'backend = "console"\n'  # 6
        "[plugin.http.upstreams.billing]\n"  # 7
        'base_url = "http://a"\n'  # 8
        "[plugin.http]\n"  # 9
        'upstreams.orders.base_url = "http://b"\n'  # 10
        'upstreams.stock = { base_url = "http://c" }\n'  # 11
        "[[plugin.gateway.routes]]\n"  # 12
        'target = "http://d"\n'  # 13
    )
    raw = {
        "app": {"env": "local"},
        "plugin": {
            "mail": {"backend": "console"},
            "http": {
                "upstreams": {
                    "billing": {"base_url": "http://a"},
                    "orders": {"base_url": "http://b"},
                    "stock": {"base_url": "http://c"},
                }
            },
        },
    }
    lines = config_lines(text)
    found = {item.where: line_of(lines, item) for item in owned_in_file(raw, {})}
    assert found == {
        "[app] env": 2,
        "[plugin.mail] backend": 6,
        "[plugin.http] upstreams.billing.base_url": 8,
        "[plugin.http] upstreams.orders.base_url": 10,
        "[plugin.http] upstreams.stock.base_url": 11,
    }


def test_owned_in_file_narrows_to_the_tables_asked_for() -> None:
    raw = {"app": {"env": "local"}, "plugin": {"mail": {"backend": "console"}}}
    assert [item.where for item in owned_in_file(raw, {}, tables={"app"})] == ["[app] env"]


def test_an_upstream_variable_is_named_after_its_upstream() -> None:
    raw = {"plugin": {"http": {"upstreams": {"billing": {"base_url": "http://a"}}}}}
    (item,) = owned_in_file(raw, {"JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL": "http://b"})
    assert item.variable == "JFAST_HTTP_UPSTREAMS__BILLING__BASE_URL"
    assert item.disagrees


# ---------------------------------------------------------------------------
# Where a disagreement surfaces: jfast check, jfast ai context, AGENTS.md
# ---------------------------------------------------------------------------


def _generated(tmp_path: Path, *plugins: str, agent_docs: bool = False) -> Path:
    from jfastframework.cli.scaffold import Scaffolder, service_context, service_trees

    root = tmp_path / "shop"
    Scaffolder().render_trees(
        service_trees("api", None, root, agent_docs=agent_docs),
        service_context("shop", plugins=list(plugins), agent_docs=agent_docs),
    )
    return root


def _check(root: Path, *args: str) -> tuple[int, dict]:
    import typer
    from typer.testing import CliRunner

    from jfastframework.cli import check as check_cli

    app = typer.Typer()

    @app.callback()
    def _root() -> None: ...

    check_cli.register(app)
    result = CliRunner().invoke(app, ["check", "--path", str(root), "--json", *args])
    return result.exit_code, json.loads(result.stdout)


def test_check_lists_a_disagreement_with_file_line_and_variable_masked(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    root = _generated(tmp_path, "storage")
    clean_env.setenv("JFAST_STORAGE_SERVE_LOCAL", "false")
    clean_env.setenv("JFAST_LOG_JSON_LOGS", "true")
    code, payload = _check(root)

    (config,) = [check for check in payload["checks"] if check["name"] == "config"]
    notices = {notice["message"]: notice for notice in config["notices"]}
    lines = (root / "jfast.toml").read_text(encoding="utf-8").splitlines()
    serve = notices[
        "[plugin.storage] serve_local = True is overridden by "
        "JFAST_STORAGE_SERVE_LOCAL='false' from the environment"
    ]
    assert serve["severity"] == "medium"
    assert serve["code"] == "environment-overrides-file"
    assert (serve["path"], lines[serve["line"] - 1]) == ("jfast.toml", "serve_local = true")
    assert payload["notices"] == 2
    assert code == 0, payload


def test_check_ci_reports_a_disagreement_and_does_not_fail_on_it(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """It describes the machine running the check, not the repository."""
    root = _generated(tmp_path, "storage")
    clean_env.setenv("JFAST_STORAGE_SERVE_LOCAL", "false")
    code, payload = _check(root, "--ci", "--allow-skips")
    assert code == 0, payload
    assert payload["notices"] == 1
    assert not any(payload["counts"].values())


def test_check_says_nothing_about_a_secret_it_overrides(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    root = _generated(tmp_path)
    toml = root / "jfast.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + '\n[plugin.database]\ndsn = "postgresql://app:filepass@localhost/app"\n',
        encoding="utf-8",
    )
    clean_env.setenv("JFAST_DB_DSN", "postgresql://app:envpass@db/app")
    _, payload = _check(root, "--only", "config")
    text = json.dumps(payload)
    assert "postgresql://***@db/app" in text
    assert "filepass" not in text and "envpass" not in text


def test_ai_context_lists_what_the_environment_owns_and_overrides(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    from jfastframework.cli.ai import ENVIRONMENT_RULE, context_payload, survey

    root = _generated(tmp_path, "llm", "storage")
    clean_env.setenv("JFAST_LLM_BUDGET_USD", "0.5")
    for brief in (False, True):
        environment = context_payload(survey(root), brief=brief)["environment"]
        assert environment["rule"] == ENVIRONMENT_RULE
        keys = {item["key"]: item for item in environment["in_file"]}
        assert keys["[plugin.llm] budget_usd"]["variable"] == "JFAST_LLM_BUDGET_USD"
        assert "[plugin.storage] serve_local" in keys
        (overridden,) = environment["overridden_here"]
        assert overridden["key"] == "[plugin.llm] budget_usd"
        assert (overridden["file"], overridden["environment"]) == ("10.0", "'0.5'")
        assert overridden["line"] == keys["[plugin.llm] budget_usd"]["line"]


def test_agents_md_tells_an_agent_which_keys_the_environment_owns(tmp_path: Path) -> None:
    root = _generated(tmp_path, "llm", "storage", agent_docs=True)
    body = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Settings the environment owns" in body
    assert "- `[plugin.llm]` api_key (JFAST_LLM_API_KEY)" in body
    assert "budget_usd (JFAST_LLM_BUDGET_USD)" in body
    assert "- `[app]` env (JFAST_ENV), debug (JFAST_DEBUG)" in body
    # Only the plugins this service runs.
    assert "JFAST_MAIL_BACKEND" not in body
    assert 'do not "fix"\neither side' in body


def test_the_generated_file_marks_every_owned_line_it_writes(tmp_path: Path) -> None:
    """Generator hygiene: an owned key the file writes says the environment wins."""
    import tomllib

    plugins = ("database", "auth", "tenancy", "llm", "storage", "notifications", "cache")
    root = _generated(tmp_path, *plugins)
    text = (root / "jfast.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    lines = text.splitlines()
    owned = owned_in_file(raw, {})
    assert owned, "the generated file writes no owned key at all"
    for item in owned:
        number = line_of(config_lines(text), item)
        assert number is not None
        # The variable is named in the comment block right above the line.
        block = "\n".join(lines[max(0, number - 5) : number])
        assert item.variable in block, f"{item.where} does not say {item.variable} wins"
