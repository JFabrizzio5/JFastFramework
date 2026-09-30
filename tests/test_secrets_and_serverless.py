"""Secret loading and serverless artifacts.

Neither touches a cloud account here. What is tested is the parsing and the
precedence rule -- which is where the surprises live -- and that the generated
scripts contain the flags that stop a deploy from silently going wrong.
"""

from __future__ import annotations

import os

import pytest

from jfastframework.deploy.serverless import FunctionConfig, render
from jfastframework.secrets import SecretsConfig, SecretsError, load_secrets, parse

# -- parsing ------------------------------------------------------------


def test_json_object_becomes_a_flat_mapping() -> None:
    assert parse('{"DB_DSN": "postgres://x", "PORT": 8000}') == {
        "DB_DSN": "postgres://x",
        "PORT": "8000",
    }


def test_dotenv_style_is_accepted() -> None:
    payload = "# a comment\nDB_DSN=postgres://x\nTOKEN='quoted'\n\nEMPTY="
    assert parse(payload) == {"DB_DSN": "postgres://x", "TOKEN": "quoted", "EMPTY": ""}


def test_null_becomes_an_empty_string() -> None:
    assert parse('{"OPTIONAL": null}') == {"OPTIONAL": ""}


def test_nested_json_is_refused() -> None:
    # There is no obvious environment-variable spelling for a nested key, and
    # inventing one produces a name nobody can predict.
    with pytest.raises(SecretsError, match="nested"):
        parse('{"db": {"host": "localhost"}}')


def test_malformed_json_is_refused() -> None:
    with pytest.raises(SecretsError, match="not valid JSON"):
        parse('{"unterminated": ')


def test_json_array_is_refused() -> None:
    with pytest.raises(SecretsError, match="must be an object"):
        parse('["a", "b"]')


# -- loading ------------------------------------------------------------


