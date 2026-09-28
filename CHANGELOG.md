# Changelog

All notable changes to `blastdb_download.py` are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Reading guide: **Fixed** entries are correctness issues (a downloaded set could
have been wrong, or a failure was misreported). **Security** is not used as a
separate heading here because the whole point of the tool is data integrity, so
integrity fixes are listed under **Fixed**.

## [1.4.7] — 2026-09-28

A fresh review of the corners not audited before: the CLI's stdout contract,
path arguments, and how filesystem failures are named.

### Fixed

- **A closed stdout was reported as a failure.**  `list --json | jq …` (or
  `| head`) makes the reader go away early; the tool answered with `ERROR:
  unexpected BrokenPipeError: [Errno 32] Broken pipe` and exit 1, so a working
  pipeline looked like a failed run.  A closed pipe is now a normal exit (0),
  with stdout pointed at `/dev/null` so the interpreter's final flush cannot
  raise again.
- **`~` and `$VARS` in paths were not expanded.**  `-r ~/db` created a directory
  literally named `~` beside the current working directory, and so did
  `root = "~/db"` in a config file.  Every path-valued option (`--root`,
  `--config`, `--log-file`, `--ncbi-dir`, `--aria2c`, `--adopt*`, `inspect
  --dir`, `config --init --path`) and the config-file values are now expanded
  once, early, through `expand_path()`.
- **`--json` output was polluted by the dry run.**  `--json --dry-run download`
  printed the human-readable plan to stdout, so `… | jq` could not parse it.
  Dry-run reports now go through `emit_dry_run()`: with `--json`, stdout carries
  exactly one JSON document (`download`, `gc` and `config --init` all covered).
- **Filesystem failures had no name an operator could look up.**  An `ENOSPC`,
  `EDQUOT`, `EROFS` or `EACCES` raised outside the transfer monitor surfaced as
  `unexpected OSError: [Errno 28] …`.  `describe_oserror()` now prints exactly
  the sentences the failure playbook in `docs/OPERATIONS.md` lists (`no space left
  on the filesystem`, `filesystem quota exceeded`, `the filesystem is mounted
  read-only`, `permission denied`), including the offending path.

### Changed

- `--progress-interval 0` and `--timeout 0` are clamped (to `>= 1 s`) instead of
  producing one progress line per chunk, respectively a non-blocking socket that
  fails immediately.  `gc_snapshots()` now returns `[(name, freed_bytes), …]`,
  so the summary counts what it really removed.

### Added

- `test_blastdb_download.py`: `TestPathAndPipeHandling` (a closed pipe is not a
  failure; `~` in an option and in a config file is expanded; a filesystem error
  is named the way the playbook lists it) plus two dry-run JSON contract tests
  in `TestDryRunAndConfig`.

### Documentation

- The parameter reference now states what `--no-dedupe-taxonomy` actually keys
  on (name plus size, not content), which is the one place where the tool trusts
  a size - deliberately, to avoid re-extracting hundreds of gigabytes of shared
  taxonomy, and switchable.

## [1.4.6] — 2026-09-28

Re-measuring the `nt` snapshot that started 1.4.3, and two consequences.

### Fixed

- **`gc` promised space it did not return.**  A snapshot shares its unchanged
  files with its neighbours as hard links, so removing it releases only the names
  whose link count drops to zero - the report used the tree size instead.  For
  `nt`, where a release changes a handful of the 345 volumes, almost every
  reported byte was shared with the snapshot that stays.  The lines now report
  the bytes that are actually freed (`freed_bytes()`, `st_nlink == 1`).
- **The metadata cross-check drops shared taxonomy files from *both* sides.**  It
  already ignored them when looking at what is on disk; a cloud snapshot's
  payload-level metadata lists members rather than archives, so a database that
  ships `taxdb.btd` / `taxdb.bti` / `taxonomy4blast.sqlite3` in its `files` list
  would have had them reported as missing and the install refused.  Unreachable
  as measured (no live database metadata lists them, and `taxdb` itself ships no
  metadata file at all - it is exactly three objects: `taxdb.btd`, `taxdb.bti`,
  `taxonomy4blast.sqlite3`), but the rule is now symmetric.

