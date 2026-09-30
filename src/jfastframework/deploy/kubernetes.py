"""Kubernetes manifests, derived from the workspace.

The service contract is what makes this generatable at all: every JFast
service, in any language, exposes `/health` for liveness and `/ready` for
readiness, reads `JFAST_*` from the environment, and owns one port. Those are
exactly the facts a Deployment needs.

Kustomize rather than a Helm chart. A chart is the right answer when you are
shipping software other people install; overlays are the right answer when you
are deploying your own services to your own clusters, and they stay readable as
plain YAML.

**Databases are not generated.** A StatefulSet for PostgreSQL emitted by a
scaffolder is how people lose data: no backups, no PITR, no tested restore, and
a `kubectl delete` away from gone. The manifests reference a DSN in a Secret and
leave the database to a managed service or to an operator someone chose on
purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jfastframework.deploy.compose import _dump_yaml

if TYPE_CHECKING:
    from jfastframework.workspace import ServiceEntry, Workspace

# Env vars a generated Deployment reads from a Secret rather than a ConfigMap.
# Only for the legacy per-service `datastores` list; a workspace that names its
# resources derives the same table from the bindings, which is what lets a
# service hold two PostgreSQL instances instead of one.
SECRET_ENV: dict[str, tuple[str, str]] = {
    "database": ("JFAST_DB_DSN", "db-dsn"),
    "cache": ("JFAST_CACHE_URL", "cache-url"),
    "mongo": ("JFAST_MONGO_DSN", "mongo-dsn"),
    "qdrant": ("JFAST_QDRANT_URL", "qdrant-url"),
}


def secret_key(variable: str) -> str:
    """``JFAST_DB_REPLICA_DSN`` -> ``db-replica-dsn``.

    One key per *variable*, not per datastore type: two PostgreSQL instances
    bound to one service differ only in the variable that carries them, so
    keying on anything else collapses them into one secret and one database.
    """
    body = variable[len("JFAST_") :] if variable.startswith("JFAST_") else variable
    return body.lower().replace("_", "-")


def secret_env(service: ServiceEntry, workspace: Workspace | None = None) -> dict[str, str]:
    """Environment variable -> Secret key, for everything this service binds."""
    if workspace is not None:
        return {
            binding.resolved_env(resource): secret_key(binding.resolved_env(resource))
            for binding, resource in workspace.bindings_for(service)
        }
    return dict(SECRET_ENV[store] for store in service.datastores if store in SECRET_ENV)


def _document(*parts: dict[str, Any]) -> str:
    return "\n---\n".join(_dump_yaml(part).lstrip("\n") for part in parts) + "\n"


def deployment(
    service: ServiceEntry,
    *,
    namespace: str,
    replicas: int = 2,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    env: list[dict[str, Any]] = [
        {"name": "JFAST_APP_NAME", "value": service.name},
        {"name": "JFAST_PORT", "value": str(service.port)},
        {
            "name": "JFAST_ENV",
            "valueFrom": {"configMapKeyRef": {"name": f"{service.name}-config", "key": "env"}},
        },
    ]
    for variable, key in (secrets if secrets is not None else secret_env(service)).items():
        env.append(
            {
                "name": variable,
                "valueFrom": {"secretKeyRef": {"name": f"{service.name}-secrets", "key": key}},
            }
        )

    ports: list[dict[str, Any]] = [{"name": "http", "containerPort": service.port}]
    if service.grpc:
        ports.append({"name": "grpc", "containerPort": service.grpc_port})

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": service.name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": service.name,
                "app.kubernetes.io/part-of": namespace,
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app.kubernetes.io/name": service.name}},
            "strategy": {
                "type": "RollingUpdate",
                # Never drop below the current replica count during a rollout.
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": service.name}},
                "spec": {
                    # A pod that runs as root is a finding in every review, and
                    # retrofitting it means rebuilding images across the fleet.
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    # Give in-flight requests time to finish before SIGTERM's
                    # deadline; the services already shut down gracefully.
                    "terminationGracePeriodSeconds": 30,
                    "containers": [
                        {
                            "name": service.name,
                            "image": f"{service.name}:latest",
                            "imagePullPolicy": "IfNotPresent",
                            "ports": ports,
                            "env": env,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            # Liveness probes the process; readiness probes the
                            # dependencies. Pointing liveness at /ready turns a
                            # database blip into a restart storm.
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": "http"},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 15,
                                "timeoutSeconds": 3,
                                "failureThreshold": 3,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/ready", "port": "http"},
                                "initialDelaySeconds": 3,
                                "periodSeconds": 10,
                                "timeoutSeconds": 3,
                                "failureThreshold": 3,
                            },
                            # Startup probe so a slow first boot (migrations,
                            # warm caches) is not mistaken for a crash.
                            "startupProbe": {
                                "httpGet": {"path": "/health", "port": "http"},
                                "periodSeconds": 5,
                                "failureThreshold": 30,
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],  # nosec B108
                        }
                    ],
                    # readOnlyRootFilesystem needs somewhere writable.
                    "volumes": [{"name": "tmp", "emptyDir": {}}],
                },
            },
        },
    }


#: The worker's drain window and the pod's kill deadline, five seconds apart so
#: a job it cannot finish is released before SIGKILL rather than left
#: invisible until the queue's visibility timeout.
WORKER_GRACE_SECONDS = 25
WORKER_TERMINATION_SECONDS = 30


def worker_deployment(
    service: ServiceEntry,
    *,
    namespace: str,
    replicas: int = 1,
    secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`jfast worker` for a service whose queue plugin is on.

    The API's Deployment with a different command: same image, environment
    and security context. No ports and no HTTP probes -- it serves nothing, so
    `/health` would fail and restart it forever. Scale it on queue depth, not
    CPU; no autoscaler is generated for it.
    """
    manifest = deployment(service, namespace=namespace, replicas=replicas, secrets=secrets)
    name = f"{service.name}-worker"
    manifest["metadata"]["name"] = name
    manifest["metadata"]["labels"] = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/component": "worker",
        "app.kubernetes.io/part-of": namespace,
    }
    spec = manifest["spec"]
    spec["selector"] = {"matchLabels": {"app.kubernetes.io/name": name}}
    # A worker needs no surge: a job is safe in the queue while none runs.
    spec["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}}
    template = spec["template"]
    template["metadata"] = {"labels": {"app.kubernetes.io/name": name}}
    template["spec"]["terminationGracePeriodSeconds"] = WORKER_TERMINATION_SECONDS
    [container] = template["spec"]["containers"]
    container["name"] = name
    container["command"] = ["jfast", "worker", "--grace", str(WORKER_GRACE_SECONDS)]
    for key in ("ports", "livenessProbe", "readinessProbe", "startupProbe"):
        container.pop(key, None)
    return manifest


def runs_worker(service: ServiceEntry, workspace: Workspace) -> bool:
    """Whether the service's jfast.toml enables the queue plugin."""
    if workspace.file is None:
        return False
    config_path = workspace.file.parent / service.path / "jfast.toml"
    if not config_path.is_file():
        return False
    from jfastframework.settings import JFastConfig

    config = JFastConfig.load(config_path)
    return "queue" in config.settings.plugins and "queue" not in config.settings.disabled_plugins


def service_manifest(service: ServiceEntry, *, namespace: str) -> dict[str, Any]:
    ports: list[dict[str, Any]] = [{"name": "http", "port": service.port, "targetPort": "http"}]
    if service.grpc:
        ports.append({"name": "grpc", "port": service.grpc_port, "targetPort": "grpc"})

    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": service.name, "namespace": namespace},
        "spec": {
            "selector": {"app.kubernetes.io/name": service.name},
            "ports": ports,
        },
    }


