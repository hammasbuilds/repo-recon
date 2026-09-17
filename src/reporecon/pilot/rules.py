"""Codemod rules, split by whether they can change behaviour.

Two classes, and the split is the point of the project:

  MECHANICAL   the rewrite is provably equivalent. `typing.List[int]` and
               `list[int]` mean the same thing to every consumer.
  BEHAVIOURAL  the rewrite is what the deprecation notice tells you to write,
               and it does not mean the same thing. These are *found* and
               *reported*, never applied automatically.

`datetime.utcnow()` is the case that motivated the split. The documented
replacement is `datetime.now(timezone.utc)`, which returns an **aware**
datetime where `utcnow()` returned a naive one. Every comparison against a
naive datetime elsewhere in the codebase then raises TypeError at runtime. It
looks like a one-token find-and-replace and it is a semantic change.

Edits are located with `ast` and applied as text slices. Round-tripping
through `ast.unparse` would be simpler and would reformat every file it
touched, turning a two-line diff into a whole-file diff nobody can review.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

MECHANICAL = "mechanical"
BEHAVIOURAL = "behavioural"


@dataclass(frozen=True)
class Edit:
    """One text replacement, located by line and column."""

    line: int  # 1-based
    col: int  # 0-based
    end_line: int
    end_col: int
    replacement: str
    rule: str
    kind: str  # MECHANICAL | BEHAVIOURAL
    before: str = ""
    note: str = ""

    @property
    def mechanical(self) -> bool:
        return self.kind == MECHANICAL


@dataclass
class Scan:
    path: str
    edits: list[Edit] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def mechanical(self) -> list[Edit]:
        return [e for e in self.edits if e.mechanical]

    @property
    def behavioural(self) -> list[Edit]:
        return [e for e in self.edits if not e.mechanical]


# -- typing generics ------------------------------------------------------

_GENERIC_ALIASES = {
    "List": "list", "Dict": "dict", "Set": "set", "FrozenSet": "frozenset",
    "Tuple": "tuple", "Type": "type", "Deque": "collections.deque",
}


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _segment(source_lines: list[str], node: ast.AST) -> str:
    """The exact source text a node covers."""
    start, end = node.lineno - 1, node.end_lineno - 1
    if start == end:
        return source_lines[start][node.col_offset : node.end_col_offset]
    parts = [source_lines[start][node.col_offset :]]
    parts.extend(source_lines[start + 1 : end])
    parts.append(source_lines[end][: node.end_col_offset])
    return "\n".join(parts)


class _Visitor(ast.NodeVisitor):
    def __init__(self, lines: list[str], enabled: set[str]):
        self.lines = lines
        self.enabled = enabled
        self.edits: list[Edit] = []

    def _add(self, node: ast.AST, replacement: str, rule: str, kind: str, note: str = "") -> None:
        if rule not in self.enabled:
            return
        self.edits.append(
            Edit(
                line=node.lineno,
                col=node.col_offset,
                end_line=node.end_lineno,
                end_col=node.end_col_offset,
                replacement=replacement,
                rule=rule,
                kind=kind,
                before=_segment(self.lines, node),
                note=note,
            )
        )

    # typing.List[int] -> list[int]
    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        name = _name_of(node.value)
        bare = name.split(".")[-1] if name else None

        if bare in _GENERIC_ALIASES and (name == bare or name == f"typing.{bare}"):
            inner = _segment(self.lines, node.slice)
            self._add(
                node, f"{_GENERIC_ALIASES[bare]}[{inner}]", "pep585-generics", MECHANICAL,
                "typing.List and list are the same type at runtime from 3.9",
            )
        elif bare == "Optional" and (name == bare or name == f"typing.{bare}"):
            inner = _segment(self.lines, node.slice)
            self._add(
                node, f"{inner} | None", "pep604-optional", MECHANICAL,
                "Optional[X] and X | None are the same annotation from 3.10",
            )
        elif bare == "Union" and (name == bare or name == f"typing.{bare}"):
            if isinstance(node.slice, ast.Tuple):
                parts = [_segment(self.lines, e) for e in node.slice.elts]
                self._add(
                    node, " | ".join(parts), "pep604-union", MECHANICAL,
                    "Union[A, B] and A | B are the same annotation from 3.10",
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _name_of(node.func) or ""

        # The behavioural one.
        if name.endswith("utcnow") and not node.args and not node.keywords:
            self._add(
                node, "datetime.now(timezone.utc)", "utcnow-deprecated", BEHAVIOURAL,
                "utcnow() returns a NAIVE datetime; now(timezone.utc) returns an AWARE "
                "one. Comparing the two raises TypeError, so this is a semantic change, "
                "not a rename. Requires `from datetime import timezone`.",
            )
        elif name.endswith("utcfromtimestamp") and len(node.args) == 1:
            arg = _segment(self.lines, node.args[0])
            self._add(
                node, f"datetime.fromtimestamp({arg}, timezone.utc)",
                "utcfromtimestamp-deprecated", BEHAVIOURAL,
                "same naive-to-aware change as utcnow()",
            )
        # datetime.datetime.now() with no tz is naive, and usually not intended.
        elif name.endswith("datetime.now") and not node.args and not node.keywords:
            self._add(
                node, "datetime.now(timezone.utc)", "naive-now", BEHAVIOURAL,
                "now() with no timezone returns a naive local datetime; whether UTC is "
                "the right choice depends on the caller, so this is never applied",
            )
        self.generic_visit(node)


ALL_RULES: tuple[tuple[str, str, str], ...] = (
    ("pep585-generics", MECHANICAL, "typing.List[int] -> list[int]"),
    ("pep604-optional", MECHANICAL, "Optional[X] -> X | None"),
    ("pep604-union", MECHANICAL, "Union[A, B] -> A | B"),
    ("utcnow-deprecated", BEHAVIOURAL, "datetime.utcnow() -> datetime.now(timezone.utc)"),
    ("utcfromtimestamp-deprecated", BEHAVIOURAL,
     "datetime.utcfromtimestamp(x) -> datetime.fromtimestamp(x, timezone.utc)"),
    ("naive-now", BEHAVIOURAL, "datetime.now() with no timezone"),
)

RULE_KIND = {name: kind for name, kind, _ in ALL_RULES}
DEFAULT_RULES = frozenset(name for name, _, _ in ALL_RULES)


def scan_source(path: str, source: str, rules: set[str] | None = None) -> Scan:
    """Find every applicable edit in one file. Applies nothing."""
    enabled = set(rules) if rules is not None else set(DEFAULT_RULES)
    scan = Scan(path=path)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        scan.parse_error = f"line {exc.lineno}: {exc.msg}"
        return scan

    visitor = _Visitor(source.splitlines(), enabled)
    visitor.visit(tree)
    # Innermost-first, so nested generics are rewritten before their parents
    # are measured. Applying outermost-first would invalidate inner offsets.
    scan.edits = sorted(
        visitor.edits, key=lambda e: (e.line, e.col, -(e.end_line * 10000 + e.end_col))
    )
    return scan
