---
name: respect-contracts
description: Read a project's contract before writing code in it, and verify
  the code against it before calling the work done.
when_to_use: Always, before any code change in a project that has a
  contracts.toml. This is the first thing to do, not a final check.
when_not_to_use: There is no contracts.toml and the user does not want one —
  then say so rather than inventing constraints.
---

## Step 1: read the contract, before writing anything

```bash
jfast contracts show --json
```

That returns:

| Field | What it tells you |
| --- | --- |
| `owns` / `does_not_own` | Whether this change belongs here at all |
| `layers` | Which file may import which. Not a suggestion — it is checked |
| `rules.forbid_call` | Calls that will fail the build, and why |
| `rules.require` | Files every module must have |
| `provides` / `consumes` | Interfaces you must not break |
| `invariants` | Rules no checker can catch. Read these carefully |

**`does_not_own` is the one to read first.** If what you were asked to build
falls under it, the correct move is to say so and name the service that owns
it — not to build it here because it was easier.

## Step 2: write code that fits

Put the file where its layer says it goes, and import only inward. The layer is
decided by the `paths` glob in `[layers.*]`, not by the filename you would have
guessed: a layered module splits by file, a modular one by package, a hexagonal
one by directory. `jfast.toml` records the layout each module was generated
with, and `modules/<name>/README.md` maps that layout's files to their roles.

If a change seems to need an import the layer forbids, one of three things is
true:

1. The code belongs in a different layer — move it.
2. The layers are wrong — change `contracts.toml` **and say that you did**, in
   the same change, with the reason.
3. It is a genuine one-off — waive it with a reason (step 4).

Never silently work around a boundary. Re-exporting a forbidden import through
an intermediate module technically passes the checker and is worse than the
violation, because now the rule is broken *and* invisible.

## Step 3: check before saying you are done

```bash
jfast contracts check
```

Non-zero exit means the change is not finished. The output carries the `why`
from the contract, which is usually the fix:

```
modules/invoice/<file>:14: forbid-call: os.getenv() is not allowed here
  (Configuration is typed. Add a field to a settings model so a bad value fails at boot.)
```

Run it alongside the rest:

```bash
pytest && ruff check . && jfast contracts check
```

## Step 4: waivers, when they are genuinely right

```python
from sqlalchemy import text  # contracts: allow one-off reporting query, JF-412
```

The reason is required. Include a ticket or a sentence someone can evaluate
later — "temporary" is not a reason.

Waive at most one thing per change. A change that needs three waivers is a
change that is fighting the architecture, and the honest move is to raise that
with the user rather than paper over it.

## If there is no contract yet

```bash
jfast contracts init                      # modular layout, the default
jfast contracts init --layout modular
jfast contracts init --layout screaming
jfast contracts init --layout hexagonal
```

Pick the layout the modules are actually in — the four templates differ in the
`paths` glob of every layer, and a contract whose globs match no file passes
without checking anything. `jfast.toml` records each module's layout; if the
service already mixes two, generate for the majority and say which modules are
left uncovered.

Then **do not leave the defaults**. The generated file is a floor. Ask the user
for, and fill in:

- what this service owns, in one sentence;
- what it deliberately does **not** own, and who does;
- the invariants that matter here (money as integers, tenant scoping, delivery
  semantics) — the rules that are expensive to get wrong and impossible to
  check statically.

Those three are the whole value. Layer rules are table stakes; the sentence
that stops a service growing into its neighbour's job is not.

## Verification

```bash
jfast contracts check
jfast contracts waivers      # nothing new that you did not intend
```

## Common mistakes

- Writing code first and reading the contract when the check fails.
- Deciding a file's layer from its name. The `paths` glob decides, and it is
  different in each of the four module layouts.
- Adding a module in a layout the contract was not generated for, then reading
  the passing check as approval. Its files matched no layer at all.
- Editing `contracts.toml` to make a violation go away, silently. Changing the
  rules is allowed; doing it without saying so is not.
- Leaving the generated `TODO:` lines in `owns` / `does_not_own`. A contract
  full of placeholders trains everyone to skim it.
- Treating a passing check as "the code is good". It proves the structure is
  right. It says nothing about whether the logic is correct.
