# Security

## What this server can and cannot do

- **No network.** None of the four tools opens a socket or makes an outbound request.
  The server talks to its client over stdin/stdout only.
- **No writes.** Every tool is read-only (`readOnlyHint: true`). Files are opened for
  reading and nothing is created, modified or deleted. The migration tool *finds* edits
  and does not apply them; `reporecon.pilot.apply` exists for the command line and is
  not reachable through any tool.
- **Nothing of yours is executed.** Source is parsed with `ast` — no candidate code,
  no test suite, no import of the repository under analysis. The only subprocesses in
  the package are two `git` invocations, in `captain/history.py` and
  `reviewbot/review.py`, each built as a fixed argument list with `shell=False` and a
  timeout, reading history from the repository you named. Nothing interpolates your
  input into a command.
- **No secrets.** No API key or token, and the only environment variable read is
  `REPO_RECON_ROOT`.
- **No dependencies.** Standard library only, so there is no third-party code in the
  supply chain at run time.

## The one thing worth bounding

A filesystem path is this server's entire input, so an MCP client that can call it can
read any source file the user can read. That is the point of the tools, and it is also
the whole attack surface.

Set **`REPO_RECON_ROOT`** to the directory you want it confined to. Every path argument
is resolved and refused if it falls outside, and so is every file the walk *finds*:
`rglob` follows directory symlinks and Windows junctions, so each file is re-checked
against the boundary after resolution. A `.git` file holding `gitdir: <path>` — how
worktrees and submodules are laid out — is refused when it points outside the root,
because that redirects git at a repository the path argument never named. Leaving the
variable unset means no confinement, which is the right default for a developer on
their own machine and the wrong one anywhere else; setting it to an empty value is a
configuration error and the server refuses to start, because the one way to set it
wrong should not be the way that turns it off.

**What the boundary cannot see: a hard link.** `Path.resolve()` resolves symlinks and
junctions and does not resolve hard links, so a hard link created *inside* the root to
a file outside it reads as a file inside the root. That needs write access inside the
root, which is a different attacker from the one this confinement is for — but it is a
real hole and the sentence above would otherwise imply it is not.

Two further limits are always on, in `cartographer.parse` where the walking happens, so
every tool gets them: at most 4,000 Python files per call and 2,000,000 characters per
file. One call cannot walk a whole disk or be made to read a multi-gigabyte generated
module. `map_repo` reports `truncated: true` when the first limit fires and records the
second as a parse error against the file, so a short answer is never mistaken for a
complete one.

## What it is not

It is not a security scanner. `check_modernisation` reports edits that are *available*,
and splits them into ones that cannot change behaviour and ones that can — it does not
tell you whether a change is safe for your callers. `review_diff` reports findings that
survived an automated attempt to disprove them, which is a filter on noise and not a
guarantee of correctness.

## Reporting a problem

Open an issue at <https://github.com/hammasbuilds/repo-recon/issues>. For anything you
would rather not post publicly, say so in an issue without the details and a private
channel will be arranged.
