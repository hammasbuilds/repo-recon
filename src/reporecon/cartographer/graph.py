"""Resolve call sites to definitions and rank what matters in a repository.

Resolution is deliberately conservative. A call is bound to a definition only
when the binding follows from an import or a same-module definition; anything
ambiguous is left unresolved and counted. An honest "unresolved: 41%" is worth
more to a reader than a graph that guessed and looks complete.
"""

from __future__ import annotations

import builtins
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .parse import UNNAMEABLE, ParsedRepo, Symbol

# Attribute calls whose receiver is a builtin type, not a repo symbol.
_NOISE_ROOTS = frozenset({"self", "cls", "super", UNNAMEABLE})

_BUILTINS = frozenset(dir(builtins))
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", frozenset()))


@dataclass
class CallEdge:
    caller: str  # qualname of the enclosing definition
    callee: str  # qualname of the resolved definition
    module: str
    line: int


@dataclass
class Resolution:
    """The outcome of binding call sites to definitions."""

    edges: list[CallEdge] = field(default_factory=list)
    unresolved: Counter = field(default_factory=Counter)  # written name -> count
    total_calls: int = 0

    @property
    def resolved_count(self) -> int:
        return len(self.edges)

    @property
    def unresolved_count(self) -> int:
        return sum(self.unresolved.values())

    # Unresolved calls split by *why* they did not resolve.
    out_of_scope: Counter = field(default_factory=Counter)  # builtin/stdlib/3rd-party
    missed: Counter = field(default_factory=Counter)  # should have resolved

    @property
    def resolution_rate(self) -> float:
        """Resolved as a share of every call site. Reported for completeness.

        This number is not the interesting one: most calls in any repo are
        `len`, `print` or a method on a third-party object, and none of those
        could ever bind to a repo definition.
        """
        if self.total_calls == 0:
            return 0.0
        return self.resolved_count / self.total_calls

    @property
    def in_scope_calls(self) -> int:
        """Calls that plausibly target this repo's own code."""
        return self.resolved_count + sum(self.missed.values())

    @property
    def repo_resolution_rate(self) -> float:
        """The honest metric: of calls that *could* target repo code, how many bound.

        Counting builtins in the denominator makes every repository look
        unmappable. The same denominator mistake that made grammar-constrained
        decoding look worse than plain prompting in langchain-lab 01.
        """
        if self.in_scope_calls == 0:
            return 0.0
        return self.resolved_count / self.in_scope_calls


@dataclass
class RepoGraph:
    repo: ParsedRepo
    by_qualname: dict[str, Symbol]
    resolution: Resolution
    callers_of: dict[str, list[CallEdge]]  # callee qualname -> edges into it
    calls_from: dict[str, list[CallEdge]]  # caller qualname -> edges out of it
    module_imports: dict[str, set[str]]  # module -> internal modules it imports
    module_importers: dict[str, set[str]]  # module -> internal modules importing it

    def callers(self, qualname: str) -> list[CallEdge]:
        """Every call site that reaches this definition. The core query."""
        return self.callers_of.get(qualname, [])

    def fan_in(self, qualname: str) -> int:
        return len(self.callers_of.get(qualname, []))


def _build_import_index(repo: ParsedRepo) -> dict[str, dict[str, str]]:
    """Per module, map a locally-bound name to the module it came from.

    `from cartographer.parse import parse_repo` in module M yields
    index[M]["parse_repo"] = "cartographer.parse".
    """
    index: dict[str, dict[str, str]] = defaultdict(dict)
    for mod in repo.modules:
        for imp in mod.imports:
            if not imp.is_internal or not imp.target:
                continue
            for name in imp.names:
                if name == "*":
                    continue
                index[mod.module][name] = imp.target
            # `import pkg.mod` binds the head name to the dotted path.
            if not imp.is_relative and "." not in "".join(imp.names):
                index[mod.module][imp.target.split(".")[0]] = imp.target
    return index


def _external_names(repo: ParsedRepo) -> dict[str, set[str]]:
    """Per module, names bound by imports of code outside this repo."""
    index: dict[str, set[str]] = defaultdict(set)
    for mod in repo.modules:
        for imp in mod.imports:
            if imp.is_internal:
                continue
            index[mod.module].add(imp.target.split(".")[0])
            for name in imp.names:
                if name != "*":
                    index[mod.module].add(name)
    return index


