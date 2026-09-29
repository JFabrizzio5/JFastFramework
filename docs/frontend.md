# Frontends

Two ways to put a UI in front of a JFast backend. Pick by whether you want a
build step.

| | `--kind web` | `--kind spa` |
| --- | --- | --- |
| Renders | Server-side Jinja2 + HTMX | Vue 3 or React, client-side |
| Build step | none | Vite |
| Lives | inside the backend service | its own service |
| Good for | admin panels, internal tools, CRUD | rich client state, offline, mobile-ish UX |

`--kind web` is covered in [modules.md](modules.md). This page is about
`--kind spa`.

---

## Create one

```bash
jfast new service admin --kind spa --frontend vue
cd admin
npm install
npm run dev
```

React instead:

```bash
jfast new service portal --kind spa --frontend react
```

Angular is **not** generated. See "Angular" at the bottom.

## Pick a look: `--template`

A frontend is generated in one of two looks. The choice changes only the
files that draw — the stylesheet, the base components, the layout, the two
shipped screens and the page `jfast new view` writes. Router, stores, the
axios instance, the 401 handling and the generator markers are one shared set,
so everything else on this page holds for both.

| `--template` | What it looks like |
| --- | --- |
| `nexora` **(default)** | Liquid glass, from the Nexora design system: frosted panels over a black studio (and a light one), a glass sidebar, an island top bar, Plus Jakarta Sans with JetBrains Mono for figures, and a WebGL liquid ribbon behind everything. The home page is a small dashboard — KPI cards, a table of registered views, a next-steps column — built only from real values: `/health`, its round trip, and the sidebar |
| `classic` | Plain Tailwind v4 panels on a neutral surface with one crimson accent. No web fonts, no WebGL. Exactly what every frontend looked like before looks existed |

```bash
jfast new service admin --kind spa --frontend vue                     # nexora
jfast new service admin --kind spa --frontend react --template classic
jfast start shop --template classic                                   # the default stack
jfast init                                                            # asks "Which look?"
```

An unknown value is refused before anything is written, naming the two that
exist. `--template` on a service that is not `--kind spa` is refused too: only
a frontend has a look.

### Views follow the look

The choice is recorded once, in the project's `.jfast-template` stamp
(`frontend_template`). `jfast new view` reads it, so a page generated a year
later is drawn like the screens around it. `--template` on `jfast new view`
overrides the stamp; a project with no stamp at all — made before stamps, or
not by jfast — gets the classic page and a note saying so, because a nexora
page would name classes that project does not have.

### What Nexora adds

| Path | Holds |
| --- | --- |
| `src/nexora/nexora.css` | The design system: the palette, light and dark tokens, and the `card-panel`, `btn-modern`, `badge-status`, `kpi-card`, `erp-table`, `nx-*` classes. Imported into Tailwind's `components` layer, so a utility next to one of its classes still wins |
| `src/nexora/accent.js` | The accent: six presets, deriving the whole palette from one colour, remembering the viewer's pick |
| `src/nexora/brand.js` | The product name on the wordmark |
| `src/nexora/background.js` | Whether the ribbon runs at all |
| `src/nexora/liquid.js` | The ribbon, on three.js |
| `src/components/AccentPicker.*`, `LiquidBackground.*` | The two components the classic look does not have |

**The ribbon costs nothing when it cannot help.** `three` is an npm
dependency, never a CDN script, and lands in a chunk of its own (about 130 kB
gzipped) that is fetched after the first paint. Under
`prefers-reduced-motion` it is never loaded, and switching that setting on
stops it. Without WebGL 2 the still CSS frame that is always underneath stays
on screen. A hidden tab renders nothing. It is mounted once at the app root,
not in the layout, so navigating does not rebuild the scene.

**The name** is `VITE_APP_NAME`, exactly as written, or `jfastframework` when
it is empty. Change it in `.env` for `npm run dev` and `.env.production` for
`npm run build`. `jfast workspace env` rewrites `.env`, so set it there again
after running it.

**The accent** is one colour, and glow, deep shade, button text and two
text-safe shades are derived from it — the text shades are moved just far
enough to read at 4.5:1 on each theme, so an amber or a slate accent does not
produce unreadable eyebrows. Three places set it, first one wins:

1. the viewer, with the swatch next to the theme toggle: Ruby, Blue, Emerald,
   Violet, Amber, Slate or any custom colour, kept in this browser under
   `<service>:accent` and replayed before first paint by `index.html`;
2. `VITE_ACCENT="#3B82F6"` in `.env` / `.env.production` — quoted, because an
   unquoted `#` starts a comment in those files;
3. the `--c-*` triplets at the top of `src/nexora/nexora.css`.

Buttons, glows, badges, every `brand-*` utility and the ribbon follow.

### Asking for a different look

The shipped look is a starting point. For the other one, generate with
`--template`; for a look jfast does not ship, restyle the tokens and the
components in `src/components/`, keeping their props, since generated pages
call them. An agent working in the project is told the same thing: the
design skill written by `--agent-docs` describes the look the project was
generated with, and opens by saying that the look the user asks for wins over
it.

## The API URL is already right

`.env` is written from `jfast.workspace.toml`:

```
VITE_API_URL=http://localhost:8030
```

That points at the gateway when the workspace has one and at the single backend
when it does not. The generated home page calls `/health` through it on first
load and shows the result, so a wrong value surfaces immediately instead of on
your first real feature.

After adding a backend (or the day a gateway appears):

```bash
jfast workspace env
```

---

## Adding a module

This is the part modelled on the generator you already had:

```bash
jfast new view Facturas
```

```
src/ModuloFacturas/
├── Components/
│   ├── Modals/
│   └── Tables/
├── Pages/FacturasView.vue
├── Routes/router.js
└── Services/facturas.service.js
```

then it registers the module in two places:

```js
// src/router/index.js
import { ModuloFacturas } from '@/ModuloFacturas/Routes/router.js'
...
  ...ModuloFacturas,
  /*nuevaRuta*/
```

```js
// src/menuAside.js
import { mdiHomeOutline, mdiViewDashboardOutline } from '@mdi/js'
...
  {
    to: '/facturas',
    icon: mdiViewDashboardOutline,
    label: 'Facturas',
  },
  /*nuevoModulo*/
```

