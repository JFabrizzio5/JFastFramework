# Contracts

`AGENTS.md` says what to do. A contract says what is **allowed**, and something
checks it.

That difference is the whole point. An agent generating code at speed will
drift past a suggestion without noticing — not out of malice, but because
nothing pushed back. A contract pushes back:

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'  (Data access. No business rules.)

1 violation(s). Fix them, or waive one inline with
    # contracts: allow <reason>
```

Non-zero exit. In CI, that is a failed build.

---

## The three audiences, one file

`contracts.toml` sits at the root of a service, written by its **first**
`jfast new module` with the layer paths of that module's layout.

| Audience | Reads it as |
| --- | --- |
| The build | `jfast contracts check` — fails on a violation |
| An agent | `jfast contracts show --json` — before writing a line |
| A human | `CONTRACTS.md` — generated, for review |

One source, three renderings, so the document and the enforced rule cannot
disagree.

Not written by `jfast new service`, and that is deliberate. A service is
generated before any module exists, so there is no layout to write a contract
for — and the guess made there used to be the layered one, always. In a
hexagonal, modular or screaming service its globs matched no file on disk, so
every layer rule applied to nothing while `contracts check` reported a pass.

A service that has no module yet, and wants the service-wide rules — forbidden
calls, event-loop safety — can write one with `jfast contracts init`.

### More than one layout in a service

A service may hold modules of several layouts; that is the point of a modular
monolith. The contract holds the layer paths of **one** of them, the first, and
a module in a second layout matches no layer glob — so only the service-wide
rules reach it. Add its paths to `contracts.toml` yourself. `contracts check`
catches the wholesale case, where *no* file matches a layer, and cannot catch
the mixed one, because the first layout's layers still match their own modules.

---

## What you declare

### Scope

```toml
[project]
name = "billing"
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."
```

`does_not_own` is the more useful half, and the one people skip. Most bad code
in a growing system is a service quietly expanding into something another
service already owns — and it never looks wrong from inside that service.

### Layers

```toml
[layers.domain]
description = "Entities and their rules. Framework-free."
paths = ["modules/*/[!_]*.py"]
may_import = ["shared"]
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain", "shared"]
```

`may_import` names **other layers**, not packages. Layers are what a reviewer
argues about; package names are what they forget.

A file is classified by the **most specific** matching pattern — fewest
wildcards, not longest string. That distinction is load-bearing:
`modules/*/[!_]*.py` is longer than `modules/*/http.py`, and ranking by length
would classify every router as domain code and then reject its imports for a
reason nobody could work out.

#### `*` stops at `/`. `**` crosses it.

A layer glob is a statement about *where in the tree* a file sits, so the
separator is a real boundary:

| Pattern | Matches | Does not match |
| --- | --- | --- |
| `modules/*/repository.py` | `modules/invoice/repository.py` | `modules/invoice/infrastructure/repository.py` |
| `modules/**/repository.py` | both of the above | — |
| `modules/*/[!_]*.py` | `modules/invoice/invoice.py` | `modules/invoice/tests/test_invoice.py`, `modules/invoice/__init__.py` |

`**/` also matches *zero* directories, so `modules/**/http.py` covers
`modules/http.py` too.

This is deliberately **not** `fnmatch`, which translates `*` to `.*` and lets it
walk straight through a separator. Under that reading the layered contract's
`storage` layer claimed a hexagonal project's
`modules/*/infrastructure/repository.py`, counted as governing something, and
`layer-unmatched` — the finding whose entire job is to catch a contract that
governs nothing — stayed quiet about a contract that governed almost nothing.
The screaming catch-all was the same story one directory further in:
`modules/*/[!_]*.py` swallowed every `modules/*/tests/*.py`, so the domain
layer's `forbid_packages` was applied to test files.

Matching is case-sensitive on every platform, on purpose. `fnmatch` folds case
on Windows, and a rule that answers differently by operating system is not a
rule.

#### `shared` is on every list, and on none of its own

Every generated contract declares a `shared` layer for `shared/*.py`, and every
other layer may import it — the domain included. That is not a loosening: it is
what makes the advice in [shared/, enums, and channels](shared-and-events.md)
legal. `[rules.placement]` tells you to move the twice-wanted enum into
`shared/`, and a layer that could not import `shared/` had no way to obey the
instruction the checker itself printed.

It stays safe because `shared` keeps `may_import = []` and forbids `sqlalchemy`
and `fastapi` on itself. Nothing can reach a database or a router through an
enum, and the direction stays one-way — which `[rules.placement]` checks from
the other side.

#### `public` is each module's door

Every generated contract also declares a `public` layer for
`modules/*/public.py`: the facade other modules call. It may import its own
module's layers — a read may go straight to storage, a write goes through the
service — and nothing inside the module may import it back.

Which layer of *another* module calls a facade is not a layer question. An
import of `modules.<other>.public` from a different module skips the layer
check and is governed by `[modules.*]` and `[rules.placement]` instead, below:
the layer rules describe the inside of one module, and the facade is the edge
of another. In the screaming layout, `public` is more specific than the
domain's catch-all `modules/*/[!_]*.py`, so the facade is not held to the
domain's rules.

### Between modules

Queries through a facade, effects through events, nothing through `shared/`.
[Services, modules and layouts](modules.md#communication-between-modules) walks
through a full example; this is what the checker holds you to.

```toml
[modules.asesor]
depends_on = ["comprobante"]    # asesor may call modules/comprobante/public.py
```

```python
# modules/comprobante/public.py
@dataclass(frozen=True, slots=True)
class CategorySpending:
    category: str
    total_cents: int

async def spending_by_category(session, *, tenant_id: str, since: date) -> list[CategorySpending]: ...

# modules/asesor/services/asesor_service.py
from modules.comprobante.public import spending_by_category
```

A module with no `[modules.<name>]` block depends on nothing. `jfast new module`
appends an empty one for each module it generates.

| Rule | Reported when | What it says |
| --- | --- | --- |
| `cross-module` | a module imports anything of another module other than its `public.py` | `module 'asesor' imports modules.comprobante.services; import modules.comprobante.public instead` — and, if that file does not exist, to create it with a function returning DTOs |
| `undeclared-dependency` | it imports `modules.<other>.public`, or queues `Job(task="...")` for a task `<other>` declares with `@task`, without `<other>` in `depends_on` | `module 'asesor' calls modules.comprobante.public but does not declare 'comprobante' in depends_on` |
| `module-cycle` | the graph of declared `depends_on` plus actual facade imports and task references has a cycle | `module dependency cycle: asesor -> comprobante -> asesor`, once per cycle |
| `unused-dependency` | a `depends_on` entry names a module this one never imports nor queues a task of | `[modules.asesor] depends_on lists 'cartera', but module 'asesor' never calls modules.cartera.public or queues one of its tasks` -- reported at the line in `contracts.toml` |
| `public-leak` | `public.py` imports or re-exports an ORM entity (any class in that module whose body assigns `__tablename__`), or imports `fastapi`/`starlette` | `modules/comprobante/public.py imports the ORM entity Comprobante` |
| `cross-module-sql` | a string in `modules/<here>/` holds SQL naming a table another module owns | `module 'asesor' queries 'comprobantes' (module 'comprobante') with raw SQL` |
| `unknown-dependency` | a `[modules.x]` block or a `depends_on` entry names no module under `modules/` -- usually a typo | `[modules.asesor] depends_on names 'comprobantes', which is not a module under modules/` |
| `shared-direction` | `shared/` imports a module | `shared/ imports modules.invoice` |

Two cases keep the old advice. An imported **enum or types** module
(`modules.x.enums`, `modules.x.domain.enums`, `modules.x.types`) is vocabulary,
so `cross-module` still names the `shared/` file to move it to. Anything else —
a service, a repository, an entity — is behaviour, and `shared/` is the wrong
answer for it: the message points at the owner's `public.py`.

Why each rule exists:

- **The facade, not `shared/`.** `shared/` is vocabulary: enums, types, pure
  functions. Behaviour moved there to dodge `cross-module` is a repository two
  modules share, which is two modules sharing a table.
- **No raw SQL.** `text("SELECT ... FROM comprobantes")` inside `asesor` is the
  coupling an import would have been, minus anything that sees it. Ownership
  comes from `__tablename__`: a table declared under `modules/comprobante/`
  belongs to `comprobante`, and only tables owned by a *different* module are
  reported. Found in string literals, including the constant parts of
  f-strings; docstrings are skipped.
- **`depends_on` is declared.** The graph is then a reviewed decision rather
  than whatever the imports add up to, and a cycle is visible in
  `contracts.toml` before it is built. Break one by turning a direction into an
  event — the downstream module subscribes instead of being called back.
- **DTOs and no HTTP in `public.py`.** The facade is called from workers and
  other modules, not only from a request; an entity carries its session and
  every column across the boundary.

Every one of these honours the inline waiver, and `[rules.placement] enabled =
false` turns all of them off together -- the event rules below included. The
facade's file name is fixed at `public.py`. `jfast inspect` reports
`module-cycle` from the same graph -- imports plus declared `depends_on` -- so
the two commands cannot disagree about whether a project has a cycle.

### Events and tasks

The other way across a boundary is an event, and it is part of the contract
too. A module declares the event types it publishes; subscriptions and task
ownership are read from the code:

```toml
[modules.comprobante]
depends_on = []
publishes = ["comprobante.registrado"]
```

```python
# modules/alerta/tasks.py
@subscribe("comprobante.registrado")
async def revisar_presupuesto(event: Event, session: TaskSession) -> None: ...
```

`alerta` does **not** declare `depends_on = ["comprobante"]` for this: a
subscriber depends on nothing, which is the point. See
[Queues and events](queues-and-events.md#events-between-modules) for how the
event is delivered.

| Rule | Reported when | Fix |
| --- | --- | --- |
| `orphan-subscription` | a `@subscribe("<type>")` names an event no module declares under `publishes` | declare it in the publishing module, or fix the name. An event from another service arrives over Kafka: use `@on(topic)` for it |
| `undeclared-event` | `Event(type="<type>")` is built in a module whose block does not list the type under `publishes` | add it to that module's `publishes` |
| `undeclared-dependency` | `Job(task="alerta.revisar")` is queued from a module other than the one whose `@task` declares it | publish an event and `@subscribe` to it instead -- or declare the dependency |

Only string literals are read -- a type or task name built at run time is not
guessed at -- and a module's `tests/` are skipped. Queuing another module's
task by name is the hidden cycle this catches: `comprobante` queuing
`alerta.revisar` while `alerta` reads `comprobante`'s facade passed every check
before, and is now `undeclared-dependency`, and `module-cycle` once declared.

`jfast contracts show --json` carries `events` (each type with its declared
publishers, the modules that build it, and its subscribers) and `tasks` (each
task, its owner and who queues it); `CONTRACTS.md` renders both as tables, and
`jfast ai context` adds each module's facade functions, `publishes`,
`subscribes` and `tasks`.

### Forbidden calls

```toml
[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py", "config/*.py", "migrations/env.py"]
why = "Configuration is typed. Add a field to a settings model so a bad value fails at boot."
```

`why` is not decoration. It is what the checker prints, and the difference
between someone fixing the cause and someone deleting the line.

A dotted pattern also catches the bare import, so `from os import getenv` does
not slip through. A bare pattern matches exactly, so forbidding `print` does
not also flag `report.print()`.

### Required structure

```toml
[[rules.require]]
path = "tests"
applies_to = "modules/*"
why = "A module with no tests is a module nobody can change safely."
```

### Event-loop safety

The hardest bug in an async service is the one that never raises. A blocking
call inside `async def` stops every other request on that worker for its
duration, and the symptom arrives as latency on endpoints that have nothing to
do with the cause. Nothing in the traceback, nothing in the log, and a profile
of the slow endpoint points at code that is innocent.

So it is a contract rule, checked on every build:

```toml
[rules.async_safety]
enabled = true
naive_datetime = true
allow_in = ["tests/*", "conftest.py", "scripts/*", "migrations/*"]
follow_local_helpers = true

[rules.async_safety.extra_blocking]
"myapp.legacy.render_pdf" = "await asyncio.to_thread(render_pdf, ...)"
```

| Key | Default | Turns off |
| --- | --- | --- |
| `enabled` | `true` | the whole table: `async-blocking` **and** `naive-datetime` |
| `naive_datetime` | `true` | `naive-datetime` only, leaving `async-blocking` on |
| `allow_in` | `["tests/*", "conftest.py", "scripts/*", "migrations/*"]` | checking under those paths. Replaces the default list, never adds to it |
| `follow_local_helpers` | `true` | following a synchronous helper in the same file into its async callers |

Two rules share this table, and only one of them is about the event loop.
`naive-datetime` — see [Time zones](timezones.md#the-contract-check) — rides
here because this was the only rules table the contract model had, so it gets
its own switch: a project silencing it would otherwise silence the async check
with it, and the one it did not mean to silence is the one that was working.
`enabled = false` still turns off both, because that is what "this table is
off" has to mean.

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
  (every other request on this worker waits. Use httpx.AsyncClient, already a dependency of gateway and auth)
blocking_demo.py:13: async-blocking: warm_cache() is synchronous and calls time.sleep(), which blocks the event loop
  (make the helper a coroutine, or offload it with asyncio.to_thread)
```

Three things it finds that a general-purpose linter does not:

* **Clients on `self`.** `self._s3 = boto3.client("s3")` in `__init__`, then
  `self._s3.put_object(...)` in an async method four screens away. The call is
  a method on an instance, which is invisible to a rule that reads one function
  at a time.
* **One hop of indirection.** The blocking call is rarely in the handler; it is
  in the synchronous helper the handler calls. Within a file, that helper is
  followed into its callers.
* **Your own code.** `extra_blocking` is where the team writes down the
  functions only it knows about, with the replacement to reach for.

It knows `boto3`, `pymongo`, `psycopg2`, the synchronous `redis` client and
`sqlite3` by name, plus the standard-library cases: `time.sleep`,
`subprocess`, `requests`, blocking `pathlib` I/O, `open()`, and `asyncio.run`
or `run_until_complete` inside a coroutine.

**What it will not do**, on the same principle as the rest of the checker:

* A synchronous `def` handler is not reported. FastAPI runs it in a threadpool;
  that is a supported way to write a route, not a bug.
* Work handed to `asyncio.to_thread`, `run_in_executor`,
  `anyio.to_thread.run_sync` or `run_in_threadpool` is correct code and is left
  alone -- including the synchronous closure you pass to it, which is why
  `storage/s3.py` reports nothing.
* A call it cannot resolve through the file's imports is not reported.
  `self._client.ping()` could be anything, and a checker that guessed would
  flag every `ping` in the codebase.
* Nothing crosses a file boundary. Resolving a name to a definition in another
  module is a type checker's job.

If you also run ruff, enable its `ASYNC` ruleset -- it covers the
standard-library cases independently. This framework does, and turning the rule
on found two blocking `Path.is_dir()` calls in its own readiness probe.

Waive one when blocking really is right:

```python
time.sleep(0)  # contracts: allow one-off at startup, not per request
```

### Interfaces

```toml
[[provides]]
name = "invoices-api"
kind = "http"
path = "/invoices"
stability = "stable"      # experimental | stable | deprecated

[[consumes]]
name = "catalog"
via_env = "API_CATALOG_URL"
```

Written down so that changing a `stable` interface is a visible decision
rather than a surprise for whoever depended on it.

### Invariants

```toml
[invariants]
rules = [
  "Money is stored in minor units as an integer. Never a float.",
  "A job handler is idempotent: delivery is at-least-once.",
]
```

The checker cannot verify these. They are here precisely because nothing else
will catch them — this is the list a reviewer, or an agent, checks by hand.

---

## Waivers

```python
from sqlalchemy import text  # contracts: allow one-off reporting query, JF-412
```

The reason is required. A waiver is a decision; `jfast contracts waivers`
lists every one, because decisions nobody revisits are exactly how a contract
stops meaning anything.

---

## Why a rule exists: `contracts explain`

`contracts check` says a rule was broken. It does not say *why the rule is
there*, and an agent handed a violation with no remedy tends to satisfy the
checker rather than fix the design — by deleting the import, copying the code
into the second module, or turning the rule off. `explain` closes that:

```bash
jfast contracts explain billing analytics          # may billing import analytics?
jfast contracts explain http sqlalchemy            # may the http layer import it?
jfast contracts explain --rule shared-direction    # what is that rule, and where
jfast contracts explain --file modules/billing/service.py
jfast contracts explain --json
jfast contracts explain                            # every rule that can fire here
```

```
may module 'asesor' import module 'comprobante'?  [FORBIDDEN]

  rule     cross-module  -- one module imported something of another module other than its
           public.py
  what     'asesor' does not declare 'comprobante' in depends_on, so it may not import it -- and
           even when it does, only through modules/comprobante/public.py
  declared contracts.toml:148
           [rules.placement]
  declared contracts.toml:219
           depends_on = []
  why      Placement: how modules talk to each other.
           Queries through a facade, effects through events, nothing through shared/.
           A module that needs another's data imports modules/<other>/public.py and nothing else
           of it: a few functions that take the caller's session and an explicit tenant_id and
           return DTOs. [...]
  instead  - to read data 'comprobante' owns: call a function in modules/comprobante/public.py
           that returns DTOs, and add 'comprobante' to depends_on under [modules.asesor] in
           contracts.toml
           - to react to something 'comprobante' did: subscribe to the event it publishes
           through the outbox, and depend on nothing
           - if it is an enum or a type both modules speak: move it to shared/enums.py and
           import it from both
           - if only 'asesor' needs it, it belongs in 'asesor'
           - waive this one line with # contracts: allow <reason> while the move is in flight

  waiver   # contracts: allow <reason> -- one line, and only the rule that fired on it
           jfast contracts waivers lists every one, so it stays a reviewable decision
           deleting the rule from contracts.toml removes it for every file and for everyone,
           silently, and nothing reports the next violation
```

Four things, and the second is the one nothing else provided:

* **Which rule** forbids it, in the same vocabulary `check` prints.
* **Where it is declared** — `contracts.toml` and a line number, with the line
  itself, so the claim can be checked rather than believed. Each layout
  declares different layers at different lines, and the answer follows the
  contract in front of it.
* **Why the rule is there**, quoted from the comment its author wrote above
  that declaration. The generated contracts carry a rationale above every rule;
  this reads it rather than inventing prose. Where there is no comment, the
  layer's `description` and the rule's `why` are used instead.
* **What to do instead**, naming a destination — `modules/comprobante/public.py`,
  not "do not do that" — and **what waiving costs**, both halves of it: the inline
  waiver takes one line and stays listed by `jfast contracts waivers`, while
  editing `contracts.toml` removes the rule for everyone, in silence.

### When it cannot answer

An answer it cannot derive is reported as `[UNKNOWN]` with what it does know —
the layers the contract declares, the modules on disk — and the command exits
non-zero, so a script can tell "no" from "I do not know". The same applies to a
file no layer claims: that is not a pass, it is a path nobody opted in.

`--json` carries all of it, plus every layer's `paths`, `may_import`,
`forbid_packages` and `description`, and the live violations for the file being
asked about. It is meant to be complete enough that a model never has to open
`contracts.toml` itself — because a model that opens it is one edit away from
deleting the rule.

`--contract PATH` points at a `contracts.toml` (or the directory holding one)
when the command is not run from inside the service.

---

## What changed structurally: `contracts diff`

```bash
jfast contracts diff
jfast contracts diff --json
```

```
Architecture changes  (cuadra)

  + asesor -> comprobante         cross-module  modules/asesor/services/asesor_service.py:1
                                  reaches past modules/comprobante/public.py, the only file another module may import
  + reporte -> cartera            undeclared-dependency  modules/reporte/services/reporte_service.py:3
                                  calls modules/cartera/public.py without 'cartera' in depends_on
  - asesor -> cartera             declared in depends_on, and no import uses it  declared at contracts.toml:219
  - http -> shared                permitted, and no import uses it  declared at contracts.toml:37

Potential breaking change:
  asesor loses direct access to comprobante.ComprobanteService when that import goes -- expose
    what it needs from modules/comprobante/public.py as a function returning DTOs
```

**It is not a git diff, and the limit is worth stating plainly.** Nothing here
reads a previous revision or knows what the code looked like yesterday. It
compares the architecture `contracts.toml` *permits* against the imports the
code *makes*:

* `+` is an edge the code has and the contract does not permit — the same
  finding `contracts check` reports, restated as an architecture change, with
  the symbols that cross the edge.
* `-` is an edge the contract permits that no import uses: a permission that
  could be tightened, not something that was removed. Between modules that is a
  `depends_on` entry no facade import uses.
* `~` is a permission on a layer that governs no file. It is not a weaker `-`;
  see below.
* **Potential breaking change** lists what enforcing the contract costs: which
  name the importer loses if that edge goes, and where to get it instead — the
  owner's `public.py`, or `shared/` for an enum. That is the
  difference between moving the code and deleting the import.

Only static imports count, and only between declared layers and directories
under `modules/`. Reporting only — it always exits zero; the build is failed by
`contracts check`.

### When a layer governs no file

`-` is a subtraction: everything the contract permits, minus everything the
code was observed to do. It means "no import uses this" only while the layers
in the edge actually hold files. On a contract whose globs match nothing — the
layered contract over hexagonal modules — no import in those layers can be
observed at all, so **every** permission they declare lands in `-` at once and
the command frames an outage as a list of opportunities:

```
  - http -> shared                permitted, and no import uses it
  - http -> service               permitted, and no import uses it
  ... eight more
```

Ten of those, on a contract enforcing nothing, is not an invitation to tighten
anything. So `diff` names the empty layers first and marks their permissions
`~`:

```
Architecture changes  (billing)

  ! http, schemas, service govern no file in this project. Their rules apply to nothing, so what
    they permit is listed under `~` rather than as a permission you could tighten. `jfast
    contracts check` fails on this with layer-unmatched.

  - storage -> shared             permitted, and no import uses it  declared at contracts.toml:47
  ~ http -> service               'http' and 'service' govern no file  declared at contracts.toml:32
  ~ http -> shared                'http' governs no file  declared at contracts.toml:32
```

Edges between layers that *do* govern files keep their `-`. They are still
answerable, and refusing to draw the whole report would cost a working
diagnostic to fix a broken one. Which layers are empty is decided by the same
function `check_coverage` uses, so `diff` and `contracts check` cannot
disagree about it. In `--json` these arrive as `unsound` and
`ungoverned_layers`, apart from `removed`.

The fix is not in this command. Point the layer's `paths` at the layout the
modules actually use, or regenerate the contract for that layout —
`jfast inspect` names each module's layout, and `jfast analyze` reports the
same state as `contract-governs-nothing`.

---

## What the checker deliberately does not do

It is static, AST-based and conservative. A checker that cries wolf gets an
ignore file within a week, and then the contract is decoration again.

- **Files matching no layer are not layer-checked.** You opt a path *in*.
  Guessing would produce noise on every script, migration and notebook.
- **But a layer that ends up matching nothing is reported.** `layer-unmatched`,
  and it fails the build. A layer with no files is normal while the code is not
  written yet, so it is only a finding when the tree *also* holds files under
  the directories the contract claims that no layer takes — which is what a
  contract written for another layout looks like from the inside. Without it, a
  contract enforcing nothing is indistinguishable from code with nothing wrong.
- **Only static imports and direct calls are inspected.** `importlib` and
  `getattr` chains are out of scope. This is a design guardrail, not a sandbox.
- **`cross-module-sql` reads string literals, not queries.** It finds a table
  name after `FROM`, `JOIN`, `INTO`, `UPDATE` or `TABLE` in a string or in the
  constant part of an f-string. A table name assembled at runtime from a
  variable is not seen, and neither is a SQLAlchemy query built on another
  module's model — though importing that model is already `cross-module`.
- **The contract is validated first.** Two layers claiming one path, or a
  `may_import` naming a layer that does not exist, are reported as contract
  errors — because otherwise the findings are confident answers to the wrong
  question.

---

## Using it with an agent

Put this in the agent's instructions, or rely on `AGENTS.md`, which already
does:

> Before writing code here, run `jfast contracts show --json`. Before calling
> the work done, run `jfast contracts check`. When it reports a violation, run
> `jfast contracts explain --rule <rule> --json` before changing anything —
> and never edit `contracts.toml` to make a check pass.

The JSON carries scope, layer boundaries, forbidden calls, interfaces and
invariants. That is enough for an agent to write code that fits the first
time, instead of code that a reviewer has to push back on.

And when it drifts anyway, the check catches it — which is the part that makes
this different from writing the same rules in prose and hoping.

---

## Commands

```bash
jfast contracts init                    # defaults for your layout
jfast contracts init --layout screaming
jfast contracts check                   # non-zero exit on a violation
jfast contracts check --json
jfast contracts show --json             # what an agent reads first
jfast contracts render                  # CONTRACTS.md
jfast contracts waivers                 # every inline exception
jfast contracts explain <a> <b>         # why that import is refused, and what to do
jfast contracts explain --rule layer    # what a reported rule means, and where it lives
jfast contracts explain --rule orphan-subscription
jfast contracts diff                    # permitted architecture vs. the built one
```

Add it to the service's CI next to the tests:

```yaml
- run: jfast contracts check
```

## A caveat worth stating

A contract catches structural drift: a layer reaching the wrong way, a
forbidden call, a missing test directory. It does not catch a wrong algorithm,
a bad name, or a rule implemented backwards.

It makes generated code *structurally* clean and it makes the rules explicit.
It does not make the code correct. Review still applies — the contract just
removes the arguments you would otherwise have every time.