def _classify_miss(callee: str, module: str, external: set[str], internal: set[str]) -> str:
    """Why did this call not resolve: out of scope, or genuinely missed?

    Out of scope means it never could have bound to a repo definition - a
    builtin, the standard library, a third-party import, or a method on a
    runtime object whose type an AST cannot know. Missed means the name points
    at repo code that resolution failed to find, and is a real defect.
    """
    head, _, rest = callee.partition(".")

    if head in _BUILTINS or head in _STDLIB or head in external:
        return "out_of_scope"
    if head in _NOISE_ROOTS:
        # self.something() where the enclosing class had no such method:
        # usually inherited from a base class defined elsewhere.
        return "out_of_scope"
    if head in internal:
        return "missed"  # points into this repo but did not bind
    if rest:
        # A method on a local variable. Needs type inference, which this tool
        # deliberately does not attempt.
        return "out_of_scope"
    return "missed"


def _module_symbol_index(repo: ParsedRepo) -> dict[str, dict[str, Symbol]]:
    """Per module, the definitions it declares, by their short name."""
    index: dict[str, dict[str, Symbol]] = defaultdict(dict)
    for mod in repo.modules:
        for sym in mod.symbols:
            # Top-level definitions only: a method is reached via its class.
            if sym.qualname.count(".") == mod.module.count(".") + 1:
                index[mod.module][sym.name] = sym
    return index


def _class_method_index(repo: ParsedRepo) -> dict[str, dict[str, Symbol]]:
    """Per class qualname, its methods by short name - for `self.x()` calls."""
    index: dict[str, dict[str, Symbol]] = defaultdict(dict)
    for mod in repo.modules:
        for sym in mod.symbols:
            if sym.kind != "method":
                continue
            owner = sym.qualname.rsplit(".", 1)[0]
            index[owner][sym.name] = sym
    return index


def _follow_reexports(
    module: str,
    name: str,
    imports: dict[str, dict[str, str]],
    by_qualname: dict[str, Symbol],
    depth: int = 6,
) -> Symbol | None:
    """Resolve a name through a chain of `__init__.py` re-exports.

    `from urdunlp import normalize` binds `normalize` to the package, but the
    definition lives in `urdunlp.normalise`. Without following the package's own
    imports, every call through a re-exporting `__init__` is unresolvable -
    which is exactly why urdu-nlp-toolkit resolved only 24% of its internal
    calls while repos that import submodules directly resolved 98-100%.

    A package that re-exports from a module that re-exports back would loop, so
    visited modules are tracked and the chain is depth-capped.
    """
    seen: set[str] = set()
    current = module
    for _ in range(depth):
        if current in seen:
            return None
        seen.add(current)
        found = by_qualname.get(f"{current}.{name}")
        if found is not None:
            return found
        nxt = imports.get(current, {}).get(name)
        if nxt is None or nxt == current:
            return None
        current = nxt
    return None


def _enclosing_scope_symbol(
    caller: str, name: str, by_qualname: dict[str, Symbol]
) -> Symbol | None:
    """Find `name` in an enclosing function scope, innermost first.

    A sibling nested function is visible to its siblings through the closure,
    so `helper()` called inside `outer.inner` may be `outer.helper`. Without
    this, closure-local helpers looked uncalled and were reported as entry
    points - `chunk_text.flush` in rag-forge was the case that exposed it.
    """
    parts = caller.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i]) + f".{name}"
        found = by_qualname.get(candidate)
        if found is not None:
            return found
    return None


def _enclosing_class(caller: str, classes: set[str]) -> str | None:
    """Walk up a caller qualname looking for a class it sits inside."""
    parts = caller.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in classes:
            return candidate
    return None


