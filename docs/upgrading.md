# Upgrading a project

```bash
jfast upgrade --check          # what breaks, for THIS project
jfast upgrade --check --json   # the same, for an agent or a CI step
```

Compares the framework version your project pins against the one installed, and
reports the changes in between **that this project can actually feel**.

---

## What it is not

It is not the changelog. `CHANGELOG.md` says what changed; the person doing the
upgrade needs to know what changes *for them*, and twenty release notes with no
way to tell which three apply is a list nobody reads twice.

So the command does not parse the changelog — it could not if it wanted to. The
breaking changes are declared as data inside the package, in
`jfastframework/upgrades.py`, each with a `detect` that inspects your project on
disk. Three reasons that file is the source and prose is not:

- **Prose is not a data format.** `### Breaking` is a heading today. Rename it
  to `### Breaking changes` and the parser reports that nothing broke — and
  reports it confidently.
- **The changelog does not ship.** The wheel contains
  `packages = ["src/jfastframework"]` and nothing else, so an installed
  framework has no changelog to read.
- **A release note is not a finding.** "Timestamps are timezone-aware" is a
  sentence. `ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz
  USING created_at AT TIME ZONE 'UTC'` is a thing you can run.

---

## The rule that makes it worth reading

**A change this project cannot be affected by is not printed.**

That is not politeness, it is the whole design. One warning that does not apply
teaches the reader that the output is padding, and the next warning — the one
that mattered — is skipped along with it.

So each entry carries a `detect` that returns the evidence found in *your*
tree:

| Change | Reported only when |
| --- | --- |
| `timestamps-timezone-aware` | a model class carries `TimestampMixin` |
| `contracts-shared-import` | a layer in `contracts.toml` omits `"shared"` |
| `contracts-layout-mismatch` | a layer's `paths` match no module's recorded layout |
| `refresh-tokens-rejected` | `auth` is enabled **and** `issue_tokens = true` |
| `logout-ends-one-session` | same |
| `token-store-rotate-refresh` | some class of yours defines `rotate_refresh` |
| `pagination-total-optional` | some file calls `paginate` or `paginate_keyset` |
| `access-token-fam-claim` | `auth` is enabled **and** `issue_tokens = true` |
| `refresh-grace-seconds` | same, **and** `refresh_grace_seconds` is unset |
| `request-limit-defaults` | `jfast.toml` does not set the limit itself |
| `cli-exit-codes` | always — see below |

A service that only *validates* somebody else's tokens is untouched by every
change to the issuing endpoints, which is most services with `auth` on. It is
never told about them.

`cli-exit-codes` is the exception, and it is honest about being one: nothing in
a project says whether its pipeline branches on an exit code, so the entry is
marked informational and states the change unconditionally rather than guessing.

### The mixin is resolved per class, not per file

The table names in the `timestamps-timezone-aware` remedy come from each
model's `__tablename__`, read with `ast` without importing the module. Both
`__tablename__ = "invoices"` and the annotated `__tablename__: str = "invoices"`
count; a class that computes its name at runtime is reported as *carries
`TimestampMixin`, declares no `__tablename__`* rather than left out, and a class
marked `__abstract__ = True` owns no table and is skipped.

Which classes carry the mixin is decided **per class**. A models file routinely
holds both the tables that mix the timestamps in and projection or view tables
that do not, and an `ALTER` naming `created_at` on a table without one is not a
warning:

```
ERROR:  column "created_at" does not exist
```

That aborts the revision where it stands — after every `ALTER` before it has
already taken `ACCESS EXCLUSIVE` and rewritten its own table. Base classes are
followed by name across the whole project, so a model that reaches the mixin
through a base declared in `shared/` is found too.

---

## What it reports

