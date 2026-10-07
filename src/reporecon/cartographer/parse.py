"""Parse a Python repository into symbols, imports and call sites.

Uses the standard library `ast` module rather than tree-sitter. For Python this is
both cheaper and more accurate: `ast` gives real scope nesting, so a method's
qualified name is known exactly rather than inferred from indentation.

Nothing here calls a model. The whole graph is built deterministically, which is
the point of the project - see README, "AST beats embeddings for structure".
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# Directories that are never source: virtualenvs, caches, build output, vendored code.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".eggs",
        "site-packages",
        # Working directories for checked-out third-party repos.
        "workdirs",
        "vendor",
        "vendored",
        "third_party",
        "_vendor",
    }
)

# A directory holding one of these is the root of some *other* project.
PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")

# Stands in for a call receiver that has no static name, e.g. a literal.
UNNAMEABLE = "<expr>"


@dataclass(frozen=True)
class Symbol:
    """A definition a reader can jump to."""

    qualname: str  # "module.Class.method"
    name: str  # "method"
    kind: str  # "function" | "class" | "method"
    module: str  # "package.module"
    path: str  # repo-relative posix path
    line: int
    end_line: int
    is_public: bool
    docstring: str | None = None
    decorators: tuple[str, ...] = ()

    @property
    def span(self) -> int:
        return self.end_line - self.line + 1


@dataclass(frozen=True)
class ImportEdge:
    """One import statement, resolved as far as statically possible."""

    module: str  # the importing module
    target: str  # the imported module, absolute where resolvable
    names: tuple[str, ...]  # names BOUND in the importing module ("*" for star-imports)
    line: int
    is_relative: bool
    is_internal: bool  # target lives inside this repo
    #: The names as written in the module imported from, where they differ from the
    #: names bound here. `from pkg.lib import helper as h` binds `h` and comes from
    #: `helper`, and a resolver needs both: the first to match what the call site wrote,
    #: the second to find the definition.
    originals: tuple[str, ...] = ()


@dataclass(frozen=True)
class CallSite:
    """A call, recorded unresolved. Resolution happens in graph.py."""

    caller: str  # qualname of the enclosing definition, or "module:<toplevel>"
    callee: str  # the name as written, e.g. "json.loads" or "helper"
    module: str
    line: int


@dataclass(frozen=True)
class Binding:
    """`engine = Engine()` - a local name and the class it was built from.

    The one kind of type inference this does. Without it every `engine.run()` is a
    method on a variable of unknown type, which is neither resolvable nor countable,
    and a repository written in classes resolves almost nothing while reporting that
    almost everything resolved.
    """

    scope: str  # qualname of the enclosing definition
    name: str  # the local variable
    cls: str  # the class name as written at the call to the constructor
    line: int


@dataclass
class ParsedModule:
    module: str
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportEdge] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    loc: int = 0
    parse_error: str | None = None


def module_name_for(path: Path, root: Path) -> str:
    """Derive a dotted module name from a file path.

    `src/` and `pkg/` layout both collapse to the same dotted name, so
    `src/cartographer/parse.py` becomes `cartographer.parse`.
    """
    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    if not parts:
        return path.stem
    parts[-1] = Path(parts[-1]).stem
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def find_nested_projects(root: Path) -> list[Path]:
    """Directories below root that are the root of a different project.

    A checked-out dependency carries its own `pyproject.toml`. Without this, a
    repo that vendors one library maps mostly that library: on `mcp-lab`, whose
    SWE-bench project checks out `requests`, a naive walk found 398 modules of
    which the large majority were not mcp-lab's code at all.
    """
    nested: list[Path] = []
    for marker in PROJECT_MARKERS:
        for found in root.rglob(marker):
            parent = found.parent
            if parent == root:
                continue  # this repo's own marker
            if any(part in SKIP_DIRS for part in parent.relative_to(root).parts):
                continue  # already excluded by name
            nested.append(parent)
    # Keep only the outermost of any nested chain.
    out: list[Path] = []
    for d in sorted(set(nested), key=lambda p: len(p.parts)):
        if not any(d.is_relative_to(seen) for seen in out):
            out.append(d)
    return out


#: How many files one walk reads, and how large a file it parses. These lived only in
#: the MCP server and were applied by two of its six tools, while SECURITY.md said
#: "two further limits are always on ... so one call cannot walk a whole disk or be
#: made to read a multi-gigabyte generated module". `map_repo` and `trace_impact` come
#: through here and had neither. A limit described as always on belongs where the
#: walking happens, not beside the two callers that remembered it.
FILE_LIMIT = 4_000
SOURCE_LIMIT = 2_000_000


def iter_python_files(
    root: Path, limit: int | None = None
) -> tuple[list[Path], list[Path], bool, list[tuple[Path, str]]]:
    """Every .py file that belongs to this repo, up to `limit`.

    Returns (files, excluded_projects, truncated, unreadable) so the caller can report
    what was skipped rather than silently shrinking the map.
    """
    # Read at call time, not bound as a default: a default is captured when the
    # function is defined, so a caller or a test that sets FILE_LIMIT on the module
    # changes nothing and measures nothing. That exact mistake cost a whole parameter
    # sweep in a sibling repository.
    limit = FILE_LIMIT if limit is None else limit
    nested = find_nested_projects(root)
    root = root.resolve()
    out: list[Path] = []
    unreadable: list[tuple[Path, str]] = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if any(p.is_relative_to(d) for d in nested):
            continue
        # `rglob` follows directory symlinks and Windows junctions, so a link inside
        # the tree pulls files from outside it into the map - which, behind the MCP
        # server's REPO_RECON_ROOT confinement, read a file the root was meant to
        # exclude and reported it as a module of this repository. A file is part of
        # this repo only if it is still under the root once resolved.
        try:
            if not p.resolve().is_relative_to(root):
                continue
        except OSError as exc:
            # A broken link, which used to be dropped in silence. A file missing from
            # the map is the kind of thing the map should say it is missing.
            unreadable.append((p, f"unreadable: {exc.strerror or exc}"))
            continue
        out.append(p)
    found = sorted(out)
    return found[:limit], nested, len(found) > limit, unreadable


def _decorator_name(node: ast.expr) -> str:
    """Flatten a decorator expression to a dotted name, best effort."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_decorator_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "<expr>"


