#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lyz  (see LICENSE)
"""
blastdb_download.py -- consistent, verified, parallel mirroring of the
pre-formatted NCBI BLAST databases.

WHY THIS EXISTS (the internal principles it relies on)
======================================================
`update_blastdb.pl` verifies one file at a time (the `.md5` sidecar of each
`*.tar.gz`) and compares mtimes.  That cannot keep a *set* of files consistent,
and a BLAST database IS a set of volumes that must all come from one build:

1. https://ftp.ncbi.nlm.nih.gov/blast/db/ is MUTABLE.  While NCBI publishes a
   new release, `nt.000.tar.gz` may already be the new build while
   `nt.050.tar.gz` is still the old one.  Per-file md5 verification cannot see
   that: every file is individually valid, the *set* is torn.

2. The cloud mirrors (GCS bucket `blast-db`, S3 bucket `ncbi-blast-databases`)
   are organised as SNAPSHOT DIRECTORIES plus a tiny `latest-dir` pointer.
   Only `latest-dir` is authoritative: a directory such as
   `2026-09-22-01-05-02/` can already hold database files while its
   `blastdb-metadata-1-1.json` is still missing (HTTP 404) - it is a
   half-populated work in progress.  Choosing "the newest looking directory
   name" downloads an incomplete database.  (Both facts were verified live.)

3. Every BLAST volume carries an EMBEDDED REVISION FINGERPRINT.  The volume
   index file (`<db>.<vol>.nin` nucleotide, `.pin` protein) starts with

       u32 version(5) | u32 dbtype(0=nucl,1=prot) | u32 volume_ordinal
       then length-prefixed title / blob basename / build timestamp

   e.g. all of `nt.000.nin ... nt.175.nin` contain "Jul 19, 2026  3:10 AM"
   while `core_nt.00.nin ...` contain "Jul 18, 2026  1:17 AM", and `<db>.njs`
   repeats that instant as ISO-8601 `last-updated`.

   => Two volumes belong to the same build iff their embedded timestamps agree.
      This is checkable offline, after the fact, without trusting the manifest.

THE PROTOCOL
============
  revision(db) := { (name, size, md5, remote token) ... }      (canonical)
  revkey(db)   := sha1(source | snapshot | db | revision(db))

  * resolve the source snapshot (cloud: the object `latest-dir` points at;
    ncbi: the live manifest, whose per-file `.md5` sidecars define the revision)
  * files byte-identical to what is already installed are hard-linked instead of
    re-downloaded
  * everything else goes to `.staging/<db>/<revkey>/`, so an aria2/partial
    resume state can never be reused for a different revision
  * every downloaded file is checked against its authoritative md5 (NCBI `.md5`
    sidecar, GCS `md5Hash`; S3 multipart ETags are NOT md5 and are not trusted)
  * the source revision is re-read AFTER the transfer; if it moved, staging is
    discarded and the database is retried or the run aborts - a mixed set is
    never installed
  * the SET is verified before installation: all volume indexes must expose the
    same embedded build timestamp, ordinals must be exactly 0..N-1 and must
    match the numbers in the file names, and `<db>.njs` must agree
  * only then is a NEW snapshot directory assembled (hard links) and
    `<root>/current` atomically re-pointed at it.  Readers using
    `BLASTDB=<root>/current` can never observe a half-updated database, and a
    rollback is one symlink swap.
  * the snapshot state file records, per file, the md5 that was proven, where
    that proof came from and which archive produced the bytes, so `verify` can
    re-check the mirror later with no network access at all.

EXIT CODES
  0 success    1 usage/runtime error    3 verification failure    4 torn/aborted
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import datetime as dt
import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:                                    # Python >= 3.11
    import tomllib
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None

__version__ = "1.4.2"
PROGRAM = "blastdb_download.py"
STATE_BASENAME = ".blastdb-download.json"
STATE_FORMAT = 1

NCBI_HOST = "ftp.ncbi.nlm.nih.gov"
NCBI_DEFAULT_DIR = "/blast/db"
NCBI_DEFAULT_BASE = f"https://{NCBI_HOST}"
GCS_ROOT = "https://storage.googleapis.com"
GCS_BUCKET = "blast-db"
S3_ROOT = "https://s3.amazonaws.com"
S3_BUCKET = "ncbi-blast-databases"
METADATA_JSON = "blastdb-metadata-1-1.json"
MANIFEST_VERSIONS = {"1.1"}

EXIT_OK, EXIT_ERR, EXIT_VERIFY, EXIT_TORN = 0, 1, 3, 4
UA = f"{PROGRAM}/{__version__}"


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
class Log:
    """Diagnostics go to stderr so that stdout stays machine parseable.

    A long download is normally started from a terminal, cron or a queue and
    watched later, so everything can also be written to a file with
    `--log-file`.  The console stays clean (`-q` silences it) while the file
    still receives every line the chosen verbosity allows.
    """

    def __init__(self) -> None:
        self.level = 1
        self.quiet = False
        self.timestamps = False
        self.path = None
        self.console = "auto"     # auto | full | errors | off
        self._fh = None
        self._lock = threading.Lock()

    # -- output plumbing ---------------------------------------------------- #
    def open_file(self, path: str) -> None:
        self.path = os.path.abspath(path)
        d = os.path.dirname(self.path) or "."
        if d:
            os.makedirs(d, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._emit(f"---- {PROGRAM} {__version__} started: "
                   f"{' '.join(sys.argv[1:])}", need=0, console=False)
        self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.write(f"{_stamp()} ---- finished\n")
                    self._fh.flush()
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def console_enabled(self, need: int = 1, always: bool = False) -> bool:
        """Should this line also go to stderr?

        `--log-file` implies `--console auto`, which keeps the terminal quiet
        while a long transfer runs: progress and info land in the file, only
        warnings and errors stay on screen.  `nohup` therefore produces an
        essentially empty `nohup.out` instead of a second copy of the log.
        """
        if self.console == "off":
            return False
        if always:                       # errors explain a non-zero exit status
            return True
        if self.console == "errors":
            return False
        if self.quiet:
            return False
        if self.console == "auto" and self.path is not None and need > 0:
            return False
        return self.level >= need

    def _emit(self, msg: str, need: int = 1, console: bool = True,
              always: bool = False) -> None:
        line = msg
        with self._lock:
            if self._fh is not None and self.level >= need:
                try:
                    self._fh.write(f"{_stamp()} {line}\n")
                    self._fh.flush()
                except OSError:
                    pass
            if console and self.console_enabled(need, always):
                text = f"{_stamp()} {line}" if self.timestamps else line
                sys.stderr.write(text + "\n")
                sys.stderr.flush()

    # -- levels ------------------------------------------------------------- #
    def error(self, msg: str) -> None:
        """"Errors are never hidden: they explain a non-zero exit status."""
        self._emit(f"ERROR: {msg}", need=0, always=True)

    def warn(self, msg: str) -> None:
        self._emit(f"WARNING: {msg}", need=0)

    def info(self, msg: str) -> None:
        self._emit(msg, need=1)

    def verbose(self, msg: str) -> None:
        self._emit(msg, need=2)

    def debug(self, msg: str) -> None:
        self._emit(f"[debug] {msg}", need=3)

    def raw(self, msg: str) -> None:
        """Transient console output (progress bars) - never logged here."""
        with self._lock:
            if not self.console_enabled(1):
                return
            sys.stderr.write(msg)
            sys.stderr.flush()

    def progress(self, msg: str) -> None:
        """A periodic progress line: kept in the log file, console friendly."""
        with self._lock:
            if self._fh is not None and self.level >= 1:
                try:
                    self._fh.write(f"{_stamp()} {msg}\n")
                    self._fh.flush()
                except OSError:
                    pass
            if not self.console_enabled(1):
                return
            if sys.stderr.isatty():
                sys.stderr.write("\r" + msg[:200].ljust(120)[:200])
            else:
                sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def newline(self) -> None:
        if self.console_enabled(1) and sys.stderr.isatty():
            with self._lock:
                sys.stderr.write("\n")
                sys.stderr.flush()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


LOG = Log()


class BlastError(Exception):
    pass


class VerificationError(BlastError):
    pass


class TornUpdateError(BlastError):
    """The remote revision changed while it was being downloaded."""


class RangeUnsupported(Exception):
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kKmMgGtTpP]?)[bB]?\s*$")
_SIZE_MULT = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30,
              "t": 1 << 40, "p": 1 << 50}


def parse_size(text: str) -> int:
    m = _SIZE_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def human_duration(seconds) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def human_bytes(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0 or unit == "PiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PiB"


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backoff(attempt: int, cap: float = 60.0) -> float:
    return min(cap, 2.0 ** max(0, attempt - 1))


def md5_file(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


PROBE_SPAN = 1 << 16


def file_probe(path: str, span: int = PROBE_SPAN):
    """A cheap content fingerprint: size plus head and tail md5.

    Reusing an already installed file without re-hashing a 3 GiB volume is the
    difference between a one second and a one hour update, but trusting the
    recorded md5 alone would happily carry a truncated or bit-rotted file into
    the next snapshot.  Probing both ends costs ~128 KiB of reads per file and
    catches truncation, prepending/appending and local damage at the edges;
    `verify` and `repair` still do full md5.
    """
    size = os.path.getsize(path)
    head = hashlib.md5()
    tail = hashlib.md5()
    with open(path, "rb") as fh:
        head.update(fh.read(span))
        if size > span:
            back = min(span, size - span) if size >= 2 * span else 0
            if back:
                fh.seek(size - back)
                tail.update(fh.read(back))
    return {"size": size, "head": head.hexdigest(), "tail": tail.hexdigest()}


def probe_matches(path: str, recorded) -> bool:
    if not recorded:
        return False
    try:
        return file_probe(path) == {"size": recorded.get("size"),
                                    "head": recorded.get("head"),
                                    "tail": recorded.get("tail")}
    except OSError:
        return False


def probe_of(meta):
    if not meta:
        return None
    p = meta.get("probe")
    if not p:
        return None
    return {"size": p.get("size"), "head": p.get("head"), "tail": p.get("tail")}


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def b64_to_hex(b64: str) -> str:
    return base64.b64decode(b64).hex()


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(d)


def atomic_write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return "link"
    except OSError as exc:
        if (exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP,
                              errno.EMLINK, errno.EACCES, errno.ENOSYS,
                              errno.EINVAL)
                and getattr(exc, "winerror", None) != 1314):
            raise
        shutil.copy2(src, dst)
        return "copy"


_WRITE_FALLBACK_LOCK = threading.Lock()


def write_at(fd: int, data: bytes, offset: int) -> None:
    """pwrite() with a portable fallback (Windows has no os.pwrite)."""
    if hasattr(os, "pwrite"):
        os.pwrite(fd, data, offset)
        return
    with _WRITE_FALLBACK_LOCK:
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, data)


def remove_quietly(path: str) -> None:
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def truncate_to_zero(path: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return
    try:
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def dir_size(path: str) -> int:
    total = 0
    seen = set()
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def free_space(path: str):
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# environment / quota self-check
# --------------------------------------------------------------------------- #
# Tools we may call.  `kind` decides how they are probed and how the doctor
# report groups them; `install` is only ever printed, never executed.
# Tools the workflow may involve.  `role` is deliberately explicit: this tool
# speaks plain anonymous HTTPS, so the cloud CLIs are never invoked by it - they
# are only listed because an operator may reach for them by hand.
#   runtime : the tool itself calls it (when present)
#   manual  : the tool never calls it; useful for the manual checks in the docs
TOOLS = (
    ("aria2c", "--version", "runtime",
     "the preferred parallel transfer engine; without it the built-in "
     "downloader is used (same correctness, fewer connections)",
     "apt install aria2   # or: conda install -c conda-forge aria2"),
    ("df", "--version", "runtime",
     "filesystem type/size reporting for `doctor` and the space check; "
     "without it free space is read in-process",
     "coreutils"),
    ("quota", "--version", "runtime",
     "per-user quota, which is often far smaller than df's free space",
     "apt install quota   # Debian/Ubuntu\n"
     "     yum install quota   # RHEL/CentOS"),
    ("lfs", "--version", "runtime", "Lustre quota", "lustre-client-utils"),
    ("xfs_quota", "-V", "runtime", "XFS project/user quota", "xfsprogs"),
    ("blastdbcmd", "-version", "runtime",
     "advisory `--smoke-test just before installation",
     "conda install -c bioconda blast"),
    ("blastdbcheck", "-version", "manual",
     "not invoked by this tool; NCBI's own ISAM/TaxID sampling, handy when you "
     "have a BLAST build that can read the database",
     "conda install -c bioconda blast"),
    ("gsutil", "version", "manual",
     "not invoked by this tool: anonymous HTTPS already covers GCS",
     "pip install gsutil"),
    ("gcloud", "version", "manual",
     "not invoked by this tool: listed only for GCP project administration",
     "see https://cloud.google.com/sdk/docs/install"),
    ("aws", "--version", "manual",
     "not invoked by this tool: anonymous HTTPS already covers the S3 bucket",
     "pip install awscli"),
    ("tar", "--version", "manual",
     "not invoked by this tool: archives are unpacked in-process",
     "tar"),
)


def _run_short(cmd, timeout=10):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or proc.stderr or "").strip()


def probe_tools() -> dict:
    """Which helpers exist, and what version they report."""
    found = {}
    for name, flag, role, why, install in TOOLS:
        path = shutil.which(name)
        entry = {"path": path, "why": why, "install": install, "role": role,
                 "version": None}
        if path:
            out = _run_short([path, flag])
            if out:
                entry["version"] = out.splitlines()[0][:120]
        found[name] = entry
    return found


def filesystem_info(path: str):
    """df -PT for the filesystem holding `path`."""
    probe = os.path.abspath(path)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    out = _run_short(["df", "-PT", probe]) or _run_short(["df", "-P", probe])
    if not out:
        return None
    lines = out.splitlines()
    if len(lines) < 2:
        return None
    # decide the layout by field count, not by whether a column "looks right":
    # `df -PT` on WSL/DrvFs prints "?" for the fs type and a naive column guess
    # then reports used space as free space
    parts = lines[1].split()
    if len(parts) >= 7:                       # fs type blocks used avail cap mnt
        fs, fs_type, size, used, avail = (parts[0], parts[1], parts[2],
                                          parts[3], parts[4])
    elif len(parts) == 6:                     # fs blocks used avail cap mnt
        fs, fs_type, size, used, avail = (parts[0], "?", parts[1], parts[2],
                                          parts[3])
    else:
        return None
    if not all(_looks_numeric(x) for x in (size, used, avail)):
        return None
    return {"path": probe, "filesystem": fs, "type": fs_type,
            "size": int(size) * 1024, "used": int(used) * 1024,
            "free": int(avail) * 1024}


def _looks_numeric(text: str) -> bool:
    return str(text).isdigit()


def detect_quota(user: str, path: str):
    """Best effort per-user quota for the filesystem holding `path`.

    A parallel filesystem routinely reports terabytes of free space while a user
    quota caps the same user at a few hundred gigabytes; the pre-flight space
    check must use the smaller of the two.  Only tools that exist are used, and
    any unparsable output simply yields None.
    """
    info = filesystem_info(path)
    mount = None
    if info and info.get("filesystem", "").startswith("/"):
        mount = info["filesystem"]
    results = []
    if shutil.which("quota"):
        out = _run_short(["quota", "-uvs", user])
        if out:
            parsed = _parse_quota_uvs(out, mount)
            if parsed:
                parsed["tool"] = "quota -uvs"
                results.append(parsed)
    if shutil.which("lfs") and mount:
        out = _run_short(["lfs", "quota", "-u", user, mount])
        if out:
            parsed = _parse_lfs_quota(out)
            if parsed:
                parsed["tool"] = "lfs quota"
                parsed["filesystem"] = mount
                results.append(parsed)
    if shutil.which("xfs_quota") and mount:
        out = _run_short(["xfs_quota", "-x", "-c",
                          f"report -h -u {user}", mount])
        if out:
            parsed = _parse_xfs_report(out)
            if parsed:
                parsed["tool"] = "xfs_quota report"
                parsed["filesystem"] = mount
                results.append(parsed)
    if not results:
        return None
    # use the tightest remaining allowance we could find
    best = None
    for entry in results:
        remaining = None
        if entry.get("limit") and entry.get("used") is not None:
            remaining = max(0, entry["limit"] - entry["used"])
        entry["remaining"] = remaining
        if remaining is not None and (best is None
                                      or remaining < best["remaining"]):
            best = entry
    return best if best is not None else results[0]


def _parse_quota_uvs(text: str, mount=None):
    """Parse `quota -uvs` (human units) for the filesystem of interest."""
    candidates = []
    for line in text.splitlines():
        if not line.strip() or "Disk quotas for" in line or "*** Report" in line:
            continue
        parts = line.split()
        if parts and parts[0] == "Filesystem":
            continue
        if parts and (parts[0].startswith("/") or parts[0].startswith("(")):
            candidates.append(parts)
    if not candidates:
        return None
    row = None
    if mount:
        for parts in candidates:
            if parts[0].startswith(mount):
                row = parts
                break
    if row is None:
        row = candidates[0]
    try:
        used = _size_from_quota(row[1])
        soft = _size_from_quota(row[2]) if len(row) > 2 else None
        hard = _size_from_quota(row[3]) if len(row) > 3 else None
    except (ValueError, IndexError):
        return None
    limit = soft or hard
    return {"filesystem": row[0], "used": used, "soft": soft, "hard": hard,
            "limit": limit, "raw": " ".join(row)}


def _size_from_quota(token: str):
    token = token.strip().rstrip("*")
    if not token or token in ("0", "-"):
        return None
    try:
        return parse_size(token)
    except argparse.ArgumentTypeError:
        return None


def _parse_lfs_quota(text: str):
    used = limit = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("/"):
            if _looks_numeric(parts[1]):
                used = int(parts[1]) * 1024
            if len(parts) > 2 and _looks_numeric(parts[2]):
                limit = int(parts[2]) * 1024
    if used is None and limit is None:
        return None
    return {"used": used, "soft": limit, "hard": None, "limit": limit,
            "raw": text.strip().splitlines()[-1][:120]}


def _parse_xfs_report(text: str):
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not parts[0].startswith("#") and \
                not parts[0].startswith("Name"):
            try:
                used = _size_from_quota(parts[-3])
                soft = _size_from_quota(parts[-2])
                hard = _size_from_quota(parts[-1])
            except IndexError:
                continue
            if used or soft or hard:
                return {"used": used, "soft": soft, "hard": hard,
                        "limit": soft or hard,
                        "raw": " ".join(parts)[:120]}
    return None


def effective_free(path: str, user: "str | None" = None):
    """Free space worth trusting: the smaller of df and the user quota."""
    disk = filesystem_info(path)
    free = disk.get("free") if disk else free_space(path)
    quota = detect_quota(user or _current_user(), path)
    remaining = (quota or {}).get("remaining")
    if remaining is not None and (free is None or remaining < free):
        return remaining, quota
    return free, quota