def config_map(service: ServiceEntry, *, namespace: str, env: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{service.name}-config", "namespace": namespace},
        "data": {"env": env},
    }


def secret_template(
    service: ServiceEntry, *, namespace: str, secrets: dict[str, str] | None = None
) -> dict[str, Any]:
    """A Secret with placeholders, never real values.

    Committing this file is fine; committing a filled-in one is not. Use
    Sealed Secrets, External Secrets or your cloud's secret manager and delete
    this once it is wired.
    """
    data = {
        key: "REPLACE_ME"
        for key in (secrets if secrets is not None else secret_env(service)).values()
    }
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"{service.name}-secrets", "namespace": namespace},
        "type": "Opaque",
        "stringData": data or {"placeholder": "REPLACE_ME"},
    }


def autoscaler(
    service: ServiceEntry, *, namespace: str, minimum: int = 2, maximum: int = 10
) -> dict[str, Any]:
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": service.name, "namespace": namespace},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": service.name,
            },
            "minReplicas": minimum,
            "maxReplicas": maximum,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {"type": "Utilization", "averageUtilization": 70},
                    },
                }
            ],
        },
    }


def disruption_budget(service: ServiceEntry, *, namespace: str) -> dict[str, Any]:
    """Keep one replica during voluntary disruptions.

    Without it, a node drain can take every replica of a service at once and
    the "zero-downtime" rollout above buys nothing.
    """
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": service.name, "namespace": namespace},
        "spec": {
            "minAvailable": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": service.name}},
        },
    }


def ingress(workspace: Workspace, *, namespace: str, host: str) -> dict[str, Any]:
    """One host, `/api` in front of the backends, matching the Caddyfile.

    Keeping the public shape identical to the local one means the frontend
    build does not change between environments.
    """
    target = workspace.gateway or (workspace.backends[0] if workspace.backends else None)
    paths: list[dict[str, Any]] = []

    if target is not None:
        paths.append(
            {
                "path": "/api",
                "pathType": "Prefix",
                "backend": {"service": {"name": target.name, "port": {"number": target.port}}},
            }
        )

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": f"{namespace}-ingress",
            "namespace": namespace,
            "annotations": {
                "nginx.ingress.kubernetes.io/rewrite-target": "/$2",
                "cert-manager.io/cluster-issuer": "letsencrypt-prod",
            },
        },
        "spec": {
            "tls": [{"hosts": [host], "secretName": f"{namespace}-tls"}],
            "rules": [{"host": host, "http": {"paths": paths}}],
        },
    }


