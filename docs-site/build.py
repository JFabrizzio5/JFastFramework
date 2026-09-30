#!/usr/bin/env python3
"""Build the JFastFramework documentation site.

    python docs-site/build.py --version latest --output site/latest

Design constraints, in order:

1. **The markdown in docs/ is the source.** A site that duplicates the docs
   goes stale the first week. This renders them; it does not restate them.
2. **No JavaScript framework, no build toolchain.** The site is HTML and one
   stylesheet, so it still builds in five years.
3. **Versioned.** Each release publishes under its own path and older versions
   keep working, because someone is always pinned to one.

Requires: ``pip install jfastframework[docs]``
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import markdown

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
ASSETS = Path(__file__).resolve().parent / "assets"


@functools.cache
def asset_url(name: str) -> str:
    """``name?v=<hash of its bytes>``: a new URL whenever the file changes.

    Pages tells browsers to keep an asset for ten minutes, and browsers keep
    one longer still under heuristics. With a fixed name, a deploy served new
    HTML against the old site.css from cache -- the new markup under the old
    styles -- until the cache let go. A changed file now has a URL no cache
    has seen.
    """
    digest = hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()[:10]
    return f"{name}?v={digest}"


GITHUB = "https://github.com/JFabrizzio5/JFastFramework"
PYPI = "https://pypi.org/project/jfastframework/"
DESCRIPTION = "A plugin-based FastAPI framework for microservices, built to be driven by AI agents."


#: Applied before the first paint, so a reader who chose light does not get a
#: dark flash on every navigation. Inline and synchronous for that reason: a
#: deferred script runs after the page has already been painted the other way.
THEME_BOOT = """<script>
(function () {
  try {
    var saved = localStorage.getItem("jfast-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    /* Private mode, or site data blocked. The default, dark, applies. */
  }
})();
</script>"""


#: The button itself. Two icons, one shown at a time by CSS, so the control
#: says what it will do rather than what is currently true.
def theme_toggle(lang: str = "en") -> str:
    label = "Switch between light and dark" if lang == "en" else "Cambiar entre claro y oscuro"
    return f"""  <button class="theme-toggle" type="button" data-theme-toggle
          aria-label="{label}">
    <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4"></circle>
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4
               M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path>
    </svg>
    <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path>
    </svg>
  </button>"""


#: Two states. No attribute is dark -- the default the glass is drawn for --
#: and light is a choice the reader makes and the site remembers.
THEME_SCRIPT = """<script>
(function () {
  var button = document.querySelector("[data-theme-toggle]");
  if (!button) return;
  button.addEventListener("click", function () {
    var root = document.documentElement;
    var current = root.getAttribute("data-theme") || "dark";
    var next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try {
      localStorage.setItem("jfast-theme", next);
    } catch (e) {
      /* Nothing to do: the choice lasts for this page only. */
    }
  });
})();
</script>"""


#: The icon set, defined once per page and referenced by <use>. Every path is
#: stroke-only and inherits currentColor, so an icon is the colour of the text
#: it sits next to without a second rule.
ICON_SPRITE = """<svg class="sprite" aria-hidden="true" focusable="false">
  <symbol id="i-terminal" viewBox="0 0 24 24">
    <path d="m7 11 2-2-2-2"/><path d="M11 13h4"/>
    <rect width="18" height="18" x="3" y="3" rx="2"/>
  </symbol>
  <symbol id="i-layers" viewBox="0 0 24 24">
    <path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66
             0l8.58-3.9a1 1 0 0 0 0-1.83z"/>
    <path d="m6.08 9.5-3.48 1.6a1 1 0 0 0 0 1.81l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9a1 1 0 0 0
             0-1.83l-3.5-1.59"/>
    <path d="m6.08 14.5-3.48 1.6a1 1 0 0 0 0 1.81l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9a1 1 0 0 0
             0-1.83l-3.5-1.59"/>
  </symbol>
  <symbol id="i-shield" viewBox="0 0 24 24">
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1
             1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>
    <path d="m9 12 2 2 4-4"/>
  </symbol>
  <symbol id="i-bot" viewBox="0 0 24 24">
    <path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>
    <path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>
  </symbol>
  <symbol id="i-zap" viewBox="0 0 24 24">
    <path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0
             13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>
  </symbol>
  <symbol id="i-split" viewBox="0 0 24 24">
    <circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>
    <path d="M6 9v6"/><circle cx="18" cy="12" r="3"/><path d="M9 12h6"/>
  </symbol>
  <symbol id="i-blocks" viewBox="0 0 24 24">
    <rect width="7" height="7" x="14" y="3" rx="1"/>
    <path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0
             0-1-1H3"/>
  </symbol>
  <symbol id="i-gauge" viewBox="0 0 24 24">
    <path d="m12 14 4-4"/>
    <path d="M3.34 19a10 10 0 1 1 17.32 0"/>
  </symbol>
  <symbol id="i-lock" viewBox="0 0 24 24">
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
    <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
  </symbol>
  <symbol id="i-book" viewBox="0 0 24 24">
    <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1
             0-5H20"/>
  </symbol>
  <symbol id="i-compare" viewBox="0 0 24 24">
    <path d="M12 3v18"/>
    <path d="m8 8-4 4 4 4"/><path d="m16 16 4-4-4-4"/>
  </symbol>
  <symbol id="i-rocket" viewBox="0 0 24 24">
    <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0
             0-2.91 0z"/>
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35
             22.35 0 0 1-4 2z"/>
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/>
    <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>
  </symbol>
  <symbol id="i-play" viewBox="0 0 24 24">
    <path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>
  </symbol>
  <symbol id="i-search" viewBox="0 0 24 24">
    <path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>
  </symbol>
  <symbol id="i-compass" viewBox="0 0 24 24">
    <path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1
             1.265-1.265z"/><circle cx="12" cy="12" r="10"/>
  </symbol>
  <symbol id="i-database" viewBox="0 0 24 24">
    <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/>
    <path d="M3 12A9 3 0 0 0 21 12"/>
  </symbol>
  <symbol id="i-inbox" viewBox="0 0 24 24">
    <polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/>
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0
             16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
  </symbol>
  <symbol id="i-monitor" viewBox="0 0 24 24">
    <rect width="20" height="14" x="2" y="3" rx="2"/>
    <line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>
  </symbol>
  <symbol id="i-package" viewBox="0 0 24 24">
    <path d="m7.5 4.27 9 5.15"/>
    <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1
             1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
    <path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>
  </symbol>
  <symbol id="i-plug" viewBox="0 0 24 24">
    <path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/>
    <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>
  </symbol>
  <symbol id="i-grid" viewBox="0 0 24 24">
    <rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/>
    <rect width="7" height="7" x="14" y="14" rx="1"/>
    <rect width="7" height="7" x="3" y="14" rx="1"/>
  </symbol>
  <symbol id="i-ship" viewBox="0 0 24 24">
    <path d="M12 10.189V14"/><path d="M12 2v3"/>
    <path d="M19 13V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6"/>
    <path d="M2 16.1A5 5 0 0 1 5.9 20M2 12.05A9 9 0 0 1 9.95 20M2 8V6"/>
  </symbol>
  <symbol id="i-cloud" viewBox="0 0 24 24">
    <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z"/>
  </symbol>
  <symbol id="i-arrows" viewBox="0 0 24 24">
    <path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>
    <path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>
  </symbol>
  <symbol id="i-users" viewBox="0 0 24 24">
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
    <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>
  </symbol>
  <symbol id="i-globe" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>
    <path d="M2 12h20"/>
  </symbol>
  <symbol id="i-activity" viewBox="0 0 24 24">
    <path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48
             0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>
  </symbol>
  <symbol id="i-map" viewBox="0 0 24 24">
    <path d="M14.106 5.553a2 2 0 0 0 1.788 0l3.659-1.83A1 1 0 0 1 21 4.619v12.764a1 1 0 0
             1-.553.894l-4.553 2.277a2 2 0 0 1-1.788 0l-4.212-2.106a2 2 0 0
             0-1.788 0l-3.659 1.83A1 1 0 0 1 3 19.381V6.618a1 1 0 0 1 .553-.894l4.553-2.277a2 2 0 0
             1 1.788 0z"/><path d="M15 5.764v15M9 3.236v15"/>
  </symbol>
  <symbol id="i-history" viewBox="0 0 24 24">
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
    <path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>
  </symbol>
</svg>"""


def icon(name: str) -> str:
    """One icon, sized and coloured by CSS."""
    return (
        f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">'
        f'<use href="#i-{name}"></use></svg>'
    )


#: Records the reader's choice when they use the language switch, and honours
#: it on the site root only. See the note in this module's history for why the
#: redirect is not applied to deep links.
LANG_SCRIPT = r"""<script>
(function () {
  var KEY = "jfast-lang";

  function store(value) {
    try {
      localStorage.setItem(KEY, value);
    } catch (e) {
      /* Private mode, or site data blocked. The click still navigates. */
    }
  }

  function read() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  // Clicking the switch is the only thing that records a preference. Landing on
  // a page in some language is not a choice; following the link is.
  var toggle = document.querySelector("a.lang");
  if (toggle) {
    toggle.addEventListener("click", function () {
      store(toggle.getAttribute("hreflang"));
    });
  }

  // Only the root redirects. A deep link resolves to the page it names.
  var path = location.pathname;
  var atRoot = path === "/" || /\/index\.html$/.test(path);
  var inSpanish = /\/es\//.test(path);
  if (!atRoot) return;

  var saved = read();
  if (saved === "es" && !inSpanish) {
    location.replace(path.replace(/index\.html$/, "") + "es/");
  } else if (saved === "en" && inSpanish) {
    location.replace(path.replace(/es\/index\.html$|es\/$/, ""));
  }
})();
</script>"""


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    source: Path
    summary: str = ""


# Sidebar grouping. Nineteen flat links is a list nobody scans; four headings
# turn it into somewhere you can find a page you half-remember.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start here", ("quickstart", "local-setup", "dev", "architecture")),
    (
        "Build",
        (
            "modules",
            "contracts",
            "inspect",
            "shared",
            "datastores",
            "rag",
            "llm",
            "transactions",
            "queues",
            "http-client",
            "frontend",
            "capabilities",
            "plugins",
        ),
    ),
    (
        "Run",
        (
            "workspaces",
            "deploy",
            "telemetry",
            "kubernetes",
            "resilience",
            "cloud",
            "migrations",
            "timezones",
        ),
    ),
    (
        "Guard",
        (
            "auth",
            "accounts",
            "encryption",
            "ratelimit",
            "storage",
            "multitenancy",
            "websockets",
            "languages",
            "agents",
            "skills",
        ),
    ),
    ("Project", ("status", "roadmap", "upgrading", "changelog")),
)

# Order is the reading order, not alphabetical: someone landing here should be
# able to start at the top and keep going.
PAGES: tuple[Page, ...] = (
    Page("quickstart", "Quickstart", REPO / "README.md", "Install, generate, run."),
    Page(
        "modules",
        "Modules and layouts",
        DOCS / "modules.md",
        "Four architectures, one per module.",
    ),
    Page(
        "datastores",
        "Datastores",
        DOCS / "datastores.md",
        "PostgreSQL, Redis, Mongo, Qdrant.",
    ),
    Page(
        "rag",
        "RAG and vector search",
        DOCS / "rag.md",
        "Tenant-scoped retrieval, hybrid search, incremental ingest.",
    ),
    Page(
        "llm",
        "Language models",
        DOCS / "llm.md",
        "Chat, vision and embeddings under a spending cap.",
    ),
    Page(
        "transactions",
        "Transactions",
        DOCS / "transactions.md",
        "Commit timing, conflicts, locks, retries.",
    ),
    Page("queues", "Queues and events", DOCS / "queues-and-events.md", "Jobs and streams."),
    Page(
        "http-client",
        "Calling other services",
        DOCS / "http-client.md",
        "Deadlines, retries and a circuit breaker.",
    ),
    Page(
        "dev",
        "The local loop",
        DOCS / "dev.md",
        "One command: containers, migrations, API and frontend.",
    ),
    Page(
        "agents",
        "Working with AI agents",
        DOCS / "agents.md",
        "Rules an agent cannot quietly break.",
    ),
    Page("frontend", "Frontends", DOCS / "frontend.md", "HTMX, Vue, React."),
    Page("workspaces", "Workspaces", DOCS / "workspaces.md", "Many services, one gateway."),
    Page(
        "languages", "Polyglot services", DOCS / "service-contract.md", "The contract Go satisfies."
    ),
    Page(
        "migrations",
        "Migrations and tests",
        DOCS / "migrations-and-tests.md",
        "Alembic and pytest.",
    ),
    Page("auth", "Authentication", DOCS / "auth.md", "JWT, scopes, rotation, revocation."),
    Page(
        "accounts",
        "Accounts",
        DOCS / "accounts.md",
        "Users, password login, roles and permissions.",
    ),
    Page(
        "encryption",
        "Encryption",
        DOCS / "encryption.md",
        "Secrets the service must read back, with key rotation.",
    ),
    Page(
        "ratelimit",
        "Rate limiting",
        DOCS / "ratelimit.md",
        "A token bucket that does not leak under load.",
    ),
    Page(
        "websockets",
        "Websockets",
        DOCS / "websockets.md",
        "Sockets across workers, and what is not delivered.",
    ),
    Page("storage", "Storage", DOCS / "storage.md", "Disks, signed URLs, S3 and MinIO."),
    Page(
        "multitenancy",
        "Multi-tenancy",
        DOCS / "multitenancy.md",
        "Subdomains, tokens, trust order.",
    ),
    Page("cloud", "Cloud", DOCS / "cloud.md", "Secrets, functions, notifications."),
    Page("kubernetes", "Kubernetes", DOCS / "kubernetes.md", "Manifests from the contract."),
    Page(
        "resilience",
        "Resilience",
        DOCS / "resilience.md",
        "Deadlines, breakers, what fails open, what /ready says, and the drills.",
    ),
    Page(
        "timezones",
        "Time zones",
        DOCS / "timezones.md",
        "Store UTC, compute in UTC, render local.",
    ),
    Page("contracts", "Contracts", DOCS / "contracts.md", "Rules an agent cannot drift past."),
    Page(
        "inspect",
        "Inspect and analyze",
        DOCS / "inspect.md",
        "What is in here, and what is wrong with it.",
    ),
    Page(
        "shared",
        "Shared code and events",
        DOCS / "shared-and-events.md",
        "Where an enum goes, and declared channels.",
    ),
    Page(
        "local-setup",
        "Running it locally",
        DOCS / "local-setup.md",
        "Install, generate, run.",
    ),
    Page(
        "capabilities",
        "Packages",
        DOCS / "capabilities.md",
        "Excel, PDF, XML: what `jfast add` installs, and why.",
    ),
    Page("plugins", "Writing a plugin", DOCS / "plugins.md", "The extension point."),
    Page("deploy", "Deployment", DOCS / "deploy.md", "Compose, Caddy, Dockerfile."),
    Page(
        "telemetry",
        "Telemetry",
        DOCS / "telemetry.md",
        "One request traced through services, SQL, models and jobs.",
    ),
    Page("skills", "Skills for agents", DOCS / "skills.md", "Making it legible to AI."),
    Page("architecture", "Architecture", REPO / "ARCHITECTURE.md", "Decisions and their costs."),
    Page(
        "status",
        "Maturity",
        REPO / "STATUS.md",
        "What is trustworthy, unverified, or broken.",
    ),
    Page("roadmap", "Roadmap", REPO / "PLAN.md", "Done, partial, not started."),
    Page(
        "upgrading",
        "Upgrading",
        DOCS / "upgrading.md",
        "What breaks, and only what applies to you.",
    ),
    Page("changelog", "Changelog", REPO / "CHANGELOG.md", "What changed, and why."),
)


DOCS_ES = DOCS / "es"

#: Shown at the top of a Spanish page that has no translation yet. Explicit,
#: because silently serving English under a Spanish URL is worse than saying so.
UNTRANSLATED_NOTICE = (
    '<div class="notice">Esta p&aacute;gina a&uacute;n no est&aacute; traducida. '
    "El contenido de abajo est&aacute; en ingl&eacute;s.</div>\n"
)

#: The doc shell's own strings.
DOC_CHROME: dict[str, dict[str, str]] = {
    "en": {"skip": "Skip to content", "docs": "Docs", "other": "Español", "toc": "On this page"},
    "es": {
        "skip": "Saltar al contenido",
        "docs": "Documentación",
        "other": "English",
        "toc": "En esta página",
    },
}


def page_source(page: Page, lang: str) -> tuple[object, bool]:
    """The file to render, and whether it is a real translation.

    Falls back to the English source rather than skipping the page, so the
    Spanish sidebar has no dead links.
    """
    if lang == "en":
        return page.source, True
    translated = DOCS_ES / page.source.name
    if translated.is_file():
        return translated, True
    return page.source, False


#: Sidebar and card titles in Spanish. Only the chrome: article bodies fall
#: back to English when `docs/es/<name>.md` does not exist, but the navigation
#: around them should not.
NAV_ES: dict[str, str] = {
    "quickstart": "Inicio rápido",
    "local-setup": "Correrlo en local",
    "dev": "El ciclo local",
    "architecture": "Arquitectura",
    "modules": "Módulos y arquitecturas",
    "contracts": "Contratos",
    "inspect": "Inspeccionar y analizar",
    "shared": "Código compartido y eventos",
    "datastores": "Almacenes de datos",
    "rag": "RAG y búsqueda vectorial",
    "llm": "Modelos de lenguaje",
    "transactions": "Transacciones",
    "queues": "Colas y eventos",
    "http-client": "Llamar a otros servicios",
    "frontend": "Frontends",
    "capabilities": "Paquetes",
    "plugins": "Escribir un plugin",
    "workspaces": "Workspaces",
    "deploy": "Despliegue",
    "kubernetes": "Kubernetes",
    "telemetry": "Telemetría",
    "resilience": "Resiliencia",
    "cloud": "Nube",
    "migrations": "Migraciones y tests",
    "timezones": "Zonas horarias",
    "auth": "Autenticación",
    "accounts": "Cuentas",
    "encryption": "Cifrado",
    "ratelimit": "Límite de peticiones",
    "websockets": "Websockets",
    "storage": "Almacenamiento",
    "multitenancy": "Multi-tenancy",
    "languages": "Servicios políglotas",
    "agents": "Trabajar con agentes de IA",
    "skills": "Skills para agentes",
    "status": "Madurez",
    "roadmap": "Hoja de ruta",
    "upgrading": "Actualizar",
    "changelog": "Cambios",
}

#: The sidebar's four headings.
SECTION_ES: dict[str, str] = {
    "Start here": "Empieza aquí",
    "Build": "Construir",
    "Run": "Ejecutar",
    "Guard": "Proteger",
    "Project": "Proyecto",
}


def page_title(page: Page, lang: str) -> str:
    """The title as the navigation should show it."""
    if lang == "en":
        return page.title
    return NAV_ES.get(page.slug, page.title)


#: One icon per page. Nineteen text links is scannable but not recognisable:
#: finding a page you read last week means reading the whole list again.
PAGE_ICON: dict[str, str] = {
    "quickstart": "play",
    "local-setup": "terminal",
    "dev": "gauge",
    "architecture": "compass",
    "modules": "layers",
    "contracts": "shield",
    "inspect": "search",
    "shared": "blocks",
    "datastores": "database",
    "rag": "search",
    "llm": "bot",
    "transactions": "lock",
    "queues": "inbox",
    "http-client": "zap",
    "frontend": "monitor",
    "capabilities": "package",
    "plugins": "plug",
    "workspaces": "grid",
    "deploy": "ship",
    "kubernetes": "cloud",
    "telemetry": "activity",
    "resilience": "activity",
    "cloud": "cloud",
    "migrations": "arrows",
    "timezones": "history",
    "auth": "lock",
    "accounts": "users",
    "encryption": "shield",
    "ratelimit": "gauge",
    "websockets": "arrows",
    "storage": "database",
    "multitenancy": "users",
    "languages": "globe",
    "agents": "bot",
    "skills": "book",
    "status": "activity",
    "roadmap": "map",
    "upgrading": "arrows",
    "changelog": "history",
}


def render_markdown(text: str) -> tuple[str, str]:
    """Render one document, returning (html, first paragraph)."""
    converter = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "codehilite", "sane_lists", "attr_list"],
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "highlight"}},
    )
    body = converter.convert(text)
    # A table scrolls inside its own frame. Scrolling the <table> itself needs
    # display: block, and a block table stops filling its box: the border and
    # the header ground ran past the last column into empty space.
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )

    # First real paragraph, for the card summary on the index.
    match = re.search(r"<p>(.*?)</p>", body, re.DOTALL)
    summary = re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""
    return body, summary


def rewrite_links(body: str) -> str:
    """Turn repository-relative markdown links into site links.

    The docs are written to be read on GitHub, where ``docs/modules.md`` is
    correct. On the site the same link has to become ``modules.html``, and
    anything with no page of its own (a skill, a script, a template) has to
    become an absolute link into the repository rather than a 404.

    Rewriting here rather than editing the markdown keeps one source of truth:
    the docs stay readable in the editor and in a pull request.
    """
    by_source = {page.source.name: page.slug for page in PAGES}

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        target, _, fragment = href.partition("#")
        suffix = f"#{fragment}" if fragment else ""
        filename = target.rsplit("/", 1)[-1]

        slug = by_source.get(filename)
        if slug is not None:
            return f'href="{slug}.html{suffix}"'

        # No page for it: point at the file in the repository.
        clean = target.lstrip("./").removeprefix("../")
        return f'href="{GITHUB}/blob/main/{clean}{suffix}"'

    return re.sub(r'href="([^"]+)"', replace, body)


def strip_leading_h1(body: str) -> tuple[str, str | None]:
    """Move the document's own <h1> into the page header."""
    match = re.match(r"\s*<h1[^>]*>(.*?)</h1>", body, re.DOTALL)
    if not match:
        return body, None
    title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return body[match.end() :], title


def brand_mark(root: str) -> str:
    """The monogram, as an <img> so one file is the single source of it."""
    src = f"{root}assets/{asset_url('mark.svg')}"
    return f'<img class="mark" src="{src}" alt="" width="39" height="26">'


def heading_toc(body: str, label: str = "On this page") -> str:
    """An "on this page" list built from the h2s the renderer already numbered.

    Only h2: a table of contents that mirrors every heading is the page again,
    and nobody reads a page twice.
    """
    found = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body, re.DOTALL)
    if len(found) < 3:
        return ""
    items = "".join(
        f'<li><a href="#{ident}">{re.sub(r"<[^>]+>", "", text).strip()}</a></li>'
        for ident, text in found
    )
    return (
        f'    <nav class="toc" aria-label="{label}">'
        f'<p class="toc-title">{label}</p>'
        f"<ul>{items}</ul></nav>" + chr(10)
    )


