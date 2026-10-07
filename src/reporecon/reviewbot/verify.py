"""Adversarial verification: try to disprove every proposal before reporting it.

Each verifier is written to *defeat* its proposal, not to confirm it. A
proposal survives only if no defeater applies. Anything knocked down is
recorded with the reason, because the retraction rate is the number this
project is built to measure - a reviewer that posts plausible-looking noise
gets muted on the second day, and precision is the entire product.

Verifiers look at the whole file, not the changed hunk. That is the point:
most false positives are false because of something the diff did not show.
"""

from __future__ import annotations

import re

import ast
from dataclasses import dataclass

from .checks import Proposal


@dataclass(frozen=True)
class Verdict:
    proposal: Proposal
    confirmed: bool
    reason: str  # why it survived, or what defeated it

    @property
    def rule(self) -> str:
        return self.proposal.rule


@dataclass
class FileContext:
    """Everything a verifier needs that the proposal itself does not carry."""

    path: str
    source: str
    tree: ast.AST | None
    lines: list[str]
    is_test: bool

    @classmethod
    def build(cls, path: str, source: str) -> FileContext:
        try:
            tree: ast.AST | None = ast.parse(source)
        except SyntaxError:
            tree = None
        name = path.replace("\\", "/").split("/")[-1]
        parts = path.replace("\\", "/").split("/")
        return cls(
            path=path,
            source=source,
            tree=tree,
            lines=source.splitlines(),
            is_test=(
                name.startswith("test_")
                or name.endswith("_test.py")
                or "tests" in parts
                or "test" in parts
            ),
        )

    def line(self, number: int) -> str:
        index = number - 1
        return self.lines[index] if 0 <= index < len(self.lines) else ""

    def comment_near(self, number: int, span: int = 1) -> str:
        """Comment text on or just above a line."""
        out = []
        for offset in range(-span, span + 1):
            text = self.line(number + offset)
            if "#" in text:
                out.append(text.split("#", 1)[1].strip().lower())
        return " ".join(out)


# Comment markers that say the author knew. Respecting them is not politeness:
# a reviewer that re-raises an acknowledged issue every run is noise.
#
# Prose markers: a person writing one of these is talking about the code, not about a
# particular checker, so they suppress any rule.
_ACKNOWLEDGED = ("intentional", "deliberate", "on purpose", "by design")

#: Suppression directives from other tools. These carry CODES, and the code says which
#: check was silenced - so `# noqa: E501` is a statement about line length and was
#: deleting shared-mutable-default findings, because the marker was substring-matched
#: and the code was never read.
_DIRECTIVES = ("noqa", "nosec", "type: ignore")

#: Which other tools' codes correspond to which of this tool's rules. A coded directive
#: suppresses only where its code is here; a BARE directive suppresses anything, which
#: is what a bare one means in the tools it comes from.
#:
#: Short on purpose. A code that is not listed does not suppress, so the cost of an
#: omission is a finding the author has to dismiss once - and the cost of the substring
#: match it replaces was silently dropping real bugs.
_DIRECTIVE_CODES: dict[str, frozenset[str]] = {
    "mutable-default": frozenset({"b006", "b008", "w0102"}),
    "bare-except": frozenset({"e722", "w0702", "w0703"}),
    "swallowed-exception": frozenset({"b110", "b112", "s110", "w0703"}),
    "eq-none": frozenset({"e711"}),
    "eq-bool": frozenset({"e712"}),
    "open-without-with": frozenset({"sim115", "r1732", "w1514"}),
    "assert-in-source": frozenset({"s101", "b101"}),
    "mutate-while-iterating": frozenset({"b909"}),
}

#: What a directive's codes look like after the colon: `# noqa: E501,F401`.
_CODE = re.compile(r"[a-z]+[0-9]+")


def _acknowledged(context: FileContext, proposal: Proposal) -> str | None:
    """Whether the author already said they know, about THIS rule.

    Read from the proposal's own line and the line above it. Not the line below: a
    comment after a statement does not acknowledge it, and `noqa`/`nosec` are
    line-scoped in the tools they come from. A comment on the next line was retracting
    findings about the previous one.
    """
    for offset in (0, -1):
        text = context.line(proposal.line + offset)
        if "#" not in text:
            continue
        comment = text.split("#", 1)[1].strip().lower()
        where = "on" if offset == 0 else "just above"
        for marker in _ACKNOWLEDGED:
            if marker in comment:
                return f"the author marked it: '{marker}' {where} line {proposal.line}"
        for directive in _DIRECTIVES:
            if directive not in comment:
                continue
            codes = set(_CODE.findall(comment.split(directive, 1)[1]))
            if not codes:
                # A bare directive silences everything, which is what it means.
                return (
                    f"the author marked it: a bare '{directive}' {where} line "
                    f"{proposal.line}"
                )
            mine = _DIRECTIVE_CODES.get(proposal.rule, frozenset())
            shared = sorted(codes & mine)
            if shared:
                return (
                    f"the author marked it: '{directive}: {shared[0].upper()}' {where} "
                    f"line {proposal.line}"
                )
            # A coded directive about something else. It is not about this rule, and
            # matching it as a substring was deleting real findings: `# noqa: E501` is
            # a line-length suppression and it retracted a shared mutable default.
    return None


# -- per-rule defeaters ---------------------------------------------------


