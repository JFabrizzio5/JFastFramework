# Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` te pregunta si lo necesitas y escribe el árbol si dices que sí.

```
k8s/
├── base/
│   ├── namespace.yaml
│   ├── billing.yaml                  Deployment, Service, ConfigMap, HPA, PDB
│   │                                 (+ Deployment billing-worker con el plugin queue)
│   ├── billing-secrets.example.yaml  placeholders, never real values
│   ├── ingress.yaml
│   └── kustomization.yaml
├── overlays/
│   ├── dev/                          1 replica
│   └── prod/                         2 replicas
└── README.md
```

Kustomize en vez de un chart de Helm. Un chart es lo correcto cuando distribuyes
software que otros instalan; los overlays son lo correcto cuando despliegas tus
propios servicios en tus propios clusters, y se mantienen legibles como YAML
plano.

---

## Por qué esto se puede generar siquiera

Por el [contrato de servicio](service-contract.md). Todo servicio JFast, en
cualquier lenguaje, expone `/health` y `/ready`, lee `JFAST_*` y es dueño de un
puerto. Esos son exactamente los datos que necesita un Deployment — por eso un
servicio en Go y uno en Python producen la misma forma de manifiesto.

---

## Probes

| Probe | Ruta | Por qué |
| --- | --- | --- |
| liveness | `/health` | Si el proceso está vivo. No prueba nada más. |
| readiness | `/ready` | Agrega el health check de cada plugin. |
| startup | `/health`, 150s | Un primer arranque lento no es un crash. |

**Apuntar liveness a `/ready` es el error a evitar.** La base de datos parpadea,
readiness falla, y si liveness comparte ese endpoint el orquestador reinicia
todos los pods sanos de golpe. La tormenta de reinicios termina de matar la base
de datos. Los dos endpoints existen precisamente para que eso no pueda pasar.

---

## El worker

Un servicio cuyo `jfast.toml` activa el plugin `queue` también recibe un
Deployment `<servicio>-worker` en el mismo archivo: la imagen, el entorno y el
contexto de seguridad de la API, corriendo `jfast worker --grace=25`. No tiene
puertos ni **probes HTTP** -- no sirve nada, así que una liveness sobre
`/health` lo reiniciaría para siempre -- y tiene su propio selector, así que el
Service de la API nunca le enruta tráfico. `terminationGracePeriodSeconds` es
30: con SIGTERM el worker deja de reclamar, deja terminar los jobs en curso
durante 25 s y libera el resto a la cola sin gastar un intento, cinco segundos
antes del SIGKILL del kubelet.

No se genera HorizontalPodAutoscaler para él: la CPU es la señal equivocada para
un consumidor de cola. Escálalo por profundidad de cola (el scaler de
PostgreSQL o Redis de KEDA) o fija las réplicas a mano. Ver
[Colas y eventos](queues-and-events.md#correr-el-worker).

---

## Lo que a propósito no se genera

**Bases de datos.** Un StatefulSet de PostgreSQL emitido por un scaffolder es la
manera en que la gente pierde datos: sin backups, sin point-in-time recovery,
sin restore probado, y a un `kubectl delete` de desaparecer. Los Deployments
leen un DSN desde un Secret. Apúntalo a una base de datos administrada, o a un
operator que alguien eligió a propósito y sabe cómo restaurar.

**Frontends.** Una SPA compilada son archivos estáticos. Sírvelos desde el
ingress, un bucket o un CDN — un contenedor de Node en producción para servir
una carpeta `dist/` es un proceso que nadie necesita.

**Secrets reales.** `*-secrets.example.yaml` tiene `REPLACE_ME`. Hacer commit de
ese archivo está bien; hacer commit de uno lleno, no. Conecta Sealed Secrets,
External Secrets o el secret manager de tu nube, y después borra el ejemplo.

---

## El hardening que viene activado por defecto

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

Meter non-root después implica reconstruir las imágenes de toda la flota, así
que los Dockerfiles generados ya crean un usuario con uid 10001 y los
manifiestos lo asumen. `readOnlyRootFilesystem` necesita algún lugar donde
escribir, así que `/tmp` es un `emptyDir`.

Los **rollouts** usan `maxUnavailable: 0`, así que la capacidad nunca baja
durante un deploy. Un **PodDisruptionBudget** mantiene una réplica viva durante
el drain de un nodo — sin él, un drain puede llevarse todas las réplicas de
golpe y el rollout sin downtime no sirve de nada.

---

## Ingress

Un solo host, `/api` delante de los backends — el gateway si el workspace tiene
uno, el único backend si no. A propósito con la misma forma pública que el
`Caddyfile` generado, para que el build de producción del frontend
(`VITE_API_URL=/api`) sea idéntico en local y en el cluster.

---

## Imágenes

`image: <service>:latest` es un placeholder y hay que reemplazarlo.

`latest` hace que un rollback no signifique nada: no hay *a dónde* volver, y dos
pods de la "misma" versión pueden estar corriendo código distinto. Usa un digest
o un SHA de commit:

```bash
cd k8s/overlays/prod
kustomize edit set image billing=registry.example.com/billing:$(git rev-parse --short HEAD)
```

---

## Después de agregar un servicio

```bash
jfast new service reporting --with database
jfast workspace k8s --force
```

Regenera en vez de editar `base/` a mano. Los cambios específicos de un entorno
van en un patch de overlay, que es para lo que existen los overlays.

---

## Lo que no está implementado

- **NetworkPolicies.** Qué servicio puede hablar con cuál es una decisión sobre
  tu sistema, no una que un generador deba adivinar. Default-deny más allows
  explícitos es la forma a escribir.
- **ServiceMonitor / PodMonitor.** El endpoint `/metrics` está ahí; conectarlo
  depende de si corres el Prometheus Operator.
- **Jobs para migraciones.** Correr Alembic como un Job de pre-deploy es el
  patrón correcto, pero ordenarlo contra un rollout de forma segura depende de
  cada aplicación.
- **Helm.** Ver arriba.
- **Un cluster contra el cual probar.** Los manifiestos se validan como YAML y
  su estructura se verifica en `tests/test_kubernetes.py`; **no** se han
  aplicado a un cluster real en CI. Trata el primer `kubectl apply` como la
  prueba.
