"""Typed configuration for the JFast kernel.

Rule enforced here: no ``os.getenv`` scattered through service code. Anything
configurable is a validated field, so a bad value fails at boot instead of at
the first request that touches it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from datetime import tzinfo
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from jfastframework.middleware import (
    DEFAULT_PERMISSIONS_POLICY,
    DEFAULT_REFERRER_POLICY,
    TrustedProxies,
    build_default_csp,
)
from jfastframework.time import set_default_zone
from jfastframework.time import zone as resolve_zone

Environment = Literal["local", "dev", "staging", "prod"]

DEFAULT_CONFIG_FILE = "jfast.toml"

HSTS_ONE_YEAR = 31_536_000

# The plugin whose presence raises the two request limits, and the pair it
# raises them to. Uploads are the one workload the kernel numbers are
# deliberately too small for, so the rule lives here rather than in the
# scaffold: a project that enables `storage` a year after `jfast new service`
# gets the same limits as one generated with it.
UPLOAD_PLUGIN = "storage"
UPLOAD_MAX_BODY_BYTES = 25 * 1024 * 1024
UPLOAD_REQUEST_TIMEOUT = 120.0


def raises_request_limits(enabled: Sequence[str], disabled: Sequence[str]) -> bool:
    """Whether this plugin selection is the one that raises the request limits.

    Shared with :mod:`jfastframework.upgrades` so that ``upgrade --check`` and
    the running service cannot disagree about which projects the raised pair
    applies to. ``storage`` is never `default_enabled`, so an empty allow-list
    cannot load it and naming it is the whole condition.
    """
    return UPLOAD_PLUGIN in enabled and UPLOAD_PLUGIN not in disabled


class JFastSettings(BaseSettings):
    """Kernel settings. Plugins own their own settings models."""

    model_config = SettingsConfigDict(
        env_prefix="JFAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "jfast-service"
    version: str = "0.1.0"
    env: Environment = "local"
    debug: bool = False

    # The *business* zone, not the server's and not the container's: what
    # "today" means for a report, an invoice period or a daily quota. Storage
    # stays UTC and the database plugin pins every session to UTC, so this is
    # the only place a local day is decided. An IANA name; validated below so a
    # typo stops the boot instead of shifting a report by a day.
    timezone: str = "UTC"

    # A container binds every interface; the container is the network
    # boundary, not this value.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    root_path: str = ""

    # Plugin selection. `plugins` is the allow-list; when empty, every
    # discovered plugin whose meta says default_enabled is loaded.
    # `disabled_plugins` always wins -- that is how you strip monitoring
    # out of a service without editing code.
    plugins: list[str] = Field(default_factory=list)
    disabled_plugins: list[str] = Field(default_factory=list)

    docs_url: str | None = "/docs"
    openapi_url: str | None = "/openapi.json"

    # Seconds any single plugin gets to answer a readiness probe. A
    # dependency that hangs at the TCP level -- not refused, hung -- would
    # otherwise hold the probe open until the socket gives up, and an
    # orchestrator cannot tell that apart from a slow service.
    readiness_timeout: float = 2.0

    # -- edge protections ------------------------------------------
    #
    # These ship on. Caddy or an ingress covers some of them when one is in
    # front, but `jfast deploy function` puts a service on Lambda with
    # nothing in front at all, and `uvicorn main:app` on a laptop has
    # nothing either. Every number below is a policy decision with a wrong
    # answer for somebody, so each one can be turned back off -- and a
    # default that is wrong for one service beats a hole in every service.

    # Exact origins. `["*"]` is accepted and refused in combination with
    # cors_allow_credentials, because browsers reject that pair anyway and
    # failing at boot beats failing in someone's console.
    cors_origins: list[str] = Field(default_factory=list)
    # Origins a list cannot spell: one per tenant subdomain, say. Matched
    # whole (Starlette's allow_origin_regex uses fullmatch), so anchors are
    # implied; escape the dots, or `.` also matches the `-` in a lookalike
    # domain. Checked in addition to cors_origins, and compiled at boot.
    #   cors_origin_regex = 'https://[a-z0-9-]+\.example\.com'   # TOML literal string
    cors_origin_regex: str | None = None
    cors_allow_credentials: bool = False
    cors_allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers: list[str] = Field(default_factory=lambda: ["*"])

    # Host header allow-list. Empty means every host, which is right
    # behind a proxy that already validated it.
    trusted_hosts: list[str] = Field(default_factory=list)

    # Largest request body accepted, in bytes. 2 MiB is far above any JSON
    # this framework generates a handler for and far below what it costs to
    # buffer one. Enabling `storage` raises it to UPLOAD_MAX_BODY_BYTES --
    # see effective_max_body_bytes -- and 0 turns the limit off, which is the
    # only way a TOML file can say "unlimited" when it has no null.
    max_body_bytes: int | None = 2 * 1024 * 1024

    # Seconds before an unfinished request is answered with 504. 30s is the
    # generated gateway's own `[plugin.gateway] timeout`, so the service
    # gives up at the same moment the thing in front of it does instead of
    # holding a worker for a response nobody is still waiting for. Raised
    # alongside the body limit when `storage` is on. 0 is off.
    request_timeout: float | None = 30.0

    # -- proxies ---------------------------------------------------
    #
    # Peers allowed to speak for their client through X-Forwarded-*.
    # Loopback plus the private ranges, because that is where the edge is in
    # everything this framework generates: Caddy on a compose bridge
    # (172.16/12), an ingress on a pod network (10/8), a sidecar on
    # localhost. A request arriving from a public address is a client
    # talking to us directly and its X-Forwarded-For is a suggestion.
    #
    # Narrow this to the balancer's real range on a deployment where a
    # public client can reach the service from inside the private network.
    # "*" trusts every peer: right on a platform whose front end has no
    # stable address, wrong anywhere the service is also reachable directly.
    trusted_proxies: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.1/32",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "fc00::/7",
        ]
    )

    # -- security headers ------------------------------------------

    security_headers: bool = True

    # None builds the policy from the service: see build_default_csp. It is
    # enforced, not report-only, because it was written around what this
    # framework's own pages load. Set csp_report_only while rolling out a
    # tighter policy of your own.
    csp: str | None = None
    csp_report_only: bool = False

    frame_options: str | None = "DENY"
    referrer_policy: str | None = DEFAULT_REFERRER_POLICY
    permissions_policy: str | None = DEFAULT_PERMISSIONS_POLICY

    # HSTS is remembered by the browser for its whole max-age, so switching
    # it on by mistake poisons http://localhost for a year and no amount of
    # cache clearing in the app fixes it. None means "a year in production",
    # and the middleware still withholds it from any request that did not
    # arrive over HTTPS. Set a number to ask for it anywhere.
    hsts_seconds: int | None = None
    hsts_include_subdomains: bool = True
    # Off: submitting to the preload list is close to irreversible and
    # covers every subdomain forever, which is not a framework's call.
    hsts_preload: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    @property
    def business_zone(self) -> tzinfo:
        """The resolved ``[app] timezone``. Already validated, so it cannot raise."""
        return resolve_zone(self.timezone)

    @property
    def effective_max_body_bytes(self) -> int | None:
        """The limit, or None when it is switched off. 0 and None both mean off.

        A service with `storage` on gets the raised default, because 2 MiB
        refuses the upload the plugin exists to accept. An explicit value --
        including an explicit 0 -- always wins, so the raise cannot undo a
        limit somebody chose.
        """
        if "max_body_bytes" not in self.model_fields_set and raises_request_limits(
            self.plugins, self.disabled_plugins
        ):
            return UPLOAD_MAX_BODY_BYTES
        return self.max_body_bytes or None

    @property
    def effective_request_timeout(self) -> float | None:
        """30s, or the raised default when `storage` is on. 0 and None mean off.

        Raised with the body limit and for the same reason: a slow connection
        does not finish 25 MiB inside 30 seconds, and a 504 half-way through an
        upload is the body limit's failure wearing a different status code.
        """
        if "request_timeout" not in self.model_fields_set and raises_request_limits(
            self.plugins, self.disabled_plugins
        ):
            return UPLOAD_REQUEST_TIMEOUT
        return self.request_timeout or None

    @property
    def effective_csp(self) -> str | None:
        """The policy that will actually be sent.

        The HTML half of the policy is asked for by the ``web`` plugin, which
        is what renders pages, so a JSON API is not granted the HTMX CDN or an
        inline allowance for markup it never produces.
        """
        if self.csp is not None:
            return self.csp or None
        return build_default_csp(
            docs_enabled=self.effective_openapi_url is not None,
            html_enabled="web" in self.plugins,
        )

    @property
    def effective_hsts_seconds(self) -> int:
        """0 when HSTS is off. The middleware still requires an HTTPS request."""
        if self.hsts_seconds is not None:
            return self.hsts_seconds
        return HSTS_ONE_YEAR if self.is_production else 0

    @property
    def effective_docs_url(self) -> str | None:
        """Interactive docs, closed in production unless asked for.

        ``/info`` disables itself in production, and ``/docs`` and the
        OpenAPI document follow it: the schema names every route, body field
        and error, which is a map for anyone probing the service. Set
        ``docs_url`` explicitly to keep it.
        """
        return self._closed_in_production("docs_url", self.docs_url)

    @property
    def effective_openapi_url(self) -> str | None:
        return self._closed_in_production("openapi_url", self.openapi_url)

    def _closed_in_production(self, field: str, value: str | None) -> str | None:
        if self.is_production and field not in self.model_fields_set:
            return None
        return value

    @field_validator("max_body_bytes", "request_timeout", "hsts_seconds")
    @classmethod
    def _refuse_negative(cls, value: float | None) -> Any:
        """0 is the off switch; below it is a typo nobody meant."""
        if value is not None and value < 0:
            raise ValueError("must be 0 (off) or greater")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """An unknown zone must stop the boot, not the first report.

        The failure mode without this is a service that starts, serves for a
        week and then answers a daily total for the wrong day -- or raises
        inside a request handler on an image with no system tzdata, which
        reads as an application bug rather than a missing package.
        """
        resolve_zone(value)
        return value

    @field_validator("cors_origin_regex")
    @classmethod
    def _validate_origin_regex(cls, value: str | None) -> str | None:
        """A pattern that does not compile would fail on the first preflight."""
        if value is None or not value.strip():
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"cors_origin_regex is not a valid pattern: {exc}") from exc
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_cidrs(cls, value: list[str]) -> list[str]:
        """A bad CIDR here silently trusts nobody, so it fails at boot instead."""
        TrustedProxies(value)
        return value

    @model_validator(mode="after")
    def _refuse_wildcard_with_credentials(self) -> JFastSettings:
        """A pair browsers reject should fail at boot, not in someone's console."""
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError(
                "cors_origins cannot be ['*'] while cors_allow_credentials is on. "
                "Browsers refuse that combination, so it would fail silently at "
                "runtime. List the origins."
            )
        return self