### Documentation

- **Corrected the 1.4.3-1.4.5 claim that the manifest agreed with the payload.**
  Re-measured on `2026-07-21-01-05-02`: the manifest entry *and* the shipped
  `-metadata.json` both carry the *summary* fields `2026-07-20` /
  `1063128812728`, while the payload's own evidence - the 345 volumes' `.nin`
  stamps, `nt.njs` and every per-file md5 - says `2026-07-19` /
  `1074151309400` (1074151365765 bytes on disk).  The file *names* agree
  (3112 = payload + `nt.njs`), so the per-file authority was never wrong; only
  the summary disagreed.  The docs now state that instead of asserting which
  side is older (`CHANGELOG.md` 1.4.3, appendix A.4 of both READMEs,
  `docs/FAQ.md` F7, `docs/DESIGN.md`, `docs/NCBI_database_download.md` §4.4).

### Added

- `test_blastdb_download.py`:
  `TestSnapshots.test_gc_reports_only_the_bytes_it_actually_frees` and
  `TestStaleUpstreamMetadata.test_metadata_listing_a_shared_taxonomy_file_is_not_a_missing_file`.

## [1.4.5] — 2026-09-28

### Changed

- **The final verification pass no longer re-reads a payload it just verified.**
  The snapshot is assembled by *hard-linking* the files the set-level gate
  verified moments earlier in the same run, so a snapshot entry is the same
  inode with the same size and the same modification time as the checked copy
  (`file_identity()`); hashing it again could only repeat the same md5.  `nt` is
  1.07 TB, so this removes a full terabyte of I/O per install - about an hour on
  a 250 MB/s disk.  Anything that is *not* provably that object - a copy from the
  cross-device fallback, a file written or replaced in the meantime, a stale
  identity record - still falls back to hashing, and a payload that cannot be
  proven is reported exactly as before.
- When every file is proven the pass reports itself in one line (`final
  verification: 3113 file(s) proven by link identity, nothing re-read`); when it
  has to read, the two lines from 1.4.4 appear, with an ETA.

### Added

- `test_blastdb_download.py`: `TestLinkIdentity` (4 tests: a hard link is proven
  without being read; a replaced file - new inode, same size, later mtime - is
  hashed again and its changed bytes are caught; the cross-device copy fallback
  is hashed again and accepted; a file without a checksum is never counted).

### Notes

- The identity carries `st_mtime_ns` on purpose: `(dev, inode, size)` alone can
  be fooled by inode *recycling* - the first version of this check matched a
  file that had been deleted and recreated with the same size, which the test
  now pins down.  `link()` does not touch `mtime`, but any write does.
- The proof relies on the documented concurrency contract - one writer per
  mirror root, enforced by the root lock - so staging does not change between
  the two gates.  The same assumption already underpins `--reuse-verify` and the
  trusted state file.

## [1.4.4] — 2026-09-28

Observability for the one phase that had none - the full-payload md5 gates.  No
verification behaviour changed: the same bytes are checked, in the same order,
with the same checksums.

### Added

- **The set-level and final verification passes log a start line and an end
  line.**  Reading every byte of a database is the longest silent phase of a
  large run: `nt` is ~1 TiB, so each pass takes about an hour on a 250 MB/s disk
  while the log says nothing at all - indistinguishable from a hang.  The start
  line states how many files and how many bytes will be read and carries an
  `ETA ~` extrapolated from the first file(s) actually hashed (a sample of at
  least 32 MiB or 1 s, whichever comes first); the end line reports files,
  bytes, elapsed time, the achieved rate and, when something failed, `N checksum
  mismatch(es)`.  There are deliberately no per-file lines (that would be
  thousands of lines for `nt`), and `verify --quick` prints neither line because
  it hashes nothing.