def _current_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def collect_environment(path: str, tools=None) -> dict:
    """Everything needed to explain a failure to a human or a ticket."""
    tools = tools or probe_tools()
    disk = filesystem_info(path)
    quota = detect_quota(_current_user(), path)
    free, _ = effective_free(path)
    return {
        "when": utcnow(),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "user": _current_user(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cores": os.cpu_count(),
        "root": os.path.abspath(path),
        "filesystem": disk,
        "quota": quota,
        "effective_free": free,
        "tools": tools,
    }


def format_environment(env: dict, verbose: bool = True) -> list:
    """Human readable environment report (used by `doctor` and by -v)."""
    lines = [f"host {env.get('host')} user {env.get('user')} "
             f"python {env.get('python')} cores {env.get('cores')}"]
    disk = env.get("filesystem") or {}
    if disk:
        lines.append(
            f"filesystem {disk.get('filesystem')} ({disk.get('type')}) "
            f"free {human_bytes(disk.get('free'))}"
            + (f" of {human_bytes(disk.get('size'))}" if disk.get("size") else ""))
    quota = env.get("quota") or {}
    if quota:
        lines.append(
            f"quota ({quota.get('tool')}) on {quota.get('filesystem')}: "
            f"used {human_bytes(quota.get('used'))}"
            + (f", limit {human_bytes(quota.get('limit'))}"
               if quota.get("limit") else ", no limit")
            + (f", remaining {human_bytes(quota.get('remaining'))}"
               if quota.get("remaining") is not None else ""))
    else:
        lines.append("quota: unknown - the `quota` command is not installed "
                     "(apt install quota); on a parallel filesystem the user "
                     "quota can be far smaller than the free space above")
    tools = env.get("tools") or {}
    runtime = sorted(n for n, t in tools.items() if t.get("role") == "runtime")
    manual = sorted(n for n, t in tools.items() if t.get("role") == "manual")
    missing = [n for n in runtime if not tools[n].get("path")]
    lines.append("not installed (all optional): "
                 + (", ".join(missing) if missing else "none"))
    if not verbose:
        return lines
    lines.append("")
    lines.append("built in: python standard library only - no pip packages, no "
                 "cloud CLIs are required")
    lines.append("used at run time when present:")
    for name in runtime:
        info = tools[name]
        lines.append(f"  {name:<12} {(info['path'] or '-'):<34} {info['why']}")
    lines.append("present but never invoked by this tool:")
    for name in manual:
        info = tools[name]
        if not info.get("path"):
            continue
        lines.append(f"  {name:<12} {info['path']:<34} {info['why']}")
    if missing:
        lines.append("")
        lines.append("to install the optional helpers:")
        seen = set()
        for name in missing:
            hint = tools[name]["install"]
            if hint in seen:
                continue
            seen.add(hint)
            lines.append(f"  {name:<12} {hint}")
    return lines


# --------------------------------------------------------------------------- #
# embedded BLAST volume fingerprint
# --------------------------------------------------------------------------- #
_WS_RE = re.compile(r"\s+")
_BUILD_DATE_RE = re.compile(
    rb"([A-Z][a-z]{2} +[0-9]{1,2}, +[0-9]{4} +[0-9]{1,2}:[0-9]{2} +[AP]M)")
_VOL_FILE_RE = re.compile(r"^(?P<db>.+)\.(?P<vol>\d{2,3})\.[A-Za-z0-9]+$")
_INDEX_SUFFIXES = (".nin", ".pin")


class VolumeMarker:
    __slots__ = ("build_date_raw", "dbtype", "ordinal", "title", "version")

    def __init__(self, version, dbtype, ordinal, title, build_date_raw):
        self.version = version
        self.dbtype = dbtype           # 0 nucleotide, 1 protein
        self.ordinal = ordinal
        self.title = title
        self.build_date_raw = build_date_raw

    @property
    def build_date(self):
        return normalise_build_date(self.build_date_raw)

    def __repr__(self) -> str:         # pragma: no cover
        return (f"VolumeMarker(v{self.version}, type={self.dbtype}, "
                f"vol={self.ordinal}, date={self.build_date_raw!r})")


def normalise_build_date(raw):
    """'Jul 21, 2026  5:36 AM' -> '2026-07-21T05:36'; None when unparsable."""
    if not raw:
        return None
    text = _WS_RE.sub(" ", str(raw).strip())
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def parse_volume_marker(head: bytes):
    """Decode the `.nin`/`.pin` header.  Returns VolumeMarker or None.

    Layout (big endian, verified against live NCBI payloads):
        u32 version | u32 dbtype(0/1) | u32 volume_ordinal | length-prefixed
        title | blob basename | build timestamp

    The timestamp is located with a permissive regex plus strptime validation
    rather than by trusting the exact length-prefix convention, which NCBI's
    writer does not apply uniformly across the three string fields.
    """
    if not head or len(head) < 16:
        return None
    version, dbtype, ordinal, _tlen = struct.unpack(">IIII", head[:16])
    if version not in (4, 5) or dbtype not in (0, 1) or ordinal > 1 << 20:
        return None
    m = _BUILD_DATE_RE.search(head[12:512])
    if not m:
        return None
    raw = m.group(1).decode("ascii", "replace")
    title = ""
    run = bytearray()
    for byte in head[16:512]:
        if 32 <= byte < 127:
            run.append(byte)
        else:
            if len(run) >= 8:
                title = run.decode("ascii", "replace")
                break
            run = bytearray()
    return VolumeMarker(version, dbtype, ordinal, title, raw)


def read_marker(path: str):
    try:
        with open(path, "rb") as fh:
            return parse_volume_marker(fh.read(512))
    except OSError:
        return None


def volume_of(name: str):
    """Volume ordinal implied by a file name.

    `nt.007.nhr` -> 7.  `<db>.nin` (single volume databases carry no numeric
    infix) -> 0.  Anything else -> None.
    """
    if name.endswith(".tar.gz"):
        return None
    m = _VOL_FILE_RE.match(name)
    if m:
        return int(m.group("vol"))
    if name.endswith(_INDEX_SUFFIXES):
        return 0
    return None


def is_volume_index(name: str) -> bool:
    return name.endswith(_INDEX_SUFFIXES)


# Shared, non-database payload that NCBI bundles inside many archives.
SHARED_TAXONOMY_FILES = ("taxdb.btd", "taxdb.bti", "taxonomy4blast.sqlite3")


_BLOB_RE = re.compile(rb"([A-Za-z0-9_.+-]{2,64}\.(?:ndb|pdb))")


def marker_blob(head: bytes):
    """The blob (`<db>.ndb` / `<db>.pdb`) a volume index refers to."""
    m = _BLOB_RE.search(head[:512] or b"")
    return m.group(1).decode("ascii", "replace") if m else None


def is_contiguous_prefix(path: str, expected_size) -> bool:
    """True when the file is a hole-free prefix of the expected object.

    A sequentially written partial download can safely be resumed from its
    length.  A split download (aria2 with -x > 1) leaves holes, and resuming
    from st_size would then silently corrupt the file, so the data map is
    consulted instead of trusting the size.
    """
    if not hasattr(os, "SEEK_DATA"):
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        end = os.fstat(fd).st_size
        if end <= 0 or (expected_size is not None and end >= expected_size):
            return False
        pos = 0
        while pos < end:
            try:
                data = os.lseek(fd, pos, os.SEEK_DATA)
                hole = os.lseek(fd, data, os.SEEK_HOLE)
            except OSError:            # hole to EOF, or no support at all
                return False
            if data != pos:
                return False
            pos = min(hole, end)
        return True
    finally:
        os.close(fd)


def is_shared_taxonomy(name: str) -> bool:
    return name in SHARED_TAXONOMY_FILES or name.startswith("taxdb.")


def is_alias_or_json(name: str) -> bool:
    return name.endswith((".njs", ".pjs", ".nal", ".pal"))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Http:
    def __init__(self, timeout: float = 60.0, tries: int = 5,
                 insecure: bool = False):
        self.timeout = float(timeout)
        self.tries = max(1, int(tries))
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self._range_ok = {}
        self._range_lock = threading.Lock()

    def _open(self, url, headers=None, method=None, data=None):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept-Encoding", "identity")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return self.opener.open(req, timeout=self.timeout)

    def call(self, url, headers=None, method=None, data=None,
             ok=(200,), allow=(404,), attempts=None):
        """Return (status, headers, body); retries transient failures."""
        attempts = attempts or self.tries
        last = None
        for i in range(attempts):
            try:
                with self._open(url, headers, method, data) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                if exc.code in ok or exc.code in allow:
                    return exc.code, dict(exc.headers or {}), body
                last = exc
                if exc.code in (408, 425, 429, 500, 502, 503, 504):
                    time.sleep(backoff(i + 1))
                    continue
                raise BlastError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, http.client.HTTPException,
                    ConnectionError, TimeoutError, OSError) as exc:
                last = exc
                time.sleep(backoff(i + 1))
        raise BlastError(f"failed to fetch {url}: {last}")

    def get_text(self, url, **kw) -> str:
        _s, _h, body = self.call(url, ok=(200,), allow=(), **kw)
        return body.decode("utf-8", "replace")

    def get_json(self, url, **kw):
        return json.loads(self.get_text(url, **kw))

    def exists(self, url: str) -> bool:
        return self.head_size(url)[0]

    def head_size(self, url: str):
        """(exists, Content-Length or None) - gives unchecksummed files a size."""
        status, headers, _b = self.call(url, method="HEAD", ok=(200,),
                                        allow=(403, 404))
        if status != 200:
            return False, None
        try:
            return True, int(headers.get("Content-Length"))
        except (TypeError, ValueError):
            return True, None

    def range_supported(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url).netloc
        with self._range_lock:
            cached = self._range_ok.get(host)
        if cached is not None:
            return cached
        ok = False
        try:
            with self._open(url, {"Range": "bytes=0-0"}) as resp:
                ok = resp.status == 206
        except Exception:
            ok = False
        with self._range_lock:
            self._range_ok[host] = ok
        LOG.debug(f"Range support on {host}: {ok}")
        return ok

    def read_range(self, url: str, start: int, end, sink,
                   chunk: int = 1 << 18, attempts=None, stop_event=None) -> int:
        """Stream [start, end] (end=None for open ended) into sink(offset, data).

        Resumes inside the range after a failure and returns the bytes
        delivered.  Raises RangeUnsupported when the server ignores Range for a
        request that does not start at 0.

        Uses read1() rather than read(): read() blocks until the whole buffer is
        full, so a stalled or slow connection would leave *nothing* on disk and
        an interrupted transfer would have to start from zero.  read1() writes
        out whatever has arrived, which is what makes resume work.
        """
        attempts = attempts or self.tries
        pos = start
        attempt = 0
        while end is None or pos <= end:
            rng = f"bytes={pos}-" if end is None else f"bytes={pos}-{end}"
            try:
                with self._open(url, {"Range": rng}) as resp:
                    if resp.status == 200:
                        if start > 0:
                            raise RangeUnsupported(url)
                        if pos > 0:                     # restart the whole file
                            sink(0, b"", truncate=True)
                            pos = 0
                    elif resp.status != 206:
                        raise BlastError(f"HTTP {resp.status} for {url}")
                    reader = getattr(resp, "read1", None) or resp.read
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            raise AbortedTransfer("stopped")
                        block = reader(chunk)
                        if not block:
                            break
                        sink(pos, block)
                        pos += len(block)
                break
            except RangeUnsupported:
                raise
            except AbortedTransfer:
                raise
            except Exception as exc:
                if stop_event is not None and stop_event.is_set():
                    raise AbortedTransfer("stopped") from exc
                attempt += 1
                if attempt >= attempts:
                    raise BlastError(f"giving up on {url}: {exc}") from exc
                time.sleep(backoff(attempt))
        return pos - start


# --------------------------------------------------------------------------- #
# download backends
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Token bucket shared by every worker, for `--limit-rate`.

    The flag used to be effective only in aria2 mode, so `--aria2c none
    --limit-rate 50M` silently ignored the limit.  Sleeping in the sink (rather
    than dropping data) simply slows the readers down.
    """

    def __init__(self, bytes_per_second):
        self.rate = float(bytes_per_second or 0)
        self.tokens = self.rate
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, count: int) -> None:
        if not self.rate or count <= 0:
            return
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.updated)
                              * self.rate)
            self.updated = now
            self.tokens -= count
            deficit = -self.tokens
        if deficit > 0:
            time.sleep(deficit / self.rate)


class Progress:
    """Bytes transferred, speed, ETA and file counts.

    On a terminal this keeps one rewritten line; anywhere else (cron, a queue,
    a log file) it emits a plain line every `interval` seconds so that a 12 hour
    download can still be followed with `tail -f`.
    """

    def __init__(self, total: int, label: str, interval: float = 30.0,
                 planned: int = 0):
        self.total = total or 0
        self.label = label
        self.interval = max(1.0, float(interval or 30.0))
        self.planned = planned
        self.files_done = 0
        self.done = 0
        self._lock = threading.Lock()
        self._last_line = 0.0
        self._t0 = time.time()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        def loop():
            while not self._stop.wait(self.interval):
                self.render(force=True)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.render(force=True, final=True)
        LOG.newline()

    def add(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.done += n
            now = time.time()
            if now - self._last_line < 0.5:
                return
            self._last_line = now
        self.render()

    def set_done(self, n: int) -> None:
        """Absolute progress, used by the monitor for the aria2 backend."""
        with self._lock:
            self.done = max(self.done, n)
        self.render()

    def file_finished(self, name: str, size=None, seconds=None,
                      rate=None) -> None:
        """One line per completed file - the useful granularity for nt/nr."""
        with self._lock:
            self.files_done += 1
            files = self.files_done
            elapsed = max(1e-6, time.time() - self._t0)
            average = self.done / elapsed
        detail = []
        if size:
            detail.append(human_bytes(size))
        if seconds:
            detail.append(f"in {human_duration(seconds)}")
        speed = rate or average
        if speed:
            detail.append(f"({human_bytes(speed)}/s)")
        extra = ("  " + " ".join(detail)) if detail else ""
        shown = min(files, self.planned) if self.planned else files
        LOG.info(f"[{shown}/{self.planned or '?'}] {name}{extra}")

    def _text(self, final=False) -> str:
        elapsed = max(1e-6, time.time() - self._t0)
        with self._lock:
            done, files = self.done, self.files_done
        speed = done / elapsed
        parts = [f"{self.label}:"]
        if self.total:
            # retried bytes stay in the counter (that is useful: it shows the
            # cost of retries), so clamp what the operator reads
            pct = min(100.0, 100.0 * done / self.total)
            parts.append(f"{pct:5.1f}%")
            parts.append(f"{human_bytes(done)}/{human_bytes(self.total)}")
        else:
            parts.append(human_bytes(done))
        parts.append(f"{human_bytes(speed)}/s")
        if self.planned:
            parts.append(f"files {min(files, self.planned)}/{self.planned}")
        if not final and self.total and speed > 0 and done < self.total:
            eta = (self.total - done) / speed
            parts.append(f"eta {human_duration(eta)}")
        return " ".join(parts)

    def render(self, force=False, final=False) -> None:
        LOG.progress(self._text(final=final))
        if final:
            LOG.newline()


class FilePlan:
    """One remote file scheduled for download."""

    __slots__ = ("chunk_size", "dest", "done_chunks", "lock", "md5",
                 "md5_origin", "name", "nchunks", "resume_from", "size",
                 "started_at", "state_path", "token", "url")

    def __init__(self, name, url, dest, size, md5, md5_origin, token=None):
        self.name = name
        self.url = url
        self.dest = dest
        self.size = size
        self.md5 = md5
        self.md5_origin = md5_origin
        self.token = token
        self.done_chunks = set()
        self.lock = threading.Lock()
        self.state_path = dest + ".blparts.json"
        self.chunk_size = 0
        self.nchunks = 1
        self.resume_from = 0
        self.started_at = time.time()

    def key(self) -> str:
        return self.dest


class BuiltinBackend:
    """Chunked, resumable, md5 verified parallel downloader (no aria2 needed)."""

    def __init__(self, http: Http, progress: Progress, tries: int,
                 stop_event=None, limit_rate=None):
        self.http = http
        self.progress = progress
        self.tries = tries
        self.stop_event = stop_event
        self.limiter = RateLimiter(limit_rate) if limit_rate else None
        self._fds = {}
        self._fd_lock = threading.Lock()

    def _fd(self, path, truncate=False):
        with self._fd_lock:
            fd = self._fds.get(path)
            if fd is None:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
                self._fds[path] = fd
            if truncate:
                os.ftruncate(fd, 0)
            return fd

    def _sink(self, plan):
        def sink(offset, data, truncate=False):
            fd = self._fd(plan.dest, truncate=truncate)
            if data:
                write_at(fd, data, offset)
                self.progress.add(len(data))
                if self.limiter is not None:
                    self.limiter.consume(len(data))
        return sink

    def _load_state(self, plan):
        plan.done_chunks = set()
        if not os.path.isfile(plan.state_path):
            return
        try:
            state = read_json(plan.state_path)
        except Exception:
            state = None
        if (state and state.get("md5") == plan.md5
                and state.get("size") == plan.size
                and state.get("chunk_size") == plan.chunk_size
                and state.get("nchunks") == plan.nchunks):
            plan.done_chunks = {int(x) for x in state.get("done", [])}
            if plan.done_chunks:
                LOG.verbose(f"resuming {plan.name} with "
                            f"{len(plan.done_chunks)}/{plan.nchunks} chunk(s) done")
        else:
            remove_quietly(plan.state_path)

    def _save_state(self, plan):
        atomic_write_json(plan.state_path, {
            "name": plan.name, "size": plan.size, "md5": plan.md5,
            "chunk_size": plan.chunk_size, "nchunks": plan.nchunks,
            "done": sorted(plan.done_chunks),
        })

    def _plan_chunks(self, plan, connections, min_split):
        if not plan.size or connections <= 1 or plan.size <= min_split:
            plan.nchunks = 1
            plan.chunk_size = plan.size or 0
            return
        n = max(1, min(connections, plan.size // max(1, min_split)))
        plan.nchunks = n
        plan.chunk_size = (plan.size + n - 1) // n

    def _sync(self, plan):
        """Make a file's data durable before we record progress for it."""
        fd = self._fds.get(plan.dest)
        if fd is None:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass

    def _prepare_resume(self, plan):
        """Decide where a single-stream transfer should restart.

        A partially written prefix of a sequential download is always valid, and
        the staging directory it lives in is named after the revision, so it can
        only ever hold bytes of the revision we are fetching now.  The final md5
        check still has the last word.
        """
        plan.resume_from = 0
        try:
            have = os.path.getsize(plan.dest)
        except OSError:
            return
        if have <= 0:
            return
        if plan.size and have > plan.size:
            LOG.warn(f"{plan.name}: staged file is larger than the remote "
                     f"object; starting over")
            truncate_to_zero(plan.dest)
            return
        if plan.size and have == plan.size:
            return                      # let verification decide
        LOG.info(f"resuming {plan.name} at {human_bytes(have)}")
        plan.resume_from = have

    def run(self, plans, jobs, connections, min_split, limit_rate=None) -> dict:
        if not plans:
            return {}
        pending = []
        for plan in plans:
            self._plan_chunks(plan, connections, min_split)
            if plan.nchunks > 1 and not self.http.range_supported(plan.url):
                LOG.verbose(f"{plan.name}: server ignores Range, using one stream")
                plan.nchunks = 1
                plan.chunk_size = plan.size or 0
            self._load_state(plan)
            if plan.nchunks == 1 and not plan.done_chunks:
                self._prepare_resume(plan)
            for idx in range(plan.nchunks):
                if idx in plan.done_chunks:
                    continue
                if plan.chunk_size:
                    start = idx * plan.chunk_size
                    end = min(plan.size - 1, start + plan.chunk_size - 1)
                    if idx == 0 and plan.resume_from:
                        start = plan.resume_from
                else:
                    start, end = plan.resume_from, None
                pending.append((plan, idx, start, end))

        results = {}
        remaining = {p.key(): p.nchunks - len(p.done_chunks) for p in plans}
        finished = set()
        for plan in plans:
            if remaining[plan.key()] <= 0:
                finished.add(plan.key())
                results[plan.key()] = self._finalise(plan)

        last_error = {}

        def run_round(items):
            failures = []
            if not items:
                return failures
            pool = futures.ThreadPoolExecutor(max_workers=max(1, jobs))
            submitted = {}
            try:
                submitted = {pool.submit(self._chunk, plan, idx, start, end):
                             (plan, idx, start, end)
                             for plan, idx, start, end in items}
                while submitted:
                    done, _ = futures.wait(list(submitted),
                                           return_when=futures.FIRST_COMPLETED)
                    for fut in done:
                        plan, idx, start, end = submitted.pop(fut)
                        err = fut.exception()
                        if err is not None:
                            failures.append((plan, idx, start, end, err))
                            last_error[plan.key()] = err
                            continue
                        # flush before recording: a state file must never claim
                        # chunks that are not on disk yet after a hard kill
                        self._sync(plan)
                        with plan.lock:
                            plan.done_chunks.add(idx)
                            remaining[plan.key()] -= 1
                            self._save_state(plan)
                            left = remaining[plan.key()]
                        if left <= 0 and plan.key() not in finished:
                            finished.add(plan.key())
                            results[plan.key()] = self._finalise(plan)
                pool.shutdown(wait=True)
            except BaseException:
                # Ctrl-C, or the space guard firing: tell the workers to stop
                # and do *not* drain the queue.  The docs are explicit that a
                # ThreadPoolExecutor only exits once its pending work is done,
                # so a plain `with` block would keep fetching hundreds of
                # already-queued pieces before the interrupt took effect.
                if self.stop_event is not None:
                    self.stop_event.set()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            return failures

        failures = run_round(pending)
        for _round in range(2):
            if not failures:
                break
            retry = []
            for plan, idx, start, end, err in failures:
                LOG.verbose(f"retrying {plan.name} chunk {idx}: {err}")
                retry.append((plan, idx, start, end))
            failures = run_round(retry)

        for plan in plans:
            if plan.key() not in results:
                stopped = self.stop_event is not None and self.stop_event.is_set()
                if stopped:
                    reason = "transfer aborted"
                else:
                    # keep the real reason (HTTP code, timeouts, ...) so the
                    # failure report can group it instead of saying "it failed"
                    reason = str(last_error.get(plan.key()) or "chunk(s) failed")
                results[plan.key()] = {"ok": False, "error": reason}
            if not results[plan.key()].get("ok"):
                remove_quietly(plan.dest)
                remove_quietly(plan.state_path)
                remove_quietly(plan.dest + ".aria2")

        with self._fd_lock:
            for fd in self._fds.values():
                try:
                    os.fsync(fd)
                except OSError:
                    pass
                os.close(fd)
            self._fds.clear()
        return results

    def _chunk(self, plan, idx, start, end):
        if self.stop_event is not None and self.stop_event.is_set():
            raise AbortedTransfer("stopped")
        self.http.read_range(plan.url, start, end, self._sink(plan),
                             attempts=self.tries, stop_event=self.stop_event)

    def _finalise(self, plan):
        fd = self._fds.get(plan.dest)
        if fd is not None:
            try:
                os.fsync(fd)
            except OSError:
                pass
        ok, why = verify_fetched_file(plan.dest, plan.size, plan.md5)
        if ok:
            remove_quietly(plan.state_path)
            self.progress.file_finished(plan.name, os.path.getsize(plan.dest),
                                        time.time() - plan.started_at)
            return {"ok": True, "size": os.path.getsize(plan.dest)}
        LOG.warn(f"{plan.name}: {why}")
        return {"ok": False, "error": why}


class AbortedTransfer(BlastError):
    """The transfer was stopped on purpose (e.g. the disk filled up)."""


class TransferMonitor:
    """Watch a running transfer: stop before the disk fills, report progress.

    A 1 TiB job that only fails at the very end leaves the operator staring at
    hundreds of "download failed" lines and a full disk.  Polling is cheap and
    turns that into one clear stop, with everything already fetched still
    usable by the next run.  The same thread reports progress for the aria2
    backend, which we cannot instrument from the inside.
    """

    def __init__(self, root, reserve, interval=5.0, progress=None, staging=None,
                 user=None, report_interval=30.0):
        self.root = root
        self.reserve = reserve
        self.interval = max(1.0, float(interval))
        self.progress = progress
        self.staging = staging
        self.user = user
        self.report_interval = max(5.0, float(report_interval))
        self.abort = threading.Event()
        self.free_at_abort = None
        self.quota = None
        self.samples = 0
        self.quota_every = max(1, int(60.0 / self.interval))
        self._thread = None
        self._last_report = 0.0

    def start(self, on_abort=None):
        if not self.reserve and self.progress is None:
            return self

        def loop():
            while not self.abort.is_set():
                free = None
                if self.reserve:
                    self.samples += 1
                    free = free_space(self.root)          # no subprocess
                    if self.samples % self.quota_every == 1:
                        self.quota = detect_quota(self.user or _current_user(),
                                                  self.root)
                        remaining = (self.quota or {}).get("remaining")
                        if remaining is not None and (free is None
                                                      or remaining < free):
                            free = remaining
                    if free is not None and free < self.reserve:
                        self.free_at_abort = free
                        self.abort.set()
                        LOG.warn(f"usable space on {self.root} dropped to "
                                 f"{human_bytes(free)}"
                                 + (f" (quota: {self.quota.get('tool')})"
                                    if self.quota else "")
                                 + f", below the {human_bytes(self.reserve)} "
                                 f"floor; stopping the transfer")
                        if on_abort:
                            on_abort()
                        return
                if self.progress is not None and self.staging:
                    now = time.time()
                    if now - self._last_report >= self.report_interval:
                        self._last_report = now
                        seen = dir_size(self.staging)
                        self.progress.set_done(seen)
                        self.progress.render(force=True)
                        if free is not None:
                            LOG.debug(f"usable space on {self.root}: "
                                      f"{human_bytes(free)}")
                if self.abort.wait(self.interval):
                    return

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.abort.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.interval)
            self._thread = None

    def raise_if_tripped(self):
        if self.free_at_abort is None:
            return
        quota_note = ""
        if self.quota and self.quota.get("limit"):
            quota_note = (f" (the user quota is {human_bytes(self.quota['limit'])}"
                          f", of which {human_bytes(self.quota['used'])} is "
                          f"already used)")
        raise AbortedTransfer(
            f"transfer stopped: only {human_bytes(self.free_at_abort)} of usable "
            f"space left on {self.root} (floor: "
            f"{human_bytes(self.reserve)}){quota_note}. Free space, raise the "
            f"quota, or point --root at a bigger filesystem and re-run the same "
            f"command; verified files and completed chunks are kept under "
            f"{os.path.join(self.root, '.staging')} and are not fetched again")


