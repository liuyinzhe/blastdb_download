# Contributing

Thanks for looking at this project.  Issues and pull requests may be written in
**English or Chinese** — both are fine, and neither is expected to be perfect.

This document is deliberately short: the useful part is the list of invariants
below, each of which is guarded by a test that will tell you precisely what you
broke.

## Ground rules

1. **No third-party Python packages.**  The tool must keep running from a bare
   Python 3.9+ installation: every import comes from the standard library.
   Optional *external programs* (`aria2c`, `quota`, `df`, `blastdbcmd`) are
   allowed only when their absence is handled gracefully — never as a hard
   requirement.  If you need a library, that is a design discussion first.
   `test_blastdb_download.py` does not police imports, so this one is on you;
   `python3 -c "import ast,sys;..."` in CI would, if we had CI.
2. **The tool never installs a set it cannot prove.**  When in doubt, fail
   closed: refuse the installation and leave `current` untouched.  A refusal is
   always preferable to a mixed database, because a mixed database produces
   wrong results *silently*.
3. **A local leftover is never trusted just because its size matches.**  Content
   identity needs a checksum (or a probe recorded at install time).  "Same size"
   has already caused one real bug (see `CHANGELOG.md`, 1.3.1).
4. **Progress and diagnostics go to a log file; stdout carries data only.**
   Anything a pipeline may parse belongs on stdout, behind `--json`.

## Running the tests

```bash
python3 test_blastdb_download.py -v          # 101 cases, ~3 minutes, no network
python3 test_blastdb_download.py TestResume  # one class
```

The suite never touches the network: it starts a local HTTP server that serves a
*deterministic* fake BLAST tree, and drives the real CLI as a subprocess.  It can
therefore reproduce things a real source will not do on demand — a release being
published mid-transfer, a torn volume set, a server that refuses payloads, a
process killed halfway, a spilling disk.

Useful habits:

- Add a test that **fails before your fix and passes after it.**  I keep the
  regression tests in the class that matches the area (`TestTornUpdate`,
  `TestResume`, `TestAria2Backend`, …).
- Assert on *bytes and files*, not on log strings, whenever the claim is about
  transfer behaviour (`self.fake.served` counts what actually crossed the wire).
- The fake source is meant to be a faithful model of the real layouts.  If you
  find it diverging from reality (it has happened twice: the gzip header carried
  the current time, and archives did not carry the shared `.ndb` blob), fix the
  fake first — a wrong model hides real bugs.

## Invariants and the tests that guard them

| Invariant | Guarded by |
|---|---|
| Every installed set is one build: embedded timestamps agree, ordinals are `0..N-1`, names match ordinals, `.njs`/metadata agree | `TestSetLevelChecks`, `TestInspect` |
| A source that publishes mid-transfer never results in an install | `TestTornUpdate` |
| Files without a published checksum are never reused or salvaged | `TestTornUpdate.test_a_checksumless_file_is_never_salvaged_across_revisions` |
| Installing one database never destroys another database's staging | `TestResume.test_…` (staging scope) |
| Interrupted transfers resume, and resume cannot cross revisions | `TestResume`, `TestStagingSalvage`, `TestPartialBatchRecovery` |
| Ctrl-C stops promptly instead of draining the queue | `TestResume.test_interrupt_does_not_drain_the_queued_chunks`, `…stop_flag_aborts…` |
| aria2 and built-in backends behave identically (checksum per job, staging placement, corrupt output removed) | `TestAria2Backend` |
| Failures are grouped by reason, retryable ones retried, fatal ones fast-failed | `TestRetryPolicy`, `TestFailureReporting` |
| Logging: default `<root>/log`, `--console` semantics, one line per finished file | `TestLoggingModes`, `TestLoggingAndProgress` |
| A derived upstream artifact (`-metadata.json`) is an authority **only** when it claims the same build as the volume fingerprints; a foreign copy is a warning, and it never excuses a broken set | `TestStaleUpstreamMetadata` |
| The final gate verifies a snapshot entry by link identity (same inode, size and mtime as the just-verified staging file) or falls back to hashing it | `TestLinkIdentity` |
| `gc` reports the space it actually frees - bytes shared with a surviving snapshot by hard link are not counted | `TestSnapshots.test_gc_reports_only_the_bytes_it_actually_frees` |
| A closed stdout (`--json | head`/`jq`) exits 0 quietly, and `--json` keeps every dry-run report on stdout as one JSON document | `TestPathAndPipeHandling`, `TestDryRunAndConfig` |
| `~` and `$VARS` in path options and config values are expanded | `TestPathAndPipeHandling` |
| External tool output is parsed in the format that tool actually writes (aria2c tags its log lines with `[file:line]`) | `TestAria2LogParsing` |
| Documentation matches the code: every flag documented, no phantom flags, anchors resolve, both languages, licence consistent | `TestCliContract` |
| The configuration template matches `DEFAULTS` key by key | `TestDryRunAndConfig` |

