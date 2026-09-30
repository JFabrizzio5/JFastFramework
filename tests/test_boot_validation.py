"""Boot fails on misconfiguration, never on first use.

One test per rule. Each builds the plugin the way `create_app` does and expects
the refusal at `register` -- with a message that names the setting -- rather
than a service that starts and fails on its first query, send or request.
"""

from __future__ import annotations

from typing import Any

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.base import Plugin
from jfastframework.plugins.builtin.auth import AuthPlugin
from jfastframework.plugins.builtin.cache import CachePlugin
from jfastframework.plugins.builtin.database import DatabasePlugin
from jfastframework.plugins.builtin.mail import MailPlugin
from jfastframework.plugins.builtin.queue import QueuePlugin
from jfastframework.plugins.builtin.storage import StoragePlugin
from jfastframework.testing import build_test_app

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
DSN = "postgresql+asyncpg://u:p@localhost:1/db"


def boot(plugin: type[Plugin], config: dict[str, Any], *, production: bool = False) -> None:
    app = build_test_app(env="prod" if production else "local")
    plugin(config).register(app.state.jfast)


def refused(
    plugin: type[Plugin], config: dict[str, Any], match: str, *, production: bool = False
) -> None:
    with pytest.raises(PluginError, match=match):
        boot(plugin, config, production=production)


# -- cache -----------------------------------------------------------------


def test_cache_url_must_be_redis() -> None:
    refused(CachePlugin, {"url": "http://localhost:6379"}, "redis://")


def test_cache_default_ttl_cannot_be_negative() -> None:
    refused(CachePlugin, {"default_ttl": -1}, "default_ttl")


def test_cache_stampede_lock_needs_a_second() -> None:
    refused(CachePlugin, {"stampede_lock_ttl": 0}, "stampede_lock_ttl")


def test_cache_stampede_wait_cannot_be_negative() -> None:
    refused(CachePlugin, {"stampede_wait": -1}, "stampede_wait")


def test_cache_connect_timeout_must_be_positive() -> None:
    refused(CachePlugin, {"connect_timeout": 0}, "connect_timeout")


def test_cache_command_timeout_cannot_be_negative() -> None:
    refused(CachePlugin, {"command_timeout": -1}, "command_timeout")


def test_cache_breaker_cool_down_must_be_positive() -> None:
    refused(CachePlugin, {"breaker_cool_down": 0}, "breaker_cool_down")


def test_cache_defaults_boot() -> None:
    boot(CachePlugin, {})


# -- database --------------------------------------------------------------


def test_database_session_timezone_must_exist() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "session_timezone": "Mars/Olympus"}, "IANA")


def test_database_pool_size_zero_is_not_unlimited() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "pool_size": 0}, "unlimited")


def test_database_max_overflow_cannot_be_negative() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "max_overflow": -2}, "max_overflow")


def test_database_pool_timeout_must_be_positive() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "pool_timeout": 0}, "pool_timeout")


def test_database_connect_timeout_must_be_positive() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "connect_timeout": 0}, "connect_timeout")


def test_database_ping_timeout_must_be_positive_per_connection() -> None:
    refused(
        DatabasePlugin,
        {"connections": {"primary": {"dsn": DSN, "ping_timeout": 0}}},
        r"connections\.primary\] connect_timeout and ping_timeout",
    )


def test_database_command_timeout_cannot_be_negative() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "command_timeout": -1}, "command_timeout")


def test_database_pin_window_must_be_positive_with_a_split() -> None:
    refused(
        DatabasePlugin,
        {
            "read_write_split": True,
            "pin_window": 0,
            "connections": {
                "primary": {"dsn": DSN},
                "replica": {"dsn": DSN, "read_only": True},
            },
        },
        "pin_window",
    )


def test_database_tenant_template_needs_the_placeholder() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "tenant_dsn_template": DSN}, "placeholder")


def test_database_tenant_ceiling_must_be_positive() -> None:
    refused(DatabasePlugin, {"dsn": DSN, "tenant_max_engines": 0}, "tenant_max_engines")


def test_database_defaults_boot() -> None:
    boot(DatabasePlugin, {"dsn": DSN})


# -- storage ---------------------------------------------------------------


def _disk(**extra: Any) -> dict[str, Any]:
    return {"disks": {"files": {"driver": "local", "root": "/tmp", **extra}}, "default": "files"}


def _bucket(**extra: Any) -> dict[str, Any]:
    return {"disks": {"files": {"driver": "s3", "bucket": "b", **extra}}, "default": "files"}


def test_storage_visibility_is_public_or_private() -> None:
    refused(StoragePlugin, _disk(visibility="Public"), "visibility")


def test_storage_public_base_url_must_be_absolute() -> None:
    refused(StoragePlugin, _disk(public_base_url="cdn.example.com"), "public_base_url")


def test_storage_endpoint_url_needs_a_scheme() -> None:
    refused(StoragePlugin, _bucket(endpoint_url="minio:9000"), "endpoint_url")


def test_storage_access_key_needs_its_secret() -> None:
    refused(StoragePlugin, _bucket(access_key="AKIA"), "access_key and secret_key")


def test_storage_s3_timeouts_must_be_positive() -> None:
    refused(StoragePlugin, _bucket(read_timeout=0), "read_timeout")


