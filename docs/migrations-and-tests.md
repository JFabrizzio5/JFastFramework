# Migrations and tests

Both are wired into every generated service. Neither is something you set up.

---

## Migrations (Alembic)

`jfast new service` writes `alembic.ini`, `migrations/env.py`,
`migrations/script.py.mako` and `migrations/versions/` whenever the `database`
plugin is enabled.

```bash
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
alembic downgrade -1
alembic history
```

Two things it does that a stock `alembic init` does not:

**The DSN comes from the application's settings.** `env.py` reads
`JFAST_DB_DSN` through the same `DatabaseSettings` the service uses, and
`alembic.ini` deliberately has no `sqlalchemy.url`. A migration that *can* run
against a different database than the service eventually will, at the worst
possible moment.

**Models are imported automatically.** Autogenerate only sees tables whose
classes have been imported. A forgotten import produces an empty migration, and
the missing table is discovered in production. `env.py` walks `modules/` and
imports two names per module, `models` and `storage`, so no import list has to
be maintained. That covers all four layouts, though not all four the same way:

| Layout | What registers the table |
| --- | --- |
| `layered` | `modules/<m>/models.py` |
| `modular` | `modules/<m>/models/`, whose `__init__.py` re-exports the entity |
| `screaming` | `modules/<m>/storage.py` |
| `hexagonal` | neither name exists. Importing `modules.<m>.models` still runs `modules/<m>/__init__.py` first, and that imports `.adapters.http`, which reaches `infrastructure/orm.py` |

The absent name is expected and swallowed; a `ModuleNotFoundError` naming
anything else is re-raised, because an import error inside a module must not
turn into an empty migration. A service with one module of each layout puts all
four tables in `Base.metadata` — but hexagonal gets there through the package
`__init__`, not through a filename `env.py` looks for.

It also enables `compare_type` and `compare_server_default` — without them
autogenerate silently misses column type changes and default changes, the two
edits people most often assume it caught.

### Constraint names are pinned

`jfastframework.db.Base` sets a `naming_convention`. Without it PostgreSQL
invents constraint names and autogenerate produces different diffs on different
machines. With it, a primary key is always `pk_<table>`, a foreign key always
`fk_<table>_<column>_<referred>`.

Adopting it on a database that already has auto-named constraints needs a
one-time migration. Do that before the fleet grows.

### Timestamps carry a zone (breaking, needs a one-time migration)

`TimestampMixin` used to map `created_at` / `updated_at` to
`TIMESTAMP WITHOUT TIME ZONE`. Values came back as `2026-08-29T20:55:15` —
no `Z`, no offset — and every JavaScript client read that as *local* time, so
a row written now rendered hours away for anyone off UTC. The mixin now uses
`jfastframework.db.UTCDateTime`, which is `TIMESTAMPTZ` on PostgreSQL and
attaches UTC on the way out everywhere else.

Every table built on the mixin needs converting once. `autogenerate` sees the
type change (`compare_type` is on) and writes a bare
`ALTER COLUMN ... TYPE timestamptz` with no `USING`. That does not fail — it
converts through the implicit cast, which reads every stored value in the
*server's* `TimeZone`. On a server not set to UTC that shifts the whole table
and nothing complains. Write it by hand instead:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

`AT TIME ZONE 'UTC'` is the load-bearing part: it states that the stored values
were UTC all along. They were — `now()` written into a `timestamp` column
stored the UTC instant with the zone stripped.

The `USING` form rewrites the table and holds an `ACCESS EXCLUSIVE` lock while
it does. Schedule it like any other rewrite on a large table.

Writes are stricter afterwards: a naive `datetime` raises rather than being
stored under an assumed zone. Use `datetime.now(UTC)`.

### Read the migration before applying it

Autogenerate is a draft, not a plan:

- A **rename** is rendered as a drop plus an add. On a table with rows, that is
  silent data loss. The generated revision says so, but only when it actually
  contains a drop and an add on the same table — a warning in every revision is
  one nobody reads. Either use `op.alter_column(..., new_column_name=...)` or
  copy the values between the two operations; `migration check` reads both.