```
  0.1.0a3 → 0.1.0a4   (pinned in requirements.txt)

  ✗ timestamps-timezone-aware  [breaking, 0.1.0a4]
      TimestampMixin columns are timezone-aware. Existing tables need a migration.

      created_at and updated_at mapped to TIMESTAMP WITHOUT TIME ZONE,
      so a row serialised as 2026-08-29T20:55:15 with no offset and
      every JavaScript client read it as local time.

      in this project:
        modules/invoice/models.py  ->  invoices
          ALTER TABLE invoices
              ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
              ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';

      → fix
        Write the statements above into an Alembic revision by hand.
        The USING clause is load-bearing and autogenerate omits it.

  ✗ contracts-shared-import  [breaking, 0.1.0a4]
      ...
      in this project:
        [layers.http]  may_import = ["service", "schemas", "storage"]  ->  add "shared"

  7 apply here, 5 breaking.
```

### The `USING` clause is not decoration

Alembic's autogenerate writes the bare form:

```sql
ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz;
```

That does not fail. It converts through the implicit cast, which reads every
stored value in the **server's** `TimeZone`, and silently shifts the whole table
on any server not set to UTC. The migration this command prints is the one that
does not:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

---

## Which version the project is on

Two places say it, and they are checked in this order:

1. `requirements.txt` — the `jfastframework[...]==X` line. This wins, because
   it is what `pip` acts on: a project upgraded by editing that line and
   reinstalling is on the new version whatever else on disk still remembers.
2. `.jfast-template` — the stamp every scaffold leaves, recording the framework
   version that generated the tree. The fallback for a project with no
   requirements file.

Neither present is a `2` (configuration error), not a silent success: the
command refuses to report on a version it had to guess.

Comparison is PEP 440-aware, so `0.1.0a10` is newer than `0.1.0a9` — which is
not what string comparison says. `packaging` is **not** a dependency of this
framework, direct or transitive, so the ordering is vendored in
`upgrades.parse_version` rather than imported; the test suite pins it against
the real `packaging`, which is installed for development.

---

## What changes in `0.1.0a12`

Fixes, and one of them is a security fix that changes answers:

- `unsigned-tenant-needs-a-session` -- `auth` and `tenancy` on, with
  `subdomain`, `path` or `header` among the sources (the plugin's default, and
  what `jfast new service --with auth,tenancy` writes). Those sources no longer
  grant a tenant by themselves: a request on `acme.` without a session is a 401
  where `current_tenant` used to hand it acme's rows, and a token whose tenant
  disagrees with the subdomain -- or carries none -- is a 403. Routes that use
  `current_tenant` need nothing; a page public on purpose takes
  `Depends(requested_tenant)`; a service whose tokens carry no tenant and that
  checks membership itself sets `[plugin.tenancy] trust_unscoped_principals =
  true`. See [multitenancy](multitenancy.md#with-auth-on-an-unsigned-source-never-grants-a-tenant-by-itself).

- `jfast-env-wins-over-the-file` -- `[app] env` (or `debug`) in `jfast.toml`,
  which every project `jfast start` generated has. `JFAST_ENV` in the process
  environment now wins over it, so a deployment that sets `JFAST_ENV=prod`
  finally runs as production: `/docs`, `/info` and `/queue/stats` close, and a
  plugin left on a development backend (console mail) refuses to start. Delete
  the line, set `JFAST_ENV=prod` under compose's `environment:`, and read the
  boot log for the `overridden by JFAST_ENV` warning.

Nothing else stops a correct `0.1.0a11` service. Three notes point at files
`0.1.0a11` generated that need a hand:

- `image-cannot-write-local-storage` -- a service with `storage` whose
  Dockerfile runs as `appuser` without owning `/app` (the image stops at boot),
  or without creating a local disk root other than `public` and `private`
  (`/ready` 503, uploads 500). `jfast deploy dockerfile` regenerates it from
  `[plugin.storage.disks]`; a volume already created as root needs one `chown`
  ([Storage](storage.md#a-volume-created-as-root)).
- `facade-tenant-optional` -- a multitenant service whose `public.py` facades
  take `tenant_id: str | None`: None reads every tenant. Make it `tenant_id:
  str` and let mypy name the callers.
- `unique-key-on-optional-field` -- a `--unique` key over a `?` field, still a
  `NULLS NOT DISTINCT` constraint: the second row without a value is a 409.
  Replace it with the partial index the note quotes, in a migration.

## What changes in `0.1.0a11`

Nothing in this release stops a correct `0.1.0a10` service from booting. What
breaks is behaviour that used to fail quietly and now fails loudly, plus a few
settings the plugins refuse at boot. The real migration of a four-module
service (a receipts SaaS on PostgreSQL, Redis, accounts and a queue) went like
this, and it is the order to follow.

**1. Move the pin, then read the report.** Edit `requirements.txt` to
`0.1.0a11` *after* running `jfast upgrade --check` against the old pin -- the
report reads the version from that line. With the new extras installed, run
`jfast check`: its seventh check, `tenancy`, is new.

**2. Replace tasks queued by name with an event.** `contracts check` now sees
`Job(task="alerta.revisar_presupuesto")` in another module as a call into it
(`undeclared-dependency`, and a `module-cycle` if the other module reads back).
The handler may live in a root `worker.py`: a task name's `<module>.` prefix
names its owner.

```python
# before -- modules/comprobante/services/comprobante_service.py
await outbox.enqueue(session, Job(task="alerta.revisar_presupuesto", payload=...))

# after
await outbox.publish(session, "comprobantes", Event(type="comprobante.registrado", data=...))
```

```python
# modules/alerta/tasks.py
@subscribe("comprobante.registrado")
async def revisar_presupuesto(event: Event, session: TaskSession) -> None:
    ...  # runs as the publishing tenant; committed on return
```

```toml
# contracts.toml
[modules.comprobante]
publishes = ["comprobante.registrado"]
```

Without a subscriber and without an event bus, `outbox.publish` now raises
`UndeliverableEvent` (a 500 naming the fix) instead of answering 201 and
retrying the row until it died. `publish-without-receiver` finds those calls.

**3. Delete `worker.py`; run `jfast worker`.** Handlers belong in
`modules/<name>/tasks.py` (`@task`, `@subscribe`), a `TaskSession` replaces the
session, commit and tenant check written by hand, and `jfast worker` drains on
SIGTERM. `jfast dev` starts it. Regenerate deployments (`jfast deploy compose`,
`jfast workspace compose`, `jfast workspace k8s`): they gain a worker service.
Copy the `[layers.tasks]` block into `contracts.toml` so tasks files have a
layer (`contracts-tasks-layer`).

**4. Check the defaults that changed.** Four are on by default and reported as
behaviour: sign-in rate limits with `cache`
(`accounts-sign-in-rate-limit`), token revocation that fails open when Redis
is down (`revocation-fail-open`), a 1 s deadline per Redis command
(`redis-command-timeout`), and database connectivity errors answering 503
instead of 500 (`database-unavailable-503`). Each note says how to keep the
old behaviour.

**5. Settings refused at boot.** `settings-refused-at-boot` lists values the
plugins now reject: `pool_size = 0`, a non-IANA `session_timezone`, a storage
`visibility` that is neither public nor private, an `access_key` without its
secret, and more. It reads `jfast.toml` only; values from the environment are
checked when the service starts.

**6. Accounts clients.** With `email_verification = "required"`, existing users
are unverified until the grandfathering `UPDATE` in the note runs. With
`mfa = true`, `/auth/login` can answer a challenge instead of tokens. A
generated frontend that reads the user from the login response has to call
`/auth/account` after signing in; `jfast upgrade --check` finds it next to the
service.

**`jfast add` and your pin.** `jfast add <plugin>` edits `requirements.txt`
and runs pip -- except when the pin is not the version you are running, or the
install is editable. Then it prints the command instead: installing the old pin
would replace the framework you are on.

## The one that stops a boot in `0.1.0a9`

### A session that commits after the response

```
PluginError: these routes open a database session that would commit after the
response is sent ...
  POST /invoices -> session_dependency
```

Before: a commit that failed -- a deferred constraint, a serialisation
failure, a connection dropped at the wrong moment -- had already been answered
`201`, and a client that read its own write straight away could arrive before
the commit. FastAPI runs a `yield` dependency's teardown after the response
unless the dependency is function-scoped, and the session commits in its
teardown. Every module `jfast new module` generated before `0.1.0a9` wires it
that way.

The fix is one line per dependency:

```python
# before
def get_service(request: Request, session=Depends(session_dependency)) -> Service: ...

# after
from jfastframework.plugins.builtin.database import DbSession

def get_service(request: Request, session: DbSession) -> Service: ...
```

`ReadSession` replaces `read_session_dependency` and `TenantSession` replaces
`tenant_session_dependency`. `Depends(session_dependency, scope="function")` is
the same thing spelled out. A generator dependency of your own that wraps a
session has to be function-scoped as well -- FastAPI refuses the other order.

`jfast upgrade --check` lists every line as `session-commits-after-response`.
The framework also needs FastAPI 0.121 or later now; `pip install -U` takes
care of it. [Transactions](transactions.md) explains the rest.

## The three that stop a boot in `0.1.0a8`

Everything the command reports has a `remedy` in its own output, so this
section exists for one reason: these three refuse to *start*, and a person
reading a stack trace at deploy time wants the fix on one page rather than in
the report they did not run.

All three started **broken** before. Each failed with no error to find — the
symptom arrived days later, from a customer or from a log nobody was reading.
Refusing at boot is the fix: a service that will not start is a rollback that
takes a minute, and a service that starts and silently drops every email is a
week.

Every one of them is decidable from the files on your laptop. Run
`jfast upgrade --check` before the deploy, not after.

### `auth` mints tokens and nothing shared records them

Before: users logged out at random, and refreshes answered `401 "this session
has been revoked"` for a session nobody revoked.

The token store is in memory unless `cache` is on, and memory is per process
while the image runs one worker per CPU. A logout revoked on the worker that
served it and nowhere else; a refresh sent to any other worker found no family
and was refused. Three requests in four on four cores.

```bash
pip install "jfastframework[cache]"
```

```toml
[plugins]
enabled = ["observability", "metrics", "database", "cache", "auth"]
```

```bash
jfast deploy compose -o docker-compose.yml   # Redis arrives with the plugin
```

A service that only *verifies* tokens somebody else minted holds no session of
its own, and needs nothing shared:

```toml
[plugin.auth]
issue_tokens = false
```

### The mail backend delivers nothing

Before: verification links, password resets and invoices reported as sent and
never received.

`console` is the default and the right default — nobody emails a real customer
from a laptop. In production it printed each message to stdout while `send`
returned successfully: no bounce, no error, no queue backing up.

```toml
[plugin.mail]
backend = "smtp"
host = "smtp.example.com"
port = 587
from_email = "noreply@example.com"
```

```bash
# in the deployed environment, never in jfast.toml
JFAST_MAIL_USERNAME=...
JFAST_MAIL_PASSWORD=...
```

Local runs are untouched: the refusal applies at `env = "prod"` only. If this
service sends no mail, drop the plugin from `[plugins].enabled`.

### A worker may run a handler past the claim protecting it

Before: a job running twice — a charge taken twice, an email sent twice — with
nothing in either run to say it happened concurrently.

A claim is invisible to other workers for `visibility_timeout` and nothing
extends it while a handler runs, so a job that outlives the window is claimed
again while the first run is still inside it. Both numbers defaulted to 300
seconds, in different files.

Drop the argument and the worker derives one from the backend:

```python
worker = Worker(backend, registry)      # 80% of the window, room for the nack
```

If a handler genuinely needs longer, raise the window and stay under it:

```toml
[plugin.queue]
visibility_timeout = 1800
```

```python
worker = Worker(backend, registry, job_timeout=1200)
```

RabbitMQ is unaffected: it redelivers on the connection rather than on a clock,
so there is no window to run inside.

### Two more that need no edit

**The pool default is 5 + 5, was 10 + 20.** Pool numbers are per process and
the image runs one worker per CPU, so the old pair was 240 connections from a
single service against a PostgreSQL that accepts 100. Say so if your server is
bigger:

```toml
[plugin.database]
pool_size = 10
max_overflow = 20
server_max_connections = 500
```

`jfast check` fails when `(pool_size + max_overflow) × workers` passes
`server_max_connections`. `0` turns that check off.

**`jfast check --only <name>` no longer exits 0 when what it needed failed.**
A skip for an *absence* — no `contracts.toml` here — is still exit 0.

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | nothing between those versions affects this project |
| `2` | no `jfast.toml`, or no pin to compare against |
| `6` | `--apply` was passed |
| `7` | something applies, or the project pins a version newer than the installed one |

`7` is `COMPATIBILITY`: the project and the installed framework do not agree.
Gate a deploy on it.

---

## `--apply` does not exist

Not "not yet in this build" — not planned for this release, on purpose.

Automatic rewriting of somebody's models, contract and settings needs a rollback
story: a clean tree to start from, a diff to review, a way back when the rewrite
is wrong. None of that exists here, and an automatic edit that is wrong costs
more than the manual one it saved. Passing `--apply` says so and exits `6`.

The report names the file and the line for every finding. Make the edits.

---

## Adding a change to the manifest

When a release breaks something, add a `Change` to `CHANGES` in
`jfastframework/upgrades.py`:

```python
Change(
    version="0.1.0a5",
    kind="breaking",              # breaking | deprecated | behaviour
    code="stable-identifier",     # what --json emits; never reuse one
    summary="One line. What broke.",
    detail="Why it broke and what the silent failure looked like.",
    detect=_something_on_disk,    # None only when nothing can decide it
    remedy="What to do, concretely.",
)
```

`detect` takes a `jfastframework.project.Project` and returns the evidence
strings — table names, layer names, the values a setting is about to acquire.
An empty list means the project is unaffected and the entry is not printed.

`Project` is read from the filesystem and never imports project code, so the
report works on a service that is broken, half-migrated, or missing its
dependencies entirely. It carries the plugin names but not their settings; read
`jfast.toml` directly when a change depends on one, as the `auth` entries do.

Write the entry with `detect=None` only when nothing on disk can decide the
question, and say so in `detail`. It is the difference between an honest
informational note and a warning people learn to ignore.

Then give it two tests in `tests/test_upgrade_detectors.py` -- a project it
flags and the closest one it must not -- and register both in `AFFECTED` and
`CLEAN`. The file's last test compares those against `CHANGES`, so an entry
without them fails the suite.

## The upgrade smoke

`scripts/smoke_upgrade.sh` walks the path a user walks, on every change:

1. a virtualenv with the **previous** release from PyPI;
2. a project generated by it -- a module in every layout, and the plugins
   `auth`, `tenancy`, `metrics`, `rag` and `queue`;
3. this checkout's wheel installed over it;
4. `jfast upgrade --check --json`, whose change codes must be **exactly** the
   ones in `scripts/smoke_upgrade.expected` -- a missing one means a detector
   stopped seeing its case, an extra one means it started seeing one that is
   not there;
5. the remedy for each code, scripted in the smoke as the entry's `remedy`
   describes it;
6. the app imported and `GET /health` answered in-process;
7. the project's own `pytest` and `jfast check --ci`.

The claim it keeps honest is the one this page makes: that `upgrade --check`
names everything a project has to change. A break it does not name shows up at
step 6 or 7 with nothing at step 4 -- which is how it found that 0.1.0a10's
hexagonal template fails its own test (its package init imports FastAPI
through `CreatePayload`).

```bash
PY=python3.12 scripts/smoke_upgrade.sh              # previous = this version minus one
PREVIOUS=0.1.0a9 scripts/smoke_upgrade.sh           # or name it
KEEP_WORK=1 scripts/smoke_upgrade.sh                # leave the project to inspect
```

A new `Change` that applies to the generated project needs its code in the
expected file and a `case` in the smoke's `remedy()`; the smoke fails and says
so until both are there.