#: Everything the page chrome says, per language. The article bodies come from
#: the markdown; these are the words around them, and a Spanish page that says
#: "Previous" and "source" is a Spanish page that was never read by anyone.
CHROME: dict[str, dict[str, str]] = {
    "en": {
        "previous": "Previous",
        "next": "Next",
        "version": "Version",
        "source": "source",
        "changelog": "changelog",
        "nav": "Documentation",
        "docs_title": "Documentation",
        "docs_lede": (
            "Every page, grouped the way the sidebar groups them. Start at the top, "
            "or jump to the one you already half-remember."
        ),
    },
    "es": {
        "previous": "Anterior",
        "next": "Siguiente",
        "version": "Versión",
        "source": "código",
        "changelog": "cambios",
        "nav": "Documentación",
        "docs_title": "Documentación",
        "docs_lede": (
            "Todas las páginas, agrupadas como en el menú lateral. Empieza "
            "arriba, o salta a la que ya recuerdas a medias."
        ),
    },
}


def pager(active: str, lang: str = "en") -> str:
    """Previous and next in reading order, in the page's own language.

    The sidebar says where everything is; this says where to go next, which is
    the question someone finishing a page actually has. Links stay in the same
    directory, so a Spanish reader is handed the next Spanish page.
    """
    chrome = CHROME[lang]
    order = [page for page in PAGES if page.slug not in ("changelog",)]
    index = next((i for i, page in enumerate(order) if page.slug == active), None)
    if index is None:
        return ""

    previous = order[index - 1] if index > 0 else None
    following = order[index + 1] if index + 1 < len(order) else None
    if previous is None and following is None:
        return ""

    parts = [f'    <nav class="pager" aria-label="{chrome["previous"]} / {chrome["next"]}">']
    if previous is not None:
        parts.append(
            f'      <a class="prev" href="{previous.slug}.html">'
            f"<small>{chrome['previous']}</small>{html.escape(page_title(previous, lang))}</a>"
        )
    if following is not None:
        parts.append(
            f'      <a class="next" href="{following.slug}.html">'
            f"<small>{chrome['next']}</small>{html.escape(page_title(following, lang))}</a>"
        )
    parts.append("    </nav>")
    return chr(10).join(parts) + chr(10)


