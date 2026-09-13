# Working with AI agents

The premise of this framework is that an agent writing code in your repository
is now normal, and that the thing which makes it survivable is not a better
prompt — it is **rules the agent cannot quietly break.**

Three surfaces, in increasing order of how much they help:

| | What it gives an agent |
| --- | --- |
| `jfast ai context --json` | everything about this project, in one call |
| `jfast next` | what is still unfinished, in the order it can be done |
| `contracts.toml` | what it may and may not do, checked by CI |
| `AGENTS.md` + `.jfast/skills/` | how this project expects work to be done |

---

## The generated agent surface

```bash
jfast new service billing --agent-docs
jfast init                              # asks
```

Writes:

```
AGENTS.md                                   the rules, at the root where agents look
.jfast/skills/respect-contracts/SKILL.md    read the contract before writing
.jfast/skills/design-system/SKILL.md        only when there is a frontend
```

**Off by default.** A project nobody points an agent at owes no agent files,
and every file shipped is a file that can drift from the code it describes.

### Why `.jfast/skills/` and not one big file

Because the value is in *not* loading all of it. A skill declares what it is
for and when to skip it:

```yaml
---
name: respect-contracts
description: Read this project's contract before writing code in it, and verify
  the code against it before calling the work done.
when_to_use: Always, before any code change in billing.
when_not_to_use: Answering a question about the project without changing it.
---
```

An agent reads the front matter of each, picks the one that matches the task,
and loads only that. One monolithic file spends the context budget on rules
that do not apply to the change in hand — and duplicates `AGENTS.md`, which
then drifts out of sync with it.

### What each file is for

**`AGENTS.md`** — the rules that apply to every change: the service-level
shape, the five checked rules, what not to do, and the commands. Short on
purpose.

It says nothing about the files *inside* a module, and that is deliberate. It
is written at service-scaffold time, before any module exists, and one service
may hold modules in all four layouts. Naming `router.py` there was a promise
that came true in one of the four. What it does instead is name the two things
that are written alongside the code and are therefore always true:
`[modules.<name>]` in `jfast.toml` for the layout, and
`modules/<name>/README.md` for that layout's file map.

**A skill** — the procedure for one kind of task: preconditions, steps with the
exact commands, how to verify, and the mistakes people actually make.

---

## The rules an agent cannot drift past

These are not advice. `jfast contracts check` fails the build:

