"""Proposers: cheap AST checks that suggest something might be wrong.

These are deliberately *over-eager*. A proposer that only fires when certain
would find almost nothing, and the interesting question this project exists to
answer is how many plausible-looking findings survive an adversarial second
look. So proposers guess, and `verify.py` tries to knock each guess down.

Nothing here is reported to a user directly. A proposal is an input to
verification, not an output.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class Proposal:
    """A suspicion, with enough context for a verifier to attack it."""

    rule: str
    line: int
    end_line: int
    message: str
    severity: str  # "bug" | "smell"
    snippet: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int]:
        return (self.rule, self.line)


def _segment(lines: list[str], node: ast.AST) -> str:
    start = max(0, node.lineno - 1)
    end = min(len(lines), getattr(node, "end_lineno", node.lineno))
    return "\n".join(lines[start:end]).strip()


class _Proposer(ast.NodeVisitor):
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.proposals: list[Proposal] = []
        self.function_stack: list[ast.AST] = []

    def _add(self, node: ast.AST, rule: str, message: str, severity: str, **evidence) -> None:
        self.proposals.append(
            Proposal(
                rule=rule,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                message=message,
                severity=severity,
                snippet=_segment(self.lines, node),
                evidence=evidence,
            )
        )

    # -- mutable default arguments ---------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    default, "mutable-default",
                    f"{type(default).__name__.lower()} used as a default argument in "
                    f"{node.name}(); it is created once and shared between calls",
                    "bug", function=node.name,
                )
            elif isinstance(default, ast.Call):
                name = getattr(default.func, "id", None) or getattr(default.func, "attr", "")
                if name in ("list", "dict", "set"):
                    self._add(
                        default, "mutable-default",
                        f"{name}() as a default argument in {node.name}(); evaluated "
                        f"once at definition, not per call",
                        "bug", function=node.name,
                    )

        self.function_stack.append(node)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- exception handling ----------------------------------------------

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        body_is_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)

        if node.type is None:
            self._add(
                node, "bare-except",
                "bare `except:` catches KeyboardInterrupt and SystemExit as well",
                "bug", swallows=body_is_pass,
            )
        elif getattr(node.type, "id", None) == "Exception" and body_is_pass:
            self._add(
                node, "swallowed-exception",
                "`except Exception: pass` discards the error and continues",
                "bug", swallows=True,
            )
        self.generic_visit(node)

    # -- comparisons ------------------------------------------------------

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                if comparator.value is None:
                    self._add(
                        node, "eq-none",
                        "compares to None with == rather than `is`",
                        "smell",
                    )
                elif isinstance(comparator.value, bool):
                    self._add(
                        node, "eq-bool",
                        f"compares to {comparator.value} explicitly",
                        "smell",
                    )
        self.generic_visit(node)

    # -- loops -------------------------------------------------------------

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        # Mutating the collection being iterated.
        target = getattr(node.iter, "id", None)
        if target:
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("append", "remove", "pop", "insert", "clear")
                    and getattr(func.value, "id", None) == target
                ):
                    self._add(
                        inner, "mutate-while-iterating",
                        f"`{target}` is mutated with .{func.attr}() while being iterated",
                        "bug", collection=target, method=func.attr,
                    )
        self.generic_visit(node)

    # -- resource handling --------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = getattr(node.func, "id", None)
        if name == "open":
            self._add(
                node, "open-without-with",
                "open() result may not be closed; consider a `with` block",
                "smell",
            )
        self.generic_visit(node)

    # -- assertions in shipped code -----------------------------------------

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
        self._add(
            node, "assert-in-source",
            "assert is removed under `python -O`, so this check may not run",
            "smell",
        )
        self.generic_visit(node)


def propose(source: str) -> list[Proposal]:
    """Every suspicion in one file. Over-eager by design."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    proposer = _Proposer(source.splitlines())
    proposer.visit(tree)
    return sorted(proposer.proposals, key=lambda p: (p.line, p.rule))


RULES = (
    "mutable-default",
    "bare-except",
    "swallowed-exception",
    "eq-none",
    "eq-bool",
    "mutate-while-iterating",
    "open-without-with",
    "assert-in-source",
)
