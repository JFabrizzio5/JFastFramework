"""The worker in every generated deployment, whenever the queue plugin is on.

Before this, `jfast start` enabled the queue and no generated file ran
anything that consumed it: the jobs accumulated while the API answered 201.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jfastframework.deploy import build_compose, render_compose
from jfastframework.deploy.kubernetes import build as build_k8s
from jfastframework.deploy.workspace import build_workspace_compose
from jfastframework.plugins.builtin.database import DatabasePlugin
from jfastframework.plugins.builtin.queue import QueuePlugin
from jfastframework.settings import JFastConfig
from jfastframework.workspace import ServiceEntry, Workspace

yaml = pytest.importorskip("yaml")


def _config() -> JFastConfig:
    return JFastConfig.load(config_path=None, overrides={"app_name": "billing", "port": 8010})


def test_single_service_compose_runs_a_worker_beside_the_api() -> None:
    compose = build_compose(_config(), [DatabasePlugin(), QueuePlugin()])
    worker = compose["services"]["worker"]
    api = compose["services"]["api"]
    assert worker["build"] == api["build"]
    assert worker["command"][:2] == ["jfast", "worker"]
    assert worker["environment"] == api["environment"]
    # It serves nothing: no ports, and the image's /health check disabled.
    assert "ports" not in worker
    assert worker["healthcheck"] == {"disable": True}
    # The API's entrypoint runs the migrations its tasks need.
    assert worker["depends_on"]["api"] == {"condition": "service_healthy"}
    # Room for the worker to release what it cannot finish before SIGKILL.
    assert worker["stop_grace_period"] == "30s"
    rendered = yaml.safe_load(render_compose(compose))["services"]["worker"]
    assert rendered["healthcheck"] == {"disable": True}
    # Compose refuses a command item that is not a string.
    assert all(isinstance(item, str) for item in rendered["command"])


def test_no_queue_no_worker() -> None:
    compose = build_compose(_config(), [DatabasePlugin()])
    assert "worker" not in compose["services"]


def _on_disk(root: Path, plugins: list[str]) -> tuple[Workspace, ServiceEntry]:
    service = ServiceEntry(name="billing", kind="api", port=8010, path="billing")
    workspace = Workspace(name="cometax", file=root / "jfast.workspace.toml")
    workspace.add(service)
    workspace.save()
    (root / "billing").mkdir()
    enabled = ", ".join(f'"{name}"' for name in plugins)
    (root / "billing" / "jfast.toml").write_text(
        f"[plugins]\nenabled = [{enabled}]\n", encoding="utf-8"
    )
    return workspace, service


def test_workspace_compose_adds_a_worker_per_service_with_a_queue(tmp_path: Path) -> None:
    workspace, _ = _on_disk(tmp_path, ["observability", "database", "queue"])
    services = build_workspace_compose(workspace, with_caddy=False)["services"]
    worker = services["billing-worker"]
    assert worker["build"] == services["billing"]["build"]
    assert worker["depends_on"]["billing"] == {"condition": "service_healthy"}
    assert "ports" not in worker


def test_workspace_compose_without_a_queue_has_no_worker(tmp_path: Path) -> None:
    workspace, _ = _on_disk(tmp_path, ["observability", "database"])
    assert "billing-worker" not in build_workspace_compose(workspace, with_caddy=False)["services"]


def _documents(files: dict[str, str], name: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(files[name]) if d]


def test_kubernetes_adds_a_worker_deployment_without_probes(tmp_path: Path) -> None:
    workspace, _ = _on_disk(tmp_path, ["observability", "database", "queue"])
    documents = _documents(build_k8s(workspace), "base/billing.yaml")
    deployments = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"}
    worker = deployments["billing-worker"]
    api = deployments["billing"]
    spec = worker["spec"]["template"]["spec"]
    [container] = spec["containers"]
    assert container["command"][:2] == ["jfast", "worker"]
    assert container["image"] == api["spec"]["template"]["spec"]["containers"][0]["image"]
    assert container["env"] == api["spec"]["template"]["spec"]["containers"][0]["env"]
    for probe in ("ports", "livenessProbe", "readinessProbe", "startupProbe"):
        assert probe not in container
    # The drain window sits inside the kill deadline.
    # Every item a string: a bare number would be refused by the API server.
    assert all(isinstance(item, str) for item in container["command"])
    grace = int(container["command"][-1].removeprefix("--grace="))
    assert grace < spec["terminationGracePeriodSeconds"]
    # Its own selector: the API's Service must not route to it.
    assert worker["spec"]["selector"]["matchLabels"] != api["spec"]["selector"]["matchLabels"]
    # No autoscaler targets it: CPU is the wrong signal for a queue consumer.
    targets = [
        d["spec"]["scaleTargetRef"]["name"]
        for d in documents
        if d["kind"] == "HorizontalPodAutoscaler"
    ]
    assert targets == ["billing"]


def test_kubernetes_without_a_queue_has_no_worker(tmp_path: Path) -> None:
    workspace, _ = _on_disk(tmp_path, ["observability", "database"])
    documents = _documents(build_k8s(workspace), "base/billing.yaml")
    assert [d["metadata"]["name"] for d in documents if d["kind"] == "Deployment"] == ["billing"]