- The two passes are now distinguishable in the log: `set-level verification`
  (before the payload is hard-linked into the snapshot) and `final verification`
  (on the assembled snapshot, before `current` is switched).  A resumed run has
  a third reader, the staging reuse scan, which already logs one line per file.
- `docs/OPERATIONS.md`: the "healthy log" sample shows both pairs of lines, plus
  a note on why the gaps between them are expected and what a resumed run adds.
- `test_blastdb_download.py`: `TestVerifyReporting` (6 tests: both gates during a
  download, `verify` reporting the set-level pass, `--quick` staying silent, a
  checksum mismatch counted in the closing line, and a guard that eight volumes
  still produce four lines instead of one per file).

## [1.4.3] — 2026-09-28

Correctness of the *verdict*, measured against a real mirror: a GCP `nt` set of
1.07 TiB (345 volumes, every byte md5-verified) was refused by the tool's own
cross-check, and the aria2c per-file counter never left `files 0/3113`.

### Fixed

- **A stale upstream `<db>-<type>-metadata.json` no longer blocks a verified
  install.**  The `2026-07-21-01-05-02` snapshot ships an
  `nt-nucl-metadata.json` (and a matching manifest entry) whose *summary* fields
  announce `last-updated 2026-07-20` and `bytes-total 1063128812728`, while the
  payload itself - the 345 volumes' embedded `.nin` stamps, `nt.njs` and every
  per-file md5 - describes `2026-07-19` / `1074151309400` bytes.  The file
  *names* agree (3112 = the payload plus `nt.njs`); only the summary disagrees,
  by one day and ~10.2 GB (re-measured 2026-09-28, see 1.4.6).  The cross-check
  now first asks whether the artifact claims the
  same build date as the volume fingerprints.  If it does, its `files` /
  `bytes-total` / `number-of-volumes` claims stay **fatal** - that is how
  truncation, missing files and extra files are caught without network access.
  If it does not, the artifact is recognised as foreign, its claims become
  **warnings** that name it as stale, and the payload is judged by what was
  actually proven: per-file md5, the manifest's file names, the embedded `.nin`
  fingerprints, contiguous volume ordinals and `<db>.njs`.  Completeness
  enforcement is
  unchanged: a genuinely missing volume still fails (the ordinals must be
  contiguous from 0), and a `bytes-total` disagreement on a matching artifact is
  still fatal.
- **The aria2c completion watcher never matched real aria2c output.**  The
  pattern expected `[NOTICE] Download complete: <path>`, but aria2c 1.37 writes
  `[NOTICE] [RequestGroup.cc:1214] Download complete: <path>`, so completions
  were never recognised and the progress line stayed at `files 0/3113` for a
  whole 1 TiB download (the transfer itself was fine: the batch finished and the
  final md5 gate passed).  The pattern now skips the `[file:line]` tag, and the
  test stub writes the real format so the two cannot drift apart again.

### Added

- `docs/FAQ.md` F7, the README troubleshooting sections (4.7 in Chinese, 17 in
  English) and appendix A.4 of both READMEs: what a foreign payload metadata
  artifact means, why NCBI's per-database metadata is not an authority on its
  own, and the two commands that check it by hand.
- The measurement is recorded in `docs/NCBI_database_download.md` §4.4 as a
  four-way table (the artifact, `nt.njs`, the manifest, the payload on disk);
  §4.3, §5.2 and appendix A.3 of both READMEs now state the same-build condition
  instead of treating the artifact as always authoritative, and the parameter
  reference for `--metadata-json` / `--no-metadata-json` says when it counts.
- `docs/OPERATIONS.md`: two new rows in the failure playbook - a foreign artifact
  (no action needed, install completes) and a same-build disagreement (refused;
  re-run `download` or `repair`, proven files are reused).