def stub_fetch(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr("jfastframework.secrets._fetch_aws", lambda config: payload)


def test_env_provider_loads_nothing() -> None:
    assert load_secrets(SecretsConfig(provider="env", name="x")) == []


def test_unknown_provider_is_refused() -> None:
    with pytest.raises(SecretsError, match="unknown secrets provider"):
        load_secrets(SecretsConfig(provider="vault", name="x"))


def test_gcp_needs_a_project() -> None:
    with pytest.raises(SecretsError, match="JFAST_SECRETS_PROJECT"):
        load_secrets(SecretsConfig(provider="gcp", name="x"))


def test_values_reach_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_fetch(monkeypatch, '{"JFAST_TEST_ONE": "1"}')
    monkeypatch.delenv("JFAST_TEST_ONE", raising=False)

    loaded = load_secrets(SecretsConfig(provider="aws", name="app"))
    assert loaded == ["JFAST_TEST_ONE"]
    assert os.environ["JFAST_TEST_ONE"] == "1"


def test_an_existing_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's own configuration beats a stale stored copy."""
    stub_fetch(monkeypatch, '{"JFAST_TEST_TWO": "from-secret"}')
    monkeypatch.setenv("JFAST_TEST_TWO", "from-environment")

    assert load_secrets(SecretsConfig(provider="aws", name="app")) == []
    assert os.environ["JFAST_TEST_TWO"] == "from-environment"


def test_override_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_fetch(monkeypatch, '{"JFAST_TEST_THREE": "from-secret"}')
    monkeypatch.setenv("JFAST_TEST_THREE", "from-environment")

    load_secrets(SecretsConfig(provider="aws", name="app"), override=True)
    assert os.environ["JFAST_TEST_THREE"] == "from-secret"


def test_prefix_selects_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    """One shared secret, several services, no cross-reading."""
    stub_fetch(monkeypatch, '{"BILLING_DSN": "a", "SEARCH_DSN": "b"}')
    monkeypatch.delenv("DSN", raising=False)

    loaded = load_secrets(SecretsConfig(provider="aws", name="app", prefix="BILLING_"))
    assert loaded == ["DSN"]
    assert os.environ["DSN"] == "a"


def test_config_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JFAST_SECRETS_PROVIDER", "aws")
    monkeypatch.setenv("JFAST_SECRETS_NAME", "prod/billing")
    monkeypatch.setenv("JFAST_SECRETS_REGION", "eu-west-1")

    config = SecretsConfig.from_env()
    assert (config.provider, config.name, config.region) == ("aws", "prod/billing", "eu-west-1")


# -- serverless ---------------------------------------------------------


def test_unknown_target_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown target"):
        render(FunctionConfig(name="fn", target="azure"))


def test_aws_needs_an_account_id() -> None:
    # Without it the ECR host is a placeholder and the script fails only
    # after the image has been built.
    with pytest.raises(ValueError, match="account-id"):
        render(FunctionConfig(name="fn", target="aws"))


def test_gcp_target_needs_a_project() -> None:
    with pytest.raises(ValueError, match="project"):
        render(FunctionConfig(name="fn", target="gcp"))


def test_aws_target_writes_three_files() -> None:
    files = render(FunctionConfig(name="billing", target="aws", account_id="123456789012"))
    assert set(files) == {"handler.py", "Dockerfile.lambda", "deploy-lambda.sh"}
    assert "Mangum(app" in files["handler.py"]


def test_aws_script_pins_the_platform() -> None:
    """An ARM build on Lambda's x86 runtime fails as a timeout, not an error."""
    files = render(FunctionConfig(name="billing", target="aws", account_id="123456789012"))
    assert "--platform linux/amd64" in files["deploy-lambda.sh"]


def test_aws_script_targets_the_right_registry() -> None:
    files = render(FunctionConfig(name="billing", target="aws", account_id="123456789012"))
    assert "123456789012.dkr.ecr.us-east-1.amazonaws.com/billing" in files["deploy-lambda.sh"]


def test_private_is_the_default_on_both_clouds() -> None:
    aws = render(FunctionConfig(name="fn", target="aws", account_id="123456789012"))
    assert "--auth-type AWS_IAM" in aws["deploy-lambda.sh"]

    gcp = render(FunctionConfig(name="fn", target="gcp", project="p"))
    assert "--no-allow-unauthenticated" in gcp["deploy-cloudrun.sh"]


def test_public_is_opt_in() -> None:
    aws = render(FunctionConfig(name="fn", target="aws", account_id="123456789012", public=True))
    assert "--auth-type NONE" in aws["deploy-lambda.sh"]

    gcp = render(FunctionConfig(name="fn", target="gcp", project="p", public=True))
    assert "--allow-unauthenticated" in gcp["deploy-cloudrun.sh"]
    assert "--no-allow-unauthenticated" not in gcp["deploy-cloudrun.sh"]


def test_limits_reach_the_scripts() -> None:
    files = render(
        FunctionConfig(
            name="fn",
            target="gcp",
            project="p",
            memory_mb=1024,
            timeout_seconds=120,
        )
    )
    assert "--memory 1024Mi" in files["deploy-cloudrun.sh"]
    assert "--timeout 120s" in files["deploy-cloudrun.sh"]


def test_generated_scripts_fail_fast() -> None:
    for config in (
        FunctionConfig(name="fn", target="aws", account_id="123456789012"),
        FunctionConfig(name="fn", target="gcp", project="p"),
    ):
        for name, content in render(config).items():
            if name.endswith(".sh"):
                assert content.startswith("#!/usr/bin/env bash")
                assert "set -euo pipefail" in content


def test_cloud_run_sets_an_env_the_settings_accept() -> None:
    """It set JFAST_ENV=production, harmless only while jfast.toml beat the
    environment; now JFAST_ENV wins, and "production" would stop the boot."""
    import re
    from typing import get_args

    from jfastframework.settings import Environment

    script = render(FunctionConfig(name="fn", target="gcp", project="p"))["deploy-cloudrun.sh"]
    match = re.search(r"JFAST_ENV=(\w+)", script)
    assert match is not None
    assert match.group(1) == "prod"
    assert match.group(1) in get_args(Environment)