def resolve(repo: ParsedRepo) -> Resolution:
    """Bind every call site we can to a definition in this repo."""
    imports = _build_import_index(repo)
    mod_syms = _module_symbol_index(repo)
    cls_methods = _class_method_index(repo)
    externals = _external_names(repo)
    by_qualname = {s.qualname: s for s in repo.symbols}
    class_names = {s.qualname for s in repo.symbols if s.kind == "class"}

    res = Resolution()

    for mod in repo.modules:
        local = mod_syms.get(mod.module, {})
        imported = imports.get(mod.module, {})
        external = externals.get(mod.module, set())
        internal_names = set(local) | set(imported)

        for call in mod.calls:
            res.total_calls += 1
            head, _, rest = call.callee.partition(".")
            target: Symbol | None = None

            if head in _NOISE_ROOTS and rest:
                # self.method() - resolve against the enclosing class.
                owner = _enclosing_class(call.caller, class_names)
                if owner:
                    target = cls_methods.get(owner, {}).get(rest.split(".")[0])
            elif not rest:
                # A bare name: a nested definition in the calling scope, one
                # defined at module level, or one imported from a repo module.
                # Closure scope is checked first because it shadows: a nested
                # `flush()` inside `chunk_text` is not the module-level `flush`.
                target = by_qualname.get(f"{call.caller}.{head}")
                if target is None:
                    target = _enclosing_scope_symbol(call.caller, head, by_qualname)
                if target is None:
                    target = local.get(head)
                if target is None and head in imported:
                    target = _follow_reexports(
                        imported[head], head, imports, by_qualname
                    )
            else:
                # Dotted: module.func, or Class.method via an import.
                first = rest.split(".")[0]
                if head in imported:
                    target = _follow_reexports(
                        imported[head], first, imports, by_qualname
                    )
                if target is None and head in local:
                    # Class defined here, method called on it.
                    target = cls_methods.get(local[head].qualname, {}).get(first)

            if target is not None:
                res.edges.append(
                    CallEdge(
                        caller=call.caller,
                        callee=target.qualname,
                        module=call.module,
                        line=call.line,
                    )
                )
            else:
                res.unresolved[call.callee] += 1
                reason = _classify_miss(
                    call.callee, mod.module, external, internal_names
                )
                if reason == "missed":
                    res.missed[call.callee] += 1
                else:
                    res.out_of_scope[call.callee] += 1

    return res


def build_graph(repo: ParsedRepo) -> RepoGraph:
    """Parse output in, queryable graph out."""
    res = resolve(repo)

    callers_of: dict[str, list[CallEdge]] = defaultdict(list)
    calls_from: dict[str, list[CallEdge]] = defaultdict(list)
    for e in res.edges:
        callers_of[e.callee].append(e)
        calls_from[e.caller].append(e)

    module_imports: dict[str, set[str]] = defaultdict(set)
    module_importers: dict[str, set[str]] = defaultdict(set)
    known = {m.module for m in repo.modules}
    for mod in repo.modules:
        for imp in mod.imports:
            if imp.is_internal and imp.target in known:
                module_imports[mod.module].add(imp.target)
                module_importers[imp.target].add(mod.module)

    return RepoGraph(
        repo=repo,
        by_qualname={s.qualname: s for s in repo.symbols},
        resolution=res,
        callers_of=dict(callers_of),
        calls_from=dict(calls_from),
        module_imports=dict(module_imports),
        module_importers=dict(module_importers),
    )


def pagerank(
    graph: RepoGraph, damping: float = 0.85, iterations: int = 40
) -> dict[str, float]:
    """Rank modules by how much the repo's own imports depend on them.

    Plain in-degree says "imported often". PageRank says "imported by modules
    that are themselves depended upon", which is the question a newcomer is
    actually asking: where does this codebase keep its load-bearing code.
    """
    nodes = [m.module for m in graph.repo.modules]
    if not nodes:
        return {}
    n = len(nodes)
    rank = dict.fromkeys(nodes, 1.0 / n)
    out_degree = {m: len(graph.module_imports.get(m, ())) for m in nodes}

    for _ in range(iterations):
        new = dict.fromkeys(nodes, (1.0 - damping) / n)
        dangling = sum(rank[m] for m in nodes if out_degree[m] == 0)
        for m in nodes:
            new[m] += damping * dangling / n
        for m in nodes:
            targets = graph.module_imports.get(m, ())
            if not targets:
                continue
            share = damping * rank[m] / len(targets)
            for t in targets:
                if t in new:
                    new[t] += share
        rank = new

    return rank


def entry_points(graph: RepoGraph) -> list[Symbol]:
    """Definitions nothing inside the repo calls - the outside world's doors.

    CLI commands, FastAPI routes, test functions and `main` all land here.
    """
    out = []
    for sym in graph.repo.symbols:
        if graph.fan_in(sym.qualname) > 0 or not sym.is_public:
            continue
        if sym.kind == "class":
            continue
        out.append(sym)
    return sorted(out, key=lambda s: (-s.span, s.qualname))


def most_called(graph: RepoGraph, limit: int = 20) -> list[tuple[Symbol, int]]:
    """The definitions the repo leans on most."""
    counts = [
        (graph.by_qualname[q], len(edges))
        for q, edges in graph.callers_of.items()
        if q in graph.by_qualname
    ]
    return sorted(counts, key=lambda kv: (-kv[1], kv[0].qualname))[:limit]
