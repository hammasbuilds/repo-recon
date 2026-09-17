"""Apply edits to source text, then prove the result is still the same program.

Two guarantees, both checked rather than assumed:

  1. **The result parses.** An edit that produces invalid Python is discarded
     and reported, never written.
  2. **Only annotations changed.** For mechanical rules the rewritten module
     must be AST-equivalent to the original once annotations are stripped. If
     a rewrite altered executable code, that is a bug in a rule and the file
     is left untouched.

Edits are applied last-first so that earlier offsets stay valid, and
overlapping edits are resolved by keeping the innermost - nested generics like
`Dict[str, List[int]]` produce edits for both and only the inner one may be
applied in a single pass.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .rules import Edit, Scan, scan_source


class RewriteRejected(RuntimeError):
    """The rewritten source did not survive its own checks."""


@dataclass
class Rewrite:
    path: str
    original: str
    result: str
    applied: list[Edit] = field(default_factory=list)
    skipped: list[tuple[Edit, str]] = field(default_factory=list)
    rejected: str | None = None

    @property
    def changed(self) -> bool:
        return self.rejected is None and self.result != self.original


def _offsets(source: str) -> list[int]:
    """Character offset at which each line starts."""
    out = [0]
    for line in source.splitlines(keepends=True):
        out.append(out[-1] + len(line))
    return out


def _span(starts: list[int], edit: Edit) -> tuple[int, int]:
    return (
        starts[edit.line - 1] + edit.col,
        starts[edit.end_line - 1] + edit.end_col,
    )


def _drop_overlaps(edits: list[Edit], starts: list[int]) -> tuple[list[Edit], list[tuple[Edit, str]]]:
    """Keep non-overlapping edits, preferring the innermost.

    `Dict[str, List[int]]` yields an edit for the whole subscript and one for
    the inner `List[int]`. Applying both would corrupt the text, so one pass
    applies the inner edit and a second pass picks up the outer one.
    """
    ordered = sorted(edits, key=lambda e: (_span(starts, e)[1] - _span(starts, e)[0]))
    kept: list[Edit] = []
    skipped: list[tuple[Edit, str]] = []
    taken: list[tuple[int, int]] = []
    for edit in ordered:
        start, end = _span(starts, edit)
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            skipped.append((edit, "overlaps a narrower edit; rerun to apply it"))
            continue
        taken.append((start, end))
        kept.append(edit)
    return kept, skipped


class _StripAnnotations(ast.NodeTransformer):
    """Remove annotations so two modules can be compared on executable code."""

    def visit_FunctionDef(self, node):  # noqa: N802
        node.returns = None
        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            arg.annotation = None
        if node.args.vararg:
            node.args.vararg.annotation = None
        if node.args.kwarg:
            node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):  # noqa: N802
        self.generic_visit(node)
        if node.value is None:
            # A bare `x: int` declares nothing executable.
            return ast.Pass()
        return ast.Assign(
            targets=[node.target], value=node.value,
            lineno=node.lineno, col_offset=node.col_offset,
        )


def _executable_shape(source: str) -> str:
    """A normalised dump of everything except annotations."""
    tree = _StripAnnotations().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def apply_edits(path: str, source: str, edits: list[Edit]) -> Rewrite:
    """Apply the given edits and verify the result."""
    rewrite = Rewrite(path=path, original=source, result=source)
    if not edits:
        return rewrite

    starts = _offsets(source)
    kept, skipped = _drop_overlaps(edits, starts)
    rewrite.skipped = skipped

    text = source
    for edit in sorted(kept, key=lambda e: _span(starts, e)[0], reverse=True):
        start, end = _span(starts, edit)
        text = text[:start] + edit.replacement + text[end:]

    try:
        ast.parse(text)
    except SyntaxError as exc:
        rewrite.rejected = f"rewrite did not parse: line {exc.lineno}: {exc.msg}"
        rewrite.result = source
        return rewrite

    if all(e.mechanical for e in kept):
        try:
            if _executable_shape(source) != _executable_shape(text):
                rewrite.rejected = (
                    "a mechanical rewrite changed executable code, which is a bug in a "
                    "rule; the file was left untouched"
                )
                rewrite.result = source
                return rewrite
        except (SyntaxError, ValueError) as exc:
            rewrite.rejected = f"could not verify equivalence: {exc}"
            rewrite.result = source
            return rewrite

    rewrite.result = text
    rewrite.applied = kept
    return rewrite


def modernise(path: str, source: str, rules: set[str] | None = None) -> Rewrite:
    """Scan, then apply only the mechanical edits.

    Behavioural edits are never applied here. They are reported by `scan` and
    are the caller's decision.
    """
    scan: Scan = scan_source(path, source, rules)
    if scan.parse_error:
        rewrite = Rewrite(path=path, original=source, result=source)
        rewrite.rejected = f"did not parse: {scan.parse_error}"
        return rewrite
    return apply_edits(path, source, scan.mechanical)


def modernise_until_stable(
    path: str, source: str, rules: set[str] | None = None, passes: int = 4
) -> tuple[str, list[Edit], list[str]]:
    """Repeat until no edit applies, so nested generics fully unwind.

    Returns (source, applied edits, rejections).
    """
    text = source
    applied: list[Edit] = []
    rejections: list[str] = []
    for _ in range(passes):
        rewrite = modernise(path, text, rules)
        if rewrite.rejected:
            rejections.append(rewrite.rejected)
            break
        if not rewrite.changed:
            break
        text = rewrite.result
        applied.extend(rewrite.applied)
    return text, applied, rejections
