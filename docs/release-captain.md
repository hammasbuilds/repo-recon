<h1 align="center">release-captain</h1>
<p align="center"><i>Release readiness scored from diff statistics, not from opinion</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="zero dependencies">
  <img src="https://img.shields.io/badge/tests-35-success" alt="tests">
  <img src="https://img.shields.io/badge/repos%20measured-28-orange" alt="repos">
</p>

---

## The finding

**The single largest commit across 28 repositories changed 47,743 lines in 5 files. The
third largest changed 5,442 lines in 693 files. Ranked by lines, the first is nine times
worse; ranked by breadth, the second is a hundred and thirty times worse. They are
opposite kinds of risk and every single-number metric ranks one of them wrongly.**

The first is a regenerated results file - almost no review risk. The second is a broad
structural change - a great deal. A release gate that thresholds on lines changed blocks
the harmless one and waves the dangerous one through.

So risk here is a small weighted vector - breadth, file count, untested source, volume,
net deletion - and the report always names which factors produced the number.

## But the honest version is narrower than that

The obvious follow-up claim is that single metrics disagree in general, so a composite is
always necessary. **Measured across 28 repositories, that is false.** Ranked top-5
overlap between metrics:

| Pair | Mean excess over chance |
|---|---|
| lines vs files | **+25%** |
| lines vs spread | **+21%** |
| files vs spread | **+32%** |

The metrics *agree*, well above chance. On ordinary commits, any one of them would do.
The composite earns its place only on the tail - generated-data commits and broad
refactors - and the tail is exactly what a release gate exists for. `mcp-lab`, the repo
with the most history and the 47,743-line dump in it, is the one repository where lines
and spread overlap **below** chance (20% observed against 28% expected).

Reporting "a composite is always better" would have been a nicer headline and it is not
what the data says.

## The measurement trap this walked into first

The first version of that table reported **75-80% overlap** and I nearly wrote it up as
strong agreement. It was an artefact. With 8 commits in a repository and a top-5
comparison, **chance alone produces 62% overlap** - you are choosing 5 of 8 items twice.
Most of these repositories have fewer than a dozen commits, so the raw number measured
history length, not metric agreement.

Every figure above is excess over the chance baseline `top / n`. Raw overlap is not
reported anywhere, because it is not readable.

## Input / Output

**In:** a git repository.

**Out:**

```
$ captain gate D:\github\mcp-lab --since 8

==============================================================================
  mcp-lab  -  NO-GO
==============================================================================

  8 commit(s) assessed

  CHECKS
    [STOP] source changes without tests
           3 of 3 source-changing commits changed no test file
    [ok  ] riskiest commit
           44.3 - bd2a6df3 Replace three results viewers with Input/Output evidence
    [ok  ] areas touched
           4 top-level areas: (root), docs, projects, scripts
    [warn] commits far above this repo's median
           1 commit(s) over 14780 lines (20x the 739-line median)

  RISKIEST COMMITS  (composite: spread, files, untested, volume, deletion)
     47.8  5a1c9e02    5442L  693f 3dir  Add project 01: MCP red-team platform
     44.3  bd2a6df3     418L   11f 3dir  Replace three results viewers with Inp
     38.6  8f43c84f    2831L   18f 3dir  Add project 04: SWE-bench coding agent
==============================================================================
```

Also:

```
$ captain explain <repo> <sha>     # the factor breakdown behind one score
$ captain rank <repo> --by churn   # or files, spread, risk - see them disagree
$ captain compare D:\github        # the agreement table above
$ captain gate <repo> --strict     # exit 1 when blocked, for CI
```

## Applied to the portfolio

Running the gate over the last 8 commits of all 28 repositories:

| Verdict | Repositories |
|---|---|
| GO | 3 |
| GO WITH WARNINGS | 20 |
| NO-GO | 5 |

The five blocked - `classical-computer-vision`, `context-bench`, `langgraph-lab`,
`mcp-lab`, `sql-analyst-agent` - are all blocked by the same check: **every recent
source-changing commit changed no test file.** Not a subtle signal, and not one a
line-count threshold would have produced.

## Thresholds are relative to the repository

A 400-line commit is unremarkable where the median is 739 lines and alarming where it is
20. Every size threshold is a multiple of that repository's own median, which is why the
outlier check on `mcp-lab` fires at 14,780 lines rather than at some number chosen in
advance.

## Problems hit while building this

**The risk model ranked an 8-line typo fix third-riskiest in `mcp-lab`, above a 693-file
change.** The `untested` factor was the fraction of changed files that were source, with
no reference to how much source had changed, so any single-file source commit without a
test scored the maximum. Scaling it by the log volume of the source change fixed the
ranking; the 693-file commit now leads and the data dump has dropped out of the top five.

**A test caught my own sloppy reasoning rather than a bug.** `test_volume_is_log_scaled`
asserted diminishing returns per decade. A log scale does not do that - it gives *equal*
steps per decade by construction, and I was comparing one decade against three. The
property worth asserting is compression against linear plus a ceiling, which is what the
test now checks.

**`README.md` and `.gitignore` each counted as a "top-level area".** Splitting a path on
`/` and taking the first element makes every root-level file its own directory, so a
docs-only commit looked as broad as a refactor. Root-level files now group as `(root)`.

**Merge commits are excluded.** Their `--numstat` is relative to a single parent and
double-counts work already attributed to the commits being merged, which inflated churn
in every repository that had one.

## What I wrote vs what I installed

**Installed: nothing.** `dependencies = []`.

`git` is the data source and is already present anywhere this would run. GitPython was
considered and skipped: `git log --numstat` with a custom format is one subprocess call
and a parser, against a dependency that wraps the same command. The web view is served by
`http.server` from the standard library.

## What it does NOT do

- **Does not read commit message conventions.** It was built expecting Conventional
  Commits and the repositories it was measured on use prose subjects, so nothing depends
  on the subject line. Diff statistics are the only input.
- **Weights are declared, not learned.** There is no labelled "this release broke
  production" data here, so `WEIGHTS` encodes a stated position rather than a fitted
  model. They are in one dictionary at the top of `risk.py` and are meant to be argued
  with.
- **No CI integration, no issue tracker, no deploy hook.** It reads history and returns a
  verdict with an exit code.
- **Never writes to the repository.** Every git command is a read.
- **Does not know what your code does.** A one-line change to a payment path and a
  one-line change to a log message score identically.

## Run it

```bash
uv run pytest -q                        # 35 tests
uv run captain gate <repo> --since 8
uv run captain compare D:\github
uv run python ui/serve.py               # web view on :8090
```

## Layout

```
src/captain/
    history.py   git log --numstat parsing, file classification, co-change
    risk.py      the weighted factors and the ranking comparison
    gate.py      the checks, the verdict, and the reason for each
    cli.py       argparse
ui/serve.py      http.server + Alpine.js, no dependency
tests/
    test_history.py  18 tests against real git repositories
    test_risk.py     17 tests
```