```
modules/payment/<file>:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

`file:line`, the rule, what happened, and **what to do about it**. That second
line matters more than it looks: an agent given a violation with no remedy
tends to satisfy the checker rather than fix the design — deleting the import,
inlining a copy, or turning the rule off. Naming the destination removes the
ambiguity.

The full list is in [Contracts](contracts.md). The ones that most often catch
generated code:

| Rule | Why an agent trips it |
| --- | --- |
| The HTTP layer may not import `sqlalchemy` | Querying from the handler is the shortest path to a working endpoint |
| Modules may not import each other | Reusing the neighbour's model is easier than moving it |
| `shared/` may not import a module | Fixing the above by importing backwards |
| No blocking calls in `async def` | `time.sleep` and `requests` are what most examples use |

---

## Waive a line, do not delete the rule

```python
from modules.invoice.enums import Status  # contracts: allow migrating to shared
```

The waiver is on one line, with a reason, and `jfast contracts waivers` lists
every one. Turning the rule off in `contracts.toml` removes it for everybody,
silently, and the next violation goes unreported — which is how a contract
stops meaning anything.

---

## One call before the first edit

```bash
jfast ai context --json
```

Everything about **this** project in one answer: every module and how it is
shaped, what `main.py` actually imports, the module import graph, the contract,
what `analyze` and `contracts check` say right now, what is still unfinished,
and the commands that return whatever was left out.

It is a composition, not a reimplementation — `inspect`, `analyze`, `graph`,
`contracts` and `next` in one payload, so it cannot disagree with the command it
tells you to run. It never imports the project, so it still answers on a service
whose dependencies are missing or whose code does not parse.

### How big is it

**There is no one number, and a single figure here was wrong for every project
that was not the one it was measured on.** The payload is facts about *your*
service, so it scales with your service. Measured on generated services —
`jfast new service shop --with database`, then `jfast new module` N times:

| modules | `--json` | `--json --brief` |
| --- | --- | --- |
| 1 | 8,298 | 2,789 |
| 3 | 9,896 | 4,073 |
| 5 | 11,508 | 5,479 |

A generated service is the floor, because nothing is wrong with it yet. The
same five-module service after some work in it — two modules `main.py` never
picked up, a file outside any module, two contract violations — measures
**14,837 bytes (~3,700 tokens) full and 6,149 (~1,500) with `--brief`**. Roughly
+800 bytes per module, and the rest is `next` and `checks` growing with what is
actually outstanding.

Where the full payload goes on that five-module service, in bytes:

```
next      3,052   contract  2,300   commands 1,576   checks  1,562
modules   1,499   omitted     921   project    135   plugins   130
```

Token figures are bytes ÷ 4 — the standard rough estimate, not a tokenizer.

**`jfast ai context --size` is the answer for your project**, and it is the
command to run before you decide whether to call the other one in a loop. The
table above is a scale, not a budget: do not plan a context window against it.

### What it deliberately leaves out

The obvious implementation of "everything a model needs" concatenates `docs/`.
That is wrong twice over:

* **`docs/` is not in the wheel.** `pyproject.toml` ships only the packages
  under `src/`, so a project created by someone who ran `pip install
  jfastframework` has none of it on disk. A command that reads it works on this
  repository and nowhere else.
* **It is 53 pages, 555 KB, roughly 139k tokens** — sixty times the size of the
  answer, for a manual that says nothing about *your* modules.

So the payload carries facts about the project, and names every gap under
`omitted` with the command that closes it:

| Left out | How to get it |
| --- | --- |
| The documentation | <https://jfabrizzio5.github.io/JFastFramework/latest/> |
| File contents | open the files it names — it never quotes source |
| Per-module file listings | `jfast ai context --module <name>` |
| Resolved settings, live plugin graph, route table | `jfast describe --json` (imports the app) |
| What each unapplied revision does to a database with rows | `jfast migration check --json`, `jfast migration plan` |
| Findings or violations past the first 20 | `jfast analyze --json`, `jfast contracts check --json` |
| History | `git log` |

Naming the gaps matters more than it looks. An agent handed a partial answer
with no seam in it treats it as a complete one, and then writes code against a
file it was never shown.

### Narrowing

```bash
jfast ai context --json --module invoice   # one module, with its file list
jfast ai context --json --brief            # the shape, without the detail
jfast ai context --size                    # what the above two cost
```

`--module` narrows the modules, findings and violations. It does **not** narrow
`next`, on purpose: you asked about one module, and the step that is blocking
you may be in another one.

`--brief` drops the contract's rules (keeping its scope, layer names and
invariants), the finding and violation lists (keeping the counts), `shared/`,
and the prose in `commands` and `omitted`.

### The commands it points at

Each entry in `commands` is a machine-readable entry point, with what it returns
and what reading it saves you:

```bash
jfast inspect --json           # modules, layouts, routes, wiring, tables
jfast analyze --json           # structural findings, each with a remedy
jfast graph --format json      # module-to-module import edges
jfast contracts show --json    # layers, forbidden calls, interfaces, invariants
jfast contracts check --json   # violations, with file, line and rule
jfast next --json              # what is unfinished, in order
jfast describe --json          # resolved settings and plugin graph
```

Every one of them except `describe` reads the filesystem without importing the
project.

---

## `jfast next` — what is unfinished

The same engine as `jfast analyze`, turned around. `analyze` says what is wrong;
`next` says what to do about it, in the order it can be done:

```
  next  shop

   1. module 'ghost' declares routes but main.py never imports it
      └─ edit main.py between the [jfast:imports] and [jfast:routers] markers
   2. 'helpers.py' belongs to no module                      move it into a module, or into shared/
   3. ghosts has no revision (this project has none)         alembic revision --autogenerate
   4. invoices has no revision (this project has none)       alembic revision --autogenerate
   5. contracts check fails (2 violations)                   jfast contracts check
   6. modules/order has no tests                             add modules/order/tests/
   7. contracts.toml still has its generated placeholders    edit contracts.toml
   8. modules/payment has no README                          add modules/payment/README.md

  wire -> shape -> persist -> verify -> cover -> document   (dependency order, not severity)