- **Data migrations** are not written at all.
- Index renames and enum membership changes are frequently missed. See
  [Enums](datastores.md#enums-which-half-of-the-guarantee-you-are-buying) for
  what the column does and does not enforce either way.
- A **new `NOT NULL` column** is repaired for you when the model gives a scalar
  `default=`: the revision adds the column with a matching `server_default`,
  backfills, and drops the default again in the same migration. Alembic only
  looks at `server_default`, so without this it emitted DDL PostgreSQL rejects
  outright on any table that has rows. A `default=` it cannot turn into SQL — a
  callable like `uuid4`, or no default at all — is announced in the revision
  instead, because there is nothing to backfill with.

### Read it with `jfast migration check`

```bash
jfast migration check              # every unapplied revision
jfast migration check --all        # applied ones too
jfast migration check --json       # for an agent, or CI
jfast migration plan               # the next risky revision, and the safe rewrite
jfast migration plan -r 7cd507730ac7   # that one, by the id alembic printed
```

`check` parses `migrations/versions/*.py` with `ast` and **never imports them**.
A revision imports the project's models, and the environment the CLI runs in is
usually not the environment those imports resolve in — a checker that only works
when the project already imports is unavailable exactly when it is needed.

#### What "unapplied" is decided by

The head comes from `alembic_version` on the database; the chain comes from
each revision's own `revision` and `down_revision`, read out of the file. Every
revision reachable by walking `down_revision` back from the head is applied, and
the rest are what `check` reports. Both spellings count — `revision = "..."` and
the annotated `revision: str = "..."` that `script.py.mako` writes — and both
are matched against the id **alembic** stamped, not the filename. Without a
database there is no head, so `check` falls back to reporting every revision and
says `scope: all`.

| Finding | Severity | What it means |
| --- | --- | --- |
| `migration-add-not-null` | critical | `add_column` with `nullable=False` and no `server_default`. PostgreSQL rejects it outright the moment the table has one row |
| `migration-rename` | critical | A `drop_column` on a table the same revision adds columns to, with nothing between them copying the values. Autogenerate renders a rename exactly like this, and the data goes with the drop |
| `migration-timestamptz-no-using` | critical | `ALTER COLUMN ... TYPE timestamptz` with no `USING`. Does not fail; silently shifts the column. See above |
| `migration-drop-table` | critical | Every row is lost and `downgrade` recreates the table empty at best. `TRUNCATE` counts |
| `migration-drop-column` | high | The column and its contents are gone; `downgrade` brings back an empty column |
| `migration-type-change` | high, or medium with `postgresql_using` | Rewrites the table under `ACCESS EXCLUSIVE`: no reads, no writes, until it finishes |
| `migration-set-not-null` | high | `alter_column(nullable=False)` scans the whole table to validate, holding the lock |
| `migration-drop-constraint` | medium | The guarantee stops holding immediately; re-adding it needs a validating scan |
| `migration-index-lock` | medium | `create_index` without `postgresql_concurrently=True` blocks every write for the duration |
| `migration-no-downgrade` | low | Not a defect. But `alembic downgrade -1` will report success and change nothing |
| `migration-raw-sql` | low | An `op.execute` statement no check here reads. Not a verdict — the absence of one |

Severities, the `Finding` shape and `--fail-on` are the same ones `jfast analyze`
uses. `--fail-on` defaults to `high`; a risk at or above it exits **4**
(`Code.MIGRATION`).

Every finding points at the line of the operation it is about, and every remedy
`plan` prints as Python is Python you can paste — `ast.parse` runs over all of
them in the test suite, and the rename remedy is applied to a real PostgreSQL
and the row read back.

#### A rename that carries its data is not reported

`migration-rename` is suppressed when a statement between the `add_column` and
the `drop_column` copies the old column into the new one:

```python
op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))
op.execute("UPDATE posts SET media_url = image_url")
op.drop_column("posts", "image_url")
```

The rule is narrow on purpose: a literal SQL string — `op.execute("...")` or
`op.execute(sa.text("..."))` — that names both columns, assigns to the new one
(`UPDATE ... SET <new> = ...`, or `INSERT INTO ... (<new>) ... SELECT ...`), and
sits **between** the two line numbers. A copy after the drop does not count; it
cannot, the column is gone by then. A backfill built at runtime, or run from a
separate script, is not visible here and the finding stands — which is honest,
because a revision that does not copy the values is a revision that loses them.
There is no waiver comment: writing the backfill *is* the way to silence it.

`migration-drop-column` is still reported at `high`. Dropping a column that a
running copy of the service may still read is worth stopping for, rename or not.

#### What `op.execute` is read for

Arbitrary SQL cannot be judged without a SQL parser. A named list of forms is
read — `DROP TABLE`, `TRUNCATE`, `ALTER TABLE ... DROP COLUMN`,
`... SET NOT NULL`, `CREATE INDEX` (without `CONCURRENTLY`), and
`ALTER COLUMN ... TYPE` — and each produces the same finding the equivalent
`op.*` call would. Anything else, including a statement assembled at runtime,
is reported as `migration-raw-sql` at `low`: **not checked**, rather than passed.
A `✓` on a statement nobody parsed claims a review that did not happen.

Also deliberately absent: a `create_index` or an `alter_column` on a table the
same revision creates — that table is empty by construction, and reporting it is
the false positive that gets the whole command muted. Widening a `VARCHAR` is
not reported either: PostgreSQL takes a longer `varchar` as a catalogue edit, not
a rewrite. That one needs `existing_type=` on the call to be recognised — the
old length is the only thing that says which direction this is, and autogenerate
always writes it. `op.alter_column("widgets", "name", type_=sa.String(200))` on
its own is reported at `high`, and narrowing (`String(200)` → `String(50)`)
always is. And a type change carrying `postgresql_using` is reported at `medium`
rather than `high`: the rewrite and its `ACCESS EXCLUSIVE` lock are unchanged and
still worth knowing, but the author wrote the conversion out, so it does not
block the default `--fail-on high`.

#### How this relates to the `env.py` hook

They are two halves of the same problem at different moments.
`migrations/env.py` installs a `process_revision_directives` hook that repairs a
scalar-default `NOT NULL` column **while the revision is being generated** — the
one case where the fix is derivable from the model. `migration check` reads
revisions that **already exist**: hand-written ones, ones merged from a branch,
and ones generated before that hook existed. A revision autogenerated by a
current service should therefore never trip `migration-add-not-null`. If one
does, it was written by hand or generated by an older version, and the finding
is correct.

#### Row counts need a database

The `Reason` line in `plan` states a real row count when a DSN resolves —
`--dsn`, then `JFAST_DB_DSN`, then `.env` in the project root:

```
Migration:  0004_add_status
File:       migrations/versions/0004_add_status.py
Risk:       CRITICAL
Reason:     status is NOT NULL and widgets has 812 rows
Database:   connected

  migration-add-not-null: widgets.status is NOT NULL with no server_default
  PostgreSQL rejects `ALTER TABLE ... ADD COLUMN ... NOT NULL` with no default
  the moment the table has a single row, so this passes on an empty database
  and stops the deploy on the one that matters. Add the column nullable,
  backfill it, then set NOT NULL.

Recommended:
  1. add the column nullable
  2. backfill it
  3. add the NOT NULL constraint
  4. `op.alter_column('widgets', 'status', nullable=False)` in a follow-up revision, once the backfill has committed
```

`Database:` is one of `connected`, `unavailable` or `skipped` — the last one is
`--no-db`. `Reason:` finishes the sentence with the real count when the database
answered: `has 812 rows`, `has 1 row`, `is empty on this database`, or
`does not exist on this database yet`.

When none resolves, it says `has an unknown row count, treat as populated`. It
never reports a table as empty on no evidence: a checker that assumes the safe
case is a checker that goes quiet in production. `--no-db` skips the connection
entirely, which is what CI should use.

### Offline SQL

```bash
alembic upgrade head --sql > migration.sql
```

Runs `env.py` without connecting, which is also how CI verifies the wiring
without a database.

---

## Tests (pytest)

`jfast new service` writes `pytest.ini` and `conftest.py`. Generated modules
bring their own tests that pass immediately:

```bash
pytest                          # modules/ and tests/
pytest modules/invoice/tests    # one module
```

### Fixtures

`conftest.py` provides `app` and `client`, built with an **explicit** plugin
list:

```python
@pytest.fixture
def app():
    return build_test_app(plugins=["observability"], app_name="billing")
```

Explicit beats implicit here: a test that names its plugins cannot break
because someone changed a default in `jfast.toml`.

From `jfastframework.testing`:

| Helper | Does |
| --- | --- |
| `build_test_app(...)` | App with a named plugin list, no config file, no env |
| `client_for(app)` | Async HTTP client with the lifespan actually executed |
| `NullPlugin` | Records `register` / `startup` / `shutdown`, for ordering tests |
| `make_config(...)` | A `JFastConfig` without touching disk |

`client_for` matters more than it looks: plugins that open resources in
`startup` need the lifespan to run, and a bare `TestClient` skips it in async
contexts.

### What the generated tests actually test

They test the declared fields, not a placeholder: a create stores every field,
each `--unique` key is refused when taken and refused again on an update that
moves onto it (and not when a row keeps its own), a missing id is a 404, an
update changes only what was sent, an explicit `null` for a NOT NULL column is
a 422, and a string past its column's length is refused before the INSERT.

**Layered and modular layouts** — service-level tests against an in-memory
repository that holds real entity objects, so the type checker sees every
attribute a test reads. No database, no containers.

**Screaming layout** — two files, deliberately separated:

- `test_<module>_domain.py` — the entity and its rules. No database, no fakes,
  no event loop. If a test here ever needs a fixture, a rule has leaked out of
  the domain.
- `test_<module>_use_cases.py` — a fake repository and an event loop, nothing
  else.

Both mark themselves with `pytest.mark.asyncio` explicitly rather than relying
on `asyncio_mode = auto`, so they pass in a project that has not configured
pytest-asyncio.

### The generated project's gates

A generated service ships the configuration its gates run with, so `ruff
check .` means the same on every machine and every ruff release:

| File | What it pins |
| --- | --- |
| `ruff.toml` | the rule set (`E F W I UP B SIM RUF ASYNC`), line length 100, FastAPI's `Depends`/`Query`/... as immutable calls so B008 does not flag the framework's own idiom, and a `framework` import section for `jfastframework` between third-party and your code |
| `mypy.ini` | `strict = True`; `migrations/versions/` excluded -- revisions are reviewed as migrations, not typed as code |
| `pytest.ini` | `asyncio_mode = auto`, `modules` and `tests` as test paths |

Freshly generated, with nothing edited, all four pass:

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy . && pytest
```

`scripts/smoke_generated_quality.sh` holds that true on every change to the
framework: it generates `jfast start` single-tenant and multitenant and a
service with every plugin that needs no server of its own, adds a module per
layout with example fields, with `--fields ... --unique ...` and `--bare`, plus
one with every type in the grammar, and runs the four gates on each project --
and `jfast check --ci`, and for the multitenant one `jfast check
--multitenant-ready`, which must find nothing in code the generator wrote.
`JFAST_SMOKE_GENERATED=1 pytest tests/test_smoke_generated_quality.py` runs it
from the suite; it takes about a minute with a warm mypy cache.

### Integration tests

Tests needing a real PostgreSQL or Redis are not generated. Mark them
`@pytest.mark.integration` and keep them out of the fast suite; a framework-level
integration harness is PLAN.md phase 6.

---

## What CI should run

```bash
pytest
ruff check src tests
mypy src
jfast doctor
```

Plus, for anything that touches templates:

```bash
bash scripts/smoke.sh              # renders both layouts, runs their tests, checks alembic
bash scripts/smoke_workspace.sh    # workspace, gateway, frontends, patching
bash scripts/smoke_generated_quality.sh   # every generated shape passes ruff, format, mypy, pytest
bash scripts/smoke_upgrade.sh      # the previous release's project, upgraded to this checkout
```

Templates are the part that breaks silently — they render fine and produce code
that does not import. `pytest` alone does not catch that.
