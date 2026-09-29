"""Rendering for `jfast inspect`, `jfast analyze` and `jfast graph`.

Kept apart from `main.py` for one reason: these are pure functions from a
`Project` to a string, so they are testable without a Typer runner, a
filesystem or a terminal. The commands in `main.py` are three lines each.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

from jfastframework.cli import ui
from jfastframework.project import (
    SEVERITY_ORDER,
    Finding,
    Module,
    Project,
    module_edges,
)

__all__ = [
    "GRAPH_FORMATS",
    "render_analysis",
    "render_graph",
    "render_module",
    "render_project",
    "severity_counts",
]

GRAPH_FORMATS = ("ascii", "mermaid", "dot", "json")


def _row(label: str, value: str) -> str:
    return f"  {label:<13}{value}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    """How many findings at each severity, worst first, zeroes omitted."""
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] += 1
    return {severity: count for severity, count in counts.items() if count}


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def render_project(project: Project, findings: list[Finding]) -> str:
    """One screen: what is in this project and whether it hangs together."""
    G = ui.G
    lines = [f"  {project.name} {project.version} ({project.env})", ""]

    if project.modules:
        lines.append(f"  modules ({len(project.modules)})")
        width = max(len(module.name) for module in project.modules)
        for module in project.modules:
            shape = "/".join(part for part in (module.layout, module.ui) if part) or "-"
            routes = ", ".join(module.route_prefixes) or "-"
            mark = "" if module.registered else f"  {G.cross} not in main.py"
            lines.append(f"    {module.name:<{width}}  {shape:<14}{routes:<16}{mark}")
    else:
        lines.append("  modules       none yet   (jfast new module <name>)")
    lines.append("")

    lines.append(_row("plugins", ", ".join(project.plugins) or "-"))
    if project.disabled:
        lines.append(_row("disabled", ", ".join(project.disabled)))
    lines.append(_row("shared", _plural(len(project.shared_files), "file")))
    lines.append(_row("migrations", str(project.migrations)))
    lines.append(_row("contract", "contracts.toml" if project.has_contract else "-"))
    lines.append(_row("frontend", project.frontend or "-"))

    lines.append("")
    if findings:
        counts = severity_counts(findings)
        summary = "  ".join(f"{severity} {count}" for severity, count in counts.items())
        lines.append(f"  {G.cross} {summary}   ->  jfast analyze")
    else:
        lines.append(f"  {G.tick} nothing to report")
    return "\n".join(lines)


def render_module(module: Module) -> str:
    """Everything known about one module."""
    G = ui.G
    lines = [f"  {module.name}", ""]
    lines.append(_row("path", module.path))
    lines.append(_row("layout", "/".join(p for p in (module.layout, module.ui) if p) or "unknown"))
    lines.append(_row("routes", ", ".join(module.route_prefixes) or "-"))
    lines.append(_row("registered", f"{G.tick} yes" if module.registered else f"{G.cross} no"))
    lines.append(_row("tests", f"{G.tick} yes" if module.has_tests else f"{G.cross} no"))
    lines.append(_row("readme", f"{G.tick} yes" if module.has_readme else f"{G.cross} no"))
    lines.append(_row("tables", ", ".join(module.tables) or "-"))
    lines.append(_row("imports", ", ".join(module.imports) or "no other module"))
    lines.append(_row("packages", ", ".join(module.external) or "-"))
    lines.append("")
    lines.append(f"  files ({len(module.files)})")
    lines.extend(f"    {name}" for name in module.files)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def render_analysis(findings: list[Finding]) -> str:
    """Findings worst first, each with the fix on its own line."""
    G = ui.G
    if not findings:
        return f"  {G.tick} no findings"

    lines: list[str] = []
    current = ""
    for finding in findings:
        if finding.severity != current:
            current = finding.severity
            if lines:
                lines.append("")
            lines.append(f"  {current.upper()}")
        where = finding.path or ""
        if where and finding.line:
            where = f"{where}:{finding.line}"
        head = f"{where}  " if where else ""
        lines.append(f"    {head}{finding.code}: {finding.message}")
        lines.append(f"      {finding.why}")

    counts = severity_counts(findings)
    lines.append("")
    lines.append("  " + "  ".join(f"{severity} {count}" for severity, count in counts.items()))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def _ascii_graph(project: Project, root: str | None) -> str:
    G = ui.G
    modules = [m for m in project.modules if root is None or m.name == root]
    if not modules:
        return "  no modules"

    lines: list[str] = []
    for module in modules:
        lines.append(f"  {module.name}")
        children = list(module.imports)
        if not children:
            lines.append(f"    {G.corner}{G.hbar} (no module dependencies)")
            continue
        for index, other in enumerate(children):
            last = index == len(children) - 1
            elbow = G.corner if last else G.branch
            lines.append(f"    {elbow}{G.hbar} {other}")
    return "\n".join(lines)


def _mermaid_graph(project: Project, root: str | None) -> str:
    edges = [
        (source, target)
        for source, target in module_edges(project)
        if root is None or root in (source, target)
    ]
    lines = ["graph TD"]
    shown = {name for edge in edges for name in edge}
    for module in project.modules:
        if root is not None and module.name not in shown and module.name != root:
            continue
        lines.append(f"    {module.name}[{module.name}]")
    for source, target in edges:
        lines.append(f"    {source} --> {target}")
    return "\n".join(lines)


def _dot_graph(project: Project, root: str | None) -> str:
    edges = [
        (source, target)
        for source, target in module_edges(project)
        if root is None or root in (source, target)
    ]
    lines = ["digraph modules {", '    rankdir="LR";']
    for module in project.modules:
        if (
            root is not None
            and module.name != root
            and not any(module.name in edge for edge in edges)
        ):
            continue
        lines.append(f'    "{module.name}";')
    for source, target in edges:
        lines.append(f'    "{source}" -> "{target}";')
    lines.append("}")
    return "\n".join(lines)


def graph_payload(project: Project, root: str | None) -> dict[str, Any]:
    edges = [
        {"from": source, "to": target}
        for source, target in module_edges(project)
        if root is None or root in (source, target)
    ]
    return {
        "schema_version": "1",
        "nodes": [module.name for module in project.modules],
        "edges": edges,
    }


def render_graph(project: Project, *, output_format: str = "ascii", root: str | None = None) -> str:
    """The module dependency graph.

    `jfast workspace graph` draws services; this draws the modules inside one.
    They are different questions, and nothing else answers the second.
    """
    if output_format == "ascii":
        return _ascii_graph(project, root)
    if output_format == "mermaid":
        return _mermaid_graph(project, root)
    if output_format == "dot":
        return _dot_graph(project, root)
    if output_format == "json":
        return jsonlib.dumps(graph_payload(project, root), indent=2)
    raise ValueError(f"unknown format {output_format!r}; choose from {', '.join(GRAPH_FORMATS)}")