```

This is the answer to "the agent skipped a step".

### The ordering is the point

Steps are sorted by **stage**, and a stage is a precondition of the ones after
it — never by how bad the finding is:

| Stage | Nothing after it is worth doing until |
| --- | --- |
| `boot` | the service starts at all — an enabled plugin nothing provides stops it |
| `scaffold` | there is a module to wire, migrate or test |
| `wire` | `main.py` imports it: an unwired module's tests pass while its routes 404 |
| `shape` | the code has stopped moving between files |
| `persist` | the tables the shape settled on actually exist |
| `verify` | the contract has been checked against where the files ended up |
| `cover` | the code under the test is wired, placed and backed by a table |
| `document` | — last: it describes what the stages above settled |

Severity would give a different and worse order. In the listing above,
`'helpers.py' belongs to no module` is `low` and `ghosts has no revision` is
`medium`, yet the loose file goes first: moving it changes which tables the
project declares, so a revision generated before the move is one you regenerate
after it. Testing an unwired module is the same mistake in a louder form — the
test passes, and the endpoint 404s.

`jfast next --json` carries the `stage` and its numeric `rank` on every step,
plus a `stages` block spelling out what each one is a precondition of.

### One fact, one step

A contract written for another layout is reported by two commands at once:
`analyze` files one `contract-governs-nothing` per empty layer, and `contracts
check` files one `layer-unmatched` for each of the same layers. Four empty
layers therefore arrived as four steps plus a
`contracts check fails (4 violations)` summary of those same four — five lines
about one file, filed under `shape` as if something had to move, each carrying
`jfast analyze  # contract-governs-nothing`, which re-prints what you are
already looking at.

It is one step now, in `verify`, and it names the command that clears it:

```
   2. contracts.toml governs nothing: 4 layers match no file here
      └─ jfast contracts init --layout hexagonal --force
```

The layout comes from what `jfast.toml` records for the modules. If they are not
all in one layout the slot stays `<layout>`, because `--force` overwrites the
contract and a guess is not worth attaching to that. And a violation that is
*not* `layer-unmatched` still gets its own `contracts check fails (n)` step —
dropping the duplicate must not drop the rest.

### When there is nothing left

```
  ✓ shop: nothing outstanding

  3 modules, every one registered, tested and documented.
  2 revisions cover every declared table.
  contracts.toml passes, and its placeholders are filled in.
```

Stated as the list of what was checked rather than as praise, because a tool
that always finds something to do trains people to ignore it, and one that says
"all good" without saying what it looked at is no better.

`jfast next` always exits 0. It is a question, not a gate — `jfast analyze
--fail-on high` is the gate.

### On a freshly generated service

It is not silent, and everything it says is true. `jfast new service` writes
three models with `__tablename__` and no revisions, and a `contracts.toml` whose
`owns` and `does_not_own` are still `TODO`:

```
   1. invoices has no revision (this project has none)   alembic revision --autogenerate
   2. orders has no revision (this project has none)     alembic revision --autogenerate
   3. payments has no revision (this project has none)   alembic revision --autogenerate
   4. contracts.toml still has its generated placeholders: owns, does_not_own, invariants
      └─ edit contracts.toml
```

It does **not** claim the modules are unwired or untested — the generator wired
and tested them, and saying otherwise is the failure that makes people stop
reading the output.

This is also the one place `next` reports something `analyze` will not. `analyze`
stays silent about migrations when a project has none at all, so a brand-new
service is not greeted with a finding. `next` exists to name the step after the
one you just took, and on that project the step is the first revision.

---

## What this does not solve

An agent that follows every rule can still build the wrong thing. Contracts
constrain *structure*, not intent: nothing here notices that the feature was
not what you asked for, that the test asserts the bug, or that a rule you wrote
in January is wrong in June.

What it buys is narrower and worth having anyway: the codebase does not decay
while you are not looking, and a review can be about whether the feature is
right rather than about where the file went.

---

## The framework's own agent surface

This repository practises it: `AGENTS.md` at the root and seven skills under
`.jfast/skills/`, covering module creation, plugin authoring, contracts, the
frontend and the design system. [Skills for agents](skills.md) covers writing
one.
