# Services, modules and layouts

Two levels of generation:

```bash
jfast new service billing            # a whole service
jfast new module invoice             # a domain module inside it
```

Both are compositions, not fixed templates. You choose the shape, per module,
and the choice is remembered.

---

## Services

```bash
jfast new service billing                            # JSON API
jfast new service storefront --kind web --port 8020  # server-rendered
jfast new service billing --agent-docs               # + AGENTS.md and skills
```

| Kind | Renders | Enabled plugins | Extra needed |
| --- | --- | --- | --- |
| `api` | JSON | observability, metrics, database | `[server,db,metrics]` |
| `web` | HTML | + web | `[server,db,metrics,web]` |
| `spa` | — | a frontend project | none; it is npm |
| `gateway` | — | gateway | `[server,gateway]` |

A frontend is a service like any other. It logs the same way, reports health
the same way, deploys the same way, and lives in the same port block. The only
difference is what comes out of the handlers.

Both backend kinds are generated with `main.py`, `jfast.toml`, `.env.example`,
`conftest.py`, `requirements.txt`, `shared/`, `.gitignore` and a README. The
`web` kind adds `templates/base.html`, `templates/index.html`,
`static/app.css` and a root `web.py` router.

`contracts.toml` is not among them. Its layer paths are a layout's, and a
service has no layout until it has a module, so it arrives with the first one —
see [Module layouts](#module-layouts) below.

`--agent-docs` additionally writes `AGENTS.md` and `.jfast/skills/` — see
[Working with AI agents](agents.md).

---

## Module layouts

Four, and each module picks its own. That is the point of a modular monolith:
a catalogue module is four files, and an orders module that has to be testable
without a database wants ports and adapters. Forcing both into the same shape
makes one of them wrong.

```bash
jfast new module invoice                        # asks
jfast new module invoice --layout hexagonal     # or say
```

Run without `--layout` in a terminal and it asks:

```
Architecture for 'invoice'
  › modular      a folder per layer. Start here: it grows without being moved.
    layered      a file per layer. For a table with an API and little else.
    screaming    one file per use case. When the verbs matter more than the nouns.
    hexagonal    ports and adapters. When the domain must be testable with no database.

  choice [modular] ›
```

**With no terminal it does not ask.** A piped install, a script or a CI job
gets `modular` rather than a prompt nobody can see. A wizard that blocks a
pipeline is worse than a flag nobody set.

**The first module also writes `contracts.toml`**, with the layer paths of the
layout you picked. That is the earliest honest moment: `jfast new service` has
no module and no layout, and the guess it used to make — layered, always —
matched none of the files the other three layouts generate, so their contracts
enforced nothing and reported a pass.

A later module in a *different* layout does not rewrite it. A contract on disk
is a document someone has had the chance to edit, and its layer paths are the
least of what it carries. Add the second layout's paths yourself; nothing else
will, and `contracts check` cannot see the gap while the first layout's layers
still match their own modules. If the whole service moves to another layout,
the check does report it, as `layer-unmatched`.

### `layered`

The familiar shape. Good when the module is mostly CRUD and the interesting
part is the data, not the rules.

```
modules/invoice/
├── router.py       HTTP in, response out
├── service.py      business rules
├── repository.py   queries
├── models.py       SQLAlchemy
├── schemas.py      Pydantic
├── enums.py
├── public.py       what other modules may call
├── README.md
└── tests/
```

### `modular`

Layered, one folder per concern. The boundaries are identical — its contract is
the layered one with different paths — so the only thing the folders buy is
room: a concern can grow to several files without anybody having to decide
where the new one goes.

```
modules/invoice/
├── api/routes.py
├── models/
│   ├── invoice_entity.py       SQLAlchemy
│   └── invoice_models.py       Pydantic
├── repositories/invoice_repository.py
├── services/invoice_service.py
├── validations/invoice_validation.py
├── enums.py
├── public.py                   what other modules may call
├── README.md
└── tests/
```

`validations/` is the one genuinely new idea: **business rules that are not
schema shape.** "This name is already taken" needs the rest of the table; "you
cannot deactivate the last active one" needs the current row. Neither is
something Pydantic can express, and both belong somewhere a reader can find
them.

Every folder has an `__init__.py` that re-exports its public names, so callers
import from the package rather than reaching into a file.

Reach for it when a module outgrows four files — not before. Six folders around
one CRUD entity is ceremony.

### `screaming`

The directory listing is the feature list. Good when the module has real rules
worth protecting from the framework, and when you want new capabilities to
arrive as new files rather than as new methods on a class nobody can navigate.

```
modules/invoice/
├── invoice.py           the domain: entity + rules, framework-free
├── use_cases/
│   ├── create_invoice.py
│   ├── list_invoices.py
│   ├── get_invoice.py
│   ├── update_invoice.py
│   └── delete_invoice.py
├── storage.py           SQLAlchemy model + repository, with to_domain()
├── http.py              router + wire schemas
├── public.py            what other modules may call
├── README.md
└── tests/
    ├── test_invoice_domain.py      no database, no fakes, no event loop
    └── test_invoice_use_cases.py   fake repository, nothing else
```

### `hexagonal`

Ports and adapters. The domain declares an abstract port, infrastructure
implements it, and the application layer depends only on the port.

```
modules/invoice/
├── domain/
│   ├── entities.py      plain dataclasses, no ORM
│   ├── ports.py         the Protocol the application depends on
│   └── enums.py
├── application/use_cases.py
├── infrastructure/
│   ├── orm.py           SQLAlchemy model
│   └── repository.py    implements the port
├── adapters/http.py     the FastAPI router
├── public.py            what other modules may call
├── README.md
└── tests/
    ├── test_invoice_domain.py      no database, no FastAPI, milliseconds
    └── test_invoice_use_cases.py   an in-memory fake implementing the port
```

**The rule that makes it worth its cost:** `domain/` may import nothing. Not
SQLAlchemy, not FastAPI, not the application layer. The generated contract
enforces exactly that:

```toml
[layers.domain]
paths = ["modules/*/domain/*.py"]
may_import = []
forbid_packages = ["sqlalchemy", "fastapi", "starlette", "httpx", "redis"]
```

The moment the domain imports the ORM, the thing you were buying — a domain
testable in milliseconds with no database — is gone, and you are paying four
folders for nothing. So it is checked rather than remembered.

Use it when the rules are the product, when more than one entry point drives
the same behaviour, or when the domain must be tested exhaustively and fast.
It is the most expensive layout here. Most modules do not need it.

### Choosing

| | Reach for it when |
| --- | --- |
| `modular` | Default. A folder per layer, so a module grows without being reorganised. |
| `layered` | A table with an API and little else: five files is the whole module. |
| `screaming` | The verbs matter more than the nouns; capabilities arrive as files. |
| `hexagonal` | The domain must be testable with no database, or the rules are the product. |

You do not have to be right on day one. Layouts are per module, so the next one
can differ, and moving between them is a refactor inside one folder.

### What every layout guarantees

All four export the same three names, which is what lets everything else stay
layout-agnostic:

| Export | Used by |
| --- | --- |
| `router` | `main.py`, spliced in by the generator |
| `build_service(session, tenant_id)` | the HTMX overlay, workers, anything else |
| `CreatePayload` | the HTMX form handler, which must build one without knowing the layout |

Those are for the app. What *other modules* see is a fourth file, `public.py`,
and nothing else — next section.

---

## Communication between modules

**Queries through a facade, effects through events, nothing through `shared/`.**

Sooner or later module B needs data module A owns. There are four ways to get
it, and only one of them survives the day A changes:

| Way | What it costs | `contracts check` |
| --- | --- | --- |
| Import A's service, repository or entity | B depends on A's internals. Rename a method in A and B breaks; neither can become a service without the other. | `cross-module` |
| Raw SQL against A's tables from B | The same coupling, with nothing to see it. No import to grep; A renames a column and B fails in production. | `cross-module-sql` |
| Move the code to `shared/` | `shared/` becomes a second home for behaviour. A repository there is two modules sharing a table. | — (that is why the rule is explicit) |
| **Call a function in A's `public.py`** | A promises a function and a DTO; everything behind it stays A's to change. | passes, once declared |

### The facade

Every generated module has `modules/<name>/public.py`, and it is the only file
another module may import from it. Say the `asesor` module — an advisor that
answers questions about spending — needs `comprobante`'s spending by category.

The owner exposes a function and a DTO:

```python
# modules/comprobante/public.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .repositories import ComprobanteRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CategorySpending:
    category: str
    total_cents: int
    receipts: int


async def spending_by_category(
    session: AsyncSession, *, tenant_id: str, since: date, until: date
) -> list[CategorySpending]:
    repository = ComprobanteRepository(session, tenant_id=tenant_id)
    rows = await repository.spending_by_category(since=since, until=until)
    return [CategorySpending(category=c, total_cents=t, receipts=n) for c, t, n in rows]
```

The query itself lives in `comprobante`'s repository, next to the table it
reads. The caller imports the facade and nothing else:

```python
# modules/asesor/services/asesor_service.py
from modules.comprobante.public import spending_by_category

spending = await spending_by_category(session, tenant_id=tenant_id, since=start, until=end)
```

And says so in `contracts.toml`:

```toml
[modules.asesor]
depends_on = ["comprobante"]
```

`jfast new module` appends an empty `[modules.<name>] depends_on = []` for
every module it generates, so adding an edge is always a line a reviewer sees
change. The generated `public.py` ships with one example — `get_<name>(session,
*, tenant_id, <name>_id) -> <Name>Summary | None` — wired through that layout's
own repository.

### Why it takes the session and a tenant_id

- **The caller's session** puts the read in the caller's transaction: it sees
  the rows the caller wrote earlier in the same request, and one request never
  holds two connections. A facade that opened its own session would read a
  different snapshot and, under load, double the pool.
- **An explicit `tenant_id`** because the facade is called from places with no
  request to infer it from — a worker, a scheduled task, another module's
  service. An implicit tenant is exactly how a job ends up reading every
  tenant's rows ([Queues and events](queues-and-events.md) has the story).
  In a multitenant service (`--access tenant`, what tenancy implies) it is
  `tenant_id: str`: `None` would build the repository with no tenant filter
  and the facade would answer for every tenant. In a single-tenant service it
  is `tenant_id: str | None`, because the rows are written with no tenant and
  `None` is the only value that finds them; `jfast check --multitenant-ready`
  lists those signatures (`facade-tenant-optional`) to change when tenancy goes
  on.
- **DTOs, not entities.** An ORM entity drags its session and lazy relations
  across the boundary, and every column becomes part of the API the day someone
  reads it. A DTO is a promise you chose to make. `public.py` may not import
  FastAPI either: it has to work where there is no request.

### Effects go through events

A facade answers questions. When `asesor` needs to *react* to something
`comprobante` did — a receipt was categorised, so the advice is stale — it
does not get called back. `comprobante` publishes an event in the same
transaction as the write, through the outbox, and `asesor` subscribes:

```python
# modules/comprobante/services/comprobante_service.py -- next to the write
from jfastframework.events import Event

await outbox.publish(
    session, "comprobantes", Event(type="comprobante.categorized", data={"id": c.id}, key=str(c.id))
)
```

```python
# modules/asesor/tasks.py
from jfastframework.events import Event, subscribe
from jfastframework.tasks import TaskSession

@subscribe("comprobante.categorized")
async def refresh_advice(event: Event, session: TaskSession) -> None:
    ...
```

```toml
# contracts.toml
[modules.comprobante]
publishes = ["comprobante.categorized"]
```

`outbox.publish` queues one job per subscriber in the same transaction as the
write, and `jfast worker` runs it as the tenant that published. No broker is
involved: this works on the default PostgreSQL stack. `comprobante` never
learns that `asesor` exists and `asesor` declares no `depends_on` for it — so
there is no edge either way, and no cycle. An event nobody subscribes to is
refused in the request, and `contracts check` reports a subscription nobody
declares publishing. Delivery guarantees are in
[Queues and events](queues-and-events.md#events-between-modules); `@on(topic)`
over Kafka is for *other services*, not for modules of this one.

### The payoff: extracting a module

The day `comprobante` moves out into its own service
(`jfast new service comprobante`), the change on this side is one file:
`public.py` keeps its signatures and its DTOs, and its body becomes a call to
the new service through the [`http` plugin](http-client.md). The `session`
argument simply stops being used. `asesor`'s import line does not change,
because it never knew where the answer came from.

That only works if `public.py` was the only way in. A module that also reached
into `comprobante`'s repository, or queried its tables, has to be found and
rewritten first — which is what the checks below exist to prevent.

### What is checked

| Rule | Fires when |
| --- | --- |
| `cross-module` | a module imports anything of another module other than `modules/<other>/public.py` |
| `undeclared-dependency` | it imports `modules.<other>.public` without `<other>` in its `depends_on` |
| `module-cycle` | the graph of declared `depends_on` plus actual facade imports has a cycle |
| `public-leak` | `public.py` imports or re-exports an ORM entity, or imports `fastapi`/`starlette` |
| `cross-module-sql` | a string in one module holds SQL naming a table another module owns |

The messages, waivers and the one switch that turns them off are in
[Contracts](contracts.md#between-modules).

---

## The layout is remembered

`jfast new module` records the choice in `jfast.toml`:

```toml
# How each module was generated, so later commands know where a
# new file belongs. Written by `jfast new module`.
[modules.invoice]
layout = "hexagonal"
ui = "api"

[modules.catalogue]
layout = "layered"
ui = "api"
```

Asking again each time eventually gets a different answer, and guessing from
the folders on disk breaks the moment somebody adds one. The runtime ignores
the table — it is CLI bookkeeping that happens to live in the file that was
already there.

---

## Modules register themselves

`main.py` ships with two markers, and the generator splices into them:

```python
from modules.invoice import router as invoice_router
# [jfast:imports]

ROUTERS: list[APIRouter] = [
    invoice_router,
    # [jfast:routers]
]

app = create_app(routers=ROUTERS)
```

Idempotent: generating the same module twice does not mount it twice. **Keep
the markers.** Without them the generator prints what to paste rather than
guessing a line number — it will not fail your scaffold over it, but it stops
registering for you.

---

## UI: JSON, HTML, or both

```bash
jfast new module invoice --ui api    # JSON only (default)
jfast new module invoice --ui htmx   # JSON plus server-rendered pages
```

`--ui htmx` is an *overlay*, composed on top of any layout rather than
duplicated per layout. It adds:

```
modules/invoice/web.py           HTML router, mounted at /ui/invoices
templates/invoice/index.html     the page
templates/invoice/_rows.html     the table body fragment
templates/invoice/_row.html      one row
templates/base.html              only if the project has none
```

**The HTML surface lives under `/ui/`.** The JSON router already owns
`/invoices` and declares the same verbs on it, so two routers on one prefix
meant whichever registered first answered — browsing returned JSON, and the
form POSTed into the API handler. Distinct prefixes mean neither can shadow the
other regardless of the order in `main.py`.

| Path | Returns |
| --- | --- |
| `/invoices` | JSON |
| `/ui/invoices` | the page, or a fragment for an `hx-get` |

The JSON router stays. A module serves both surfaces from the same rules, which
is the point: the HTML views are not a second implementation.

### Partial rendering

HTMX sends `HX-Request: true` and expects a fragment. `render()` handles both
from one handler:

```python
return render(request, "invoice/index.html", {"page": page},
              partial="invoice/_rows.html")
```

Browser navigation gets the whole page. `hx-get` gets just the rows. One
handler, one context, no duplicated markup — the page `{% include %}`s the same
fragment it returns.

### Errors

With the `web` plugin enabled, a `JFastError` raised during an HTMX request
comes back as an HTML fragment instead of `problem+json`. HTMX swaps the
response body into the DOM, so JSON would render to the user as raw text. Plain
requests still get `problem+json`, which keeps a mixed API/web service honest on
both surfaces.

---

## Fields: generate the module you meant

A module generated with no fields carries an example -- `name`,
`description`, `is_active` -- that fits no real domain. The first module built
on 0.1.0a10 went from 654 generated lines to 276 kept. Say what the module
holds instead, and every place the example used to be gets the real fields:

```bash
jfast new module presupuesto \
  --fields "cartera_id:int, mes:str(7), gasto:money, leida:bool=false, nota:text?" \
  --unique "cartera_id,mes"
```

That writes the entity (with its unique constraint), the create, update and
read models with matching limits, a repository finder per unique key, the rule
that turns a taken key into a readable 409 -- on create and on the update that
touches the key -- the `public.py` DTO with the real fields, and tests that
exercise every one of them. It works for all four layouts. For a module whose
fields are not known yet, or that holds nothing but relations:

```bash
jfast new module alerta --bare      # the structure, no fields, no example
```

### The grammar

One field per comma; commas inside parentheses do not split.

```
field := name ":" type ["?"] ["=" default]
```

| Type | Python | Column | On the wire |
| --- | --- | --- | --- |
| `int` | `int` | `INTEGER` | |
| `bigint` | `int` | `BIGINT` | |
| `str(N)` | `str` | `VARCHAR(N)` | `max_length=N`; `min_length=1` unless nullable |
| `str` | `str` | `VARCHAR(255)` | as `str(255)` |
| `text` | `str` | `TEXT` | `min_length=1` unless nullable |
| `bool` | `bool` | `BOOLEAN` | |
| `float` | `float` | `FLOAT` | |
| `decimal(P,S)` | `Decimal` | `NUMERIC(P,S)` | `max_digits=P, decimal_places=S` |
| `money` | `int` | `BIGINT` | integer minor units: 1050 is 10.50 |
| `date` | `date` | `DATE` | |
| `datetime` | `datetime` | `TIMESTAMPTZ` | `AwareDatetime`: a naive one is a 422 |
| `json` | `dict[str, Any]` | `JSONB` (`JSON` off PostgreSQL) | |
| `enum(a,b,...)` | a `StrEnum`, `<Module><Field>` | `VARCHAR` + `CHECK (field IN ('a', 'b'))` | the enum: any other value is a 422 |

- `?` makes it nullable, and optional in a create.
- `=value` is the default, written in the type's own syntax: `=0`, `=false`,
  `=pending`, `="two words"`, `=0.00`. `date`, `datetime` and `json` take none:
  a default "now" is a decision about a zone, and belongs in the service.
- `--unique "a,b"` makes the pair unique per tenant; repeat it for more keys.
  Every constraint is named, because two tenant-first constraints on one table
  would otherwise share a name.
- `money` is integers on purpose. Floats do not add up to the cent; a
  `decimal(12,2)` is the alternative when the amount really has a fixed scale.
- `enum(personal,empresa,otra)` writes `class CarteraTipo(StrEnum)` into the
  module's enums file (`domain/enums.py` in hexagonal), the same shape `jfast
  new enum` writes, and uses it everywhere the field appears: the column, the
  create/update/read models, the domain entity and `public.py`. Values are
  snake_case, at least two; the member is the value upper-cased
  (`in_review` is `IN_REVIEW`). The default is one of them (`=personal`), `?`
  makes it optional, and it may be part of a `--unique` key.
  The column is SQLAlchemy's `Enum(native_enum=False)` storing the member's
  *value* -- a `VARCHAR`, so a row reads back as the enum and the in-memory
  SQLite a test uses creates the same table -- plus a named CHECK
  (`ck_<table>_<field>`) built from the enum. Not a native PostgreSQL `ENUM`:
  autogenerate renders this pair as one `sa.Enum(...)` column and one
  `sa.CheckConstraint`, where `create_constraint=True` wrote the CHECK twice.
  Autogenerate does not compare CHECK constraints, so a member added later
  needs a migration that drops `ck_<table>_<field>` and creates it again
  (and widens the column if the new value is longer than the longest one).

Every mistake is refused before a file is written, with the fix in the
message: an unknown type lists the ones there are, `id` or `tenant_id` says
they are already on every entity, `str(0)` points at `text`, and a name that
would shadow something the generated code uses (`payload`, `json`,
`model_...`) asks for another name.

`tenant_id` is on every generated entity whatever the fields -- see
[going multitenant later](multitenancy.md#going-multitenant-later): it
is what turns "we have a second customer" into a backfill instead of a schema
rewrite.

`--ui htmx` is refused with `--fields` or `--bare`: the pages it draws are for
the example fields. Generate the API module and write the pages for yours.

### Who may call the routes

The generated routes read the caller the way the service is configured, so a
module is never more open than the service around it:

| `jfast.toml` enables | Routes | Tenant |
| --- | --- | --- |
| `tenancy` | `tenant_id: str = Depends(current_tenant)` in the factory | the caller's; 401 without a session, 403 without a tenant |
| `auth` or `accounts` | `APIRouter(dependencies=[Depends(require_auth)])` | none: one customer |
| neither | open | whatever the request resolved to -- nothing |

`--access open|auth|tenant` overrides it for one module.

### Every module is formatted and typed

What the generator writes passes the project's own gates -- `ruff check .`,
`ruff format --check .`, `mypy .` (strict) and `pytest` -- in every layout and
every form; [the generated project's gates](migrations-and-tests.md#the-generated-projects-gates)
has the details. New files are also run through the project's ruff when it is
installed (it is in `requirements-dev.txt`), because a long module name makes
lines no template can wrap in advance. Without ruff the files are written as
rendered, and `ruff format .` finishes the job.

Every module also gets an empty `tasks.py`: the place its `@task` and
`@subscribe` declarations go, found at boot by the API and by `jfast worker`
alike. See [queues and events](queues-and-events.md).

---

## Table names

Table names are pluralised: `order` → `orders`, `category` → `categories`.
Not cosmetics — singular nouns collide with SQL reserved words far more often
than plurals (`order`, `user`, `group`). Override when it guesses wrong:

```bash
jfast new module order --table sales_orders
```

A project that names its modules in Spanish gets Spanish plurals. Say so once,
in `jfast.toml`:

```toml
[scaffold]
language = "es"
```

and `camion` becomes `camiones`, `sucursal` becomes `sucursales`, `lapiz`
becomes `lapices`, `lunes` stays `lunes`. In a compound name the head noun
takes the plural, as it does in Spanish: `orden_compra` → `ordenes_compra`.
`--language en` or `--language es` overrides the project for one module;
anything else is refused. Without the setting, English rules apply, as before.

---

## Combinations

Four layouts × two UIs = eight, from six template trees. Composition rather
than eight copies that drift apart:

```bash
jfast new module invoice --layout modular --ui htmx
jfast new module ledger  --layout hexagonal
```

Every combination is exercised in CI. `scripts/smoke_layouts.sh` generates all
four layouts and asserts each renders, imports, mounts its routes and passes
its own contract; `scripts/smoke_htmx.sh` submits the actual form on each; and
`scripts/smoke_generated_quality.sh` runs ruff, ruff format, mypy and pytest on
every layout with example fields, `--fields` and `--bare`.

---

## After generating

```bash
jfast contracts check                       # before anything else
pytest modules/invoice/tests
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
```

Or all of it at once, with the database up and the migration applied first:

```bash
jfast dev
```

See [The local loop](dev.md).

Read the generated migration before applying it. Autogenerate misses
server-side defaults, enum changes and index renames.

Generated files carry a `.jfast-template` stamp recording which template and
which framework version produced them. That stamp is what a future
`jfast upgrade` uses to show a diff instead of a rewrite.
