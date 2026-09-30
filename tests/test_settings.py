"""Config loading: file keys, precedence, plugin blocks."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jfastframework.settings import JFastConfig

CONFIG = """
[app]
name = "billing"
port = 8010
env = "staging"

[plugins]
enabled = ["observability", "metrics"]
disabled = ["sentry"]

[plugin.metrics]
path = "/internal/metrics"
"""


def write(tmp_path: Path, body: str = CONFIG) -> Path:
    path = tmp_path / "jfast.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_app_name_key_maps_onto_the_app_name_field(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.app_name == "billing"


def test_scalar_app_keys_are_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JFAST_ENV", raising=False)
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.port == 8010
    assert config.settings.env == "staging"


def test_plugin_lists_are_loaded(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.plugins == ["observability", "metrics"]
    assert config.settings.disabled_plugins == ["sentry"]


def test_plugin_block_is_returned_per_plugin(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.plugin_config("metrics") == {"path": "/internal/metrics"}
    assert config.plugin_config("absent") == {}


def test_overrides_beat_the_config_file(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path), overrides={"port": 9999})
    assert config.settings.port == 9999


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=tmp_path / "nope.toml")
    assert config.settings.app_name == "jfast-service"
    assert config.raw == {}


# -- the deployment keys: JFAST_ENV and JFAST_DEBUG beat [app] -----------
#
# F12: `jfast start` wrote `[app] env = "local"` and the file won, so a
# production image with JFAST_ENV=prod ran as local -- /docs, /info and
# /queue/stats open, console mail, no HSTS -- and nothing said so.


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    for name in ("JFAST_ENV", "JFAST_DEBUG", "JFAST_PORT"):
        monkeypatch.delenv(name, raising=False)
    # pydantic-settings reads .env from the working directory.
    monkeypatch.chdir(tmp_path)
    return monkeypatch


LOCAL = '[app]\nname = "mesa"\nenv = "local"\nport = 8700\n'


def test_jfast_env_wins_over_the_env_jfast_start_wrote(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_ENV", "prod")
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL))
    assert config.settings.env == "prod"
    assert config.settings.is_production
    assert config.settings.effective_docs_url is None
    assert config.overridden == (
        "[app] env = 'local' in jfast.toml is overridden by JFAST_ENV='prod' from the environment",
    )


def test_the_file_still_decides_when_the_environment_is_silent(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL.replace("local", "staging")))
    assert config.settings.env == "staging"
    assert config.overridden == ()


def test_agreeing_values_are_not_reported(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("JFAST_ENV", "local")
    assert JFastConfig.load(config_path=write(tmp_path, LOCAL)).overridden == ()


def test_the_environment_can_also_lower_it_and_says_so(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_ENV", "local")
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL.replace("local", "prod")))
    assert config.settings.env == "local"
    assert "overridden by JFAST_ENV='local'" in config.overridden[0]


def test_a_dotenv_file_does_not_beat_the_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    """The generated .env.example says JFAST_ENV=local; a copy must not turn prod off."""
    (tmp_path / ".env").write_text("JFAST_ENV=local\n", encoding="utf-8")
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL.replace("local", "prod")))
    assert config.settings.env == "prod"


def test_a_dotenv_file_still_fills_an_env_the_file_does_not_set(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("JFAST_ENV=staging\n", encoding="utf-8")
    config = JFastConfig.load(config_path=write(tmp_path, '[app]\nname = "mesa"\n'))
    assert config.settings.env == "staging"


def test_jfast_debug_wins_over_a_committed_debug(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_DEBUG", "false")
    config = JFastConfig.load(config_path=write(tmp_path, '[app]\nname = "m"\ndebug = true\n'))
    assert config.settings.debug is False
    assert "JFAST_DEBUG='false'" in config.overridden[0]


def test_every_other_key_still_loses_to_the_file(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_PORT", "9999")
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL))
    assert config.settings.port == 8700


def test_explicit_overrides_still_beat_the_environment(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("JFAST_ENV", "prod")
    config = JFastConfig.load(config_path=write(tmp_path, LOCAL), overrides={"env": "local"})
    assert config.settings.env == "local"
    assert config.overridden == ()


def test_a_misspelt_jfast_env_stops_the_boot(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    # It used to be ignored under a file that set env; now it is read, and
    # "production" is not one of local, dev, staging, prod.
    clean_env.setenv("JFAST_ENV", "production")
    with pytest.raises(ValidationError):
        JFastConfig.load(config_path=write(tmp_path, LOCAL))


def test_the_disagreement_is_a_warning_at_boot(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from jfastframework import create_app

    clean_env.setenv("JFAST_ENV", "prod")
    body = LOCAL + '\n[plugins]\nenabled = ["observability"]\n'
    app = create_app(config_path=write(tmp_path, body))
    assert app.state.jfast.settings.env == "prod"
    # observability owns the handlers (JSON on stdout, not propagated), so
    # the line is read where an operator reads it.
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert any(
        line["level"] == "WARNING" and "overridden by JFAST_ENV='prod'" in line["message"]
        for line in lines
    )
