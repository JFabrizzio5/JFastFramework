---
name: create-module
description: Scaffold a domain module, choosing its layout (layered, modular,
  screaming or hexagonal) and whether it serves JSON, HTML, or both.
when_to_use: The user asks for a new business entity, resource, CRUD surface,
  or database table — "add orders", "we need an invoices endpoint".
when_not_to_use: The change belongs inside an existing module, or it is a
  cross-cutting capability such as auth, caching or metrics — use
  create-plugin for those.
---

## Preconditions

The `database` plugin must be enabled:

```bash
jfast plugins list --json | jq -r '.[].name'
```

If it is absent, add `"database"` to `[plugins].enabled` first. A module
without it fails at `ctx.require("db.engine")`. For `--ui htmx`, the `web`
plugin must be enabled too.

## Choose the shape before generating

**Layout.** Four. **Use `modular` unless the user asks for another** -- it is
the default, and a project created from scratch starts with it. Reach for the
others when the module's shape calls for it:

| Pick | Splits by | When |
| --- | --- | --- |
| `modular` (default) | a package per layer, plus `validations/` | The starting point. Same boundaries as layered, with room: a layer that outgrows one file does not force a reorganisation, and rules needing the table have `validations/`. |
| `layered` | a file per layer: `router` / `service` / `repository` / `models` / `schemas` | A table with an API and little else. Five files is the whole module. |
| `screaming` | a file per use case, in `use_cases/` | Real domain rules, and capabilities that keep being added. The directory listing should read as the feature list. |
| `hexagonal` | `domain/` (entities + ports), `application/`, `infrastructure/`, `adapters/` | The domain must run with no database, or a port will get a second adapter. Costs four directories and a mapping layer. |

Both `screaming` and `hexagonal` keep the domain framework-free; the port is
what separates them. Take `hexagonal` when something other than PostgreSQL will
implement the repository, `screaming` when nothing will.

**UI.**

| Pick | When |
| --- | --- |
| `api` (default) | JSON consumers only. |
| `htmx` | The user will look at this in a browser. Adds pages *and* keeps the JSON router. |

## Steps

1. **Name it.** Singular, snake_case, the domain noun: `order`, `invoice`,
   `payment_method`. Not `order_manager`, not `orders_service`. The table name
   is pluralised for you.

2. **Generate.**

   ```bash
   jfast new module invoice                            # modular, JSON (the default)
   jfast new module invoice --layout layered           # a file per layer
   jfast new module invoice --layout screaming         # use case per file
   jfast new module invoice --layout hexagonal         # ports and adapters
   jfast new module invoice --ui htmx                  # + server-rendered pages
   ```

   The layout lands in `jfast.toml` under `[modules.invoice]`, and the module's
   own `README.md` maps its files to their roles — read it before adding a
   file, because modules in one service may differ.

3. **Match the contract to the layout.** `contracts.toml` carries the layer
   paths of one layout only. A module generated in a different one matches no
   layer glob and is checked by the service-wide rules alone:

   ```bash
   jfast contracts init --layout hexagonal --force
   jfast contracts check
   ```

   If the service already holds modules of another layout, say so rather than
   regenerating over their contract.

4. **Fill it in.** The module's `README.md` has the full map; the short form:

   | Layout | Table | Wire models | Rules |
   | --- | --- | --- | --- |
   | `layered` | `models.py` | `schemas.py` | `service.py` |
   | `modular` | `models/invoice_entity.py` | `models/invoice_models.py` | `services/`, plus `validations/` for a rule needing more than the payload |
   | `screaming` | `storage.py` | `http.py` | `invoice.py` for one object's own state; a new file in `use_cases/` when it needs the repository |
   | `hexagonal` | `infrastructure/orm.py` | `adapters/http.py` | `domain/entities.py` for one object's own state; `application/use_cases.py` when it needs the port |

   The domain file — `invoice.py`, or `domain/entities.py` — takes **no
   framework imports**. In `hexagonal`, do not reach past the port: add a
   method to `domain/ports.py` and implement it in both adapters. A check that
   only reads the payload is a `Field` constraint, not a rule.

   Replace the `name` / `description` / `is_active` placeholders rather than
   accumulating around them, and raise `ConflictError`, `NotFoundError`,
   `ValidationError` from `jfastframework.errors` — they already serialise to
   problem+json.

5. **Check it was mounted.** The generator splices the import and the router
   into `main.py` at the `# [jfast:imports]` and `# [jfast:routers]` markers.
   Every layout exports `router` from the module package, so the line is the
   same either way, and `--ui htmx` adds a second one for the HTML router. If
   the markers were edited away the generator prints the lines instead of
   guessing a line number — paste them and put the markers back.

6. **Migrate.**

   ```bash
   alembic revision --autogenerate -m "add invoices"
   alembic upgrade head
   ```

   Read the generated migration before applying it. Autogenerate misses
   server-side defaults, enum changes and index renames.

7. **Finish the module README.** Replace the placeholder comments; delete the
   "Invariants" section if there are none. Do not leave placeholder text in the
   tree.

## Verification

```bash
pytest modules/invoice/tests
ruff check modules/invoice
jfast contracts check
curl -s localhost:8000/openapi.json | jq '.paths | keys'
```

The generated tests use a fake repository and need no database. If they fail,
the rules are wrong, not the environment.

With `--ui htmx`, also confirm partial rendering actually differs:

```bash
curl -s localhost:8000/invoices | head -3
curl -s -H 'HX-Request: true' localhost:8000/invoices | head -3
```

## Common mistakes

- Business rules in the HTTP layer. They belong in the service or the use case.
- A framework import in a screaming or hexagonal module's domain file. That
  rule is in the wrong file.
- Assuming a module's shape from its neighbour. Read `jfast.toml` and the
  module's `README.md`.
- Generating a non-default layout and leaving the layered contract in place, so
  the check passes without having looked at a single layer.
- Reusing the read schema for creates. Input and output contracts diverge fast.
- Applying an autogenerated migration unread.
- Generating a module for what is really one field on an existing model.
