# shared/

Code more than one module needs.

## The rule

**This is vocabulary, not behaviour.** An enum, a type or a pure function that
two modules both speak moves here -- and `jfast contracts check` reports the
cross-import and names the file to move it to:

```
modules/nomina/service.py:4: cross-module: module 'nomina' imports modules.cheques.enums, private to module 'cheques'
  (an enum or type two modules both speak is vocabulary: move it to shared/enums.py and import it from both)
```

**Data is not.** When one module needs another's rows, it calls a function in
`modules/<other>/public.py` that returns DTOs, and declares the dependency in
`contracts.toml`. A repository or a service moved here to dodge that check is
two modules sharing a table.

**The direction is one-way.** Modules import `shared/`; `shared/` imports
nothing from a module. That is checked too, because without it this directory
becomes the place everything ends up and the graph becomes a circle.

## What goes here

| Belongs | Does not |
| --- | --- |
| Enums two modules both speak | Anything only one module uses |
| Value objects and shared types | Anything that touches the database |
| Pure functions | Anything that makes an HTTP call |

The database line is the one that matters. Two modules sharing a repository is
two modules sharing a table, and that is how a set of services becomes a
distributed monolith with extra latency. The contract in `contracts.toml`
forbids `sqlalchemy` here for that reason.

## Where an enum goes

Start it in the module that needs it:

```bash
jfast new enum InvoiceStatus --module invoice
```

Move it when a second one does:

```bash
jfast new enum DocumentStatus --shared
```

The check tells you when that day arrives. You do not have to predict it.
