<h1 align="center">review-bot</h1>
<p align="center"><i>Propose findings, then try to disprove each one. Report only what survives.</i></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/runtime%20deps-0-brightgreen" alt="zero dependencies">
  <img src="https://img.shields.io/badge/tests-36-success" alt="tests">
  <img src="https://img.shields.io/badge/files%20measured-1%2C325-orange" alt="files">
</p>

---

## The finding

**Across 1,325 source files, 24% of the findings this reviewer would have posted were
wrong — and for two rules, 100% of them were.**

```
SOURCE FILES ONLY: files=1,325  proposed=476  confirmed=362  retracted=114 (24%)

rule                      prop  conf  retracted
assert-in-source           340   340        0%
open-without-with          118    14       88%
mutable-default              6     4       33%
bare-except                  5     0      100%
swallowed-exception          3     0      100%
eq-none                      3     3        0%
mutate-while-iterating       1     1        0%
```

Every `bare-except` in the corpus re-raises after cleanup. Every
`except Exception: pass` has a comment saying the failure is acceptable. Both are
textbook lint findings, both are confidently reportable from the AST alone, and in this
corpus both are wrong every single time.

`open-without-with` is the volume case: 118 proposals, 14 real, because 100 of them were
already inside a `with` statement one line up.

**Precision is the entire product.** A reviewer that posts a plausible-looking wrong
comment gets muted on the second day, and after that its correct findings are worth
nothing either.

## How it works

Two stages, deliberately adversarial:

1. **Proposers are over-eager.** They guess from the AST. A proposer that only fired when
   certain would find almost nothing.
2. **Verifiers try to defeat each proposal.** A finding is reported only if no defeater
   applies. Anything knocked down is recorded *with the reason*, and the retraction rate
   is printed on every run.

Verifiers read the **whole file**, while findings are restricted to lines the diff
**added**. That asymmetry is the design: most false positives are false because of
something the diff did not show — a `raise` three lines below, a `sqlalchemy` import at
the top of the file, a `# noqa` the author already wrote.

## Input / Output

**In:** a git repository and a ref, or a folder of Python files.

**Out:** the findings that survived verification, the ones that did not and why, and the
retraction rate per rule.

```
$ review-bot scan requests/ --show-retracted

  3 finding(s) to report  -  4 of 7 proposals retracted (57%)

  requests\packages\urllib3\packages\ordered_dict.py
    line   197  [bug] mutable-default
              dict used as a default argument in __repr__(); it is created once
              and shared between calls

  RETRACTED  (proposed, then defeated)
    requests\adapters.py:460 bare-except
              defeated: the handler re-raises, so nothing is swallowed
    setup.py:50 open-without-with
              defeated: the call is already inside a `with` statement
```

```
$ review-bot diff <repo> --ref HEAD~1     # review only what a change added
$ review-bot diff <repo> --strict         # exit 1 if anything is reported
$ review-bot rules                        # which proposers have defeaters
$ python ui/server.py                     # API for the Remix UI on :8110
```

## The defeaters

| Rule | What defeats it |
|---|---|
| `mutable-default` | the default is never mutated inside the function, so sharing it is unobservable |
| `bare-except` | the handler re-raises |
| `swallowed-exception` | a nearby comment explains the intent |
| `eq-none`, `eq-bool` | the file imports SQLAlchemy, Django, pandas or similar, where `== None` builds a query expression and `is None` would be wrong |
| `open-without-with` | already inside a `with`, or the handle is consumed immediately |
| `assert-in-source` | the file is a test |
| *any* | the author wrote `noqa`, `nosec`, `intentional`, `deliberate`, `on purpose` or `by design` nearby |

Respecting acknowledgement markers is not politeness. Re-raising something the author
already marked, every run, is precisely how a bot gets turned off.

## A measurement trap worth naming

The first run of this reported **96% retracted** across 1,720 files, which looked like a
spectacular result. It was an artefact: `assert-in-source` fired 9,248 times and 8,906 of
those defeats were "the file is a test". One badly scoped proposer dominated the
denominator entirely.

A review bot does not lint asserts in a test suite, so test files are excluded by default
and the honest number is 24%. The 96% is not reported anywhere except here, as the
mistake it was.

## Problems hit while building this

**`open('x').read()` was flagged 118 times.** Technically the handle is not explicitly
closed; in practice CPython refcounting closes it immediately and the idiom is
everywhere. A rule can be *correct* and still be noise, which is a different failure from
being wrong.

**`assert-in-source` fires 340 times in source files and retracts 0%.** By the numbers it
is this tool's most reliable rule. It is also the least useful one, because 340 correct
low-severity findings is not a review. Precision alone does not make a rule worth having,
and the README says so rather than letting the table imply otherwise.

**Verifying inside the hunk defeated the whole idea.** An early version passed only the
changed lines to the verifiers, so the `raise` below a bare `except` and the SQLAlchemy
import above a `== None` were both invisible and nothing was ever retracted. Proposals
are scoped to the diff; verification never is.

## What I wrote vs what I installed

**Installed: nothing.** `dependencies = []`. `ast` proposes, `subprocess` reads
`git diff`, a small parser handles unified-diff hunks, and the API is `http.server`. Only
the Remix front end needs npm.

No model is involved. A model would write better explanations and would also confirm
findings it had not checked, which is the failure this project is built to avoid.

## What it does NOT do

- **Seven rules.** This is not a linter and does not want to be. The contribution is the
  verification stage, which any rule set could be dropped into.
- **It cannot know intent.** Every defeater is a heuristic, and `mutable-default` in
  particular guesses from whether the parameter is mutated *in that function* — a helper
  that mutates it elsewhere would be missed.
- **No cross-file analysis.** A defeater living in another module is invisible.
- **Python only.**
- **It does not post comments anywhere.** It prints and exits; wiring it to a forge is
  deliberately left out.
- **A 24% retraction rate is not 100% precision.** It means 24% of what would have been
  posted was caught first. The remainder has not been independently validated, and on a
  different codebase every number here would change.

## Run it

```bash
uv run pytest -q                              # 36 tests
uv run review-bot scan <path> --show-retracted
uv run review-bot diff <repo> --ref HEAD~1
uv run python ui/server.py                    # then: cd ui && npm install && npm run dev
```

## Layout

```
src/reviewbot/
    checks.py   over-eager proposers, seven rules
    verify.py   defeaters, one per rule, plus acknowledgement markers
    review.py   diff parsing, scoping, aggregation, rendering
    cli.py      argparse
ui/             Remix front end, http.server API
tests/test_review.py   36 tests, mostly about what it refuses to report
```