- `CONTRIBUTING.md`: two invariants - a derived artifact is an authority only
  when it claims the same build as the volume fingerprints, and external tool
  output is parsed in the format that tool actually writes.
- `test_blastdb_download.py`: `TestStaleUpstreamMetadata` (5 tests: a foreign
  artifact installs with a warning, a matching-but-wrong one is still fatal, a
  foreign artifact does not excuse a missing volume, `inspect` reports it as a
  warning, and a truncated payload is still caught) and `TestAria2LogParsing`
  (4 tests built from real aria2c log lines).
- Both README version tables now list 1.4.0 through 1.4.3 (the Chinese one had
  also been missing 1.3.0 and 1.3.1).

## [1.4.2] — 2026-09-24

### Added

- `docs/FAQ.md`: the caveats from both manuals rearranged as *symptom → cause →
  action*, in seven groups (choosing a source, the command line, logging, failure
  and resume, space and quota, verification and BLAST interop, operations and
  rollback).  Every answer ends in a command or a pointer to the manual, and it
  is linked from the documentation index of both READMEs.
- A documentation index entry for the FAQ in `README.md` and `README.en.md`.

## [1.4.1] — 2026-09-24

Documentation engineering: the docs became a small set, and the audits that keep
them honest were extended to cover all of it.

### Added

- `CONTRIBUTING.md` (English): the invariants a change must preserve, the test
  that guards each one, how to run the suite, how to add an option / a check / a
  data source, and the release checklist.
- `docs/DESIGN.md`: the protocol (`revkey`), the authority matrix (what each
  check trusts and what it can catch), the on-disk layout and the state file
  schema, the five reuse layers, the failure policy, the concurrency model, how
  to extend the tool, the invariant list, and the measured facts with the exact
  commands to re-measure them.
- `docs/OPERATIONS.md`: sizing and time estimates, cron and systemd units,
  monitoring (what a healthy log looks like, an offline inspection script), a
  failure playbook keyed by the reason strings the tool prints, resume/salvage,
  and migration from `update_blastdb.pl`.
- A documentation index in both READMEs, and `docs/NCBI_database_download.md`
  for the background notes (moved out of the repository root).

### Changed

- `README_blastdb_download.md` is now a short **pointer** to `README.md` instead
  of a second 50 kB copy, so the two cannot drift.  The compatibility path still
  resolves.

### Fixed