def test_storage_s3_attempts_count_the_first_try() -> None:
    refused(StoragePlugin, _bucket(max_attempts=0), "max_attempts")


def test_storage_s3_breaker_values() -> None:
    refused(StoragePlugin, _bucket(breaker_failures=-1), "breaker_failures")


def test_storage_prefix_is_a_path() -> None:
    refused(StoragePlugin, {**_disk(), "prefix": "storage"}, "prefix")


def test_storage_short_signing_key_is_refused_in_production() -> None:
    refused(StoragePlugin, {**_disk(), "signing_key": "short"}, "32 bytes", production=True)


def test_storage_s3_settings_reach_the_disk() -> None:
    app = build_test_app()
    plugin = StoragePlugin(_bucket(connect_timeout=1.5, breaker_failures=0))
    plugin.register(app.state.jfast)
    disk: Any = app.state.jfast.require("storage").disk("files")
    assert disk._connect_timeout == 1.5
    assert disk.breaker is None


# -- mail ------------------------------------------------------------------


def test_mail_backend_must_exist() -> None:
    refused(MailPlugin, {"backend": "sendgrid"}, "backend")


def test_mail_attachment_limit_must_be_positive() -> None:
    refused(MailPlugin, {"max_attachment_bytes": 0}, "max_attachment_bytes")


def test_mail_from_email_must_be_an_address() -> None:
    refused(MailPlugin, {"from_email": "billing"}, "not an address")


def test_mail_smtp_needs_a_host() -> None:
    refused(MailPlugin, {"backend": "smtp", "host": ""}, "host")


def test_mail_smtp_port_is_a_port() -> None:
    refused(MailPlugin, {"backend": "smtp", "port": 70000}, "port")


def test_mail_smtp_timeout_must_be_positive() -> None:
    refused(MailPlugin, {"backend": "smtp", "timeout": 0}, "timeout")


def test_mail_ssl_and_starttls_are_exclusive() -> None:
    refused(MailPlugin, {"backend": "smtp", "use_ssl": True, "use_starttls": True}, "exclusive")


def test_mail_production_needs_a_sender() -> None:
    refused(
        MailPlugin,
        {"backend": "smtp", "username": "apikey", "password": "x"},
        "sender address",
        production=True,
    )


# -- auth ------------------------------------------------------------------


def _secret(**extra: Any) -> dict[str, Any]:
    return {"mode": "secret", "secret": SECRET, "algorithms": ["HS256"], **extra}


def test_auth_algorithms_cannot_be_empty() -> None:
    refused(AuthPlugin, _secret(algorithms=[]), "algorithms is empty")


def test_auth_algorithms_must_be_known_to_pyjwt() -> None:
    refused(AuthPlugin, _secret(algorithms=["HS257"]), "PyJWT does not know")


@pytest.mark.parametrize(
    "field",
    [
        "leeway",
        "access_lifetime_minutes",
        "refresh_lifetime_days",
        "refresh_grace_seconds",
        "jwks_cache_seconds",
        "jwks_attempts",
        "jwks_breaker_failures",
    ],
)
def test_auth_numeric_floors(field: str) -> None:
    refused(AuthPlugin, _secret(**{field: -1}), field)


def test_auth_jwks_timeout_must_be_positive() -> None:
    refused(AuthPlugin, _secret(jwks_timeout=0), "jwks_timeout")


def test_auth_jwks_url_must_be_http() -> None:
    refused(AuthPlugin, {"mode": "jwks", "jwks_url": "id.example.com/jwks"}, "http")


def test_auth_jwks_url_must_be_https_in_production() -> None:
    refused(
        AuthPlugin,
        {"mode": "jwks", "jwks_url": "http://id.example.com/jwks", "audience": "a"},
        "plain http",
        production=True,
    )


def test_auth_short_secret_is_refused_in_production() -> None:
    refused(
        AuthPlugin,
        _secret(secret="short", audience="a"),
        "32",
        production=True,
    )


def test_auth_public_key_mode_cannot_issue() -> None:
    refused(
        AuthPlugin,
        {"mode": "public_key", "public_key": "-----BEGIN PUBLIC KEY-----", "issue_tokens": True},
        "public_key",
    )


def test_auth_jwks_settings_reach_the_client() -> None:
    app = build_test_app()
    plugin = AuthPlugin(
        {"mode": "jwks", "jwks_url": "https://id.example.com/jwks", "jwks_timeout": 1.5}
    )
    plugin.register(app.state.jfast)
    assert plugin._jwks is not None
    assert plugin._jwks.timeout == 1.5


# -- queue -----------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["visibility_timeout", "max_attempts", "prefetch", "scheduler_retention_days"]
)
def test_queue_numeric_floors(field: str) -> None:
    refused(QueuePlugin, {"backend": "redis", field: 0}, field)


def test_queue_postgres_name_is_a_table_name() -> None:
    refused(QueuePlugin, {"backend": "postgres", "name": "jobs; drop table x"}, "table name")


def test_queue_rabbitmq_url_scheme() -> None:
    refused(
        QueuePlugin,
        {"backend": "rabbitmq", "rabbitmq_url": "http://localhost:5672"},
        "amqp://",
    )


def test_queue_rabbitmq_guest_default_is_refused_in_production() -> None:
    refused(QueuePlugin, {"backend": "rabbitmq"}, "development default", production=True)
