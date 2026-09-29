#!/usr/bin/env python3
"""Validate a built documentation site.

    python docs-site/check.py site/latest

A docs site fails quietly: a broken link or a half-rendered template still
looks like a page. These checks are the difference between "it built" and
"it works".
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urlsplit


class Collector(HTMLParser):
    """Gathers hrefs, srcs and heading ids, and checks tags balance."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        # Links that say which language they lead to: the switch, and nothing
        # else, is allowed to cross from one language to the other.
        self.switches: set[str] = set()
        self.assets: list[str] = []
        self.ids: set[str] = set()
        self.titles: list[str] = []
        self._in_title = False
        self.h1_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.ids.add(identifier)
        if tag == "a" and (href := values.get("href")):
            self.links.append(href)
            if values.get("hreflang"):
                self.switches.add(href)
        if tag in ("link", "script", "img") and (src := values.get("href") or values.get("src")):
            self.assets.append(src)
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self.h1_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.titles.append(data.strip())


def check(site: Path) -> list[str]:
    problems: list[str] = []
    # Absolute, because links are resolved to absolute paths below and compared
    # with the page's folder: with CI's relative `site/latest/` the two never
    # matched, and every Spanish link read as leaving Spanish.
    site = site.resolve()
    # The Spanish pages too. They sit one directory down, and checking only the
    # top level is how es/docs.html shipped loading a stylesheet that is not
    # there, and every Spanish sidebar link shipped leading to English.
    pages = sorted(site.glob("*.html")) + sorted((site / "es").glob("*.html"))

    if not pages:
        return [f"{site}: no pages were built"]
    if not (site / "index.html").exists():
        problems.append("index.html is missing")
    if not (site / "assets" / "site.css").exists():
        problems.append("assets/site.css is missing")

    for page in pages:
        text = page.read_text(encoding="utf-8")
        name = page.relative_to(site).as_posix()
        spanish = page.parent.name == "es"

        # A template that failed to substitute renders as literal braces. It
        # looks like a page and reads like a bug report.
        #
        # Code and prose *about* templating legitimately contains those same
        # braces -- the changelog discusses the delimiters -- so check only
        # outside <code> and <pre>.
        prose = re.sub(r"<(pre|code)\b.*?</\1>", "", text, flags=re.DOTALL)
        for artifact in ("{{", "}}", "{%", "[[ ", " ]]"):
            if artifact in prose:
                problems.append(f"{name}: unrendered template artifact {artifact!r}")

        collector = Collector()
        collector.feed(text)

        if not collector.titles or not collector.titles[0]:
            problems.append(f"{name}: empty <title>")
        if collector.h1_count != 1:
            problems.append(f"{name}: {collector.h1_count} <h1> elements, expected exactly 1")

        for href in collector.links:
            target, fragment = urldefrag(href)
            if not target or target.startswith(("http://", "https://", "mailto:", "data:")):
                continue
            # `site.css?v=<hash>` names the file site.css; the query only
            # busts caches.
            target = urlsplit(target).path
            if target.startswith("/"):
                problems.append(f"{name}: absolute link {href!r} breaks under a versioned path")
                continue
            resolved = (page.parent / target).resolve()
            crosses = resolved.suffix == ".html" and resolved.parent != page.parent
            if spanish and crosses and href not in collector.switches:
                problems.append(f"{name}: {href!r} leaves Spanish for another language")
            if not resolved.exists():
                problems.append(f"{name}: dead link {href!r}")
            elif fragment and resolved.suffix == ".html" and resolved != page:
                other = Collector()
                other.feed(resolved.read_text(encoding="utf-8"))
                if fragment not in other.ids:
                    problems.append(f"{name}: dead anchor {href!r}")

        for src in collector.assets:
            if src.startswith(("http://", "https://", "data:")):
                continue
            if not (page.parent / urlsplit(src).path).resolve().exists():
                problems.append(f"{name}: missing asset {src!r}")

        # A page in es/ that says it is English is read aloud in the wrong
        # accent and ranked for the wrong searches.
        declared = re.search(r'<html lang="([a-z]+)"', text)
        expected = "es" if spanish else "en"
        if not declared or declared.group(1) != expected:
            problems.append(f"{name}: <html lang> is not {expected!r}")

    css = (site / "assets" / "site.css").read_text(encoding="utf-8")
    # Every token the light theme re-points must exist on :root, or it is
    # undefined in the default theme -- dark -- for whatever only light set.
    root_block = re.search(r":root\s*\{(.*?)\}", css, re.DOTALL)
    light_block = re.search(r':root\[data-theme="light"\]\s*\{(.*?)\}', css, re.DOTALL)
    if root_block and light_block:
        default = set(re.findall(r"(--[\w-]+):", root_block.group(1)))
        light = set(re.findall(r"(--[\w-]+):", light_block.group(1)))
        for token in sorted(light - default):
            problems.append(f"site.css: {token} is only defined in the light theme")

    return problems


def main() -> int:
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "site/latest")
    problems = check(site)
    pages = len(list(site.glob("*.html"))) + len(list((site / "es").glob("*.html")))

    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s) in {pages} page(s)")
        return 1
    print(f"  OK    {pages} pages, links and assets resolve, tokens complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