- Documentation defects found by the extended audits: stale relative links after
  the move into `docs/`, two flags quoted from other tools that read as if this
  tool had them (a ruff option and aria2's log level), and one Chinese section
  link that no longer existed.

### Tests

- The audits now cover every markdown file: no document may mention an option
  this CLI does not have (with an explicit exemption for the note that quotes
  `update_blastdb.pl`'s own options), both manuals must still document every
  option, every relative link must resolve to a real file, **cross-file anchors
  must still exist**, and the compatibility README must stay a pointer.
  Total: 101 cases.

## [1.4.0] — 2026-09-24

Documentation round: an explicit dependency contract, and an English README so
the project can be published and reviewed by people who do not read Chinese.

### Added

- **An English README** (`README.en.md`), complete rather than abbreviated: the
  same feature list, recipes, source comparison, robust recipe, command and
  parameter reference, configuration, dependency contract, caveats, verification
  and resume sections, plus the design appendix.  Both READMEs now carry a
  language switch, and the documentation tests audit **all** of them: every
  documented option must exist in the CLI, no phantom options may be documented,
  and every internal link must resolve.
- **A dependency contract** in both READMEs: what is genuinely required (only
  Python 3.9+, the standard library, a symlink-capable filesystem and anonymous
  HTTPS access to a source) versus optional helpers (`aria2c`, `quota`, `lfs`,
  `xfs_quota`, `df`, `blastdbcmd`) with the exact consequence of each absence -
  and, separately, the tools this project never invokes.

### Fixed

- **`doctor` misdescribed `gsutil`/`gcloud`/`aws` as being for "authenticated
  downloads".**  The tool never calls them: cloud access is plain anonymous
  HTTPS, so those CLIs have no role here.  The tool table now carries an explicit
  `role` (runtime vs manual), `doctor` groups its output accordingly, and the
  redundant triple listing of tools was collapsed into one report.
- Both READMEs now document `--limit-rate` as applying to **both** downloaders,
  and `--reuse-verify` with the "files without a published checksum are never
  reused" rule.

## [1.3.1] — 2026-09-24

Third audit round: every designed feature was re-read against its documentation
and, where the implementation leans on a library, against the current CPython
docs (`concurrent.futures`, `os.lseek`/`SEEK_DATA`, `fcntl.flock`, `subprocess`,
`shutil`, `tarfile`, `argparse`, `urllib`).

### Fixed

- **Installing one database wiped the staging of every other database.**
  `drop_staging()` was called with "everything" after a successful install, so a
  later small install destroyed, say, a half finished 900 GiB `nt` download.
  It is now scoped to the databases installed by that run (`gc` still clears
  everything).  Regression test: interrupt one database, install another, the
  first one's staging must survive.
- **A torn retry deleted the previous revision's staging**, which is exactly the
  input of the cross-revision salvage step, so the documented "keep what still
  matches" behaviour could never trigger: a torn `nt` update re-downloaded
  everything.  The stale tree is now left for the salvage step, which reuses the
  matching files and deletes the rest.
- **"Equal size" was accepted as proof of identity for files without a published
  checksum.**  Two revisions of `<db>-nucl-metadata.json` can have the same
  length, so a torn retry salvaged the *old* metadata, the metadata/fingerprint
  cross-check then disagreed with it, and a healthy retry failed verification
  (nothing bad was installed, but the run was wasted).  Reuse, salvage and
  `--adopt` now require a published md5; the handful of checksum-less files are
  simply fetched again.
- **`--limit-rate` was ignored by the built-in downloader** (it only worked with
  aria2).  A shared token-bucket limiter now paces the built-in backend too, and
  the option is documented as applying to both.
- **A wedged `aria2c` could hang an abort.**  `terminate()` is now followed by a
  `kill()` escalation when SIGTERM does not take effect within 30 s, as the
  `subprocess` documentation recommends.
- **A corrupt snapshot state file broke `list`/`gc`/`rollback`.**  Unreadable
  state files are now reported and skipped instead of raising.
- Progress can no longer display more than 100 % or more files than planned when
  retries add bytes to the counter.
- `--adopt` probes the remote size before deciding "complete file" or "file to
  resume", so a complete-but-unverifiable file is no longer mistaken for a
  resumable partial.

### Documentation

- README: why `<db>-nucl-metadata.json` is always re-fetched (no published
  checksum, and equal size is not identity), and what `--adopt-partial` does to
  the adopted file (it completes it in place; a cross-filesystem copy may
  materialise sparse holes).
- CHANGELOG: this entry.

## [1.3.0] — 2026-09-24

Field feedback: `nohup ... --log-file x` still filled `nohup.out`, the periodic
progress line arrived every 30 seconds over a 4.6 hour transfer, and there was
no line saying which files had finished.

### Added

- `--console auto|full|errors|off`.  The default `auto` keeps only warnings and
  errors on the terminal once a log file is in use, so progress and the plan go
  to the file and `nohup.out` stays empty.  `full` mirrors everything,
  `errors` and `off` are quieter still.
- A **default log file**: `download`, `repair`, `gc` and `rollback` now write
  `<root>/log` unless a path is given, and say so at startup.  A command that
  changes a mirror should always leave a trace; `--no-log-file` opts out.
- **One log line per finished file**, for both backends:
  `[3/12] 16S_ribosomal_RNA.nnd  218.82 KiB in 2s (415.34 KiB/s)`.  With aria2
  this comes from a watcher that tails aria2's own `[NOTICE] Download complete:`
  lines (aria2 now runs with its notice log level enabled).

### Changed

- `--progress-interval` now defaults to **3600** seconds instead of 30.  For a
  4.6 hour `nt` transfer that is a handful of progress lines instead of ~550,
  and the per-file lines supply the useful granularity.
- Per-file completion no longer triggers an extra progress line, so the log
  contains exactly one line per event.

### Added

- Test coverage for the **aria2 backend**, which until now was never exercised
  (every other test forces `--aria2c none`).  A minimal aria2c stand-in checks
  the input-file contract - every job carries `checksum=md5=`, and every job
  writes into the revision-scoped staging directory - and that a checksum
  failure leaves nothing behind for the next run to resume.

### Fixed

- A misplaced sub-command option now names its owner
  (`hint: `--force` is an option of the `download` sub-command; put it after
  `download`) instead of only saying "unrecognized arguments".
- A bad `--aria2c PATH` is reported **before** any network access, instead of
  after the source has been resolved.
- `md5 mismatch` / `size mismatch` are now classified as retryable failures (a
  torn transfer is worth another attempt; the old text only matched the generic
  "checksum mismatch" wording).

### Changed

- `taxdb` is now added automatically for **every** source, not only the cloud
  mirrors.  NCBI publishes `taxdb.tar.gz` as a first-class database (62 MiB
  compressed) and, although its single-volume archives were measured to bundle
  the same payload, whether every multi-volume archive does is undocumented - and
  a missing `taxdb` is a silent failure (species names come back as `N/A`).  The
  cost is about 0.006% of an `nt` download, duplicate copies are resolved
  deterministically at install time, and `--no-taxdb` still opts out.

## [1.2.0] — 2026-09-24

Audit round: a static-analysis pass (ruff, plus a custom AST audit), a review of
every shared-state and failure path, and an API check of the concurrency and
sparse-file primitives against the CPython documentation.

### Fixed

- **A file with no published size or checksum was accepted as "verified" just
  for existing.** `verify_local_file()` now refuses to prove the identity of
  such a file, which closes a narrow path where a truncated leftover (for
  example an interrupted `<db>-nucl-metadata.json`) could be mistaken for a
  complete one and carried into a new snapshot. Freshly fetched bytes are
  checked by their own helper (`verify_fetched_file()`), so a download that has
  nothing to compare against must still be non-empty.
- **The NCBI per-database metadata JSON now carries a size.** It is not
  checksummed by NCBI, so its `Content-Length` from the probe is used as the
  authority, and a truncated copy is now detected.
- **Ctrl-C / abort drained the whole chunk queue before taking effect.**
  `ThreadPoolExecutor` only exits once its pending work is done, so queued
  pieces (hundreds for `nt`) kept downloading after an interrupt. The executor
  is now shut down with `cancel_futures=True` and the stop flag is set, so an
  interrupt stops the transfer promptly. (Explicitly recommended by the
  `concurrent.futures` documentation for long-running tasks.)
- **Extracted archive members now win over adopted copies.** When a payload
  file came from `--adopt-extracted` *and* its archive was also fetched, the
  size-based extraction de-duplication could keep the adopted copy. The archive
  is the authority, so those members are now always re-extracted.
- **Failure reports showed `df` free space instead of usable space.** The space
  line in a failure report is now quota aware, like the pre-flight check.
- Removed dead code (`archive_volume()` and its regex, which the final
  all-or-nothing payload adoption design never used) and fixed several
  misleading-value findings: `smoke_test()` accumulated an unused list,
  `latest_dir_moved()` still claimed to influence the re-listing decision (it
  never does; the code now says so), and `ensure_probes()`/`merge_tables()`
  discarded values in silently confusing ways.

### Changed

- The background monitor no longer shells out to `df`/`quota` every few
  seconds: free space is read in-process and the quota is re-checked about once
  a minute, which removes thousands of subprocesses from a long download.
- `--adopt-extracted` now prints a summary line naming the build date whose
  consistency was verified.
- Lint baseline is clean for every correctness-oriented rule set
  (`F`, `E9`, `B`, `PLW`, `RET`, `RUF0xx`).

### Documentation

- Added this changelog, linked from the README.
- Documented the adoption of files produced by the old `aria2c` loop
  (`--adopt`, `--adopt-partial`, `--adopt-extracted`) and the `staging`
  command, both in the README and in `NCBI_database_download.md`.
- Recorded the two failure modes found in the field: `taxdb` reporting an
  "incomplete snapshot" (a scoped listing bug) and reasons never reaching
  `--log-file`.

## [1.1.0] — 2026-09-24

Field feedback: the first real `nt` run failed with a poorly explained error,
which drove a logging/retry/self-check round, and the operator's older
half-finished downloads needed to be reusable.

### Fixed

- **`-k/--min-split-size` had no effect at all.** The option wrote to the
  argparse destination `min_split_size` while the code read `min_split`, so the
  documented flag was silently dead. A contract test now drives every global
  option through the parser and asserts that it reaches the effective
  configuration, which catches this class of bug for good.
- **Cloud snapshot listings missed files not named after the database.**
  Scoping the object listing to the database prefixes (added to remove a 19 s
  full-snapshot sweep) broke `taxdb`, whose third file is
  `taxonomy4blast.sqlite3`; the tool reported "the snapshot is incomplete". The
  manifest is now the authority: files the prefixes miss are looked up by exact
  name, which is also why a snapshot resolving to the same revision key as
  before still reuses what is already installed.
- **Reasons for failures never reached `--log-file`.** The log file was closed
  before the exception handlers ran, so `ERROR:` lines only appeared on stderr.
  The file now outlives the handlers, a failing run logs the resolved source and
  plan, and an unexpected exception is reported (with a traceback at `-v`)
  instead of vanishing.
- **An empty manifest is now reported as "still being published"** (torn, exit
  code 4) rather than as an invalid manifest.
- **The cloud re-check no longer re-lists ~10 000 objects** after a transfer; it
  re-reads `latest-dir` and keeps the pinned, immutable snapshot. Small
  downloads went from ~24 s to ~9 s.
- **Staging reuse verified only the recorded md5**, so a locally damaged file
  would be hard-linked into the next snapshot. Reuse now re-checks the file
  (size plus head/tail content probes by default, `--reuse-verify md5` for full
  hashes), and a mismatch means re-download rather than propagation.
- **`read()` buffered a megabyte**, so a stalled connection left nothing on
  disk and an interrupted transfer had to start over. Streaming now uses
  `read1()`, which is what makes resume actually work.
- **Archives stayed in staging until the very end**, so `nt` needed about
  2 TiB of peak space while unpacking. Each archive is now released as soon as
  it is unpacked (peak: payload plus one archive, ~1.0 TiB), and the pre-flight
  estimate says so.
- **A failed batch re-downloaded archives that had already been unpacked.** An
  extraction receipt now records each archive's members and verifies them on
  reuse.
- **`--dry-run` was blocked by the space check** instead of printing the plan.

### Added

- `--log-file FILE`: a timestamped log that also survives `-q` (which now only
  silences the console; errors always reach it).
- Periodic progress without a terminal: percentage, bytes, speed, ETA and file
  counts every `--progress-interval` seconds, for cron/queue/`tail -f`.
- Reason-aware retries: `--file-retries` whole-batch attempts for retryable
  reasons (timeouts, resets, 403 rate limiting, 5xx, checksum mismatches), with
  automatic concurrency reduction, and a fast refusal for reasons retrying
  cannot fix (no space, quota, permission, 404).
- `--min-free SIZE`: abort a transfer when usable space drops below the floor,
  keeping everything already fetched for the next run.
- `doctor`: free space, quota (`quota -uvs`, `lfs quota`, `xfs_quota`) and which
  optional helpers exist (`aria2c`, `gsutil`, `gcloud`, `aws`, `blastdbcmd`,
  `blastdbcheck`, `quota`, ...) with install hints. Space checks use the smaller
  of `df` and the user quota.
- `staging [--against-remote]`: what interrupted or older runs left in
  `.staging`, and whether it can be resumed as is or how many files are still
  salvageable.
- `--adopt DIR` (repeatable), `--adopt-partial`, `--adopt-extracted`: adopt
  files already downloaded by another tool (the old `aria2c` loop). Complete
  files are verified against the authoritative md5; half-finished files are
  resumed with their aria2 control file or only when they are a hole-free
  prefix; already-extracted payload is adopted only when it forms one complete,
  consistent build.
- An extraction receipt plus cross-revision salvage: when the revision key
  changes, matching files are kept (hard-linked) and the rest of the stale tree
  is deleted.
- Autofix-style guards in the test suite: documentation flags versus CLI flags,
  configuration template versus built-in defaults, and the two README copies.
- An exclusive lock per mirror root for the mutating commands.

### Changed

- Global options are accepted before **or** after the sub-command
  (`download nt --jobs 8` now works; previously it was an "unrecognized
  arguments" error).
- `--dry-run` prints the plan, the per-database disk estimate and the usable
  space.
- Failed downloads are reported grouped by reason with examples and a concrete
  next step, instead of one line per file.

## [1.0.0] — 2026-09-23

Initial release.

### Design

The whole point is that a BLAST database is a *set* of volumes that must come
from one build, so the tool works at set level rather than file level:

- The cloud mirrors are used through the authoritative `latest-dir` pointer, and
  the snapshot it names is treated as immutable; a directory that looks newer
  but has no manifest is ignored.
- Every plan is keyed by a revision fingerprint (`revkey` = source, snapshot,
  database, per-file md5), which is re-read after the transfer: a source that
  publishes mid-download makes the run retry or abort, never install.
- The set is verified from the payload itself before installation: the embedded
  build timestamp in every volume index must agree, volume ordinals must be
  contiguous and must match the file names, `<db>.njs` and
  `<db>-<type>-metadata.json` must agree with them, payload file names must
  belong to the database, `bytes-total` must equal the bytes on disk, and
  shared taxonomy payload conflicts are resolved deterministically.
- Installation builds a new snapshot directory (hard-linking unchanged files)
  and atomically re-points `current`, so readers never see a half-updated
  database and a rollback is one symlink swap.
- Every snapshot carries a state file recording the md5 that was proven for each
  file, where that proof came from and which archive produced the bytes, which
  is what makes `verify` work fully offline.

### Added

- `download`/`update`, `showall`, `verify`, `repair`, `inspect`, `list`, `gc`,
  `rollback`, `config` (with `--init`/`--template`).
- Sources: NCBI over HTTPS (mutable, with `.md5` sidecars), GCP and S3 snapshot
  directories, `--source auto`, `--ncbi-base`/`--ncbi-dir` for mirrors, and
  `--ncbi-url mirror|manifest` to choose whether the payload URL is taken from
  the manifest or from the configured base.
- Parallel downloads with aria2c when available, otherwise a built-in chunked,
  resumable, md5-verifying downloader.
- Three-layer resume: chunk state, staging reuse, and hard-linked reuse from the
  installed snapshot, with staging keyed by revision so a stale resume can never
  contaminate a new revision.
- `inspect`: an offline embedded-fingerprint check of any directory, including
  ones produced by `update_blastdb.pl` or a hand-written `aria2c` loop.
- 80 end-to-end tests driven through the real CLI against a local fake source
  that can reproduce a mid-publication update, a torn set, a missing volume, a
  corrupt or truncated file, a refusing server and a killed process.
