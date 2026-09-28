# blastdb_download.py

**Consistent, verifiable, resumable mirroring of the pre-formatted NCBI BLAST databases.**

A modern replacement for `update_blastdb.pl`: one file, pure Python standard
library, optional aria2c acceleration — and its first job is to guarantee that
every database it installs is one *complete build*, not a mixture of two
releases. A mixed set does not raise an error; it quietly produces wrong
alignments.

[中文](README.md) | **English**

```
blastdb_download.py        # the program (single file, no third-party packages)
test_blastdb_download.py   # 101 end-to-end tests against a local fake source
CHANGELOG.md               # release notes (Keep a Changelog)
CONTRIBUTING.md            # how to contribute, and the invariants a change must keep
docs/FAQ.md                # frequently asked questions (Chinese)
docs/DESIGN.md             # protocol, state file, verification matrix, how to extend (Chinese)
docs/OPERATIONS.md         # sizing, cron/systemd, monitoring, failure playbook (Chinese)
docs/NCBI_database_download.md  # background notes and the measured facts (Chinese)
```

### Documentation index

| Question | Read |
|---|---|
| How do I download, which source, how do I debug | this file (or the Chinese [README.md](README.md)) |
| Quick answers: why did it fail, how do I continue, do I need tool X | [docs/FAQ.md](docs/FAQ.md) *(Chinese)* |
| Capacity planning, cron/systemd, monitoring, failure playbook, migration | [docs/OPERATIONS.md](docs/OPERATIONS.md) *(Chinese)* |
| The protocol, state file format, invariants, how to add a source | [docs/DESIGN.md](docs/DESIGN.md) *(Chinese)* |
| What changed in each release, and why | [CHANGELOG.md](CHANGELOG.md) |
| Contributing or reporting a bug | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Common recipes](#common-recipes)
- [Choosing a data source](#choosing-a-data-source-measured)
- [Download engine](#download-engine-source-independent)
- [The most robust recipe](#the-most-robust-recipe-copy-this)
- [How it works](#how-it-works)
- [Command reference](#command-reference)
- [Parameter reference](#parameter-reference)
- [Configuration file](#configuration-file)
- [Third-party dependencies (what is actually required)](#third-party-dependencies-what-is-actually-required)
- [Caveats and troubleshooting](#caveats-and-troubleshooting)
- [Verifying an existing mirror](#verifying-an-existing-mirror)
- [Resuming interrupted work](#resuming-interrupted-work)
- [Comparison with update_blastdb.pl](#comparison-with-update_blastdbpl)
- [Tests](#tests)
- [Changelog](#changelog)
- [License and provenance](#license-and-provenance)
- [Appendix: why this is necessary](#appendix-why-this-is-necessary)

---

## Features

| | |
|---|---|
| **Set-level consistency** | The unit of consistency is the whole database (all volumes), not a single file. The source revision is read before and after a transfer, and the set is verified again before installation — a mixed set is never installed |
| **Evidence from inside the payload** | Every volume index (`.nin`/`.pin`) embeds its build timestamp and volume ordinal. Those are checked against each other, against `<db>.njs` and against `<db>-<type>-metadata.json` — no trust in any single metadata file |
| **Only the authoritative snapshot pointer** | Cloud mirrors are used through `latest-dir`, whose manifest must exist and list files that all exist in that snapshot (a directory that merely looks newer is ignored) |
| **Per-file authoritative checksums** | NCBI `.md5` sidecars, GCS object `md5Hash`. S3 multipart ETags are **not** md5 and are never treated as one — the weakness is reported honestly |
| **Atomic installation** | A new snapshot directory plus an atomic re-point of the `current` symlink: readers never see a half-updated database, and a rollback is one symlink swap. Unchanged files are hard-linked, so snapshots cost almost nothing |
| **Incremental updates** | Per-file md5 comparison; unchanged volumes are hard-linked in place of being downloaded again |
| **Three-layer resume** | Chunk state, staging reuse, extraction receipt — plus salvage across revisions. Staging is keyed by revision fingerprint, so a stale resume can never contaminate a new revision |
| **Parallel transfers** | aria2c when present (with `checksum=md5=` verification inside the download), otherwise a built-in chunked, resumable, checksum-verifying downloader |
| **Fully offline verification** | The state file records the md5 that was proven for every file, where the proof came from and which archive produced the bytes, so `verify` needs no network |
| **Inspect other people's mirrors** | `inspect` reports the embedded build fingerprint of *any* directory — including one produced by `update_blastdb.pl` or a hand-written aria2c loop — with no state file and no BLAST installation |
| **Adopt existing downloads** | `--adopt` reuses files another tool already downloaded, `--adopt-partial` finishes half-downloaded volumes, `--adopt-extracted` accepts an already unpacked payload when it forms one complete build |
| **Logging that survives nohup** | Everything goes to a log file (`<root>/log` by default for commands that change the mirror); progress lines every `--progress-interval` seconds plus one line per finished file, while the terminal keeps only warnings and errors |
| **Reason-aware retries** | Transient failures (timeouts, resets, 403 rate limiting, 5xx, checksum mismatches) are retried whole-batch with automatic concurrency reduction; reasons retrying cannot fix (no space, quota, permission, 404) fail fast |
| **Environment self-check** | `doctor` reports usable space, the user quota (`quota`/`lfs`/`xfs_quota`) and which optional helpers exist, with install hints. Space checks use the smaller of `df` and the quota |
| **Concurrency safety** | Mutating commands take a lock on the mirror root, so two runs cannot corrupt each other's staging |
| **Machine readable** | Diagnostics on stderr, data on stdout, `--json` for pipelines |

---

## Quick start

Python 3.9+ (3.11+ to read a TOML config); aria2c is optional.

```bash
# 1) what is available (names / table / human readable)
python3 blastdb_download.py showall --format pretty

# 2) download (GCP immutable snapshot by default; taxdb is added automatically)
python3 blastdb_download.py -r /data/blastdb -j 8 -x 4 download nt core_nt

# 3) point BLAST at it (`current` is an atomically switched symlink)
export BLASTDB=/data/blastdb/current
blastn -db nt -query q.fa -out out.txt
```

See the plan, the disk estimate and the usable space without downloading
anything:

```bash
python3 blastdb_download.py -r /data/blastdb --dry-run download nt
```

Want the freshest data instead of the cloud snapshot (which lags a month or
two)? Add `-s ncbi`.

```bash
python3 blastdb_download.py -r /data/blastdb -s ncbi -j 8 download nt
```

> Global options are accepted **before or after** the sub-command:
> `download nt --jobs 8` and `--jobs 8 download nt` are equivalent. Options that
> belong to a sub-command must come after it, and the error message says so.

---

## Common recipes

**Daily incremental update (cron)**

```bash
30 3 * * *  blast  /usr/bin/python3 /opt/blastdb_download.py \
  -r /data/blastdb -s ncbi -j 8 --keep-snapshots 2 \
  download nt core_nt taxdb >> /var/log/blastdb.log 2>&1
```

**Regional / internal mirror instead of talking to NCBI directly**

```bash
python3 blastdb_download.py -r /data/blastdb -s ncbi \
    --ncbi-base https://mirror.example.org --ncbi-dir /blast/db download nt
```

**Rate limiting, forcing the built-in downloader, or naming an aria2c binary**

```bash
python3 blastdb_download.py -r /data/blastdb --limit-rate 50M download nt
python3 blastdb_download.py -r /data/blastdb --aria2c none download nt
python3 blastdb_download.py -r /data/blastdb --aria2c /usr/local/bin/aria2c download nt
```

**Tightening or relaxing the "reuse an installed file" check**

```bash
# default probe: size plus head/tail content fingerprints (milliseconds, catches
# truncation and damage at the edges)
python3 blastdb_download.py -r /data/blastdb --reuse-verify probe download nt
# full md5 every time: strongest, but hashes the whole tree on each run
python3 blastdb_download.py -r /data/blastdb --reuse-verify md5 download nt
```

**When the source publishes a new release mid-download**

```bash
# default: discard this attempt's staging and retry after a wait
python3 blastdb_download.py -r /data/blastdb -s ncbi download nt
# rather fail than wait (exit code 4, and nothing is left installed)
python3 blastdb_download.py -r /data/blastdb -s ncbi --on-torn fail download nt
# use the immutable cloud snapshot instead
python3 blastdb_download.py -r /data/blastdb -s gcp download nt
```

**Ignore what is installed and fetch everything again**

```bash
python3 blastdb_download.py -r /data/blastdb download --force nt
```

**Verify / repair / roll back**

```bash
python3 blastdb_download.py -r /data/blastdb verify
python3 blastdb_download.py -r /data/blastdb verify --quick            # probes only
python3 blastdb_download.py -r /data/blastdb verify --against-remote   # is there a newer revision?
python3 blastdb_download.py -r /data/blastdb repair nt                 # only the broken files
python3 blastdb_download.py -r /data/blastdb list
python3 blastdb_download.py -r /data/blastdb rollback --list
python3 blastdb_download.py -r /data/blastdb rollback --to 1
python3 blastdb_download.py -r /data/blastdb gc --keep 2
```

**Write a configuration template**

```bash
python3 blastdb_download.py -r /data/blastdb config --init
python3 blastdb_download.py config --template          # print only, write nothing
```

**About `taxdb` (a `N/A` in the `%S` field means it is missing)**

All three sources provide taxonomy; you never need a particular source for it:

```bash
# the default source adds taxdb automatically
python3 blastdb_download.py -r /data/blastdb download nt
# asking for it explicitly is fine too (idempotent, never downloaded twice)
python3 blastdb_download.py -r /data/blastdb download nt taxdb
# NCBI source: also added by default (~62 MiB); opt out with --no-taxdb
python3 blastdb_download.py -r /data/blastdb -s ncbi download nt
python3 blastdb_download.py -r /data/blastdb -s ncbi --no-taxdb download nt
```

**Continuing work that an older aria2c loop left behind**

```bash
python3 blastdb_download.py -r /data/public/databases/NT/other \
        --adopt /data/public/databases/NT --adopt-partial \
        -s ncbi -j 8 --log-file /var/log/nt.log download nt
```

---

## Choosing a data source (measured)

All three sources carry the same databases (including `taxdb`); they differ in
freshness, verification strength and space/CPU cost. The numbers below were
checked live on 2026-09-24.

| | `-s ncbi` | `-s gcp` (default) | `-s aws` |
|---|---|---|---|
| **Freshness** | **newest**: `nt` published 2026-09-15, files updated to 09-22 | monthly snapshot: `latest-dir` = `2026-07-21-01-05-02` (1–2 months behind) | same snapshot as GCP (`2026-07-21-01-05-02`) |
| **Directory nature** | **mutable in place**: files are replaced one by one during a release | immutable snapshot directory plus a `latest-dir` pointer | same as GCP |
| **Layout** | `*.tar.gz` plus a `.md5` sidecar per file | raw index files (**no unpacking**) | same as GCP |
| **Per-file authoritative checksum** | yes, `.md5` sidecar (real md5) | yes, object `md5Hash` (real md5) | ⚠ large-object ETags are **multipart hashes**, not md5 → verification falls back to size + embedded fingerprints + set checks, and `verify` says so |
| **Bytes to download (`nt`)** | ≈ 932 GiB (compressed archives) | ≈ 1.0 TiB (raw files, slightly more) | same as GCP |
| **Peak disk (`nt`)** | payload 1.07 TiB **plus one archive** (0.73–4.78 GiB); ≈1.9 TiB with `--keep-archives` | the payload (≈1.0 TiB), no unpacking overhead | same as GCP |
| **CPU/time** | unpacks 383 archives | none | none |
| **Main risk** | a release window can tear a download (the tool retries or aborts, and never installs a mixture) | data lags; **only about three snapshots (≈1 month) are retained** | weaker verification; same retention window |
| **Best for** | wanting the newest data | **the production default**: strongest determinism, least space and CPU | faster paths or credentials, when weaker verification is acceptable |

**How to choose**

- **Just want it safe** → the default `-s gcp`: immutable snapshot, real md5 per
  file, no unpacking. The most deterministic of the three.
- **Want it new** → `-s ncbi`: the consistency protocol is built for exactly
  this mutable directory (revision fingerprint read before and after the
  transfer, embedded build timestamps verified). The price is the occasional
  retry or abort when a release is being published.
- **`-s aws`** → only when AWS connectivity is clearly faster or credentials
  require it; note that large files have no md5 to check against.
- **Both** → build an immutable baseline with `-s gcp` (usable and fully
  verifiable immediately), then chase updates with `-s ncbi` as needed;
  unchanged files are hard-linked between the two, so nothing is stored twice.
- **Do not mix sources for the same database in one `--root`**: it works (the
  `current` tree carries unchanged databases over), but a cloud `taxdb` and a
  taxonomy payload bundled inside an NCBI archive can come from different builds;
  the tool then picks the newer one deterministically and warns.

## Download engine (source independent)

Whether aria2c is used depends **only on whether it is on `PATH`**; all sources
behave the same:

```bash
--aria2c auto      # default: use `which aria2c` if found, else the built-in downloader
--aria2c none      # force the built-in downloader
--aria2c /path/to/aria2c   # explicit binary (an invalid path fails before any network I/O)
```

`doctor` reports what was detected, e.g. `aria2c  /usr/bin/aria2c (aria2 version 1.37.0)`.

With aria2c (GCP source as the example):

- every file in aria2's input file carries **`checksum=md5=<GCS md5Hash>`**, so
  **aria2 verifies the official md5 inside the download** and the tool verifies
  the result again afterwards;
- files are split across connections according to `-x/--connections` and
  `-k/--min-split-size` (GCS supports ranges; files below 64 MiB are not split);
- `.aria2` control files live in the **revision-scoped** staging directory, so a
  resume can never cross revisions;
- aria2 leaves failed files on disk; the tool deletes them, otherwise the next
  resume would carry corrupt bytes forward;
- progress: aria2's own readout on a terminal, and the background monitor
  reports staging size when there is no terminal.

**Without aria2c nothing is lost in correctness**: the built-in downloader also
does parallel chunked ranges, positioned writes, resume and md5 verification.
Force it with `--aria2c none`.

## The most robust recipe (copy this)

```bash
ROOT=/data/public/databases
LOG=/var/log/blastdb
mkdir -p "$LOG"

# 0) environment self-check: usable space, user quota, optional helpers
blastdb_download.py -r "$ROOT" doctor

# 1) dry run: plan, per-database disk estimate, usable space, auto-added taxdb
blastdb_download.py -r "$ROOT" -s gcp --dry-run download nt taxdb

# 2) the transfer: immutable snapshot source, complete log, space floor,
#    reason-aware retries, a snapshot kept for rollback
nohup blastdb_download.py -r "$ROOT" -s gcp \
      -j 8 --connections 4 --min-split-size 64M \
      --min-free 50G --file-retries 3 --keep-snapshots 2 \
      --log-file "$LOG/nt.log" \
      download nt taxdb &

tail -f "$LOG/nt.log"          # percentage, speed, ETA, file counts

# 3) full verification afterwards (about ten minutes for 1 TiB)
blastdb_download.py -r "$ROOT" verify

# 4) hand it to BLAST
export BLASTDB="$ROOT/current"
blastdbcmd -db nt -info
```

What each option buys:

| Option | What it buys |
|---|---|
| `doctor` | Know whether space/quota/tools are sufficient *before* starting (on parallel filesystems the quota is often far smaller than `df`) |
| `--dry-run` | The plan, the disk estimate, the usable space and which databases would be added; read-only, consumes nothing |
| `-s gcp` | Immutable snapshot + real `md5Hash` + no unpacking (the most deterministic route) |
| `--log-file` | A timestamped log with the plan, progress, grouped failure reasons and retries (kept even under `-q`) |
| `--min-free 50G` | Stop as soon as usable space falls below the floor (**everything already fetched is kept**) instead of filling the disk and then reporting hundreds of failures |
| `--file-retries 3` | Whole-batch retries for transient failures (timeouts, resets, 403 rate limiting, 5xx), with automatic concurrency reduction |
| `--keep-snapshots 2` | Keep the previous snapshot for a one-command, instant rollback |
| `verify` | Full md5 + embedded fingerprints + completeness checks; offline, repeatable |
| `nohup`/`tmux`/systemd | A disconnect does not matter; even Ctrl-C is safe because re-running the same command resumes |

**Notes for long runs**

- **Cloud snapshots are retained for about a month** (AWS currently holds
  `07-10 / 07-14 / 07-21`, GCS holds `07-21 / 09-19 / 09-22`). A 1 TiB `nt`
  transfer that outlives the window will see 404s; the tool reports that clearly,
  and re-running lands on the new snapshot, salvaging the volumes that still
  match and re-fetching the rest.
- **Peak disk**: the NCBI layout needs the payload plus one archive;
  `--keep-archives` turns that into payload plus all archives (≈1.9 TiB for `nt`).
- **Interrupt any time**: `current` is never left half updated, and everything
  already fetched stays in `<root>/.staging` for the next run.

---

## How it works

```
revision(db) := { (file name, size, authoritative md5, remote token) ... }
revkey(db)   := sha1(source | snapshot | database | revision(db))
```

1. **Resolve the source snapshot**: cloud reads `latest-dir` (the only
   authoritative pointer) and validates the manifest; NCBI reads the live
   manifest and treats each `.md5` sidecar as the authority.
2. **Reuse instead of re-download**: files that are byte-identical to what is
   installed are hard-linked into the new snapshot.
3. **Download into revision-scoped staging**: `<root>/.staging/<db>/<revkey>/`,
   so aria2 control files and chunk state can never be resumed across revisions.
4. **Verify every file** against its authoritative md5; failed files are deleted
   (aria2 deliberately leaves failed payloads behind).
5. **Re-read the remote revision**: if it moved, the source was publishing while
   we transferred, so staging is discarded and the database retried or the run
   aborted.
6. **Verify the set**: one build timestamp across all volumes, ordinals exactly
   `0..N-1`, the ordinal embedded in a file matching its name, `<db>.njs`
   agreeing with the fingerprints, and payload file names belonging to the
   database.  `<db>-<type>-metadata.json` counts only when it claims the same
   build as the fingerprints (then its `files` / `bytes-total` /
   `number-of-volumes` must match); a copy left over from a previous build is
   reported as a warning (see appendix A.4).
7. **Install atomically**: build the new snapshot directory, then atomically
   re-point `current`. On any failure `current` is untouched.

---

## Command reference

| Command | Purpose |
|---|---|
| `showall` | List the databases available at the source with size, update time and volume count |
| `download` (alias `update`) | Download or update databases into a new snapshot and switch atomically |
| `verify` | Verify an installed snapshot (md5, set fingerprints, optional remote comparison) |
| `repair` | Re-download only what fails verification; everything else is hard-linked |
| `inspect` | **Offline** embedded-fingerprint report for any directory (no state file, no BLAST) |
| `staging` | What interrupted or older runs left in `.staging`, and how much of it is still usable |
| `doctor` | Usable space, user quota and which optional helpers exist |
| `list` | Local snapshots, with the current one marked |
| `gc` | Prune old snapshots and staging leftovers |
| `rollback` | Point `current` at an older snapshot |
| `config` | Print the effective configuration, or write a template |

Sub-command specific options:

| Sub-command | Options |
|---|---|
| `showall` | `--format name\|tsv\|pretty\|json` |
| `download` | `--force`, `--takeover`, `--no-check-md5` |
| `verify` | `--snapshot NAME`, `--quick`, `--no-md5`, `--against-remote` |
| `repair` | `--snapshot NAME`, `--quick` |
| `inspect` | `--dir DIR` |
| `staging` | `--against-remote` |
| `gc` | `--keep N` |
| `rollback` | `--to N`, `--snapshot NAME`, `--list` |
| `config` | `--init`, `--template`, `--path FILE`, `--force` |

Exit codes: `0` success · `1` usage/runtime error · `2` command line error
(argparse, for example a sub-command option in the wrong place) · `3`
verification failed (installation refused) · `4` a source revision changed
mid-transfer and the run aborted · `130` interrupted with Ctrl-C.

---

## Parameter reference

Every option below is global, and may be written before or after the
sub-command.

**Location and source**

| Option | Meaning |
|---|---|
| `-r, --root DIR` | Mirror root holding the snapshots and `current` (default `./blastdb`) |
| `-c, --config FILE` | Use this configuration file only |
| `-s, --source {gcp,aws,ncbi,auto}` | Data source, default `gcp`; `ncbi` is freshest, `auto` prefers `ncbi` |
| `--ncbi-dir PATH` | NCBI directory, default `/blast/db` (e.g. `/blast/db/v5`) |
| `--ncbi-base URL` | Origin of the NCBI tree or a regional replica; setting it also redirects payload URLs |
| `--ncbi-url {auto,mirror,manifest}` | `mirror` fetches `<file>` from `--ncbi-base`/`--ncbi-dir`; `manifest` uses the published URL (`ftp://` becomes `https://`) |
| `--insecure` | Do not verify TLS certificates (debugging a private mirror) |

**Transfer**

| Option | Meaning |
|---|---|
| `-j, --jobs N` | Total parallel connections, default `max(1, min(8, cores/2))` |
| `-x, --connections N` | Connections per file, default 4 |
| `-k, --min-split-size SIZE` | Smallest range request unit, default 64M; larger files are split |
| `--limit-rate SIZE` | Overall download limit, e.g. `50M`; honoured by **both** downloaders |
| `--timeout SEC` | Connect/read timeout, default 60 |
| `--tries N` | Attempts per request, default 5 |
| `--aria2c PATH\|auto\|none` | aria2c binary; `none` forces the built-in downloader |
| `--no-probe-sizes` | Do not HEAD objects of unknown size |

**Consistency policy**

| Option | Meaning |
|---|---|
| `--on-torn {retry,fail}` | When the source publishes mid-transfer: retry (default) or abort |
| `--torn-retries N` | Extra attempts before giving up, default 3 |
| `--torn-wait SEC` | Seconds between those attempts, default 60 |
| `--reuse-verify {probe,md5,size}` | How hard to re-check an installed file before hard-linking it, default `probe` (size plus head/tail fingerprints) |
| `--no-check-md5` | Skip the md5 re-hash of the assembled tree (`download`) |
| `--smoke-test {auto,always,never}` | Ask the local `blastdbcmd` whether it accepts the database; default `auto` is advisory only |

**Content and space**

| Option | Meaning |
|---|---|
| `--taxdb` / `--no-taxdb` | Whether to add `taxdb` automatically (default: yes, for every source) |
| `--metadata-json` / `--no-metadata-json` | Whether to fetch `<db>-<type>-metadata.json` as well (default: yes; **within one build** it is an important offline completeness check, but NCBI occasionally ships a stale copy - see [appendix A.4](#a4-the-payload-metadata-is-a-complete-file-list-but-upstream-may-ship-an-old-copy)) |
| `--keep-archives` | Keep the verified `.tar.gz` files inside the snapshot (default: deleted after unpacking) |
| `--no-dedupe-taxonomy` | Re-extract archive members even when an identical-sized copy exists (default: deduplicate).  Note that **deduplication keys on name plus size** - NCBI's archives ship identical taxonomy payloads as measured, but use this switch to force re-extraction if you suspect upstream changed a same-sized member |
| `--keep-snapshots N` | How many snapshots to keep, default 2 (`current` is always kept) |
| `--min-free SIZE` | Stop a transfer when usable space falls below this, default 2 GiB |
| `--no-disk-check` | Skip the pre-flight free space check |
| `--adopt DIR` | Also look for already downloaded files in DIR (repeatable) |
| `--adopt-partial` | Also adopt half-finished files so they are resumed, not restarted |
| `--adopt-extracted` | Also adopt already extracted payload when it forms one complete build |

**Logging and output**

| Option | Meaning |
|---|---|
| `--log-file FILE` | Write diagnostics to FILE (timestamped); the console then keeps only warnings and errors |
| `--no-log-file` | Do not create the default `<root>/log` for download/repair/gc/rollback |
| `--console auto\|full\|errors\|off` | Terminal policy: `auto` (default) keeps warnings/errors once a log file is in use, `full` mirrors everything, `errors` and `off` are quieter |
| `--progress-interval SEC` | Seconds between periodic progress lines, default 3600; every finished file also gets a line |
| `--file-retries N` | Whole-batch attempts for retryable failures, default 3 |
| `--dry-run` | Print the plan and transfer nothing |
| `--json` | Machine readable output on stdout (a `--dry-run` plan included: human text always goes to stderr, so `| jq` is safe; a stdout closed early exits 0) |
| `-q, --quiet` | No console diagnostics (the log file is unaffected) |
| `-v, --verbose` | More diagnostics, repeatable; `-vv` reaches debug level |

---

## Configuration file

**The tool never writes a configuration file on its own.** It runs with
built-in defaults; create one only when you want to change them.

```bash
# write a fully commented template to <root>/blastdb-download.toml
python3 blastdb_download.py -r /data/blastdb config --init

# or somewhere else
python3 blastdb_download.py -r /data/blastdb config \
        --init --path ~/.config/blastdb-download/config.toml

# it is never overwritten without --force; use --template to only look at it
python3 blastdb_download.py config --template
```

Every option in the template is commented out, so creating it **changes no
behaviour**; it is also a complete parameter reference.

Search order (first match wins, most specific first):

```
command line
  > --config FILE
  > <root>/blastdb-download.toml
  > $XDG_CONFIG_HOME/blastdb-download/config.toml
  > ~/.blastdb-download.toml
  > built-in defaults
```

To find out why a setting is not taking effect:

```bash
python3 blastdb_download.py -r /data/blastdb config | jq '{config_file, config_search_path, jobs}'
```

`config_file` is `null` when no file was loaded.

```toml
[general]
source = "ncbi"              # gcp | aws | ncbi | auto
jobs = 8
keep_snapshots = 2
reuse_verify = "probe"       # probe | md5 | size
min_free = "50G"
limit_rate = "50M"
console = "auto"             # auto | full | errors | off
log_file = "/var/log/blastdb.log"
aria2c = "/usr/bin/aria2c"   # or "auto" / "none"
aria2c_extra_args = ["--disk-cache=256M"]   # passed straight to aria2c
```

---

## Third-party dependencies (what is actually required)

**Python is the only thing you must install.** The tool is a single file with
**zero third-party Python packages** (every import is standard library), so
`pip install` is never needed, and no external program is mandatory:

| Dependency | Required? | Purpose / what happens without it |
|---|---|---|
| **Python ≥ 3.9** | ✅ **yes** | Runs the tool (3.11+ is needed to *read* a TOML config; `config --init` works on 3.9/3.10 too) |
| **Python standard library** | ✅ **yes** (bundled) | Networking (urllib), unpacking (tarfile/gzip), hashing (hashlib), concurrency (threading/concurrent.futures) — **no pip packages** |
| **A writable filesystem with symlinks** | ✅ **yes** | `current` is an atomically switched symlink. Hard links are **strongly recommended**: without them snapshots fall back to copying (still correct, just more space). Windows notes below |
| **Network access to a source** | ✅ **yes** | NCBI `ftp.ncbi.nlm.nih.gov`, GCS `storage.googleapis.com`, S3 `s3.amazonaws.com` — all over **anonymous HTTPS**, no cloud SDK or credentials |

Everything else is **optional** (its absence never affects correctness; `doctor`
tells you what is present and how to install it):

| Optional tool | What installing it gives you | Behaviour without it |
|---|---|---|
| **aria2c** | Multi-connection splitting, checksum verification inside the download, `.aria2` resume files | The **built-in downloader** is used: same chunked ranges, resume, md5 verification and space watchdog; only connection counts and progress display differ. `--aria2c none` forces it explicitly |
| **quota** (Debian/Ubuntu `apt install quota`, RHEL `yum install quota`) | Reads the **user quota** and includes it in space decisions | Only `df` is consulted. On parallel filesystems and containers the quota is often far smaller than `df`, so installing `quota` is strongly recommended there |
| **lfs** (Lustre client) / **xfs_quota** (xfsprogs) | Quota reading on those filesystems | Same as above (falls back to `df`) |
| **df** (coreutils) | `doctor` shows filesystem type and size | The space check falls back to Python's `shutil.disk_usage`; only the report loses detail |
| **blastdbcmd** (BLAST+) | The `--smoke-test` check before installation, and manual spot checks (`%a/%o/%T/%S`) | The smoke test is skipped (it is advisory anyway, see caveat 2 below) |

**Never invoked by this tool** (also labelled that way by `doctor`):

| Tool | Note |
|---|---|
| **gsutil / gcloud / aws** | Not needed: cloud access is anonymous HTTPS. The official CLIs have no role here (`doctor` lists them only to avoid confusion) |
| **tar / gzip commands** | Not needed: archives are unpacked in-process with `tarfile`, so there is no external tar invocation to get wrong |
| **curl / wget** | Not needed: all HTTP is done by Python itself |
| **blastdbcheck** | The tool never calls it; the documentation merely suggests running it manually when you want extra ISAM sampling |

`.gitignore` suggestion:

```gitignore
__pycache__/
*.pyc

# mirror data and runtime artifacts
blastdb/
*.blparts.json
*.aria2
current
```

---

## Caveats and troubleshooting

**1. `blastdb_download.py download nt > log` captures almost nothing.**
Diagnostics go to **stderr**; stdout only carries data (`showall`, `--dry-run`,
`--json`). Use a log file instead:

```bash
python3 blastdb_download.py ... download nt > nt.out 2> nt.err
python3 blastdb_download.py ... download nt 2>&1 | tee nt.log
```

**2. `nohup.out` used to receive a second copy of the log — it no longer does.**
Commands that change the mirror (`download`, `repair`, `gc`, `rollback`) now
write `<root>/log` by default and say so at startup:

```
logging to /data/public/databases/NT/log (use --log-file to move it, --no-log-file to disable)
```

Once a log file is in use, the console switches to `--console auto`: only
warnings and errors stay on screen, so `nohup ... > nohup.out` leaves an
essentially empty file. `--console off` silences everything (including errors),
`--console full` mirrors the log to the terminal, and `--no-log-file` disables
the default file.

Granularity in the log:

```
2026-09-24 13:02:27 [3/12] 16S_ribosomal_RNA.nnd  218.82 KiB in 2s (415.34 KiB/s)
2026-09-24 13:02:28 16S_ribosomal_RNA:   4.6% 832.71 KiB/17.58 MiB 415.31 KiB/s files 3/12 eta 41s
```

One line per finished file (always), plus a periodic progress line every
`--progress-interval` seconds (default 3600). With aria2c the per-file lines come
from tailing aria2's own `[NOTICE] Download complete:` log lines.

**3. `blastdbcmd` / `blastdbcheck` report `mdb_env_open: MDB_INVALID: File is not an LMDB file`.**
That is a **client-side BLAST/LMDB incompatibility**, unrelated to download
correctness: NCBI's official `tar.gz` extracted by hand fails the same way, while
a database built locally by `makeblastdb` works. `--smoke-test auto` therefore
only warns rather than blocking, and the authority remains the md5 evidence plus
`inspect` / `verify`. Use a BLAST build that can read NCBI's LMDB files.

**4. `%S` (species name) shows `N/A`.**
Species names come from `taxdb`, which must live in the same `BLASTDB`
directory. Cloud sources add it automatically; NCBI ships a standalone
`taxdb.tar.gz` and some archives also bundle the payload. The tool adds `taxdb`
automatically for every source (about 62 MiB); `--no-taxdb` opts out.

**5. Why is `<db>-nucl-metadata.json` re-fetched every time?**
Because NCBI publishes **no md5** for it, and an equal size is not proof of
identity: two revisions of that JSON can have the same length and different
content. An earlier implementation reused the old revision's copy on that basis,
which made a torn retry salvage the wrong metadata and fail verification.
Reuse, salvage and adoption now require a published md5; the checksum-less files
(just this ~500 B JSON) are simply fetched again.

**6. `--adopt-partial` writes into the file it adopts — deliberately.**
Adopting a half-finished file uses a **hard link plus append-only resume**, so
your partial file is completed in place; if the run is abandoned it is still a
hole-free prefix, never corrupt data. When verification fails, the tool unlinks
the *staging* name and your file remains. Across filesystems the hard link
becomes a copy, and a sparse aria2 partial can then materialise its holes as
real disk usage — keep the adopt directory and `--root` on the same filesystem,
or let the tool download again.

**7. Cloud snapshots lag, and are retained for about a month.**
`latest-dir` currently points at `2026-07-21-01-05-02` while NCBI already has
2026-09 data. Use `-s ncbi` for freshness, `-s gcp` for immutability.

**8. An S3 source has no real md5.**
Large-object ETags are multipart hashes (`"...-358"`), so verification degrades
to size, embedded fingerprints and set checks; the state file records
`verification: mixed` honestly. Use `-s gcp` when you need strong checksums.

**9. `--reuse-verify probe` (the default) cannot see damage in the middle of a file.**
It reads size plus 64 KiB at each end, which catches truncation, appends and
edge damage at negligible cost. Use `--reuse-verify md5`, or run `verify`
(full md5) periodically.

**10. Do not run `gc` right after an interruption**: `gc` clears staging. A
successful install clears it too, because the snapshot tree is the resume point.

**11. Ctrl-C is not instant.** The signal is noticed when the current chunk or
request finishes or times out (default socket timeout 60 s, tunable with
`--timeout`); queued chunks are cancelled rather than drained. The stderr message
says where to resume.

**12. Two runs must not share a mirror root.** `download`, `repair`, `gc` and
`rollback` take `<root>/.lock`, and the second process fails with a clear message
instead of corrupting the first. Read-only commands (`list`, `verify`,
`showall`, `inspect`, `staging`, `doctor`, `config`) take no lock and can run
during a download.

**13. `current` must be a symlink.** If `<root>/current` is a real directory
(for example a tree previously maintained by `update_blastdb.pl`), the tool
refuses to run; pass `--takeover` to have it renamed to
`.legacy-current-<timestamp>` first.

**14. Disk space.** `nt` needs on the order of 1 TiB unpacked. The pre-flight
check requires the estimate plus 5 % plus a 1 GiB reserve, and `--no-disk-check`
overrides it. Snapshots share data through hard links, so only genuinely changed
files cost extra space; `--keep-snapshots` controls how many are kept.

**15. Windows works, but is not the primary target.** The built-in downloader
falls back to `lseek`+`write` (slightly slower, same correctness), hard links
fall back to copies when not permitted, and the `current` **symlink requires
Developer Mode** or an elevated shell. Linux/macOS/WSL is recommended, and using
`--aria2c` is advisable on Windows.

**16. Do not commit downloaded data or runtime artifacts.** See the `.gitignore`
above.

**17. The log says `<db>-nucl-metadata.json` "describes a different build".**
That is NCBI shipping an **old copy** of that derived artifact, not damage: in the
`2026-07-21-01-05-02` snapshot `nt-nucl-metadata.json` was a day older and about
10.2 GB smaller than the snapshot's own 345 volumes, `nt.njs` and manifest.  Since
1.4.3 the tool recognises a foreign artifact (the log adds `stale, ignored`) and
**installs anyway**.  A genuine `refusing to install` means a same-build
disagreement - missing or truncated files - so just re-run `download` (files that
were already proven are reused).  See appendix A.4 and [`docs/FAQ.md`](docs/FAQ.md) F7.

---

## Verifying an existing mirror

`inspect` needs no state file, no BLAST and no network; it works on **any**
directory, including one produced by `update_blastdb.pl` or a hand-written
aria2c loop:

```bash
python3 blastdb_download.py inspect nt --dir /data/public/databases/NT
python3 blastdb_download.py --json inspect nt --dir /data/public/databases/NT
```

A healthy database:

```
nt  (/data/public/databases/NT)
  files              : 3452
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: 2026-07-19T03:10 (all volumes agree)
  verdict            : OK
```

A torn one (the typical result of a hand-written aria2c loop):

```
nt  (/data/public/databases/NT)
  volume indexes     : 345 (ordinals 0..344)
  embedded build date: MIXED -> 2026-07-19T03:10: nt.000.nin, nt.001.nin ...
                              2026-09-16T13:46: nt.344.nin
  verdict            : FAILED
      MIXED BUILD TIMESTAMPS across volumes - this file set is torn
```

It also detects gaps in the volume ordinals, a file name whose ordinal disagrees
with the embedded one, missing or unexpected payload files, truncated files
(`bytes-total` mismatch) and payload whose metadata comes from another build.

How the available checks differ:

| Check | Finds | Needs |
|---|---|---|
| `blastdb_download.py inspect` | mixed builds, volume gaps, missing files, truncation, payload/metadata disagreement | only Python |
| `blastdb_download.py verify` | the above plus per-file md5 against the proof recorded at install time | a state file (a mirror installed by this tool) |
| `blastdbcmd -db X -entry ACC -outfmt "%a %o %T %S"` | spot checks: accession, OID, taxid, species name | a working BLAST plus `taxdb` |
| `blastdbcheck -db X -dbtype nucl -verbosity 2 -random 200` | ISAM sampling, volume structure, optional TaxID checks | a working BLAST (a conda 2.17.0 build that cannot read NCBI's LMDB will simply report FAILURE) |

---

## Resuming interrupted work

Three layers, all automatic: re-running the same command continues instead of
starting over.

| Layer | Covers | Mechanism |
|---|---|---|
| 1. Inside a file | one large file transferred halfway | Per-file chunk state plus positioned writes, so only missing chunks are fetched; single-stream transfers continue from the bytes on disk (`Range: bytes=<length>-`). aria2c does the same through its `.aria2` control files |
| 2. Staging reuse | files that arrived completely but were never installed | Verified against their md5 (or the extraction receipt) and reused |
| 3. Snapshot reuse | routine incremental updates | Unchanged files are hard-linked into the new snapshot |

What makes it *safe* rather than merely convenient: the staging directory name
contains the revision fingerprint, so a resume can never cross revisions; chunks
are flushed before they are recorded, so a power cut cannot leave a state file
claiming data that is not on disk; and the final md5 check is the backstop.

Salvage on a revision change: when the revision key moves (a republished
release, a different source, different options), files whose md5 still matches
are kept and hard-linked into the new revision's staging, and the rest of the
stale tree is deleted. Files without a published checksum are never salvaged,
because an equal size is not proof of identity.

Also available for work started by other tools:

```bash
python3 blastdb_download.py -r /data/db staging --against-remote
python3 blastdb_download.py -r /data/db --dry-run download nt
python3 blastdb_download.py -r /data/db --adopt /path/to/old/aria2c/output \
        --adopt-partial --adopt-extracted download nt
```

---

## Comparison with update_blastdb.pl

| | `update_blastdb.pl` | `blastdb_download.py` |
|---|---|---|
| Unit of consistency | one file | **the whole database (all volumes)** |
| Cross-volume mixing | not detected | **embedded build timestamps + volume ordinals + `.njs`/metadata cross-checks** |
| While a release is being published | can install a mixture | retries or aborts; **never installs** |
| Cloud verification | none | GCS `md5Hash` per file; S3 degrades honestly |
| Freshness decision | mtime / `Last-Modified` | authoritative md5 plus a local state file and content probes |
| Resume | curl re-fetches whole files | three layers, revision-scoped |
| Installation | overwrite in place | new snapshot plus an atomic `current` switch, instant rollback |
| Incremental update | per-file mtime | per-file md5 with hard-link reuse |
| Shared payload conflicts | last extraction wins (silently) | deterministic resolution with a warning |
| Verify an existing mirror | not available | `inspect`, offline and without BLAST |
| BLAST usage | `BLASTDB=<dir>` | `BLASTDB=<root>/current` |
| Dependencies | Perl, Net::FTP, curl, JSON::PP | Python standard library; aria2c optional |

---

## Tests

```bash
python3 test_blastdb_download.py -v      # 100 cases, about 3 minutes
```

The suite drives the real CLI against a local fake source that can reproduce the
failures that matter, and it makes no network access at all:

- download → verify → a second run downloads nothing; multi-chunk transfers
- a source publishing mid-download (abort with exit 4, or converge after retry),
  and exhaustion when the source never settles
- mixed build timestamps, volume gaps, a payload whose metadata disagrees, a
  manifest listing a file the source does not serve — all refused
- locally damaged or truncated files: found by `verify` and `verify --quick`,
  repaired file-by-file without carrying damage into the next snapshot
- atomic switching, `rollback`, `gc --keep 2`
- resuming: a `SIGKILL` mid-transfer resumes from the bytes on disk, finished
  chunks and already-unpacked archives are reused (verified by counting bytes,
  not by reading log messages), Ctrl-C exits 130 without draining the queue
- adopting existing downloads, including a torn set being refused, and a
  checksum-less file never being salvaged across revisions
- logging: a default `<root>/log`, a log file keeping the console quiet,
  `--console full|off`, warnings still reaching the screen, one line per file
- the CLI contract itself: every documented option exists and reaches the
  effective configuration, options work on both sides of the sub-command,
  a misplaced sub-command option names its owner, concurrent runs are refused,
  the configuration template matches the built-in defaults, and both READMEs
  document every flag, resolve their internal links, and agree with the LICENSE

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Summary:

| Version | Highlights |
|---|---|
| **1.4.7** | A closed pipeline (`--json | head`/`jq`) is no longer reported as `BrokenPipeError` with exit 1 but exits 0; `~`/`$VARS` in path options and config files are expanded (a directory literally named `~` used to be created); `--json` stdout is no longer polluted by dry-run text; filesystem errors (ENOSPC/EDQUOT/EROFS/EACCES) are named the way the operations playbook lists them |
| **1.4.6** | `gc` no longer over-reports the space it frees (snapshots share hard links; only `st_nlink == 1` bytes count); the metadata cross-check excludes shared taxonomy files on **both** sides; **corrected** the 1.4.3-1.4.5 documentation that claimed the manifest agreed with the payload (measured: the *summary* fields of the manifest and the metadata are the ones that disagree) |
| **1.4.5** | The **final verification gate no longer re-reads**: a snapshot entry is a hard link to the file the set-level gate just hashed, so same inode + size + mtime is as strong as recomputing the md5 - saving a full 1 TiB read per `nt` install (~1 h at 250 MB/s). Anything unprovable (cross-device copy, a file written or replaced in between) still falls back to hashing |
| **1.4.4** | Observability: the **set-level and final verification passes each log a start and an end line** (the start line carries an `ETA ~` extrapolated from the first file actually hashed; the end line gives files, bytes, elapsed time, achieved rate and a failure count). The two long silent phases now have visible boundaries; **no per-file lines**, and `verify --quick` prints nothing because it hashes nothing. **Verification behaviour is unchanged** |
| **1.4.3** | Field verification: a **stale upstream metadata artifact no longer blocks an install** - the `-metadata.json` is only an authority when the build date it claims matches the volume fingerprints (then `files` / `bytes-total` / `number-of-volumes` disagreements are still **fatal**); otherwise it is downgraded to a warning (`stale, ignored`) and the payload is judged by per-file md5, the manifest, the `.nin` fingerprints and contiguous volume ordinals. Also fixes the **aria2c completion watcher**, which never matched real logs (`[NOTICE] [RequestGroup.cc:1214] Download complete:`) and left the counter at `files 0/N` |
| **1.4.2** | Documentation round: [`docs/FAQ.md`](docs/FAQ.md) (symptom → cause → action, seven groups), linked from the documentation index of both READMEs |
| **1.4.1** | Documentation engineering: `CONTRIBUTING.md`, `docs/DESIGN.md`, `docs/OPERATIONS.md`; the documentation tests were extended to audit links, anchors, option parity and both languages across **all** files |
| **1.4.0** | An **English README** (complete, not abbreviated) and an explicit **dependency contract** (genuinely required vs optional: `aria2c`, `quota`, `df`, `blastdbcmd`, plus the tools that are never invoked); fixed `doctor` describing `gsutil`/`gcloud`/`aws` as being for authenticated downloads when access is plain anonymous HTTPS |
| **1.3.1** | Audit round 3: installing one database no longer wipes another database's staging; a torn retry no longer deletes the tree that salvage needs; files without an authoritative checksum are never reused; `--limit-rate` works in both downloaders; `aria2c` aborts escalate from SIGTERM to SIGKILL; a corrupt state file no longer breaks `list`/`gc`/`rollback` |
| **1.3.0** | Logging round: `--console`, a default `<root>/log`, `--progress-interval` default 3600 s, one log line per finished file (also for aria2c, by tailing its log) |
| **1.2.0** | Audit round 2: strict verification semantics (no size-only proofs), NCBI metadata size from `Content-Length`, prompt abort, archives beaten by extracted members, quota-aware reports, dead code removed |
| **1.1.0** | Field feedback: `-k/--min-split-size` had no effect at all; the cloud listing missed `taxonomy4blast.sqlite3`; failure reasons never reached `--log-file`; `--log-file`, terminal-free progress, reason-aware retries, `--min-free`, `doctor`, `staging`, `--adopt*`, archives released after unpacking, extraction receipts |
| **1.0.0** | First release: set-level consistency, atomic `current`, three-layer resume, `inspect` |

---

## License and provenance

Released under the **MIT** license (see `LICENSE`), © 2026 lyz.

This is an **independent implementation**, not a translation of NCBI's
`update_blastdb.pl`: its behaviour, the cloud `latest-dir` semantics, the
`blastdb-metadata-1-1.json` manifest format and the `.nin`/`.pin` volume index
header were all derived by observing NCBI's public endpoints and public data, and
no upstream source code was copied. The comparison table above refers to
`update_blastdb.pl`, which NCBI publishes under the *NCBI Public Domain Notice*
(SPDX: `NCBI-PD`), authored by Christiam Camacho; upstream sources are **not**
bundled with this project. If this tool helps your work, please also cite
BLAST+: Camacho C, et al. *BLAST+: architecture and applications.* BMC
Bioinformatics. 2009;10:421.

This project is not affiliated with NCBI/NLM and does not speak for it. BLAST and
NCBI are trademarks of their respective owners. The databases downloaded by this
tool are provided by NCBI, which places no restriction on the use or distribution
of the data on its site (<https://www.ncbi.nlm.nih.gov/home/about/policies/>);
individual databases can contain third-party data, so check the terms that apply
to your use case.

---

## Appendix: why this is necessary

### A.1 The NCBI directory is mutable in place

`https://ftp.ncbi.nlm.nih.gov/blast/db/` replaces files one by one while a
release is published. `nt` has 383 volumes and `nr` 176; during the window
`nt.000.tar.gz` can already be the new build while `nt.050.tar.gz` is still the
old one.

`update_blastdb.pl` verifies single files: each one is individually valid, but
the **set is torn**. BLAST does not complain; it just returns wrong results. A
hand-written `for i in {000..376}; do aria2c ...; done` is worse still: it never
even fixes a starting revision, so the spread can be a whole day.

### A.2 The cloud buckets are snapshot directories plus a pointer

```
latest-dir               -> 2026-07-21-01-05-02      <- the only authoritative pointer
2026-07-21-01-05-02/     <- complete snapshot (10168 objects, raw index files)
2026-09-19-01-05-02/     <- has blastdb-metadata-1-1.json
2026-09-22-01-05-02/     <- newer name, but the manifest is still 404 (being populated)
```

Measured on 2026-09-23: `latest-dir` still pointed at `2026-07-21-01-05-02` while
`2026-09-22-01-05-02/` already existed **without a manifest**. Any logic that
picks "the newest looking directory" downloads an incomplete database.

Also, the cloud layout is raw index files: **S3 ETags for large objects are
multipart hashes** (`"...-358"`), not md5; GCS `md5Hash` is a real md5.

### A.3 Every volume embeds its own build fingerprint

The volume index file (`.nin` for nucleotide, `.pin` for protein) starts with:

```
u32 version(5) | u32 dbtype(0=nucl,1=prot) | u32 volume ordinal | title | blob basename | build timestamp
```

Measured by reading the first 512 bytes of cloud objects:

| File | Embedded ordinal | Embedded build timestamp |
|---|---|---|
| `nt.000.nin` / `nt.001.nin` / `nt.002.nin` / `nt.175.nin` | 0 / 1 / 2 / 175 | all `Jul 19, 2026  3:10 AM` |
| `core_nt.00.nin` / `core_nt.01.nin` | 0 / 1 | all `Jul 18, 2026  1:17 AM` |
| `nr.000.pin` / `nr.001.pin` | 0 / 1 | all `Jul 14, 2026 12:42 AM` |

and `<db>.njs` repeats the same instant as `last-updated: 2026-07-19T03:10:00`.
`<db>-<type>-metadata.json` usually agrees too, but it can lag by a day (`nt` in
the `2026-07-21-01-05-02` snapshot does - see A.4), so **same-build judgments use
the embedded timestamps only**.

> **Consequence:** two volumes belong to the same build if and only if their
> embedded build timestamps are equal. That judgment is made entirely locally and
> does not depend on anyone's metadata, so a release published during the
> transfer cannot fool it.

### A.4 The payload metadata is a complete file list (but upstream may ship an old copy)

`<db>-nucl-metadata.json` also carries a `files` list, `number-of-volumes` and
`bytes-total`. Within one build these agree exactly with what is on disk (file
set identical, byte total equal), which makes it an offline completeness check:
a missing file, an extra file or a truncated file is visible immediately, with
no network access and without computing a single md5.

It is, however, a *derived* artifact whose **summary fields can disagree with
the payload it ships with**, while the payload itself is consistent.  Measured in
the real `2026-07-21-01-05-02` snapshot (`nt`, 345 volumes, 1.07 TB):

| Source | `last-updated` | `bytes-total` | file names |
|---|---|---|---|
| manifest `blastdb-metadata-1-1.json`, `nt` summary | 2026-07-20T00:00:00 | 1063128812728 | 3112 |
| `nt-nucl-metadata.json` in the snapshot | 2026-07-20T00:00:00 | 1063128812728 | 3112 (same names as the manifest) |
| payload `nt.njs` | 2026-07-19T03:10:00 | 1074151309400 | 3111 (it does not list itself) |
| the 345 volumes' embedded `.nin` stamps | `Jul 19, 2026  3:10 AM` | - | - |
| payload on disk (every per-file md5 checked) | - | 1074151365765 | 3112 |

The file **names agree** (3112 = payload + `nt.njs`); only the **summary fields**
differ, by one day and about 10.2 GB.  In other words the payload is consistent
(per-file md5s, `.nin` fingerprints and `.njs` all agree) and what disagrees is
the summary.  Do not conclude from it that your data is the older build, and do
not use `bytes-total` to judge completeness.  The tool
therefore asks first whether the artifact claims the same build date as the
volume fingerprints. If it does, it is an authority and any disagreement in
`files` / `bytes-total` / `number-of-volumes` is **fatal**. If it does not, the
artifact is treated as foreign, its claims are downgraded to **warnings** that
name it as stale (look for `stale, ignored` in the log), and the payload is
judged by what was actually proven: per-file md5, the manifest, the embedded
`.nin` fingerprints, volume ordinals being contiguous `0..N-1`, and `<db>.njs`.
Set completeness is **unchanged** - a genuinely missing volume is still stopped
by "the volume set is not contiguous from 0".
