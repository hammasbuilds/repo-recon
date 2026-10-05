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
        # The parameter each default belongs to, carried on the proposal. The verifier
        # used to re-derive it from the line number, so `def f(a=[], b=[])` gave every
        # proposal the FIRST parameter: the genuine bug on `b` was retracted with
        # "the default is never mutated inside f()", a false statement about the code,
        # and the mirror case confirmed two findings where one was real.
        for arg, default in _defaults_with_parameters(node):
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    default,
                    "mutable-default",
                    f"{type(default).__name__.lower()} used as the default for "
                    f"{arg}= in {node.name}(); it is created once and shared between calls",
                    "bug",
                    function=node.name,
                    parameter=arg,
                )
            elif isinstance(default, ast.Call):
                name = getattr(default.func, "id", None) or getattr(default.func, "attr", "")
                if name in ("list", "dict", "set"):
                    self._add(
                        default,
                        "mutable-default",
                        f"{name}() as the default for {arg}= in {node.name}(); evaluated "
                        f"once at definition, not per call",
                        "bug",
                        function=node.name,
                        parameter=arg,
                    )

        self.function_stack.append(node)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- exception handling ----------------------------------------------

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        swallowed = _swallows(node.body)

        if node.type is None:
            self._add(
                node,
                "bare-except",
                "bare `except:` catches KeyboardInterrupt and SystemExit as well",
                "bug",
                swallows=bool(swallowed),
            )
        elif swallowed:
            # It used to require the type to be a bare `Exception` Name AND the body to
            # be exactly one `Pass`. Four of the six obvious swallows were invisible:
            # `except Exception: return None`, `except (ValueError, KeyError): pass`,
            # `except OSError: pass`, `except Exception: continue` - and this
            # repository's own `except OSError: continue` among them, so its reviewer
            # could not see its own code.
            caught = _exception_names(node.type)
            self._add(
                node,
                "swallowed-exception",
                f"`except {caught}: {swallowed}` discards the error and continues",
                "bug",
                swallows=True,
            )
        self.generic_visit(node)

    # -- comparisons ------------------------------------------------------

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                if comparator.value is None:
                    self._add(
                        node,
                        "eq-none",
                        "compares to None with == rather than `is`",
                        "smell",
                    )
                elif isinstance(comparator.value, bool):
                    self._add(
                        node,
                        "eq-bool",
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
                        inner,
                        "mutate-while-iterating",
                        f"`{target}` is mutated with .{func.attr}() while being iterated",
                        "bug",
                        collection=target,
                        method=func.attr,
                    )
        self.generic_visit(node)

    # -- resource handling --------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = getattr(node.func, "id", None)
        if name == "open":
            self._add(
                node,
                "open-without-with",
                "open() result may not be closed; consider a `with` block",
                "smell",
            )
        self.generic_visit(node)

    # -- assertions in shipped code -----------------------------------------

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
        self._add(
            node,
            "assert-in-source",
            "assert is removed under `python -O`, so this check may not run",
            "smell",
        )
        self.generic_visit(node)


def _swallows(body: list[ast.stmt]) -> str:
    """How this handler discards the exception, or "" if it does not.

    A handler that does nothing with the error and leaves: `pass`, `continue`, a bare
    `return`, or a return of a constant. Logging it, re-raising it, or doing any real
    work is not swallowing - a handler with a body is a decision, and this check is for
    the ones with no body to speak of.
    """
    if len(body) != 1:
        return ""
    only = body[0]
    if isinstance(only, ast.Pass):
        return "pass"
    if isinstance(only, ast.Continue):
        return "continue"
    if isinstance(only, ast.Break):
        return "break"
    if isinstance(only, ast.Return):
        if only.value is None:
            return "return"
        if isinstance(only.value, ast.Constant):
            return f"return {only.value.value!r}"
    return ""


def _exception_names(node: ast.expr | None) -> str:
    """The caught type as written: `Exception`, `OSError`, `(ValueError, KeyError)`."""
    if node is None:
        return ""
    if isinstance(node, ast.Tuple):
        return "(" + ", ".join(_exception_names(e) for e in node.elts) + ")"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_exception_names(node.value)}.{node.attr}"
    return "<expr>"


def _defaults_with_parameters(node: ast.AST) -> list[tuple[str, ast.expr]]:
    """(parameter name, default expression) pairs, positional then keyword-only."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    pairs = [
        (arg.arg, default)
        for arg, default in zip(
            positional[len(positional) - len(args.defaults) :], args.defaults, strict=False
        )
    ]
    pairs += [
        (arg.arg, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False)
        if default is not None
    ]
    return pairs


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