# Vanilla, inline, and small enough to read. The site has no build step and
# this is not the place to start one. The labels follow <html lang>.
COPY_SCRIPT = """<script>
(function () {
  var es = document.documentElement.lang === "es";
  var label = es ? "copiar" : "copy";
  var done = es ? "copiado" : "copied";
  document.querySelectorAll("pre").forEach(function (pre) {
    var wrap = document.createElement("div");
    wrap.className = "snippet" + (pre.classList.contains("terminal") ? " terminal-wrap" : "");
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);

    var button = document.createElement("button");
    button.className = "copy";
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", function () {
      var text = pre.innerText.replace(/^\\$ /gm, "");
      navigator.clipboard.writeText(text).then(function () {
        button.textContent = done;
        setTimeout(function () { button.textContent = label; }, 1200);
      });
    });
    wrap.appendChild(button);
  });
})();
</script>"""


#: Plus Jakarta Sans for the text and the display type, JetBrains Mono for
#: code and for every label that is a label rather than a sentence.
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=JetBrains+Mono:wght@400;500;600;700;800&amp;"
    'family=Plus+Jakarta+Sans:wght@400;500;600;700;800;900&amp;display=swap">'
)


def release_version() -> str:
    """The package version this documentation describes, read from the source.

    Not ``--version``: that is the directory the site is published under --
    ``latest``, or ``v0.3`` -- and printing it where a reader expects a release
    number is how every page came to say "JFastFramework latest". Read, not
    imported, so building the site needs nothing the framework depends on.
    """
    source = (REPO / "src" / "jfastframework" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', source, re.MULTILINE)
    return match.group(1) if match else "unknown"


def version_label(channel: str, release: str) -> str:
    """What the picker shows: the channel, and the release it currently is."""
    return f"{channel} · {release}" if channel == "latest" else channel


def layout(
    *,
    title: str,
    body: str,
    version: str,
    versions: list[str],
    active: str,
    release: str,
    depth: int = 0,
    hero: str = "",
    wide: bool = False,
    lang: str = "en",
) -> str:
    # Assets live at the site root; pages live beside each other. A Spanish page
    # is one directory down, so it reaches the stylesheet through ../ and its
    # neighbours without it -- mixing the two up is how every Spanish sidebar
    # link used to land on the English page.
    assets = "../" * depth
    chrome = DOC_CHROME[lang]
    words = CHROME[lang]
    other_lang = "es" if lang == "en" else "en"
    other_page = f"es/{active}.html" if lang == "en" else f"../{active}.html"

    # Built outside the f-string: nesting the same quote character inside one
    # is a syntax error before Python 3.12, and CI runs 3.11.
    def nav_link(page: Page) -> str:
        active_class = ' class="active"' if page.slug == active else ""
        glyph = icon(PAGE_ICON.get(page.slug, "book"))
        return (
            f'        <a href="{page.slug}.html"{active_class}>'
            f"{glyph}<span>{html.escape(page_title(page, lang))}</span></a>"
        )

    by_slug = {page.slug: page for page in PAGES}
    grouped: list[str] = []
    placed: set[str] = set()
    for heading, slugs in SECTIONS:
        links = [nav_link(by_slug[slug]) for slug in slugs if slug in by_slug]
        if not links:
            continue
        placed.update(slugs)
        label = heading if lang == "en" else SECTION_ES.get(heading, heading)
        grouped.append(f"        <h4>{html.escape(label)}</h4>")
        grouped.extend(links)
    # Anything a section forgot still appears, rather than vanishing quietly.
    leftovers = [nav_link(page) for page in PAGES if page.slug not in placed]
    if leftovers:
        grouped.append("        <h4>" + ("More" if lang == "en" else "Más") + "</h4>")
        grouped.extend(leftovers)
    nav_items = "\n".join(grouped)

    def version_option(name: str) -> str:
        selected = " selected" if name == version else ""
        return f'          <option value="{name}"{selected}>{version_label(name, release)}</option>'

    options = "\n".join(version_option(v) for v in versions)
    # The same page under another version: up out of this version's directory
    # (and out of es/ first, on a Spanish page), then back down.
    up = "../" * (depth + 1)
    down = "es/" if lang == "es" else ""
    switch = f"location.href='{up}'+this.value+'/{down}'+location.pathname.split('/').pop()"

    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · JFastFramework</title>
<meta name="description" content="{html.escape(DESCRIPTIONS[lang])}">
<meta name="theme-color" content="#030305">
{FONTS}
<link rel="stylesheet" href="{assets}assets/{asset_url("site.css")}">
<link rel="icon" href="{assets}assets/{asset_url("favicon.svg")}">
{THEME_BOOT}
</head>
<body class="docs">
<a class="skip" href="#main">{chrome["skip"]}</a>

<header class="topbar">
  <a class="brand" href="index.html">
    {brand_mark(assets)}
    <span class="wordmark"><b>jfast</b>framework</span>
  </a>
  <div class="topbar-right">
    <label class="version-picker">
      <span class="sr-only">{words["version"]}</span>
      <select onchange="{switch}">
{options}
      </select>
    </label>
    <a class="ghost" href="docs.html">{chrome["docs"]}</a>
    <a class="ghost lang" href="{other_page}" hreflang="{other_lang}">{chrome["other"]}</a>
    <a class="ghost ext" href="{PYPI}">PyPI</a>
    <a class="ghost ext" href="{GITHUB}">GitHub</a>
{theme_toggle(lang)}
  </div>
</header>

{ICON_SPRITE}

<div class="shell{" wide" if wide else ""}">
  <nav class="sidebar" aria-label="{words["nav"]}">
{nav_items}
  </nav>

  <main id="main" class="content">
{hero}
{heading_toc(body, chrome["toc"]) if not wide else ""}
{body}
{pager(active, lang) if not wide else ""}
    <footer class="page-footer">
      <p>JFastFramework {html.escape(release)} · MIT ·
        <a href="{PYPI}">PyPI</a> ·
        <a href="{GITHUB}">{words["source"]}</a> ·
        <a href="changelog.html">{words["changelog"]}</a>
      </p>
    </footer>
  </main>
</div>
{COPY_SCRIPT}
{THEME_SCRIPT}
{REVEAL_SCRIPT}
{LANG_SCRIPT}
</body>
</html>
"""


LANDING_CHROME: dict[str, dict[str, str]] = {
    "en": {
        "lang": "en",
        "skip": "Skip to content",
        "docs": "Docs",
        "other_label": "Español",
        "documentation": "Documentation",
        "source": "Source",
        "maturity": "Maturity",
        "changelog": "Changelog",
        "tagline": "The backend nobody wants to write twice.",
    },
    "es": {
        "lang": "es",
        "skip": "Saltar al contenido",
        "docs": "Documentación",
        "other_label": "English",
        "documentation": "Documentación",
        "source": "Código",
        "maturity": "Madurez",
        "changelog": "Cambios",
        "tagline": "El backend que nadie quiere escribir dos veces.",
    },
}


#: three.js from cdnjs, pinned and hashed: a CDN that changes the file under a
#: fixed URL gets a blocked script and the still fallback, not somebody else's
#: code running on this page. The hash is cdnjs's own for r128.
THREE_JS = (
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"\n'
    '        integrity="sha512-dLxUelApnYxpLt6K2iomGngnHO83iUvZytA3YjDUCjT0HDOHKXnVYdf3hU4'
    'JjM8uEhxf9nD1/ey98U3t2vZ0qQ=="\n'
    '        crossorigin="anonymous" referrerpolicy="no-referrer"></script>'
)

#: A few degrees of tilt under the pointer. Without it the cards stay flat,
#: which is also what a reader who asked for less motion gets.
TILT_SCRIPT = """<script>
(function () {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (!window.matchMedia("(hover: hover)").matches) return;
  document.querySelectorAll("[data-tilt]").forEach(function (card) {
    card.addEventListener("pointermove", function (e) {
      var r = card.getBoundingClientRect();
      var rx = ((e.clientY - r.top) / r.height - 0.5) * -6;
      var ry = ((e.clientX - r.left) / r.width - 0.5) * 6;
      card.style.transform = "perspective(1000px) rotateX(" + rx + "deg) rotateY(" +
        ry + "deg) scale3d(1.012,1.012,1.012)";
    });
    card.addEventListener("pointerleave", function () {
      card.style.transform = "";
    });
  });
})();
</script>"""


def landing_layout(*, description: str, release: str, body: str, lang: str) -> str:
    """The standalone front page, in one language.

    Deliberately not `layout()`. No sidebar, because there is nothing to
    navigate yet; no version picker, because somebody who has not installed it
    does not have a version. What is left is the argument and one way in.

    The Spanish page lives one directory down: assets need `../`, and every page
    it links to is the Spanish page beside it.
    """
    chrome = LANDING_CHROME[lang]
    assets = "../" if lang != "en" else ""
    other = "es" if lang == "en" else "en"
    other_href = "es/index.html" if lang == "en" else "../index.html"

    return f"""<!doctype html>
<html lang="{chrome["lang"]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JFastFramework · {html.escape(description)}</title>
<meta name="description" content="{html.escape(description)}">
<meta name="theme-color" content="#030305">
<link rel="alternate" hreflang="en" href="{"../index.html" if lang != "en" else "index.html"}">
<link rel="alternate" hreflang="es" href="{"index.html" if lang != "en" else "es/index.html"}">
{FONTS}
<link rel="stylesheet" href="{assets}assets/{asset_url("site.css")}">
<link rel="icon" href="{assets}assets/{asset_url("favicon.svg")}">
{THEME_BOOT}
</head>
<body class="landing">
<canvas id="liquid" class="liquid-canvas" aria-hidden="true"></canvas>
<div class="liquid-vignette" aria-hidden="true"></div>
<div id="liquid-glow" class="liquid-glow" aria-hidden="true"></div>

<a class="skip" href="#main">{chrome["skip"]}</a>

<header class="topbar">
  <a class="brand" href="index.html">
    {brand_mark(assets)}
    <span class="wordmark"><b>jfast</b>framework</span>
  </a>
  <div class="topbar-right">
    <a class="ghost" href="docs.html">{chrome["docs"]}</a>
    <a class="ghost lang" href="{other_href}" hreflang="{other}">{chrome["other_label"]}</a>
    <a class="ghost ext" href="{PYPI}">PyPI</a>
    <a class="ghost ext" href="{GITHUB}">GitHub</a>
{theme_toggle(lang)}
  </div>
</header>

{ICON_SPRITE}

<main id="main" class="landing-main">
{body}
</main>

<footer class="landing-footer">
  <p><b>JFastFramework</b> {html.escape(release)} · MIT · {chrome["tagline"]}</p>
  <nav>
    <a href="docs.html">{chrome["documentation"]}</a>
    <a href="status.html">{chrome["maturity"]}</a>
    <a href="changelog.html">{chrome["changelog"]}</a>
    <a href="{PYPI}">PyPI</a>
    <a href="{GITHUB}">{chrome["source"]}</a>
  </nav>
</footer>
{COPY_SCRIPT}
{THEME_SCRIPT}
{REVEAL_SCRIPT}
{LANG_SCRIPT}
{TILT_SCRIPT}
{THREE_JS}
<script src="{assets}assets/{asset_url("liquid.js")}"></script>
</body>
</html>
"""


#: One real violation, copied from what `jfast contracts check` prints. Shown
#: rather than described, because the second line -- the one that names the
#: file to move the code to -- is the whole argument, and paraphrasing it loses
#: exactly that.
CHECK_SAMPLE = (
    '<span class="c">$</span> jfast contracts check\n'
    "\n"
    "modules/payment/service.py:41: cross-module: module 'payment' imports module 'invoice'\n"
    '  <span class="dim">(two modules that need the same thing should share it:'
    " move it to shared/enums.py)</span>\n"
    "\n"
    "modules/billing/service.py:88: blocking-call: time.sleep stalls the event loop\n"
    '  <span class="dim">(every request on this worker waits; use'
    " await asyncio.sleep)</span>"
)


#: Reveal-on-scroll. The hiding class is added by this script, so a reader with
#: JavaScript off -- or a crawler -- gets the finished page rather than a blank
#: one. Sections already on screen at load are shown immediately, which stops
#: the fold animating in after the reader is already looking at it.
REVEAL_SCRIPT = """<script>
(function () {
  var targets = [].slice.call(document.querySelectorAll(".reveal"));
  if (!targets.length) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  document.documentElement.classList.add("js-reveal");

  var queued = false;

  function sweep() {
    queued = false;
    var limit = window.innerHeight * 0.92;
    targets = targets.filter(function (el) {
      if (el.getBoundingClientRect().top > limit) return true;
      el.classList.add("shown");
      return false;
    });
    if (!targets.length) {
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", request);
    }
  }

  function request() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(sweep);
  }

  window.addEventListener("scroll", request, { passive: true });
  window.addEventListener("resize", request, { passive: true });
  sweep();
})();
</script>"""


#: What a generated project actually runs. Names rather than logos: a logo
#: strip needs twelve images and permission to use them, and the names are what
#: somebody is scanning for anyway.
STACK = (
    "FastAPI",
    "PostgreSQL",
    "pgvector",
    "Redis",
    "SQLAlchemy",
    "Alembic",
    "Vue 3",
    "React",
    "Tailwind",
    "Docker",
    "Caddy",
)


def project_numbers() -> dict[str, int]:
    """The figures the landing quotes, counted from the repository at build time.

    They used to be typed into the copy, and the copy said 17 plugins and 509
    tests long after both had moved on. A number on the front page is a claim;
    this keeps it one that was true when the page was built.
    """
    builtin = REPO / "src" / "jfastframework" / "plugins" / "builtin"
    plugins = len([p for p in builtin.glob("*.py") if p.name != "__init__.py"])

    tests = 0
    for path in (REPO / "tests").rglob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        tests += len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.MULTILINE))

    scaffold = (REPO / "src" / "jfastframework" / "cli" / "scaffold.py").read_text(encoding="utf-8")
    found = re.search(r"MODULE_LAYOUTS = \(([^)]*)\)", scaffold)
    layouts = len(re.findall(r'"\w+"', found.group(1))) if found else 0

    return {"plugins": plugins, "tests": tests, "layouts": layouts}


def landing_body(release: str, lang: str) -> str:
    """The landing's sections, in one language.

    One function for both, because the alternative -- two hand-written pages --
    means the Spanish one is a snapshot of what the English one said in August
    and nobody notices for a year. Every href to a page is relative to the
    landing's own directory, so the Spanish landing leads to Spanish pages.
    """
    t = LANDING_COPY[lang]
    numbers = project_numbers()
    assets = "" if lang == "en" else "../"

    prompt = '<span class="c">$</span>'
    ok = '<span class="ok">✓</span>'
    start = "\n".join(
        [
            f"{prompt} pip install jfastframework",
            f"{prompt} jfast start shop",
            "",
            f'{ok} shop/        <span class="dim">FastAPI · Postgres · Redis</span>',
            f'{ok} shop-web/    <span class="dim">Vue 3 · Vite</span>',
            f'{ok} compose      <span class="dim">{t["term_compose"]}</span>',
            f'{ok} Caddyfile    <span class="dim">TLS · {t["term_proxy"]}</span>',
        ]
    )

    stack = "\n".join(f'        <span class="chip">{name}</span>' for name in STACK)

    def metric(value: str, label: str) -> str:
        return f'        <div class="metric"><b>{value}</b><span>{label}</span></div>'

    metrics = "\n".join(
        [
            metric("1", t["m_command"]),
            metric(f"{numbers['plugins']}", t["m_plugins"]),
            metric(f"{numbers['layouts']}", t["m_layouts"]),
            metric(f"{numbers['tests']:,}", t["m_tests"]),
        ]
    )

    def kicker(name: str, text: str) -> str:
        return f'      <p class="section-kicker">{icon(name)}<span>{text}</span></p>'

    def idea(key: str, number: str, span: str = "") -> str:
        cls = f' class="{span}"' if span else ""
        return f"""        <article{cls} data-tilt>
          <span class="card-icon">{icon(t[key + "_i"])}</span>
          <span class="bento-num">{number}</span>
          <h3>{t[key + "_h"]}</h3>
          <p>{t[key + "_p"]}</p>
          <p class="cost"><span class="cost-label">{t["cost_label"]}</span>{t[key + "_c"]}</p>
        </article>"""

    def guard(key: str) -> str:
        return f"""        <article data-tilt>
          <span class="card-icon">{icon(t[key + "_i"])}</span>
          <h3>{t[key + "_h"]}</h3>
          <p>{t[key + "_p"]}</p>
        </article>"""

    def audience(key: str, step: str) -> str:
        return f"""        <article class="span-3" data-tilt>
          <b class="step">{step}</b>
          <h3>{t[key + "_h"]}</h3>
          <p>{t[key + "_p"]}</p>
        </article>"""

    def link_card(key: str, href: str) -> str:
        return f"""        <a class="link-card" href="{href}" data-tilt>
          <span class="card-icon">{icon(t[key + "_i"])}</span>
          <h3>{t[key + "_h"]}</h3>
          <p>{t[key + "_p"]}</p>
          <span class="go">{t["read"]} <span class="arrow">&rarr;</span></span>
        </a>"""

    return f"""    <section class="hero">
      <div class="hero-grid">
        <div class="hero-copy">
          <p class="pill-badge glass"><span class="pulse" aria-hidden="true"></span>
            v{html.escape(release)} · {t["eyebrow"]}</p>
          <h1 class="display">
            <span>{t["h1_a"]}</span>
            <span class="faded">{t["h1_b"]}</span>
            <span>{t["h1_c"]}</span>
          </h1>
          <p class="lede">{t["lede"]}</p>
          <div class="cta">
            <a class="button primary" href="local-setup.html" data-pulse>
              {icon("play")}<span>{t["cta_primary"]}</span></a>
            <a class="button" href="modules.html">
              <span>{t["cta_secondary"]}</span><span class="arrow">&rarr;</span></a>
            <a class="cta-link" href="architecture.html">{t["cta_link"]} &rarr;</a>
          </div>
        </div>

        <div class="hero-mark" aria-hidden="true">
          <div class="mark-lockup">
            <img src="{assets}assets/{asset_url("mark.svg")}" width="132" height="96" alt="">
            <span class="wordmark"><b>jfast</b>framework</span>
          </div>
        </div>
      </div>

      <div class="start-terminal">
        <pre class="terminal"><code>{start}</code></pre>
      </div>

      <div class="metrics">
{metrics}
      </div>
      <p class="dim-note">{t["honest"]}</p>

      <div class="stack">
        <p class="stack-label">{t["stack_label"]}</p>
{stack}
      </div>
    </section>

    <section class="argument reveal">
{kicker("compare", t["before_kicker"])}
      <h2>{t["before_title"]}</h2>
      <div class="bento">
        <div class="compare-col before span-5" data-tilt>
          <p class="compare-label">{t["before_label"]}</p>
          <p>{t["before"]}</p>
        </div>
        <div class="compare-col after span-7" data-tilt>
          <p class="compare-label">{t["after_label"]}</p>
          <p>{t["after"]}</p>
        </div>
      </div>
    </section>

    <section class="argument reveal">
{kicker("layers", t["pillars_kicker"])}
      <h2>{t["pillars_title"]}</h2>
      <div class="bento argument-grid">
{idea("p1", "01")}
{idea("p2", "02")}
{idea("p3", "03")}
      </div>
    </section>

    <section class="argument reveal">
{kicker("shield", t["guard_kicker"])}
      <h2>{t["guard_title"]}</h2>
      <p class="lede">{t["guard_lede"]}</p>
      <pre class="terminal check"><code>{CHECK_SAMPLE}</code></pre>
      <div class="bento argument-grid">
{guard("g1")}
{guard("g2")}
{guard("g3")}
      </div>
    </section>

    <section class="argument reveal">
{kicker("users", t["audience_kicker"])}
      <h2>{t["audience_title"]}</h2>
      <div class="bento audience-grid">
{audience("a1", "01")}
{audience("a2", "02")}
{audience("a3", "03")}
{audience("a4", "04")}
      </div>
    </section>

    <section class="argument reveal">
{kicker("book", t["start_kicker"])}
      <h2>{t["start_title"]}</h2>
      <div class="bento">
{link_card("s1", "local-setup.html")}
{link_card("s2", "modules.html")}
{link_card("s3", "transactions.html")}
      </div>
      <p class="dim-note"><a href="docs.html">{t["all_docs"]} &rarr;</a></p>
    </section>

    <section class="cta-panel reveal">
      <h2>{t["final_title"]}</h2>
      <p>{t["final_p"]}</p>
      <div class="cta">
        <a class="button ruby" href="local-setup.html" data-pulse>
          <span>{t["cta_primary"]}</span><span class="arrow">&rarr;</span></a>
        <a class="button" href="#main">
          <span>{t["back_top"]}</span><span class="arrow">&uarr;</span></a>
      </div>
    </section>
