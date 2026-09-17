"""Turn a graph into the answers a newcomer actually asks.

Four questions, in the order someone opening an unfamiliar repository asks them:

1. How big is this and what am I looking at?
2. Where is the load-bearing code?
3. Where does execution start?
4. If I change X, what breaks?

Everything here is computed from the graph. No model is involved, which is why
the numbers are reproducible run to run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .graph import RepoGraph, build_graph, entry_points, most_called, pagerank
from .parse import Symbol, parse_repo


@dataclass
class ModuleCard:
    module: str
    path: str
    loc: int
    symbols: int
    importance: float
    imports: list[str]
    imported_by: list[str]


@dataclass
class SymbolCard:
    qualname: str
    name: str
    kind: str
    path: str
    line: int
    fan_in: int
    docstring: str | None


@dataclass
class RepoReport:
    name: str
    root: str
    modules: int
    symbols: int
    loc: int
    total_calls: int
    in_scope_calls: int
    resolved_calls: int
    resolution_rate: float
    repo_resolution_rate: float
    excluded_projects: list[str]
    parse_errors: list[tuple[str, str]]
    core_modules: list[ModuleCard] = field(default_factory=list)
    entry_points: list[SymbolCard] = field(default_factory=list)
    most_called: list[SymbolCard] = field(default_factory=list)
    unresolved_sample: list[tuple[str, int]] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)


def _symbol_card(sym: Symbol, fan_in: int) -> SymbolCard:
    doc = sym.docstring.strip().splitlines()[0] if sym.docstring else None
    return SymbolCard(
        qualname=sym.qualname,
        name=sym.name,
        kind=sym.kind,
        path=sym.path,
        line=sym.line,
        fan_in=fan_in,
        docstring=doc,
    )


def build_report(root: Path, top: int = 12) -> RepoReport:
    """Map one repository."""
    repo = parse_repo(root)
    graph = build_graph(repo)
    return report_from_graph(graph, top=top)


def report_from_graph(graph: RepoGraph, top: int = 12) -> RepoReport:
    repo = graph.repo
    res = graph.resolution
    ranks = pagerank(graph)
    by_module = {m.module: m for m in repo.modules}

    core: list[ModuleCard] = []
    for module, score in sorted(ranks.items(), key=lambda kv: -kv[1])[:top]:
        mod = by_module.get(module)
        if mod is None:
            continue
        core.append(
            ModuleCard(
                module=module,
                path=mod.path,
                loc=mod.loc,
                symbols=len(mod.symbols),
                importance=round(score, 5),
                imports=sorted(graph.module_imports.get(module, ())),
                imported_by=sorted(graph.module_importers.get(module, ())),
            )
        )

    entries = [_symbol_card(s, 0) for s in entry_points(graph)[:top]]
    called = [_symbol_card(s, c) for s, c in most_called(graph, top)]

    return RepoReport(
        name=repo.root.name,
        root=str(repo.root),
        modules=len(repo.modules),
        symbols=len(repo.symbols),
        loc=sum(m.loc for m in repo.modules),
        total_calls=res.total_calls,
        in_scope_calls=res.in_scope_calls,
        resolved_calls=res.resolved_count,
        resolution_rate=round(res.resolution_rate, 4),
        repo_resolution_rate=round(res.repo_resolution_rate, 4),
        excluded_projects=repo.excluded_projects,
        parse_errors=repo.parse_errors,
        core_modules=core,
        entry_points=entries,
        most_called=called,
        unresolved_sample=res.missed.most_common(10),
    )


def impact_of(graph: RepoGraph, qualname: str, depth: int = 3) -> dict[str, list[str]]:
    """What breaks if this definition changes.

    Breadth-first over reversed call edges. Depth is capped because beyond three
    hops almost everything in a small repo is reachable, and a blast radius of
    "everything" tells a reader nothing.
    """
    if qualname not in graph.by_qualname:
        return {}
    seen = {qualname}
    layers: dict[str, list[str]] = {}
    frontier = [qualname]
    for hop in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for edge in graph.callers(node):
                if edge.caller in seen:
                    continue
                seen.add(edge.caller)
                nxt.append(edge.caller)
        if not nxt:
            break
        layers[f"hop_{hop}"] = sorted(nxt)
        frontier = nxt
    return layers


def render_text(report: RepoReport) -> str:
    """The terminal view. Deliberately narrow enough to paste into a README."""
    w = 78
    out: list[str] = []
    add = out.append

    add("=" * w)
    add(f"  {report.name}  -  {report.modules} modules, {report.symbols} definitions, "
        f"{report.loc:,} lines")
    add("=" * w)

    if report.excluded_projects:
        add("")
        add(f"  Excluded {len(report.excluded_projects)} vendored project(s):")
        for p in report.excluded_projects[:5]:
            add(f"    - {p}")

    if report.parse_errors:
        add("")
        add(f"  {len(report.parse_errors)} file(s) did not parse:")
        for path, err in report.parse_errors[:5]:
            add(f"    - {path}: {err}")

    add("")
    add("  CALL RESOLUTION")
    add(f"    {report.resolved_calls} of {report.in_scope_calls} repo-internal calls "
        f"resolved  ({report.repo_resolution_rate:.0%})")
    add(f"    {report.total_calls} call sites in total; the rest are builtins,")
    add("    the standard library, or methods on third-party objects.")

    add("")
    add("  LOAD-BEARING MODULES  (PageRank over internal imports)")
    for c in report.core_modules[:8]:
        add(f"    {c.importance:.4f}  {c.module}")
        add(f"              {c.path}  -  {c.loc} lines, imported by {len(c.imported_by)}")

    add("")
    add("  MOST-CALLED DEFINITIONS")
    for s in report.most_called[:8]:
        add(f"    {s.fan_in:4d} callers  {s.qualname}  ({s.path}:{s.line})")

    add("")
    add("  ENTRY POINTS  (public, nothing in the repo calls them)")
    for s in report.entry_points[:8]:
        doc = f"  - {s.docstring}" if s.docstring else ""
        add(f"    {s.qualname}  ({s.path}:{s.line}){doc}")

    add("")
    add("=" * w)
    return "\n".join(out)