def _callee_name(node: ast.expr) -> str | None:
    """The written name of a call target, e.g. `json.loads` or `helper`.

    Returns None for calls we cannot name statically, such as `funcs[0]()`.
    Those are counted as unresolvable rather than silently dropped.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        # A receiver we cannot name - `"".join(...)`, `f(x).strip()` - is marked
        # rather than dropped. Returning the bare attribute here was a real bug:
        # `"".join` became the name `join`, which then looked like an unresolved
        # call to a repo function and inflated the genuine-miss list.
        return f"{base}.{node.attr}" if base else f"{UNNAMEABLE}.{node.attr}"
    return None


class _Visitor(ast.NodeVisitor):
    """Walks one module, tracking the enclosing definition as it goes."""

    def __init__(
        self,
        module: str,
        path: str,
        internal_prefixes: frozenset[str],
        is_package: bool = False,
    ):
        self.module = module
        self.path = path
        self.internal_prefixes = internal_prefixes
        self.is_package = is_package
        self.symbols: list[Symbol] = []
        self.imports: list[ImportEdge] = []
        self.calls: list[CallSite] = []
        self.bindings: list[Binding] = []
        self._scope: list[str] = []  # enclosing def/class names
        self._class_depth = 0

    # -- scope helpers -------------------------------------------------

    @property
    def _current_caller(self) -> str:
        if not self._scope:
            return f"{self.module}:<toplevel>"
        return f"{self.module}.{'.'.join(self._scope)}"

    def _qualname(self, name: str) -> str:
        parts = [self.module, *self._scope, name]
        return ".".join(p for p in parts if p)

    # -- definitions ---------------------------------------------------

    def _record_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif self._class_depth > 0:
            kind = "method"
        else:
            kind = "function"

        self.symbols.append(
            Symbol(
                qualname=self._qualname(node.name),
                name=node.name,
                kind=kind,
                module=self.module,
                path=self.path,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                is_public=not node.name.startswith("_"),
                docstring=ast.get_docstring(node),
                decorators=tuple(_decorator_name(d) for d in node.decorator_list),
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record_def(node)
        self._scope.append(node.name)
        # A nested function is not a method of the enclosing class.
        saved, self._class_depth = self._class_depth, 0
        self.generic_visit(node)
        self._class_depth = saved
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record_def(node)
        self._scope.append(node.name)
        saved, self._class_depth = self._class_depth, 0
        self.generic_visit(node)
        self._class_depth = saved
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._record_def(node)
        self._scope.append(node.name)
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1
        self._scope.pop()

    # -- imports -------------------------------------------------------

    def _is_internal(self, target: str) -> bool:
        head = target.split(".", 1)[0]
        return head in self.internal_prefixes

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append(
                ImportEdge(
                    module=self.module,
                    target=alias.name,
                    names=(alias.asname or alias.name,),
                    line=node.lineno,
                    is_relative=False,
                    is_internal=self._is_internal(alias.name),
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        target = node.module or ""
        is_relative = (node.level or 0) > 0
        if is_relative:
            # Resolve "from .sibling import x" against the current package.
            #
            # An `__init__.py` already *is* its package - `module_name_for`
            # strips the `__init__` part - so one fewer level is dropped for it.
            # Getting this wrong sent `from .normalize import ...` inside
            # `urdunlp/__init__.py` to a top-level `normalize` that does not
            # exist, and took that repo's call resolution down to 24%.
            pkg = self.module.split(".")
            levels = node.level - 1 if self.is_package else node.level
            base = pkg[: max(0, len(pkg) - levels)]
            target = ".".join([*base, target]) if target else ".".join(base)

        self.imports.append(
            ImportEdge(
                module=self.module,
                target=target,
                # The name BOUND here, which is the asname when there is one. This read
                # `a.name` - the name in the module being imported from - so
                # `from pkg.lib import helper as h` put `helper` in the index while the
                # call site writes `h()`, and the call resolved to nothing.
                names=tuple(a.asname or a.name for a in node.names),
                originals=tuple(a.name for a in node.names),
                line=node.lineno,
                is_relative=is_relative,
                is_internal=is_relative or self._is_internal(target),
            )
        )
        self.generic_visit(node)

    # -- local types ----------------------------------------------------

    def _bind(self, target: ast.expr, value: ast.expr | None, line: int) -> None:
        """Record `name = ClassName(...)`, and `name: ClassName` with or without a value.

        Deliberately shallow: one target, a bare or dotted constructor call, no
        reassignment tracking and no branches. A name assigned twice from two classes
        keeps the first binding, which is why the resolver treats a binding as a
        candidate rather than as proof - it still has to find a class of that name with
        a method of that name.
        """
        if not isinstance(target, ast.Name):
            return
        cls = ""
        if isinstance(value, ast.Call):
            cls = _callee_name(value.func) or ""
        if not cls:
            return
        self.bindings.append(
            Binding(scope=self._current_caller, name=target.id, cls=cls, line=line)
        )

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if len(node.targets) == 1:
            self._bind(node.targets[0], node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        annotation = _callee_name(node.annotation)
        if isinstance(node.target, ast.Name) and annotation:
            self.bindings.append(
                Binding(
                    scope=self._current_caller,
                    name=node.target.id,
                    cls=annotation,
                    line=node.lineno,
                )
            )
        elif isinstance(node.target, ast.Name):
            self._bind(node.target, node.value, node.lineno)
        self.generic_visit(node)

    # -- calls ---------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _callee_name(node.func)
        if name:
            self.calls.append(
                CallSite(
                    caller=self._current_caller,
                    callee=name,
                    module=self.module,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)


def parse_module(path: Path, root: Path, internal_prefixes: frozenset[str]) -> ParsedModule:
    """Parse one file. A syntax error is recorded, never raised.

    Real repositories contain files that do not parse under the running
    interpreter - Python 2 leftovers, templates with placeholder syntax. Refusing
    to map a repo because one file is broken would make the tool useless on
    exactly the unfamiliar codebases it exists for.
    """
    module = module_name_for(path, root)
    rel = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return ParsedModule(module=module, path=rel, parse_error=f"unreadable: {exc}")
    if len(source) > SOURCE_LIMIT:
        # Recorded as a parse error rather than truncated: half a file parses into a
        # different program, and a module reported with half its symbols is worse than
        # one reported as unread.
        return ParsedModule(
            module=module,
            path=rel,
            loc=source.count("\n") + 1,
            parse_error=f"{len(source):,} characters; the limit is {SOURCE_LIMIT:,}",
        )

    parsed = ParsedModule(module=module, path=rel, loc=source.count("\n") + 1)
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        parsed.parse_error = f"syntax error line {exc.lineno}: {exc.msg}"
        return parsed

    visitor = _Visitor(
        module=module,
        path=rel,
        internal_prefixes=internal_prefixes,
        is_package=path.name == "__init__.py",
    )
    visitor.visit(tree)
    parsed.symbols = visitor.symbols
    parsed.imports = visitor.imports
    parsed.calls = visitor.calls
    parsed.bindings = visitor.bindings
    return parsed


def detect_internal_prefixes(root: Path, files: list[Path]) -> frozenset[str]:
    """Top-level package names that belong to this repo.

    Anything else an import mentions is third-party or stdlib. Getting this wrong
    in either direction corrupts the graph: too narrow and internal edges vanish,
    too wide and `os` becomes a repo module.
    """
    prefixes: set[str] = set()
    for f in files:
        rel = f.relative_to(root)
        parts = list(rel.parts)
        if parts and parts[0] in {"src", "lib"}:
            parts = parts[1:]
        if len(parts) >= 2:
            prefixes.add(parts[0])  # a package directory
        elif len(parts) == 1:
            prefixes.add(Path(parts[0]).stem)  # a top-level module
    return frozenset(prefixes)


@dataclass
class ParsedRepo:
    root: Path
    modules: list[ParsedModule]
    excluded_projects: list[str]  # repo-relative paths of vendored projects
    #: True when the walk hit FILE_LIMIT, so everything measured from it is a floor.
    truncated: bool = False

    @property
    def symbols(self) -> list[Symbol]:
        return [s for m in self.modules for s in m.symbols]

    @property
    def parse_errors(self) -> list[tuple[str, str]]:
        return [(m.path, m.parse_error) for m in self.modules if m.parse_error]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def parse_repo(root: Path) -> ParsedRepo:
    """Parse every Python file that belongs to this repository."""
    root = root.resolve()
    files, nested, truncated, unreadable = iter_python_files(root)
    internal = detect_internal_prefixes(root, files)
    with warnings.catch_warnings():
        # Vendored and legacy files raise SyntaxWarning on invalid escapes.
        warnings.simplefilter("ignore", SyntaxWarning)
        modules = [parse_module(f, root, internal) for f in files]
    modules += [
        ParsedModule(module=module_name_for(f, root), path=_relative(f, root), parse_error=why)
        for f, why in unreadable
    ]
    return ParsedRepo(
        root=root,
        modules=modules,
        excluded_projects=[d.relative_to(root).as_posix() for d in nested],
        truncated=truncated,
    )
