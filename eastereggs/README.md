# jfastframework-eastereggs

> **Warning: unprofessional, community-made.** These easter eggs were
> contributed by the community, and some of them are not professional: crude
> names, art and videos you would not want in front of a client. They are kept
> on purpose, and kept out of the framework. Nothing in this folder is installed
> unless you install it yourself.

Terminal easter eggs for JFastFramework, shipped as their own distribution so
that installing the framework never installs them.

| You run | You get |
| --- | --- |
| `pip install jfastframework` | the framework, no easter eggs |
| `pip install "jfastframework[all]"` | every plugin extra, still no easter eggs |
| `pip install ./eastereggs` (from a checkout) | the easter eggs |

Straight from GitHub, without cloning:

```bash
pip install "jfastframework-eastereggs @ git+https://github.com/JFabrizzio5/JFastFramework@Develop#subdirectory=eastereggs"
```

There is no `jfastframework[eastereggs]` extra. An extra names a distribution
pip has to find on an index, and this one is not on PyPI; the extra would be a
documented command that fails to resolve.

## Use

Every folder in [`src/`](src/) is one easter egg, and the folder name is the
import name. Importing one prints nothing and opens nothing. Calling does:

```python
import importlib

egg = importlib.import_module("name")  # any folder name under src/
egg.show()  # bundled terminal art
```

Some come in pairs. `play_video(companion)` on the first opens a YouTube video
in a new browser tab when handed the other one, and refuses any other module;
each `play_video` docstring names its companion. Playback follows the browser's
autoplay policy.

## Name collisions

These are top-level modules, and several share their name with unrelated
projects on PyPI. Installing one of those and this into the same environment
leaves whichever was installed last. Keep this out of any environment you
deploy.
