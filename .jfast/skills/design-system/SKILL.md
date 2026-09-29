---
name: design-system
description: Apply a DESIGN.md to generated UI so screens share one visual
  language instead of looking like default templates.
when_to_use: Building or restyling any user-facing surface — a dashboard, an
  admin panel, a landing page, a Vue 3 frontend for a service.
when_not_to_use: Backend-only work, or a change that touches no rendered
  output.
---

## The contract

A `DESIGN.md` is the design system in a form an agent can read. Place it at the
frontend root, or next to a module that owns its own surface.

```markdown
# DESIGN.md — <product>

## Voice
Two or three adjectives, and one line on what to avoid.

## Color
--bg, --surface, --border, --text, --text-muted, --accent, --danger
Light and dark values for each. Every token defined in both.

## Type
Family, scale (e.g. 12 / 14 / 16 / 20 / 28 / 40), weights in use, line heights.

## Space
One base unit, and the multiples allowed. Nothing off-scale.

## Radius, elevation, motion
Border radii in use. Shadow levels. Transition durations and easing.

## Components
For each: anatomy, states (default / hover / active / disabled / loading /
empty / error), and what it must never do.
```

## The user's look wins

A generated frontend starts in one of two looks — `nexora` (the default) or
`classic`, chosen with `--template` and recorded in the project's
`.jfast-template`. That is a starting point. When the user asks for another
look, produce it: `--template classic` for the other shipped one, or a restyle
of the tokens and components for anything else. The rules below keep a look
consistent; none of them is a reason to keep the default.

The pages the nexora look was drawn from -- JFast Suite: dashboard, CRM,
payments, social feed, tables, widgets -- are copied into
`src/jfastframework/templates/agent_design_nexora/.jfast/skills/nexora-reference/suite/`,
and every nexora frontend generated with `--agent-docs` gets them as the
`nexora-reference` skill. Look there before designing a screen from nothing.

## Steps

1. **Find the DESIGN.md.** Check the frontend root, then the module, then the
   repository root. If none exists, write one *before* writing components —
   otherwise the tokens get invented per file and never converge.

2. **Emit tokens once.** In a classic SPA that means `@theme` in
   `src/style.css` (Tailwind v4), whose surface tokens are declared
   `@theme inline` so one variable swap flips the whole interface. In a
   nexora SPA the tokens are in `src/nexora/nexora.css` and `src/style.css`
   maps them onto the same utilities; the accent is derived from one colour
   by `src/nexora/accent.js`. In a
   server-rendered service it means custom properties on `:root` in
   `static/app.css`. Either way: define every token on the light `:root` and
   only *redefine* it in the dark block. A colour whose single definition is
   inside a media query has no value in the other theme.

3. **Consume tokens only.** No literal hex values, no one-off pixel values in
   components. If a needed value is missing from the system, add it to
   `DESIGN.md` first, then use it. That ordering is what keeps the system real.

4. **Cover every state.** Loading, empty and error states are where generated
   UI reveals itself as generated. An empty state with no copy is a bug.

5. **Check contrast.** Body text at 4.5:1 against its background, large text at
   3:1. A palette that fails contrast is not a style preference.

## Vue 3 conventions in this stack

- `<script setup>` and the Composition API. Never Options API.
- One Pinia store per domain: `useOrderStore`, not one global store.
- HTTP goes through the single axios instance in `src/services/api.js`, called
  from a module's `Services/`. Not from components, not from stores.
- Components `PascalCase.vue`; route-level views end in `View.vue`.

```
src/
├── components/     reusable, presentational
├── composables/    useTheme, useToast — hooks/ in the React scaffold
├── layouts/        the authenticated shell
├── views/          one per top-level route
├── stores/         one Pinia store per domain
├── services/       api.js, the axios instance everything goes through
├── router/         index.js, with the /*nuevaRuta*/ marker
├── menuAside.js    the sidebar, with the /*nuevoModulo*/ marker
└── style.css       the tokens
```

A generated module is a `src/Modulo<Name>/` folder of its own —
`Components/{Modals,Tables}`, `Pages`, `Routes`, `Services` — written by
`jfast new view <Name>`. Put its screens and its HTTP there rather than in the
top-level folders above.

## Verification

- Toggle light and dark. Every surface, border and text token resolves in both.
- Resize to 375px wide. Nothing scrolls horizontally.
- Grep the diff for raw colour values — `#`, `rgb(`, `oklch(` — and hardcoded
  `px` outside the token file. Any hit is a violation.
- Grep the diff for `dark:` in an SPA component. The token already changes with
  the theme; a component that hardcodes both halves is two themes waiting to
  diverge.
- Tab through the page. Focus is visible at every stop.

## Common mistakes

- Copying a reference product's look literally. `DESIGN.md` references are for
  calibrating quality and patterns, not for cloning.
- Tokens defined only for the light theme.
- New values invented mid-component instead of added to the system.
- Shipping the happy path with no empty or error state.
