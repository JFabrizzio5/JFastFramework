# The local loop

```bash
jfast dev
```

Containers up and waited for, migrations applied, then the API, the queue
worker and the frontend together. One command for the things somebody does
every morning, in the order that makes the failures land where they belong.

---

## What it does, in order

| Stage | What runs | If it cannot |
| --- | --- | --- |
| 1. Infrastructure | `docker compose up -d <db> <cache>`, then waits for health | says why, and stops |
| 2. Migrations | `alembic upgrade head` | **stops** |
| 3. API | `uvicorn main:app --reload --no-proxy-headers` | — |
| 4. Worker | `jfast worker`, when the `queue` plugin is enabled | says why, and carries on |
| 5. Frontend | `npm run dev` in the frontend project | says why, and carries on |

The worker is there because the queue is on in every generated service, and a
queue nobody consumes is jobs piling up in `jfast_jobs` -- and event
subscribers that never run -- while the API answers 201. It boots the same
`main:app`, so it runs the same modules' `@task` and `@subscribe`. It does
**not** reload: restart `jfast dev` after changing a task or a subscriber.

> `--no-proxy-headers` is not optional decoration. uvicorn ships its own
> forwarded-header resolver **on**, trusting `127.0.0.1` -- which is exactly
> what `serve` binds -- and it rewrites the client address before any
> middleware runs, so `trusted_proxies` never gets to decide. `jfast dev`
> and `jfast serve` pass it for you.


Every stage is skippable and every skip is announced:

```bash
jfast dev --no-infra      # the containers are already up
jfast dev --no-migrate    # you are mid-migration and know it
jfast dev --no-web        # backend only
jfast dev --no-worker     # no queue worker (run `jfast worker` yourself)
jfast dev --port 9000     # override the port in jfast.toml
```

No Docker on the machine, no compose file, no `alembic.ini`, no frontend: each
one degrades to a printed line and the rest still runs. A dev command that
refuses to start because the optional half is missing is a dev command people
stop using.

---

## Why a failing migration stops everything

It is the one hard stop, and deliberately so.

A server booted against a schema that is behind does not fail at boot. It fails
later, in a request that has nothing to do with the missing column, with an
error that names a table rather than the migration nobody ran. The half hour
that costs is worth more than the convenience of starting anyway.

```
  ✗ alembic upgrade head failed:
    (psycopg.errors.UndefinedColumn) column "tenant_id" does not exist
```

Fix it, or pass `--no-migrate` and own the consequence.

---

## The `.env` translation

This is the part worth understanding, because it is invisible when it works.

The generated `.env` is written for **compose**, where services address each
other by name and compose interpolates the password:

```bash
JFAST_DB_DSN=postgresql+asyncpg://app:${SHOP_DATABASE_PASSWORD}@shop-database:5432/app
```

Both halves of that are false for a process running on your machine.
`shop-database` resolves on the compose network and nowhere else, and nothing
interpolates `${SHOP_DATABASE_PASSWORD}` for a plain `uvicorn` — the DSN would
reach asyncpg with the braces still in it.

So `jfast dev` rewrites both before it runs anything:

```bash
JFAST_DB_DSN=postgresql+asyncpg://app:s3cr3t@localhost:9431/app
```

The host port comes from what compose actually publishes, read out of the
compose file. **The same environment goes to Alembic and to the server**,
because Alembic runs first — getting the translation onto only the server means
the migration fails with a DNS error naming a host that was never meant to
resolve there.

Anything it cannot resolve is left exactly as it was. A wrong guess would be
harder to debug than the original value. A variable already set in your shell
is left alone too, as pydantic-settings leaves it over `.env`.

The translation is not only `jfast dev`'s. `jfast serve`, `jfast worker` and
`jfast migration check`/`plan` apply it when they find the compose file the same way (the service directory or
one level up), and `jfast exec -- <command>` runs anything else with it --
`jfast exec -- alembic revision --autogenerate -m "add invoices"`, `jfast exec
-- pytest`. Inside a container nothing is translated: there the compose names
resolve and `localhost` is the container. The production image has no compose
file to find in any case (`.dockerignore` drops it, and the workspace's lives
outside the build context).

---

## Stopping it

Ctrl-C stops everything it started. So does `SIGTERM` — from `timeout`, from a
supervisor, from closing the terminal.

That second one needed its own handler and is worth knowing about if you ever
edit this. The children are deliberately put in **their own process groups**,
so a Ctrl-C at the terminal does not reach them directly and the parent can
shut them down in order. But the default disposition for `SIGTERM` kills the
interpreter outright — no `finally`, no cleanup — and the children, being in
their own groups, would have survived as orphans still holding the ports. The
next `jfast dev` then fails with `address already in use` and a confusing hunt.

`scripts/smoke_dev.sh` asserts it: start, `SIGTERM`, count what is left.

---

## `dev` versus `serve`

| | `jfast serve` | `jfast dev` |
| --- | --- | --- |
| Backend | yes | yes |
| Containers | no | brings them up |
| Migrations | no | applies them |
| Queue worker | no (`jfast worker`) | starts it |
| Frontend | no | starts it |
| `.env` translated for the host | yes | yes |

`serve` is the smaller tool and stays that way: one process, no side effects,
nothing started that you have to remember to stop. Reach for it when the
database is already running and you want a server and nothing else.

---

## Everything in containers instead

```bash
docker compose up --build
```

Which is what runs in production, and needs nothing installed locally. The
compose file `jfast start` writes is complete — the workspace `.env` with the
generated passwords and each service's `.env` are written alongside it, so this
works on a fresh clone with no further steps.

Use `jfast dev` when you want a reload loop and a debugger. Use compose when
you want to know it works the way it will be deployed.