The framework is detected from the project, so you do not repeat
`--frontend react` inside a React project. So is the look: the page is drawn
in nexora or classic according to the project's `.jfast-template` (see
[Views follow the look](#views-follow-the-look)).

### The markers

Keep `/*nuevaRuta*/` and `/*nuevoModulo*/`. Three properties are guaranteed,
and each one is a failure mode that is otherwise silent:

| Property | Without it |
| --- | --- |
| **Idempotent** — a guard string is checked first | Two routes and two sidebar entries per re-run |
| **Loud** — a missing file or marker raises with the path | A blank page and no explanation |
| **Marker-preserving** — the marker is written back after the block | The second module has nowhere to go |

A reformatted marker (`/* nuevaRuta */`) still matches: matching strictly would
turn a `prettier` run into a silent no-op.

### Naming

`Facturas` → `ModuloFacturas`, `/facturas`, `facturas.service.js`.
`BillingAccount` → `ModuloBillingAccount`, `/billing-account`, and the sidebar
label reads `Billing Account`.

---

## Base components

Both frontends ship the same set, so the two are the same product rather than
two projects that happen to share a backend.

| Component | What it handles for you |
| --- | --- |
| `BaseButton` | `primary` / `outline` / `ghost` / `danger`, two sizes, and a loading state that also disables it |
| `BaseInput` | Label, error text, hint, disabled — it is meant to be impossible to ship a bare unlabelled input |
| `BaseModal` | Backdrop blur, Escape and backdrop click, focus trapped while open and returned to the trigger on close |
| `BaseBadge` | Status colours, with the word always shown |
| `SkeletonLoader` | The shape of the content that is coming |
| `EmptyState` | A heading, a sentence, and the action that creates the first one |
| `ToastHost` | Renders the toast queue; mounted once at the app root |

**A destructive button is outlined, never solid.** Solid red is already taken
by "proceed", and a screen where the accent means two opposite things has no
accent.

**The mobile drawer locks body scroll**, the same way `BaseModal` does and for
the same reason: it covers the page, so a swipe meant for the menu otherwise
scrolls the article underneath and takes the menu with it. It restores the
previous value rather than clearing it, so a modal already holding the lock
keeps it, and it closes itself past `md` — where the sidebar is static, there
is nothing to close and the lock would just be a page that will not scroll.

### The two states people forget

**Loading is not the word "Loading".** `SkeletonLoader` takes props for lines
and shape so it can be made the shape of what is arriving — which is what stops
the layout jumping when it does.

**Empty is not an empty page.** `EmptyState` says what would be there and
offers the action that creates it. A blank table reads as a bug.

### Toasts

```js
// Vue
import { useToast } from '@/composables/useToast.js'
const toast = useToast()
toast.success('Invoice created')

// React
import useToast from '@/hooks/useToast.js'
const toasts = useToast((state) => state.toasts)

// Anywhere that cannot call a hook — an interceptor, a loader, a guard:
import { toast } from '@/stores/notification.store.js'
toast.error('Could not reach the server')
```

One store, two doors. A toast raised deep inside a service and one raised in a
component land in the same list, in order, under the same timers. Two stores
would mean a toast raised from a service goes into a list the host does not
render — nothing on screen, and no error either.

Timers live outside the store and are cleared on manual dismiss. A dismissed
toast still has a timeout aimed at it; left alive, every manual dismiss leaks
one that later writes state for a toast that is gone.

---

## State

| | Vue | React |
| --- | --- | --- |
| Library | Pinia | Zustand |
| Auth | `src/stores/auth.store.js` | same path |
| Notifications | `src/stores/notification.store.js` | same path |

Both ship wired up: Pinia is installed on the app, Zustand is in
`package.json`, and the auth store persists its token the same way
`services/api.js` already reads it — one source of truth, named in a comment in
the file.

**When not to use a store.** State that no other component reads belongs in the
component. A store is for something two unrelated places need: the layout reads
the user, axios needs the token. That is two, so it is a store.

---

## Sessions, and what happens on a 401

Access tokens are short-lived. Both frontends ship the two halves that need:

| | Where | Covers |
| --- | --- | --- |
| Route guard | `src/router/index.js` / `index.jsx` | navigating to a page while signed out |
| 401 interceptor | `src/services/api.js` | the token expiring on a page already open |

A guard on its own is not enough. It runs on navigation, and nothing navigates
while four panels on the current screen quietly fail.

**Routes are public until they say otherwise.** `PUBLIC_BY_DEFAULT` at the top
of the router is the one line to change. It ships open because the backend's
auth plugin does not mount `/auth/login` — it has no user store — so a scaffold
that gated its home page would show everyone a sign-in form nothing can
satisfy. Until yours has one, opt routes in with `meta: { requiresAuth: true }`
(Vue) or `handle: { requiresAuth: true }` (React). The React guard wraps the
whole route list rather than each route, so a module added at `/*nuevaRuta*/`
is covered without the generator knowing anything about authentication.

`LOGIN_ROUTE` is exported from `auth.store.js`, because `api.js` needs it too
and a second copy of `'/login'` is a second place to forget.

### The interceptor, and the bug the obvious version has

On a 401 it refreshes once and replays the request. Three things in it are not
optional:

**One refresh for the whole burst.** Four panels loading together produce four
401s. Four refreshes present the same refresh token four times — they are
rotated, so three of those are spent, and a backend that reads replay as theft
revokes the session. `refreshOnce()` hands every caller the same promise.

**A refresh that is itself refused ends the session.** It clears the store and
does a whole-page navigation to `LOGIN_ROUTE` with `?next=`, once, however many
requests failed together. Retrying a refused refresh is the loop.

**The replayed request needs the new token put on it, and this is the part that
looks like it already works.** Refreshing updates where the token normally
comes from — `api.defaults.headers` in Vue. Defaults do not reach a config that
already carries `Authorization`, and the replayed config carries one: axios
merged the old token in when the request was first built. So the refresh
returns 200, the retries go back out with the token that just expired, and the
app renders as logged in with every panel empty:

```
/invoices      401  Bearer A1
/clients       401  Bearer A1
/projects      401  Bearer A1
/notifications 401  Bearer A1
refresh        200  -> A2
/invoices      401  Bearer A1     <-- refreshed, retried, same dead token
/clients       401  Bearer A1
/projects      401  Bearer A1
/notifications 401  Bearer A1
```

Vue fixes it by setting `original.headers.Authorization` before the replay.
React does not need that line, and for a reason worth knowing rather than by
luck: its request interceptor sets the header per request from `localStorage`,
and the replay runs through it again. That holds only while the assignment
stays unconditional — wrap it in `if (!config.headers.Authorization)` and React
has the identical bug.

Sign-in itself is `LoginView`, deliberately minimal: it posts to `/auth/login`,
stores the session, and returns to `?next` if that is a path (never an absolute
URL — `next` comes from the address bar).

---

## Light and dark

Three states, not two:

| `<html>` | Result |
| --- | --- |
| no attribute | follow the operating system |
| `data-theme="dark"` | dark, whatever the system says |
| `data-theme="light"` | light, whatever the system says |

Tailwind's stock `dark:` only reads the system, which leaves someone who wants
the other one with no way to say so. `src/style.css` redefines the variant to
check an explicit choice first:

```css
@custom-variant dark {
  &:where([data-theme="dark"], [data-theme="dark"] *) { @slot; }
  @media (prefers-color-scheme: dark) {
    &:where(:root:not([data-theme="light"]), :root:not([data-theme="light"]) *) { @slot; }
  }
}
```

The `:not([data-theme="light"])` in the second half is the part worth
understanding: without it, an explicit light choice loses to a system set to
dark and the switch appears to work in one direction only.

**The toggle** is `ThemeToggle`, in the header of `LayoutAuthenticated`. It
flips what is currently on screen and pins it. Beside it, and only once the
choice is pinned, sits a second button — `followSystem()`, labelled "Follow the
system theme". That one is not decoration: the toggle can only ever pin `light`
or `dark`, so without it the first click takes the third state away for good
and the app is back to the two-state switch this design exists to avoid. The
choice is stored under `<service>:theme`.

**`resolved` is shared, not per-caller.** Both `useTheme()` implementations keep
`theme` *and* `resolved` at module scope — a Vue `computed`, a value React
recomputes on every render behind a subscriber set. Building `resolved` inside
the composable is the version that looks right and is not: one click updates
the component that handled it and nothing else, so a header toggle and a
settings switch on the same page end up showing opposite icons while `<html>`
carries only one of them. A test for this needs two components, not one — a
single toggle asserting `data-theme` flips is asserting the half that was never
broken.

**The flash is handled.** `index.html` carries twelve inline lines that read
the stored choice and set the attribute before the first paint. Applying the
theme after mount instead means one frame in the wrong colour on every reload,
which is the single most noticeable defect a theme switch can have.

### Surfaces are tokens, not colours

```css
@theme inline {
  --color-surface: var(--ui-surface);   /* the page */
  --color-panel: var(--ui-panel);       /* cards, sidebar, header */
  --color-elevated: var(--ui-elevated); /* hover, skeletons, code */
  --color-line: var(--ui-line);         /* every border */
  --color-ink: var(--ui-ink);           /* primary text */
  --color-ink-soft: var(--ui-ink-soft); /* secondary */
  --color-ink-faint: var(--ui-ink-faint);
}
```

`inline` is what makes it work: the generated utilities emit
`var(--ui-surface)` rather than resolving at build time, so redefining seven
variables under `[data-theme="dark"]` flips the whole interface.

What that buys is visible in the components — **not one of them carries a
`dark:` class**. `bg-panel` is correct on both themes. A component that needs a
light value and a dark value on every line does not have a design system, it
has two hardcoded themes that drift apart the first time somebody edits one.

The one exception is status colour: `success`, `warning`, `danger`, `info` on
`BaseBadge` and `ToastHost`. There the hue **is** the meaning, so it cannot come
from a shared surface token. Those are built as a 10% tint of one hue plus a
`dark:` on the text only, because a tint reads correctly on either surface
where a solid `bg-emerald-50` does not.

`color-scheme` is set alongside the tokens, so scrollbars, date pickers and the
caret — drawn by the browser, never touched by a class — match the rest.

---

## Conventions the templates enforce

**One axios instance.** `src/services/api.js` holds the base URL, the timeout,
the 401 handling above, and an interceptor that turns the backend's RFC 7807
`detail` into `error.message`. Without it every component shows "Request failed
with status code 409" instead of "Invoice INV-1 already exists". The one
exception is `plain`, exported from the same file: same configuration, no
interceptors, and `/auth/refresh` is all it is for — a refresh that went
through `api` would have its own 401 answered by another refresh.

**HTTP in `Services/`, never in components.** The generated page calls
`listFacturas()`; it owns loading and error state, the service owns the request.

**Tailwind v4 with tokens.** One `@import "tailwindcss"` and the `@theme`
blocks in `src/style.css`. Reach for `bg-panel`, `border-line` and `text-ink`
rather than a neutral ramp: a component that names `zinc-200` directly is one
the theme cannot reach, and two components that pick different ramps is how a
UI ends up looking subtly wrong with nothing identifiably broken in it. See
[.jfast/skills/design-system/SKILL.md](../.jfast/skills/design-system/SKILL.md).
Nexora keeps the same utilities — `src/style.css` points `bg-panel`, `text-ink`
and the `brand-*` ramp at its own tokens — and adds its component classes on
top, so a page written for either look still takes its colours from tokens.

**Icons without a runtime.** `@mdi/js` ships path strings only; `BaseIcon`
renders one into an SVG. No icon font, tree-shaken to what you import.

Vue specifics: `<script setup>` and the Composition API, never Options API;
components `PascalCase.vue`; route-level pages end in `View.vue`.

React specifics: function components and hooks; components `PascalCase.jsx`;
route-level pages end in `View.jsx`.

---

## Verified how

Be precise about this, because it matters for how much you should trust it.

**Tested in CI:** every file renders; the module structure lands where it
should; router and sidebar are patched correctly and idempotently; Vue's `{{ }}`
and JSX's braces survive scaffolding; the framework is detected from the
project. And, since the base components landed, **`npm install` followed by
`npm run build` on both generated frontends** — `scripts/smoke_components.sh`,
which also asserts that `ToastHost` is mounted somewhere, that there is exactly
one toast store, that the bundle contains a way back to `system` and a
refresh-on-401, and that the drawer locks body scroll. Rendering a template
proves the braces were right; it does not prove a component imports something
that exists.

**Exercised against a stubbed backend, in Node, by hand:** three `useTheme()`
callers and one click, asserting all three agree afterwards; and four
concurrent 401s against the real `api.js` and `auth.store.js` with an axios
adapter for a server, asserting one refresh and four 200s, then the same with
a refresh that is itself refused. Both were written first and both failed on
the previous templates — the first with
`["Switch to dark theme", "Switch to light theme", "Switch to light theme"]`,
the second with four retries carrying the token that had just expired. These
are throwaway harnesses, not a suite: they are **not** in CI.

**Exercised in a real browser, once, by hand:** the theme switch on both
generated frontends — light, dark, an explicit light choice against a system
set to dark, the setting surviving a reload, and the mobile drawer opening.
Computed styles were read back rather than eyeballed. That is one run on one
browser, not a suite: it is not in CI and it will not catch a regression.

**Both looks, both frameworks, in CI:** `scripts/smoke_frontend.sh` generates
Vue and React in `nexora` and in `classic`, runs `jfast new view` in each, and
builds them; it also checks that three.js stays out of the entry chunk and that
the build honours `VITE_ACCENT`. The nexora screens, the ribbon, the accent
picker and both themes were looked at in a browser by hand when they were
written — once, not in CI.

**Not tested:** `npm run dev` as an interactive session, the sign-in form
against a real `/auth/login` (the generated backend does not mount one), and
anything else about how it looks. The build passing means it compiles, not that
a modal traps focus correctly with a real screen reader.

---

## Angular

Not generated, deliberately. A hand-rolled `angular.json` and builder config
that has never been run by `ng serve` is worse than no scaffold: it looks
finished and fails in a way that is hard to attribute.

If you want Angular today: `ng new` the project yourself, then keep the same
`Modulo<Name>` structure by hand. Generator support is PLAN.md phase 3, and it
should land only alongside a CI job that actually builds the output.
