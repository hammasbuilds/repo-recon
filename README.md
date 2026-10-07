<h1 align="center">repo-recon (Python · AST · MCP server · zero dependencies)</h1>
<p align="center"><i>Six readers for a Python codebase you did not write — structure, impact, modernisation risk, release readiness — behind one MCP server that writes nothing and whose only subprocesses are `git log`, `git rev-parse` and `git ls-files`</i></p>

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

`server.json` is the [MCP Registry](https://registry.modelcontextprotocol.io) manifest.
It declares `registryType: "pypi"` at the package's current version, which is what the
registry needs and is **not** a statement that the upload has happened - this package
is not on PyPI. The order matters: submitted to the registry before the release, every
client that installs from it fails at `uvx repo-recon`. **Upload to PyPI first, then
submit the manifest.** `tests/test_manifest.py` keeps the declared version equal to
`pyproject.toml`'s, so the manifest cannot drift ahead of the package it names - but
nothing in this repository can tell you whether the upload has been done, which is why
the order is written here.

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
number means anything. Of the call sites in this repository, **28.9%** bind to a
definition here. Of the call sites that *could* — excluding `len`, `print`, and every
method on a third-party object — **93.7%** do.

The first number measures nothing. Most calls in any codebase are to code the repository
does not own, and counting them in the denominator makes every repository look
unmappable. The first version of the underlying tool reported 12% and the fix was the
denominator, not the resolver. `repo_resolution_rate` is the one to read; both are
returned, and the tool description says which is which.

## Output

`python demo.py`, which is where every number above comes from. The AST half is pinned by a test, so it cannot drift from the code. The git half **names the commit it was measured at** and is re-derivable from it: four tests check that the revision is a commit here, that it is an ancestor of HEAD, that re-running `release_readiness` at it reproduces the printed verdict and count, and that it has not fallen too far behind. This paragraph used to say the git half "moves with every commit, so take it as a shape rather than as a figure to check" - which was true, and was the defect: a figure that is only true "currently" cannot be checked by anyone, including its own suite.

This tool measures the repository it lives in, so every commit that adds a test moves the symbol and line counts and both resolution rates - and the test asserting the README matches the code fails on the commit that adds a test, correctly. `python demo.py --update-readme` rewrites this block and the two rates above it from one run, so the remedy is a command rather than four figures hand-edited in two places. A separate test re-runs the writer against a current README and fails if it changes anything, which is what makes "this block is that command's output" a checkable claim rather than a promise:

```
map_repo - what is in here
--------------------------
52 modules, 625 symbols, 13,273 lines
call graph: 1026 of 1095 in-scope calls bound to a definition here (93.7%)
  the same number over EVERY call site, builtins included: 28.9% - which measures nothing
core modules by PageRank over the import graph:
  reporecon.captain.history
  reporecon.cartographer.parse
  reporecon.mcp_server
  reporecon.reviewbot.checks
  reporecon.captain.risk
entry points - called by nothing in this repository:
  reporecon.reviewbot.verify.FileContext.build
  reporecon.captain.risk.RiskFactors.explain
  reporecon.captain.risk.RiskFactors.score
  reporecon.cartographer.graph.RepoGraph.fan_in
  reporecon.cartographer.report.RepoReport.to_json

trace_impact - what breaks if this changes
------------------------------------------
reporecon.cartographer.parse.parse_repo is reached by 46 definition(s):
  hop_1: reporecon.cartographer.cli._compare_command, reporecon.cartographer.cli._impact_command, reporecon.cartographer.report.build_report, reporecon.mcp_server.ReconServer.trace_impact
  hop_2: demo._run, reporecon.cartographer.cli._map_command, reporecon.mcp_server.ReconServer.map_repo, tests.cartographer.test_annotated_parameters_resolve.test_a_call_on_an_annotated_parameter_resolves
  hop_3: demo.main, tests.test_mcp_server.test_a_bad_integer_argument_says_the_range, tests.test_mcp_server.test_a_link_inside_the_root_does_not_reach_outside_it, tests.test_mcp_server.test_nothing_is_written_while_the_tools_run

check_modernisation - and what it would change
----------------------------------------------
21 of 21 files read: 0 mechanical, 0 behavioural
  rules available: naive-now, pep585-generics, pep604-optional, pep604-union, utcfromtimestamp-deprecated, utcnow-deprecated
  nothing to modernise - this package is already written for 3.10+

review_diff - only what survived a disproof attempt
---------------------------------------------------
2 proposed, 2 confirmed, 0 retracted
  line 966: assert-in-source - assert is removed under `python -O`, so this check may not run
  line 1059: swallowed-exception - `except NotAGitRepository: return` discards the error and continues

the same checks over four functions written to exercise them:
  5 proposed: 2 confirmed, 2 disproved, 1 acknowledged
  kept     line 1: mutable-default
  kept     line 25: swallowed-exception
  dropped  line 7: mutable-default - the default is never mutated inside count(); sharing it between calls cannot be observed
  dropped  line 13: mutable-default - the author marked it: 'noqa: B006' on line 13
  dropped  line 24: open-without-with - the handle is consumed immediately and not retained

release_readiness - the whole history audited, not a release decision
---------------------------------------------------------------------
verdict: warn over 29 commits
  measured at b9fd5b0eb9a9
  pass  source changes without tests: 0 of 19 source-changing commits changed no test file
  warn  riskiest commit: 55.8 against a median of 32.0 for this release (1.7x) - 8b659616 agentic-ai-lab: eleven tools, 376 tests, no dependencies and
  pass  areas touched: 5 top-level areas: (root), .github, docs, src, tests
  warn  commits far above this repo's median: 2 commit(s) over 2360 lines (20x the 118-line median)

riskiest_commits - with the score's components
----------------------------------------------
a normal commit here is 118 lines across 4 files
   55.8  8b659616  agentic-ai-lab: eleven tools, 376 tests, no 
         files 25.0, spread 20.0, volume 10.8
   54.8  f21541b7  Four readers for an unfamiliar codebase, beh
         files 25.0, spread 20.0, volume 9.8
   42.8  df1063a0  Delete the surface nothing reads, and descri
         spread 20.0, files 16.2, volume 5.5
```

## What it will not do

- **No network.** Nothing opens a socket.
- **No writes through the MCP server.** Every one of the six tools opens files for
  reading, and `tests/test_mcp_server.py` proves it rather than asserting it: it
  fingerprints every file under a repository by size and modification time, calls all
  six tools, and diffs. `check_modernisation` *finds* edits and no tool applies them.

  The command line is a different surface. `migration-pilot apply --write` rewrites the
  files it was pointed at - that is what it is for - and it is reachable from a shell,
  not from a client. This line used to read "No writes. Files are opened for reading",
  which was true of the server and false of a command this package installs.
- **Nothing of yours is executed.** Source is parsed with `ast`; the repository under
  analysis is never imported. Every subprocess the package starts is `git`, with a fixed
  argument list and `shell=False`. Counted by spying on `subprocess.run`, the server
  runs **five** per history call — `rev-parse --verify`, `log`, `rev-parse
  --absolute-git-dir`, `rev-parse --is-shallow-repository` and `ls-files -z` — and
  **six** when `REPO_RECON_ROOT` is set, because the confinement check asks git which
  repository it is about to read rather than parsing `.git`. None at all for `map_repo`,
  `trace_impact`, `check_modernisation` or `review_diff`.

  This line said "two `git` invocations", which is the number of call sites in the source
  and not the number a call makes; it then said four, and `ls-files` had already made it
  five. `tests/test_subprocess_surface.py` counts them now, so the next one to arrive
  fails a test instead of a review.
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
uv sync --all-groups   # no runtime dependencies; the dev group is pytest,
                       # ruff and jsonschema
uv run pytest -q
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
