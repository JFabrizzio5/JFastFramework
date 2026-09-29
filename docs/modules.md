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

## Table names

Table names are pluralised: `order` → `orders`, `category` → `categories`.
Not cosmetics — singular nouns collide with SQL reserved words far more often
than plurals (`order`, `user`, `group`). Override when it guesses wrong:

```bash
jfast new module order --table sales_orders
```

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
its own contract; `scripts/smoke_htmx.sh` submits the actual form on each.

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