def _defeat_mutable_default(context: FileContext, proposal: Proposal) -> str | None:
    """A shared mutable default is only a bug if the function mutates it."""
    if context.tree is None:
        return None
    name = proposal.evidence.get("function")
    target = next(
        (
            n
            for n in ast.walk(context.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    if target is None:
        return None

    # From the proposal, which knows; falling back to the line only for a proposal
    # made before this field existed. Several defaults on one line are indistinguishable
    # by line, and that is the common case: `def f(a=[], b=[])`.
    parameter = proposal.evidence.get("parameter") or _parameter_for_default(target, proposal.line)
    if parameter is None:
        return None

    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and getattr(node.func.value, "id", None) == parameter
            and node.func.attr
            in (
                "append",
                "extend",
                "insert",
                "pop",
                "remove",
                "clear",
                "update",
                "add",
                "setdefault",
                "sort",
            )
        ):
            return None  # mutated: the proposal stands
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Subscript) and getattr(sub.value, "id", None) == parameter:
                    return None
    return (
        f"the default is never mutated inside {name}(); sharing it between calls cannot be observed"
    )


def _parameter_for_default(function: ast.AST, line: int) -> str | None:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    for arg, default in zip(
        positional[len(positional) - len(args.defaults) :], args.defaults, strict=False
    ):
        if default.lineno == line:
            return arg.arg
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if default is not None and default.lineno == line:
            return arg.arg
    return None


def _defeat_bare_except(context: FileContext, proposal: Proposal) -> str | None:
    """A bare except that re-raises is a cleanup idiom, not swallowed control flow."""
    if context.tree is None:
        return None
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.ExceptHandler) or node.lineno != proposal.line:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Raise):
                return "the handler re-raises, so nothing is swallowed"
    return None


def _defeat_eq_none(context: FileContext, proposal: Proposal) -> str | None:
    """`== None` is a real smell in Python, and legitimate in a query DSL.

    SQLAlchemy, Django and pandas all overload `__eq__` to build expressions,
    where `is None` would be wrong.
    """
    lowered = context.source.lower()
    for marker in (
        "sqlalchemy",
        "django.db",
        "from django",
        "import pandas",
        "peewee",
        "tortoise",
        "sqlmodel",
    ):
        if marker in lowered:
            return f"the file imports {marker}, where `== None` builds a query expression"
    return None


def _defeat_open_without_with(context: FileContext, proposal: Proposal) -> str | None:
    """Only a smell if the handle actually escapes without being closed."""
    line = context.line(proposal.line)
    stripped = line.strip()
    if stripped.startswith("with ") or " with " in stripped:
        return "the call is already inside a `with` statement"
    # `open(...).read()` closes via refcount immediately in CPython and is a
    # widespread idiom; flagging it is noise.
    if ").read()" in stripped or ").write(" in stripped or ").readlines()" in stripped:
        return "the handle is consumed immediately and not retained"
    return None


def _defeat_assert_in_source(context: FileContext, proposal: Proposal) -> str | None:
    if context.is_test:
        return "the file is a test; asserts are the point"
    return None


def _defeat_eq_bool(context: FileContext, proposal: Proposal) -> str | None:
    lowered = context.source.lower()
    for marker in ("sqlalchemy", "django.db", "import pandas", "sqlmodel"):
        if marker in lowered:
            return f"the file imports {marker}, where `== True` builds a query expression"
    return None


def _defeat_swallowed_exception(context: FileContext, proposal: Proposal) -> str | None:
    comment = context.comment_near(proposal.line, span=2)
    if any(word in comment for word in ("best effort", "optional", "ignore", "cleanup")):
        return f"a nearby comment explains the intent: '{comment[:60]}'"
    return None


def _defeat_mutate_while_iterating(context: FileContext, proposal: Proposal) -> str | None:
    """Mutating a list and then leaving the loop is the correct idiom, not the bug.

    `for x in xs: if match(x): xs.remove(x); break` never iterates over the mutated
    list, because it stops. The rule had no entry in `_DEFEATERS` at all, so it could
    never be retracted - inside a module whose whole premise is "a proposal survives
    only if no defeater applies". A rule that cannot lose is not being verified.
    """
    if context.tree is None:
        return None
    for loop in ast.walk(context.tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor)):
            continue
        for block in _blocks(loop):
            for index, statement in enumerate(block):
                mutates = any(
                    getattr(node, "lineno", None) == proposal.line
                    for node in ast.walk(statement)
                    if isinstance(node, ast.Call)
                )
                if not mutates:
                    continue
                after = block[index:]
                if any(isinstance(later, (ast.Break, ast.Return)) for later in after):
                    return "the loop stops immediately after mutating, so it never iterates on"
    return None


def _blocks(node: ast.AST) -> list[list[ast.stmt]]:
    """Every statement list inside `node`, so a statement's own block can be found."""
    out: list[list[ast.stmt]] = []
    for inner in ast.walk(node):
        for name in ("body", "orelse", "finalbody"):
            block = getattr(inner, name, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                out.append(block)
    return out


_DEFEATERS = {
    "mutable-default": _defeat_mutable_default,
    "bare-except": _defeat_bare_except,
    "swallowed-exception": _defeat_swallowed_exception,
    "eq-none": _defeat_eq_none,
    "eq-bool": _defeat_eq_bool,
    "open-without-with": _defeat_open_without_with,
    "assert-in-source": _defeat_assert_in_source,
    "mutate-while-iterating": _defeat_mutate_while_iterating,
}


def verify(context: FileContext, proposal: Proposal) -> Verdict:
    """Try every defeater. Confirmed only if none applies."""
    acknowledged = _acknowledged(context, proposal)
    if acknowledged:
        return Verdict(proposal, False, acknowledged)

    defeater = _DEFEATERS.get(proposal.rule)
    if defeater is not None:
        reason = defeater(context, proposal)
        if reason:
            return Verdict(proposal, False, reason)

    return Verdict(proposal, True, "no defeater applied")


def verify_all(context: FileContext, proposals: list[Proposal]) -> list[Verdict]:
    return [verify(context, p) for p in proposals]
