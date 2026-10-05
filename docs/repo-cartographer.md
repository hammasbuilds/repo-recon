<h1 align="center">repo-cartographer</h1>
<p align="center"><i>Map an unfamiliar Python codebase from its AST - no embeddings, no model, no dependencies</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="zero dependencies">
  <img src="https://img.shields.io/badge/tests-32-success" alt="tests">
  <img src="https://img.shields.io/badge/median%20resolution-99%25-orange" alt="resolution">
</p>

---

## Results

**A plain `ast` walk binds 99% of a repository's internal calls to their definitions
(median across 29 real repositories, 519 modules, 88,166 lines). No embeddings, no model,
no index.**

The usual approach to "explain this codebase" is to embed every chunk and retrieve by
similarity. For structural questions - *what calls this, what breaks if I change it, where
does execution start* - that is the wrong tool. Similarity cannot tell a definition from a
mention. Symbol resolution can, and it is exact.

Three of the four repositories below 90% are genuinely hard cases, and the fourth was a bug
in this tool. Both are reported.

## Input / Output

**In:** a path to a Python repository.

**Out:**

```
$ cartographer map ~/code/rag-forge

==============================================================================
  rag-forge  -  34 modules, 128 definitions, 2,071 lines
==============================================================================

  CALL RESOLUTION
    133 of 146 repo-internal calls resolved  (91%)
    624 call sites in total; the rest are builtins,
    the standard library, or methods on third-party objects.

  LOAD-BEARING MODULES  (PageRank over internal imports)
    0.1534  ragforge.types
              src/ragforge/types.py  -  47 lines, imported by 11
    0.1239  ragforge.config
              src/ragforge/config.py  -  53 lines, imported by 11
    0.0391  ragforge.store
              src/ragforge/store/__init__.py  -  27 lines, imported by 6

  MOST-CALLED DEFINITIONS
      12 callers  ragforge.config.get_settings  (src/ragforge/config.py:51)
      10 callers  ragforge.store.get_store  (src/ragforge/store/__init__.py:12)
       9 callers  ragforge.db.connection  (src/ragforge/db.py:37)

  ENTRY POINTS  (public, nothing in the repo calls them)
    ragforge.cli.ask  (src/ragforge/cli.py:25)  - Ask a question against the index.
    ragforge.api.main.ask  (src/ragforge/api/main.py:57)
==============================================================================
```

Also:

```
$ cartographer impact ~/code/rag-forge get_settings   # blast radius of a change
$ cartographer compare ~/code                         # every checkout under a folder
$ cartographer map <repo> --json -o map.json             # for the UI
```

## Measured across 29 repositories

| | |
|---|---|
| Repositories mapped | 29 |
| Modules | 519 |
| Lines | 88,166 |
| **Median repo-call resolution** | **99%** |
| At or above 90% | 25 of 29 |
| Parse failures | 0 |

The four below 90%: `classical-computer-vision` (78%) and `computer-vision` (60%) both use
dynamic dispatch through registry dictionaries, which no AST can follow;
`multimodal-emotion` (75%) is two scripts with almost no internal calls, so one miss moves
the percentage several points; `repo-cartographer` itself (79%) resolves `args.func(args)`
nowhere, because argparse binds that callable at runtime.

## The denominator matters more than the resolver

The first version of this tool reported **12%** resolution on `mcp-lab` and **7%** on
`machine-learning`. Both numbers were meaningless. Most call sites in any Python file are
`len`, `print`, `path.resolve()` or a method on a third-party object - none of which could
*ever* bind to a definition in the repository being mapped.

Counting them in the denominator makes every codebase look unmappable:

| Repository | All call sites | Repo-internal calls only |
|---|---|---|
| machine-learning | 8% | **99%** |
| mcp-lab | 12% | **91%** |
| rag-forge | 21% | **91%** |
| credit-risk-engine | 24% | **100%** |
| langchain-lab | 38% | **98%** |

This is the same mistake `langchain-lab` project 01 found in extraction scoring, where the
standard metric dropped failed extractions from the denominator and made
grammar-constrained decoding look 19 points *worse* than plain prompting when it was three
times better. Same shape, different field: **the resolver was never the problem, the
denominator was.**

Both numbers are reported. `resolution_rate` is every call site; `repo_resolution_rate` is
the honest one.

## What I wrote vs what I installed

**Installed: nothing.** `dependencies = []`. The parser, the call resolver, the PageRank
implementation, the scope handling and the CLI are all standard library. `pytest` and
`ruff` are development-only.

This is not purism. tree-sitter was tried first and beaten by `ast` on the actual task, and
an embedding index was never built because similarity search cannot answer the structural
questions this tool exists for.

## Scope

- **Python only.** No JavaScript, Go or Rust. The `ast` module is the reason it works and
  the reason it does not generalise.
- **No type inference.** `conn.execute(...)` is unresolvable without knowing what `conn` is.
  These are counted as out of scope, not guessed at.
- **No dynamic dispatch.** Registry dictionaries, `getattr`, plugin loading and
  argparse-bound callables are invisible. This is why `computer-vision` sits at 60%.
- **Does not run, import, or install the code it maps.** Parsing only, so mapping a
  repository is safe even when its dependencies are not installed.
- **Does not rank by quality.** PageRank finds what is depended upon, which is not the same
  as what is good.
- **The explanation layer is optional and not the point.** The map is produced with no model
  at all; a model only turns it into prose.

## Run it

```bash
uv run pytest -q                      # 32 tests
uv run cartographer map <repo>
uv run cartographer compare <folder>  # every checkout under it
```

## Layout

```
src/cartographer/
    parse.py    file walk, AST walk, symbols, imports, call sites
    graph.py    call resolution, the honest denominator, PageRank
    report.py   the four questions a newcomer asks
    cli.py      argparse, because the core has no dependencies
tests/
    test_parse.py   18 tests
    test_graph.py   14 tests
```