class Aria2LogWatcher:
    """Turn aria2's own notice lines into per-file progress events.

    aria2 downloads in a separate process, so the tool cannot see individual
    files finish.  Its log can: with `--log-level=notice` every completion is
    written as `[NOTICE] Download complete: <path>`, which is exactly the
    granularity an operator wants for a 3450 file job.
    """

    COMPLETE = re.compile(r"\[NOTICE\]\s+Download complete:\s+(.+?)\s*$")

    def __init__(self, path, progress, sizes=None, interval=1.0):
        self.path = path
        self.progress = progress
        self.sizes = sizes or {}
        self.interval = interval
        self.seen = set()
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0

    def start(self):
        if self.progress is None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, 2 * self.interval))
            self._thread = None
        self._drain()

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._drain()

    def _drain(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                data = fh.read()
                self._offset = fh.tell()
        except OSError:
            return
        for line in data.splitlines():
            match = self.COMPLETE.search(line)
            if not match:
                continue
            name = os.path.basename(match.group(1))
            if name in self.seen:
                continue
            self.seen.add(name)
            self.progress.file_finished(name, self.sizes.get(name))


class Aria2Backend:
    """Drives one aria2c process; verification is done by us, not by aria2."""

    def __init__(self, binary, tries, extra_args=None, show_progress=True):
        self.binary = binary
        self.tries = tries
        self.extra_args = list(extra_args or [])
        self.show_progress = show_progress
        self.log_text = ""

    def run(self, plans, jobs, connections, min_split, limit_rate=None,
            watchdog=None, progress=None) -> dict:
        if not plans:
            return {}
        fd, plan_path = tempfile.mkstemp(prefix="blastdb-aria2-", suffix=".txt")
        log_fd, log_path = tempfile.mkstemp(prefix="blastdb-aria2-",
                                            suffix=".log")
        os.close(log_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for plan in plans:
                fh.write(plan.url + "\n")
                fh.write(f"  out={plan.name}\n")
                fh.write(f"  dir={os.path.dirname(plan.dest)}\n")
                if plan.md5:
                    fh.write(f"  checksum=md5={plan.md5}\n")
                if plan.size is None or plan.size > min_split:
                    fh.write(f"  split={max(1, connections)}\n")
                    fh.write(f"  max-connection-per-server={max(1, connections)}\n")
                    fh.write(f"  min-split-size={min_split}\n")
                fh.write("  continue=true\n  allow-overwrite=true\n")
                fh.write("  auto-file-renaming=false\n")
        cmd = [
            self.binary, "--no-conf",
            "--continue=true", "--allow-overwrite=true",
            "--auto-file-renaming=false",
            # --conditional-get would let aria2 skip a file that the md5 told us
            # to fetch, so it is explicitly disabled.
            "--conditional-get=false",
            "--file-allocation=none",
            f"--max-concurrent-downloads={max(1, jobs)}",
            f"--max-connection-per-server={max(1, connections)}",
            f"--split={max(1, connections)}",
            f"--min-split-size={min_split}",
            f"--max-tries={max(1, self.tries)}",
            "--retry-wait=5", "--connect-timeout=30", "--timeout=120",
            "--console-log-level=warn", "--summary-interval=0",
            # notice level is what carries the per-file completion lines the
            # watcher turns into progress events
            "--log-level=notice", "--log=" + log_path,
            f"--user-agent={UA}",
            "--input-file=" + plan_path,
        ]
        if self.show_progress:
            cmd += ["--show-console-readout=true", "--summary-interval=10"]
        else:
            cmd += ["--show-console-readout=false", "--quiet=true"]
        if limit_rate:
            cmd.append(f"--max-overall-download-limit={int(limit_rate)}")
        cmd.extend(self.extra_args)
        LOG.verbose("running: " + " ".join(cmd))

        def terminate(proc):
            """Ask nicely, then insist.

            SIGTERM lets aria2 save its control files (which is what makes the
            next run resume); if it does not exit within 30 s we escalate, so a
            wedged aria2c cannot leave the tool hanging forever.
            """
            try:
                proc.terminate()
                proc.wait(timeout=30)
                return
            except subprocess.TimeoutExpired:
                LOG.warn("aria2c did not stop on SIGTERM; killing it "
                         "(resume state up to the last flush is preserved)")
            except Exception:
                return
            try:
                proc.kill()
                proc.wait(timeout=15)
            except Exception:
                LOG.warn("aria2c could not be stopped; it may keep writing in "
                         "the staging directory")

        watcher = Aria2LogWatcher(
            log_path, progress,
            sizes={p.name: p.size for p in plans}).start()
        rc = None
        try:
            proc = subprocess.Popen(cmd)
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if watchdog is not None and watchdog.abort.is_set():
                        terminate(proc)
                        rc = proc.poll()
                        break
        except FileNotFoundError as exc:
            raise BlastError(
                f"cannot execute aria2c ({self.binary}): {exc}") from exc
        finally:
            watcher.stop()
            remove_quietly(plan_path)
        LOG.debug(f"aria2c exit status {rc}")
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                self.log_text = fh.read()
        except OSError:
            self.log_text = ""
        remove_quietly(log_path)

        results = {}
        for plan in plans:
            ok, why = verify_fetched_file(plan.dest, plan.size, plan.md5)
            if ok:
                results[plan.key()] = {"ok": True, "size": os.path.getsize(plan.dest)}
            else:
                # aria2 deliberately leaves failed / corrupt payloads behind
                remove_quietly(plan.dest)
                remove_quietly(plan.dest + ".aria2")
                results[plan.key()] = {"ok": False, "error": why,
                                       "from_log": self.reason_for(plan.name)}
        return results

    def reason_for(self, name: str):
        """Best-effort: the last aria2 message that mentions this file."""
        if not self.log_text:
            return None
        hits = [ln.strip() for ln in self.log_text.splitlines()
                if name in ln and ("ERROR" in ln or "rror" in ln)]
        return hits[-1] if hits else None


def verify_local_file(path: str, size, md5):
    """Prove that an *existing* file is the one we want.

    Without a published size or checksum there is nothing to prove, and merely
    existing is not evidence: such a file is rejected so that a truncated or
    stale leftover can never be mistaken for a verified one.
    """
    if not os.path.isfile(path):
        return False, "the downloader did not produce this file"
    got = os.path.getsize(path)
    if size is None and not md5:
        return False, ("no published size or checksum for this file, so its "
                       "identity cannot be verified")
    if size is not None and got != size:
        return False, f"size mismatch (got {got}, expected {size})"
    if md5:
        got_md5 = md5_file(path)
        if got_md5 != md5:
            return False, f"md5 mismatch (got {got_md5}, expected {md5})"
    return True, "ok"


def verify_fetched_file(path: str, size, md5):
    """Check bytes that were just fetched from the authoritative URL."""
    if not os.path.isfile(path):
        return False, "the downloader did not produce this file"
    got = os.path.getsize(path)
    if size is None and not md5:
        # nothing was published to check against; the only thing we can insist
        # on is that something arrived
        return (True, "ok (no published size or checksum)") if got else \
            (False, "the downloader produced an empty file")
    return verify_local_file(path, size, md5)


_ERROR_PATTERNS = (
    (("no space left", "errno=28", "enospc"),
     "no space left on the filesystem"),
    (("quota exceeded", "disk quota"), "filesystem quota exceeded"),
    (("permission denied", "errno=13", "errno=1 "), "permission denied"),
    (("too many open files", "errno=24"), "too many open files"),
    (("404", "not found"), "HTTP 404 not found (source may be mid-publication)"),
    (("403", "forbidden"), "HTTP 403 forbidden (often rate limiting)"),
    (("500", "502", "503", "504"), "HTTP 5xx from the server"),
    (("resolve host", "name or service not known", "nodename"),
     "host name resolution failed"),
    (("certificate", "ssl"), "TLS certificate problem"),
    (("timed out", "timeout"), "timeout"),
    (("checksum", "digest mismatch"), "checksum mismatch"),
    (("refused",), "connection refused"),
    (("reset by peer", "broken pipe", "connection reset"),
     "connection reset by the server"),
)


def normalise_download_error(text) -> str:
    """Turn a backend error message into a short, groupable reason."""
    if not text:
        return "unknown reason (see the full log with -v)"
    low = str(text).lower()
    for needles, reason in _ERROR_PATTERNS:
        if any(n in low for n in needles):
            return reason
    cleaned = re.sub(r"https?://\S+", "<url>", str(text))
    cleaned = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "", cleaned)
    cleaned = re.sub(r"CUID#\d+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:[]")
    return cleaned[:160] or "unknown reason"


def report_failures(bad, results, ctx, plans_total, label) -> None:
    """Explain a batch of failures: grouped reasons and what to do next."""
    groups = {}
    for plan in bad:
        info = results.get(plan.key()) or {}
        reason = normalise_download_error(info.get("from_log") or
                                         info.get("error"))
        groups.setdefault(reason, []).append(plan.name)
    LOG.error(f"{len(bad)}/{plans_total} file(s) failed to download.")
    for reason, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        LOG.error(f"  {len(names):>5} x {reason}")
        LOG.error(f"          e.g. {', '.join(names[:3])}"
                  + ("" if len(names) <= 3 else f" (+{len(names) - 3} more)"))
    free, quota = effective_free(ctx.root, ctx.user)
    if free is not None:
        what = "usable space"
        if quota and quota.get("remaining") is not None and \
                quota["remaining"] <= free:
            what = f"usable space (limited by the {quota.get('tool')} quota)"
        LOG.error(f"  {what} on {ctx.root}: {human_bytes(free)}")
    LOG.error("  nothing was installed; the verified files and completed "
              "chunks are kept under "
              f"{os.path.join(ctx.root, '.staging')}")
    LOG.error("  to continue, fix the cause above and re-run the same command; "
              "already downloaded files are not fetched twice")
    if any("space" in r or "quota" in r for r in groups):
        LOG.error("  space hints: --min-free 50G lowers the abort threshold, "
                  "--limit-rate 50M slows the transfer down, and "
                  "-s gcp avoids the extract step (raw index files instead of "
                  "archives) so nothing is written twice")
    if any("403" in r or "reset" in r or "5xx" in r for r in groups):
        LOG.error("  rate limit hints: reduce the load with -j 4 -x 2 "
                  "(or --limit-rate 50M) and re-run")
    if any("404" in r for r in groups):
        LOG.error("  404 hints: the source is probably publishing a new "
                  "release; wait and re-run, or use -s gcp for the immutable "
                  "snapshot")


def find_aria2c(explicit=None):
    if explicit in ("none", "", "off"):
        return None
    if explicit and explicit != "auto":
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise BlastError(f"--aria2c {explicit}: not an executable file")
    return shutil.which("aria2c")


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
class RemoteFile:
    __slots__ = ("md5", "md5_origin", "name", "role", "size", "token", "url")

    def __init__(self, name, url, size=None, md5=None, md5_origin="none",
                 token=None, role="payload"):
        self.name = name
        self.url = url
        self.size = size
        self.md5 = md5
        self.md5_origin = md5_origin
        self.token = token
        self.role = role

    def state(self) -> dict:
        out = {"size": self.size, "md5": self.md5, "md5_origin": self.md5_origin,
               "role": self.role}
        if self.token:
            out["token"] = str(self.token)
        return out


class DbTarget:
    def __init__(self, dbname, dbtype, description, last_updated, version,
                 source_key, snapshot_id, manifest_url, files, archives,
                 number_of_volumes=None, bytes_total=None,
                 bytes_compressed=None):
        self.dbname = dbname
        self.dbtype = dbtype
        self.description = description
        self.last_updated = last_updated
        self.manifest_version = version
        self.source_key = source_key
        self.snapshot_id = snapshot_id
        self.manifest_url = manifest_url
        self.files = files            # everything that must land in the tree
        self.archives = archives      # transport-only .tar.gz members of `files`
        self.number_of_volumes = number_of_volumes
        self.bytes_total = bytes_total
        self.bytes_compressed = bytes_compressed
        self.keep_archives = False

    def signature(self) -> str:
        parts = [self.source_key, str(self.snapshot_id), self.dbname,
                 self.dbtype or "", self.last_updated or "",
                 str(self.number_of_volumes or "")]
        for rf in sorted(self.files, key=lambda x: x.name):
            parts.append(f"{rf.name}:{rf.md5 or rf.size or '?'}:{rf.token or ''}")
        return "\n".join(parts)

    def revkey(self) -> str:
        return sha1_hex(self.signature())[:16]

    def expected_download_bytes(self):
        if self.bytes_compressed and self.archives:
            return self.bytes_compressed
        sizes = [rf.size for rf in self.files if rf.size]
        return sum(sizes) if sizes else None

    def expected_disk_bytes(self):
        """Peak space needed while installing this database.

        Archives are unpacked into the staging directory, so at the moment an
        archive is being extracted both it and its payload exist.  Archives are
        dropped from staging as soon as they are unpacked (unless
        --keep-archives), so the peak is `payload + one archive` rather than
        `payload + all archives`.
        """
        if self.archives:
            payload = self.bytes_total or 0
            if self.keep_archives:
                return max(payload + (self.bytes_compressed or 0),
                           self.bytes_compressed or 0) or None
            nvol = max(1, self.number_of_volumes or len(self.archives))
            one_archive = (self.bytes_compressed or 0) // nvol
            return (payload + one_archive) or None
        return self.expected_download_bytes()


def validate_manifest(obj, url: str) -> None:
    if isinstance(obj, list) and not obj:
        raise TornUpdateError(
            f"{url} lists no databases at all; the snapshot is most likely "
            f"still being published. Wait and retry, or use another source.")
    if not isinstance(obj, list):
        raise BlastError(f"invalid BLAST database manifest at {url}: "
                         f"expected a JSON array")
    first = obj[0]
    if not isinstance(first, dict):
        raise BlastError(f"invalid manifest entry in {url}")
    if first.get("version") not in MANIFEST_VERSIONS:
        LOG.warn(f"manifest {url} declares version {first.get('version')!r}, "
                 f"expected one of {sorted(MANIFEST_VERSIONS)}")


def meta_suffix(dbtype):
    d = (dbtype or "").lower()
    return "prot" if d.startswith("prot") else "nucl"


class Source:
    key = "?"

    def __init__(self, ctx):
        self.ctx = ctx
        self.http = ctx.http

    def resolve(self):                      raise NotImplementedError
    def snapshot_id(self):                  raise NotImplementedError
    def list_databases(self):               raise NotImplementedError
    def revision(self, dbname, fresh=False): raise NotImplementedError
    def invalidate(self):                   pass

    def snapshot_label(self) -> str:
        return str(self.snapshot_id())

    def describe(self) -> str:
        return f"{self.key}:{self.snapshot_label()}"


class CloudSource(Source):
    """A GCS/S3 snapshot directory addressed through the `latest-dir` pointer."""

    def __init__(self, ctx, key, root, bucket):
        super().__init__(ctx)
        self.key = key
        self.root = root
        self.bucket = bucket
        self.snapshot = None
        self._listings = {}          # (prefix, ...) -> {name: metadata}
        self._manifest = None
        self._manifest_url = ""

    def resolve(self) -> None:
        url = f"{self.root}/{self.bucket}/latest-dir"
        text = self.http.get_text(url).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}(-\d{2}-\d{2}-\d{2})?$", text):
            raise BlastError(f"unexpected latest-dir content at {url}: {text!r}")
        self.snapshot = text
        LOG.verbose(f"{self.key}: latest-dir = {text}")

    def snapshot_id(self):
        return self.snapshot

    def invalidate(self):
        self._listings = {}
        self._manifest = None

    def listing_for(self, prefixes, fresh=False):
        """Object metadata for just the prefixes we care about.

        Listing the whole snapshot means ~10k objects (about 16 MB of JSON and
        19 s for the 2026-07 snapshot) even when a single small database is
        being installed, so the listing is scoped to the requested database.
        """
        key = tuple(prefixes)
        if fresh or key not in self._listings:
            self._listings[key] = self._list_objects(prefixes)
        return self._listings[key]

    def latest_dir_moved(self):
        """Did `latest-dir` start pointing somewhere else?

        Reported for information only, and always False so the caller still
        skips the expensive re-listing: the snapshot we pinned is immutable, so
        a *newer* pointer cannot invalidate what we are installing.
        """
        try:
            url = f"{self.root}/{self.bucket}/latest-dir"
            text = self.http.get_text(url).strip()
        except BlastError as exc:
            LOG.warn(f"{self.key}: cannot re-read latest-dir ({exc}); "
                     f"continuing with the pinned snapshot {self.snapshot}")
            return False
        if text != self.snapshot:
            LOG.info(f"{self.key}: latest-dir now points at {text}; the "
                     f"snapshot being installed ({self.snapshot}) stays pinned")
        return False

    def manifest(self, fresh=False):
        if self._manifest is None or fresh:
            url = f"{self.root}/{self.bucket}/{self.snapshot}/{METADATA_JSON}"
            status, _h, body = self.http.call(url, ok=(200,), allow=(403, 404))
            if status != 200:
                raise TornUpdateError(
                    f"snapshot {self.snapshot} of {self.key} has no "
                    f"{METADATA_JSON} yet (HTTP {status}) - it is still being "
                    f"published. Wait and retry, or use --source ncbi")
            obj = json.loads(body.decode("utf-8"))
            validate_manifest(obj, url)
            self._manifest = obj
            self._manifest_url = url
        return self._manifest

    def url_for(self, name):
        return f"{self.root}/{self.bucket}/{self.snapshot}/{name}"

    def _list_objects(self, prefixes):
        raise NotImplementedError

    def list_databases(self):
        return sorted(({
            "dbname": e.get("dbname"),
            "dbtype": e.get("dbtype"),
            "description": e.get("description"),
            "last_updated": e.get("last-updated"),
            "bytes_total": e.get("bytes-total"),
            "bytes_compressed": e.get("bytes-total-compressed"),
            "number_of_volumes": e.get("number-of-volumes"),
            "number_of_sequences": e.get("number-of-sequences"),
            "number_of_letters": e.get("number-of-letters"),
        } for e in self.manifest()), key=lambda x: x["dbname"] or "")

    def revision(self, dbname, fresh=False):
        entry = None
        for item in self.manifest():
            if item.get("dbname") == dbname:
                entry = item
                break
        if entry is None:
            raise BlastError(f"{dbname}: not present in snapshot "
                             f"{self.snapshot} of source {self.key}")
        # Only the objects this database owns, never the whole snapshot.  The
        # prefixes are a fast path, not the authority: a database may ship files
        # whose name does not start with its own (taxdb carries
        # taxonomy4blast.sqlite3), so anything the manifest names but the
        # prefixes missed is looked up by exact name below.
        prefixes = [f"{self.snapshot}/{dbname}.", f"{self.snapshot}/{dbname}-"]
        if fresh and self._listings and not self.latest_dir_moved():
            # A cloud snapshot directory is immutable and its manifest names
            # exactly the same objects, so re-listing them would prove nothing
            # new: every byte we installed was already verified against the
            # md5Hash read at plan time.  Only a *moved* pointer or a rewritten
            # manifest would matter.
            LOG.debug(f"{self.key}: snapshot {self.snapshot} unchanged; "
                      f"skipping the object re-listing")
            listing = self.listing_for(prefixes)
        else:
            listing = self.listing_for(prefixes, fresh=fresh)
        listed = [str(u).rstrip("/").rsplit("/", 1)[-1]
                  for u in entry.get("files", [])]
        absent = [n for n in listed if n not in listing]
        if absent:
            LOG.debug(f"{dbname}: {len(absent)} file(s) are not under the "
                      f"database prefixes ({', '.join(absent[:3])}); looking "
                      f"them up by name")
            listing = dict(listing)
            listing.update(self.listing_for(
                [f"{self.snapshot}/{n}" for n in absent], fresh=fresh))
            absent = [n for n in listed if n not in listing]
        files, missing = [], []
        for uri in entry.get("files", []):
            name = uri.rstrip("/").rsplit("/", 1)[-1]
            meta = listing.get(name)
            if meta is None:
                missing.append(name)
                continue
            files.append(RemoteFile(name, self.url_for(name), meta.get("size"),
                                    meta.get("md5"),
                                    meta.get("md5_origin", "none"),
                                    meta.get("token")))
        if missing:
            raise TornUpdateError(
                f"{dbname}: {len(missing)} manifest file(s) are absent from "
                f"snapshot {self.snapshot} (e.g. {', '.join(missing[:3])}); the "
                f"snapshot is incomplete")
        if self.ctx.add_metadata_json:
            mname = f"{dbname}-{meta_suffix(entry.get('dbtype'))}-metadata.json"
            meta = listing.get(mname)
            if meta is not None:
                files.append(RemoteFile(mname, self.url_for(mname),
                                        meta.get("size"), meta.get("md5"),
                                        meta.get("md5_origin", "none"),
                                        meta.get("token"), role="metadata"))
            else:
                LOG.debug(f"{dbname}: no {mname} in snapshot {self.snapshot}")
        return DbTarget(dbname, entry.get("dbtype"), entry.get("description"),
                        entry.get("last-updated"), str(entry.get("version")),
                        self.key, self.snapshot, self._manifest_url, files, [],
                        entry.get("number-of-volumes"), entry.get("bytes-total"),
                        entry.get("bytes-total-compressed"))


