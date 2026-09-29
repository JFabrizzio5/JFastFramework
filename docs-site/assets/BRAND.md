# Brand assets

| File | Where it is used |
| --- | --- |
| `mark.svg` | The topbar monogram, and the landing's hero, large, on a pane of glass. |
| `favicon.svg` | The browser tab. A square crop, with no gradients, because at 16px they turn to mud. |
| `site.css` | The palette -- liquid ruby. Tokens are defined once on `:root` (dark, the default) and re-pointed once under `[data-theme="light"]`. |
| `liquid.js` | The glass ribbon behind the landing: two twisted tubes drawn with three.js, refracting a ruby body. |

## The mark

A J leaning forward. **One shear angle, 18 degrees, governs every cut** in it,
including the foot of the hook -- that single constraint is what keeps three
shapes reading as one letter rather than as a pile of parallelograms. Flat
fills only, no gradients: a favicon at 16px loses a gradient and keeps a
silhouette, so the silhouette is what carries the mark.

The two speed lines behind it are set at 22% and 15% opacity. They are meant to
be noticed second, and they are dropped entirely from the favicon, where at
16px they are two grey pixels of noise.

`favicon.svg` centres the letter's bounding box inside its tile. If you edit
the paths, recompute the transform: getting it wrong clips the hook off the
right edge, which reads as a rendering bug rather than as a logo.

## The ribbon

`liquid.js` needs three.js, which the landing loads from cdnjs, pinned to r128
with an integrity hash. If the CDN is blocked, the hash does not match, or the
browser has no WebGL, the script adds `no-webgl` to `<html>` and CSS paints a
still ruby glow where the ribbon was. Nothing else on the page depends on it.

It is on the landing only. Documentation keeps a still glow behind the text:
reading over motion is reading nobody finishes.

## The palette

Ruby, on a near-black studio.

| Token | Dark (default) | Light |
| --- | --- | --- |
| Accent | `#e11d2e` | `#c8102e` |
| Link text | `#ff4d5e` | `#b80f28` |
| Glass body | `#7a0714` | `#7a0714` |
| Background | `#030305` | `#f8f9fd` |
| Terminal | `rgba(6, 6, 9, .86)` | `#0b0b10` |

Links use the brighter ruby on the dark ground: `#e11d2e` there is 4.3:1, under
AA for body text, while `#ff4d5e` is 6.4:1.

The wordmark sets `jfast` in the accent and `framework` in the text colour, so
it reads as one word and still says which half is the name.
