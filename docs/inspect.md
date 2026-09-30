# Reading a project back

```bash
jfast inspect     # what is in here
jfast analyze     # what is wrong with it
jfast graph       # what depends on what
jfast check       # all of the above, plus the rest, with one exit code
```

`check` is one line of a pipeline, not the pipeline. It runs **no linter, no
type checker and no test suite** — see [What `check` does not
check](#what-check-does-not-check).

Three commands that answer the questions a maintainer actually asks at month
six, none of which the CLI could answer before: it was very good at the first
ten minutes of a project and silent afterwards.

---

## The gap they close

`jfast describe` answers "what is this service" by **building the app**. It
resolves the plugin graph, imports every plugin and reads the live settings.
That answer is the authoritative one and it has two problems.

It is unavailable exactly when you want it. A missing dependency, a syntax
error, a half-finished refactor — the moments you most need to know what is in
front of you are the moments `describe` cannot run.

And it says nothing about modules. Generate two of them and neither name
appears anywhere in its output:

```bash
jfast new module invoice && jfast new module order
jfast describe --json | grep -c invoice
0
```

The framework knew the module layout, the layer rules and the wiring, and had
no way to tell you any of it. So every agent that touched a project reached for
`grep`, and so did every person.

These three read the filesystem instead. Slightly less authoritative, always
available, and they know what a module is.

---

## `jfast inspect`

```
  shop 0.1.0 (local)

  modules (2)
    invoice  layered/api   /invoices
    order    layered/api   /orders

  plugins      observability, metrics, database
  shared       2 files
  migrations   0
  contract     contracts.toml
  frontend     -

  ✓ nothing to report
```

One screen. The command to run first in an unfamiliar service, and the one an
agent should run before it edits anything.

```bash
jfast inspect module order    # one module in detail
jfast inspect --json          # the same, as data
jfast inspect --path ../shop  # from outside
```

`inspect module` adds the file list, the packages it imports, the tables it
declares, and whether it has tests and a README.

**It never imports your code.** Everything above comes from parsing the source,
so it works on a project whose dependencies are not installed and on one that
does not currently run.

---

## `jfast analyze`

Everything structurally wrong with the project, worst first, each with the fix
on its own line:

```
  CRITICAL
    module-cycle: import cycle: invoice -> order -> invoice
      Modules in a cycle are one module with folders between them: neither can
      be extracted into a service, and a change to one breaks the other in a
      way no test covers. Keep the query in one direction, through the other
      module's public.py, and turn the other direction into an event.

  HIGH
    modules/ghost/  module-unregistered: module 'ghost' declares routes but
                    main.py never imports it
      The endpoints do not exist at runtime. Nothing fails: the tests in the
      module pass, the server starts, and the route 404s.
```

### What it checks

| Code | Severity | What it catches |
| --- | --- | --- |
| `module-cycle` | critical | Two modules importing each other. Neither can ever be extracted. |
| `module-unregistered` | high | A module with routes that `main.py` never imports. The endpoints simply do not exist. |
| `route-conflict` | high | Two routers on one prefix. Whichever registers first wins; the other's paths are unreachable and FastAPI does not warn. |
| `shared-imports-module` | high | `shared/` reaching back into a module, which turns the dependency graph into a circle. |
| `plugin-unknown` | high | A plugin enabled in `jfast.toml` that nothing provides. The app refuses to start. |
| `contract-governs-nothing` | high | A layer in `contracts.toml` whose `paths` match no file here. Its rules apply to nothing, and `contracts check` passes while enforcing nothing. |
| `module-no-migration` | medium | A module whose table no revision creates. Fails at the first query and nowhere earlier. |
| `cross-module-import` | medium | One module importing another past its `public.py`. An import of `modules/<other>/public.py` is how modules are meant to talk, and is not reported here; `contracts check` decides whether it is declared. |
| `code-outside-module` | low | A `.py` at the root belonging to nothing. |

### Why the list is short

Every check is decidable from the source text. Nothing here guesses.

That is a deliberate limit, and it costs real coverage: N+1 detection, dead
code and unused dependencies are all things a fuzzier pass could report, and
all things it would sometimes be wrong about. A checker that is right nine
times out of ten gets muted after the second false positive — and then the true
findings go with it. Ten checks nobody argues with beat thirty nobody runs.

`module-no-migration` is the shape of that rule in practice. It fires only once
there **are** revisions and this module's table is in none of them. With no
migrations at all the project simply has not reached that step, which is what a
freshly generated one looks like, and greeting a new project with a finding is
how a tool teaches people to ignore it.

### Against `contracts check`

The contract enforces the rules you declared **inside** a file — which layer
may import what, which calls are forbidden, what may not block. `analyze`
reports on the shape of the project **between** files. Run both; they fail for
different reasons and with different exit codes.

`analyze` does not run `contracts check`, and `contract-governs-nothing` is not
an exception to that. It reads one thing out of `contracts.toml`: whether the
layers match any file at all. Which layout the files are in and whether the
contract describes it are facts about how files are *arranged*, decidable
without opening one — this command's own question. What a layer permits inside
a file stays `contracts check`'s, and re-emitting its findings here would give
one report two owners, two severities and two remedies, which is how two
commands drift into contradicting each other.

Staying silent instead was the worse option, and it is what this replaces. On a
service scaffolded with the layered contract and then filled with hexagonal
modules, `contracts check` failed with three `layer-unmatched` violations,
`jfast next` reported *contracts check fails (3 violations)* — and `jfast
analyze` printed `✓ no findings` about the same directory. The finding is
produced by calling `check_coverage`, the checker's own function, so the two
commands now report the same count and the same sentences.

```bash
jfast analyze --fail-on critical   # only the worst
jfast analyze --fail-on never      # report, never fail
jfast analyze --json               # counts and findings, as data
```

Default is `--fail-on high`.

---

## `jfast graph`

```
  invoice
    └─ (no module dependencies)
  order
    └─ invoice
```

`jfast workspace graph` draws services. This draws the modules inside one,
which is the graph that decides whether a module can ever be extracted: a
module nothing imports is a service waiting to happen, and a cycle is two
modules that will never be either.

```bash
jfast graph --module billing        # only its edges
jfast graph --format mermaid        # paste into a README
jfast graph --format dot            # pipe to graphviz
jfast graph --format json
```

---

## `jfast check`

Every **static** check this framework has, in one command and one exit code.

Every check on this page already existed. What did not exist was a single thing
to run, so CI ran three of them, an agent ran whichever one it remembered, and
the two nobody wired up never ran at all.

It does not run your linter, your type checker or your tests, and every run
says so in its last two lines. Read [What `check` does not
check](#what-check-does-not-check) before you replace anything with it.

```
  shop

  ✓ config      pass   shop (local)
  ✓ plugins     pass   3 enabled, 20 installed
  ✗ analyze     fail   high 1
  ✗ contracts   fail   high 1
  ✓ migrations  pass   0 revisions read, no database
  ✓ deploy      pass   compose renders, 2 services

  ANALYZE  (validation failure, exit 1)
  HIGH
    modules/order/  module-unregistered: module 'order' declares routes but
                    main.py never imports it
      The endpoints do not exist at runtime. Nothing fails: the tests in the
      module pass, the server starts, and the route 404s.

  CONTRACTS  (contract violation, exit 5)
  HIGH
    modules/invoice/service.py:43  forbid-call: print() is not allowed here
      Use the structured logger; print output has no request id and no level.

  4 pass, 2 fail in 0.13s
  exit 5  (contract violation)

  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
```

```bash
jfast check                          # everything, human output
jfast check --json                   # everything, as data
jfast check --ci                     # strict: any finding fails, and so does any skip
jfast check --only contracts,analyze # a subset
jfast check --fail-on critical       # same threshold flag as `jfast analyze`
jfast check --multitenant-ready      # instead of the battery: what a switch to multitenant breaks
jfast check --path ../shop           # from outside
```

### Against `doctor` and `workspace validate`

`doctor` asks a question about **your machine**: does the configuration
resolve, does every enabled plugin import on this interpreter. Its answer
changes when you switch virtualenv and never when you change a line of code.

`check` asks a question about **the repository**: is what is committed here
consistent with itself. It runs `doctor`'s two questions as its first two
checks, because a broken install makes every downstream answer a lie — but
`doctor` stays, because when you are debugging your own laptop you want the
two-second answer and not the whole battery.

`jfast workspace validate` reads one file, the workspace resource graph, and is
scoped to a workspace rather than to a service. `check` runs it as part of
`deploy`.

Only `check` belongs in CI.

### The seven checks

Each one is named after the command that already ran it. There is no second
vocabulary, and the name is what `--only` takes.

| Check | What it runs | Exit code on failure |
| --- | --- | --- |
| `config` | `JFastConfig.load` — the same load `doctor` does | `2` |
| `plugins` | `registry.discover()`, `registry.build()`, and the unimportable list | `3` |
| `analyze` | `jfast analyze` — structure between files | `1` |
| `tenancy` | tenant settings that contradict each other or the code ([multitenancy](multitenancy.md#settings-that-contradict-each-other)) | `1` |
| `contracts` | `jfast contracts check` — the rules you declared | `5` |
| `migrations` | `jfast migration check`, statically: no database | `4` |
| `deploy` | `workspace.validate()` plus rendering `docker-compose` | `1` |

Nothing here starts a container, opens a socket or talks to a database. That is
what makes the command safe in a pre-commit hook, and it is also its limit:
`deploy` proves the compose file can be generated and is internally consistent,
not that the images pull.

### What `check` does not check

**`jfast check` runs no linter, no formatter, no type checker and no test
suite.** Nothing in its output changes when any of them fail:

```bash
# an unused import, a misformatted file, a str assigned to an int,
# and a failing test, all present at once
ruff check .            # 17 errors
ruff format --check .   # 4 files would be reformatted
mypy .                  # 2 errors
pytest                  # 1 failed

jfast check             # 6 pass — byte-identical to the clean project
jfast check --ci        # exit 0
```

That is not a bug to be fixed by running them. `pytest` executes your code for
an unbounded time against whatever a fixture decides to start, and `mypy` in a
tree whose dependencies are not installed reports missing imports that your own
configuration would have silenced — either one inside this command turns a
pre-commit hook into a build, and a checker that manufactures findings is muted
within a week. `check` answers whether the **repository** is consistent with
itself; those four answer whether the **code** is correct. Two questions, two
commands.

So the cost is paid in the open. Every run ends with the four it left to you,
pass or fail:

```
  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
```

and `--json` carries the same under `not_covered`, because a script reading
`"ok": true` cannot read a footer:

```json
{
  "ok": true,
  "not_covered": [
    {"what": "lint", "catches": "unused imports, undefined names, unreachable code",
     "command": "ruff check ."},
    {"what": "formatting", "catches": "a diff nobody agreed to review",
     "command": "ruff format --check ."},
    {"what": "types", "catches": "a str where an int was declared", "command": "mypy ."},
    {"what": "tests", "catches": "whether any of it works", "command": "pytest"}
  ]
}
```

The line to put in CI is the whole line:

```bash
jfast check --ci && ruff check . && ruff format --check . && mypy . && pytest
```

### One exit code from many checks

Six checks, one number. When several fail, the code is taken from the failure
that invalidates the most of the report — not from the worst severity:

```
2 config  >  3 plugins  >  4 migrations  >  5 contracts  >  1 analyze/deploy
```

A `jfast.toml` that does not parse makes every other answer a guess, so it
wins. A plugin that will not import is next, for the same reason. **Migrations
beat contracts** because a schema the code does not agree with fails in
production at the first query and is the one result you must not ship past; a
contract violation, by contrast, is visible in the diff and fails safely.
Contract beats `analyze` because a contract is a rule somebody wrote down,
while `analyze` findings are the broadest and least specific class — last, so
any more informative code wins over it.

The single number is a summary, never the whole answer. `--json` carries
`codes` — every failing check's code — so a script never has to infer the other
five:

```json
{
  "ok": false,
  "exit_code": 5,
  "exit_meaning": "contract violation",
  "codes": [1, 5],
  "failed": ["analyze", "contracts"],
  "skipped": [],
  "complete": true
}
```

### A skip is not a pass

A check that could not run reports as `skip`, with the reason, in both output
modes:

```
  · contracts   skip   no contracts.toml in /tmp/shop (jfast contracts init)

  4 pass, 1 fail, 1 skip in 0.07s
  · skipped: contracts -- not a pass
```

In `--json` it is `status: "skip"` with a non-null `reason`, its name is in
`skipped` and **not** in `passed`, and `complete` is `false`. A battery that
quietly reports green on a check it did not run is worse than no battery: it
converts "I did not look" into "I looked and it was fine".

**Under `--ci`, a skip fails**, with exit `3` — environment error. That is what
a skip in CI means: the runner was missing something the check needed, and the
pipeline is not checking what you think it is checking. Locally a skip is
normal and informative; in CI it is a hole.

`--ci --allow-skips` is the one way past it, and it is deliberately explicit —
a team that genuinely cannot supply something writes the flag into the pipeline
where a reviewer can see it, rather than never noticing the hole.

A skip only ever decides the exit code when nothing else failed. A run that
both violates its contract and skipped a check exits `5`, not `3`: there is a
concrete defect to report, and blaming the runner for it would be wrong.

### What it costs

On a freshly generated service the checks themselves take **120–185 ms**, and
the whole process **0.65–0.9 s** wall clock — nearly all of the difference is
Python's own startup and imports, which a pre-commit hook pays for any tool.

Measured per check, on that same service:

| Check | Cost |
| --- | --- |
| `config` | ~1 ms |
| `migrations` | ~3–12 ms |
| `contracts` | ~30 ms |
| `plugins` | ~45–85 ms |
| `analyze` | ~60–100 ms (includes `plugins`) |
| `deploy` | ~60–90 ms (includes `plugins`) |

`plugins` is the slow one and it dominates the rest: `registry.discover()`
imports every installed plugin. It runs once per invocation and the result is
handed to `analyze` — which needs the installed set to report `plugin-unknown`
— and to `deploy`, rather than being recomputed. Everything else is AST
parsing, which scales with the number of `.py` files and not with the size of
the project's dependencies.

`--only contracts,migrations` is the subset that avoids plugin discovery
entirely, at ~35–45 ms. That is the shape to put in a pre-commit hook if the
full run ever stops feeling instant — with the full battery still in CI.

One caveat on `migrations`: with no database it reads **every** revision, not
just the pending ones — which are applied is a fact only the database has — and
treats every table as populated, which is the conservative reading. On a
repository with a long migration history, `jfast migration check --dsn ...` is
the narrower answer, and `--only` can drop this check from the battery.

---

## Exit codes

Standard across the whole CLI, documented and tested. A script that has to read
English to find out why a command failed is a script that breaks when the
wording changes.

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | validation failure — a check found something wrong |
| `2` | configuration error — a file is missing or does not parse |
| `3` | environment error — Docker, a container, a database |
| `4` | migration risk or failure |
| `5` | contract violation |
| `6` | user input error |
| `7` | compatibility or version error |

Telling `1` from `5` is what lets CI say *why* it failed without parsing text:

```bash
jfast analyze || case $? in
  1) echo "structure" ;;
  2) echo "config" ;;
esac
```

These are part of the public API. A code never changes meaning; a new failure
mode gets a new number.

---

## For an agent

```bash
jfast inspect --json          # what exists
jfast graph --format json     # what depends on what
jfast analyze --json          # what is wrong, with the fix in `why`
jfast contracts show --json   # the rules
jfast contracts check --json  # the violations
jfast check --json            # every static check, with one exit code
```

`jfast check --json` is the last *jfast* call to make before reporting a change
as finished: it is the only one that fails when something was never examined, so
"it passed" cannot mean "it did not run". Read `skipped` and `complete` before
reading `ok` — and read `not_covered`, which names the linter, the formatter,
the type checker and the test suite it did not run. `"ok": true` is not "the
change is done"; it is "the repository is consistent with itself".

Five commands, no source reading, no guessing at conventions. The `why` field
on every finding is the part that matters: an agent handed a violation with no
remedy tends to satisfy the checker rather than fix the design.