class JFastConfig:
    """Merged view of ``jfast.toml`` plus environment-backed kernel settings.

    ``jfast.toml`` layout::

        [app]
        name = "billing"
        port = 8010
        timezone = "America/Santiago"

        [plugins]
        enabled = ["observability", "metrics", "database"]
        disabled = ["sentry"]

        [plugin.database]
        pool_size = 20
    """

    def __init__(self, settings: JFastSettings, raw: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.raw: dict[str, Any] = raw or {}

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = DEFAULT_CONFIG_FILE,
        overrides: dict[str, Any] | None = None,
    ) -> JFastConfig:
        raw: dict[str, Any] = {}
        if config_path is not None:
            path = Path(config_path)
            if path.is_file():
                raw = tomllib.loads(path.read_text(encoding="utf-8"))

        app_section = dict(raw.get("app", {}))
        # `[app] name` reads better in a config file than `app_name`, but the
        # env var has to stay JFAST_APP_NAME to avoid colliding with the very
        # common JFAST_NAME. Map the friendly key onto the field.
        if "name" in app_section:
            app_section.setdefault("app_name", app_section.pop("name"))

        plugins_section = raw.get("plugins", {})
        if "enabled" in plugins_section:
            app_section.setdefault("plugins", plugins_section["enabled"])
        if "disabled" in plugins_section:
            app_section.setdefault("disabled_plugins", plugins_section["disabled"])
        if overrides:
            app_section.update(overrides)

        settings = JFastSettings(**app_section)
        # Published here rather than in the field validator: a validator runs
        # on every JFastSettings a test constructs, and a process-wide default
        # must change only when a configuration is actually loaded.
        set_default_zone(settings.timezone)
        return cls(settings=settings, raw=raw)

    def plugin_config(self, name: str) -> dict[str, Any]:
        """Raw config block for one plugin (``[plugin.<name>]`` in jfast.toml)."""
        return dict(self.raw.get("plugin", {}).get(name, {}))
