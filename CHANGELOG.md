# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/) with pre-release identifiers per
[PEP 440](https://peps.python.org/pep-0440/). While the API is pre-alpha, services
pin exactly (`jfastframework==0.1.0a5`); a compatible-release pin (`~=`) starts
making sense at 0.2.

## Renumbering

The entries below were originally numbered `0.1.0` through `0.7.0`. That numbering
overstated the maturity of the code. Nothing has ever been published; the workspace
file format is about to change; the Redis queue backend does not implement the
visibility timeout its own contract documents; the RabbitMQ and Kafka backends have
never been run against a real broker.

The package therefore restarts at `0.1.0a1`. The history is kept verbatim as a
development log — it records what was built and when — but those numbers were never
releases and were never installable. `pip install jfastframework` will not resolve a
pre-release without `--pre`, so the packaging tool enforces the warning rather than a
sentence in a README.

Subsystem-level maturity lives in [STATUS.md](STATUS.md), which is the file to read
before depending on any single part of this.

## [Unreleased]

### Added

- Added the opt-in `pene` and `vagina` terminal novelty imports. They print
  bundled terminal art, and their combined helper opens a configured YouTube
  video in a new browser tab.

## [0.1.0a8] - 2026-09-03

One process is not the shape this runs in.

Every number in this framework describes a single process -- a token store, a
connection pool, an engine map. The Dockerfile it generates ends in `uvicorn
--workers $JFAST_WORKERS`, one per CPU. Nothing had ever multiplied the two, so
the arithmetic that decides whether a deployment works was never done.

### Added

- **`[plugin.database] server_max_connections`**, and a `jfast check` finding
  that does the multiplication. `pool_size = 10` plus `max_overflow = 20` is 30
  per process, and eight workers make 240 against a PostgreSQL whose own
  default ceiling is 100 -- from one service. The first sign of that was
  `FATAL: sorry, too many clients already`, in production, raised against
  whichever service connected *after* the one that took the room. `0` turns the
  check off for a managed instance sized from RAM.

- **A finding for a database-per-tenant nobody opens.** Setting
  `tenant_dsn_template` says each tenant has its own database, and nothing
  generated reads it: `jfast new module` depends on `session_dependency`, which
  is the shared primary, while routing to a tenant's own database is
  `tenant_session_dependency`. Nothing fails and nothing leaks -- the rows carry
  `tenant_id` and the repository filters on it -- but they are all in the
  primary while the per-tenant databases stay empty.

- **A queue job could run twice, by default.** The claim is invisible to other
  workers for `visibility_timeout` and nothing extends it while a handler runs,
  so a job that outlives the window is claimed again -- by another worker, while
  the first is still inside it. Both numbers defaulted to 300 seconds and lived
  in different places (`[plugin.queue] visibility_timeout` and the worker's
  `job_timeout`), so the shipped pair raced at the boundary and raising one
  without the other made the duplicate certain. A charge taken twice, an email
  sent twice, and neither run aware of the other. The worker derives its ceiling
  from the backend now -- 80% of the window, leaving room for the `nack` to land
  -- and refuses to start when a `job_timeout` is given that reaches past it.
  RabbitMQ publishes no visibility timeout, because it redelivers on the
  connection rather than on a clock, and is left alone.

- **`mail` refuses a backend that delivers nothing in production.** The default
  is `console`, deliberately -- nobody emails a real customer from a laptop --
  and in production it means every message is printed to stdout, `send` reports
  success, nothing bounces and no queue backs up. The verification link, the
  password reset and the invoice simply never arrive, and the only symptom is a
  customer saying so a week later. The `smtp` branch already refused to start
  with credentials missing, for exactly this reason; `console` and `memory` now
  refuse for the same one.

- **Channels on the memory backend are named in production.** The memory
  backend is process-local, which one worker per CPU turns into a publish that
  reaches the subscribers in one worker out of N. It is the right choice for a
  channel each worker should act on separately and the wrong one for a side
  effect that must happen once; nothing in the framework can tell those apart,
  so the warning names the channels and leaves the decision there. `websocket`
  has no such ambiguity and already refuses to register without Redis.

### Changed

- **The default pool is 5 + 5, not 10 + 20.** Thirty per process was a number
  chosen as if one process were the whole service; it is not, and the check
  above failed the tree this framework's own `jfast start` generates -- which
  is the right way to find out. Ten per worker is 80 across the default eight,
  with room beside it for everything else that connects. Ten is not small for
  an async service either: a connection is held while a query runs, not for the
  length of a request. Raise it against a server sized for it, and raise
  `server_max_connections` to say the server is.

### Fixed

- **`TenantPoolExhausted` is a 503.** Every tenant engine being busy is
  backpressure: the service is healthy, it is at capacity, and the same request
  succeeds a moment later. As a bare `RuntimeError` it reached the unhandled
  handler and came back `500 "An unexpected error occurred"` -- which tells a
  client to stop and a reader to hunt for a defect, while hiding the one signal
  that says raise `tenant_max_engines`. `RuntimeError` stays in the bases.

### Audited, and found correct

Written down because "we looked" is worth as much as "we fixed" to whoever
reads this next, and because each of these was checked by running it:

- One configured database builds exactly one engine, and the per-tenant map is
  lazy: nothing is opened for a tenant that never arrives.
- The tenant LRU holds its ceiling under pressure. An engine is never disposed
  while a request holds it -- the entry leaves the map first and closes on the
  last release -- and when every engine is busy a new tenant raises rather than
  opening one more.
- The repository raises on a tenant-scoped model with no `tenant_id` column
  rather than returning every tenant's rows.
- JWT verification refuses a refresh token used as a bearer, a wrong signing
  key, `alg=none`, an expired token and a foreign audience. A token with no
  `typ` is accepted, which is correct for an external identity provider.
- Local storage refuses `../`, backslashes, percent-encoded traversal, absolute
  paths and null bytes, each with the key in the message; none escaped the disk
  root.
- `X-Forwarded-For` from an untrusted peer is ignored, HSTS is sent only over
  HTTPS, and a body past the limit is a 413 in problem+json.

### Fixed -- the session, which was also per process

- **`auth` minting sessions with no shared store now refuses to start in
  production.** Without the `cache` plugin the token store is in memory, which
  is per worker, and both halves of session security are then per worker with
  it: a logout revokes on the process that served it and nowhere else, so the
  token keeps working on the others; and a refresh reaching any worker but the
  issuing one finds no family and is answered `401 "this session has been
  revoked"` -- a revocation that never happened, three times in four on four
  cores. The old answer was a warning at boot, in a JSON log, which is not
  read. Both failures are reproduced in `tests/test_session_store.py` before
  the refusal is asserted. A service that only verifies tokens minted elsewhere
  keeps starting: it holds no session to lose.

- **`jfast check` reports the same configuration as HIGH at any environment.**
  The refusal lands in production, and a service is developed at
  `env = "local"` -- so without this the boot that fails is the deployment.

### Fixed

- **A missing extra names the command that installs it.** A plugin imports its
  client library inside `register`, so an uninstalled extra surfaced as
  `No module named 'qdrant_client'` -- the distribution's name, not the one
  anybody types -- from `create_app`, `jfast doctor` and `jfast check` alike.
  `PluginMeta.extra` had held `jfastframework[qdrant]` the whole time and the
  failure never reached it.

- **The default CSP follows what the service renders.** `'unsafe-inline'` and
  the HTMX CDN are there for pages: the generated HTMX base carries an inline
  handler and `/docs` is an inline `SwaggerUIBundle` call. A JSON API renders
  neither, and kept both in production with the schema already closed. The
  allowance now arrives with the `web` plugin, the way the docs CDNs already
  arrived with the docs.

- **Upper bounds on what this framework is built on.** `fastapi`, `pydantic`
  and `pydantic-settings` were floors only, which is a promise about code that
  does not exist yet. `starlette` was worse: imported directly by nine modules
  here and declared by none of them, so the version this runs on was whatever
  FastAPI pulled -- and FastAPI's own requirement is `starlette>=0.46.0` with
  no ceiling. Raising a bound is now a release with a test run behind it.

- **A skip caused by a blocked prerequisite is no longer a pass.**
  `jfast check --only plugins` exited 0 on a `jfast.toml` that does not parse:
  the full battery exits 2 because the config check reports it, `--only`
  deselects that check, and the skip left behind read as success -- to a
  pipeline, silently. `CheckResult.blocked_by` carries the code of whatever
  blocked the check, and that code now decides the exit whether or not `--ci`
  is on. The distinction is the point: "no contracts.toml here" is an absence
  and stays exit 0, "the configuration did not load" is a failure this run
  could not look past. `--fail-on never` still wins over both, because an
  escape hatch with an exception is not one.

### Added earlier, written down here

These five shipped in `0.1.0a6` and never got an entry: the notes for them sat
under `[Unreleased]` while two releases were cut below them. Recorded now under
the release that noticed, rather than backdated into one that did not say it.

Five commands that move the CLI past the first ten minutes of a project. Each
answers a question the framework could already have answered and did not.

- **`jfast check`** — every check that exists, one screen, one exit code. They
  all existed already; what did not exist was a single thing to run, so CI ran
  three of them and the two nobody wired up never ran at all. Exit-code
  precedence is by **how much of the report a failure invalidates**, not by
  severity: a `jfast.toml` that will not parse makes everything after it a
  guess. `--json` carries the code of every check that failed, because one
  number is never the whole answer. Under `--ci` a **skip fails** — in CI a skip
  means the runner was missing something, and a battery that reports green on
  checks it did not run is worse than no battery.

- **`jfast migration check` / `plan`** — reads revisions before they run:
  `NOT NULL` on a populated table, a rename rendered as drop-plus-add, an index
  built while holding a write lock, a type change with no `USING`. Verified by
  watching `alembic upgrade head` fail against real PostgreSQL and predicting
  it. Row counts come from `EXISTS ... LIMIT 1` and `pg_class.reltuples`, never
  a `count(*)`, and with no database it reports "unknown, treat as populated"
  rather than assuming empty.

- **`jfast contracts explain`** — why a rule exists, where it is declared, and
  what to do instead. `contracts check` tells you a rule broke; an agent handed
  a violation with no remedy tends to satisfy the checker rather than fix the
  design, by deleting the import or turning the rule off. The answer cites the
  line in your `contracts.toml` and the comment its author wrote there, not
  invented prose. `contracts diff` compares the architecture the contract
  permits against the imports the code actually has — **not** a git diff, and
  the docs say so plainly.

- **`jfast ai context --json` and `jfast next`** — everything a model needs
  about a project in one call. The size is **project-dependent and there is no
  single number**: a generated service measures 8.3 KB at one module and 11.5 KB
  at five, and a five-module service with real findings and contract violations
  measures 14.8 KB (`--brief` 2.8 KB to 6.1 KB across the same range).
  **`jfast ai context --size` prints the figure for your project** — that is the
  one to plan against. For scale: shipping `docs/` instead would have been
  555,859 bytes. What it deliberately leaves out is listed in an `omitted`
  field with the command that returns it, `jfast migration check` included.
  `next` orders steps by **dependency, not
  severity** — a module that is not registered comes before its missing tests,
  because testing an unwired module proves nothing — and on a clean project it
  says what it checked rather than inventing work.

- **`jfast upgrade --check`** — what breaks moving to a newer framework
  version, **filtered to what applies to this project**: it reads your models,
  your `contracts.toml` and your plugin settings and reports only the changes
  that can affect you. A warning that does not apply is how people learn to skip
  the output. The manifest is data in the package rather than a parse of the
  changelog, which is prose, does not ship in the wheel, and breaks silently on
  a reworded heading. `--apply` is refused rather than stubbed: rewriting
  someone's project needs a rollback story this does not have.


## [0.1.0a7] - 2026-09-03

The quickstart, made to survive being followed literally.

Four commands end in a panel telling somebody what to run next. On `0.1.0a6`
three of those lines did not work on a project the framework had just written:
`docker compose up --build` had no Dockerfile to build, the api container it
did start read the host's DSN and crash-looped, and `jfast start` shipped a
module it never mounted, so the endpoints its own generated tests covered were
absent at runtime.

Every piece worked in isolation, which is why 1302 tests were green while the
first ten minutes were not. The release that fixes them also adds the two
checks that would have caught them: `tests/test_quickstart.py`, which asserts
the sequence rather than the pieces, and `scripts/smoke_compose.sh`, which runs
the generated compose files and asks the running containers what they serve.

### Added

- **`InfraService.client_env`** — how a service on the same compose network
  reaches a container, declared by the plugin that owns it. The container was
  always derived from the plugin graph and the connection string never was;
  that gap is where the crash loop lived. Declared by `database` (per named
  connection), `cache`, `mongo`, `qdrant`, `queue`/RabbitMQ and `events`/Kafka.
  A plugin whose address is per-disk configuration rather than one variable --
  `storage` -- declares nothing, and a test fixes that as the correct answer.

- **A Dockerfile with every generated Python service.** The `.dockerignore` has
  shipped with the scaffold since `0.1.0a6` for the same reason: the file is
  needed before the command that used to write it gets run.

- **`requirements-dev.txt` in generated services.** `requirements.txt` is the
  deploy list, and a test runner does not belong in a production image.

- **`scripts/smoke_compose.sh` and the `compose` CI job.** `smoke_docker.sh`
  builds the image but wires the network, the database and the environment by
  hand -- which is the work the generated compose file exists to do, so nothing
  ever ran that file. Both generators are covered, because they fail
  differently.

### Fixed

- **`jfast start` left its own module unmounted.** It rendered `modules/item/`
  and stopped; `jfast new module` had spliced the router into `main.py` since
  that path existed. Nothing failed: the generated tests passed, the server
  booted and `/items` 404ed. `jfast check` reported it HIGH and exited 1 on a
  tree this framework had just written. Both paths now call the same two
  functions.

- **No generator wrote a Dockerfile.** Both compose generators give the
  application service `build:`, so `docker compose up --build` -- the first line
  of the panel -- stopped at `failed to read dockerfile` before any container
  started. `jfast deploy dockerfile` had the file all along and nothing said to
  run it. `jfast deploy compose` now says so when an older project has none.

- **The api container was never told where its database was.** It loaded a
  `.env` written for a process on the host -- `localhost` and the published
  port, which inside a container is that container. `environment` beats
  `env_file` in compose, so the internal address goes there and the `.env` stays
  correct for its own reader. The workspace generator had derived this from the
  resource graph since it existed; the single-service one had nothing.

- **`pytest` was printed by a scaffold that installed none.** The step
  `jfast new module` prints answered `No module named pytest`.

- **`jfast start` told you to overwrite the `.env` it had just written.**
  `cp .env.example .env`, "the defaults already match compose": both halves were
  false. The generated file comes from the resource graph; the example is static
  and points at `localhost:8001` with a password nobody set. The local path now
  points at `jfast dev`, which resolves the host addresses and the workspace
  secret -- the thing no static file can hold for both readers.


## [0.1.0a6] - 2026-09-03

Hardening, in the three places where "it works here" and "it works" are
different claims: another operating system, inside a container, and behind a
proxy. Nothing in this release is a feature; all of it is a case that was not
covered.

### Added

- **Windows in CI, with `mypy --platform win32`.** A path separator, a case-folded
  glob and a console encoding are all things that pass on Linux and fail on a
  laptop, and the only way to know is to run there. The type check is run for
  win32 as well as the host, because a branch that only exists on one platform
  is a branch the other platform's checker never reads.

- **`tzdata` as a core dependency.** Timezone-aware timestamps arrived in
  `0.1.0a5`; a zone database is what makes them resolve. On a slim container
  image there is no system copy, so the code that was made correct in the last
  release would have raised at runtime on the machines it matters on.

- **A generated `.dockerignore`, and it survives regeneration.** Without one the
  build context carries `.env`, `.git` and every virtualenv into the image —
  which is both slow and a way to ship a secret.

- **`scripts/smoke_docker.sh`** — builds the generated Dockerfile and checks the
  result for leaked secrets and image hygiene. A Dockerfile that is only ever
  read is a Dockerfile whose problems are found in production.

- **`CONTRIBUTING.md`, `LICENSE`, and packaging metadata.**

### Fixed

- **JWKS refresh stampede.** Every request that arrived during a key refresh
  started its own. Under load the first slow fetch became many concurrent slow
  fetches, which is the failure mode that turns a key rotation into an outage.

- **`jfast check` and `jfast doctor` now validate buildability.** Both reported
  on a project they had not established could be built, which is the class of
  green result this project has spent two releases removing.

- **The body-size middleware after the response has started.** Refusing a body
  is only possible while the response has not begun; doing it afterwards
  corrupts the stream instead of rejecting the request.

- **Gateway forwarding of headers, query strings, cookies and encodings.**
  Several edge cases where what reached the upstream was not what arrived, each
  with a regression test.

- **Docker builds are multi-stage and run as a non-root user.**

Twenty-six files, 1,068 insertions, and eleven of the twenty-six are tests.


## [0.1.0a5] - 2026-08-30

Twenty findings from a second external report, this one raised against
`0.1.0a4`. Nine were verified by hand before anything was touched, two turned
out worse than reported, and two were rejected -- one of them rejected as a
defect and answered as a naming problem instead.

The theme is narrower than last release's. `0.1.0a4` built seven diagnostic
commands in a day and **three of them lied**: `migration check` approved three
dangerous statements, `contracts check` passed on a contract that governed no
file at all, and `analyze` reported no findings on that same project while
`jfast next` said the check had failed. A diagnostic nobody believes gets
ignored; a diagnostic people believe and that is wrong costs more than the
defect it was built to catch, because it is the reason they stopped reading the
migration by hand.

Two rules came out of that, and both ship as tests rather than as notes:

1. No diagnostic is trusted without a case where it must fail and does.
2. A test asserts the half that can break, **by the route a user takes** -- not
   the advertised string when what fails is the port mapping, not
   `contracts init` when people run `jfast new service`, not the middleware in
   isolation when it runs behind uvicorn.

1,287 tests pass and 7 skip: five want a real Redis, two are one command that
is deliberately reachable two ways.

### Breaking

- **A layer glob's `*` stops at `/`.** Layer paths went through `fnmatch`,
  which translates `*` to `.*` and crosses directory separators, so
  `modules/*/repository.py` also claimed
  `modules/billing/infrastructure/repository.py` -- a layer could appear to
  govern a tree nobody wrote it for, and `layer-unmatched`, the finding that
  exists to catch a contract governing nothing, never fired. Matching is
  case-sensitive on every platform now as well: `fnmatch` folds case on
  Windows, so one contract passed on a laptop and failed in CI.

  A file that matched a layer yesterday and matches none today is governed by
  nothing, `forbid_packages` included. Where the reach was intended, widen the
  pattern:

  ```diff
  -paths = ["modules/*/repository.py"]
  +paths = ["modules/**/repository.py"]
  ```

  `**` crosses directories on purpose and says so, and `**/` also matches
  *zero* segments, so `modules/**/http.py` still covers `modules/http.py`.
  `jfast upgrade --check` lists the files that changed hands under
  `layer-globs-narrowed`, computed against your tree rather than read off the
  patterns: a contract written in `**` throughout is unaffected and gets no
  report.

- **`datetime.now()` with no zone is a contract violation.** The new
  `naive-datetime` rule rides `[rules.async_safety]`, which every existing
  contract already enables, so it arrives with no opt-in and a build that
  passed yesterday fails today, naming a rule the project has never seen.
  `datetime.utcnow()` and `datetime.utcfromtimestamp()` are the other two --
  naive despite the name, and deprecated since 3.12.

  ```diff
  -created = datetime.now()
  +from jfastframework.time import now
  +created = now()
  ```

  `datetime.now(UTC)` is equally fine: the rule is about the missing argument,
  not about which module you reach for. A `*args` or `**kwargs` splat is never
  reported, because the syntax cannot say whether the zone is in there. To
  defer the whole rule, one line in `contracts.toml`:

  ```toml
  [rules.async_safety]
  naive_datetime = false
  ```

  That leaves the async-blocking half on, which is the half you already had.

- **Every database session is pinned to UTC.** `[plugin.database]
  session_timezone` defaults to `"UTC"` and travels as an asyncpg startup
  parameter, so `date_trunc('day', ...)`, `CURRENT_DATE`, `now()::date` and any
  `AT TIME ZONE` without an explicit zone stop reading the server's `TimeZone`.
  On a server configured to anything else those queries return different rows
  than they did yesterday -- **which is the point**, but it is a change of
  answer rather than of code, and a daily report is where it shows. To keep the
  old behaviour, say so:

  ```toml
  [plugin.database]
  session_timezone = ""   # leave the server's TimeZone alone
  ```

  Sent in the startup packet and not as a `SET` after connect, deliberately: a
  pooled connection is checked out mid-life, so one `DISCARD ALL`, one
  `RESET ALL` or one pgbouncer server-reset undoes a statement that ran once
  and the session falls back to the server's zone with nothing in the logs.

- **`jfast new service` no longer writes `contracts.toml`.** It had no way to
  know the layout -- a service is generated before any module exists -- so it
  wrote the layered contract into hexagonal, modular and screaming services,
  where it matched no file and enforced nothing while `contracts check` still
  exited 0. The first `jfast new module --layout X` writes the contract for X
  now, and never replaces one already on disk. Pass
  `jfast new service --layout X` when the layout is already decided.

  For projects generated before this, the consequence is a new failure:
  `contracts check` reports `layer-unmatched` (exit 5) for a layer that matches
  no file while files it should have claimed go unclaimed. Point the layer at
  the folders your modules really use --

  ```diff
  [layers.storage]
  -paths = ["modules/*/repository.py"]
  +paths = ["modules/*/infrastructure/*.py"]
  ```

  -- and `jfast inspect` names each module's layout.
  `jfast contracts init --layout X --force` also fixes it and overwrites the
  whole file, every layer, rule and waiver the project added included, so it is
  the last resort here rather than the first.

- **`TokenStore.rotate_refresh` returns an outcome, not a bool**, and takes a
  `grace` keyword. A bool could not tell a client retrying apart from a stolen
  token being replayed -- both are "this one was already used" -- and only the
  store can answer the two together. Every truthiness test on the old return
  value also passes for `"replayed"`, which is the one outcome that has to end
  the family:

  ```diff
  -async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
  +async def rotate_refresh(
  +    self, token_id: str, *, family: str, ttl: int, grace: int = 0
  +) -> RefreshOutcome:
  ```

  `RefreshOutcome` is `Literal["rotated", "raced", "replayed"]` in
  `jfastframework.auth.store`, where `MemoryTokenStore` and `RedisTokenStore`
  are worked examples. A store that cannot honour a grace window returns
  `"replayed"` wherever it used to return `False`. Only a project that wrote
  its own store is affected; the shipped ones are read correctly by the
  framework.

- **An `on_refresh` hook returning `None` revokes the session family.** It used
  to refuse that one request and leave the access token already in the client's
  hands good for its full lifetime, so a banned user went on working for up to
  fifteen more minutes. The hook also runs *before* the refresh token is
  consumed now: a hook that raises leaves the token usable, so the client's
  natural retry is a retry rather than a replay that costs the family for the
  whole refresh lifetime.

- **`Page.total` is `int | None`.** This shipped in `0.1.0a4` and was recorded
  there as a fix, which was the wrong file: the modes that skip the `COUNT`
  never knew a total, and reporting one anyway was a number nobody could act
  on. It reaches clients and not only code that type-checks -- the JSON of
  every paginated endpoint this framework generates can carry `"total": null`,
  and a response model declaring `total: int` fails validation on the page that
  produces it.

  ```diff
  class PageResponse(BaseModel):
  -    total: int
  +    total: int | None
  ```

  `has_more` answers "is there a next page" without a total.
  `jfast upgrade --check` lists the call sites under
  `pagination-total-optional`.

- **A service with `storage` enabled gets 25 MiB and 120 s from the kernel.**
  The raised pair was written into `jfast.toml` by the scaffold and lived
  nowhere else, so a project that enabled `storage` a year after
  `jfast new service` ran on 2 MiB and 30 s while `upgrade --check` promised it
  25 MiB. The rule lives in `JFastSettings` now, which makes the manifest true
  by construction. An explicit value under `[app]` -- including an explicit `0`
  -- still wins, so write one to keep a smaller limit.

- **Generated compose files pin no `container_name`.** It is global to the
  daemon, so a second copy of the same workspace could not start beside the
  first. Compose derives the running container's name from the project instead,
  which breaks any script that named one directly:

  ```diff
  -docker exec -it shop_postgres psql -U shop
  +docker compose exec postgres psql -U shop
  ```

### Added

- **A time-zone subsystem, because "store UTC" was half the problem.**
  `0.1.0a4` made what is *stored* aware. Computing with those values stayed a
  property of wherever the container ran: `date_trunc('day', created_at)`
  answers differently on two replicas whose servers carry different `TimeZone`
  settings, from byte-identical rows, and nothing fails.

  `jfastframework.time` answers the half that is a business question. `now()`
  is the only clock the framework reads and is always aware UTC; `today(tz)`
  and `day_bounds(day, tz)` decide which *local* day a UTC instant belongs to.
  `day_bounds` returns a half-open `[start, end)` in UTC -- never `BETWEEN`,
  whose closed upper bound either double-counts midnight or drops the last
  microsecond depending on the column's precision -- and resolves both edges
  with `fold=0`, which is what makes it right on the local day that has no
  midnight (America/Santiago starts DST at 00:00) and on the one that has two.
  `in_zone()` renders and is presentation only; `parse()` refuses a string with
  no offset unless the caller says which zone wrote it.

  Three zones, named apart because they are three questions: storage is UTC and
  is not configurable; `[app] timezone` is the **business** zone, what "today"
  means for a report, an invoice period or a daily quota;
  `[plugin.tenancy.timezones]` overrides it per tenant, which is the case one
  deployment serving several countries exists to handle, with a `tenant_zone`
  dependency that falls back to the business zone and never to the server's.
  Every name is validated at boot, so a typo stops the service instead of
  shifting one tenant's reports by a day a week later.

  `"UTC"` resolves to `datetime.UTC` and reads no file: `zoneinfo` reads
  `/usr/share/zoneinfo` for that key like any other, and a slim container with
  no tzdata raises for every name. Naming a real zone on such an image fails at
  boot with a message that says which package is missing.

  `jfast doctor` reports `db_timezone` as a pair -- what an unpinned client
  computes in, and what this service computes in -- read over two connections
  because a startup parameter *becomes* the session's reset value, so
  `pg_settings.reset_val` reports `UTC` on a server set to anything. A server
  that is not UTC is not this service's failure and is still worth one line:
  every other client of that database, psql and a BI tool and a migration run
  by hand, is reporting a different day. See
  [docs/timezones.md](docs/timezones.md).

- **`jfast check` says what it does not check.** With an unused import, a
  misformatted file, a `str` assigned to an `int` and a failing test all
  present at once, its output was byte-identical to the clean project's and it
  exited 0, `--ci` included. Nothing in the report was false. What was false
  was the impression the name left, and a team acting on that impression
  deletes its own verification script and loses four gates in one commit.

  It still runs none of them, on purpose: `pytest` executes your code for an
  unbounded time and `mypy` in a tree whose dependencies are not installed
  manufactures findings, and either one inside this command turns a pre-commit
  hook into a build. So the answer is naming rather than adding. Every run
  ends with

  ```
  not checked here: lint, formatting, types, tests
  ruff check .  ruff format --check .  mypy .  pytest
  ```

  pass or fail, and `--json` carries the same four under `not_covered`, each
  with what it catches and the command that catches it.

- **`contracts check` prints how many files each layer governs**, in the order
  the contract declares them, on every run:

  ```
  layers
    domain             4 files
    application        2 files
    infrastructure     3 files
    adapters           2 files
    shared             2 files
  ```

  A layer at 0 is the finding, and it is the one this checker reported late:
  `layer-unmatched` fires only once files that layer should have claimed are
  *also* unclaimed, and a layer that drops from 40 files to 3 in a refactor
  raises nothing at all. Counted through `layer_for` rather than by raw
  globbing, so a layer whose every match is taken by a more specific pattern
  shows as the zero it is.

  One fact, one owner, across four commands: `analyze` reports
  `contract-governs-nothing` by calling `check_coverage` rather than
  reimplementing it -- it used to print "no findings" on a project whose
  contract governed nothing while `jfast next` said the check failed --
  `contracts diff` lists what an empty layer permits under `~` with the reason
  instead of selling an outage as ten opportunities to tighten the contract,
  and `next` collapses the pair into one step rather than five lines about one
  thing to do.

- **`ratelimit`, `channels` and `websocket` are in the plugin menu.** All three
  shipped as entry points and appeared in no catalogue, so the only way to find
  them was to read `pyproject.toml`. `tenancy` was the mirror image: in the
  catalogue and absent from every generated `jfast.toml`, because the filter
  that builds that menu required an extra and `tenancy` needs none. Plugins
  with no extra now print `no extra needed` on their row. Two tests tie the two
  hand-kept lists together, in both directions -- a catalogue entry with no
  entry point offers an install that cannot work.

- **`[plugin.auth] refresh_grace_seconds`**, 10 by default. Two tabs of one
  browser refresh at the same instant and one of them loses; with no window the
  loser's attempt reads as a theft and revokes the session both tabs were
  sharing. The losing request is still refused -- there is one live refresh
  token and the winner has it -- but the family survives. `0` restores strict
  reuse detection, and longer than a request round trip only buys a stolen
  token more time.

- **A sign-in screen in both generated frontends.** `LoginView`, a route guard
  that is public by default with the one line to change marked and explained,
  and a 401 handler in `services/api.js` that refreshes **once** for a whole
  burst: four panels loading together produce four 401s, and four refreshes
  present the same rotated token four times, which a backend that treats replay
  as theft answers by revoking the session.

- **`jfast new service --layout`**, for a service whose layout is already
  decided, and `followSystem()` in both frontends' theme composable.

### Fixed -- silent

- **uvicorn was resolving the client address before the framework saw it.**
  There is no `proxy_headers` argument anywhere in `0.1.0a4`'s source, and
  uvicorn ships its own `X-Forwarded-For` handling **on**, trusting loopback,
  running before any application middleware. `jfast serve` binds `127.0.0.1`,
  which is exactly the peer uvicorn believes -- so `trusted_proxies` validated
  locally and was right for the wrong reason, while in a pod every sidecar
  could pick its own client address. A forged `X-Forwarded-Proto` flipped
  `scope["scheme"]` too, which is the gate HSTS is decided on.

  `jfast serve`, `jfast dev` and the generated Dockerfile entrypoint all pass
  `--no-proxy-headers` now: one resolver in the process, and it is the one that
  reads `jfast.toml`. A peer that arrives already substituted is caught rather
  than believed -- a proxy appends the address it received the connection
  *from*, never its own, so a genuine transport peer does not appear in the
  chain it is relaying -- and such a request is treated as having no client at
  all, one bucket an attacker cannot rotate out of, with the scheme downgraded
  when the header could have written it. The tests for both halves run against
  a real uvicorn on a real socket, started the way it ships.

- **Keyset pagination stopped at the first NULL and reported that it had
  finished.** Worse than the report said: not "the page ends early" but an
  empty page with `has_more=False`, and every row from that cursor onward
  unreachable from any cursor -- 180 of 200 on SQLite, where NULLs sort first,
  40 of 200 on PostgreSQL, where they sort last. `last_message_at` and
  `edited_at` are exactly the columns a feed orders by.

  Every ordering the repository builds now spells `NULLS LAST` on the nullable
  columns, because the default is not one thing -- PostgreSQL puts NULLs last
  ascending and first descending, SQLite puts them first either way -- and the
  keyset comparison is written against that: `IS NULL` for a tie, and a step
  past a non-NULL value admits the NULL block that follows it. The cost is
  stated rather than hidden: a nullable ordering column demotes the index range
  scan to an index scan with a filter, measured at 840 buffers against 4 with
  NOT NULL columns, one page at depth 100k of 200k rows, against 1,049 for the
  same page by `OFFSET`. Still the cheapest of the three ways to page, no
  longer flat.

- **Three parsers could not read this framework's own template.**
  `cli/migrations.py` and `upgrades.py` filtered `ast.Assign`, and the
  generated `script.py.mako` emits `revision: str = ...` and
  `down_revision: str | None = ...`, which are `ast.AnnAssign`. The same blind
  spot hid `__tablename__: str = "users"` -- SQLAlchemy 2.0 style, which is
  what the rest of a generated model is written in -- from `project.py`, so
  `module-no-migration` stayed quiet about a table no revision creates. The
  test that covers it parses a revision **generated by the template** rather
  than one written by hand.

- **`jfast upgrade --check` handed over `ALTER TABLE`s for tables with no
  timestamps.** It reported one per `__tablename__` in any file that
  *imported* `TimestampMixin`, and a models file routinely holds both the
  tables that mix it in and the projection tables that do not. An `ALTER`
  naming `created_at` on a table without one aborts the revision -- after every
  statement before it has already taken ACCESS EXCLUSIVE and rewritten its own
  table. Resolved per class now, through the base closure, so a model reaching
  the mixin through a base in `shared/` is found and an `__abstract__` base is
  skipped.

- **The `jfast` console script could not load a project's own plugins.**
  `[plugins.paths]` names modules that live in the project rather than in a
  wheel, and `python -m jfastframework` puts the project directory on
  `sys.path` while the console script does not -- so one `jfast.toml` passed
  under one spelling and reported its own plugin missing under the other. One
  insertion, in `registry.discover`, and a dotted path that does not import is
  recorded as broken rather than raising out of discovery. `check`, `analyze`
  and `doctor` now see one set of plugins between them.

- **Kafka advertised a port compose did not publish.** `host_port` moved the
  advertised address only; the mapping still came from
  `base_port + port_offset`. A client bootstrapped, reconnected to the
  advertised address and hung. `InfraService.host_port` feeds both now, from
  one field, and the test asserts the `ports:` mapping as well -- the old one
  checked the advertised string alone, so it passed for as long as the defect
  existed.

- **`jfast deploy compose` threw a service's uploads away on the next build.**
  The volume that keeps local storage disks was written by the workspace
  generator only, so the same service deployed through the other command kept
  the rows and lost the files they point at. Both generators share one function
  now, as they already did for the shape of a container.

### Fixed -- correctness

- `jfast migration check` reads `op.execute`. The statement forms whose risk
  the leading keywords settle are analysed, and one matching none of them is
  reported as unread rather than passed over in silence.
- A drop-plus-add with a backfill between them is no longer reported as data
  loss. Copying the values is what turns the pair into a rename, and flagging
  it anyway is the false positive that leaves `--fail-on never` as the only way
  to put a correct migration through CI.
- The rename banner a generated revision carries says that it speaks for the
  moment the file was written and that `jfast migration check` re-reads the
  file: when the command is quiet and the banner is still there, the banner is
  the stale one. Nothing updates a comment, and a STOP nobody deletes is a STOP
  nobody reads.
- Every backticked remedy `migration check` prints is asserted to be Python
  somebody can paste. The SQL and shell fragments in the same sentences are
  not, and are not marked as though they were.
- `useTheme().resolved` is a module-level `computed` in both frontends. It was
  a `ref` built per caller, so only the component that handled the click ever
  refreshed, under a comment claiming the property it did not deliver. There is
  a `followSystem()` now as well: `toggleTheme` can only pin light or dark, so
  without it the first click permanently dropped one of the three states.
- The generated `alembic.ini` uses `version_path_separator = newline`. `os` is
  deprecated and warns on every alembic command.
- `contracts explain` documents `naive-datetime` and points at
  `[rules.async_safety]`, so a violation of the new rule arrives with a remedy
  rather than with a rule name and a line number.
- The generated `jfast.toml` documents `timezone`, `[plugin.database]
  session_timezone` and `[plugin.tenancy.timezones]` where each is set, and
  `AGENTS.md` no longer claims a contract every service is born with.

## [0.1.0a4] - 2026-08-30

Twenty defects found by building a real application against `0.1.0a3`, plus a
security audit and a review of the CLI. Almost every one of them **failed in
silence**: a consumer that subscribed to nothing, a refreshed token that
authenticated and was authorized for nothing, a compose file that regenerated
identically while ignoring a plugin, a contract that forbade the pattern its own
documentation prescribes.

### Breaking

- **Timestamps are timezone-aware.** `TimestampMixin` mapped to `TIMESTAMP
  WITHOUT TIME ZONE`, so `created_at` serialised as `2026-08-29T20:55:15` with
  no offset and every JavaScript client read it as local time -- a post written
  now rendered as "in 6 hours" east of UTC. Existing tables need a migration,
  and **the `USING` clause is load-bearing**:

  ```sql
  ALTER TABLE invoices
      ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
      ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
  ```

  Alembic's autogenerate writes the bare form with no `USING`. That does not
  fail -- it converts through the implicit cast, reading every stored value in
  the *server's* `TimeZone`, and silently shifts the whole table on any server
  not set to UTC.

- **Refresh tokens minted by `0.1.0a3` and earlier are refused** with a 401.
  Every active session re-authenticates once. Those sessions were already
  broken: rotating one returned a token with no scopes.

- **`/auth/logout` ends the calling session only.** It previously revoked every
  session of that person. There is no "sign out everywhere" replacement in this
  release -- a subject-level cursor needs a `TokenStore` change and ships
  separately.

- **Generated contracts let every layer import `shared/`.** `contracts.toml`
  belongs to the project once generated, so add `"shared"` to each layer's
  `may_import` by hand. `contracts init --force` writes the corrected defaults
  and overwrites the whole file, discarding the project-specific lines that are
  the part worth having.

- **`max_body_bytes` and `request_timeout` now have values** (2 MiB, 30 s; 25
  MiB and 120 s on a service with storage). Both were `None`. `None` and `0`
  still mean unlimited.

- **`contracts check` exits `5`, `doctor` exits `2` or `3`.** Exit codes are
  standard and documented.

- **Access tokens carry a `fam` claim.** Consequence: revoking a session now
  invalidates its outstanding access tokens immediately.

### Fixed — silent

- **The events consumer subscribed to nothing.** `startup()` returned bare when
  no handler was registered: no log, no warning. The consumer started, joined
  the group and received nothing, ever -- because the registration window was
  one line wide, between the plugin providing the bus and startup reading it.
  Handlers are now declared at import time and drained at registration, the
  same shape `Channel` already used, and both branches log.

- **A refreshed token had no scopes and no roles.** Deeper than it looked: the
  refresh token never carried them, so forwarding what `rotate` received would
  have forwarded nothing. It now carries the grant under `grt`, a claim of this
  issuer's own -- never the configured scope claim, so `verify()` cannot read it
  back as authorization.

- **A logout bricked the next login for up to 30 days.** The refresh family was
  the subject, and `revoke_family` denied it for the full refresh lifetime -- so
  the *next* login was born into a revoked family. 15-minute access tokens and
  no working refresh until the entry expired. Families are random per session.

- **A refresh token was accepted as a bearer token.** Same key, same issuer, and
  neither `require_auth` nor the middleware checked `typ`, so a 30-day token
  opened every `require_auth`-only route. Contained until now only because it
  carried no scopes -- which is why the grant went under its own claim.

- **`jfast workspace compose` ignored any plugin declaring infrastructure.**
  Byte-identical output with the plugin on or off. Two generators read two
  sources of truth, and the one real projects use could not express a plugin at
  all. There is one generator now; a plugin it cannot represent is a warning,
  never silence.

- **Following the documentation violated the generated contract.** Every service
  ships `shared/enums.py` and every layer's `may_import` omitted `shared`, so
  moving code where the docs, the placement rule and the checker's own message
  all tell you to move it failed the check.

- **`jfast start` wrote a database password into a file it called gitignored.**
  The rule lived in the `workspace init` command body, not in `Workspace.save()`.

- **The first migration of every generated service could not run.** Introduced
  by the timestamp change in this same release and caught before it shipped:
  Alembic renders a custom type by its dotted path and emits no import, and the
  revision *parses* because the name is only evaluated inside `upgrade()`. It
  died at `alembic upgrade head` with a `NameError` on a line that looked fine.

### Fixed — correctness

- `NOT NULL` with a scalar model default now emits a `server_default` and drops
  it again, so an add on a populated table applies and the next autogenerate is
  empty.
- The rename warning is conditional. It was in *every* revision's docstring, so
  it was wallpaper -- the person who reported the data loss had it in front of
  them.
- `paginate` no longer forces an exact `COUNT`; `paginate_keyset` avoids the
  offset scan (44,925 buffers at offset 10,000 against 588 on the first page).
- `public_base_url` works on local disks. It was accepted, passed to S3 only,
  and silently dropped for local -- and it deliberately does not apply to
  `temporary_url`, because a CDN in front of a signed URL serves the object
  after the signature expires.
- `jfast new enum` puts the file in the layer the layout uses. On a hexagonal
  module it landed outside the layout, where no contract glob reached it.
- Kafka advertises two listeners, so a host process can reach the broker.
- Compose emits `shm_size` on PostgreSQL, a volume for storage disks, and a
  worker count derived from the container's cgroup quota rather than the host's
  CPU count.

### Added

- **`jfast inspect`, `jfast analyze`, `jfast graph`.** The CLI could not tell
  you what was in your own project: `describe` builds the app to answer, which
  fails exactly when you need it, and it says nothing about modules. These read
  the filesystem and never import project code.
- **`ratelimit`** — token bucket in a Lua script, so the read/decide/write is
  indivisible. Fails open, loudly, and reports `/ready` degraded. The gateway
  README had promised this plugin for a year.
- **`websocket`** — connection registry, authenticated handshake over
  `Sec-WebSocket-Protocol` (never the query string), bounded send buffers,
  heartbeats, and cross-worker delivery over the existing Redis backplane.
- **Named database connections**, a read/write split with primary pinning after
  a write, and per-tenant engines behind a bounded LRU that never disposes an
  engine a request is holding.
- **An upload pipeline** on each disk, with a `validate` step that sniffs the
  content type from the bytes, and an optional `optimise-image` step behind
  `jfastframework[images]`.
- **Security headers** — CSP derived from whether the schema is exposed, HSTS
  gated on production *and* HTTPS, `frame-ancestors`, and trusted-proxy
  resolution so `X-Forwarded-For` is not a bypass.
- **`get_or_set`** with stampede protection and hit/miss counters. The cache had
  no tests at all.
- **Standard exit codes** in `jfastframework.cli.exits`.
- **A theme switch** in both generated frontends: three states, no flash on
  reload, and a mobile drawer where a phone previously had no navigation.
- **CI starts PostgreSQL and Redis**, and fails if the tests that need them
  skip. A skip is silent, which is how a concurrency guarantee gets shipped
  without ever being observed.
- **A test asserting the agent-facing documentation is true.** For each of the
  four layouts it extracts every path `AGENTS.md` and the skills name and
  asserts it exists. `AGENTS.md` had been describing a `layered` tree to every
  project, so on a hexagonal module all four files it named were absent.

### Added

- **`jfast inspect`, `jfast analyze` and `jfast graph`.** The CLI could not tell
  you what was in your own project. `jfast describe` answers "what is this
  service" by building the app -- unavailable when a dependency is missing or
  the code does not import -- and it says nothing about modules at all:
  generate two and neither name appears in its output. These three read the
  filesystem instead and never import project code, so they work on a project
  that is currently broken.

  `inspect` is one screen: modules, how each is shaped, what it serves, whether
  it is wired into the app. `inspect module <name>` goes deeper. `analyze`
  reports eight structural problems worst-first, each with the fix on its own
  line -- import cycles, a module `main.py` never registers, two routers
  claiming one prefix, `shared/` importing a module, an enabled plugin nothing
  provides. `graph` draws the module dependency graph as text, mermaid, dot or
  JSON.

  Every check is decidable from the source. Nothing guesses: a checker that is
  right nine times in ten gets muted after the second false positive, and the
  true findings go with it. `module-no-migration` fires only once there *are*
  revisions, so a freshly generated project reports nothing.

- **Standard exit codes**, in `jfastframework.cli.exits`, documented in
  [docs/inspect.md](docs/inspect.md) and covered by tests. `1` validation, `2`
  configuration, `3` environment, `4` migration, `5` contract, `6` usage, `7`
  compatibility. CI can now say *why* it failed without parsing English.

- **A theme switch in both generated frontends.** There was none: `dark:`
  classes were everywhere and nothing ever set the theme, so the app followed
  the operating system and a person who wanted the other one had no way to say
  so. `src/style.css` redefines the `dark` variant to read an explicit
  `data-theme` first and fall back to `prefers-color-scheme`; `ThemeToggle`
  sits in the header; the choice is stored per service and applied by twelve
  inline lines in `index.html` **before the first paint**, so there is no flash
  on reload.

- **A mobile drawer.** The sidebar was `hidden md:block` and the mobile header
  held nothing but the service name, which meant that on a phone the generated
  app had no navigation at all.

### Changed

- **The frontend templates paint themselves in semantic tokens.** The layout
  used `slate-*` and the components used `zinc-*` -- two different neutral
  ramps, one blue-tinted and one not -- so a card never quite matched the page
  it sat on, in either theme. That mismatch is most of what "the template looks
  bland" is: nothing is wrong with any single component, the greys just do not
  agree.

  `bg-surface`, `bg-panel`, `bg-elevated`, `border-line`, `text-ink`,
  `text-ink-soft` and `text-ink-faint` now come from `@theme inline`, so the
  utilities emit `var(--ui-*)` and one variable swap flips the interface. **Not
  one component carries a `dark:` class any more**, except the four status
  hues on `BaseBadge` and `ToastHost` where the colour is the meaning.
  `color-scheme` is set alongside, so scrollbars and date pickers follow too.

- The full brand ramp (`50` through `900`) ships, rather than six scattered
  steps. A missing step is not an error -- `bg-brand-200` compiled to nothing
  and the element silently lost its colour.

- `contracts check` exits `5` instead of `1`; `doctor` exits `2` on a config
  failure and `3` on an environment one. **Breaking** for anything asserting on
  the number, which is why it happens now rather than after 1.0.

### Fixed

- **The rule that a router may not import SQLAlchemy was documented but never
  enforced.** `docs/agents.md` lists it first in the table of rules that catch
  generated code, and no generated `contracts.toml` carried it: the outermost
  layer declared `may_import` -- which constrains other *layers*, not packages
  -- and no `forbid_packages` at all. The same hole was in all four layouts,
  `adapters` in hexagonal included. A scaffolded service with
  `from sqlalchemy import select` in its router passed `jfast contracts check`
  with exit code 0.

  `forbid_packages = ["sqlalchemy"]` now ships on `layers.http` (`layers.adapters`
  in hexagonal) in every contract template. The four layouts still generate and
  pass their own contract; the planted import now fails with
  `layer-package: 'http' must not import 'sqlalchemy'` and exit code 1.

  Existing projects are unaffected -- `contracts.toml` is yours once generated.
  Add the line to adopt the rule.

### Added

- [PLAN-CLI.md](PLAN-CLI.md) -- a proposal for the CLI as a lifecycle tool
  rather than a generator: `adopt`, `analyze`, `inspect`, `migration check`,
  `extract`, standard exit codes and `--json` everywhere. Audited against the
  current command surface before being written down, which is how the contract
  bug above was found. Nothing in it is accepted yet.

## [0.1.0a3] - 2026-08-29

### Documentation

- **A landing page separate from the wiki.** The front page rendered through the
  same chrome as every documentation page -- a sidebar of nineteen links, a
  version picker, a pager -- so arriving at the project meant arriving already
  inside the manual. `index.html` is now its own page and `docs.html` is the
  documentation home.
- **The README says what this is for.** It opened with a category ("a
  plugin-based FastAPI framework") and went straight into a fifteen-section
  feature tour, which is written for somebody who has already decided. It now
  leads with who it is for, **who it is not for**, the shape of what gets
  generated, and four concrete situations it was built for. The reference half
  is unchanged; the problem was never that it existed.
- **A Spanish edition.** Every page has a Spanish URL under `/es/`, and 24 of
  26 are really translated -- the rest render the English source under a notice
  saying so, rather than serving English silently or leaving dead links in a
  translated sidebar. A translation lives at `docs/es/<name>.md` and overrides
  the English one; adding a page is dropping a file there.
- **`jfast dev` and the agent surface are documented** (`docs/dev.md`,
  `docs/agents.md`), along with the four module layouts, the base components,
  the stores, and `python -m jfastframework`. Ten things shipped undocumented;
  a check over the documentation now reports none.
- **A light/dark switch**, replacing "whatever the operating system says". Three
  states rather than two: an explicit choice, or follow the system.
- **The sidebar distinguishes labels from links.** Group headings and entries
  were both muted grey in one column, so a heading read as a smaller link. The
  headings are now accent-coloured and monospaced with a rule after them, and
  every entry carries an icon.
- **The language a reader picks is remembered**, and acted on **only at the site
  root**. Redirecting deep links would mean a shared URL lands somebody
  somewhere they did not click, and a crawler bounced off every page it
  requests indexes nothing.

### Fixed

- **`jfast start` wrote a compose file that could not start.** Its own
  next-steps panel said to run `docker compose up --build`, and that command
  failed: no workspace `.env`, so compose refused to interpolate
  `${SHOP_DATABASE_PASSWORD}` rather than defaulting it, and no per-service
  `.env`, which the compose file lists as an `env_file` and treats as an error
  when missing. Both are now written alongside the compose file, so the two
  agree by construction instead of by instruction.
- **The HTMX form never submitted.** `--ui htmx` mounted its HTML router on the
  same prefix as the JSON one, both declaring the same verbs, so whichever
  registered first answered: browsing returned JSON and the form POSTed into
  the API handler. The HTML surface now lives under `/ui/<table>`. It also
  passed a plain dict to a service that reads `payload.name`, and extended a
  `base.html` that only ships with `--kind web`. Three separate faults on one
  path, none of which generation, import, mounting or `contracts check` could
  see -- only sending the form finds them, which `scripts/smoke_htmx.sh` now
  does on all four layouts.
- **A validation error whose input was bytes returned 500, not 422.** pydantic
  puts the offending value in the error's `input` field, and a form posted
  without a content type puts the raw body there -- which the JSON encoder
  cannot represent, so serialising the 422 raised inside the handler. The
  caller got a stack trace about `json.dumps` instead of the field name, and a
  status that invites a retry of a request that can never succeed.

### Added

- **Four module layouts, and a prompt that asks which.** `layered`, `modular`,
  `screaming` and `hexagonal`. A modular monolith should not force a catalogue
  and an orders module to the same shape; the point of the boundary is that
  each side can differ. `jfast new module` asks when `--layout` is omitted, and
  falls back to `layered` without asking when there is no terminal, so a
  script or a CI job does not hang on a prompt nobody can see.
- **`jfast.toml` remembers which layout each module used.** Asking again each
  time eventually gets a different answer, and guessing from the folders on
  disk breaks the moment somebody adds one. Written where the rest of the
  service's configuration lives, and ignored by the runtime.
- **A contract per layout.** `contracts_modular` and `contracts_hexagonal`
  match the folders their layouts actually create; without them
  `jfast contracts init --layout hexagonal` pointed at a template that did not
  exist. The hexagonal one is the interesting one: `domain/` may import
  nothing -- not the ORM, not FastAPI -- because a domain that imports
  SQLAlchemy has already stopped paying for the layout.
- **Modules register themselves in `main.py`.** The frontend has patched its
  own routes and menu since the beginning; the backend printed two lines and
  left them to be pasted, so a generated module was inert until somebody did.
  A module that is not mounted looks exactly like a module that does not work.
  Non-fatal by design: a hand-edited `main.py` that lost its markers gets the
  lines to paste rather than an unwound scaffold.
- **`jfast dev`.** Containers up and waited for, migrations applied, then the
  API and the frontend together. Every stage degrades and says so; the one hard
  stop is a failing migration, because a server on a stale schema fails later
  in a request that has nothing to do with the missing column. It also
  translates the generated `.env` for a host process -- container hostnames
  become `localhost:<published port>` and `${...}` is interpolated -- since
  neither is true outside the compose network. Ctrl-C and SIGTERM both take the
  children with them, which needed its own handler: the default SIGTERM
  disposition kills the interpreter outright, and the children, being in their
  own process groups, would have survived holding the ports.
- **`python -m jfastframework`.** For when the console script is not reachable:
  a Windows install where `Scripts/` is not on PATH, a virtualenv nobody
  activated. The only module path that worked before printed a `RuntimeWarning`
  on every invocation.
- **Base components and toasts, in both frontends.** `BaseButton`, `BaseInput`,
  `BaseModal`, `BaseBadge`, `SkeletonLoader`, `EmptyState` and a toast host,
  mirrored between Vue and React so the two are the same product. Loading is a
  skeleton shaped like what is coming rather than the word "Loading", and empty
  says what would be there and offers the action that creates the first one.
- **Pinia and Zustand stores, wired up.** Pinia was a dependency no generated
  file imported; React had no state library at all. Auth and notifications now
  ship as stores, so a toast raised inside a service and one raised in a
  component land in the same list.
- **The spinner has callers.** `ui.working()` was written, ASCII-safe, and
  invoked from nowhere -- the function existed, the feature did not.

### Changed

- **The brand colour is crimson**, replacing the placeholder blue, matching the
  documentation site and the terminal.

<!-- earlier in this cycle -->


### Fixed

- **`jfast init` and `jfast start` crashed on a Windows console.** Not a broken
  character -- a `UnicodeEncodeError` raised by `sys.stdout` partway through
  writing a project, so the command died with a traceback having already
  created half of it. cp1252 has no `U+2713`; cp850 has neither that nor
  `U+203A`; the banner's block characters are in neither. Every symbol now
  resolves through `cli/glyphs.py` against the encoding the console actually
  reports, and falls back to ASCII -- panels, tree guides and spinner included.
  The check is a real `str.encode` rather than a list of known-good codepages,
  because terminals lie about themselves and `PYTHONIOENCODING` overrides all
  of it.
- **Three generated stylesheets pointed at a file that was never generated.**
  `frontend_vue`, `frontend_react` and `service_web` all told the reader to
  consult `.jfast/skills/design-system/SKILL.md`, and no generated project
  contained it. A pointer to nothing is worse than no pointer: it costs a
  reader the trip, and it teaches an agent that this project's instructions are
  unreliable. The reference is now conditional on the skill being written, and
  a test walks every generated file to keep the two in step.

### Added

- **A tree instead of a wall.** A scaffold prints forty-odd paths, and in a
  flat column the one line worth reading -- a file left alone because it
  already existed -- looks exactly like the thirty-nine that were written.
  Every generating command funnels through one reporter, so the shape of the
  output is decided once rather than per command.
- **The agent surface, opt-in: `--agent-docs`, or a question in `jfast init`.**
  An `AGENTS.md` and a skill under `.jfast/skills/`, reusing the layout the
  framework repo already uses rather than inventing a second place to look for
  conventions. A frontend also gets the design skill, which is what makes the
  stylesheet reference above true. Off by default, because a project nobody
  points an agent at owes no agent files -- and every file shipped is a file
  that can drift.


## [0.1.0a2] - 2026-08-28

### Added

- **A `shared/` layer, and a check that says when to use it.** Two modules
  that import each other are one module with a folder between them: neither
  can be extracted into a service later, and a change to one breaks the other
  in a way no test covers. `jfast contracts check` now reports the
  cross-import **and names the file to move the code to**, so the fix does
  not need a design discussion. The direction is enforced both ways: modules
  import `shared/`, `shared/` imports no module -- without that second rule
  `shared/` becomes the place everything ends up, which is the failure mode
  of every `utils` package ever written.
- **`jfast new enum`**, which asks where it goes when you do not say.
  The placement is the decision; the file is not. Start one in the module
  that needs it and the check tells you the day a second module wants it, so
  nobody has to predict it. Generated modules and `shared/` both ship an
  `enums.py` using `str, Enum`, because a plain Enum serialises as
  `Status.DRAFT` down some paths and `"DRAFT"` down others.
- **Declared channels (`channels` plugin).** Replaces a file of string
  constants, which fails in three ways: nothing checks the payload, the
  transport is welded to the call site, and nobody can list the channels a
  system uses. A `Channel` validates its payload **where the message is
  built** rather than in a worker three services away, and carries its own
  backend -- memory by default and needing no infrastructure, redis for a
  channel something in another language also speaks, kafka when a consumer
  that was down has to catch up. Mixing them is the normal case.

- **`jfast serve`.** Runs a service locally, and refuses to start when there
  is no `jfast.toml` in the directory -- which is the case that used to boot
  silently with framework defaults, no database, and no complaint. Binds
  loopback rather than `0.0.0.0`, because a development server should not be
  on the network unless you say so.
- **`mail` plugin.** Templates, three backends, and **queued by default**: a
  slow or briefly refusing mail server should not become the latency or the
  error of the request that triggered it. `send_now()` is the synchronous
  escape hatch and reads like one. The default backend is `console`, so
  nobody emails a customer from a laptop by accident and no credentials are
  needed to develop. In production with the smtp backend it refuses to start
  without credentials rather than failing on the first send.
- **`jfast add` and a capability catalogue.** Excel and PDF assembly, HTML to
  PDF, large XML, dataframes, vision, validation, locale, retries. Nothing is
  installed by default: a service that serves JSON should not carry numpy, and
  the plugin graph stops describing the service the moment it does. In a
  workspace with several backends it asks which one, because adding a heavy
  dependency to the wrong service is invisible until the image is built.
- **`jfastframework.exports.pdf`**, for assembling many documents. `merge()`
  **reports what it could not include** -- the obvious implementation logs a
  warning and returns a bundle that looks complete, which for a fiscal or
  legal bundle is worse than an error. It also merges in batches, because
  `PdfWriter.append()` holds every page until `write()` and peak memory
  otherwise grows with the whole job.
- **`jfastframework.exports.excel`**, using openpyxl's write-only mode so a
  cursor can be streamed to a file without either being held whole.
- **The installer is rendered with `rich`** -- which arrives with Typer, so no
  new dependency. A banner, tabulated choices, a summary before anything is
  written, and the next steps with what each command does beside it.

### Fixed

Six defects that shipped in `0.1.0a1`. Together they meant a generated
service could not be installed, could not be built into an image, could not
answer a GET, and could not answer a PATCH. Each is now covered by a test,
and by `scripts/smoke_docker.sh`, which builds the generated image and runs
it against a real PostgreSQL -- the check whose absence let all six through.

- **Every route taking a database session answered 422.**
  `session_dependency(request: Any)`: FastAPI decides what a dependency
  parameter *is* from its annotation, and from `Any` it concluded the only
  thing left -- a required query parameter. Reproduced from the OpenAPI
  schema (`name='request' in='query' required=True`), not inferred. Now
  annotated `Request`.
- **Every update returned 500 once a timestamp was serialised.**
  `TimestampMixin.updated_at` carries `onupdate`, which SQLAlchemy expires at
  flush; the next read -- Pydantic building the response -- attempted IO in a
  coroutine and raised `MissingGreenlet`. The mixin now asks for
  `eager_defaults`, so PostgreSQL returns the value with `RETURNING` in the
  same statement. A `session.refresh()` would have worked too, at the cost of
  a SELECT on every write, including the writes that never read a timestamp.
- **The generated Dockerfile could not build.** `COPY pyproject.toml ./`
  named a file the generator never writes, and COPY fails when its source is
  absent. Globbed, like the `requirements.txt*` line directly below it always
  was.
- **A container against an empty database answered 500 to everything.**
  Nothing ran migrations. The image now has an entrypoint that runs
  `alembic upgrade head` and then `exec`s uvicorn: `set -e` stops the
  container on a failed migration instead of serving a half-migrated schema,
  and `exec` keeps uvicorn as PID 1 so it receives SIGTERM. `create_all` was
  rejected as the fix -- it builds a schema Alembic does not know about, and
  the first real migration then diverges in silence.
- **The workspace search walked to the root of the filesystem.** Running
  `jfast start` once in a home directory left a workspace file there, and
  every project underneath then joined it: one compose file, one port space,
  unrelated services registering against each other, and nothing failing. The
  search now stops at the home directory and at a `.git`, because a
  repository root is where a project ends.
- **A service started from the wrong directory booted misconfigured in
  silence.** `session_dependency` reached into `request.app.state.jfast`
  directly and raised `KeyError: 'jfast'` on any app this framework did not
  build. It now goes through `get_context()`, which says so.

### Added

- **`scripts/smoke_docker.sh`**, gated in CI. It builds the image the
  generator writes, runs it against a real PostgreSQL, and asserts three
  things: a failed migration stops the container with the database's own
  error, a successful one leaves an `alembic_version` table behind, and
  `/ready` reports the database healthy from inside the container.

- **Every generated service shipped a `requirements.txt` pip could not
  satisfy.** The template carried a literal `jfastframework[...]~=0.7`, which
  survived the renumbering to `0.1.0a1`, so `pip install -r requirements.txt`
  in a scaffolded project failed with *No matching distribution found*. The
  pin is now derived from the framework's own version by `framework_pin()`.

  A pre-release is pinned **exactly**, because `~=0.1` does not match
  `0.1.0a1` either: a compatible-release clause normalises to
  `>= 0.1, == 0.*` and `0.1.0a1` sorts below `0.1.0`, so it is out of range
  even with `--pre`. Once the framework reaches a final release the pin
  becomes `~=major.minor` on its own.

  A test now fails if any requirements template hardcodes a version again.
  The resolution itself is deliberately not checked in CI: at release time
  the version being pinned is not published yet, so that check would fail on
  exactly the commit that is correct.

### Added

- **The documentation site has the project's own identity.** A monogram
  (`mark.svg`) and favicon in black and crimson, replacing the letters-in-a-box
  placeholder and the emoji favicon. `docs-site/assets/BRAND.md` says where the
  owl goes; the site falls back to the monogram when it is absent, so a missing
  binary cannot break the build.
- **The sidebar is grouped** into Start here, Build, Run, Guard and Project.
  Twenty-two flat links is a list nobody scans.
- **An "on this page" index** on any page with three or more sections,
  previous/next links in reading order, and a copy button on every code block.
- **A Maturity page**, rendering `STATUS.md`. It is the most useful page on the
  site for anyone deciding whether to depend on a part of this.
- **`docs/local-setup.md` opens with one copy-paste block** that goes from
  nothing to a running stack. It is executed end to end before shipping, not
  written from memory.

- **The resource graph.** Datastores are named instances the workspace owns
  (`[[workspace.resources]]`), and a service binds to one under a variable
  (`uses = [{ resource = "core-db", as = "JFAST_DB_DSN" }]`). Two databases
  of the same type, and one cache shared by two services, are both now
  expressible; neither was before.
- **The DSN is generated.** `jfast workspace env` writes each service's `.env`
  from its bindings. The compose file used to emit a datastore container and
  leave the connection string to a human, which is where drift came from.
- **One password per resource**, generated into a gitignored workspace `.env`
  and never overwritten once set. It replaces the single workspace-wide
  `POSTGRES_PASSWORD`, where a leak anywhere was a leak everywhere.
- `jfast workspace resource`, `jfast link`, `jfast unlink`,
  `jfast workspace validate`, `jfast workspace migrate-resources` and
  `jfast workspace graph` (mermaid or dot, edges labelled with the variable).
- Binding two resources to one variable is refused, and `validate` reports a
  port claimed twice, a binding to a resource that does not exist, and a
  resource nobody uses.

### Fixed

- **The site's hero advertised `pip install jfastframework`,** which does not
  resolve because nothing has been published. It now shows the clone-and-install
  that works today, and says why it is not on PyPI yet.
- A mangled em dash in the hero copy, which had been rendering as `â` since the
  page was written.

### Fixed

- **Every smoke script reported success when it failed.** `trap 'rm -rf
  "${WORK}"' EXIT` ends with a successful `rm`, and bash hands the trap's
  status to the script -- so a failed assertion exited 0 and CI went green.
  All nine now preserve the failing code.

- **The Redis queue had no visibility timeout.** `visibility_timeout` was
  stored and never read, and recovery only drained the worker's own
  processing list -- under a key that included `id(self)`, a memory address.
  A worker that died came back under a different name and never recovered
  its own in-flight jobs, so the guarantee `queues.base` documents for every
  backend did not exist here. Workers now register in a hash with a
  heartbeat on server time, and any worker returns the jobs of a consumer
  whose heartbeat has gone stale. `close()` hands work back immediately, so
  a rolling deploy does not park jobs until the timeout expires.
- **`/ready` ran its checks serially and without a timeout.** A dependency
  hanging at the TCP level held the probe open until the socket gave up.
  Checks now run concurrently under `readiness_timeout` (default 2s), and a
  timeout is reported as `timeout` rather than `fail` -- one means the
  dependency said no, the other means it never answered.
- **`BaseRepository.paginate()` emitted no `ORDER BY`.** Pages were not
  stable: a row could appear twice while another was never returned.
  Ordering defaults to the primary key and is overridable per repository.
- **The tenant filter failed open.** A repository given a `tenant_id` for a
  model with no such column silently returned every tenant's rows. It now
  raises at construction; a genuinely global model declares
  `tenant_scoped = False`.

### Added

- **Edge protections in the kernel**, all off unless configured: CORS,
  `TrustedHostMiddleware`, a request body size limit answering 413, and a
  request timeout answering 504. Caddy covers these when it is in front;
  `jfast deploy function` puts a service on Lambda with nothing in front.
  Wildcard CORS origins combined with credentials is refused at boot,
  because browsers reject that pair and it would otherwise fail silently.
- `safe_identifier()` validates any table name interpolated into SQL, at the
  point it enters, so the interpolation that follows is provably safe.
- `pip-audit` and `bandit` run in CI as hard gates. Every existing finding is
  waived explicitly with its reason, or fixed.
- Tests for the Redis queue against an in-memory double of the commands it
  issues, and for the repository against SQLite. 380 tests total.

### Changed

- `/docs` and `/openapi.json` are closed when `env = prod` unless set
  explicitly. `/info` already did this; the three are now one rule.
- The PostgreSQL claim query moved to a named `CLAIM_SQL` constant, built
  once per call rather than assembled inline.
- `RabbitMQQueue` no longer takes `visibility_timeout`. The broker redelivers
  unacknowledged messages when a channel closes, so the parameter never did
  anything, and one that does nothing is a promise the caller believes.


### Added

- **`async-blocking` contract rule.** `jfast contracts check` now reports
  calls that stall the event loop from inside `async def`: the standard-library
  cases, the synchronous clients this framework ships with (boto3, pymongo,
  psycopg2, sync redis, sqlite3), a blocking client stored on `self`, and one
  hop into a synchronous helper defined in the same file. Correct offloading
  through `asyncio.to_thread` and friends is recognised and left alone.
  Configurable under `[rules.async_safety]`; waivable inline.

### Changed

- Ruff's `ASYNC` ruleset is enabled for the framework. `ASYNC109` is ignored
  with a reason: it wants a cancel scope instead of a `timeout` parameter, and
  `dequeue(timeout=...)` maps onto a broker primitive.

### Fixed

- `web` plugin: the readiness probe ran two blocking `Path.is_dir()` calls on
  the event loop, once per probe per replica. Now offloaded.


## [0.7.0] - 2026-08-28

Files, tenants, and the three cloud services a deployed app reaches for.

### Added

**`storage` plugin**
- Named disks with a visibility, modelled on Laravel's: code writes to
  `storage.disk("private")` and where that lives is configuration.
- Local and S3/MinIO drivers behind one `StorageBackend` protocol. A local disk
  writes atomically (temp file + `replace`) so a reader never sees a partial
  object.
- A private disk **refuses** to produce a permanent URL. `temporary_url()` signs
  the key *and* the expiry with HMAC, compared in constant time — signing only
  one of the two makes a single valid link a key to the whole disk.
- Every key is validated before it reaches a filesystem or a bucket: traversal,
  absolute paths, backslashes and null bytes rejected, `..` resolved first.
  Local disks re-check after resolution, because a symlink inside the root can
  still point outside it.
- Downloads are `Content-Disposition: attachment` + `nosniff`. An uploaded
  `.html` or `.svg` served inline runs the uploader's script on your origin.
- Expired and forged links return the same 403 with the same message.
- MinIO in the generated compose file at port offset `+6`, opt-in.

**`tenancy` plugin**
- Resolves the tenant from a token claim, a subdomain, a path prefix or a
  header, in that **order of trust**. `header` is not in the default list and
  warns in production: `X-Tenant-ID: acme` is one curl away from another
  tenant's data.
- Subdomain parsing rejects multi-label hosts, the bare base domain, and a
  reserved list (`www`, `api`, `admin`, …). `base_domain` is required, or every
  hostname looks like a tenant.
- `require_tenant` returns problem+json 403, with health, metrics and docs
  exempt so probes still pass.
- `jfast workspace caddy --wildcard-tenants` emits a wildcard site block with
  on-demand TLS **and** the `ask` endpoint that gates it. Without `ask`, anyone
  pointing DNS at you can burn your certificate rate limit.

**Social login (`auth`)**
- Google, Microsoft and GitHub presets; any other provider by its endpoints.
- `/auth/{provider}/start` and `/auth/{provider}/callback`, with the state and
  nonce carried in an httponly, samesite=lax cookie and both verified on the
  way back.
- ID tokens verified for audience and issuer. Without the audience check, a
  token minted for anyone else's Google app logs in here.
- `@auth.on_identity` is where a verified identity becomes your user. Missing
  it is a 500, not a cheerful 200.
- `OIDCIdentity.federated_id` is provider-qualified, because subject ids are
  unique per provider and not globally.

**Secrets**
- `load_secrets()` populates `os.environ` from AWS Secrets Manager or Google
  Secret Manager before `create_app()`. An existing environment value wins
  unless overridden; only names are logged, never values; nested JSON is
  refused rather than given an unpredictable flattened name.

**Serverless**
- `jfast deploy function <name> --target aws|gcp` writes the handler, the
  Dockerfile and a deploy script — and does not run them.
- Both targets run the same ASGI app the container runs. Private by default on
  both clouds; `--public` opts in and warns.

**`notifications` plugin**
- Firebase Cloud Messaging over the HTTP v1 API, with a `console` backend that
  logs instead of sending for development and tests.
- Unregistered device tokens are reported back so they can be deleted.
- Not verified against a real FCM project in CI; the payload construction is.

### Fixed

- **The `observability` plugin no longer overwrites a resolved tenant.** It
  trusted `X-Tenant-ID` unconditionally and clobbered `request.state.tenant_id`
  on the way past, so a tenant resolved from a signed claim was replaced by
  `None` before the handler ran. It now fills the gap only when nothing else
  resolved one.
- **`tenancy` runs innermost.** `add_middleware` puts middleware *outermost*,
  which ran tenancy before auth and left the signed `token` source permanently
  unreadable. It is appended instead.
- **`secrets.parse` refuses a JSON array** instead of falling through to the
  `KEY=value` parser and silently loading nothing.

### Added (internal)

- `errors.problem_response()` for middleware, which runs outside FastAPI's
  exception handlers and would otherwise surface a 500 with a stack trace.

## [0.6.0] - 2026-08-27

JWT authentication, and Kubernetes manifests derived from the service contract.

### Added

**`auth` plugin**
- Verification in three modes: `jwks` (fetch the issuer's public keys — the
  default, and the only sane one across services), `public_key` (a pinned PEM),
  `secret` (HMAC, for a single service).
- `require_auth`, `require_scopes(...)`, `require_roles(...)`, `optional_auth`
  as FastAPI dependencies. 401 for "who are you", 403 for "you may not".
- JWKS client with caching, rotation on an unknown `kid`, and a rate limit on
  refresh so forged `kid`s cannot be used to hammer the identity provider.
  Cached keys keep working through a JWKS outage; `/ready` reports staleness.
- Token issuance for a service that owns its own login, with **refresh
  rotation and reuse detection**: a replayed refresh token revokes the whole
  session family.
- Revocation: `POST /auth/logout` denies the `jti` and its refresh family,
  backed by Redis when the `cache` plugin is on. The in-memory fallback
  reports itself as not shared rather than pretending.
- `GET /auth/me` returns identity and permissions — never the token, never the
  raw claims.

**Security decisions, each with a test**
- Algorithms are pinned by configuration and passed explicitly to the decoder,
  so `alg: none` and RS256→HS256 confusion are both refused. Configuring
  symmetric and asymmetric algorithms together is rejected at startup: that
  combination *is* the attack.
- `aud` and `iss` are verified — off by default in most libraries, and without
  them a token for a sibling service is accepted here.
- Expiry leeway is 30 seconds, not minutes.
- Rejection reasons go to the log; the client gets a plain 401.
- **`tenant_id` now comes from a signed claim**, not the forgeable
  `X-Tenant-ID` header. This is the main security reason to enable auth.

**Kubernetes**
- `jfast workspace k8s` — a kustomize tree: Deployment, Service, ConfigMap,
  HPA and PodDisruptionBudget per service, one Ingress, `dev`/`prod` overlays.
- `jfast init` asks whether you need it.
- Liveness probes `/health`, readiness probes `/ready` — the two-endpoint
  contract is what keeps a database blip from restarting every healthy pod.
  A startup probe allows 150s for a slow first boot.
- Non-root, read-only root filesystem, dropped capabilities,
  `maxUnavailable: 0`, and a PDB so a node drain cannot take every replica.
- Ingress serves `/api`, the same shape as the generated Caddyfile, so the
  frontend build is identical locally and in the cluster.

### Not generated, deliberately
- **Databases.** A StatefulSet for PostgreSQL from a scaffolder is how people
  lose data. The manifests read a DSN from a Secret.
- **Real secrets.** `*-secrets.example.yaml` holds placeholders.
- **A login endpoint.** Checking a password against your user table is the
  application's job; `auth.issuer` is provided for your own route.
- **NetworkPolicies, ServiceMonitors, migration Jobs, Helm.** Each needs a
  decision about your system that a generator should not guess.

### Notes
- The manifests are validated as YAML and asserted structurally in
  `tests/test_kubernetes.py`. They have **not** been applied to a real
  cluster in CI. Treat the first `kubectl apply` as the test.

## [0.5.0] - 2026-08-27

Per-project contracts, and a quickstart that CI actually executes.

### Added

**Contracts**
- `contracts.toml` in every generated service: scope (`owns` /
  `does_not_own`), layer boundaries, forbidden calls, required structure,
  declared interfaces, and invariants no checker can verify.
- `jfast contracts init | check | show --json | render | waivers`.
  `check` exits non-zero, so it fails a build rather than printing advice.
- A static, AST-based checker: layer boundaries (relative and absolute
  imports), per-layer forbidden packages, forbidden calls with the reason
  attached, and required files per module.
- Inline waivers — `# contracts: allow <reason>` — with the reason required
  and `jfast contracts waivers` listing every one.
- The contract is validated before the code: two layers claiming one path, or
  a `may_import` naming a layer that does not exist, are reported as contract
  errors rather than producing confident answers to the wrong question.
- `CONTRACTS.md` generated from the same file, so the document and the
  enforced rule cannot disagree.
- `.jfast/skills/respect-contracts/SKILL.md`, and `AGENTS.md` now opens with
  `jfast contracts show --json`.

**Getting started**
- `docs/local-setup.md` — installing from a checkout, generating a project,
  the loop you actually use, and the failure modes worth knowing.
- `scripts/smoke_docs.sh` runs those commands **exactly as documented**, in
  CI. Documentation that has never been executed is a guess.

### Fixed
- **A generated service with `queue` enabled could not start without
  PostgreSQL.** `setup()` raised at startup, so the process crash-looped with
  an asyncpg traceback instead of serving. It now starts, logs the reason, and
  reports itself unready — an orchestrator handles "not ready" gracefully and
  handles a crash loop by paging someone. Same fix for `rag`'s `auto_migrate`.
- The layered contract defaults claimed `modules/*/schemas.py` for two layers.
  Caught by a freshly generated service failing its own contract, which is
  exactly the check that should catch it.
- Layer matching ranked patterns by string length, so the screaming layout's
  catch-all `modules/*/[!_]*.py` beat `modules/*/http.py` and classified every
  router as domain code. It now ranks by specificity — fewest wildcards.

## [0.4.0] - 2026-08-27

Polyglot services, queues and events, one-command start, Caddy at the edge,
and a documentation site — plus real build verification for everything that
had until now only been verified by grep.

### Added

**`jfast start`**
- One command for the opinionated default: a Python modular monolith with
  PostgreSQL + pgvector, Redis, background jobs and a starter module, a Vue
  frontend, a Caddyfile and a workspace compose file.
- A monolith rather than three services on purpose: splitting later is a move,
  un-splitting is a rewrite.

**Polyglot services**
- `docs/service-contract.md` — the contract every JFast service satisfies
  regardless of language: `/health`, `/ready`, `X-Request-ID`, problem+json,
  `JFAST_*` config, ten-port blocks, JSON logs on stdout.
- `jfast new service --language go` — a Go service with **zero third-party
  dependencies**, implementing the contract in ~300 vendored lines. CI runs
  `go vet`, `go test`, `go build`, starts the binary and curls it.
- `jfastframework/languages.py` — the language registry. You only need the
  toolchain for the languages you actually use.

**gRPC**
- `--grpc` generates the `.proto` contract (health, problem, a domain service)
  and reserves port offset +9. **Contract only:** no stubs are generated and no
  server is wired, because pinning a `protoc` version inside a scaffolder makes
  generated stubs disagree with whatever CI has. See `proto/README.md`.

**Queues**
- `queue` plugin with a `QueueBackend` protocol and three backends: PostgreSQL
  (`FOR UPDATE SKIP LOCKED`, transactional enqueue), Redis (`BLMOVE` into a
  per-worker processing list), RabbitMQ (dead-letter exchange with a TTL for
  delays).
- `TaskRegistry` and `Worker`: bounded exponential backoff, dead-lettering,
  job timeouts, in-flight draining on shutdown, immediate wake on stop.
- `GET /queue/stats`.

**Events**
- `events` plugin — Kafka publish/subscribe with partition keys, offsets
  committed after handling, and KRaft-mode infrastructure (no ZooKeeper).

**Edge and workspace deployment**
- `jfast workspace compose` — one compose file for every service, whatever its
  language, with per-service datastores.
- `jfast workspace caddy` — a Caddyfile putting the workspace behind one
  hostname. Backends live under `/api` with or without a gateway, so the
  frontend's production build keeps working the day one appears.
- Frontends gained `.env.production` with `VITE_API_URL=/api` — relative, so
  no CORS and no rebuild per environment.

**Documentation site**
- `docs-site/build.py` renders the repository's own markdown into a static,
  versioned site; `docs-site/check.py` validates links, anchors, assets, theme
  tokens and unrendered template artifacts.
- `.github/workflows/pages.yml` publishes it, rebuilding every released minor
  version from its own tag so older versions keep working.

### Fixed
- **The generated Vue and React routers were not valid JavaScript.** The marker
  comment `/*nuevaRuta*/` sat inside a `/* … */` block comment, whose inner
  `*/` closed the comment early. Every grep-based check passed — the marker
  *was* there — and only `vite build` caught it. Both frontends now install and
  build in CI.
- **The worker busy-waited on any non-blocking backend.** PostgreSQL polls and
  returns instantly when the queue is empty, so the loop never yielded: it
  burned a core and starved the event loop, which meant the HTTP handlers in
  the same process stopped responding while "the worker is running".
- `jfast start`'s frontend called a dev port that Caddy served under a
  different path. Both now agree on `/api`.

### Changed
- `PLUGIN_CATALOG` gained `queue` and `events`; `--with queue` pulls in a
  backend the service can actually reach, the same way `rag` does.
- `SERVICE_KINDS` and the installer offer Go.
- CI gained three jobs: Go (`setup-go`), frontend (`setup-node`) and the docs
  site. The frontend job exists because of the router bug above.

### Not done, deliberately
- **Angular** is not generated. A hand-rolled `angular.json` that has never run
  under `ng serve` looks finished and fails in a way that is hard to attribute.
- **React Native** is not generated, for the same reason.
- **RabbitMQ and Kafka** are written against documented APIs but have not been
  round-tripped against real brokers in CI.
- **Laravel and .NET** extensions are not started. The service contract is the
  extension point; a language needs a `LanguageSpec`, a template tree and a CI
  job that builds what it generates.

## [0.3.0] - 2026-08-27

Multi-service workspaces, an API gateway, frontend generation, and the
migration/testing setup that the previous release only *documented*.

### Added

**Workspaces**
- `jfast.workspace.toml` and `jfastframework.workspace`: services register
  themselves, take the next free ten-port block, and the file records what the
  frontend should call.
- `jfast workspace init | list | gateway | env`.

**API gateway**
- `gateway` plugin: prefix-based reverse proxy with hop-by-hop header
  stripping, `X-Request-ID` propagation, and 502/504 as problem+json.
- Generated automatically once a workspace has more than one backend. One
  backend deliberately does not get one.
- Not a catch-all: only configured prefixes are proxied, so the gateway keeps
  its own `/health`, `/ready` and `/metrics`. Readiness does not probe
  upstreams, so one restart does not fail the whole system.

**Frontends**
- `jfast new service <name> --kind spa --frontend vue|react` — Vite +
  Tailwind v4 project with a working home page that calls the backend's
  `/health` on load.
- `jfast new view <Name>` — the `Modulo<Name>/{Components,Pages,Routes,Services}`
  structure, registered in the router and the sidebar at marker comments.
- `jfastframework.cli.patcher`: idempotent, loud, marker-preserving patching.
  Re-running the generator does not duplicate; a missing marker raises with the
  path instead of silently doing nothing.
- Framework auto-detected from the project, so `--frontend` is not repeated.

**Migrations and tests in generated services**
- `alembic.ini`, `migrations/env.py`, `script.py.mako` and `versions/`.
  `env.py` reads the app's own `JFAST_DB_DSN` and auto-imports every module's
  models, so autogenerate cannot silently emit an empty migration.
  `compare_type` and `compare_server_default` are on.
- `pytest.ini` and a `conftest.py` with `app` / `client` fixtures.

**Datastore selection from the terminal**
- `jfast init` — interactive installer: kind, frontend, datastores, port.
- `jfast new service --with database,cache,qdrant,rag` — the plugin list, the
  `[plugin.*]` blocks, the `.env` keys and the pinned extras all derive from it.
- `rag`'s store is inferred from the datastores chosen, so `--with qdrant,rag`
  cannot generate a service configured for pgvector.

### Changed
- **Breaking:** `jfast new service --port` now defaults to the next free block
  in the workspace instead of 8000.
- `SERVICE_KINDS` gained `spa` and `gateway`.
- `.vue`, `.jsx` and `.tsx` templates render through the square-bracket Jinja
  environment, so Vue interpolation and JSX braces survive scaffolding.

### Fixed
- **CI:** `mypy --strict` failed on `redis.asyncio.from_url` being untyped in
  some redis releases and annotated in others — a strict run that passed
  locally and failed in CI on nothing we wrote. Optional third-party packages
  are now `follow_imports = "skip"`, which is honest about types we neither
  control nor can rely on across the support matrix.
- **CI:** `scripts/smoke.sh` hardcoded `.venv/bin/python`, which does not exist
  in a CI job. It now falls back to `PATH`.
- Dropped the dead `tomli` dependency marker (`requires-python` is already
  `>=3.11`).

## [0.2.0] - 2026-08-27

Datastores became a choice, and a frontend became a service.

### Added

**Datastores**
- `VectorStore` protocol in `jfastframework.vectors`, with `Chunk` and
  `SearchHit` as the shared vocabulary. Every store normalises its score to
  cosine similarity in [0, 1].
- `qdrant` plugin — client, health check, container with HTTP and gRPC ports.
- `mongo` plugin — Motor client and database handle.
- `rag` now selects its store from config: `pgvector`, `qdrant`, or a dotted
  path to your own class. Same for the embedder.
- `InfraService.extra_ports` for containers exposing more than one port.

**Server-rendered frontends**
- `web` plugin — Jinja2 templates, static files, and `render()` with HTMX
  partial rendering: a browser navigation gets the page, an `hx-get` gets the
  fragment, from one handler.
- HTMX-aware error handling: a `JFastError` raised during an HTMX request
  returns an HTML fragment instead of `problem+json`, which HTMX would
  otherwise swap into the DOM as raw text.

**Generator**
- `jfast new service <name> [--kind api|web]` — scaffolds a whole service.
- `jfast new module <name> [--layout layered|screaming] [--ui api|htmx]`.
- `module_screaming` layout: framework-free domain, one file per use case,
  storage and HTTP at the edges, domain tests separated from use-case tests.
- `ui_htmx` overlay — composed onto either layout rather than duplicated, so
  three template trees cover all four combinations.
- Two Jinja environments in the scaffolder: `.html.j2` templates use `[[ ]]`
  for scaffold-time values so the runtime `{{ }}` the browser needs survives.
- Table names are pluralised, which also dodges the SQL reserved words that
  singular nouns keep landing on (`order`, `user`, `group`). Override with
  `--table`.

**Docs**
- `docs/modules.md`, `docs/datastores.md`.

### Changed
- **Breaking:** `jfast new <name>` is now `jfast new module <name>`.
- **Breaking:** `[plugin.rag] table` renamed to `collection` — it names a
  Qdrant collection just as often as a PostgreSQL table now.
- `rag` no longer hard-requires `database`. It declares `after` and validates
  the store it was actually configured with, naming the missing plugin.
- Module templates moved to `module_layered/`; both layouts now export
  `build_service(session, tenant_id)`, the seam the HTMX overlay consumes.
- mypy no longer pins `python_version`; it checks against the interpreter it
  runs on, which CI varies across the support matrix.

### Fixed
- `chunk_text` emitted a final sliver already contained in the previous chunk
  whenever the text did not divide evenly — a wasted embedding call and a
  duplicate in every result set.

## [0.1.0] - 2026-08-27

First alpha. Kernel and built-in plugins.

### Added
- `create_app()` with plugin resolution, registration and lifespan orchestration
- Typed configuration: `JFastSettings`, `JFastConfig`, `jfast.toml` + env
- `AppContext` with `provide` / `require` indirection between plugins
- Plugin contract: `PluginMeta`, `PluginSettings`, lifecycle hooks,
  `infra()`, `describe()`
- Registry: entry-point discovery, allow/deny lists, dependency ordering,
  cycle detection, duplicate-provider detection
- RFC 7807 `application/problem+json` error model
- `/health`, `/ready`, `/info` system endpoints
- Built-in plugins: `observability`, `metrics`, `sentry`, `database`, `cache`, `rag`
- `jfastframework.db`: declarative `Base` with a pinned constraint naming
  convention, `TimestampMixin`, `TenantMixin`, generic `BaseRepository`
- Deploy generation: `docker-compose` and `Dockerfile` derived from the plugin
  graph's `infra()` declarations
- `jfast` CLI: `new`, `describe`, `doctor`, `plugins list`, `deploy`
- Jinja2 module template, `jfastframework.testing` fixtures
- Agent surface: `AGENTS.md`, `.jfast/skills/` with four starter skills

### Notes
- Multi-tenancy is a convention enforced by `BaseRepository`, not a guarantee.
  Row-level security is phase 2. Do not describe it as isolation until then.
- The v0 prototype is preserved under `legacy/` for reference.
