"""`jfast version`, `jfast describe` and `jfast plugins list`: what this
service is, read from its configuration.
"""

from __future__ import annotations

from typing import Any

import typer

from jfastframework.cli.common import _declared_paths, _echo, _load, _root_of
from jfastframework.settings import DEFAULT_CONFIG_FILE

plugins_app = typer.Typer(help="Inspect the plugin graph.", no_args_is_help=True)


def version() -> None:
    """Print the framework version."""
    from jfastframework import __version__

    typer.echo(__version__)


@plugins_app.command("list")
def plugins_list(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    all_available: bool = typer.Option(
        False, "--all", help="Include discovered plugins this service does not enable."
    ),
) -> None:
    """List the plugins this service loads."""
    from jfastframework.plugins import registry

    cfg = _load(config)

    if all_available:
        available = registry.discover(
            extra_paths=_declared_paths(cfg), search_path=_root_of(config)
        )
        broken: dict[str, str] = getattr(registry.discover, "broken", {})
        discovered: dict[str, Any] = {
            "available": {name: vars(cls.meta) for name, cls in available.items()},
            "unimportable": broken,
        }
        lines = [f"{name:<16} {cls.meta.description}" for name, cls in sorted(available.items())]
        lines += [f"{name:<16} UNIMPORTABLE: {err}" for name, err in sorted(broken.items())]
        _echo(discovered, json_out, "\n".join(lines) or "no plugins discovered")
        return

    enabled: list[dict[str, Any]] = [p.describe() for p in registry.build(cfg)]
    lines = [
        f"{p['name']:<16} v{p['version']:<8} provides: {', '.join(p['provides']) or '-'}"
        for p in enabled
    ]
    _echo(enabled, json_out, "\n".join(lines) or "no plugins enabled")


def describe(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(True, "--json/--text"),
) -> None:
    """Full machine-readable description of the service.

    This is the command an AI agent should run first: it returns the settings
    schema, the plugin graph, the provider keys and the infra containers,
    without importing the application.
    """
    from jfastframework.plugins import registry

    cfg = _load(config)
    instances = registry.build(cfg)
    plugin_names = [plugin.meta.name for plugin in instances]
    providers = sorted({key for p in instances for key in p.meta.provides})
    infra = [service.name for p in instances for service in p.infra()]

    payload: dict[str, Any] = {
        "app": cfg.settings.model_dump(mode="json"),
        "plugins": [plugin.describe() for plugin in instances],
        "providers": providers,
        "infra": infra,
        "settings_schema": cfg.settings.model_json_schema(),
    }
    human = "\n".join(
        [
            f"service : {cfg.settings.app_name} v{cfg.settings.version} ({cfg.settings.env})",
            f"plugins : {', '.join(plugin_names) or '-'}",
            f"provides: {', '.join(providers) or '-'}",
            f"infra   : {', '.join(infra) or '-'}",
        ]
    )
    _echo(payload, json_out, human)


def register(app: typer.Typer) -> None:
    """Attach `version`, `describe` and the `plugins` group to *app*."""
    app.command()(version)
    app.command()(describe)
    app.add_typer(plugins_app, name="plugins")