def namespace_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    }


def kustomization(resources: list[str], *, namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": namespace,
        "resources": sorted(resources),
    }


def overlay_kustomization(environment: str, *, namespace: str, replicas: int) -> dict[str, Any]:
    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": f"{namespace}-{environment}" if environment != "prod" else namespace,
        "resources": ["../../base"],
        "patches": [{"path": "replicas.yaml"}],
    }


def build(
    workspace: Workspace,
    *,
    namespace: str | None = None,
    host: str = "example.com",
    environments: tuple[str, ...] = ("dev", "prod"),
) -> dict[str, str]:
    """Every manifest, as ``{relative path: contents}``."""
    ns = namespace or workspace.name.replace("_", "-")
    deployable = [s for s in workspace.services if not s.is_frontend]

    files: dict[str, str] = {}
    resources: list[str] = ["namespace.yaml"]

    files["base/namespace.yaml"] = _document(namespace_manifest(ns))

    for service in deployable:
        secrets = secret_env(service, workspace)
        documents = [
            deployment(service, namespace=ns, secrets=secrets),
            service_manifest(service, namespace=ns),
            config_map(service, namespace=ns, env="prod"),
            autoscaler(service, namespace=ns),
            disruption_budget(service, namespace=ns),
        ]
        if runs_worker(service, workspace):
            documents.append(worker_deployment(service, namespace=ns, secrets=secrets))
        files[f"base/{service.name}.yaml"] = _document(*documents)
        resources.append(f"{service.name}.yaml")

        files[f"base/{service.name}-secrets.example.yaml"] = _document(
            secret_template(service, namespace=ns, secrets=secrets)
        )

    if deployable:
        files["base/ingress.yaml"] = _document(ingress(workspace, namespace=ns, host=host))
        resources.append("ingress.yaml")

    files["base/kustomization.yaml"] = _document(kustomization(resources, namespace=ns))

    for environment in environments:
        replicas = 1 if environment != "prod" else 2
        files[f"overlays/{environment}/kustomization.yaml"] = _document(
            overlay_kustomization(environment, namespace=ns, replicas=replicas)
        )
        patches = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": service.name},
                "spec": {"replicas": replicas},
            }
            for service in deployable
        ]
        if patches:
            files[f"overlays/{environment}/replicas.yaml"] = _document(*patches)

    files["README.md"] = _readme(ns, host, deployable, environments, workspace)
    return files


def _readme(
    namespace: str,
    host: str,
    services: list[ServiceEntry],
    environments: tuple[str, ...],
    workspace: Workspace | None = None,
) -> str:
    rows = "\n".join(
        f"| `{s.name}` | {s.language} | {s.port} | "
        f"{', '.join(f'`{v}`' for v in secret_env(s, workspace)) or '—'} |"
        for s in services
    )
    envs = "\n".join(f"kubectl apply -k overlays/{e}" for e in environments)
    example = services[0].name if services else "api"

    return f"""# Kubernetes — {namespace}

Generated by `jfast deploy k8s`. Regenerate after adding a service; do not
edit `base/` by hand.

## Services

| Service | Language | Port | Datastore variables |
| --- | --- | --- | --- |
{rows}

## Apply

```bash
{envs}
```

## What is generated, and what is not

**Generated:** Deployment, Service, ConfigMap, HorizontalPodAutoscaler,
PodDisruptionBudget per service, plus one Ingress on `{host}` routing `/api`
the same way the local Caddyfile does — so the frontend build is identical in
both places. A service with the `queue` plugin also gets a `<service>-worker`
Deployment: the same image running `jfast worker`, no ports, no HTTP probes,
and 30 s to drain on shutdown (the worker releases what it cannot finish in 25).

**Not generated: databases.** A StatefulSet for PostgreSQL emitted by a
scaffolder is how people lose data — no backups, no point-in-time recovery, no
tested restore, and one `kubectl delete` from gone. The Deployments read a DSN
from a Secret; point it at a managed database, or at an operator someone chose
deliberately.

## Secrets

`*-secrets.example.yaml` holds placeholders. Committing that file is fine;
committing a filled-in one is not. Wire Sealed Secrets, External Secrets or
your cloud's secret manager, then delete the example.

## Probes

Liveness hits `/health` (cheap, probes nothing). Readiness hits `/ready`
(aggregates every plugin's health check). Pointing liveness at `/ready` is the
common mistake: a database blip then restarts every healthy pod at once, and
the restart storm finishes off the database.

A startup probe allows 150s for a slow first boot, so migrations or a cold
cache are not mistaken for a crash.

## Images

`image: <service>:latest` is a placeholder. Set a real registry and an
immutable tag — a digest or a commit SHA. `latest` makes a rollback
meaningless, because there is nothing to roll back *to*.

```bash
cd overlays/prod
TAG=$(git rev-parse --short HEAD)
kustomize edit set image {example}=registry.example.com/{example}:$TAG
```
"""