class GcpSource(CloudSource):
    def __init__(self, ctx):
        super().__init__(ctx, "gcp", GCS_ROOT, GCS_BUCKET)

    def _list_objects(self, prefixes):
        base = f"{GCS_ROOT}/storage/v1/b/{self.bucket}/o"
        out, pages = {}, 0
        for prefix in prefixes:
            token = None
            while True:
                q = (f"{base}?prefix={urllib.parse.quote(prefix, safe='')}"
                     f"&maxResults=1000"
                     f"&fields=items(name,size,md5Hash,generation),nextPageToken")
                if token:
                    q += "&pageToken=" + urllib.parse.quote(token, safe="")
                data = self.http.get_json(q)
                for item in data.get("items", []):
                    name = item["name"].split("/", 1)[1]
                    md5 = None
                    if item.get("md5Hash"):
                        try:
                            md5 = b64_to_hex(item["md5Hash"])
                        except Exception:
                            md5 = None
                    out[name] = {
                        "size": int(item["size"]) if item.get("size") else None,
                        "md5": md5,
                        "md5_origin": "gcs-md5Hash" if md5 else "none",
                        "token": item.get("generation")}
                token = data.get("nextPageToken")
                pages += 1
                if not token or pages > 500:
                    break
        LOG.verbose(f"gcp: {len(out)} object(s) listed under "
                    f"{', '.join(prefixes)}")
        return out


class AwsSource(CloudSource):
    def __init__(self, ctx):
        super().__init__(ctx, "aws", S3_ROOT, S3_BUCKET)

    def _list_objects(self, prefixes):
        base = f"{S3_ROOT}/{self.bucket}/"
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        out, pages = {}, 0
        for prefix in prefixes:
            token = None
            while True:
                q = (f"{base}?list-type=2&max-keys=1000"
                     f"&prefix={urllib.parse.quote(prefix, safe='')}")
                if token:
                    q += "&continuation-token=" + urllib.parse.quote(token,
                                                                     safe="")
                _s, _h, body = self.http.call(q, ok=(200,), allow=())
                root = ET.fromstring(body)
                for node in root.findall(f"{ns}Contents"):
                    key = node.findtext(f"{ns}Key") or ""
                    name = key.split("/", 1)[1] if "/" in key else key
                    etag = (node.findtext(f"{ns}ETag") or "").strip('"')
                    md5 = (etag.lower()
                           if re.fullmatch(r"[0-9a-fA-F]{32}", etag) else None)
                    out[name] = {"size": int(node.findtext(f"{ns}Size") or 0),
                                 "md5": md5,
                                 "md5_origin": "s3-etag" if md5 else "none",
                                 "token": etag or None}
                trunc = (root.findtext(f"{ns}IsTruncated")
                         or "false").lower() == "true"
                token = root.findtext(f"{ns}NextContinuationToken")
                pages += 1
                if not trunc or not token or pages > 500:
                    break
        no_md5 = sum(1 for v in out.values() if not v["md5"])
        if no_md5:
            LOG.warn(f"aws: {no_md5}/{len(out)} objects only expose a multipart "
                     f"ETag, which is not an md5; they will be verified by size, "
                     f"the internal volume fingerprint and the final set check. "
                     f"Use --source gcp to get real md5Hash values.")
        LOG.verbose(f"aws: {len(out)} object(s) listed under "
                    f"{', '.join(prefixes)}")
        return out


class NcbiSource(Source):
    """The live, mutable https://ftp.ncbi.nlm.nih.gov/blast/db layout."""

    key = "ncbi"

    def __init__(self, ctx, ncbi_dir=NCBI_DEFAULT_DIR, base=None):
        super().__init__(ctx)
        self.dir = ncbi_dir.rstrip("/") or NCBI_DEFAULT_DIR
        self.base = (base or ctx.ncbi_base).rstrip("/") + self.dir
        self.url_mode = ctx.ncbi_url
        self._manifest = None
        self._manifest_headers = {}
        self._manifest_url = ""
        self._meta_cache = {}

    def resolve(self):
        self.manifest()

    def snapshot_id(self):
        return (self._manifest_headers.get("ETag")
                or self._manifest_headers.get("Last-Modified") or "live")

    def snapshot_label(self):
        return f"live{self.dir}"

    def invalidate(self):
        self._manifest = None
        self._meta_cache = {}

    def manifest(self, fresh=False):
        if self._manifest is None or fresh:
            url = f"{self.base}/{METADATA_JSON}"
            status, headers, body = self.http.call(url, ok=(200,), allow=())
            if status != 200:
                raise BlastError(f"cannot read {url}")
            obj = json.loads(body.decode("utf-8"))
            validate_manifest(obj, url)
            self._manifest = obj
            self._manifest_headers = headers
            self._manifest_url = url
        return self._manifest

    def list_databases(self):
        return sorted(({
            "dbname": e.get("dbname"),
            "dbtype": e.get("dbtype"),
            "description": e.get("description"),
            "last_updated": e.get("last-updated"),
            "bytes_total": e.get("bytes-total"),
            "bytes_compressed": e.get("bytes-total-compressed"),
            "number_of_volumes": e.get("number-of-volumes"),
            "number_of_sequences": e.get("number-of-sequences"),
            "number_of_letters": e.get("number-of-letters"),
        } for e in self.manifest()), key=lambda x: x["dbname"] or "")

    def _sidecar_md5(self, url, fresh=False):
        """Read `<url>.md5`.  `fresh=True` bypasses the per-run cache, which is
        what makes the post-transfer revision re-check meaningful."""
        if not fresh and url in self._meta_cache:
            return self._meta_cache[url]
        status, _h, body = self.http.call(url + ".md5", ok=(200,),
                                          allow=(403, 404))
        md5 = None
        if status == 200:
            parts = body.decode("ascii", "replace").split()
            if parts and re.fullmatch(r"[0-9a-fA-F]{32}", parts[0]):
                md5 = parts[0].lower()
        self._meta_cache[url] = md5
        return md5

    def payload_url(self, uri: str) -> str:
        """Where to actually fetch a file listed in the manifest.

        mode `mirror` (the default once --ncbi-base points somewhere else) keeps
        the file name but takes the host/directory from --ncbi-base, so that a
        replica whose manifest still names ftp.ncbi.nlm.nih.gov is used end to
        end.  mode `manifest` trusts the published URL (ftp:// -> https://),
        which is what the real NCBI tree does.
        """
        name = uri.rstrip("/").rsplit("/", 1)[-1]
        if self.url_mode == "mirror" and uri.startswith(("ftp://", "http://",
                                                          "https://")):
            return f"{self.base}/{name}"
        if uri.startswith("ftp://"):
            return "https://" + uri[len("ftp://"):]
        if uri.startswith(("http://", "https://")):
            return uri
        return f"{self.base}/{name}"

    def revision(self, dbname, fresh=False):
        entry = None
        for item in self.manifest(fresh=fresh):
            if item.get("dbname") == dbname:
                entry = item
                break
        if entry is None:
            raise BlastError(f"{dbname}: not present in {self.base}/{METADATA_JSON}")
        uris = entry.get("files", [])
        md5s = {}
        with futures.ThreadPoolExecutor(max_workers=min(8, max(1, self.ctx.jobs))) as pool:
            futs = {pool.submit(self._sidecar_md5, self.payload_url(uri), fresh): uri
                    for uri in uris}
            for fut in futures.as_completed(futs):
                uri = futs[fut]
                try:
                    md5s[uri] = fut.result()
                except Exception as exc:
                    LOG.debug(f"{dbname}: sidecar for {uri} failed: {exc}")
                    md5s[uri] = None
        archives = []
        for uri in uris:
            url = self.payload_url(uri)
            name = url.rsplit("/", 1)[-1]
            md5 = md5s.get(uri)
            if md5 is None:
                raise TornUpdateError(
                    f"{dbname}: the md5 sidecar for {name} is missing or "
                    f"malformed; the database is mid-publication. Nothing was "
                    f"installed.")
            archives.append(RemoteFile(name, url, None, md5,
                                       "ncbi-md5-sidecar", md5, role="archive"))
        files = list(archives)
        if self.ctx.add_metadata_json:
            mname = f"{dbname}-{meta_suffix(entry.get('dbtype'))}-metadata.json"
            murl = f"{self.base}/{mname}"
            present, length = self.http.head_size(murl)
            if present:
                # NCBI publishes no checksum for this file, but the Content-Length
                # still lets a truncated copy be rejected later
                files.append(RemoteFile(mname, murl, length, None, "self", None,
                                        role="metadata"))
        return DbTarget(dbname, entry.get("dbtype"), entry.get("description"),
                        entry.get("last-updated"), str(entry.get("version")),
                        self.key, self.snapshot_id(), self._manifest_url, files,
                        archives, entry.get("number-of-volumes"),
                        entry.get("bytes-total"),
                        entry.get("bytes-total-compressed"))


def open_source(ctx) -> Source:
    key = (ctx.source or "gcp").lower()
    if key == "auto":
        key = "ncbi"
    if key == "gcp":
        src = GcpSource(ctx)
    elif key == "aws":
        src = AwsSource(ctx)
    elif key == "ncbi":
        src = NcbiSource(ctx, ctx.ncbi_dir, ctx.ncbi_base)
    else:
        raise BlastError(f"unknown source {ctx.source!r}")
    src.resolve()
    return src


# --------------------------------------------------------------------------- #
# snapshot store
# --------------------------------------------------------------------------- #
class Snapshot:
    def __init__(self, root, name):
        self.root = root
        self.name = name
        self.dir = os.path.join(root, name)
        self.state_path = os.path.join(self.dir, STATE_BASENAME)
        self._state = None

    def exists(self):
        return os.path.isdir(self.dir)

    @property
    def state(self):
        if self._state is None:
            if not os.path.isfile(self.state_path):
                raise BlastError(f"{self.dir} has no {STATE_BASENAME}; it was not "
                                 f"produced by {PROGRAM}")
            self._state = read_json(self.state_path)
        return self._state

    def dbs(self):
        return sorted(self.state.get("dbs", {}).keys())

    def db_state(self, dbname):
        return self.state.get("dbs", {}).get(dbname)


class Store:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.staging = os.path.join(self.root, ".staging")

    def ensure_root(self):
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.staging, exist_ok=True)

    @property
    def current_link(self):
        return os.path.join(self.root, "current")

    def current_name(self):
        try:
            if os.path.islink(self.current_link):
                return os.readlink(self.current_link)
        except OSError:
            return None
        return None

    def current(self):
        name = self.current_name()
        return Snapshot(self.root, name) if name else None

    def snapshots(self):
        """Every snapshot that has a readable state file.

        A truncated or hand-edited state file must not make `list`, `gc` or
        `rollback` unusable: such a directory is reported and skipped.
        """
        out = []
        for name in os.listdir(self.root):
            if name.startswith(".") or name == "current":
                continue
            state_path = os.path.join(self.root, name, STATE_BASENAME)
            if not os.path.isfile(state_path):
                continue
            snap = Snapshot(self.root, name)
            try:
                snap._state = read_json(state_path)
            except Exception as exc:
                LOG.warn(f"ignoring snapshot {name}: {STATE_BASENAME} cannot be "
                         f"read ({exc})")
                continue
            out.append(snap)
        out.sort(key=lambda s: (s.state.get("created") or "", s.name))
        return out

    def by_tree_fingerprint(self, fingerprint):
        for snap in self.snapshots():
            if snap.state.get("tree_fingerprint") == fingerprint:
                return snap
        return None

    def swap_current(self, name):
        tmp = os.path.join(self.root, f".current.tmp{os.getpid()}")
        remove_quietly(tmp)
        try:
            os.symlink(name, tmp)
        except OSError as exc:
            raise BlastError(
                f"cannot create the `current` symlink in {self.root}: {exc}. "
                f"On Windows symlinks need Developer Mode or an elevated "
                f"shell; on POSIX check that the filesystem supports symlinks."
            ) from exc
        os.replace(tmp, self.current_link)
        fsync_dir(self.root)


    def staging_dir(self, dbname, revkey):
        path = os.path.join(self.staging, dbname, revkey)
        os.makedirs(path, exist_ok=True)
        return path

    def build_snapshot(self, name, tree, meta, content_hash, verify_existing=None):
        """tree: {file name: (src path, size, md5, owner dbname)}

        A snapshot is never silently destroyed: when the deterministic name is
        already taken by different content, the new tree is built under
        `<name>.<content hash>`.  When the name is taken by content that claims
        to be identical but does not verify any more, the broken directory is
        moved aside and replaced.
        """
        final = os.path.join(self.root, name)
        replace = False
        if os.path.isdir(final):
            try:
                existing_hash = read_json(os.path.join(final, STATE_BASENAME)
                                          ).get("content_hash")
            except Exception:
                existing_hash = None
            if existing_hash == content_hash:
                if verify_existing is None or verify_existing(Snapshot(self.root,
                                                                      name)):
                    LOG.info(f"snapshot {name} already holds exactly this "
                             f"content")
                    return Snapshot(self.root, name)
                LOG.warn(f"snapshot {name} claims this content but no longer "
                         f"verifies; rebuilding it")
                replace = True
            else:
                name = f"{name}.{content_hash[:8]}"
                final = os.path.join(self.root, name)
                LOG.info(f"previous content kept as is; building {name} instead")
                if os.path.isdir(final):
                    if verify_existing is not None and \
                            verify_existing(Snapshot(self.root, name)):
                        return Snapshot(self.root, name)
                    remove_quietly(final)
        tmp = os.path.join(self.root, f".{name}.tmp{os.getpid()}")
        remove_quietly(tmp)
        os.makedirs(tmp)
        linked = copied = 0
        for fname in sorted(tree):
            src, _size, _md5, _owner = tree[fname]
            dst = os.path.join(tmp, fname)
            if os.path.exists(dst):
                raise BlastError(f"duplicate file name {fname} while building "
                                 f"{name}")
            how = link_or_copy(src, dst)
            linked += how == "link"
            copied += how == "copy"
        state = dict(meta)
        state.update({"tool": PROGRAM, "tool_version": __version__,
                      "format": STATE_FORMAT, "content_hash": content_hash})
        atomic_write_json(os.path.join(tmp, STATE_BASENAME), state)
        fsync_dir(tmp)
        if replace:
            aside = os.path.join(self.root, f".broken.{name}.{os.getpid()}")
            remove_quietly(aside)
            os.rename(final, aside)
            os.rename(tmp, final)
            remove_quietly(aside)
        else:
            os.rename(tmp, final)
        fsync_dir(self.root)
        LOG.debug(f"snapshot {name}: {linked} hard-linked, {copied} copied")
        return Snapshot(self.root, name)


class RootLock:
    """Advisory exclusive lock for one mirror root.

    Two runs sharing a root would interleave their staging directories and race
    on `current`; only the mutating commands take the lock, so `list`, `verify`
    and `showall` stay usable while a download is running.
    """

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, ".lock")
        self.fh = None

    def __enter__(self):
        os.makedirs(self.root, exist_ok=True)
        try:
            import fcntl
        except ImportError:              # Windows
            LOG.debug("fcntl is unavailable; the mirror lock is disabled")
            return self
        self.fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                self.fh.seek(0)
                holder = self.fh.read().strip()
            except OSError:
                pass
            self.fh.close()
            self.fh = None
            raise BlastError(
                f"another {PROGRAM} run is already using this mirror"
                + (f" ({holder})" if holder else "")
                + f". Wait for it to finish, or stop it first. Lock: {self.path}"
            ) from None
        try:
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(f"pid {os.getpid()} since {utcnow()}\n")
            self.fh.flush()
        except OSError:
            pass
        return self

    def __exit__(self, *exc_info):
        if self.fh is None:
            return False
        try:
            import fcntl
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self.fh.close()
        self.fh = None
        return False


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def verify_table(dbname, table, quick=False, check_md5=True):
    """Verify a {name: (path, state)} table as a *set* of BLAST volumes.

    With `quick` the head/tail probes recorded at install time are re-checked
    instead of full md5 hashes.  Returns (issues, info).
    """
    issues = []
    markers = {}
    dates = {}
    for name in sorted(table):
        path, meta = table[name]
        meta = meta or {}
        if not belongs_to_database(name, dbname):
            issues.append(f"{name}: does not belong to the {dbname} database "
                          f"(the source served the wrong payload)")
            continue
        if not os.path.isfile(path):
            issues.append(f"{name}: missing")
            continue
        actual = os.path.getsize(path)
        expected = meta.get("size")
        if expected is not None and actual != expected:
            issues.append(f"{name}: size {actual} != recorded {expected}")
            continue
        if check_md5 and not quick and meta.get("md5"):
            got = md5_file(path)
            if got != meta["md5"]:
                issues.append(f"{name}: md5 {got} != recorded {meta['md5']}")
                continue
        elif meta.get("probe") and not probe_matches(path, meta["probe"]):
            issues.append(f"{name}: content probe mismatch (size {actual})")
            continue
        if is_volume_index(name):
            marker = read_marker(path)
            if marker is None:
                issues.append(f"{name}: cannot read the BLAST volume fingerprint")
            else:
                markers[name] = marker

    vols = {}
    for name, marker in markers.items():
        vol = volume_of(name)
        if vol is None:
            continue
        vols.setdefault(vol, []).append((name, marker))
        if marker.ordinal != vol:
            issues.append(f"{name}: embedded volume ordinal {marker.ordinal} does "
                          f"not match the number in the file name")
        norm = marker.build_date or marker.build_date_raw
        dates.setdefault(norm, []).append(name)
    if vols:
        ordinals = sorted(vols)
        if ordinals[0] != 0 or ordinals != list(range(ordinals[-1] + 1)):
            issues.append(f"volume set is not contiguous from 0: have "
                          f"{ordinals[0]}..{ordinals[-1]} ({len(ordinals)} files)")
    elif not any(is_alias_or_json(n) for n in table) and not all(
            is_shared_taxonomy(n) or n.endswith("-metadata.json") for n in table):
        issues.append("no volume index and no alias file is present - this is "
                      "not a complete BLAST database (an archive that was "
                      "fetched but never unpacked looks exactly like this)")
    if len(dates) > 1:
        detail = "; ".join(f"{k!r}: {len(v)} volume(s), e.g. {v[0]}"
                           for k, v in sorted(dates.items(), key=lambda x: str(x[0])))
        issues.append("MIXED BUILD TIMESTAMPS across volumes - this file set is "
                      f"torn ({detail})")
    elif dates and next(iter(dates)) is None:
        LOG.debug(f"{dbname}: volume build timestamps are unparsable")

    for name in table:
        if not name.endswith((".njs", ".pjs")):
            continue
        try:
            js = read_json(table[name][0])
        except Exception as exc:
            issues.append(f"{name}: unreadable JSON ({exc})")
            continue
        if js.get("dbname") and js["dbname"] != dbname:
            issues.append(f"{name}: declares dbname {js['dbname']!r}, expected "
                          f"{dbname!r}")
        nvol = js.get("number-of-volumes")
        if nvol and vols and int(nvol) != len(vols):
            issues.append(f"{name}: declares {nvol} volumes but {len(vols)} "
                          f"volume index files are present")
        stamp = normalise_build_date(js.get("last-updated"))
        if stamp and len(dates) == 1:
            only = next(iter(dates))
            if only and normalise_build_date(only) != stamp:
                issues.append(f"{name}: last-updated {stamp} disagrees with the "
                              f"volume fingerprints ({only})")

    issues.extend(check_database_metadata(dbname, table, dates, vols))

    info = {"volumes": len(vols), "build_dates": sorted(k for k in dates if k),
            "markers": markers}
    return issues, info


def check_database_metadata(dbname, table, dates, vols):
    """Cross-check `<db>-nucl-metadata.json` against the files on disk.

    The payload metadata NCBI ships next to a database describes the very same
    build: it lists the files it consists of, its volume count, its build
    timestamp and the exact total size of those files.  That makes it a
    self-contained completeness check - a missing, extra, truncated or
    differently-built payload file shows up here with no network access and
    without a single md5.  (For the tar.gz layout the same file lists the
    archive instead, so only the timestamp and volume checks apply.)
    """
    issues = []
    for name in sorted(table):
        if not name.endswith("-metadata.json"):
            continue
        try:
            js = read_json(table[name][0])
        except Exception as exc:
            issues.append(f"{name}: unreadable JSON ({exc})")
            continue
        if js.get("dbname") and js["dbname"] != dbname:
            issues.append(f"{name}: declares dbname {js['dbname']!r}, expected "
                          f"{dbname!r}")
        nvol = js.get("number-of-volumes")
        if nvol and vols and int(nvol) != len(vols):
            issues.append(f"{name}: declares {nvol} volume(s) but {len(vols)} "
                          f"volume index file(s) are present")
        stamp = normalise_build_date(js.get("last-updated"))
        if stamp and len(dates) == 1:
            only = next(iter(dates))
            ours = normalise_build_date(only) if only else None
            if ours and ours[:10] != stamp[:10]:
                issues.append(f"{name}: last-updated {stamp[:10]} disagrees with "
                              f"the volume fingerprints ({ours[:10]}) - the "
                              f"payload and its metadata come from different "
                              f"builds")
        flist = [str(f).rstrip("/").rsplit("/", 1)[-1]
                 for f in (js.get("files") or [])]
        if not flist or all(f.endswith(".tar.gz") for f in flist):
            continue                     # archive level metadata, nothing to add
        expected = set(flist)
        present = {n for n in table
                   if not n.endswith(("-metadata.json", ".tar.gz"))
                   and not is_shared_taxonomy(n)}
        missing = sorted(expected - present)
        extra = sorted(present - expected)
        if missing:
            issues.append(f"{name} lists {len(missing)} file(s) that are absent: "
                          + ", ".join(missing[:4])
                          + (" ..." if len(missing) > 4 else ""))
        if extra:
            issues.append(f"{name} does not list {len(extra)} file(s) that are "
                          f"present: " + ", ".join(extra[:4])
                          + (" ..." if len(extra) > 4 else ""))
        total = js.get("bytes-total")
        if total and not missing and not extra:
            have = sum((table[n][1] or {}).get("size") or 0
                       for n in expected if n in table)
            if have and have != int(total):
                issues.append(f"{name}: bytes-total {total} != the {have} bytes "
                              f"of payload on disk (truncated or replaced "
                              f"file?)")
    return issues