"""


#: The one-line description, per language. Used in <title> and in the meta
#: description, which is the text a search result shows.
DESCRIPTIONS: dict[str, str] = {
    "en": DESCRIPTION,
    "es": (
        "Un framework FastAPI de plugins para microservicios, pensado para que "
        "escribas la lógica de negocio y no la plomería."
    ),
}


def build_landing(release: str, lang: str = "en") -> str:
    """The front page, in one language."""
    return landing_layout(
        description=DESCRIPTIONS[lang],
        release=release,
        body=landing_body(release, lang),
        lang=lang,
    )


#: Everything the landing says, in both languages. One structure, two
#: dictionaries: a translated page that drifts from the original is worse than
#: no translation, and the only way to keep two pages in step is to make them
#: the same page with different strings.
LANDING_COPY: dict[str, dict[str, str]] = {
    "en": {
        "eyebrow": "alpha",
        "h1_a": "Stop rebuilding",
        "h1_b": "the same",
        "h1_c": "backend.",
        "lede": (
            "One command: FastAPI, PostgreSQL, Redis, jobs, a Vue frontend and "
            "a reverse proxy. Wired together, containerised, migrating on boot. "
            "What you write is the part only you know."
        ),
        "cta_primary": "Start in one command",
        "cta_secondary": "See what it generates",
        "cta_link": "Why it is built this way",
        "term_compose": "one container per resource",
        "term_proxy": "one hostname",
        "m_command": "command to a running stack",
        "m_plugins": "plugins, each removable",
        "m_layouts": "architectures per module",
        "m_tests": "test functions",
        "honest": (
            'Alpha. <a href="status.html">The maturity table</a> says which '
            "parts are tested against real infrastructure and which are not."
        ),
        "stack_label": "It generates",
        "before_kicker": "Before and after",
        "before_title": "The same feature, twice.",
        "before_label": "By hand",
        "after_label": "Here",
        "before": (
            "Wire the session. Decide where the query goes. Add a model, a "
            "router, a migration, a compose service. Find out in review that "
            "someone queried the database from a handler."
        ),
        "after": (
            "<code>jfast new module invoice</code> &mdash; router, service, "
            "repository, schemas and a test, registered, under a contract that "
            "fails the build if the handler ever touches the database."
        ),
        "pillars_kicker": "The design",
        "pillars_title": "Three ideas, and what each one costs.",
        "cost_label": "Trade-off",
        "p1_i": "zap",
        "p1_h": "Business logic first",
        "p1_p": "The plumbing is a plugin list. The diff on a branch is the feature.",
        "p1_c": "You inherit opinions, and they are enforced.",
        "p2_i": "bot",
        "p2_h": "Agents cannot drift",
        "p2_p": "Every rule is machine-checked, and every violation names the fix.",
        "p2_c": "Writing the contract is work.",
        "p3_i": "split",
        "p3_h": "Split when you know the seams",
        "p3_p": "A modular monolith today; one command promotes a module to a service.",
        "p3_c": "A monolith on day one.",
        "guard_kicker": "Guardrails",
        "guard_title": "Vibe-code with a seatbelt.",
        "guard_lede": (
            "Generating code fast is solved. Keeping it coherent after the fourth feature is not."
        ),
        "g1_i": "blocks",
        "g1_h": "Modules stay separate",
        "g1_p": "Two that import each other can never be split again.",
        "g2_i": "gauge",
        "g2_h": "No blocking calls",
        "g2_p": "One <code>time.sleep</code> stalls every request on the worker.",
        "g3_i": "database",
        "g3_h": "A 201 means the row exists",
        "g3_p": "The session commits before the response, and a race is a 409, not a lost write.",
        "audience_kicker": "Who it is for",
        "audience_title": "Whoever is writing it.",
        "a1_h": "First backend",
        "a1_p": "The generated module is a worked example you can read in five minutes.",
        "a2_h": "Shipping features",
        "a2_p": "Skip the plumbing; the checker catches the shortcut you took at 6pm.",
        "a3_h": "Leading a team",
        "a3_p": "The review is about the feature, because the structure is CI's job.",
        "a4_h": "Driving agents",
        "a4_p": "Rules an agent reads first and cannot quietly break.",
        "start_kicker": "Documentation",
        "start_title": "Start reading.",
        "read": "Read",
        "s1_i": "rocket",
        "s1_h": "Run it locally",
        "s1_p": "One command to a running stack. Fifteen minutes.",
        "s2_i": "layers",
        "s2_h": "Modules and layouts",
        "s2_p": "Four architectures, and how to pick one.",
        "s3_i": "database",
        "s3_h": "Transactions",
        "s3_p": "When a write commits, and what it fails with.",
        "all_docs": "All documentation",
        "final_title": "Write the rules. Not the plumbing.",
        "final_p": (
            "Generate the stack in one command, and let CI keep it the shape you "
            "chose while a team &mdash; or an agent &mdash; works on it."
        ),
        "back_top": "Back to top",
    },
    "es": {
        "eyebrow": "alpha",
        "h1_a": "Deja de reconstruir",
        "h1_b": "el mismo",
        "h1_c": "backend.",
        "lede": (
            "Un comando: FastAPI, PostgreSQL, Redis, jobs, un frontend en Vue y "
            "un reverse proxy. Conectados, en contenedores, migrando al arrancar. "
            "Lo que escribes es la parte que solo tú conoces."
        ),
        "cta_primary": "Empieza con un comando",
        "cta_secondary": "Mira qué genera",
        "cta_link": "Por qué está hecho así",
        "term_compose": "un contenedor por recurso",
        "term_proxy": "un solo host",
        "m_command": "comando y el stack corre",
        "m_plugins": "plugins, todos removibles",
        "m_layouts": "arquitecturas por módulo",
        "m_tests": "funciones de test",
        "honest": (
            'Alpha. <a href="status.html">La tabla de madurez</a> dice qué '
            "partes están probadas contra infraestructura real y cuáles no."
        ),
        "stack_label": "Genera",
        "before_kicker": "Antes y después",
        "before_title": "La misma función, dos veces.",
        "before_label": "A mano",
        "after_label": "Aquí",
        "before": (
            "Conectar la sesión. Decidir dónde va la query. Agregar "
            "modelo, router, migración y servicio de compose. Enterarte en "
            "el review de que alguien consultó la base desde un handler."
        ),
        "after": (
            "<code>jfast new module invoice</code> &mdash; router, service, "
            "repository, schemas y un test, registrados, bajo un contrato que "
            "rompe el build si el handler toca la base."
        ),
        "pillars_kicker": "El diseño",
        "pillars_title": "Tres ideas, y lo que cuesta cada una.",
        "cost_label": "Costo",
        "p1_i": "zap",
        "p1_h": "La lógica de negocio primero",
        "p1_p": ("La plomería es una lista de plugins. El diff de la rama es la funcionalidad."),
        "p1_c": "Heredas opiniones, y se exigen.",
        "p2_i": "bot",
        "p2_h": "Los agentes no se desvían",
        "p2_p": ("Cada regla la verifica una máquina, y cada violación dice el arreglo."),
        "p2_c": "Escribir el contrato es trabajo.",
        "p3_i": "split",
        "p3_h": "Separa cuando sepas dónde",
        "p3_p": "Hoy un monolito modular; un comando convierte un módulo en servicio.",
        "p3_c": "Un monolito el día uno.",
        "guard_kicker": "Barandales",
        "guard_title": "Vibe-coding con cinturón.",
        "guard_lede": (
            "Generar código rápido ya está resuelto. "
            "Mantenerlo coherente tras la cuarta funcionalidad, no."
        ),
        "g1_i": "blocks",
        "g1_h": "Los módulos siguen separados",
        "g1_p": "Dos que se importan entre sí ya no se pueden separar.",
        "g2_i": "gauge",
        "g2_h": "Nada bloqueante",
        "g2_p": "Un <code>time.sleep</code> frena todas las peticiones del worker.",
        "g3_i": "database",
        "g3_h": "Un 201 significa que la fila existe",
        "g3_p": (
            "La sesión confirma antes de responder, y una carrera es un 409, "
            "no una escritura perdida."
        ),
        "audience_kicker": "Para quién es",
        "audience_title": "Para quien lo escriba.",
        "a1_h": "Tu primer backend",
        "a1_p": "El módulo generado es un ejemplo que lees en cinco minutos.",
        "a2_h": "Sacando funcionalidades",
        "a2_p": "Te saltas la plomería; el checker atrapa el atajo de las 6pm.",
        "a3_h": "Liderando un equipo",
        "a3_p": "El review es sobre la funcionalidad; la estructura la revisa la CI.",
        "a4_h": "Dirigiendo agentes",
        "a4_p": "Reglas que la IA lee primero y no puede romper en silencio.",
        "start_kicker": "Documentación",
        "start_title": "Por dónde empezar.",
        "read": "Leer",
        "s1_i": "rocket",
        "s1_h": "Correrlo en local",
        "s1_p": "Un comando hasta el stack corriendo. Quince minutos.",
        "s2_i": "layers",
        "s2_h": "Módulos y arquitecturas",
        "s2_p": "Cuatro arquitecturas, y cómo elegir.",
        "s3_i": "database",
        "s3_h": "Transacciones",
        "s3_p": "Cuándo se confirma una escritura, y con qué falla.",
        "all_docs": "Toda la documentación",
        "final_title": "Escribe las reglas. No la plomería.",
        "final_p": (
            "Genera el stack con un comando, y deja que la CI lo mantenga con la "
            "forma que elegiste mientras un equipo &mdash; o un agente &mdash; trabaja en él."
        ),
        "back_top": "Volver arriba",
    },
}


def build_docs_home(
    version: str,
    versions: list[str],
    summaries: dict[str, str],
    release: str,
    lang: str = "en",
) -> str:
    """The documentation home: every page as a card, with the sidebar.

    Separate from the landing on purpose. Somebody who arrives here has already
    decided to read, so the sidebar is help rather than clutter -- and the
    landing is free to be a landing. The Spanish one is a Spanish page: its
    headings, its titles, and summaries taken from the Spanish text.
    """
    words = CHROME[lang]
    by_slug = {page.slug: page for page in PAGES}
    sections = []
    for heading, slugs in SECTIONS:
        cards = "\n".join(
            f"""        <a class="card" href="{slug}.html">
          <h3>{html.escape(page_title(by_slug[slug], lang))}</h3>
          <p>{html.escape(summaries.get(slug) or by_slug[slug].summary)}</p>
        </a>"""
            for slug in slugs
            if slug in by_slug
        )
        if not cards:
            continue
        label = heading if lang == "en" else SECTION_ES.get(heading, heading)
        sections.append(
            f"""    <section class="doc-section">
      <h2>{html.escape(label)}</h2>
      <div class="cards">
{cards}
      </div>
    </section>"""
        )

    body = "\n".join(sections) + "\n"
    return layout(
        title=words["docs_title"],
        body=body,
        version=version,
        versions=versions,
        active="docs",
        release=release,
        depth=0 if lang == "en" else 1,
        hero=(
            f'    <header class="doc-head"><h1>{words["docs_title"]}</h1>'
            f"<p>{words['docs_lede']}</p></header>\n"
        ),
        wide=True,
        lang=lang,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        default="latest",
        help="The directory this build is published under: latest, or a minor series.",
    )
    parser.add_argument("--output", type=Path, default=REPO / "site" / "latest")
    parser.add_argument(
        "--versions",
        default="",
        help="Comma-separated versions for the picker. Defaults to this one.",
    )
    args = parser.parse_args()

    versions = [v.strip() for v in args.versions.split(",") if v.strip()] or [args.version]
    if args.version not in versions:
        versions.insert(0, args.version)
    release = release_version()

    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSETS, output / "assets", dirs_exist_ok=True)

    summaries: dict[str, dict[str, str]] = {"en": {}, "es": {}}
    spanish = output / "es"
    spanish.mkdir(exist_ok=True)
    translated_count = 0

    for lang in ("en", "es"):
        target = output if lang == "en" else spanish
        depth = 0 if lang == "en" else 1

        for page in PAGES:
            if not page.source.is_file():
                if lang == "en":
                    print(f"  skipped  {page.slug} ({page.source.name} not found)")
                continue

            source, is_translated = page_source(page, lang)
            body, summary = render_markdown(source.read_text(encoding="utf-8"))
            body = rewrite_links(body)
            body, own_title = strip_leading_h1(body)
            if lang == "en" or is_translated:
                summaries[lang][page.slug] = summary
            if lang != "en" and is_translated:
                translated_count += 1

            heading = html.escape(own_title or page_title(page, lang))
            hero = f'    <header class="doc-head"><h1>{heading}</h1></header>\n'
            if not is_translated:
                hero += UNTRANSLATED_NOTICE

            (target / f"{page.slug}.html").write_text(
                layout(
                    title=page_title(page, lang),
                    body=body,
                    version=args.version,
                    versions=versions,
                    active=page.slug,
                    release=release,
                    hero=hero,
                    depth=depth,
                    lang=lang,
                ),
                encoding="utf-8",
            )
        print(f"  built    {len(PAGES)} pages ({lang})")

    print(f"  translated {translated_count}/{len(PAGES)} pages into Spanish")

    (output / "docs.html").write_text(
        build_docs_home(args.version, versions, summaries["en"], release), encoding="utf-8"
    )
    (spanish / "docs.html").write_text(
        build_docs_home(
            args.version,
            versions,
            {**summaries["en"], **summaries["es"]},
            release,
            lang="es",
        ),
        encoding="utf-8",
    )
    print("  built    docs.html (en, es)")

    (output / "index.html").write_text(build_landing(release), encoding="utf-8")
    print("  built    index.html")

    # Spanish lives one directory down rather than as index.es.html: a folder
    # is what a reader can bookmark and what a CDN can serve as a default.
    (spanish / "index.html").write_text(build_landing(release, "es"), encoding="utf-8")
    print("  built    es/index.html")
    (output / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    print(f"\nSite written to {output} (release {release})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
