<h1 align="center">repo-recon (Python · AST · MCP server · zero dependencies)</h1>
<p align="center"><i>Four readers for a Python codebase you did not write — structure, impact, modernisation risk, release readiness — behind one MCP server that writes nothing and runs only `git log` and `git diff`</i></p>

<!-- mcp-name: io.github.hammasbuilds/repo-recon -->

<p align="center">
  <a href="#install">Install</a> &middot;
  <a href="#the-six-tools">The six tools</a> &middot;
  <a href="#mechanical-is-not-behavioural">Mechanical is not behavioural</a> &middot;
  <a href="#the-denominator-is-the-measurement">The denominator</a> &middot;
  <a href="#output">Output</a> &middot;
  <a href="#what-it-will-not-do">What it will not do</a>
</p>

<p align="center">
  <a href="https://github.com/hammasbuilds/repo-recon/actions/workflows/ci.yml"><img src="https://github.com/hammasbuilds/repo-recon/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/dependencies-zero-success" alt="deps">
  <img src="https://img.shields.io/badge/MCP-stdio%20server-7c3aed" alt="mcp">
  <img src="https://img.shields.io/badge/tools-6%20read--only-0ea5e9" alt="tools">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="license"></a>
</p>

---

## Install

No dependencies, so nothing to resolve:

```bash
pip install -e .
repo-recon-mcp --check     # prints what it exposes and exits
```

Add it to an MCP client — Claude Code, or anything that speaks the protocol:

```json
{
  "mcpServers": {
    "repo-recon": {
      "command": "repo-recon-mcp",
      "env": { "REPO_RECON_ROOT": "/path/to/your/code" }
    }
  }
}
```

`REPO_RECON_ROOT` is optional and worth setting. A filesystem path is this server's
entire input, so it is the one thing worth bounding; every path argument is resolved and
refused if it falls outside. See [SECURITY.md](SECURITY.md).

## The six tools

| Tool | Answers | From |
|---|---|---|
| **`map_repo`** | what is in here, what depends on what, where to start reading | the AST, PageRank over the import graph |
| **`trace_impact`** | what breaks if this definition changes | reversed call edges, capped at three hops |
| **`check_modernisation`** | what can be modernised, and what that would change the *meaning* of | AST rewrite rules, two classes kept apart |
| **`review_diff`** | which review findings survive an attempt to disprove them | propose, verify against full context, retract |
| **`release_readiness`** | is this safe to ship | churn, spread and test-coupling against this repo's own median |
| **`riskiest_commits`** | which commits carry the most risk, and why | the same factors, with the points each contributed |

Every one is `readOnlyHint: true`, `openWorldHint: false`, and declares an
`outputSchema` for the `structuredContent` it returns — validated against every tool on
every path by the test suite, because a schema nobody checks is a claim rather than a
contract.

## Mechanical is not behavioural

`check_modernisation` returns two counts and never one. A **mechanical** edit cannot
change behaviour: `List[str]` to `list[str]` is the same type at runtime. A
**behavioural** edit can:

```
datetime.datetime.utcnow()  ->  datetime.now(timezone.utc)
```

That is the correct modern form, and it turns a naive datetime into an aware one. Any
caller comparing the result against another naive datetime now raises `TypeError`. Both
edits are "modernisations"; reporting them in one pile is how a tidy-up becomes an
outage, so every behavioural edit carries a note saying what it would change and the
counts are never added together.

## The denominator is the measurement

`map_repo` returns two resolution rates, and which one you read decides whether the
number means anything. Of the call sites in this repository, **28.1%** bind to a
definition here. Of the call sites that *could* — excluding `len`, `print`, and every
method on a third-party object — **87.8%** do.

The first number measures nothing. Most calls in any codebase are to code the repository
does not own, and counting them in the denominator makes every repository look
unmappable. The first version of the underlying tool reported 12% and the fix was the
denominator, not the resolver. `repo_resolution_rate` is the one to read; both are
returned, and the tool description says which is which.

## Output

`python demo.py`, which is where every number above comes from. The AST half is pinned by a test, so it cannot drift from the code. The git half is read from this repository's own history and moves with every commit, so take it as a shape rather than as a figure to check:

```

map_repo - what is in here
--------------------------
33 modules, 429 symbols, 7,641 lines
call graph: 616 of 702 in-scope calls bound to a definition here (87.8%)
  the same number over EVERY call site, builtins included: 28.1% - which measures nothing
core modules by PageRank over the import graph:
  reporecon.captain.history
  reporecon.reviewbot.checks
  reporecon.cartographer.parse
  reporecon.pilot.rules
  reporecon.captain.risk
entry points - called by nothing in this repository:
  reporecon.pilot.rules._Visitor.visit_Subscript
  tests.test_mcp_server.test_a_method_on_a_repo_class_counts_and_resolves
  tests.cartographer.test_graph.graph
  reporecon.pilot.rules._Visitor.visit_Call

trace_impact - what breaks if this changes
------------------------------------------
reporecon.cartographer.parse.parse_repo is reached by 24 definition(s):
  hop_1: reporecon.cartographer.cli._compare_command, reporecon.cartographer.cli._impact_command, reporecon.cartographer.report.build_report, reporecon.mcp_server.ReconServer.trace_impact
  hop_2: demo.main, reporecon.cartographer.cli._map_command, reporecon.mcp_server.ReconServer.map_repo, tests.test_mcp_server.test_a_unique_name_is_not_ambiguous_with_its_longer_neighbour
  hop_3: demo:<toplevel>

check_modernisation - and what it would change
----------------------------------------------
21 files: 0 mechanical, 0 behavioural
  rules available: naive-now, pep585-generics, pep604-optional, pep604-union, utcfromtimestamp-deprecated, utcnow-deprecated
  nothing to modernise - this package is already written for 3.10+

review_diff - only what survived a disproof attempt
---------------------------------------------------
0 proposed, 0 confirmed, 0 retracted

release_readiness - scored from the history, not from opinion
-------------------------------------------------------------
verdict: warn over 17 commits
  pass  source changes without tests: 0 of 13 source-changing commits changed no test file
  warn  riskiest commit: 55.4 against a median of 33.8 for this release (1.6x) - 8b659616 agentic-ai-lab: eleven tools, 376 tests, no dependencies and
  pass  areas touched: 5 top-level areas: (root), .github, docs, src, tests
  warn  commits far above this repo's median: 1 commit(s) over 2860 lines (20x the 143-line median)

riskiest_commits - with the score's components
----------------------------------------------
a normal commit here is 143 lines across 4 files
   55.4  8b659616  agentic-ai-lab: eleven tools, 376 tests, no 
         files 25.0, spread 20.0, volume 10.4
   54.4  f21541b7  Four readers for an unfamiliar codebase, beh
         files 25.0, spread 20.0, volume 9.4
   42.6  df1063a0  Delete the surface nothing reads, and descri
         spread 20.0, files 16.2, volume 5.3
```

## What it will not do

- **No network.** Nothing opens a socket.
- **No writes.** Files are opened for reading. The migration tool *finds* edits and does
  not apply them.
- **Nothing of yours is executed.** Source is parsed with `ast`; the repository under
  analysis is never imported. The only subprocesses are two `git` invocations with fixed
  argument lists.
- **No dependencies.** Standard library only.
- **It is not a security scanner**, and `review_diff`'s surviving findings are a filter
  on noise rather than a guarantee of correctness.

## Where it came from

The four tools were four standalone packages in
[agentic-ai-llm](https://github.com/hammasbuilds/agentic-ai-llm), each with its own
command line and its own tests, and each is still usable that way —
`repo-cartographer`, `migration-pilot`, `release-captain` and `review-bot` are all
installed by this package. Their original READMEs are kept in [docs/](docs). They were
extracted with their history rather than copied, which is why `git log` here predates
this repository.

They are together because they answer one question between them, and because an MCP
server is graded as one artifact: a monorepo of 32 packages has no tool surface a client
can read, and four servers would be four listings of one idea.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

The four tools' own suites, carried over unchanged apart from their import paths, plus
a set that drives the MCP server over a pipe the way a client does: the protocol, the
difference between a protocol error and a correctable tool result, the confinement, and
a check that every tool's output matches its declared schema on every path. Another
pins this README's own numbers to the code, because every repository here that stated a
count in prose eventually stated a wrong one. CI runs all of it on Python 3.10 to 3.13
and then starts the server.

## License

MIT. See [LICENSE](LICENSE).
