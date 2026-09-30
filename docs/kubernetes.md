# Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` asks whether you need it and writes the tree if you say yes.

```
k8s/
├── base/
│   ├── namespace.yaml
│   ├── billing.yaml                  Deployment, Service, ConfigMap, HPA, PDB
│   │                                 (+ billing-worker Deployment with the queue plugin)
│   ├── billing-secrets.example.yaml  placeholders, never real values
│   ├── ingress.yaml
│   └── kustomization.yaml
├── overlays/
│   ├── dev/                          1 replica
│   └── prod/                         2 replicas
└── README.md
```

Kustomize rather than a Helm chart. A chart is right when you ship software
other people install; overlays are right when you deploy your own services to
your own clusters, and they stay readable as plain YAML.

---

## Why this is generatable at all

Because of the [service contract](service-contract.md). Every JFast service, in
any language, exposes `/health` and `/ready`, reads `JFAST_*`, and owns one
port. Those are exactly the facts a Deployment needs — which is why a Go
service and a Python one produce the same manifest shape.

---

## Probes

| Probe | Path | Why |
| --- | --- | --- |
| liveness | `/health` | Is the process up. Probes nothing else. |
| readiness | `/ready` | Aggregates every plugin's health check. |
| startup | `/health`, 150s | A slow first boot is not a crash. |

**Pointing liveness at `/ready` is the mistake to avoid.** The database blips,
readiness fails, and if liveness shares that endpoint the orchestrator restarts
every healthy pod at once. The restart storm then finishes off the database.
Two endpoints exist precisely so that cannot happen.

---

## The worker

A service whose `jfast.toml` enables the `queue` plugin also gets a
`<service>-worker` Deployment in the same file: the API's image, environment
and security context, running `jfast worker --grace 25`. It has no ports and
**no HTTP probes** -- it serves nothing, so a `/health` liveness probe would
restart it forever -- and its own selector, so the API's Service never routes
to it. `terminationGracePeriodSeconds` is 30: on SIGTERM the worker stops
claiming, lets running jobs finish for 25 s, and releases the rest to the queue
without spending an attempt, five seconds before the kubelet's SIGKILL.

No HorizontalPodAutoscaler is generated for it: CPU is the wrong signal for a
queue consumer. Scale it on queue depth (KEDA's PostgreSQL or Redis scaler) or
set the replicas by hand. See
[Queues and events](queues-and-events.md#running-the-worker).

---

## What is deliberately not generated

**Databases.** A StatefulSet for PostgreSQL emitted by a scaffolder is how
people lose data: no backups, no point-in-time recovery, no tested restore, and
one `kubectl delete` from gone. The Deployments read a DSN from a Secret. Point
it at a managed database, or at an operator someone chose on purpose and knows
how to restore from.

**Frontends.** A built SPA is static files. Serve them from the ingress, a
bucket or a CDN — a Node container in production to serve a `dist/` folder is a
process nobody needs.

**Real secrets.** `*-secrets.example.yaml` holds `REPLACE_ME`. Committing that
file is fine; committing a filled-in one is not. Wire Sealed Secrets, External
Secrets or your cloud's secret manager, then delete the example.

---

## Hardening that is on by default

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile: {type: RuntimeDefault}
containers:
  - securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: [ALL]}
```

Retrofitting non-root means rebuilding images across the fleet, so the
generated Dockerfiles already create a uid-10001 user and the manifests assume
it. `readOnlyRootFilesystem` needs somewhere writable, so `/tmp` is an
`emptyDir`.

**Rollouts** use `maxUnavailable: 0`, so capacity never dips during a deploy.
A **PodDisruptionBudget** keeps one replica through a node drain — without it a
drain can take every replica at once and the zero-downtime rollout buys nothing.

---

## Ingress

One host, `/api` in front of the backends — the gateway if the workspace has
one, the single backend if not. Deliberately the same public shape as the
generated `Caddyfile`, so the frontend's production build (`VITE_API_URL=/api`)
is identical locally and in the cluster.

---

## Images

`image: <service>:latest` is a placeholder and must be replaced.

`latest` makes a rollback meaningless: there is nothing to roll back *to*, and
two pods of the "same" version can be running different code. Use a digest or
a commit SHA:

```bash
cd k8s/overlays/prod
kustomize edit set image billing=registry.example.com/billing:$(git rev-parse --short HEAD)
```

---

## After adding a service

```bash
jfast new service reporting --with database
jfast workspace k8s --force
```

Regenerate rather than hand-editing `base/`. Environment-specific changes
belong in an overlay patch, which is what overlays are for.

---

## What is not implemented

- **NetworkPolicies.** Which service may talk to which is a decision about your
  system, not one a generator should guess. Default-deny plus explicit allows
  is the shape to write.
- **ServiceMonitor / PodMonitor.** The `/metrics` endpoint is there; wiring it
  depends on whether you run the Prometheus Operator.
- **Jobs for migrations.** Running Alembic as a pre-deploy Job is the right
  pattern, but ordering it against a rollout safely is application-specific.
- **Helm.** See above.
- **A cluster to test against.** The manifests are validated as YAML and their
  structure is asserted in `tests/test_kubernetes.py`; they have **not** been
  applied to a real cluster in CI. Treat the first `kubectl apply` as the test.
