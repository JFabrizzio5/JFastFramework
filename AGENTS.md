# AGENTS.md

Contract for AI agents and human contributors working in this repository.
Read this before writing code. It is short on purpose.

---

## Orient yourself first

Do not grep. Ask the tooling:

```bash
jfast contracts show --json  # the rules THIS project holds itself to
jfast describe --json        # settings schema, plugin graph, providers, infra
jfast plugins list --all     # everything discoverable, including unimportable
jfast workspace list --json  # services, ports, API base URL, needs_gateway
jfast doctor                 # does the config resolve, do enabled plugins import
```

**Start with `jfast contracts show --json`.** It carries the rules this
specific project holds itself to: what it owns, what it deliberately does not,
which layer may import which, which calls are forbidden and why, and the
invariants no checker can verify. Writing code without reading it means
guessing at constraints that are already written down.

Then load the skill for your task from `.jfast/skills/`. Each skill states when
it applies and the exact steps. Do not improvise a workflow a skill already
covers.

---

## Layer boundaries

| Layer | May do | May never do |
| --- | --- | --- |
| `router.py` | validate input, call the service, shape the response | contain business rules, touch the session directly |
| `service.py` | enforce domain rules, orchestrate repositories | import FastAPI types, build SQL |
| `repository.py` | build and run queries | enforce business rules |
| `models.py` | define tables | contain logic |
| `schemas.py` | define the wire contract | mirror models blindly |

A rule that spans two modules belongs in a service, never in a repository.

---

## Hard rules

1. **Never hardcode a port, URL, DSN or credential.** Add a field to a settings
   model. If it does not belong to the kernel, it belongs to a plugin's
   `Settings`.
2. **Never call `os.getenv` in application code.** That is what `PluginSettings`
   is for. The only acceptable getenv is inside a settings model default, and
   even that is a smell.
3. **Never add a dependency to the kernel.** New dependencies go in an extra
   and are imported inside the plugin that needs them, not at module top level.
4. **Never write a template as a string literal in Python.** Templates are
   files under `src/jfastframework/templates/`. This is the mistake the v0
   prototype in `legacy/` made; do not reintroduce it.
5. **Never import one plugin from another.** Use `ctx.provide` / `ctx.require`.
6. **Never let a secret reach `describe()` or `/info`.** Type it `SecretStr`.
7. **Never run DDL at import time.** Schema changes go through Alembic, or at
   worst through a plugin's `startup` behind an explicit setting.

---

## Adding a feature: the decision

| The feature… | Goes in |
| --- | --- |
| is needed by every service (logging, errors, health) | the kernel |
| is optional and has its own dependency | a plugin |
| is specific to one service's domain | a module in that service |
| is a new backend for something that already has a protocol | an implementation of that protocol, selected by config (see `jfastframework.vectors`) |
| generates files | a template tree plus a CLI command |

When a second implementation of something appears, extract a protocol rather
than branching inside the consumer. `rag` does not know what pgvector is; it
knows `VectorStore`.

When in doubt, make it a plugin. Moving a plugin into the kernel later is easy;
removing something from the kernel is a breaking change.

---

## Writing a plugin

Full guide in [docs/plugins.md](docs/plugins.md). The shape:

```python
class MyPlugin(Plugin):
    meta = PluginMeta(
        name="my_plugin",
        requires=("database",),      # hard dependency, pulled in automatically
        after=("observability",),    # soft ordering, no error if absent
        provides=("my.client",),     # declared up front, conflicts caught early
        default_enabled=False,       # off unless the service asks for it
        extra="jfastframework[mine]",
    )
    Settings = MySettings

    def register(self, ctx):    ...   # build time. Routers, middleware, providers. No I/O.
    async def startup(self, ctx):  ... # runtime. Open connections here.
    async def shutdown(self, ctx): ... # runtime. Close what startup opened.
    async def health(self, ctx):   ... # probe. Return HealthReport.
    def infra(self, ctx=None):     ... # containers this plugin needs.
```

`register` must not do I/O. It runs before the event loop is serving, and a
blocking call there stalls startup for every service that loads the plugin.

---

## Verifying a change

Run all four. A change is not done until they pass.

```bash
pytest
ruff check src tests
mypy src
jfast doctor
jfast contracts check
```

If you changed a template, a plugin's `infra()`, the workspace, or anything the
generator touches, run the end-to-end suites:

```bash
bash scripts/smoke.sh              # both module layouts, HTMX, alembic wiring
bash scripts/smoke_contracts.sh    # a generated service passes its own contract
bash scripts/smoke_workspace.sh    # workspace, auto-gateway, view patching
bash scripts/smoke_start.sh        # the default stack, end to end
bash scripts/smoke_docs.sh         # the quickstart, run exactly as written
bash scripts/smoke_go.sh           # go vet, test, build, run   (needs go)
bash scripts/smoke_frontend.sh     # npm install + vite build   (needs node)
```

If you touched `eastereggs/` or the framework's wheel `packages`, run
`bash scripts/smoke_eastereggs.sh`: it proves `pip install jfastframework`
still ships no easter eggs.

Templates are the part that breaks silently: they render fine and produce code
that does not import, or a Jinja environment eats the `{{ }}` a Vue file needed
at runtime. `pytest` alone catches neither — that is not hypothetical, it is
how the router bug in 0.4.0 survived every grep-based check.

**Still not covered:** RabbitMQ and Kafka against real brokers. Say so rather
than implying a green build.

---

## Documentation duties

- New plugin → a section in `docs/plugins.md` and a row in the README table.
- New CLI command → the README quickstart if a user would run it.
- Behaviour change to the kernel → `ARCHITECTURE.md`, with the trade-off named.
- Anything shipped or dropped → move the checkbox in `PLAN.md`.

Do not mark something `[x]` in `PLAN.md` that has gaps. Use `[~]` and name the
gaps. A roadmap that overstates completion is worse than no roadmap.

---

## Commits

Short imperative subject, area first:

```
kernel: fail fast when two plugins claim the same provider
rag: chunk on paragraph boundaries instead of fixed width
docs: document the port-block convention
```

Code, comments and documentation in English. Conversation with the maintainer
in Spanish.

---

## Comment style

Comments explain constraints the code cannot express — why a value is what it
is, what breaks if it changes. Do not narrate the line below.

```python
# Bad
# increment the counter
counter += 1

# Good
# expire_on_commit=False keeps ORM objects usable after the request scope
# commits, which is what response serialisation needs.
sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
```