def table_from_state(snapshot, files, include_shared=False):
    """Build a verification table from a snapshot state entry."""
    table = {}
    for name, meta in (files or {}).items():
        table[name] = (os.path.join(snapshot.dir, name), meta)
    return table


def smoke_test(ctx, snapshot, dbnames):
    """Ask the locally installed BLAST whether it accepts the database.

    This is deliberately ADVISORY unless `--smoke-test always` is given: a
    blastdbcmd that cannot read NCBI's LMDB volume files (a well known
    client/build mismatch, which also happens with NCBI's own archives) says
    nothing about whether the download was correct - the md5 evidence and the
    embedded volume fingerprints are the authority here.
    """
    if ctx.smoke_test == "never":
        return []
    blastdbcmd = shutil.which("blastdbcmd")
    if not blastdbcmd:
        if ctx.smoke_test == "always":
            raise BlastError("--smoke-test always was requested but blastdbcmd "
                             "is not on PATH")
        return []
    fatal = ctx.smoke_test == "always"
    warnings = []
    for dbname in dbnames:
        st = snapshot.db_state(dbname) or {}
        names = st.get("files") or {}
        if not any(is_volume_index(n) or is_alias_or_json(n) for n in names):
            LOG.debug(f"{dbname}: no volume index or alias, skipping smoke test")
            continue
        try:
            proc = subprocess.run([blastdbcmd, "-db",
                                   os.path.join(snapshot.dir, dbname), "-info"],
                                  capture_output=True, text=True, timeout=180,
                                  check=False)
        except Exception as exc:
            warnings.append(f"{dbname}: blastdbcmd could not run ({exc})")
            continue
        if proc.returncode != 0:
            line = (proc.stderr or proc.stdout or "").strip().splitlines()
            warnings.append(f"{dbname}: blastdbcmd -info exited "
                            f"{proc.returncode}"
                            + (f": {line[0][:200]}" if line else ""))
        else:
            LOG.verbose(f"{dbname}: blastdbcmd accepts the database")
    for msg in warnings:
        LOG.warn(f"smoke test: {msg}")
    return warnings if fatal else []


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
class Context:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.root = os.path.abspath(cfg["root"])
        self.source = cfg["source"]
        self.ncbi_dir = cfg["ncbi_dir"]
        self.ncbi_base = (cfg["ncbi_base"] or NCBI_DEFAULT_BASE).rstrip("/")
        mode = cfg.get("ncbi_url") or "auto"
        if mode == "auto":
            mode = ("mirror" if self.ncbi_base != NCBI_DEFAULT_BASE
                    else "manifest")
        self.ncbi_url = mode
        # keep `config` output truthful about what will actually happen
        self.cfg["ncbi_url"] = mode
        self.cfg["ncbi_base"] = self.ncbi_base
        self.cfg["root"] = self.root
        self.jobs = max(1, int(cfg["jobs"]))
        self.connections = max(1, int(cfg["connections"]))
        self.min_split = int(cfg["min_split"])
        self.tries = max(1, int(cfg["tries"]))
        self.dry_run = bool(cfg["dry_run"])
        self.add_metadata_json = bool(cfg["add_metadata_json"])
        self.with_taxdb = bool(cfg["with_taxdb"])
        self.keep_snapshots = max(1, int(cfg["keep_snapshots"]))
        self.keep_archives = bool(cfg["keep_archives"])
        self.dedupe_taxonomy = bool(cfg["dedupe_taxonomy"])
        self.probe_sizes = bool(cfg["probe_sizes"])
        self.reuse_verify = cfg["reuse_verify"]
        self.min_free = int(cfg["min_free"] or 0)
        self.file_retries = max(1, int(cfg["file_retries"]))
        self.progress_interval = float(cfg["progress_interval"])
        self.log_file = cfg.get("log_file")
        self.auto_log = bool(cfg.get("auto_log", True))
        self.console = cfg.get("console") or "auto"
        self.user = cfg.get("user") or _current_user()
        self.adopt_dirs = [os.path.abspath(d) for d in (cfg.get("adopt") or [])]
        self.adopt_partial = bool(cfg.get("adopt_partial"))
        self.adopt_extracted = bool(cfg.get("adopt_extracted"))
        for directory in self.adopt_dirs:
            if not os.path.isdir(directory):
                raise BlastError(f"--adopt {directory}: not a directory")
        self.on_torn = cfg["on_torn"]
        self.torn_retries = int(cfg["torn_retries"])
        self.torn_wait = float(cfg["torn_wait"])
        self.smoke_test = cfg["smoke_test"]
        self.disk_check = bool(cfg["disk_check"])
        self.aria2c = cfg["aria2c"]
        self.aria2c_extra = list(cfg["aria2c_extra_args"] or [])
        self.limit_rate = cfg["limit_rate"]
        self.json_out = bool(cfg["json"])
        self.http = Http(timeout=cfg["timeout"], tries=self.tries,
                         insecure=bool(cfg["insecure"]))
        self.store = Store(self.root)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def resolve_requested(ctx, src, requested, auto_taxdb=True):
    available = {row["dbname"] for row in src.list_databases()}
    unknown = [d for d in requested if d not in available]
    if unknown:
        raise BlastError("unknown database(s): " + ", ".join(unknown)
                         + f" (use `{PROGRAM} showall` to list them)")
    wanted = list(requested)
    if auto_taxdb and ctx.with_taxdb and "taxdb" not in wanted \
            and "taxdb" in available:
        # Taxonomy is a first class database in every source.  The cloud
        # mirrors ship it as three separate objects; NCBI publishes
        # taxdb.tar.gz AND bundles the same payload inside some archives (the
        # single volume ones, measured).  Whether every multi volume archive
        # carries it is not documented, and a missing taxdb is silent - species
        # names just come back as "N/A" - so it is fetched unless asked not to.
        # It costs about 65 MiB compressed, i.e. 0.006% of an nt download, and
        # duplicate copies are resolved deterministically at install time.
        size = next((r.get("bytes_compressed") for r in src.list_databases()
                     if r["dbname"] == "taxdb"), None)
        LOG.info(f"also fetching taxdb"
                 + (f" ({human_bytes(size)})" if size else "")
                 + " so taxonomy lookups work; use --no-taxdb to skip it")
        wanted.append("taxdb")
    return wanted


def identity_provable(rf) -> bool:
    """Can this file's content be proven against an authority?

    A published md5 is a proof; an equal size is not (two builds of the same
    small metadata file can have the same length and different content).  Files
    without a checksum are therefore never *reused* from an older revision, and
    are only accepted when they were just fetched from the authoritative URL.
    """
    return bool(getattr(rf, "md5", None))


def reusable_file(ctx, rf, st, prev_dir):
    """Return the local path when an installed file may be re-used verbatim.

    The remote identity must match what is recorded (md5 when the source
    publishes one - NCBI sidecars and GCS md5Hash - otherwise the object token,
    which is a multipart ETag on S3) and the installed bytes must still look
    like what we installed (`--reuse-verify probe|md5|size`).
    """
    if not st:
        return None
    if rf.md5:
        if st.get("md5") != rf.md5:
            return None
    elif not (rf.token and st.get("token") == rf.token
              and st.get("size") is not None and st.get("size") == rf.size):
        return None
    path = os.path.join(prev_dir, rf.name)
    if not os.path.isfile(path):
        return None
    mode = ctx.reuse_verify
    if mode == "md5" and rf.md5:
        return path if md5_file(path) == rf.md5 else None
    recorded = probe_of(st)
    if mode in ("md5", "probe") and recorded:
        return path if probe_matches(path, recorded) else None
    size = st.get("size")
    if size is not None and os.path.getsize(path) != size:
        return None
    return path


def split_reuse(ctx, target, prev, allow_reuse):
    """Return (reuse table, files to fetch) for one database.

    A file is reusable when its authoritative md5 equals what is already
    installed and the installed copy still verifies.  Members of a reusable
    archive are reusable too, which is what makes incremental updates of nt/nr
    cheap.
    """
    reuse, pending = {}, []
    prev_files = {}
    if prev and allow_reuse:
        st = prev.db_state(target.dbname) or {}
        if st.get("source_key") == target.source_key:
            prev_files = st.get("files") or {}
    for rf in target.files:
        st = prev_files.get(rf.name)
        path = reusable_file(ctx, rf, st, prev.dir) if st is not None else None
        if path:
            reuse[rf.name] = (path, dict(st))
        else:
            pending.append(rf)
    # members of reused archives travel with them
    reused_archives = {rf.name for rf in target.archives if rf.name in reuse}
    reused_md5s = {rf.md5 for rf in target.archives
                   if rf.name in reuse and rf.md5}
    if reused_archives:
        for name, st in prev_files.items():
            if name in reuse:
                continue
            # match on the archive name: S3 objects may only expose a
            # multipart ETag, so md5 is not always available as a key
            if not (st.get("archive") in reused_archives
                    or (st.get("archive_md5") in reused_md5s)):
                continue
            path = os.path.join(prev.dir, name)
            if not os.path.isfile(path):
                continue
            recorded = probe_of(st)
            if (ctx.reuse_verify == "probe" and recorded
                    and not probe_matches(path, recorded)):
                LOG.warn(f"{target.dbname}: {name} no longer matches the "
                         f"recorded content; it will not be reused")
                continue
            reuse[name] = (path, dict(st))
    return reuse, pending


def ensure_probes(table):
    for _name, (path, st) in table.items():
        if st is not None and not st.get("probe"):
            try:
                st["probe"] = file_probe(path)
            except OSError:
                pass
    return table


_RETRYABLE_REASONS = ("timeout", "connection reset", "http 5xx", "http 403",
                     "host name resolution", "the downloader did not produce",
                     "checksum mismatch", "md5 mismatch", "size mismatch",
                     "unknown reason", "chunk(s) failed")


def failure_policy(reason: str) -> str:
    """Decide what to do about one failure reason.

    `retry`  - another attempt may well succeed (timeouts, resets, rate limits)
    `fatal`  - retrying cannot help (no space, permissions, a missing object)
    """
    text = (reason or "").lower()
    if any(k in text for k in ("no space left", "quota exceeded",
                               "permission denied", "http 404",
                               "too many open files", "not a directory")):
        return "fatal"
    if any(k in text for k in _RETRYABLE_REASONS):
        return "retry"
    return "retry"


def download_files(ctx, plans, label, staging=None):
    if not plans:
        return {}
    if ctx.probe_sizes:
        probe_sizes(ctx, plans)
    total = sum(p.size or 0 for p in plans)
    aria = find_aria2c(ctx.aria2c)
    progress = Progress(total, label, ctx.progress_interval,
                        planned=len(plans)).start()
    if aria:
        LOG.info(f"{label}: fetching {len(plans)} file(s) "
                 f"({human_bytes(total) if total else 'size unknown'}) with "
                 f"aria2c, {ctx.jobs} parallel")
    else:
        LOG.info(f"{label}: fetching {len(plans)} file(s) "
                 f"({human_bytes(total) if total else 'size unknown'}) with the "
                 f"built-in downloader, {ctx.jobs} parallel")
    try:
        return _download_rounds(ctx, plans, label, total, aria, progress,
                                staging)
    finally:
        progress.stop()