## Changing behaviour

- **New option**: add it to `global_option_specs()` (or the sub-parser), add it
  to `DEFAULTS`, and document it in **all** READMEs — `TestCliContract` fails
  otherwise, which is intentional.  Never reuse an argparse destination name that
  is not a `DEFAULTS` key: that is exactly the bug that made
  `-k/--min-split-size` dead on arrival (`CHANGELOG.md`, 1.1.0).
- **New check**: prefer evidence that comes from the payload itself (embedded
  fingerprints, `bytes-total`, file lists) over remote claims.  Record where the
  proof came from in the state file (`md5_origin`, `archive`, `adopted_from`).
- **New data source**: implement `Source` (`resolve`, `snapshot_id`,
  `list_databases`, `revision`, `invalidate`).  `revision()` must return the same
  `revkey()` for the same content and a different one for different content; the
  protocol depends on that and nothing else.
- **Changing the state format**: bump `STATE_FORMAT` and keep reading the old
  one, or document that old snapshots must be re-created.  `verify` should keep
  working on snapshots from the previous minor release.

## Release checklist

1. `python3 test_blastdb_download.py` — green.
2. Bump `__version__` in `blastdb_download.py`.
3. Add a `CHANGELOG.md` section (Keep a Changelog; Fixed/Changed/Added, and say
   *why* something was wrong, not just what changed).
4. Update both READMEs (`README.md`, `README.en.md`) and, if paths moved, the
   documentation index.  `README_blastdb_download.md` is a pointer to
   `README.md`; do not turn it back into a copy.
5. `bash -c 'python3 -m py_compile blastdb_download.py test_blastdb_download.py'`
   plus a `ruff check` pass restricted to the correctness rule families
   (F, E9, B) if you have ruff installed.
6. Commit with a message that names the version, as in the existing history.

## Project layout

```
blastdb_download.py         the program: sources, verification, download engines, CLI
test_blastdb_download.py    the test suite and the fake source it runs against
README.md                   the primary (Chinese) manual
README.en.md                the English manual
README_blastdb_download.md  pointer to README.md (kept for existing links)
CHANGELOG.md                release notes
CONTRIBUTING.md             this file
docs/DESIGN.md              protocol, layout, state file, invariants, how to extend
docs/OPERATIONS.md          sizing, monitoring, failure playbook, migration
docs/NCBI_database_download.md  background: the original notes, measured facts, checks
LICENSE                     MIT
```

## Reporting a bug

Please include, where possible:

- the command line (the log file starts with it, which is why `--log-file`
  records it),
- `blastdb_download.py doctor --json`,
- the log tail: the plan, the grouped failure reasons and the retry lines are
  usually enough to identify the cause,
- `blastdb_download.py staging --against-remote --json` when the problem is about
  resuming.

If the problem is a *wrong result* rather than a failure, please run
`blastdb_download.py inspect <db> --dir <mirror> --json` and include it: a mixed
build is the first thing to rule out, and `inspect` answers it offline.