def _download_rounds(ctx, plans, label, total, aria, progress, staging):
    remaining = list(plans)
    results = {}
    attempt = 0
    jobs, connections = ctx.jobs, ctx.connections
    while True:
        attempt += 1
        monitor = TransferMonitor(ctx.root, ctx.min_free,
                                  progress=progress if aria else None,
                                  staging=staging,
                                  user=ctx.user,
                                  report_interval=ctx.progress_interval).start()
        if aria:
            backend = Aria2Backend(aria, ctx.tries, ctx.aria2c_extra,
                                   show_progress=sys.stderr.isatty()
                                   and LOG.level < 2)
        else:
            backend = BuiltinBackend(ctx.http, progress, ctx.tries,
                                     stop_event=monitor.abort,
                                     limit_rate=ctx.limit_rate)
        try:
            if isinstance(backend, Aria2Backend):
                round_results = backend.run(remaining, jobs, connections,
                                            ctx.min_split, ctx.limit_rate,
                                            watchdog=monitor, progress=progress)
            else:
                round_results = backend.run(remaining, jobs, connections,
                                            ctx.min_split, ctx.limit_rate)
        finally:
            monitor.stop()
        results.update(round_results)
        bad = [p for p in remaining if not round_results.get(p.key(), {}).get("ok")]
        if not bad:
            break
        reasons = {}
        for plan in bad:
            info = round_results.get(plan.key()) or {}
            reasons[plan.key()] = normalise_download_error(
                info.get("from_log") or info.get("error"))
        retryable = [p for p in bad if failure_policy(reasons[p.key()]) == "retry"]
        monitor.raise_if_tripped()
        if not retryable:
            report_failures(bad, round_results, ctx, len(plans), label)
            raise BlastError(f"{len(bad)}/{len(plans)} file(s) of {label} failed "
                             f"for a reason that retrying cannot fix")
        if attempt >= ctx.file_retries:
            report_failures(bad, round_results, ctx, len(plans), label)
            raise BlastError(f"{len(bad)}/{len(plans)} file(s) of {label} still "
                             f"failed after {attempt} attempt(s)")
        limited = any("403" in reasons[p.key()] or "reset" in reasons[p.key()]
                      for p in retryable)
        if limited:
            jobs = max(1, jobs // 2)
            connections = max(1, connections // 2)
        wait = backoff(attempt, cap=60.0)
        LOG.warn(f"{label}: {len(retryable)} file(s) failed on attempt "
                 f"{attempt}/{ctx.file_retries}, retrying in {wait:.0f}s"
                 + (f" with reduced parallelism (-j {jobs} -x {connections})"
                    if limited else "")
                 + "; reasons: "
                 + ", ".join(sorted({reasons[p.key()] for p in retryable}))[:200])
        remaining = retryable
        time.sleep(wait)
    return results


def probe_sizes(ctx, plans):
    missing = [p for p in plans if p.size is None]
    if not missing:
        return

    def one(plan):
        try:
            status, headers, _b = ctx.http.call(plan.url, method="HEAD",
                                                ok=(200,), allow=(403, 404))
            if status == 200 and headers.get("Content-Length"):
                plan.size = int(headers["Content-Length"])
        except Exception as exc:
            LOG.debug(f"HEAD {plan.url}: {exc}")

    with futures.ThreadPoolExecutor(max_workers=min(8, ctx.jobs)) as pool:
        list(pool.map(one, missing))


_TAR_NAME_RE = re.compile(r"^[A-Za-z0-9._+\-]+$")


def extract_archive(path, dest_dir, known, dedupe=True, force=()):
    """Stream-extract an NCBI archive.  Returns {member: (size, md5)}.

    Members whose (name, size) is already known are skipped when `dedupe` is
    set: NCBI bundles the same taxonomy payload into many archives.
    """
    out = {}
    with tarfile.open(path, "r:*") as tf:
        for member in tf:
            if not member.isfile():
                continue
            base = os.path.basename(member.name)
            if not _TAR_NAME_RE.match(base):
                raise BlastError(f"{path}: refusing unsafe member name "
                                 f"{member.name!r}")
            if dedupe and base not in force and known.get(base) == member.size:
                LOG.debug(f"{path}: skipping {base} (identical size already present)")
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            tmp = os.path.join(dest_dir, base + ".part")
            digest = hashlib.md5()
            size = 0
            with open(tmp, "wb") as fh:
                while True:
                    block = src.read(1 << 22)
                    if not block:
                        break
                    digest.update(block)
                    fh.write(block)
                    size += len(block)
                fh.flush()
                os.fsync(fh.fileno())
            if size != member.size:
                remove_quietly(tmp)
                raise BlastError(f"{path}: member {base} is truncated "
                                 f"({size} != {member.size})")
            os.replace(tmp, os.path.join(dest_dir, base))
            out[base] = (size, digest.hexdigest())
            known[base] = size
    return out


def extraction_receipt_path(stage: str) -> str:
    return os.path.join(stage, "extracted", ".receipt.json")


def load_extraction_receipt(stage: str) -> dict:
    try:
        return read_json(extraction_receipt_path(stage)) or {}
    except Exception:
        return {}


def save_extraction_receipt(stage: str, receipt: dict) -> None:
    try:
        atomic_write_json(extraction_receipt_path(stage), receipt)
    except OSError as exc:
        LOG.debug(f"cannot write the extraction receipt in {stage}: {exc}")


def remembered_members(ctx, target, stage, rf, receipt):
    """Members of an archive we already unpacked in an earlier run.

    Archives are deleted as soon as they are unpacked (otherwise a 1 TiB job
    needs 2 TiB of peak space), so without this a re-run after a failure would
    fetch every archive again.  The receipt lives inside the revision scoped
    staging directory, so its members can only belong to this revision, and
    they are still verified against the sizes and md5s recorded at extraction.
    """
    entry = (receipt or {}).get(rf.name)
    if not entry or (rf.md5 and entry.get("archive_md5") != rf.md5):
        return None
    extract_dir = os.path.join(stage, "extracted")
    members = {}
    for name, meta in (entry.get("members") or {}).items():
        path = os.path.join(extract_dir, name)
        ok, _why = verify_local_file(path, meta.get("size"), meta.get("md5"))
        if not ok:
            LOG.verbose(f"{target.dbname}: {name} from an earlier extraction "
                        f"no longer verifies; {rf.name} will be fetched again")
            return None
        members[name] = (path, {"size": meta.get("size"),
                                "md5": meta.get("md5"),
                                "md5_origin": "archive-member",
                                "role": "payload",
                                "archive": rf.name,
                                "archive_md5": rf.md5,
                                "probe": file_probe(path)})
    return members or None


def salvage_other_revisions(ctx, target, stage, pending):
    """Reuse files another revision's staging directory already has.

    A release that is republished (or a switch between sources) produces a new
    revkey, so the previous staging tree is not the one this run writes into -
    but most volumes are unchanged between releases and are byte identical.
    Salvage those, then drop the rest of the old tree, which is exactly the
    "delete what does not belong, retry what is left" behaviour an operator
    wants after a torn or failed batch.
    """
    base = os.path.join(ctx.store.staging, target.dbname)
    mine = os.path.basename(stage)
    if not os.path.isdir(base):
        return pending, {}
    others = [d for d in sorted(os.listdir(base))
              if d != mine and os.path.isdir(os.path.join(base, d))]
    if not others or not pending:
        return pending, {}
    salvaged, still, per_rev = set(), [], {}
    for rf in pending:
        if not identity_provable(rf):
            LOG.debug(f"{target.dbname}: {rf.name} has no published checksum, "
                      f"so it is not salvaged from an older revision")
            still.append(rf)
            continue
        for rev in others:
            src = os.path.join(base, rev, rf.name)
            ok, _why = verify_local_file(src, rf.size, rf.md5)
            if not ok:
                continue
            dst = os.path.join(stage, rf.name)
            remove_quietly(dst)
            link_or_copy(src, dst)
            salvaged.add(rf.name)
            per_rev.setdefault(rev, []).append(rf.name)
            break
        else:
            still.append(rf)
    for rev, names in per_rev.items():
        LOG.info(f"{target.dbname}: salvaged {len(names)} file(s) from the "
                 f"staged revision {rev}")
    for rev in others:
        kept = len(per_rev.get(rev, []))
        remove_quietly(os.path.join(base, rev))
        LOG.verbose(f"{target.dbname}: discarded the rest of staged revision "
                    f"{rev} ({kept} file(s) kept)")
    return still, {name: True for name in salvaged}


def adopt_complete_and_partial(ctx, target, stage, table, pending):
    """Adopt files an operator already downloaded with another tool.

    Their existing `aria2c` loop leaves `<db>.<vol>.tar.gz` plus `.md5` and, for
    the volume that was in flight, an `.aria2` control file.  Everything that
    verifies against the authoritative md5 is hard-linked into staging, and a
    half finished file is brought over *together with the information needed to
    resume it* instead of being downloaded from scratch.
    """
    if not ctx.adopt_dirs:
        return pending, {}
    # The decision "complete file" vs "file to resume" needs the remote size.
    # Without it a complete-but-unverifiable file looks like a resumable partial
    # and the engine would resume at its end instead of fetching it properly.
    unknown = [rf for rf in pending if rf.size is None
               and any(os.path.isfile(os.path.join(d, rf.name))
                       for d in ctx.adopt_dirs)]
    if unknown:
        probes = [FilePlan(rf.name, rf.url, os.path.join(stage, rf.name), None,
                           rf.md5, rf.md5_origin, rf.token) for rf in unknown]
        probe_sizes(ctx, probes)
        for rf, probe in zip(unknown, probes):
            rf.size = probe.size
    report = {}
    still = []
    aria = find_aria2c(ctx.aria2c)

    def note(directory, kind, name):
        report.setdefault(directory, {"complete": [], "partial": []})[kind].append(name)

    for rf in pending:
        dst = os.path.join(stage, rf.name)
        handled = False
        for directory in ctx.adopt_dirs:
            src = os.path.join(directory, rf.name)
            if not os.path.isfile(src):
                continue
            if not identity_provable(rf):
                LOG.verbose(f"{rf.name}: no published checksum, so the copy in "
                            f"{directory} cannot be proven to be this revision; "
                            f"fetching it instead")
                continue
            ok, _why = verify_local_file(src, rf.size, rf.md5)
            if ok:
                remove_quietly(dst)
                link_or_copy(src, dst)
                st = rf.state()
                st["size"] = os.path.getsize(dst)
                if not st["md5"]:
                    st["md5"] = md5_file(dst)
                    st["md5_origin"] = "self"
                st["probe"] = file_probe(dst)
                st["adopted_from"] = src
                table[rf.name] = (dst, st)
                note(directory, "complete", rf.name)
                handled = True
                break
            if not ctx.adopt_partial:
                continue
            if rf.size is not None and os.path.getsize(src) >= rf.size:
                continue                 # complete but does not verify: unusable
            ctl = src + ".aria2"
            if os.path.isfile(ctl):
                if aria is None:
                    LOG.warn(f"{rf.name}: {src} is a split download in progress "
                             f"and aria2c is not available; install aria2c to "
                             f"resume it safely, otherwise it is downloaded again")
                    continue
                remove_quietly(dst)
                remove_quietly(dst + ".aria2")
                link_or_copy(src, dst)
                link_or_copy(ctl, dst + ".aria2")
                LOG.info(f"{target.dbname}: resuming {rf.name} from {src} "
                         f"(aria2 control file found)")
            elif is_contiguous_prefix(src, rf.size):
                remove_quietly(dst)
                # hard link, not a copy: the engine continues writing at the end
                # of this prefix, so the operator's file ends up completed too
                # (and a sparse aria2 file would cost real space if copied)
                link_or_copy(src, dst)
                LOG.info(f"{target.dbname}: resuming {rf.name} from {src} "
                         f"({human_bytes(os.path.getsize(src))} already present; "
                         f"the adopted file is completed in place)")
            else:
                LOG.verbose(f"{rf.name}: {src} is incomplete and cannot be "
                            f"resumed safely (holes or no control file); "
                            f"ignoring it")
                continue
            note(directory, "partial", rf.name)
            still.append(rf)
            handled = True
            break
        if not handled:
            still.append(rf)
    return still, report


def check_payload_uniformity(dbname, cand):
    """Every volume of a database carries the same kinds of file."""
    per_volume = {}
    for name in cand:
        vol = volume_of(name)
        if vol is None:
            continue
        per_volume.setdefault(vol, set()).add(name.rsplit(".", 1)[-1])
    if len(per_volume) < 2:
        return []
    vols = sorted(per_volume)
    reference = per_volume[vols[-1]]
    problems = []
    for vol in vols[:-1]:
        missing = reference - per_volume[vol]
        if missing:
            problems.append(f"volume {vol} is missing "
                            f"{', '.join(sorted(missing))} which the other "
                            f"volumes have")
    return problems


def adopt_extracted_payload(ctx, target, table, pending):
    """Adopt a *complete* extracted payload set (tar.gz layout only).

    Members of an NCBI archive have no published md5, so a payload that is only
    partly present cannot be proven byte for byte.  A complete set can still be
    validated structurally: one build date across all volumes, that date equal
    to the one the source is publishing, the volume count matching, every blob
    referenced by a volume index present, and the same file kinds in every
    volume.  That is strong evidence rather than proof, so it happens only when
    the operator asks for it with --adopt-extracted.
    """
    if not ctx.adopt_extracted or not target.archives:
        return pending, None
    cand = {}
    for directory in ctx.adopt_dirs:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or name.endswith((".aria2", ".md5")):
                continue
            if name.endswith(".tar.gz"):
                continue
            if not belongs_to_database(name, target.dbname):
                continue
            if is_shared_taxonomy(name) and target.dbname != "taxdb":
                continue
            path = os.path.join(directory, name)
            if os.path.isfile(path) and name not in cand:
                cand[name] = path
    if not cand:
        return pending, None

    local = {n: (p, {"size": os.path.getsize(p)}) for n, p in cand.items()}
    issues, info = verify_table(target.dbname, local)
    dates = info.get("build_dates") or []
    markers = info.get("markers") or {}
    rev_date = (normalise_build_date(target.last_updated) or "")[:10]
    problems = list(issues)
    if len(dates) != 1:
        problems.append(f"the local files do not share one build date: {dates}")
    elif rev_date and dates[0][:10] != rev_date:
        problems.append(f"the local files were built on {dates[0][:10]} while "
                        f"the source is publishing {rev_date}")
    expected_vols = target.number_of_volumes
    if expected_vols and len(markers) != expected_vols:
        problems.append(f"the local files cover {len(markers)} volume(s) but "
                        f"the source has {expected_vols}")
    for name in markers:
        try:
            with open(cand[name], "rb") as fh:
                blob = marker_blob(fh.read(512))
        except OSError:
            blob = None
        if blob and blob not in cand:
            problems.append(f"{name} refers to {blob}, which is not in the "
                            f"adopted directory")
    problems.extend(check_payload_uniformity(target.dbname, cand))

    if problems:
        LOG.warn(f"{target.dbname}: not adopting the extracted payload from "
                 f"{', '.join(ctx.adopt_dirs)}")
        for msg in problems[:8]:
            LOG.warn(f"    {msg}")
        if len(problems) > 8:
            LOG.warn(f"    ... and {len(problems) - 8} more")
        if len(markers) and expected_vols and len(markers) < expected_vols:
            LOG.warn(f"    {expected_vols - len(markers)} volume(s) are missing "
                     f"locally; their archives are deleted, so they have to be "
                     f"downloaded again (use -s gcp for per file verification)")
        return pending, None

    for name, path in cand.items():
        table[name] = (path, {"size": os.path.getsize(path),
                              "md5": md5_file(path),
                              "md5_origin": "self",
                              "role": "payload",
                              "adopted_from": path,
                              "probe": file_probe(path)})
    LOG.info(f"{target.dbname}: adopted {len(cand)} already extracted file(s) "
             f"from {', '.join(ctx.adopt_dirs)} after checking that they form "
             f"one complete build ({dates[0][:10]})")
    return [], {"extracted": len(cand), "build_date": dates[0]}


def fetch_target(ctx, src, target, prev, allow_reuse):
    """Download, verify and assemble one database into a {name: (path, state)}
    table.  The set-level fingerprint check runs before anything is installed.
    """
    reuse, pending = split_reuse(ctx, target, prev, allow_reuse)
    table = {}
    for name, (path, st) in reuse.items():
        table[name] = (path, st)

    # ---- layer 2 of resume: files an earlier run already fetched ---------- #
    stage = ctx.store.staging_dir(target.dbname, target.revkey())
    receipt = load_extraction_receipt(stage)
    recovered = set()
    for rf in target.archives:
        members = remembered_members(ctx, target, stage, rf, receipt)
        if members:
            LOG.info(f"{target.dbname}: {rf.name} was already unpacked by an "
                     f"earlier run; reusing its {len(members)} member(s)")
            table.update(members)
            recovered.add(rf.name)
    if recovered:
        pending = [rf for rf in pending if rf.name not in recovered]

    # ---- adopt what the operator already downloaded with another tool ---- #
    pending, adopted = adopt_complete_and_partial(ctx, target, stage, table,
                                                  pending)
    for directory, kinds in sorted(adopted.items()):
        bits = []
        if kinds["complete"]:
            bits.append(f"{len(kinds['complete'])} complete")
        if kinds["partial"]:
            bits.append(f"{len(kinds['partial'])} to resume")
        if bits:
            LOG.info(f"{target.dbname}: adopted {' and '.join(bits)} file(s) "
                     f"from {directory}")
    pending, extracted_payload = adopt_extracted_payload(ctx, target, table,
                                                         pending)
    if extracted_payload:
        LOG.info(f"{target.dbname}: {extracted_payload['extracted']} file(s) of "
                 f"build {extracted_payload.get('build_date')} come from the "
                 f"adopted payload; no archive is fetched for them")

    # ---- layer 3 of resume: salvage from other (stale) staged revisions --- #
    pending, salvaged = salvage_other_revisions(ctx, target, stage, pending)
    if salvaged:
        for rf in target.files:
            if rf.name in salvaged:
                path = os.path.join(stage, rf.name)
                st = rf.state()
                st["size"] = os.path.getsize(path)
                if not st["md5"]:
                    st["md5"] = md5_file(path)
                    st["md5_origin"] = "self"
                st["probe"] = file_probe(path)
                table[rf.name] = (path, st)

    if pending:
        still = []
        for rf in pending:
            path = os.path.join(stage, rf.name)
            if not identity_provable(rf):
                # cannot be proven; it is tiny, so just fetch it again
                still.append(rf)
                continue
            ok, _why = verify_local_file(path, rf.size, rf.md5)
            if not ok:
                still.append(rf)
                continue
            st = rf.state()
            st["size"] = os.path.getsize(path)
            if not st["md5"]:
                st["md5"] = md5_file(path)
                st["md5_origin"] = "self"
            st["probe"] = file_probe(path)
            table[rf.name] = (path, st)
            LOG.info(f"{target.dbname}: {rf.name} was already fetched by an "
                     f"earlier run and still verifies; not downloading it again")
        pending = still

    if pending:
        plans = [FilePlan(rf.name, rf.url, os.path.join(stage, rf.name), rf.size,
                          rf.md5, rf.md5_origin, rf.token) for rf in pending]
        download_files(ctx, plans, target.dbname, staging=stage)
        for rf in pending:
            path = os.path.join(stage, rf.name)
            st = rf.state()
            st["size"] = os.path.getsize(path)
            if not st["md5"]:
                st["md5"] = md5_file(path)
                st["md5_origin"] = "self"
            st["probe"] = file_probe(path)
            table[rf.name] = (path, st)

    # ---- extract every archive whose members are not in the table yet ----- #
    def members_present(rf):
        return any((st or {}).get("archive") == rf.name
                   or ((st or {}).get("archive_md5")
                       and (st or {}).get("archive_md5") == rf.md5)
                   for _p, st in table.values())

    to_extract = [rf for rf in target.archives
                  if rf.name in table and not members_present(rf)]
    if to_extract:
        extract_dir = os.path.join(stage, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        known = {name: (meta or {}).get("size") for name, (_p, meta) in table.items()}
        # a file that was adopted from elsewhere is re-extracted from the
        # archive when we have one: the archive is the authority, not the copy
        adopted_names = {n for n, (_p, st) in table.items()
                         if (st or {}).get("adopted_from")}
        for rf in to_extract:
            archive_path = table[rf.name][0]
            LOG.verbose(f"extracting {rf.name}")
            members = extract_archive(archive_path, extract_dir, known,
                                      ctx.dedupe_taxonomy, force=adopted_names)
            for name, (size, md5) in members.items():
                member_path = os.path.join(extract_dir, name)
                table[name] = (member_path,
                               {"size": size, "md5": md5,
                                "md5_origin": "archive-member",
                                "role": "payload",
                                "archive": rf.name,
                                "archive_md5": rf.md5,
                                "probe": file_probe(member_path)})
            known.update({n: v[0] for n, v in members.items()})
            receipt[rf.name] = {
                "archive_md5": rf.md5,
                "members": {n: {"size": v[0], "md5": v[1]}
                            for n, v in members.items()},
            }
            save_extraction_receipt(stage, receipt)
            if not ctx.keep_archives:
                # release the archive immediately: for nt that is 2.7 GB of
                # staging per volume, i.e. a whole terabyte of peak usage
                remove_quietly(archive_path)

    if target.archives and not ctx.keep_archives:
        for rf in target.archives:
            table.pop(rf.name, None)

    ensure_probes(table)
    issues, info = verify_table(target.dbname, table)
    if issues:
        raise VerificationError(
            f"{target.dbname}: refusing to install, the downloaded set does not "
            f"verify:\n    " + "\n    ".join(issues[:20]))
    for name in list(table):
        if not os.path.isfile(table[name][0]):
            raise BlastError(f"{target.dbname}: {name} vanished while assembling")
    LOG.verbose(f"{target.dbname}: {len(table)} files, "
                f"{info['volumes']} volume(s), build "
                f"{info['build_dates'] or 'unknown'}")
    return table, info


def merge_tables(tables, db_dates=None):
    """Combine per-database tables into one tree, resolving name conflicts.

    A BLASTDB directory is flat, but two databases can ship the same file:
    the taxonomy payload (`taxdb.btd`, `taxdb.bti`, `taxonomy4blast.sqlite3`)
    is bundled inside many NCBI archives, at whatever build date that archive
    has.  NCBI's own client extracts them in arbitrary order, so which copy a
    mirror ends up with is effectively random - and an old taxonomy file can
    silently lack the taxids a newer database references.

    Here the winner is chosen deterministically: the standalone `taxdb`
    database when it is part of the tree, otherwise the database with the
    newest build date.  Every discarded copy is reported.  A conflict on any
    other file name is fatal, because that really is an inconsistent tree.
    """
    db_dates = db_dates or {}
    order = sorted(tables,
                   key=lambda d: (d != "taxdb", _date_key(db_dates.get(d)), d))
    tree, owners, shared = {}, {}, {}
    for dbname in order:
        for name, (path, st) in tables[dbname].items():
            if name not in tree:
                tree[name] = (path, st.get("size"), st.get("md5"), dbname)
                owners[name] = dbname
                continue
            other = tree[name]
            same = (other[1] == st.get("size")
                    and (not other[2] or not st.get("md5") or other[2] == st["md5"]))
            if same:
                shared.setdefault(dbname, {})[name] = owners[name]
                continue
            if is_shared_taxonomy(name):
                LOG.warn(f"{dbname} bundles a different {name} "
                         f"({st.get('md5')}) than {owners[name]}; keeping the "
                         f"copy from {owners[name]}")
                shared.setdefault(dbname, {})[name] = owners[name]
                continue
            raise VerificationError(
                f"{dbname} and {owners[name]} provide different versions of "
                f"{name} ({other[2]} vs {st.get('md5')}). Refusing to build a "
                f"tree with an inconsistent shared payload.")
    return tree, owners, shared


def _date_key(value):
    """Sortable key that puts 'no date known' last."""
    if not value:
        return (1, "")
    return (0, -_date_sort(value))


def _date_sort(value):
    try:
        return dt.datetime.strptime(str(value)[:16], "%Y-%m-%dT%H:%M").timestamp()
    except ValueError:
        digits = re.sub(r"[^0-9]", "", str(value))
        return int(digits[:14]) if digits else 0


def tree_fingerprint(revkeys):
    return sha1_hex("\n".join(f"{db}:{rev}" for db, rev in sorted(revkeys.items())))[:16]


def disk_check(ctx, needed):
    if not ctx.disk_check or not needed:
        return
    free, quota = effective_free(ctx.root, ctx.user)
    if free is None:
        LOG.warn("cannot determine free disk space")
        return
    required = int(needed * 1.05) + (1 << 30)
    where = f"usable space ({quota.get('tool')})" if quota and \
        quota.get("remaining") is not None and quota["remaining"] <= free \
        else "free space"
    LOG.verbose(f"disk: about {human_bytes(needed)} will be added "
                f"(+5% and 1 GiB reserve), {human_bytes(free)} {where}")
    if required > free:
        extra = ""
        if quota and quota.get("limit"):
            extra = (f"\n  the user quota on {quota.get('filesystem')} is "
                     f"{human_bytes(quota['limit'])}, of which "
                     f"{human_bytes(quota['used'])} is already used")
        raise BlastError(
            f"not enough {where} under {ctx.root}: need about "
            f"{human_bytes(required)}, have {human_bytes(free)}.{extra}\n"
            f"  hints: free space or raise the quota; `-s gcp` needs only the "
            f"payload (no archive extract step); --min-free lowers the "
            f"mid-transfer floor; --no-disk-check overrides this check")


# --------------------------------------------------------------------------- #
# command: download
# --------------------------------------------------------------------------- #
def belongs_to_database(name: str, dbname: str) -> bool:
    """Every payload file of a BLAST database is named after it.

    `<db>.nin`, `<db>.00.nsq`, `<db>-nucl-metadata.json`, `<db>.nal` ...  The
    only exception is the taxonomy payload, which NCBI bundles into many
    archives and which is shared by the whole mirror.  Checking this catches a
    truncated manifest, a wrong object served by a CDN, or an archive that does
    not actually contain the database that was requested.
    """
    if is_shared_taxonomy(name):
        return True
    return name.startswith((dbname + ".", dbname + "-"))


def tree_name_for(src, desired):
    """Predict the snapshot directory name (only used by --dry-run)."""
    fingerprint = tree_fingerprint(desired)
    if src.key in ("gcp", "aws"):
        return f"{src.key}-{src.snapshot_id()}-{fingerprint[:8]}"
    return f"{src.key}-<build date>-{fingerprint[:8]}"


def compute_work(src, targets, prev, force):
    """Databases that actually need to be fetched (the rest is carried over)."""
    work = []
    for target in targets:
        st = (prev.db_state(target.dbname) if prev else None) or {}
        if (not force and st.get("revkey") == target.revkey()
                and st.get("source_key") == src.key):
            LOG.verbose(f"{target.dbname}: already installed at revision "
                        f"{target.revkey()}")
            continue
        work.append(target)
    return work


def cmd_download(ctx, args, auto_taxdb=True, allow_reuse=None):
    store = ctx.store
    store.ensure_root()
    sweep_leftovers(ctx)
    # a bad --aria2c path is a usage error: report it before touching the network
    find_aria2c(ctx.aria2c)
    if os.path.isdir(store.current_link) and not os.path.islink(store.current_link):
        if not args.takeover:
            raise BlastError(
                f"{store.current_link} is a real directory but this tool needs it "
                f"to be a symlink. Move it aside or pass --takeover.")
        aside = os.path.join(ctx.root, f".legacy-current-{int(time.time())}")
        LOG.warn(f"renaming {store.current_link} -> {aside}")
        os.rename(store.current_link, aside)

    src = open_source(ctx)
    LOG.info(f"source: {src.describe()}")
    requested = resolve_requested(ctx, src, args.databases,
                                  auto_taxdb=auto_taxdb)
    targets = [src.revision(db) for db in requested]
    for target in targets:
        target.keep_archives = ctx.keep_archives
    if allow_reuse is None:
        allow_reuse = not args.force
    prev = store.current()
    if prev and not prev.exists():
        LOG.warn(f"current -> {prev.name} does not exist; ignoring it")
        prev = None

    # ---- what does the new tree look like? -------------------------------- #
    desired = {t.dbname: t.revkey() for t in targets}
    if prev:
        for dbname in prev.dbs():
            desired.setdefault(dbname, (prev.db_state(dbname) or {}).get("revkey"))
    want_fp = tree_fingerprint(desired)
    existing = store.by_tree_fingerprint(want_fp)
    if existing and not args.force:
        if store.current_name() != existing.name:
            store.swap_current(existing.name)
            LOG.info(f"snapshot {existing.name} already matches the requested "
                     f"state; current -> {existing.name}")
        else:
            LOG.info(f"all requested databases are already installed at the "
                     f"requested revision ({existing.name})")
        return EXIT_OK

    work = compute_work(src, targets, prev, args.force)
    if not work:
        LOG.info("all requested databases are already installed at the requested "
                 "revision")
        return EXIT_OK
    needed = sum(t.expected_disk_bytes() or 0 for t in work)

    if ctx.dry_run:
        print(f"DRY-RUN: snapshot {tree_name_for(src, desired)} would contain "
              f"{len(desired)} database(s); {len(work)} need fetching:")
        for target in work:
            _reuse, pending = split_reuse(ctx, target, prev, allow_reuse)
            need = target.expected_disk_bytes()
            print(f"  {target.dbname}: rev {target.revkey()}, "
                  f"{len(target.files)} file(s), {len(pending)} to download, "
                  f"about {human_bytes(need)} of disk"
                  + (" (incl. one archive while unpacking)"
                     if target.archives and not ctx.keep_archives else ""))
            for rf in pending[:10]:
                print(f"      {rf.url}")
            if len(pending) > 10:
                print(f"      ... and {len(pending) - 10} more")
        free = free_space(ctx.root)
        if free is not None:
            print(f"  free space on {ctx.root}: {human_bytes(free)}")
        return EXIT_OK

    disk_check(ctx, needed)

    # ---- download with torn-update detection ------------------------------ #
    attempt = 0
    tables = {}
    while True:
        attempt += 1
        try:
            tables = {}
            for target in work:
                tables[target.dbname], _info = fetch_target(ctx, src, target,
                                                            prev, allow_reuse)
            stale = []
            for target in work:
                again = src.revision(target.dbname, fresh=True)
                if again.revkey() != target.revkey():
                    stale.append(f"{target.dbname} {target.revkey()} -> "
                                 f"{again.revkey()}")
            if stale:
                raise TornUpdateError("the source published a new revision while "
                                      "downloading: " + ", ".join(stale))
            break
        except TornUpdateError as exc:
            if ctx.on_torn == "fail" or attempt > ctx.torn_retries:
                raise TornUpdateError(
                    f"{exc}. Aborted after {attempt} attempt(s); nothing was "
                    f"installed. Use --on-torn retry (the default) to wait for "
                    f"the publication to settle, or --source aws|gcp for an "
                    f"immutable snapshot.") from exc
            LOG.warn(f"{exc}; discarding staging and retrying in "
                     f"{ctx.torn_wait:.0f}s (retry {attempt}/{ctx.torn_retries})")
            time.sleep(ctx.torn_wait)
            src.invalidate()
            src.resolve()
            requested = resolve_requested(ctx, src, args.databases,
                                          auto_taxdb=auto_taxdb)
            targets = [src.revision(db) for db in requested]
            for target in targets:
                target.keep_archives = ctx.keep_archives
            desired = {t.dbname: t.revkey() for t in targets}
            if prev:
                for dbname in prev.dbs():
                    desired.setdefault(dbname,
                                       (prev.db_state(dbname) or {}).get("revkey"))
            work = compute_work(src, targets, prev, args.force)
            # The previous revision's staging is deliberately kept: the salvage
            # step in fetch_target reuses every file whose md5 still matches and
            # then deletes what is left.  Deleting it here made a torn `nt`
            # retry re-download everything, which is exactly what the documented
            # salvage behaviour promises not to do.

    # ---- carry over everything we did not touch --------------------------- #
    carried = {}
    if prev:
        for dbname in prev.dbs():
            if dbname in tables:
                continue
            st = prev.db_state(dbname) or {}
            table = {}
            for name, meta in (st.get("files") or {}).items():
                path = os.path.join(prev.dir, name)
                if os.path.isfile(path):
                    table[name] = (path, meta)
            if table:
                carried[dbname] = ensure_probes(table)

    all_tables = dict(carried)
    all_tables.update(tables)
    db_dates = {}
    for dbname, table in all_tables.items():
        dates = set()
        for name in table:
            if is_volume_index(name):
                marker = read_marker(table[name][0])
                if marker and marker.build_date:
                    dates.add(marker.build_date)
        if not dates:
            st = (prev.db_state(dbname) if prev else None) or {}
            dates = {d for d in [normalise_build_date(st.get("last_updated"))] if d}
        db_dates[dbname] = min(dates) if dates else None
    tree, _owners, shared = merge_tables(all_tables, db_dates)

    dates = set()
    for dbname in tables:
        if db_dates.get(dbname):
            dates.add(db_dates[dbname])
    stamp = max(dates)[:10] if dates else time.strftime("%Y-%m-%d")
    fingerprint = tree_fingerprint(desired)
    if src.key in ("gcp", "aws"):
        # keep the cloud snapshot visible in the directory name
        name = f"{src.key}-{src.snapshot_id()}-{fingerprint[:8]}"
    else:
        name = f"{src.key}-{stamp}-{fingerprint[:8]}"

    # ---- state ------------------------------------------------------------ #
    db_meta = {}
    for dbname, table in all_tables.items():
        prev_state = (prev.db_state(dbname) if prev else None) or {}
        if dbname in carried and prev_state:
            db_meta[dbname] = prev_state
            continue
        target = next((t for t in targets if t.dbname == dbname), None)
        files_state = {n: dict(m or {}) for n, (_p, m) in table.items()}
        for n in list(files_state):
            if n in shared.get(dbname, {}):
                del files_state[n]
        verif = "md5" if all(m.get("md5") for m in files_state.values()) else "mixed"
        db_meta[dbname] = {
            "revkey": desired.get(dbname),
            "source_key": src.key,
            "source_snapshot": src.snapshot_id(),
            "manifest_url": target.manifest_url if target else None,
            "dbtype": target.dbtype if target else None,
            "description": target.description if target else None,
            "last_updated": target.last_updated if target else None,
            "number_of_volumes": target.number_of_volumes if target else None,
            "bytes_total": target.bytes_total if target else None,
            "verification": verif,
            "files": files_state,
            "shared_files": shared.get(dbname, {}),
        }
    meta = {
        "created": utcnow(),
        "source": src.key,
        "source_snapshot": src.snapshot_id(),
        "snapshot": name,
        "previous": prev.name if prev else None,
        "build_date": stamp,
        "tree_fingerprint": fingerprint,
        "dbs": db_meta,
    }

    if ctx.dry_run:
        print(f"DRY-RUN would create a snapshot with {len(all_tables)} "
              f"database(s), {len(tree)} file(s):")
        for dbname in sorted(all_tables):
            print(f"  {dbname}: rev {desired.get(dbname)}")
        return EXIT_OK

    content_hash = sha1_hex("\n".join(
        f"{n}:{tree[n][2] or tree[n][1]}" for n in sorted(tree)))
    LOG.info(f"assembling snapshot {name} ({len(tree)} files, "
             f"{len(all_tables)} database(s))")

    def verify_existing(snap):
        """Used before re-using a directory that claims to hold our content."""
        for dbname in tables:
            st = db_meta.get(dbname) or {}
            issues, _info = verify_table(
                dbname, table_from_state(snap, st.get("files")),
                quick=False, check_md5=True)
            if issues:
                LOG.warn(f"existing snapshot {snap.name} fails verification for "
                         f"{dbname}: {issues[0]}")
                return False
        return True

    snapshot = store.build_snapshot(name, tree, meta, content_hash,
                                    verify_existing=verify_existing)

    # ---- final gate ------------------------------------------------------- #
    issues = []
    for dbname in sorted(all_tables):
        st = db_meta.get(dbname) or {}
        issues.extend(f"{dbname}: {m}" for m in
                      verify_table(dbname, table_from_state(snapshot, st.get("files")),
                                   quick=False, check_md5=args.check_md5)[0])
    issues.extend(f"smoke: {m}" for m in smoke_test(ctx, snapshot,
                                                    sorted(all_tables)))
    if issues:
        LOG.error(f"snapshot {snapshot.name} failed final verification:")
        for m in issues[:40]:
            LOG.error("  " + m)
        if len(issues) > 40:
            LOG.error(f"  ... and {len(issues) - 40} more")
        raise VerificationError("refusing to install a snapshot that does not "
                                "verify; `current` was left untouched")

    store.swap_current(snapshot.name)
    LOG.info(f"installed {snapshot.name}; {store.current_link} -> "
             f"{snapshot.name}")
    for dbname in sorted(all_tables):
        st = db_meta[dbname]
        dates = sorted({read_marker(os.path.join(snapshot.dir, n)).build_date
                        for n in (st.get("files") or {})
                        if is_volume_index(n)
                        and read_marker(os.path.join(snapshot.dir, n))})
        extra = f", build {dates[0]}" if len(dates) == 1 else ""
        LOG.info(f"  {dbname}: rev {st.get('revkey')}, "
                 f"{len(st.get('files') or {})} file(s), "
                 f"verification={st.get('verification')}{extra}")

    gc_snapshots(ctx, ctx.keep_snapshots, dry_run=False,
                 protect={snapshot.name})
    # staging is scratch space for the databases just installed (the tree is
    # their resume point now); other databases keep theirs
    drop_staging(ctx, keep_revkeys=set(),
                 dbnames={t.dbname for t in targets})
    if ctx.json_out:
        print(json.dumps({"snapshot": snapshot.name,
                          "previous": prev.name if prev else None,
                          "tree_fingerprint": fingerprint,
                          "databases": sorted(all_tables)}, indent=2))
    return EXIT_OK


def sweep_leftovers(ctx: Context) -> None:
    """Drop half-built snapshot directories left behind by a killed run.

    Only `<root>/.<name>.tmp<pid>` entries whose process is gone are removed, so
    a concurrent run is never touched.
    """
    try:
        names = os.listdir(ctx.root)
    except OSError:
        return
    for name in names:
        m = re.match(r"^\..+\.tmp(\d+)$", name)
        if not m or pid_alive(int(m.group(1))):
            continue
        LOG.verbose(f"removing build directory left over by an interrupted run: "
                    f"{name}")
        remove_quietly(os.path.join(ctx.root, name))


def drop_staging(ctx, keep_revkeys=None, dbnames=None):
    """Remove staging directories, by default for the databases just installed.

    `dbnames=None` means "every database", which is what `gc` wants.  A
    successful install must only clear the databases it handled: wiping the
    whole `.staging` would throw away the partially downloaded work of an
    unrelated database (a 900 GiB `nt` run, say) because some small database was
    installed afterwards.
    """
    base = ctx.store.staging
    if not os.path.isdir(base):
        return
    for dbname in os.listdir(base):
        if dbnames is not None and dbname not in dbnames:
            continue
        dbdir = os.path.join(base, dbname)
        if not os.path.isdir(dbdir):
            continue
        for rev in os.listdir(dbdir):
            if keep_revkeys and rev in keep_revkeys:
                continue
            remove_quietly(os.path.join(dbdir, rev))
        try:
            os.rmdir(dbdir)
        except OSError:
            pass


def gc_snapshots(ctx, keep, dry_run, protect=None):
    protect = set(protect or ())
    if ctx.store.current_name():
        protect.add(ctx.store.current_name())
    snaps = ctx.store.snapshots()          # oldest first
    keep_set = set(protect)
    for snap in reversed(snaps):
        if len(keep_set) >= keep:
            break
        keep_set.add(snap.name)
    removed = 0
    for snap in snaps:
        if snap.name in keep_set:
            continue
        size = dir_size(snap.dir)
        if dry_run:
            print(f"DRY-RUN would remove snapshot {snap.name} ({human_bytes(size)})")
            continue
        LOG.info(f"removing old snapshot {snap.name} ({human_bytes(size)})")
        remove_quietly(snap.dir)
        removed += 1
    return removed


# --------------------------------------------------------------------------- #
# other commands
# --------------------------------------------------------------------------- #
def cmd_showall(ctx, args):
    src = open_source(ctx)
    dbs = src.list_databases()
    if args.format == "json" or ctx.json_out:
        print(json.dumps({"source": src.key, "snapshot": src.snapshot_id(),
                          "databases": dbs}, indent=2))
        return EXIT_OK
    if args.format == "name":
        for row in dbs:
            print(row["dbname"])
        return EXIT_OK
    if args.format == "pretty":
        print(f"{'BLASTDB':<34} {'DESCRIPTION':<60} {'SIZE':>12}  "
              f"{'LAST_UPDATED':<12} VOLS")
    for row in dbs:
        size = row.get("bytes_total") or row.get("bytes_compressed") or 0
        if args.format == "pretty":
            print(f"{row['dbname']:<34} {(row.get('description') or '')[:58]:<60} "
                  f"{human_bytes(size):>12}  "
                  f"{str(row.get('last_updated'))[:10]:<12} "
                  f"{row.get('number_of_volumes') or ''}")
        else:
            print(f"{row['dbname']}\t{row.get('description')}\t"
                  f"{size / 1e9:.4f}\t{row.get('last_updated')}")
    LOG.info(f"source {src.describe()}: {len(dbs)} databases")
    return EXIT_OK


def pick_snapshot(ctx, name=None):
    if name:
        snap = Snapshot(ctx.store.root, name)
        if not snap.exists():
            raise BlastError(f"no such snapshot: {name}")
        return snap
    snap = ctx.store.current()
    if snap is None:
        raise BlastError(f"{ctx.store.current_link} is not a symlink; use "
                         f"--snapshot NAME")
    return snap


def cmd_verify(ctx, args):
    snap = pick_snapshot(ctx, args.snapshot)
    dbs = args.databases or snap.dbs()
    out, failed = {}, 0
    for dbname in dbs:
        st = snap.db_state(dbname)
        if st is None:
            out[dbname] = {"status": "absent", "issues": ["not in snapshot"]}
            failed += 1
            LOG.error(f"{dbname}: not present in snapshot {snap.name}")
            continue
        issues, info = verify_table(dbname, table_from_state(snap, st.get("files")),
                                    quick=args.quick, check_md5=not args.no_md5)
        if args.against_remote:
            try:
                src = open_source(ctx)
                remote = src.revision(dbname)
                if remote.revkey() != st.get("revkey"):
                    issues.append(f"REMOTE: a newer revision is available "
                                  f"({remote.revkey()} vs installed "
                                  f"{st.get('revkey')})")
            except BlastError as exc:
                issues.append(f"REMOTE: cannot check ({exc})")
        issues.extend(f"smoke: {m}" for m in smoke_test(ctx, snap, [dbname]))
        out[dbname] = {"status": "ok" if not issues else "failed",
                       "revkey": st.get("revkey"),
                       "files": len(st.get("files") or {}),
                       "volumes": info.get("volumes"),
                       "build_dates": info.get("build_dates"),
                       "verification": st.get("verification"),
                       "issues": issues}
        if issues:
            failed += 1
            LOG.error(f"{dbname}: {len(issues)} problem(s)")
            for m in issues[:20]:
                LOG.error("  " + m)
        else:
            LOG.info(f"{dbname}: ok - {out[dbname]['files']} files, "
                     f"{out[dbname]['volumes']} volume(s), build "
                     f"{out[dbname]['build_dates']}, rev {st.get('revkey')}")
    if ctx.json_out:
        print(json.dumps({"snapshot": snap.name, "databases": out}, indent=2))
    return EXIT_VERIFY if failed else EXIT_OK


def cmd_repair(ctx, args):
    snap = pick_snapshot(ctx, args.snapshot)
    dbs = args.databases or snap.dbs()
    broken = []
    for dbname in dbs:
        st = snap.db_state(dbname)
        if st is None:
            broken.append(dbname)
            continue
        issues, _info = verify_table(dbname, table_from_state(snap, st.get("files")),
                                     quick=args.quick)
        if issues:
            LOG.warn(f"{dbname}: {len(issues)} problem(s) - will re-download")
            for m in issues[:5]:
                LOG.warn("  " + m)
            broken.append(dbname)
    if not broken:
        LOG.info("nothing to repair")
        return EXIT_OK
    LOG.info("repairing: " + ", ".join(broken))
    sub = argparse.Namespace(**vars(args))
    sub.databases = broken
    sub.force = True
    sub.takeover = False
    sub.check_md5 = True
    # repair keeps the files that still verify, but a repair must not trust the
    # cheap probe - it re-hashes them instead.
    ctx.reuse_verify = "md5"
    ctx.cfg["reuse_verify"] = "md5"
    return cmd_download(ctx, sub, auto_taxdb=False, allow_reuse=True)


def cmd_list(ctx, args):
    current = ctx.store.current_name()
    rows = []
    for snap in ctx.store.snapshots():
        rows.append({"snapshot": snap.name,
                     "current": snap.name == current,
                     "created": snap.state.get("created"),
                     "source": snap.state.get("source"),
                     "source_snapshot": snap.state.get("source_snapshot"),
                     "build_date": snap.state.get("build_date"),
                     "tree_fingerprint": snap.state.get("tree_fingerprint"),
                     "databases": snap.dbs(),
                     "bytes": dir_size(snap.dir)})
    if ctx.json_out:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    for row in rows:
        print(f"{'*' if row['current'] else ' '} {row['snapshot']:<38} "
              f"{row['created']!s:<21} {human_bytes(row['bytes']):>12}  "
              f"{','.join(row['databases'])}")
    if not rows:
        LOG.info("no snapshots yet")
    return EXIT_OK


def cmd_gc(ctx, args):
    keep = args.keep if args.keep is not None else ctx.keep_snapshots
    n = gc_snapshots(ctx, keep, args.dry_run)
    staged = 0
    base = ctx.store.staging
    if os.path.isdir(base):
        for dbname in os.listdir(base):
            dbdir = os.path.join(base, dbname)
            if not os.path.isdir(dbdir):
                continue
            for rev in os.listdir(dbdir):
                if args.dry_run:
                    print(f"DRY-RUN would remove staging {dbname}/{rev}")
                    continue
                remove_quietly(os.path.join(dbdir, rev))
                staged += 1
            if not args.dry_run:
                try:
                    os.rmdir(dbdir)
                except OSError:
                    pass
    LOG.info(f"gc: removed {n} snapshot(s), {staged} staging director(ies)")
    return EXIT_OK


def cmd_rollback(ctx, args):
    snaps = ctx.store.snapshots()
    if not snaps:
        raise BlastError("no snapshots available")
    current = ctx.store.current_name()
    if args.list:
        for i, snap in enumerate(reversed(snaps)):
            print(f"{i}: {snap.name}"
                  + ("   <- current" if snap.name == current else ""))
        return EXIT_OK
    if args.snapshot:
        target = Snapshot(ctx.store.root, args.snapshot)
        if not target.exists():
            raise BlastError(f"no such snapshot: {args.snapshot}")
    else:
        idx = args.to if args.to is not None else 1
        ordered = list(reversed(snaps))
        if idx >= len(ordered):
            raise BlastError(f"only {len(ordered)} snapshot(s) available")
        target = ordered[idx]
    ctx.store.swap_current(target.name)
    LOG.info(f"current -> {target.name} ({','.join(target.dbs())})")
    return EXIT_OK


def cmd_inspect(ctx, args):
    """Report the embedded build fingerprint of an existing directory.

    Works on any directory of BLAST files - including one produced by
    `update_blastdb.pl` or by a hand written aria2c loop - because it needs no
    state file and no BLAST installation: it reads the volume indexes.
    """
    base = os.path.abspath(args.dir or os.path.join(ctx.root, "current"))
    if not os.path.isdir(base):
        raise BlastError(f"{base} is not a directory")
    listing = [n for n in sorted(os.listdir(base))
               if os.path.isfile(os.path.join(base, n))]
    out, failed = {}, 0
    for dbname in args.databases:
        table, shared = {}, []
        for name in listing:
            path = os.path.join(base, name)
            if is_shared_taxonomy(name):
                if dbname == "taxdb":
                    table[name] = (path, {"size": os.path.getsize(path)})
                else:
                    shared.append(name)
                continue
            if not belongs_to_database(name, dbname):
                continue
            table[name] = (path, {"size": os.path.getsize(path)})
        if not table:
            out[dbname] = {"status": "absent",
                           "issues": [f"no files belonging to {dbname} in {base}"]}
            failed += 1
            LOG.error(f"{dbname}: no files found in {base}")
            continue
        issues, info = verify_table(dbname, table)
        markers = info.get("markers") or {}
        volumes = []
        for name in sorted(markers):
            marker = markers[name]
            volumes.append({"file": name, "ordinal": marker.ordinal,
                            "build": marker.build_date or marker.build_date_raw,
                            "size": table[name][1].get("size")})
        dates = info.get("build_dates") or []
        verdict = "OK" if not issues else "FAILED"
        out[dbname] = {"status": verdict, "directory": base,
                       "volumes": volumes, "build_dates": dates,
                       "files": len(table), "shared_files": shared,
                       "bytes": sum((m or {}).get("size") or 0
                                    for _p, m in table.values()),
                       "issues": issues}
        if issues:
            failed += 1
        if not ctx.json_out:
            LOG.info(f"{dbname}  ({base})")
            LOG.info(f"  files              : {len(table)}")
            nvol = len(volumes)
            if nvol:
                span = f"{volumes[0]['ordinal']}..{volumes[-1]['ordinal']}"
                LOG.info(f"  volume indexes     : {nvol} (ordinals {span})")
            if len(dates) == 1:
                LOG.info(f"  embedded build date: {dates[0]} (all volumes agree)")
            elif len(dates) > 1:
                detail = "; ".join(
                    f"{d}: " + ", ".join(
                        v["file"] for v in volumes if v["build"] == d)[:80]
                    for d in dates)
                LOG.info(f"  embedded build date: MIXED -> {detail}")
            else:
                LOG.info("  embedded build date: unknown (no readable volume "
                         "index)")
            if shared:
                LOG.info(f"  shared taxonomy    : "
                         f"{', '.join(shared[:6])}"
                         + (" ..." if len(shared) > 6 else ""))
            if nvol and nvol <= 12 and args.verbose:
                for v in volumes:
                    LOG.info(f"    {v['file']:<24} ordinal={v['ordinal']:<4} "
                             f"build={v['build']}")
            LOG.info(f"  verdict            : {verdict}")
            for msg in issues[:20]:
                LOG.info(f"      {msg}")
    if ctx.json_out:
        print(json.dumps(out, indent=2, sort_keys=True))
    return EXIT_VERIFY if failed else EXIT_OK


def staging_rows(ctx, dbnames=None):
    """Inventory of everything left in .staging (i.e. resumable work)."""
    base = ctx.store.staging
    rows = []
    if not os.path.isdir(base):
        return rows
    for dbname in sorted(os.listdir(base)):
        dbdir = os.path.join(base, dbname)
        if not os.path.isdir(dbdir) or (dbnames and dbname not in dbnames):
            continue
        for rev in sorted(os.listdir(dbdir)):
            path = os.path.join(dbdir, rev)
            if not os.path.isdir(path):
                continue
            try:
                names = os.listdir(path)
            except OSError:
                continue
            archives = [n for n in names if n.endswith(".tar.gz")]
            controls = [n for n in names if n.endswith(".aria2")]
            extract_dir = os.path.join(path, "extracted")
            extracted = []
            if os.path.isdir(extract_dir):
                extracted = [n for n in os.listdir(extract_dir)
                             if not n.startswith(".")]
            rows.append({
                "dbname": dbname,
                "revkey": rev,
                "path": path,
                "bytes": dir_size(path),
                "archives": archives,
                "partials": controls,
                "extracted": extracted,
                "receipt": len(load_extraction_receipt(path)),
                "files": [n for n in names
                          if os.path.isfile(os.path.join(path, n))
                          and not n.endswith((".aria2", ".json"))],
            })
    return rows


def salvageable_count(ctx, dbname, target):
    """How many files of `target` staging can already satisfy (md5 checked)."""
    count = 0
    rows = staging_rows(ctx, [dbname])
    for rf in target.files:
        for row in rows:
            for base in (row["path"], os.path.join(row["path"], "extracted")):
                ok, _why = verify_local_file(os.path.join(base, rf.name),
                                             rf.size, rf.md5)
                if ok:
                    count += 1
                    break
            else:
                continue
            break
    return count


def cmd_staging(ctx, args):
    """What is left over from interrupted or older runs, and how much of it is
    still usable for the revision the source is publishing now."""
    rows = staging_rows(ctx, args.databases or None)
    if not rows:
        LOG.info(f"nothing in {ctx.store.staging}: no interrupted work to resume")
        return EXIT_OK
    report = []
    for row in rows:
        entry = {k: row[k] for k in ("dbname", "revkey", "bytes", "receipt")}
        entry["archives"] = len(row["archives"])
        entry["partial_downloads"] = len(row["partials"])
        entry["extracted_files"] = len(row["extracted"])
        entry["resumable"] = None
        entry["salvageable"] = None
        report.append(entry)
        if not ctx.json_out:
            LOG.info(f"{row['dbname']}  revkey {row['revkey']}  "
                     f"({human_bytes(row['bytes'])})")
            LOG.info(f"  complete archives   : {len(row['archives'])}")
            if row["partials"]:
                LOG.info(f"  half downloaded     : {len(row['partials'])} "
                         f"(resumable, aria2 control files present)")
            if row["extracted"]:
                LOG.info(f"  already unpacked    : {len(row['extracted'])} "
                         f"file(s), {row['receipt']} archive(s) in the receipt")
    if args.against_remote:
        src = open_source(ctx)
        for row, entry in zip(rows, report, strict=True):
            try:
                target = src.revision(row["dbname"])
            except BlastError as exc:
                entry["error"] = str(exc)
                LOG.warn(f"{row['dbname']}: cannot compare with {src.key} ({exc})")
                continue
            entry["source"] = f"{src.key}:{src.snapshot_id()}"
            entry["resumable"] = target.revkey() == row["revkey"]
            if entry["resumable"]:
                LOG.info(f"  -> identical to the revision {src.key} publishes "
                         f"now: re-running resumes it as is")
                continue
            salvageable = salvageable_count(ctx, row["dbname"], target)
            entry["salvageable"] = salvageable
            LOG.info(f"  -> {src.key} now publishes revision {target.revkey()}"
                     f"({target.last_updated}); this leftover is an older one: "
                     f"{salvageable}/{len(target.files)} file(s) still match and "
                     f"will be reused, the rest is downloaded again")
    if ctx.json_out:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return EXIT_OK


def cmd_doctor(ctx, args):
    """Report the environment this mirror will run in."""
    env = collect_environment(ctx.root)
    if ctx.json_out:
        print(json.dumps(env, indent=2, sort_keys=True, default=str))
        return EXIT_OK
    LOG.info(f"mirror root        : {env['root']}")
    for line in format_environment(env, verbose=True):
        LOG.info(line)
    LOG.info("")
    LOG.info("notes: quota needs the `quota` package and a filesystem mounted "
             "with quota support; a user quota is often far smaller than df's "
             "free space, which is why the space check uses the smaller of the "
             "two.  See the dependency table in README.md.")
    return EXIT_OK


def cmd_config(ctx, args):
    if getattr(args, "template", False):
        print(render_config_template(), end="")
        return EXIT_OK
    if getattr(args, "init", False):
        path = args.path or os.path.join(ctx.root, "blastdb-download.toml")
        if os.path.exists(path) and not args.force:
            raise BlastError(f"{path} already exists; pass --force to overwrite "
                             f"it, or --template to print one instead")
        if ctx.dry_run:
            print(f"DRY-RUN would write the configuration template to {path}")
            return EXIT_OK
        atomic_write_text(path, render_config_template())
        LOG.info(f"wrote configuration template to {path}")
        LOG.info("it is read automatically from now on; every setting in it is "
                 "commented out, so nothing changes until you edit it")
        return EXIT_OK
    print(json.dumps({**{k: v for k, v in ctx.cfg.items()
                         if not k.startswith("_")},
                      "config_search_path": config_candidates(ctx.root)},
                     indent=2, sort_keys=True, default=str))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# configuration and CLI
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "root": os.path.join(os.getcwd(), "blastdb"),
    "source": "gcp",
    "ncbi_dir": NCBI_DEFAULT_DIR,
    "ncbi_base": NCBI_DEFAULT_BASE,
    "ncbi_url": "auto",      # auto | mirror | manifest
    "jobs": max(1, min(8, (os.cpu_count() or 4) // 2)),
    "connections": 4,
    "min_split": 64 << 20,
    "tries": 5,
    "timeout": 60.0,
    "keep_snapshots": 2,
    "add_metadata_json": True,
    "with_taxdb": True,
    "keep_archives": False,
    "dedupe_taxonomy": True,
    "probe_sizes": True,
    "reuse_verify": "probe",
    "min_free": 2 << 30,
    "file_retries": 3,
    "progress_interval": 3600.0,
    "log_file": None,
    "auto_log": True,
    "console": "auto",
    "user": None,
    "adopt": [],
    "adopt_partial": False,
    "adopt_extracted": False,
    "on_torn": "retry",
    "torn_retries": 3,
    "torn_wait": 60.0,
    "smoke_test": "auto",
    "disk_check": True,
    "aria2c": "auto",
    "aria2c_extra_args": [],
    "limit_rate": None,
    "insecure": False,
    "json": False,
    "dry_run": False,
}


def config_candidates(root_hint=None):
    """Configuration files, most specific first; the first one wins.

    Nothing is ever generated implicitly - `config --init` writes a template
    when you ask for it.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    out = []
    if root_hint:
        out.append(os.path.join(os.path.abspath(root_hint),
                                "blastdb-download.toml"))
    out.append(os.path.join(xdg, "blastdb-download", "config.toml"))
    out.append(os.path.join(os.path.expanduser("~"), ".blastdb-download.toml"))
    return out


def load_config(path=None, root_hint=None):
    cfg = dict(DEFAULTS)
    chosen = None
    if path:
        if not os.path.isfile(path):
            raise BlastError(f"config file not found: {path}")
        chosen = path
    else:
        for cand in config_candidates(root_hint):
            if os.path.isfile(cand):
                chosen = cand
                break
    if chosen:
        if tomllib is None:
            raise BlastError("reading a TOML config requires Python 3.11+")
        with open(chosen, "rb") as fh:
            data = tomllib.load(fh)
        for section in ("general", "download", "layout"):
            for key, value in (data.get(section) or {}).items():
                if key in cfg:
                    cfg[key] = value
        for key, value in data.items():
            if key in cfg:
                cfg[key] = value
        LOG.verbose(f"loaded configuration from {chosen}")
    cfg["config_file"] = chosen
    return cfg


CONFIG_TEMPLATE = """\
# Configuration for blastdb_download.py __VERSION__          generated __DATE__
#
# Nothing is written or changed implicitly: this file only exists because you
# asked for it with `config --init`.  Every setting below is COMMENTED OUT, so
# it documents the built-in defaults without pinning them - uncomment only what
# you want to change.
#
# Precedence (first match wins):
#   command line options
#   > --config FILE
#   > <root>/blastdb-download.toml
#   > $XDG_CONFIG_HOME/blastdb-download/config.toml
#   > ~/.blastdb-download.toml
#   > built-in defaults

[general]
# root = "/data/blastdb"            # example; mirror root: snapshots + `current`
# source = "gcp"                    # gcp | aws | ncbi | auto
#                                  #   gcp/aws: immutable snapshot, ~1 month behind
#                                  #   ncbi:    freshest, torn-update protection
# ncbi_dir = "/blast/db"            # NCBI directory, e.g. /blast/db/v5
# ncbi_base = "https://ftp.ncbi.nlm.nih.gov"
#                                  # origin of the NCBI tree, or a regional replica
# ncbi_url = "auto"                 # auto | mirror | manifest
#                                  #   auto = mirror when ncbi_base is not the default

# ---- transfer ------------------------------------------------------------
# jobs = 8                         # example; default max(1, min(8, cores/2))
#                                  #   total parallel connections
# connections = 4                  # connections per file
# min_split = 67108864             # smallest range request unit, bytes (64 MiB)
# limit_rate = "50M"               # example; overall download limit
# tries = 5                        # attempts per request
# timeout = 60.0                   # connect/read timeout in seconds

# ---- consistency ---------------------------------------------------------
# keep_snapshots = 2               # snapshots kept for rollback and `gc`
# reuse_verify = "probe"           # probe | md5 | size
#                                  #   how hard to re-check an installed file
#                                  #   before hard-linking it into a new snapshot
# on_torn = "retry"                # retry | fail
#                                  #   source published a new revision mid-transfer
# torn_retries = 3                 # extra attempts before giving up (exit code 4)
# torn_wait = 60.0                 # seconds between those attempts
# disk_check = true                # refuse to start without enough free space
# min_free = 2147483648            # abort mid-transfer below this (2 GiB)
# file_retries = 3                 # whole-batch attempts for retryable failures
# progress_interval = 3600.0       # seconds between periodic progress lines
# console = "auto"                 # auto | full | errors | off
# auto_log = true                  # write <root>/log for download/repair/gc/rollback
# log_file = "/var/log/blastdb.log" # example; also write diagnostics to a file
# user = "alice"                   # example; whose quota to check

# ---- adopting an existing download ---------------------------------------
# adopt = ["/data/public/databases/NT"]  # example; directories to search for
#                                  # files another tool already downloaded
# adopt_partial = true             # example; resume half finished files too
# adopt_extracted = true           # example; also adopt extracted payload that
#                                  # forms a complete build (tar.gz only)

# ---- content -------------------------------------------------------------
# with_taxdb = true                # implicitly mirror `taxdb` for cloud sources
# add_metadata_json = true         # also fetch <db>-nucl-metadata.json
# keep_archives = false            # keep the verified .tar.gz in the snapshot
# dedupe_taxonomy = true           # skip archive members already present
# probe_sizes = true               # HEAD unknown object sizes

# ---- tools ---------------------------------------------------------------
# aria2c = "auto"                  # "auto" | "none" | /path/to/aria2c
# aria2c_extra_args = []
# smoke_test = "auto"              # auto | always | never
# insecure = false                 # do not verify TLS certificates

# ---- output --------------------------------------------------------------
# json = false                     # always emit machine readable output
# dry_run = false                  # only print the plan; never transfer anything
"""


def render_config_template() -> str:
    return (CONFIG_TEMPLATE.replace("__VERSION__", __version__)
            .replace("__DATE__", utcnow()[:10]))


def global_option_specs():
    """Single source of truth for the options that work anywhere on the line.

    They are registered on the top level parser *and*, with
    ``default=argparse.SUPPRESS``, on every sub-command parser, so
    ``download nt --jobs 8`` and ``--jobs 8 download nt`` both work and neither
    copy clobbers the other (see the argparse `parents` documentation).
    """
    return [
        (("-c", "--config"), dict(metavar="FILE",
                                  help="read settings from this TOML file")),
        (("-r", "--root"), dict(metavar="DIR",
                                help="mirror root holding the snapshot trees "
                                     "(default: ./blastdb)")),
        (("-s", "--source"), dict(choices=["gcp", "aws", "ncbi", "auto"],
                                  help="where to download from (default: gcp)")),
        (("--ncbi-dir",), dict(metavar="PATH",
                               help=f"NCBI directory (default {NCBI_DEFAULT_DIR})")),
        (("--ncbi-base",), dict(metavar="URL",
                                help=f"origin of the NCBI tree, e.g. a regional "
                                     f"replica (default https://{NCBI_HOST}); "
                                     f"setting it also redirects the payload URLs")),
        (("--ncbi-url",), dict(choices=["auto", "mirror", "manifest"],
                               help="mirror: fetch <file> from "
                                    "--ncbi-base/--ncbi-dir; manifest: use the "
                                    "URL published in the manifest (ftp:// "
                                    "becomes https://).  Default: mirror when "
                                    "--ncbi-base points somewhere else")),
        (("--aria2c",), dict(metavar="PATH|auto|none",
                             help="aria2c binary, or 'none' to use the built-in "
                                  "downloader (default: auto)")),
        (("-j", "--jobs"), dict(type=int,
                                help="parallel connections "
                                     "(default: max(1, min(8, cores/2)))")),
        (("-x", "--connections"), dict(type=int,
                                       help="connections per file (default: 4)")),
        (("-k", "--min-split-size"), dict(type=parse_size, metavar="SIZE",
                                          dest="min_split",
                                          help="smallest range request unit; "
                                               "files larger than this are "
                                               "split (default: 64M)")),
        (("--limit-rate",), dict(type=parse_size, metavar="SIZE",
                                 help="overall download limit, e.g. 50M")),
        (("--timeout",), dict(type=float, metavar="SEC",
                              help="connect/read timeout (default: 60)")),
        (("--tries",), dict(type=int, metavar="N",
                            help="attempts per request (default: 5)")),
        (("--keep-snapshots",), dict(type=int, metavar="N",
                                     help="snapshots kept for rollback "
                                          "(default: 2)")),
        (("--reuse-verify",), dict(choices=["probe", "md5", "size"],
                                   help="how hard to re-check an installed file "
                                        "before hard-linking it into a new "
                                        "snapshot (default: probe)")),
        (("--on-torn",), dict(choices=["retry", "fail"],
                              help="what to do when the source publishes a new "
                                   "revision mid-transfer (default: retry)")),
        (("--torn-retries",), dict(type=int, metavar="N",
                                   help="extra attempts before giving up "
                                        "(default: 3)")),
        (("--torn-wait",), dict(type=float, metavar="SEC",
                                help="seconds between those attempts "
                                     "(default: 60)")),
        (("--taxdb",), dict(dest="with_taxdb", action="store_true", default=None,
                            help="also mirror taxdb (default for cloud sources)")),
        (("--no-taxdb",), dict(dest="with_taxdb", action="store_false",
                               help="do not implicitly add taxdb")),
        (("--metadata-json",), dict(dest="add_metadata_json",
                                    action="store_true", default=None,
                                    help="also fetch <db>-nucl-metadata.json "
                                         "(default)")),
        (("--no-metadata-json",), dict(dest="add_metadata_json",
                                       action="store_false",
                                       help="fetch exactly the files listed in "
                                            "the manifest")),
        (("--keep-archives",), dict(action="store_true", default=None,
                                    help="keep verified .tar.gz archives inside "
                                         "the snapshot")),
        (("--no-dedupe-taxonomy",), dict(dest="dedupe_taxonomy",
                                         action="store_false", default=None,
                                         help="extract every archive member even "
                                              "when an identical-sized copy is "
                                              "already present")),
        (("--no-probe-sizes",), dict(dest="probe_sizes", action="store_false",
                                     default=None,
                                     help="do not HEAD objects of unknown size")),
        (("--min-free",), dict(type=parse_size, metavar="SIZE",
                                help="abort a transfer when usable space drops "
                                     "below this (default: 2 GiB); the files "
                                     "that did arrive are kept for the next run")),
        (("--file-retries",), dict(type=int, metavar="N",
                                   help="whole-batch attempts when individual "
                                        "files fail for a retryable reason "
                                        "(default: 3)")),
        (("--progress-interval",), dict(type=float, metavar="SEC",
                                        help="seconds between periodic progress "
                                             "lines (default: 3600); a line is "
                                             "also written for every finished "
                                             "file")),
        (("--console",), dict(choices=["auto", "full", "errors", "off"],
                              help="what still goes to the terminal: auto "
                                   "(default) keeps only warnings and errors "
                                   "on screen once a log file is in use, full "
                                   "mirrors everything, errors/off are "
                                   "quieter still")),
        (("--no-log-file",), dict(dest="auto_log", action="store_false",
                                  default=None,
                                  help="do not create the default "
                                       "<root>/log for download/repair/gc/"
                                       "rollback")),
        (("--adopt",), dict(action="append", metavar="DIR", dest="adopt",
                            help="also look for already downloaded files in "
                                 "DIR (repeatable); complete files are verified "
                                 "against the authoritative md5 and hard-linked "
                                 "into staging instead of being fetched again")),
        (("--adopt-partial",), dict(action="store_true", default=None,
                                    help="also adopt half finished files so they "
                                         "are resumed, not restarted")),
        (("--adopt-extracted",), dict(action="store_true", default=None,
                                      help="also adopt extracted payload files "
                                           "when they form one complete build "
                                           "(tar.gz layout only)")),
        (("--log-file",), dict(metavar="FILE",
                               help="append all diagnostics to FILE as well "
                                    "(timestamped), so a long unattended run "
                                    "can be followed with tail -f")),
        (("--smoke-test",), dict(choices=["auto", "always", "never"],
                                 help="ask the local blastdbcmd whether it "
                                      "accepts the database (default: auto, "
                                      "advisory only)")),
        (("--no-disk-check",), dict(dest="disk_check", action="store_false",
                                    default=None,
                                    help="skip the free space check")),
        (("--insecure",), dict(action="store_true", default=None,
                               help="do not verify TLS certificates")),
        (("--dry-run",), dict(action="store_true", default=None,
                              help="print the plan and transfer nothing")),
        (("--json",), dict(action="store_true", default=None,
                           help="machine readable output on stdout")),
        (("-q", "--quiet"), dict(action="store_true", default=False,
                                 help="no diagnostics on stderr")),
        (("-v", "--verbose"), dict(action="count", default=0,
                                   help="more diagnostics; repeatable")),
    ]


def add_global_options(parser, shared=False):
    for flags, kwargs in global_option_specs():
        kw = dict(kwargs)
        if shared:
            kw["default"] = argparse.SUPPRESS
        parser.add_argument(*flags, **kw)


def iter_subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield name, sub


class _Parser(argparse.ArgumentParser):
    """Name the sub-command an option belongs to when it is misplaced.

    Global options are accepted before and after the sub-command, but a
    sub-command option (say `--force`) is not: argparse then only says
    "unrecognized arguments", which is a poor hint for something the docs list
    right next to options that *do* work anywhere.
    """

    def error(self, message):
        match = re.match(r"unrecognized arguments: (.*)", message or "")
        if match:
            misplaced = [tok for tok in match.group(1).split()
                         if tok.startswith("-")]
            owners = {}
            for name, sub in iter_subparsers(self):
                for action in sub._actions:
                    for option in action.option_strings:
                        if option in misplaced and option not in owners:
                            owners[option] = name
            for option, name in owners.items():
                message += (f"\n  hint: `{option}` is an option of the `{name}` "
                            f"sub-command; put it after `{name}`")
            if not owners and misplaced:
                message += ("\n  hint: run with --help to see the options of "
                            "each sub-command")
        super().error(message)


def build_parser():
    parser = _Parser(
        prog=PROGRAM,
        description="Download pre-formatted BLAST databases with set-level "
                    "consistency guarantees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s showall --format pretty
  %(prog)s download nt core_nt --jobs 8 --connections 4
  %(prog)s verify
  %(prog)s repair nt
  %(prog)s rollback --to 1

BLAST is used through the `current` symlink:
  export BLASTDB=/data/blastdb/current
  blastn -db nt -query q.fa -out out.txt

global options are accepted before or after the sub-command.

run `%(prog)s doctor` to see the free space, the user quota and which optional
helpers (aria2c, gsutil, aws, blastdbcmd, quota, ...) are available here.""")
    parser.add_argument("--version", action="version",
                        version=f"{PROGRAM} {__version__}")
    add_global_options(parser)

    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, shared=True)

    sub = parser.add_subparsers(dest="command", parser_class=_Parser)

    p = sub.add_parser("showall", help="list databases available at the source",
                       parents=[common])
    p.add_argument("--format", choices=["name", "tsv", "pretty", "json"],
                   default="name")
    p.set_defaults(func=cmd_showall)

    p = sub.add_parser("download", aliases=["update"], parents=[common],
                       help="download or update databases")
    p.add_argument("databases", nargs="+", metavar="DB")
    p.add_argument("--force", action="store_true",
                   help="ignore the installed revision and fetch everything")
    p.add_argument("--takeover", action="store_true",
                   help="rename a pre-existing real `current` directory aside")
    p.add_argument("--no-check-md5", dest="check_md5", action="store_false",
                   default=True,
                   help="skip the md5 re-verification of the assembled snapshot")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("verify", help="verify an installed snapshot",
                       parents=[common])
    p.add_argument("databases", nargs="*", metavar="DB")
    p.add_argument("--snapshot", metavar="NAME")
    p.add_argument("--quick", action="store_true",
                   help="check size and content probes, not full md5")
    p.add_argument("--no-md5", action="store_true",
                   help="do not hash anything")
    p.add_argument("--against-remote", action="store_true",
                   help="also report whether a newer revision is published")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("repair", parents=[common],
                       help="re-download anything failing verification")
    p.add_argument("databases", nargs="*", metavar="DB")
    p.add_argument("--snapshot", metavar="NAME")
    p.add_argument("--quick", action="store_true")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("list", help="list local snapshots", parents=[common])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("gc", parents=[common],
                       help="prune old snapshots and staging leftovers")
    p.add_argument("--keep", type=int, metavar="N",
                   help="snapshots to keep (default: --keep-snapshots)")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("rollback", parents=[common],
                       help="point `current` at an older snapshot")
    p.add_argument("--to", type=int, metavar="N",
                   help="0 = newest, 1 = the one before it, ...")
    p.add_argument("--snapshot", metavar="NAME")
    p.add_argument("--list", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("inspect", parents=[common],
                       help="report the embedded build fingerprint of an "
                            "existing directory (no state file needed)")
    p.add_argument("databases", nargs="+", metavar="DB")
    p.add_argument("--dir", metavar="DIR",
                   help="directory to inspect (default: <root>/current)")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("staging", parents=[common],
                       help="what interrupted runs left behind and how much of "
                            "it is still usable")
    p.add_argument("databases", nargs="*", metavar="DB")
    p.add_argument("--against-remote", action="store_true",
                   help="compare with what the source publishes right now")
    p.set_defaults(func=cmd_staging)

    p = sub.add_parser("doctor", parents=[common],
                       help="report space, quota and which helper tools exist")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", parents=[common],
                       help="print the effective configuration")
    p.add_argument("--init", action="store_true",
                   help="write a commented configuration template")
    p.add_argument("--template", action="store_true",
                   help="print a commented configuration template")
    p.add_argument("--path", metavar="FILE",
                   help="target file for --init "
                        "(default: <root>/blastdb-download.toml)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing file with --init")
    p.set_defaults(func=cmd_config)

    return parser


def merge_args(cfg, args):
    for key in DEFAULTS:
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    if getattr(args, "root", None):
        cfg["root"] = os.path.abspath(args.root)
    return cfg


MUTATING_COMMANDS = {"download", "update", "repair", "gc", "rollback"}


def main(argv=None):
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    # `-v` before and after the sub-command end up in different namespaces; take
    # whichever count is higher so `-v` always means "one more level"
    spelled = sum(argv.count(flag) for flag in ("-v", "--verbose"))
    LOG.quiet = bool(getattr(args, "quiet", False)) or \
        "-q" in argv or "--quiet" in argv
    LOG.level = 1 + max(int(getattr(args, "verbose", 0) or 0), spelled)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_ERR
    cfg = None
    try:
        cfg = merge_args(load_config(args.config, args.root), args)
        LOG.console = cfg.get("console") or "auto"
        LOG.timestamps = not sys.stderr.isatty()
        ctx = Context(cfg, args)
        log_path = ctx.log_file
        if log_path is None and ctx.auto_log \
                and getattr(args, "command", None) in MUTATING_COMMANDS:
            # a mirror that changes must be traceable: default to <root>/log
            log_path = os.path.join(ctx.root, "log")
            LOG.info(f"logging to {log_path} (use --log-file to move it, "
                     f"--no-log-file to disable)")
        if log_path:
            LOG.open_file(log_path)
        if LOG.level >= 2:
            for line in format_environment(collect_environment(ctx.root),
                                           verbose=LOG.level >= 3):
                LOG.verbose(line)
        if getattr(args, "command", None) in MUTATING_COMMANDS:
            with RootLock(ctx.root):
                return args.func(ctx, args)
        return args.func(ctx, args)
    except VerificationError as exc:
        LOG.error(str(exc))
        return EXIT_VERIFY
    except TornUpdateError as exc:
        LOG.error(str(exc))
        return EXIT_TORN
    except BlastError as exc:
        LOG.error(str(exc))
        return EXIT_ERR
    except KeyboardInterrupt:
        LOG.error("interrupted")
        if cfg:
            staging = os.path.join(os.path.abspath(cfg["root"]), ".staging")
            try:
                pending = os.listdir(staging)
            except OSError:
                pending = []
            if pending:
                LOG.error(f"partial downloads are kept under {staging}; re-run the "
                          f"same command to resume - finished files and completed "
                          f"chunks are not fetched again")
        return 130
    except Exception as exc:                 # never lose a traceback silently
        LOG.error(f"unexpected {type(exc).__name__}: {exc}")
        if LOG.level >= 2:
            import traceback
            LOG.error(traceback.format_exc())
        return EXIT_ERR
    finally:
        # the log file must outlive the exception handlers above, otherwise the
        # reason for a failure never reaches it
        LOG.close()


if __name__ == "__main__":
    sys.exit(main())
