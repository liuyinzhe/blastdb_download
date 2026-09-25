# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lyz  (see LICENSE)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Self-contained tests for blastdb_download.py.

They drive the real CLI against a local fake NCBI tree served over HTTP so the
interesting failure modes can actually be reproduced:

  * NCBI publishes a new revision *while* we are downloading  -> must abort
    (or converge after a retry) and must never install a mixed set,
  * two volumes of one database carry different embedded build timestamps
    -> must be refused,
  * a volume is missing from the manifest                     -> must be refused,
  * a locally damaged file would be hard-linked into the next snapshot
    -> must be caught and repaired,
  * atomic snapshot switching, rollback, gc, config, dry-run.

Run:  python3 test_blastdb_download.py -v
"""

import argparse
import base64
import hashlib
import gzip
import http.server
import io
import json
import os
import re
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "blastdb_download.py")

import importlib.util as _ilu            # noqa: E402  (to read DEFAULTS)
_spec = _ilu.spec_from_file_location("blastdb_download", SCRIPT)
module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(module)
MANIFEST = "blastdb-metadata-1-1.json"
PAYLOAD = 200_000          # big enough to be split into several chunks


# --------------------------------------------------------------------------- #
# fake NCBI payloads (real .nin header layout, see parse_volume_marker)
# --------------------------------------------------------------------------- #
def nin_bytes(dbname, ordinal, date_str, kind="nucl"):
    title = f"Fake {dbname} database".encode()
    blob = (f"{dbname}.ndb" if kind == "nucl" else f"{dbname}.pdb").encode()
    out = struct.pack(">IIII", 5, 0 if kind == "nucl" else 1, ordinal,
                      len(title) + 1) + title + b"\0"
    out += struct.pack(">I", len(blob)) + blob + b"\0"
    out += struct.pack(">I", len(date_str) + 1) + date_str.encode() + b"\0"
    return out + b"\0" * 16


def filler(seed, size):
    """Deterministic, gzip-incompressible bytes."""
    out = bytearray()
    block = hashlib.sha256(seed).digest()
    while len(out) < size:
        block = hashlib.sha256(block).digest()
        out += block
    return bytes(out[:size])


def nsq_bytes(idx, tag, size):
    return b"seq-" + tag + filler(bytes([idx]) + tag, size)


def tar_gz(members):
    """Deterministic gzipped tar.

    gzip normally stamps the current time into its header, which would make two
    builds of the "same" revision differ byte for byte (and any test that
    compares archives flaky across a second boundary), so mtime is pinned.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tf:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 1700000000
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def iso_of(date_str):
    """'Jul 01, 2026  1:00 AM' -> '2026-07-01T01:00:00'."""
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S",
                       _t.strptime(" ".join(date_str.split()),
                                   "%b %d, %Y %I:%M %p"))


def make_revision(dates=("Jul 01, 2026  1:00 AM",), kind="nucl",
                  tag=b"a", size=PAYLOAD, extra_manifest_files=(),
                  dbname="testdb", meta_last_updated=None):
    """Build one fake revision of a database (default `testdb`).

    Mirrors the real layout: a manifest entry pointing at archives, plus the
    `<db>-<type>-metadata.json` payload metadata that the mirror serves beside
    them (it is not listed in the manifest, it is found by convention).
    """
    files = {}
    payload = {}
    multi = len(dates) > 1
    for idx, date in enumerate(dates):
        base = f"{dbname}.{idx:02d}" if multi else dbname
        members = {
            f"{base}.nin": nin_bytes(dbname, idx, date, kind),
            f"{base}.nsq": nsq_bytes(idx, tag, size),
        }
        if idx == 0:
            # the logical blob the volume indexes refer to, exactly as NCBI
            # ships it inside the lowest numbered archive
            members[f"{dbname}.ndb"] = b"blob-" + tag + bytes(64)
        files[f"{base}.tar.gz"] = tar_gz(members)
        payload.update(members)
    meta = {
        "dbname": dbname, "version": "1.1",
        "dbtype": "Nucleotide" if kind == "nucl" else "Protein",
        "description": f"fake {dbname}",
        "number-of-letters": 1000, "number-of-sequences": 10,
        "files": sorted(payload),
        "last-updated": meta_last_updated or iso_of(dates[0]),
        "bytes-total": sum(len(v) for v in payload.values()),
        "bytes-to-cache": 0,
        "number-of-volumes": len(dates),
    }
    return {"files": files, "kind": kind, "dates": list(dates),
            "last_updated": iso_of(dates[0]),
            "extra_manifest_files": list(extra_manifest_files),
            "extra_objects": {
                f"{dbname}-{'nucl' if kind == 'nucl' else 'prot'}-metadata.json":
                    json.dumps(meta, indent=2).encode()}}


# --------------------------------------------------------------------------- #
# fake source
# --------------------------------------------------------------------------- #
class FakeNcbi:
    """Holds several revisions per database and which one is 'published'."""

    def __init__(self):
        self.dbs = {}                 # dbname -> {rev name: revision}
        self.published = {}           # dbname -> rev name
        self.manifest_fetches = 0
        self.flip_on_fetch = None     # (dbname, rev name) to publish once
        self.flip_cycle = None        # [(dbname, rev), ...] alternating
        self.manifest_host = None     # None -> this fake server; else a fake
                                      # official host such as ftp.ncbi.nlm.nih.gov
        self.requests = []            # every object name actually requested
        self.served = {}              # object name -> bytes actually written
        self.stall_after = None       # write this many bytes, then stall
        self.stall_only_request = None  # None = every response
        self.payload_responses = 0
        self.stall_seconds = 20.0
        self.fail_payloads = None     # e.g. 404 to simulate a refusing server
        self.fail_names = set()       # refuse exactly these objects
        self.fail_times = {}          # name -> remaining refusals (transient)
        self.fail_code = 404          # code used for fail_names / fail_times
        self.lock = threading.Lock()
        self.host = "127.0.0.1"

    def add(self, dbname, revision, rev="a", publish=True):
        self.dbs.setdefault(dbname, {})[rev] = revision
        if publish or dbname not in self.published:
            self.published[dbname] = rev

    def publish(self, dbname, rev):
        self.published[dbname] = rev

    def current(self, dbname):
        return self.dbs[dbname][self.published[dbname]]

    def manifest_bytes(self):
        out = []
        for dbname in sorted(self.dbs):
            rev = self.current(dbname)
            listed = list(rev["files"])
            listed += [n for n in rev["extra_manifest_files"] if n not in listed]
            host = self.manifest_host or self.host
            scheme = "ftp" if host.startswith("ftp.") else "http"
            out.append({
                "dbname": dbname,
                "version": "1.1",
                "dbtype": "Nucleotide" if rev["kind"] == "nucl" else "Protein",
                "description": f"fake {dbname}",
                "number-of-letters": 1000,
                "number-of-sequences": 10,
                "files": [f"{scheme}://{host}/blast/db/{n}" for n in listed],
                "last-updated": rev["last_updated"],
                "bytes-total": sum(len(v) for v in rev["files"].values()),
                "bytes-to-cache": 0,
                "number-of-volumes": len(rev.get("dates", ())) or 1,
            })
        return json.dumps(out).encode()

    def lookup(self, name):
        for dbname in sorted(self.dbs):
            rev = self.current(dbname)
            if name in rev["files"]:
                return rev["files"][name]
            if name in rev.get("extra_objects", {}):
                return rev["extra_objects"][name]
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeNcbi/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/octet-stream", extra=(),
              name=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        fake = self.server.fake
        # small pieces so an interrupted transfer leaves a partial file and the
        # test can stop the client at a deterministic byte offset
        stall = False
        limit = None
        if name and self.command == "GET":
            with fake.lock:
                fake.payload_responses += 1
                nth = fake.payload_responses
                limit = fake.stall_after
            stall = bool(limit) and (fake.stall_only_request is None
                                     or nth == fake.stall_only_request)
        piece, sent = 16384, 0
        for off in range(0, max(1, len(body)), piece):
            chunk = body[off:off + piece]
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            sent += len(chunk)
            if name:
                with fake.lock:
                    fake.served[name] = fake.served.get(name, 0) + len(chunk)
            if stall and sent >= limit:
                time.sleep(fake.stall_seconds)

    def do_HEAD(self):
        self._serve()

    def do_GET(self):
        self._serve()

    def _serve(self):
        fake = self.server.fake
        name = self.path.split("?")[0].rsplit("/", 1)[-1]
        with fake.lock:
            fake.requests.append(name)
            if name == MANIFEST:
                fake.manifest_fetches += 1
                if fake.manifest_fetches >= 2:
                    if fake.flip_cycle:
                        dbname, rev = fake.flip_cycle[
                            (fake.manifest_fetches - 2) % len(fake.flip_cycle)]
                        fake.published[dbname] = rev
                    elif fake.flip_on_fetch:
                        dbname, rev = fake.flip_on_fetch
                        fake.published[dbname] = rev
                        fake.flip_on_fetch = None
                return self._send(200, fake.manifest_bytes(),
                                  "application/json")
            if name.endswith(".md5"):
                data = fake.lookup(name[:-4])
                if data is None:
                    return self._send(404, b"no sidecar")
                digest = hashlib.md5(data).hexdigest()
                return self._send(200, f"{digest}  {name[:-4]}\n".encode(),
                                  "text/plain")
            data = fake.lookup(name)
        refused = fake.fail_payloads and not name.endswith(".md5")
        if (self.command == "GET" and name in fake.fail_names
                and not name.endswith(".md5")):
            # only GETs are refused: a HEAD must still answer, exactly like a
            # server that lists an object it then refuses to serve
            refused = True
        if (self.command == "GET" and name in fake.fail_times
                and not name.endswith(".md5")):
            with fake.lock:
                if fake.fail_times.get(name, 0) > 0:
                    fake.fail_times[name] -= 1
                    refused = True
        if data is None or refused:
            return self._send(fake.fail_payloads or fake.fail_code,
                              b"not found")
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d*)$", rng or "")
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            chunk = data[start:end + 1]
            return self._send(206, chunk,
                              extra=[("Content-Range",
                                      f"bytes {start}-{end}/{len(data)}")],
                              name=name)
        return self._send(200, data, name=name)


class Server:
    def __init__(self):
        self.fake = FakeNcbi()
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self.fake
        self.fake.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://{self.fake.host}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
# base test case
# --------------------------------------------------------------------------- #
class Case(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.fake = self.server.fake
        self.root = tempfile.mkdtemp(prefix="blastdb-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self.server.stop)

    def cli_args(self):
        """Base command line; subclasses append flags via extra_cli_args()."""
        return [sys.executable, SCRIPT, "-r", self.root, "-s", "ncbi",
                "--ncbi-base", self.server.base, "--no-taxdb",
                "--smoke-test", "never", "--aria2c", "none",
                "--no-log-file"] + \
            self.extra_cli_args()

    def extra_cli_args(self):
        return []

    def run_cli(self, *args, expect=None, env=None):
        cmd = self.cli_args() + list(args)
        env = {**os.environ, **(env or {})}
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                              env=env, check=False)
        if expect is not None and proc.returncode != expect:
            self.fail(f"exit {proc.returncode} != {expect}\n"
                      f"cmd: {' '.join(cmd)}\n"
                      f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
        return proc

    def run_cli_async(self, *args, env=None):
        cmd = self.cli_args() + list(args)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**os.environ, **(env or {})})

    def cleanup_child(self, child):
        """Kill an async CLI run and close its pipes when the test ends."""
        def _c(p):
            if p.poll() is None:
                try:
                    p.kill()
                    p.wait(timeout=30)
                except Exception:
                    pass
            for stream in (p.stdout, p.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
        self.addCleanup(_c, child)
        return child

    def wait_for(self, predicate, timeout=20.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")
        return False

    def staged(self, name):
        """Path of a file inside the revision staging directory, or None."""
        for root, _dirs, files in os.walk(os.path.join(self.root, ".staging")):
            if name in files:
                return os.path.join(root, name)
        return None

    def current(self):
        link = os.path.join(self.root, "current")
        self.assertTrue(os.path.islink(link), "current must be a symlink")
        return os.readlink(link)

    def snapshots(self):
        return sorted(d for d in os.listdir(self.root)
                      if d != "current" and not d.startswith(".")
                      and os.path.isdir(os.path.join(self.root, d)))

    def state(self, snap=None):
        snap = snap or self.current()
        with open(os.path.join(self.root, snap, ".blastdb-download.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)


# --------------------------------------------------------------------------- #
class TestHappyPath(Case):
    def test_install_verify_and_reuse(self):
        self.fake.add("testdb", make_revision())
        out = self.run_cli("-v", "download", "testdb", expect=0)
        # the archive plus the payload metadata json
        self.assertIn("fetching 2 file(s)", out.stderr)
        self.assertIn("with the built-in downloader", out.stderr)
        files = os.listdir(os.path.join(self.root, self.current()))
        for name in ("testdb.nin", "testdb.nsq", ".blastdb-download.json"):
            self.assertIn(name, files)

        self.run_cli("verify", expect=0)

        again = self.run_cli("-v", "download", "testdb", expect=0)
        self.assertNotIn("fetching ", again.stderr)
        self.assertIn("already installed", again.stderr)

    def test_multichunk_download(self):
        self.fake.add("testdb", make_revision())
        self.run_cli("-q", "-x", "4", "-k", "1K", "download", "testdb",
                     expect=0)
        self.run_cli("verify", expect=0)
        self.run_cli("-q", "-x", "4", "-k", "1K", "download", "--force",
                     "testdb", expect=0)
        self.run_cli("verify", expect=0)

    def test_second_checkout_adds_a_database_to_the_same_snapshot(self):
        self.fake.add("testdb", make_revision())
        self.fake.add("otherdb", make_revision(
            dates=("Jul 02, 2026  2:00 AM",), dbname="otherdb"))
        self.run_cli("-q", "download", "testdb", expect=0)
        self.run_cli("-q", "-v", "download", "otherdb", expect=0)
        files = os.listdir(os.path.join(self.root, self.current()))
        self.assertIn("testdb.nin", files, "the first database is carried over")
        self.assertIn("otherdb.nin", files)
        self.run_cli("verify", expect=0)

    def test_taxdb_is_added_automatically_for_every_source(self):
        self.fake.add("testdb", make_revision())
        self.fake.add("taxdb", make_revision(dbname="taxdb"))
        for source_args in ((), ("-s", "ncbi")):
            out = self.run_cli("--taxdb", *source_args, "--dry-run", "download",
                               "testdb", expect=0)
            self.assertIn("also fetching taxdb", out.stderr)
            self.assertIn("taxdb: rev", out.stdout)

    def test_no_taxdb_opts_out(self):
        self.fake.add("testdb", make_revision())
        self.fake.add("taxdb", make_revision(dbname="taxdb"))
        out = self.run_cli("--no-taxdb", "--dry-run", "download", "testdb",
                           expect=0)
        self.assertNotIn("also fetching taxdb", out.stderr)
        self.assertNotIn("taxdb: rev", out.stdout)

    def test_an_explicit_taxdb_request_is_not_duplicated(self):
        self.fake.add("testdb", make_revision())
        self.fake.add("taxdb", make_revision(dbname="taxdb"))
        out = self.run_cli("--taxdb", "--dry-run", "download", "testdb",
                           "taxdb", expect=0)
        self.assertEqual(out.stdout.count("taxdb: rev"), 1)

    def test_unknown_database_is_rejected(self):
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("download", "nosuchdb", expect=1)
        self.assertIn("unknown database", proc.stderr)


class TestTornUpdate(Case):
    """The core guarantee: NCBI publishing a new release mid-download."""

    def setUp(self):
        super().setUp()
        self.fake.add("testdb", make_revision(tag=b"a"), rev="a")
        self.fake.add("testdb", make_revision(
            dates=("Jul 09, 2026  9:00 AM",), tag=b"b"), rev="b",
            publish=False)

    def test_new_revision_mid_download_aborts_cleanly(self):
        self.fake.flip_on_fetch = ("testdb", "b")
        proc = self.run_cli("--on-torn", "fail", "-v", "download", "testdb",
                            expect=4)
        self.assertIn("new revision", proc.stderr)
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")),
                         "nothing may be installed after a torn update")
        self.assertEqual(self.snapshots(), [])

    def test_retry_converges_on_the_new_revision(self):
        self.fake.flip_on_fetch = ("testdb", "b")
        proc = self.run_cli("--on-torn", "retry", "--torn-retries", "3",
                            "--torn-wait", "0", "-v", "download", "testdb",
                            expect=0)
        self.assertIn("retrying", proc.stderr)
        self.assertEqual(self.fake.published["testdb"], "b")
        # the installed content must be revision B, not a mixture
        st = self.state()
        # one archive plus the nin/nsq/ndb it unpacks to
        self.assertEqual(len(st["dbs"]["testdb"]["files"]), 4)
        self.run_cli("verify", expect=0)

    def test_a_checksumless_file_is_never_salvaged_across_revisions(self):
        """The per-database metadata JSON has no published md5.

        Two revisions of it can have the same length, so "same size" is not a
        proof of identity: reusing the old one poisons the set-level check and
        makes an otherwise healthy retry fail.  It is about 500 bytes, so it is
        fetched again instead.
        """
        self.fake.add("testdb", make_revision(tag=b"a"), rev="a")
        self.fake.add("testdb", make_revision(
            dates=("Jul 09, 2026  9:00 AM",), tag=b"b"), rev="b",
            publish=False)
        self.fake.flip_on_fetch = ("testdb", "b")

        proc = self.run_cli("--on-torn", "retry", "--torn-retries", "3",
                            "--torn-wait", "0", "-v", "download", "testdb",
                            expect=0)
        self.assertIn("retrying in", proc.stderr)
        self.assertNotIn("salvaged", proc.stderr,
                         "a file without a checksum must not be salvaged: an "
                         "equal size is not proof of identity")
        self.run_cli("verify", expect=0)

    def test_retries_exhausted_reports_torn(self):
        # the source never settles: every manifest read publishes the other
        # revision, so no attempt can ever be proven consistent
        self.fake.flip_cycle = [("testdb", "b"), ("testdb", "a")]
        proc = self.run_cli("--on-torn", "retry", "--torn-retries", "1",
                            "--torn-wait", "0", "download", "testdb",
                            expect=4)
        self.assertIn("Aborted", proc.stderr)
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))


class TestSetLevelChecks(Case):
    def test_mixed_build_timestamps_refused(self):
        self.fake.add("testdb", make_revision(
            dates=("Jul 01, 2026  1:00 AM", "Jul 05, 2026  1:00 AM")))
        proc = self.run_cli("download", "testdb", expect=3)
        self.assertIn("MIXED BUILD TIMESTAMPS", proc.stderr)
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))
        self.assertEqual(self.snapshots(), [])

    def test_volume_gap_refused(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 3)
        rev["files"] = {k: v for k, v in rev["files"].items()
                        if not k.startswith("testdb.01.")}
        self.fake.add("testdb", rev)
        proc = self.run_cli("download", "testdb", expect=3)
        self.assertIn("not contiguous", proc.stderr)

    def test_payload_metadata_disagreeing_with_the_index_is_refused(self):
        # a single volume database has nothing to compare volumes against, but
        # its own metadata still states which build it belongs to
        self.fake.add("testdb", make_revision(
            dates=("Jul 01, 2026  1:00 AM",),
            meta_last_updated="2026-07-05T00:00:00"))
        proc = self.run_cli("download", "testdb", expect=3)
        self.assertIn("different builds", proc.stderr)
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))
        self.assertEqual(self.snapshots(), [])

    def test_manifest_listing_a_file_the_source_cannot_serve_is_torn(self):
        rev = make_revision()
        rev["extra_manifest_files"] = ["testdb.99.tar.gz"]
        self.fake.add("testdb", rev)
        proc = self.run_cli("--on-torn", "fail", "download", "testdb",
                            expect=4)
        self.assertIn("md5 sidecar", proc.stderr)
        self.assertEqual(self.snapshots(), [])


class TestIntegrityAndRepair(Case):
    def extra_cli_args(self):
        # these tests are about individual payload files, so keep the set small
        return ["--no-metadata-json"]

    def setUp(self):
        super().setUp()
        self.fake.add("testdb", make_revision())
        self.run_cli("-q", "download", "testdb", expect=0)

    def damage(self, offset):
        path = os.path.join(self.root, self.current(), "testdb.nsq")
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(b"DAMAGED!")

    def test_probe_verify_catches_damage_and_repair_fixes_it(self):
        self.damage(16)                       # inside the head probe
        bad = self.run_cli("verify", "--quick", expect=3)
        self.assertIn("probe mismatch", bad.stderr)
        proc = self.run_cli("-v", "repair", expect=0)
        m = re.search(r"fetching (\d+) file\(s\)", proc.stderr)
        self.assertIsNotNone(m, proc.stderr)
        self.assertEqual(int(m.group(1)), 1,
                         "only the damaged volume should be fetched again")
        self.run_cli("verify", expect=0)

    def test_full_verify_catches_middle_damage(self):
        size = os.path.getsize(os.path.join(self.root, self.current(),
                                            "testdb.nsq"))
        self.damage(size // 2)
        proc = self.run_cli("verify", expect=3)
        self.assertIn("md5", proc.stderr)
        self.run_cli("repair", expect=0)
        self.run_cli("verify", expect=0)

    def test_truncated_file_is_not_carried_into_the_next_snapshot(self):
        path = os.path.join(self.root, self.current(), "testdb.nsq")
        with open(path, "r+b") as fh:
            fh.truncate(1000)
        # a new upstream revision forces a rebuild that would like to hard-link
        # the damaged file; it must be re-fetched instead
        self.fake.add("testdb", make_revision(
            dates=("Jul 09, 2026  9:00 AM",), tag=b"b"), rev="b")
        self.run_cli("-v", "download", "testdb", expect=0)
        self.run_cli("verify", expect=0)
        self.assertEqual(
            os.path.getsize(os.path.join(self.root, self.current(),
                                         "testdb.nsq")),
            len(nsq_bytes(0, b"b", PAYLOAD)))


class TestResume(Case):
    """A killed run must continue, not start over (three separate layers)."""

    def extra_cli_args(self):
        # keep the payload to one object so the byte accounting is exact
        return ["--no-metadata-json"]

    def test_file_left_in_staging_by_a_killed_run_is_reused(self):
        rev = make_revision(size=PAYLOAD)
        self.fake.add("testdb", rev)
        archive = rev["files"]["testdb.tar.gz"]

        # what revision key would the tool use?
        out = self.run_cli("--dry-run", "download", "testdb", expect=0)
        revkey = re.search(r"rev ([0-9a-f]{16})", out.stdout)
        self.assertIsNotNone(revkey, out.stdout)

        # pretend another run was killed after fetching the archive completely
        stage = os.path.join(self.root, ".staging", "testdb", revkey.group(1))
        os.makedirs(stage)
        with open(os.path.join(stage, "testdb.tar.gz"), "wb") as fh:
            fh.write(archive)

        proc = self.run_cli("-v", "download", "testdb", expect=0)
        self.assertIn("already fetched by an earlier run", proc.stderr)
        self.assertEqual(self.fake.served.get("testdb.tar.gz", 0), 0,
                         "nothing may be re-fetched")
        # ... and it must still have been unpacked and installed
        files = os.listdir(os.path.join(self.root, self.current()))
        self.assertIn("testdb.nin", files)
        self.assertIn("testdb.nsq", files)
        self.run_cli("verify", expect=0)

    def test_killed_single_stream_download_resumes_from_the_partial_file(self):
        rev = make_revision(size=400_000)
        self.fake.add("testdb", rev)
        full = len(rev["files"]["testdb.tar.gz"])
        self.fake.stall_after = 300_000          # stall once 300 KiB are out

        child = self.cleanup_child(self.run_cli_async("-x", "1", "download",
                                                     "testdb"))
        self.wait_for(lambda: self.fake.served.get("testdb.tar.gz", 0) >= 300_000,
                      what="300 KiB to be transferred")
        time.sleep(0.3)                          # let the client flush to disk
        child.kill()
        child.wait(timeout=30)
        served_first = self.fake.served["testdb.tar.gz"]
        partial = self.staged("testdb.tar.gz")
        self.assertIsNotNone(partial, "the partial file must survive the kill")
        partial_size = os.path.getsize(partial)
        self.assertGreater(partial_size, 0)
        self.assertLess(partial_size, full)

        self.fake.stall_after = None
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        self.assertIn("resuming testdb.tar.gz at", proc.stderr)
        total = self.fake.served["testdb.tar.gz"]
        self.assertLess(total, full * 1.4,
                        f"a restart would have served {served_first + full} bytes, "
                        f"resuming served {total}")
        self.run_cli("verify", expect=0)

    def test_partial_chunk_state_resumes_the_remaining_chunks(self):
        rev = make_revision(size=400_000)
        self.fake.add("testdb", rev)
        full = len(rev["files"]["testdb.tar.gz"])

        # 4 chunks of ~100 KiB; hang the second range request so that three
        # chunks complete and one stays half written
        self.fake.stall_after = 8_000
        self.fake.stall_only_request = 2
        child = self.cleanup_child(self.run_cli_async("-x", "4", "-k", "8K",
                                                     "download", "testdb"))
        self.wait_for(lambda: self.fake.payload_responses >= 4,
                      what="all four range requests to be in flight")
        self.wait_for(lambda: self.staged("testdb.tar.gz") is not None,
                      what="the staged file to appear")
        self.wait_for(lambda: self.fake.served.get("testdb.tar.gz", 0) >= full // 2,
                      what="most chunks to be transferred")
        time.sleep(0.3)
        child.kill()
        child.wait(timeout=30)
        served_first = self.fake.served["testdb.tar.gz"]

        self.fake.stall_after = None
        self.fake.stall_only_request = None
        self.run_cli("-x", "4", "-k", "8K", "download", "testdb", expect=0)
        self.run_cli("verify", expect=0)
        self.assertLess(self.fake.served["testdb.tar.gz"], served_first + full * 0.9,
                        "finished chunks must not be fetched a second time")
        self.assertEqual(
            os.path.getsize(os.path.join(self.root, self.current(),
                                         "testdb.nsq")),
            len(nsq_bytes(0, b"a", 400_000)))

    def test_interrupt_does_not_drain_the_queued_chunks(self):
        """Ctrl-C must stop promptly, install nothing, and exit 130.

        60 pieces are queued and every request stalls.  What makes this fast is
        the combination of the stop flag (in-flight pieces abort instead of
        waiting for their socket timeout) and the interpreter discarding queued
        work at exit; `cancel_futures=True` makes that explicit rather than
        relying on teardown.  This test pins the observable behaviour; the
        mechanism itself is covered by the unit test below.
        """
        self.fake.add("testdb", make_revision(size=400_000))
        self.fake.stall_after = 1_000
        self.fake.stall_seconds = 30
        child = self.cleanup_child(self.run_cli_async(
            "--timeout", "2", "--tries", "1", "-x", "60", "-k", "1K",
            "download", "testdb"))
        self.wait_for(lambda: self.fake.payload_responses >= 8,
                      what="the queue to start")
        started = time.time()
        child.send_signal(signal.SIGINT)
        _out, err = child.communicate(timeout=90)
        elapsed = time.time() - started
        self.assertEqual(child.returncode, 130, err)
        self.assertLess(elapsed, 12.0,
                        f"interrupt took {elapsed:.1f}s; the queued chunks were "
                        f"probably drained instead of cancelled")
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))
        self.fake.stall_after = None
        self.fake.stall_seconds = 20.0

    def test_a_set_stop_flag_aborts_a_chunk_before_any_network_use(self):
        """The abort path must not wait for a socket timeout to be noticed."""
        stop = threading.Event()
        stop.set()
        backend = module.BuiltinBackend(http=object(), progress=object(),
                                       tries=1, stop_event=stop)
        plan = module.FilePlan("x", "http://invalid.invalid/x", "/tmp/x",
                               None, None, "none")
        with self.assertRaises(module.AbortedTransfer):
            backend._chunk(plan, 0, 0, 10)

    def test_leftover_build_directory_is_swept(self):
        self.fake.add("testdb", make_revision())
        dead = os.path.join(self.root, ".ncbi-2026-07-01-abc.tmp999999")
        os.makedirs(dead)
        live = os.path.join(self.root, f".ncbi-2026-07-01-abc.tmp{os.getpid()}")
        os.makedirs(live)
        self.run_cli("-v", "download", "testdb", expect=0)
        self.assertFalse(os.path.exists(dead), "a dead run's build dir is removed")
        self.assertTrue(os.path.exists(live), "a live run's build dir is kept")
        shutil.rmtree(live, ignore_errors=True)

    def test_interrupt_message_points_at_the_staging_directory(self):
        self.fake.add("testdb", make_revision(size=400_000))
        self.fake.stall_after = 8_000             # keep the transfer open
        child = self.cleanup_child(self.run_cli_async(
            "--timeout", "2", "--tries", "1", "-x", "1", "download", "testdb"))
        self.wait_for(lambda: self.staged("testdb.tar.gz") is not None,
                      what="the staged file to appear")
        child.send_signal(signal.SIGINT)
        _out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 130, err)
        self.assertIn("re-run the same command to resume", err)


class TestFailureReporting(Case):
    """The operator must be able to tell *why* a big batch failed."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def test_reasons_are_grouped_with_a_next_step(self):
        # three volumes, every payload refused by the source
        self.fake.add("testdb", make_revision(dates=("Jul 01, 2026  1:00 AM",) * 3))
        self.fake.fail_payloads = 404
        child = self.cleanup_child(
            self.run_cli_async("--tries", "1", "download", "testdb"))
        proc = child
        _out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("3/3 file(s) failed to download", err)
        self.assertIn("HTTP 404", err)
        self.assertIn("3 x HTTP 404", err)          # grouped, not one line each
        self.assertIn("e.g. testdb.00.tar.gz", err)
        self.assertIn("nothing was installed", err)
        self.assertIn("re-run the same command", err)
        self.assertIn("space on", err)          # "usable space", quota aware
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))
        self.assertEqual(self.snapshots(), [])

    def test_space_floor_aborts_a_running_transfer(self):
        self.fake.add("testdb", make_revision(size=PAYLOAD))
        self.fake.stall_after = 8_000            # keep the transfer open
        child = self.cleanup_child(self.run_cli_async(
            "--no-disk-check", "--min-free", "1T", "--timeout", "3",
            "--tries", "1", "-x", "1", "download", "testdb"))
        _out, err = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 1, err)
        self.assertIn("below the", err)
        self.assertIn("floor", err)
        self.assertIn("not fetched again", err)
        self.assertFalse(os.path.islink(os.path.join(self.root, "current")))

    def test_partial_files_survive_a_failed_batch(self):
        self.fake.add("testdb", make_revision(size=PAYLOAD))
        self.fake.fail_payloads = 500
        self.run_cli("--tries", "1", "download", "testdb", expect=1)
        # nothing to resume yet, but the staging directory must not be emptied
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".staging")))


class TestRetryPolicy(Case):
    """Per-file failures are retried when retrying can help, and not when it cannot."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def test_transient_failure_is_retried_and_succeeds(self):
        self.fake.add("testdb", make_revision(size=8000))
        # the built-in backend retries a chunk twice on its own, so three
        # refusals are needed before the batch level retry is what saves it
        self.fake.fail_times = {"testdb.tar.gz": 3}
        self.fake.fail_code = 403          # retryable, e.g. rate limiting
        proc = self.run_cli("--tries", "1", "--file-retries", "3", "-v",
                            "download", "testdb", expect=0)
        self.assertIn("retrying in", proc.stderr)
        self.assertIn("attempt 1/3", proc.stderr)
        self.run_cli("verify", expect=0)

    def test_permanent_failure_is_not_retried(self):
        self.fake.add("testdb", make_revision(size=8000))
        self.fake.fail_names = {"testdb.tar.gz"}
        proc = self.run_cli("--tries", "1", "--file-retries", "5", "-v",
                            "download", "testdb", expect=1)
        self.assertIn("retrying cannot fix", proc.stderr)
        self.assertNotIn("retrying in", proc.stderr,
                         "404s must fail fast, not burn 5 attempts")
        self.assertEqual(self.snapshots(), [])

    def test_retries_exhausted_report_the_attempts(self):
        self.fake.add("testdb", make_revision(size=8000))
        self.fake.fail_times = {"testdb.tar.gz": 99}
        self.fake.fail_code = 403          # retryable, so the rounds do happen
        proc = self.run_cli("--tries", "1", "--file-retries", "2",
                            "download", "testdb", expect=1)
        self.assertIn("still failed after 2 attempt(s)", proc.stderr)

    def test_failure_policy_classification(self):
        for reason, expected in (
                ("no space left on the filesystem", "fatal"),
                ("filesystem quota exceeded", "fatal"),
                ("permission denied", "fatal"),
                ("HTTP 404 not found (source may be mid-publication)", "fatal"),
                ("HTTP 403 forbidden (often rate limiting)", "retry"),
                ("connection reset by the server", "retry"),
                ("timeout", "retry"),
                ("checksum mismatch", "retry")):
            self.assertEqual(module.failure_policy(reason), expected, reason)


class TestStagingSalvage(Case):
    """Bytes already in staging are reused even when the revkey changed."""

    def test_matching_files_are_salvaged_across_a_changed_file_set(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 3, size=8000)
        self.fake.add("testdb", rev)
        names = sorted(rev["files"])

        # run 1: without the payload metadata, and the source refuses the last
        # archive, so two archives stay in staging and nothing is installed
        self.fake.fail_names = {names[-1]}
        self.run_cli("--no-metadata-json", "--tries", "1", "download",
                     "testdb", expect=1)
        self.assertEqual(self.snapshots(), [])
        self.assertIsNotNone(self.staged(names[0]))

        # run 2: the file set changes (metadata json included) so the revision
        # key differs, but the two staged archives are still exactly right and
        # must not be downloaded again
        self.fake.fail_names = set()
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertIn("salvaged 2 file(s)", proc.stderr)
        for name in names[:2]:
            self.assertEqual(served.get(name, 0), 0,
                             f"{name} must not be fetched again")
        self.assertGreater(served.get(names[-1], 0), 0)
        self.run_cli("verify", expect=0)
        # a successful install clears staging, so the stale tree cannot be
        # picked up by mistake later
        base = os.path.join(self.root, ".staging", "testdb")
        self.assertFalse(os.path.exists(base),
                         "staging is cleared once the snapshot is installed")


class TestCloudRecheckIsCheap(Case):
    """Re-verifying an immutable cloud snapshot must not re-list 10k objects."""

    class StubHttp:
        def __init__(self, manifest, listing):
            self.manifest_json = manifest
            self.listing_payload = listing
            self.calls = []
            self.requests = []

        def get_text(self, url, **kw):
            self.requests.append(url)
            if url.endswith("latest-dir"):
                return "2026-07-21-01-05-02"
            raise AssertionError(f"unexpected GET {url}")

        def get_json(self, url, **kw):
            self.requests.append(url)
            self.calls.append("list")
            prefix = urllib.parse.unquote(
                urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                .get("prefix", [""])[0])
            return {"items": [i for i in self.listing_payload
                              if i["name"].startswith(prefix)]}

        def call(self, url, headers=None, method=None, data=None, ok=(200,),
                 allow=(404,), attempts=None):
            self.requests.append(url)
            if url.endswith("blastdb-metadata-1-1.json"):
                return 200, {}, json.dumps(self.manifest_json).encode()
            raise AssertionError(f"unexpected call {url}")

        def exists(self, url):
            return False

    def test_files_outside_the_database_prefix_are_found(self):
        """taxdb ships taxonomy4blast.sqlite3, which the fast path cannot see."""
        manifest = [{"dbname": "taxdb", "version": "1.1",
                     "dbtype": "Nucleotide", "description": "taxonomy",
                     "number-of-letters": 1, "number-of-sequences": 1,
                     "files": [f"gs://blast-db/2026-07-21-01-05-02/{n}"
                               for n in ("taxdb.btd", "taxdb.bti",
                                         "taxonomy4blast.sqlite3")],
                     "last-updated": "2026-07-21T00:00:00", "bytes-total": 30,
                     "number-of-volumes": 1, "bytes-total-compressed": 30}]
        listing = [{"name": f"2026-07-21-01-05-02/{n}", "size": "10",
                    "md5Hash": base64.b64encode(b"y" * 16).decode(),
                    "generation": "1"}
                   for n in ("taxdb.btd", "taxdb.bti",
                             "taxonomy4blast.sqlite3")]
        stub = self.StubHttp(manifest, listing)
        ctx = module.Context(dict(module.DEFAULTS), argparse.Namespace(
            command="download", databases=["taxdb"]))
        ctx.http = stub
        src = module.GcpSource(ctx)
        src.resolve()
        target = src.revision("taxdb")
        self.assertEqual(sorted(f.name for f in target.files),
                         ["taxdb.btd", "taxdb.bti", "taxonomy4blast.sqlite3"])
        self.assertTrue(all(f.md5 for f in target.files))
        self.assertTrue(any("taxonomy4blast" in u for u in stub.requests),
                        "the file was never looked up by name")

    def test_fresh_revision_of_an_immutable_snapshot_does_not_re_list(self):
        manifest = [{"dbname": "testdb", "version": "1.1",
                     "dbtype": "Nucleotide", "description": "d",
                     "number-of-letters": 1, "number-of-sequences": 1,
                     "files": ["gs://blast-db/2026-07-21-01-05-02/testdb.nin"],
                     "last-updated": "2026-07-21T00:00:00", "bytes-total": 10,
                     "number-of-volumes": 1, "bytes-total-compressed": 10}]
        listing = [{"name": "2026-07-21-01-05-02/testdb.nin", "size": "10",
                    "md5Hash": base64.b64encode(b"x" * 16).decode(),
                    "generation": "1"}]
        stub = self.StubHttp(manifest, listing)
        ctx = module.Context(dict(module.DEFAULTS), argparse.Namespace(
            command="download", databases=["testdb"]))
        ctx.http = stub
        src = module.GcpSource(ctx)
        src.resolve()
        first = src.revision("testdb")
        calls_after_first = list(stub.calls)
        self.assertIn("list", calls_after_first)
        again = src.revision("testdb", fresh=True)
        self.assertEqual(stub.calls, calls_after_first,
                         "the fresh re-check re-listed objects")
        self.assertEqual(first.revkey(), again.revkey())
        self.assertTrue(any("latest-dir" in u for u in stub.requests))


class TestAwsListing(Case):
    """The S3 XML listing path (rewritten to be prefix scoped)."""

    XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>ncbi-blast-databases</Name>
  <Prefix>{prefix}</Prefix>
  <IsTruncated>false</IsTruncated>
  <Contents><Key>2026-07-21-01-05-02/testdb.nin</Key>
    <LastModified>2026-07-23T08:40:55.000Z</LastModified>
    <ETag>&quot;d41d8cd98f00b204e9800998ecf8427e&quot;</ETag>
    <Size>123</Size></Contents>
  <Contents><Key>2026-07-21-01-05-02/testdb.nsq</Key>
    <LastModified>2026-07-23T08:40:55.000Z</LastModified>
    <ETag>&quot;abc-358&quot;</ETag>
    <Size>456</Size></Contents>
</ListBucketResult>"""

    class StubHttp:
        def __init__(self):
            self.requests = []

        def get_text(self, url, **kw):
            if url.endswith("latest-dir"):
                return "2026-07-21-01-05-02"
            raise AssertionError(url)

        def call(self, url, headers=None, method=None, data=None, ok=(200,),
                 allow=(404,), attempts=None):
            self.requests.append(url)
            prefix = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query).get("prefix", [""])[0]
            body = TestAwsListing.XML.replace("{prefix}", prefix)
            return 200, {}, body.encode()

        def exists(self, url):
            return False

    def test_prefix_scoped_listing_and_etag_handling(self):
        stub = self.StubHttp()
        ctx = module.Context(dict(module.DEFAULTS), argparse.Namespace(
            command="showall", databases=[]))
        ctx.http = stub
        src = module.AwsSource(ctx)
        src.resolve()
        listing = src.listing_for([f"{src.snapshot}/testdb."])
        self.assertEqual(sorted(listing), ["testdb.nin", "testdb.nsq"])
        self.assertEqual(listing["testdb.nin"]["size"], 123)
        # a plain 32 hex ETag is a real md5, a multipart one is not
        self.assertEqual(listing["testdb.nin"]["md5"],
                         "d41d8cd98f00b204e9800998ecf8427e")
        self.assertEqual(listing["testdb.nin"]["md5_origin"], "s3-etag")
        self.assertIsNone(listing["testdb.nsq"]["md5"],
                          "a multipart ETag must never be treated as md5")
        self.assertEqual(listing["testdb.nsq"]["token"], "abc-358")


class TestAdoptExisting(Case):
    """Continue a download that another tool (the old aria2c loop) started."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def old_dir(self, name="old"):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_complete_archives_are_adopted_instead_of_downloaded(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 3, size=8000)
        self.fake.add("testdb", rev)
        names = sorted(rev["files"])
        old = self.old_dir()
        # the old loop finished two volumes and deleted nothing yet
        for name in names[:2]:
            with open(os.path.join(old, name), "wb") as fh:
                fh.write(rev["files"][name])
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "--adopt", old, "download", "testdb", expect=0)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertIn("adopted 2 complete file(s)", proc.stderr)
        for name in names[:2]:
            self.assertEqual(served.get(name, 0), 0, f"{name} was re-downloaded")
        self.assertGreater(served.get(names[2], 0), 0)
        self.run_cli("verify", expect=0)

    def test_a_corrupt_adopted_file_is_not_used(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",), size=8000)
        self.fake.add("testdb", rev)
        old = self.old_dir()
        with open(os.path.join(old, "testdb.tar.gz"), "wb") as fh:
            fh.write(b"garbage that does not match the published md5")
        proc = self.run_cli("-v", "--adopt", old, "download", "testdb", expect=0)
        self.assertNotIn("adopted", proc.stderr)
        self.run_cli("verify", expect=0)

    def test_half_finished_file_is_resumed_not_restarted(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",), size=400_000)
        self.fake.add("testdb", rev)
        payload = rev["files"]["testdb.tar.gz"]
        old = self.old_dir()
        with open(os.path.join(old, "testdb.tar.gz"), "wb") as fh:
            fh.write(payload[:300_000])          # sequential prefix
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "--adopt", old, "--adopt-partial",
                            "download", "testdb", expect=0)
        self.assertIn("resuming testdb.tar.gz from", proc.stderr)
        served = self.fake.served.get("testdb.tar.gz", 0) - before.get(
            "testdb.tar.gz", 0)
        self.assertLess(served, len(payload) * 0.5,
                        "most of the file should have been kept")
        self.assertGreater(served, 0)
        self.run_cli("verify", expect=0)

    def test_a_split_partial_without_aria2_is_refused(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",), size=400_000)
        self.fake.add("testdb", rev)
        payload = rev["files"]["testdb.tar.gz"]
        old = self.old_dir()
        path = os.path.join(old, "testdb.tar.gz")
        with open(path, "wb") as fh:              # sparse, like aria2 -x 4
            fh.write(payload[:100_000])
            fh.seek(len(payload) - 100_000)
            fh.write(payload[-100_000:])
        proc = self.run_cli("-v", "--adopt", old, "--adopt-partial",
                            "--aria2c", "none", "download", "testdb", expect=0)
        self.assertNotIn("resuming", proc.stderr)
        self.run_cli("verify", expect=0)

    def test_a_complete_extracted_database_is_adopted(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 2, size=8000)
        self.fake.add("testdb", rev)
        # lay out what the old loop leaves behind: extracted members, no archives
        old = self.old_dir()
        with tarfile.open(fileobj=io.BytesIO(rev["files"]["testdb.00.tar.gz"])) as tf:
            for member in tf:
                if member.isfile():
                    with open(os.path.join(old, member.name), "wb") as fh:
                        fh.write(tf.extractfile(member).read())
        with tarfile.open(fileobj=io.BytesIO(rev["files"]["testdb.01.tar.gz"])) as tf:
            for member in tf:
                if member.isfile():
                    with open(os.path.join(old, member.name), "wb") as fh:
                        fh.write(tf.extractfile(member).read())
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "--adopt", old, "--adopt-extracted",
                            "download", "testdb", expect=0)
        self.assertIn("already extracted file(s)", proc.stderr)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertEqual(served, {}, f"nothing may be downloaded: {served}")
        files = os.listdir(os.path.join(self.root, self.current()))
        self.assertIn("testdb.00.nin", files)
        self.assertIn("testdb.01.nsq", files)
        self.run_cli("verify", expect=0)

    def test_a_torn_extracted_set_is_refused(self):
        first = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 2, size=8000)
        second = make_revision(dates=("Jul 09, 2026  9:00 AM",) * 2, size=8000)
        self.fake.add("testdb", second)          # the source publishes the new one
        old = self.old_dir()
        # the old loop mixed releases: volume 0 old, volume 1 new
        for name in ("testdb.00.tar.gz",):
            with tarfile.open(fileobj=io.BytesIO(first["files"][name])) as tf:
                for member in tf:
                    if member.isfile():
                        with open(os.path.join(old, member.name), "wb") as fh:
                            fh.write(tf.extractfile(member).read())
        for name in ("testdb.01.tar.gz",):
            with tarfile.open(fileobj=io.BytesIO(second["files"][name])) as tf:
                for member in tf:
                    if member.isfile():
                        with open(os.path.join(old, member.name), "wb") as fh:
                            fh.write(tf.extractfile(member).read())
        before = dict(self.fake.served)
        # refusing the adoption is a warning: the run then just downloads the
        # real thing from the source and installs that
        proc = self.run_cli("--adopt", old, "--adopt-extracted", "-v",
                            "download", "testdb", expect=0)
        self.assertIn("not adopting the extracted payload", proc.stderr)
        self.assertIn("build date", proc.stderr)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertTrue(any(v > 0 for v in served.values()),
                        "the source must be used when adoption is refused")
        self.run_cli("verify", expect=0)
        self.run_cli("inspect", "testdb", "--dir",
                     os.path.join(self.root, self.current()), expect=0)

    def test_incomplete_extracted_set_is_refused_with_a_reason(self):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 3, size=8000)
        self.fake.add("testdb", rev)
        old = self.old_dir()
        with tarfile.open(fileobj=io.BytesIO(rev["files"]["testdb.00.tar.gz"])) as tf:
            for member in tf:
                if member.isfile():
                    with open(os.path.join(old, member.name), "wb") as fh:
                        fh.write(tf.extractfile(member).read())
        before = dict(self.fake.served)
        proc = self.run_cli("--adopt", old, "--adopt-extracted", "-v",
                            "download", "testdb", expect=0)
        self.assertIn("cover 1 volume(s) but the source has 3", proc.stderr)
        self.assertIn("missing locally", proc.stderr)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertTrue(any(v > 0 for v in served.values()))
        self.run_cli("verify", expect=0)

    def test_adopt_directory_must_exist(self):
        proc = self.run_cli("--adopt", "/nope/nothing/here", "download",
                            "testdb", expect=1)
        self.assertIn("not a directory", proc.stderr)


class TestStagingInventory(Case):
    """`staging` answers "how much of that interrupted run is still usable?"."""

    META = "testdb-nucl-metadata.json"

    def leave_a_half_finished_run(self, volumes=6, refuse=()):
        rev = make_revision(dates=("Jul 01, 2026  1:00 AM",) * volumes,
                            size=8000)
        self.fake.add("testdb", rev)
        names = sorted(rev["files"])
        self.fake.fail_names = {self.META if r == "meta" else r for r in refuse}
        self.run_cli("--tries", "1", "download", "testdb", expect=1)
        self.fake.fail_names = set()
        return rev, names

    def test_inventory_without_network(self):
        self.leave_a_half_finished_run(volumes=6, refuse=("meta",))
        proc = self.run_cli("staging", expect=0)
        self.assertIn("testdb", proc.stderr)
        self.assertIn("complete archives   : 6", proc.stderr)
        data = json.loads(self.run_cli("--json", "staging", expect=0).stdout)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["dbname"], "testdb")
        self.assertEqual(data[0]["archives"], 6)
        self.assertGreater(data[0]["bytes"], 0)

    def test_same_revision_is_reported_as_resumable_as_is(self):
        self.leave_a_half_finished_run(refuse=("meta",))
        proc = self.run_cli("staging", "--against-remote", expect=0)
        self.assertIn("resumes it as is", proc.stderr)
        data = json.loads(self.run_cli("--json", "staging", "--against-remote",
                                       expect=0).stdout)
        self.assertTrue(data[0]["resumable"])

    def test_an_older_revision_reports_how_much_is_salvageable(self):
        _rev, names = self.leave_a_half_finished_run(volumes=4, refuse=("meta",))
        # the source now publishes a bigger release; the four volumes already
        # staged are byte identical and must be reported as reusable
        bigger = make_revision(dates=("Jul 01, 2026  1:00 AM",) * 5, size=8000)
        self.fake.add("testdb", bigger, rev="b")
        self.fake.publish("testdb", "b")
        proc = self.run_cli("staging", "--against-remote", expect=0)
        self.assertIn("still match", proc.stderr)
        data = json.loads(self.run_cli("--json", "staging", "--against-remote",
                                       expect=0).stdout)
        self.assertFalse(data[0]["resumable"])
        self.assertEqual(data[0]["salvageable"], 4, json.dumps(data, indent=2))

        # and a real run then reuses exactly those four
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        self.assertIn("salvaged 4 file(s)", proc.stderr)
        for name in names:
            self.assertEqual(served.get(name, 0), 0,
                             f"{name} should have been salvaged")
        self.assertGreater(served.get("testdb.04.tar.gz", 0), 0)
        self.run_cli("verify", expect=0)

    def test_empty_staging_says_so(self):
        proc = self.run_cli("staging", expect=0)
        self.assertIn("no interrupted work", proc.stderr)




# Options of other tools that the background document quotes verbatim.
FOREIGN_TOOL_FLAGS = {"--decompress", "--num_threads", "--showall", "--passive",
                      "--legacy_exit_code", "--blastdb_version", "--force_ftp",
                      "--write-fai", "--remove", "--log-level", "--max-keys"}

ARIA2_STUB = r"""#!/usr/bin/env python3
# Minimal aria2c stand-in: understands --input-file and verifies md5s.
import hashlib
import os
import sys
import urllib.request


def parse(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    jobs, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line and not line.startswith((" ", "\t")):
            opts, j = {}, i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")):
                key, _, value = lines[j].strip().partition("=")
                opts[key] = value
                j += 1
            jobs.append((line.strip(), opts))
            i = j
        else:
            i += 1
    return jobs


def main(argv):
    inputs = next((a.split("=", 1)[1] for a in argv
                   if a.startswith("--input-file=")), None)
    logfile = next((a.split("=", 1)[1] for a in argv
                    if a.startswith("--log=")), None)

    def note(text):
        if logfile:
            with open(logfile, "a", encoding="utf-8") as fh:
                fh.write("[NOTICE] %s\n" % text)

    if not inputs:
        return 1
    jobs = parse(inputs)
    rc = 0
    for url, opts in jobs:
        dest = os.path.join(opts.get("dir", "."), opts.get("out", "out"))
        if os.environ.get("ARIA2_STUB_CORRUPT"):
            open(dest, "wb").write(b"corrupt")      # aria2 leaves it on disk
            rc = 32
            continue
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            print("[ERROR] %s URI=%s" % (exc, url), file=sys.stderr)
            rc = 1
            continue
        want = opts.get("checksum", "")
        if want.startswith("md5="):
            digest = hashlib.md5(open(dest, "rb").read()).hexdigest()
            if digest != want.split("=", 1)[1]:
                print("[ERROR] Checksum error detected. file=%s" % dest,
                      file=sys.stderr)
                rc = 32
                continue
        note("Download complete: %s" % dest)
    record = os.environ.get("ARIA2_STUB_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as fh:
            for url, opts in jobs:
                fh.write("%s\t%s\t%s\n" % (opts.get("out"),
                                             opts.get("checksum", ""),
                                             opts.get("dir")))
    return rc


sys.exit(main(sys.argv[1:]))
"""


class TestAria2Backend(Case):
    """The aria2 path had no coverage: every other test passes --aria2c none."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def stub(self):
        path = os.path.join(self.root, "aria2c-stub")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ARIA2_STUB)
        os.chmod(path, 0o755)
        return path

    def test_aria2_is_used_and_every_file_carries_its_checksum(self):
        self.fake.add("testdb", make_revision(size=8000))
        record = os.path.join(self.root, "calls.tsv")
        proc = self.run_cli("--aria2c", self.stub(), "-v", "download", "testdb",
                            expect=0, env={"ARIA2_STUB_RECORD": record})
        self.assertIn("with aria2c", proc.stderr)
        with open(record, encoding="utf-8") as fh:
            rows = [ln.split("\t") for ln in fh.read().splitlines() if ln]
        self.assertTrue(rows, "aria2c was never handed a plan")
        for name, checksum, directory in rows:
            self.assertTrue(checksum.startswith("md5="),
                            f"{name} was queued without a checksum")
            self.assertEqual(os.path.dirname(directory),
                             os.path.join(self.root, ".staging", "testdb"),
                             "aria2 must write into the revision scoped staging "
                             "directory so a resume cannot cross revisions")
        self.run_cli("verify", expect=0)

    def test_a_checksum_failure_is_reported_and_the_bad_file_removed(self):
        self.fake.add("testdb", make_revision(size=8000))
        proc = self.run_cli("--aria2c", self.stub(), "--tries", "1",
                            "--file-retries", "1", "download", "testdb",
                            expect=1, env={"ARIA2_STUB_CORRUPT": "1"})
        # size is checked first when the size is known, md5 otherwise
        self.assertRegex(proc.stderr, r"(size|md5) mismatch")
        self.assertIsNone(self.staged("testdb.tar.gz"),
                          "aria2 leaves failed files behind, so the tool must "
                          "delete them: a corrupt partial must never be "
                          "resumed into a snapshot")
        self.assertEqual(self.snapshots(), [])

    def test_aria2_completions_are_reported_one_line_per_file(self):
        self.fake.add("testdb", make_revision(size=8000))
        log = os.path.join(self.root, "run.log")
        self.run_cli("--aria2c", self.stub(), "--log-file", log,
                     "--progress-interval", "3600", "download", "testdb",
                     expect=0)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        completed = [ln for ln in text.splitlines()
                     if re.match(r".*\[1/1\] testdb\.tar\.gz", ln)]
        self.assertEqual(len(completed), 1,
                         f"expected one completion line, got:\n{text}")
        self.assertIn("KiB", completed[0])

    def test_limit_rate_is_honoured_without_aria2(self):
        """--limit-rate used to be ignored by the built-in downloader."""
        self.fake.add("testdb", make_revision(size=300_000))
        started = time.time()
        self.run_cli("--aria2c", "none", "--limit-rate", "100K", "--tries", "1",
                     "download", "testdb", expect=0)
        elapsed = time.time() - started
        # about 300 KiB at 100 KiB/s; unlimited would be a fraction of a second
        self.assertGreater(elapsed, 1.5,
                           f"the rate limit was ignored ({elapsed:.1f}s)")

    def test_aria2_can_be_forced_off(self):
        self.fake.add("testdb", make_revision(size=8000))
        proc = self.run_cli("--aria2c", "none", "-v", "download", "testdb",
                            expect=0)
        self.assertIn("with the built-in downloader", proc.stderr)

    def test_a_missing_aria2c_binary_is_a_clear_error_before_any_network(self):
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("--aria2c", "/nope/aria2c", "download", "testdb",
                            expect=1)
        self.assertIn("not an executable file", proc.stderr)
        self.assertNotIn("source:", proc.stderr,
                         "the bad tool path must be reported before the source "
                         "is even contacted")


class TestLoggingModes(Case):
    """Where output goes: file, console, or both."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def test_mutating_commands_log_to_the_mirror_by_default(self):
        self.fake.add("testdb", make_revision(size=8000))
        proc = self.run_cli("-v", "--no-log-file", "config", expect=0)
        self.assertNotIn("logging to", proc.stderr)
        self.run_cli("-q", "config", expect=0)

        # without --no-log-file the harness option is dropped for this one call
        args = [a for a in self.cli_args() if a != "--no-log-file"]
        proc = subprocess.run(args + ["download", "testdb"],
                              capture_output=True, text=True, timeout=900,
                              check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        default = os.path.join(self.root, "log")
        self.assertTrue(os.path.isfile(default), "download must log by default")
        with open(default, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("started:", text)
        self.assertIn("installed", text)

    def test_a_log_file_keeps_the_console_quiet(self):
        self.fake.add("testdb", make_revision(size=8000))
        log = os.path.join(self.root, "run.log")
        proc = self.run_cli("--log-file", log, "-v", "download", "testdb",
                            expect=0)
        # info/verbose/progress belong to the file, not to nohup.out
        self.assertNotIn("fetching", proc.stderr)
        self.assertNotIn("eta ", proc.stderr)
        self.assertNotIn("%", proc.stderr)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("fetching 1 file(s)", text)
        self.assertIn("installed", text)

    def test_console_full_mirrors_everything(self):
        self.fake.add("testdb", make_revision(size=8000))
        log = os.path.join(self.root, "run.log")
        proc = self.run_cli("--log-file", log, "--console", "full", "-v",
                            "download", "testdb", expect=0)
        self.assertIn("fetching 1 file(s)", proc.stderr)

    def test_console_off_silences_even_errors(self):
        self.fake.add("testdb", make_revision())      # so the manifest is valid
        log = os.path.join(self.root, "run.log")
        proc = self.run_cli("--log-file", log, "--console", "off", "download",
                            "nosuchdb", expect=1)
        self.assertEqual(proc.stderr, "")
        with open(log, encoding="utf-8") as fh:
            self.assertIn("ERROR: unknown database", fh.read())

    def test_warnings_still_reach_the_console_by_default(self):
        self.fake.add("testdb", make_revision(size=8000))
        self.fake.fail_code = 403
        self.fake.fail_times = {"testdb.tar.gz": 3}
        log = os.path.join(self.root, "run.log")
        proc = self.run_cli("--log-file", log, "--tries", "1",
                            "--file-retries", "2", "download", "testdb",
                            expect=0)
        self.assertIn("retrying in", proc.stderr,
                      "a warning the operator should see stays on screen")


class TestLoggingAndProgress(Case):
    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def test_log_file_receives_everything_even_when_quiet(self):
        self.fake.add("testdb", make_revision())
        log = os.path.join(self.root, "run.log")
        proc = self.run_cli("--log-file", log, "-q", "download", "testdb",
                            expect=0)
        self.assertEqual(proc.stderr, "", "-q keeps the console silent")
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("started:", text)
        self.assertIn("finished", text)
        self.assertIn("fetching 1 file(s)", text)
        self.assertIn("installed", text)
        self.assertRegex(text, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d",
                         "log lines carry timestamps")

    def test_failures_and_retries_land_in_the_log(self):
        self.fake.add("testdb", make_revision(size=8000))
        self.fake.fail_times = {"testdb.tar.gz": 3}
        self.fake.fail_code = 403
        log = os.path.join(self.root, "run.log")
        self.run_cli("--log-file", log, "--tries", "1", "--file-retries", "3",
                     "-q", "download", "testdb", expect=0)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("WARNING", text)
        self.assertIn("retrying in", text)
        self.assertIn("fetching 1 file(s)", text)

    def test_a_failure_is_written_to_the_log_file(self):
        self.fake.add("testdb", make_revision(size=8000))
        self.fake.fail_names = {"testdb.tar.gz"}
        log = os.path.join(self.root, "run.log")
        self.run_cli("--log-file", log, "--tries", "1", "-q", "download",
                     "testdb", expect=1)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("started:", text)
        self.assertIn("source:", text)
        self.assertIn("fetching", text)
        self.assertIn("ERROR: 1/1 file(s) failed to download", text,
                      "the reason for a failure must reach the log file")
        self.assertIn("finished", text)

    def test_a_bad_database_name_is_logged_too(self):
        self.fake.add("testdb", make_revision())
        log = os.path.join(self.root, "run.log")
        self.run_cli("--log-file", log, "-q", "download", "nosuchdb", expect=1)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("ERROR: unknown database", text)

    def test_progress_is_reported_without_a_terminal(self):
        self.fake.add("testdb", make_revision(size=200_000))
        self.fake.stall_after = 40_000          # keep it running for a moment
        child = self.cleanup_child(self.run_cli_async(
            "--progress-interval", "0.5", "--timeout", "2", "--tries", "1",
            "download", "testdb"))
        _out, err = child.communicate(timeout=60)
        self.assertIn("files 0/1", err)
        self.assertRegex(err, r"\d+\.\d%/s|%|eta ")
        self.fake.stall_after = None

    def test_doctor_reports_space_and_tools(self):
        proc = self.run_cli("doctor", expect=0)
        self.assertIn("filesystem", proc.stderr)
        self.assertIn("free", proc.stderr)
        self.assertIn("quota", proc.stderr)
        data = json.loads(self.run_cli("--json", "doctor", expect=0).stdout)
        self.assertIn("tools", data)
        self.assertIn("aria2c", data["tools"])
        self.assertEqual(data["filesystem"]["free"],
                         module.filesystem_info(data["root"])["free"])


class TestPartialBatchRecovery(Case):
    """The shape of a real nt download: several volumes arrive, then the rest
    fail; after fixing the cause, re-running must not fetch them again."""

    def extra_cli_args(self):
        return ["--no-metadata-json"]

    def volumes(self, n=6):
        return make_revision(dates=("Jul 01, 2026  1:00 AM",) * n, size=8000)

    def test_failed_batch_then_resume_downloads_only_the_rest(self):
        rev = self.volumes(6)
        self.fake.add("testdb", rev)
        names = sorted(rev["files"])
        got_through, refused = names[:4], names[4:]
        self.fake.fail_names = set(refused)

        first = self.run_cli("--tries", "1", "download", "testdb", expect=1)
        self.assertIn(f"{len(refused)}/{len(names)} file(s) failed to download",
                      first.stderr)
        self.assertIn("HTTP 404", first.stderr)
        self.assertEqual(self.snapshots(), [], "nothing may be installed")
        # extraction is skipped for a failed batch, so the four volumes that
        # did arrive stay in staging and are reused by the next run
        self.assertIsNotNone(self.staged(names[0]),
                             "volumes that arrived must be kept for the retry")

        self.fake.fail_names = set()
        before = dict(self.fake.served)
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        for name in got_through:
            self.assertEqual(served.get(name, 0), 0,
                             f"{name} must not be downloaded twice")
        for name in refused:
            self.assertGreater(served.get(name, 0), 0,
                               f"{name} still had to be fetched")
        # extraction never ran for the failed batch, so these are recovered by
        # the "file is still in staging" path; either way nothing is fetched twice
        self.assertIn("was already fetched by an earlier run", proc.stderr)
        self.run_cli("verify", expect=0)

    def test_interrupted_after_extraction_resumes_from_the_receipt(self):
        rev = self.volumes(4)
        self.fake.add("testdb", rev)
        names = sorted(rev["files"])
        out = self.run_cli("--dry-run", "download", "testdb", expect=0)
        revkey = re.search(r"rev ([0-9a-f]{16})", out.stdout).group(1)
        extract = os.path.join(self.root, ".staging", "testdb", revkey,
                               "extracted")
        os.makedirs(extract)
        # reproduce what a killed run leaves behind: two archives already
        # unpacked (and released) plus their receipt
        receipt = {}
        for name in names[:2]:
            with tarfile.open(fileobj=io.BytesIO(rev["files"][name])) as tf:
                members = {}
                for member in tf:
                    if not member.isfile():
                        continue
                    data = tf.extractfile(member).read()
                    with open(os.path.join(extract, member.name), "wb") as fh:
                        fh.write(data)
                    members[member.name] = {
                        "size": len(data), "md5": hashlib.md5(data).hexdigest()}
            receipt[name] = {
                "archive_md5": hashlib.md5(rev["files"][name]).hexdigest(),
                "members": members}
        with open(os.path.join(extract, ".receipt.json"), "w") as fh:
            json.dump(receipt, fh)

        before = dict(self.fake.served)
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        self.assertIn("already unpacked by an earlier run", proc.stderr)
        served = {k: v - before.get(k, 0) for k, v in self.fake.served.items()}
        for name in names[:2]:
            self.assertEqual(served.get(name, 0), 0,
                             f"{name} must not be fetched again")
        self.run_cli("verify", expect=0)


class TestArchiveLifetime(Case):
    def test_archives_are_released_unless_asked_for(self):
        self.fake.add("testdb", make_revision())
        self.run_cli("-q", "download", "testdb", expect=0)
        files = os.listdir(os.path.join(self.root, self.current()))
        self.assertIn("testdb.nin", files)
        self.assertNotIn("testdb.tar.gz", files,
                         "the archive must be dropped after unpacking to keep "
                         "the peak disk usage down")

    def test_keep_archives_retains_them(self):
        self.fake.add("testdb", make_revision())
        self.run_cli("-q", "--keep-archives", "download", "testdb", expect=0)
        files = os.listdir(os.path.join(self.root, self.current()))
        self.assertIn("testdb.tar.gz", files)
        self.run_cli("verify", expect=0)


class TestSnapshots(Case):
    def test_revision_switch_is_atomic_and_rollback_works(self):
        self.fake.add("testdb", make_revision(tag=b"a"), rev="a")
        self.fake.add("testdb", make_revision(
            dates=("Jul 09, 2026  9:00 AM",), tag=b"b"), rev="b",
            publish=False)
        self.run_cli("-q", "--keep-snapshots", "5", "download", "testdb",
                     expect=0)
        first = self.current()

        self.fake.publish("testdb", "b")
        self.run_cli("-q", "--keep-snapshots", "5", "download", "testdb",
                     expect=0)
        second = self.current()
        self.assertNotEqual(first, second)
        self.run_cli("verify", expect=0)
        self.run_cli("verify", "--snapshot", first, expect=0)

        self.run_cli("rollback", "--to", "1", expect=0)
        self.assertEqual(self.current(), first)
        self.run_cli("verify", expect=0)

    def test_gc_keeps_two_snapshots_by_default(self):
        for i, tag in enumerate((b"a", b"b", b"c")):
            self.fake.add("testdb", make_revision(
                dates=(f"Jul 0{i + 1}, 2026  1:00 AM",), tag=tag),
                rev=f"r{i}")
            self.fake.publish("testdb", f"r{i}")
            self.run_cli("-q", "download", "testdb", expect=0)
        self.assertLessEqual(len(self.snapshots()), 2)
        self.assertTrue(os.path.islink(os.path.join(self.root, "current")))

    def test_list_json(self):
        self.fake.add("testdb", make_revision())
        self.run_cli("-q", "download", "testdb", expect=0)
        rows = json.loads(self.run_cli("list", "--json", expect=0).stdout)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["current"])
        self.assertIn("testdb", rows[0]["databases"])


class TestReplica(Case):
    """A mirror whose manifest still names ftp.ncbi.nlm.nih.gov (the real case:
    /blast/db/v5/blastdb-metadata-1-1.json and every replica look like this)."""

    def test_ncbi_base_redirects_the_payload_urls_too(self):
        self.fake.manifest_host = "ftp.ncbi.nlm.nih.gov"
        self.fake.add("testdb", make_revision())
        # run_cli always passes --ncbi-base <fake server>, so nothing may be
        # fetched from the real ncbi.nlm.nih.gov
        proc = self.run_cli("-v", "download", "testdb", expect=0)
        self.assertIn("fetching 2 file(s)", proc.stderr)
        self.assertIn("with the built-in downloader", proc.stderr)
        self.run_cli("verify", expect=0)

    def test_manifest_mode_keeps_the_published_url(self):
        # .invalid never resolves (RFC 2606), so this stays offline & fast
        self.fake.manifest_host = "ftp.invalid"
        self.fake.add("testdb", make_revision())
        # manifest mode rewrites ftp:// to https:// and keeps the published
        # host, so neither the payload nor its sidecar may reach the mirror
        self.run_cli("--ncbi-url", "manifest", "--tries", "1",
                     "--timeout", "2", "download", "testdb", expect=4)
        self.assertEqual(self.fake.requests.count("testdb.tar.gz"), 0)
        self.assertEqual(self.fake.requests.count("testdb.tar.gz.md5"), 0)

    def test_showall_and_config_report_the_mode(self):
        self.fake.manifest_host = "ftp.ncbi.nlm.nih.gov"
        self.fake.add("testdb", make_revision())
        data = json.loads(self.run_cli("config", expect=0).stdout)
        self.assertEqual(data["ncbi_url"], "mirror")
        data = json.loads(self.run_cli("--ncbi-url", "manifest", "config",
                                       expect=0).stdout)
        self.assertEqual(data["ncbi_url"], "manifest")


class TestCliContract(Case):
    """The documented command line must actually work as documented."""

    README = os.path.join(HERE, "README_blastdb_download.md")
    README_GITHUB = os.path.join(HERE, "README.md")
    README_EN = os.path.join(HERE, "README.en.md")

    def readmes(self):
        """Every user facing README, so the audits below cover all of them."""
        out = []
        for path in (self.README, self.README_GITHUB, self.README_EN):
            if os.path.isfile(path):
                out.append(path)
        return out

    def documentation_files(self):
        """Every markdown document in the project, in any language."""
        out = [f for f in (self.README, self.README_GITHUB, self.README_EN,
                           os.path.join(HERE, "CHANGELOG.md"),
                           os.path.join(HERE, "CONTRIBUTING.md"))
               if os.path.isfile(f)]
        docs = os.path.join(HERE, "docs")
        if os.path.isdir(docs):
            out += [os.path.join(docs, n) for n in sorted(os.listdir(docs))
                    if n.endswith(".md")]
        return out

    def all_option_strings(self):
        """Every option string of every parser, recursively."""
        found = set()
        seen = set()
        stack = [module.build_parser()]
        while stack:
            parser = stack.pop()
            if id(parser) in seen:
                continue
            seen.add(id(parser))
            for action in parser._actions:
                found.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    stack.extend(action.choices.values())
        return found

    def test_global_options_work_after_the_subcommand(self):
        # the exact shape that used to fail with "unrecognized arguments"
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("download", "testdb", "--jobs", "8",
                            "--connections", "4", "--dry-run", expect=0)
        self.assertIn("DRY-RUN", proc.stdout)
        self.assertEqual(self.snapshots(), [], "a dry run must not download")
        proc = self.run_cli("showall", "--source", "ncbi", "--format", "name",
                            expect=0)
        self.assertIn("testdb", proc.stdout)
        self.run_cli("list", "--json", expect=0)
        self.run_cli("gc", "--dry-run", "--keep", "3", expect=0)

    def test_a_misplaced_subcommand_option_names_its_owner(self):
        # --force is a download option: argparse alone would only say
        # "unrecognized arguments"
        proc = self.run_cli("--force", "download", "testdb", expect=2)
        self.assertIn("is an option of the `download` sub-command", proc.stderr)
        proc = self.run_cli("download", "--quick", "testdb", expect=2)
        self.assertIn("is an option of the `verify` sub-command", proc.stderr)
        proc = self.run_cli("download", "--nonsense", "testdb", expect=2)
        self.assertIn("--help", proc.stderr)

    def test_global_options_work_before_the_subcommand(self):
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("--jobs", "8", "--dry-run", "download", "testdb",
                            expect=0)
        self.assertIn("DRY-RUN", proc.stdout)

    def test_verbose_on_both_sides_reaches_debug_level(self):
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("-v", "download", "testdb", "-v", expect=0)
        self.assertIn("[debug]", proc.stderr, "-v -v must reach debug level")
        self.assertIn("hard-linked", proc.stderr)

    def test_quiet_keeps_stdout_clean_and_stderr_empty(self):
        # `blastdb_download.py download nt > log` must not swallow the log:
        # diagnostics are on stderr, data on stdout
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("-q", "--dry-run", "download", "testdb", expect=0)
        self.assertIn("DRY-RUN", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_the_licence_and_the_spdx_headers_agree(self):
        """A placeholder is easy to ship by accident; a placeholder is not a
        copyright holder."""
        with open(os.path.join(HERE, "LICENSE"), encoding="utf-8") as fh:
            licence = fh.read()
        match = re.search(r"^Copyright \(c\) (\d{4}) (.+)$", licence, re.M)
        self.assertIsNotNone(match, "LICENSE has no copyright line")
        holder = match.group(2).strip()
        self.assertNotIn("<", holder, f"LICENSE still has a placeholder: {holder}")
        self.assertNotEqual(holder.lower(), "copyright holder")
        self.assertIn("MIT License", licence)
        for name in ("blastdb_download.py", "test_blastdb_download.py"):
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                head = "".join(fh.readlines()[:6])
            self.assertIn("SPDX-License-Identifier: MIT", head, name)
            self.assertIn(f"Copyright (c) {match.group(1)} {holder}", head,
                          f"{name} disagrees with LICENSE about the holder")

    def test_internal_document_links_resolve(self):
        """A stale `](#anchor)` in a long README is invisible until someone
        clicks it; this checks every one against the real headings, in every
        language."""

        def anchor(title):
            a = title.strip().lower()
            a = re.sub(r"[^\w\- ]", "", a)
            return a.replace(" ", "-")

        manuals = {self.README_GITHUB, self.README_EN}
        for path in self.documentation_files():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            headings = {anchor(h) for h in
                        re.findall(r"^#{2,4} (.+)$", text, re.M)}
            links = set(re.findall(r"\]\(#([^)]+)\)", text))
            where = os.path.relpath(path, HERE)
            if path in manuals:
                self.assertTrue(links, f"{where} should have a table of contents")
            self.assertEqual(sorted(links - headings), [],
                             f"{where}: internal links point at headings that do "
                             f"not exist (they were probably renamed)")

    def test_every_readme_links_to_the_other_languages(self):
        with open(self.README_GITHUB, encoding="utf-8") as fh:
            zh = fh.read()
        with open(self.README_EN, encoding="utf-8") as fh:
            en = fh.read()
        self.assertIn("README.en.md", zh)
        self.assertIn("README.md", en)
        for text, needles in ((zh, ("## 第三方软件依赖", "必须", "aria2c")),
                              (en, ("## Third-party dependencies",
                                    "Required?", "aria2c"))):
            for needle in needles:
                self.assertIn(needle, text,
                              f"the dependency section is incomplete: {needle!r}")

    def test_the_compatibility_readme_is_a_pointer_not_a_copy(self):
        """README.md is canonical; the older path must not become a second copy."""
        if not os.path.isfile(self.README):
            self.skipTest("no compatibility README")
        with open(self.README, encoding="utf-8") as fh:
            text = fh.read()
        self.assertLess(len(text), 2000,
                        "README_blastdb_download.md should stay a short pointer to "
                        "README.md, not a 50 kB copy that can drift")
        self.assertIn("README.md", text)
        self.assertIn("docs/", text)

    def test_every_relative_documentation_link_resolves(self):
        """Markdown links between the documents must point at real files."""
        for path in self.documentation_files():
            base = os.path.dirname(path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue
                resolved = os.path.normpath(os.path.join(
                    base, urllib.parse.unquote(file_part)))
                where = os.path.relpath(path, HERE)
                self.assertTrue(os.path.exists(resolved),
                                f"{where} links to {target}, which does not exist")
                fragment = target.split("#", 1)[1] if "#" in target else ""
                if fragment and resolved.endswith(".md"):
                    with open(resolved, encoding="utf-8") as fh:
                        other = fh.read()
                    anchors = set()
                    for title in re.findall(r"^#{2,4} (.+)$", other, re.M):
                        a = title.strip().lower()
                        a = re.sub(r"[^\w\- ]", "", a)
                        anchors.add(a.replace(" ", "-"))
                    self.assertIn(urllib.parse.unquote(fragment), anchors,
                                  f"{where} links to {target}, but that section "
                                  f"does not exist any more")

    def test_every_global_option_writes_to_a_configuration_key(self):
        """A documented option that lands on an unused dest is silently dead.

        `-k/--min-split-size` used to write to `min_split_size` while the code
        read `min_split`, so the flag did nothing at all.
        """
        ignorable = {"help", "version", "quiet", "verbose", "config",
                     "command"}
        parser = module.build_parser()
        dead = []
        for action in parser._actions:
            if action.dest in ignorable or action.dest == argparse.SUPPRESS:
                continue
            if not isinstance(action, argparse._SubParsersAction) and \
                    action.dest not in module.DEFAULTS:
                dead.append(f"{'/'.join(action.option_strings)} -> "
                            f"{action.dest}")
        self.assertEqual(dead, [], "these options write to a destination that "
                                   "merge_args never reads")

    def test_every_global_option_reaches_the_effective_configuration(self):
        """Drive every global option through the real parser and check the
        effective configuration actually changes."""
        values = {
            "root": "/tmp/blastdb-cli-contract", "source": "ncbi",
            "ncbi_dir": "/x", "ncbi_base": "https://example.invalid",
            "ncbi_url": "mirror", "aria2c": "none", "jobs": "3",
            "connections": "3", "min_split": "8K", "limit_rate": "8K",
            "timeout": "1.5", "tries": "3", "keep_snapshots": "3",
            "reuse_verify": "md5", "on_torn": "fail", "torn_retries": "3",
            "torn_wait": "1.5", "smoke_test": "never", "min_free": "8K",
            "file_retries": "2", "progress_interval": "5",
            "log_file": "/tmp/blastdb-cli-contract.log",
            "adopt": "/tmp/blastdb-cli-contract-adopt",
            "console": "errors",
        }
        skip = {"help", "version", "quiet", "verbose", "config", "command"}
        problems = []
        for action in module.build_parser()._actions:
            if action.dest in skip or action.dest == argparse.SUPPRESS:
                continue
            if isinstance(action, argparse._SubParsersAction):
                continue
            flag = action.option_strings[0]
            kind = action.__class__.__name__
            if kind == "_StoreTrueAction":
                argv, expected = [flag], True
            elif kind == "_StoreFalseAction":
                argv, expected = [flag], False
            else:
                if action.dest not in values:
                    problems.append(f"{flag}: no sample value in the test")
                    continue
                raw = values[action.dest]
                argv = [flag, raw]
                expected = action.type(raw) if action.type else raw
                if kind == "_AppendAction":
                    expected = [expected]
            args = module.build_parser().parse_args(["download", "testdb"] + argv)
            cfg = module.merge_args(dict(module.DEFAULTS), args)
            got = cfg.get(action.dest, "<missing from the configuration>")
            if action.dest == "root":
                expected = os.path.abspath(expected)
            if got != expected:
                problems.append(f"{flag}: configuration has {got!r}, expected "
                                f"{expected!r}")
        self.assertEqual(problems, [])

    def test_every_documented_flag_exists_in_the_cli(self):
        known = {o for o in self.all_option_strings() if o.startswith("--")}
        # no document may invent a flag ...
        for path in self.documentation_files():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # flags inside an `aria2c_extra_args = [...]` example belong to
            # aria2, not to this CLI
            foreign = set()
            for m in re.finditer(r"aria2c_extra_args\s*=\s*\[([^\]]*)\]", text):
                foreign |= set(re.findall(r"--[a-z][a-z0-9-]*", m.group(1)))
            documented = set(re.findall(r"--[a-z][a-z0-9_-]*", text)) - foreign
            where = os.path.relpath(path, HERE)
            # the background note documents NCBI's own update_blastdb.pl, whose
            # options are not ours; elsewhere a phantom flag is a real mistake
            allowed = FOREIGN_TOOL_FLAGS if path not in (self.README_GITHUB,
                                                         self.README_EN) else set()
            self.assertEqual(sorted(documented - known - allowed), [],
                             f"{where} documents flags that do not exist")
        # ... and the two manuals must document every flag there is
        for path in (self.README_GITHUB, self.README_EN):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            foreign = set()
            for m in re.finditer(r"aria2c_extra_args\s*=\s*\[([^\]]*)\]", text):
                foreign |= set(re.findall(r"--[a-z][a-z0-9-]*", m.group(1)))
            documented = set(re.findall(r"--[a-z][a-z0-9_-]*", text)) - foreign
            self.assertEqual(sorted(known - documented - {"--help", "--version"}),
                             [], f"{os.path.basename(path)} does not document "
                                 f"these flags")

    def test_concurrent_runs_on_one_mirror_are_refused(self):
        self.fake.add("testdb", make_revision(size=400_000))
        self.fake.stall_after = 8_000          # hold the first run open
        child = self.cleanup_child(self.run_cli_async("download", "testdb"))
        self.wait_for(lambda: self.staged("testdb.tar.gz") is not None,
                      what="the first run to start downloading")
        second = self.run_cli("download", "testdb", expect=1)
        self.assertIn("already using this mirror", second.stderr)
        child.kill()
        child.wait(timeout=30)
        # read-only commands stay usable while a download is running
        self.run_cli("list", expect=0)
        self.fake.stall_after = None

    def test_platform_note_is_documented(self):
        with open(self.README_GITHUB, encoding="utf-8") as fh:
            text = fh.read()
        missing = [n for n in ("Windows", "Developer Mode", "stderr")
                   if n not in text]
        self.assertEqual(missing, [], "README is missing platform notes")


class TestInspect(Case):
    """Offline fingerprint check of an arbitrary directory."""

    def build_tree(self, dates, tag=b"a"):
        self.fake.add("testdb", make_revision(dates=dates, tag=tag), rev=tag.decode())
        self.run_cli("-q", "--keep-snapshots", "5", "download", "testdb",
                     expect=0)
        return os.path.join(self.root, self.current())

    def copy_tree(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
        return dst

    def test_clean_directory_passes(self):
        tree = self.build_tree(("Jul 01, 2026  1:00 AM",) * 2)
        proc = self.run_cli("inspect", "testdb", "--dir", tree, expect=0)
        self.assertIn("all volumes agree", proc.stderr)
        self.assertIn("verdict            : OK", proc.stderr)

    def test_mixed_directory_is_detected(self):
        first = self.build_tree(("Jul 01, 2026  1:00 AM",) * 2, tag=b"a")
        second = self.build_tree(("Jul 09, 2026  9:00 AM",) * 2, tag=b"b")
        mixed = self.copy_tree(first, os.path.join(self.root, "mixed"))
        # a hand written aria2c loop happily produces exactly this: volume 0 of
        # one release next to volume 1 of the next
        shutil.copy2(os.path.join(second, "testdb.01.nin"),
                     os.path.join(mixed, "testdb.01.nin"))
        proc = self.run_cli("inspect", "testdb", "--dir", mixed, expect=3)
        self.assertIn("MIXED", proc.stderr)

    def test_missing_payload_file_is_detected(self):
        tree = self.copy_tree(self.build_tree(("Jul 01, 2026  1:00 AM",) * 2),
                              os.path.join(self.root, "gap"))
        os.unlink(os.path.join(tree, "testdb.01.nsq"))
        proc = self.run_cli("inspect", "testdb", "--dir", tree, expect=3)
        self.assertIn("absent", proc.stderr)

    def test_truncated_payload_file_is_detected(self):
        tree = self.copy_tree(self.build_tree(("Jul 01, 2026  1:00 AM",) * 2),
                              os.path.join(self.root, "short"))
        with open(os.path.join(tree, "testdb.00.nsq"), "r+b") as fh:
            fh.truncate(1000)
        proc = self.run_cli("inspect", "testdb", "--dir", tree, expect=3)
        self.assertIn("truncated or replaced", proc.stderr)

    def test_json_output(self):
        tree = self.build_tree(("Jul 01, 2026  1:00 AM",) * 2)
        data = json.loads(self.run_cli("--json", "inspect", "testdb",
                                       "--dir", tree, expect=0).stdout)
        self.assertEqual(data["testdb"]["status"], "OK")
        self.assertEqual(len(data["testdb"]["volumes"]), 2)
        self.assertEqual(data["testdb"]["build_dates"], ["2026-07-01T01:00"])

    def test_absent_database_is_reported(self):
        self.build_tree(("Jul 01, 2026  1:00 AM",))
        proc = self.run_cli("inspect", "nosuchdb", expect=3)
        self.assertIn("no files found", proc.stderr)


class TestDryRunAndConfig(Case):
    def test_dry_run_downloads_nothing(self):
        self.fake.add("testdb", make_revision())
        proc = self.run_cli("--dry-run", "download", "testdb", expect=0)
        self.assertIn("DRY-RUN", proc.stdout)
        self.assertEqual(self.snapshots(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "current")))

    def test_config_file_is_honoured(self):
        self.fake.add("testdb", make_revision())
        cfg = os.path.join(self.root, "config.toml")
        with open(cfg, "w") as fh:
            fh.write('[general]\nreuse_verify = "md5"\nkeep_snapshots = 7\n')
        data = json.loads(self.run_cli("-c", cfg, "config", expect=0).stdout)
        self.assertEqual(data["reuse_verify"], "md5")
        self.assertEqual(data["keep_snapshots"], 7)

    def test_config_template_documents_every_default(self):
        import tomllib
        text = self.run_cli("config", "--template", expect=0).stdout
        # uncomment only the `# key = value` lines; prose stays a comment
        uncommented = "\n".join(
            re.sub(r"^#\s?", "", ln)
            if re.match(r"^#\s*[A-Za-z_][A-Za-z0-9_]*\s*=", ln) else ln
            for ln in text.splitlines())
        general = tomllib.loads(uncommented)["general"]
        self.assertEqual(sorted(general), sorted(module.DEFAULTS),
                         "the template must document exactly the settable keys")
        # a line marked `example` is documentation, not a default claim
        examples = set(re.findall(
            r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\s*=.*#.*\bexample\b", text,
            re.MULTILINE))
        for key in examples:
            general.pop(key, None)
        for key, value in general.items():
            self.assertEqual(value, module.DEFAULTS[key],
                             f"{key} drifted from the built-in default")

    def test_config_init_writes_an_inert_file_that_is_then_picked_up(self):
        path = os.path.join(self.root, "blastdb-download.toml")
        self.run_cli("config", "--init", expect=0)
        self.assertTrue(os.path.isfile(path))
        # already exists -> refuse, unless --force
        self.run_cli("config", "--init", expect=1)
        self.run_cli("config", "--init", "--force", expect=0)
        # it is now the loaded config file, and because every setting in it is
        # commented out nothing changed
        data = json.loads(self.run_cli("config", expect=0).stdout)
        self.assertEqual(data["config_file"], path)
        self.assertNotIn("_config_file", data)
        self.assertEqual(data["source"], "ncbi")
        self.assertEqual(data["keep_snapshots"], 2)
        self.assertEqual(data["config_search_path"][0], path)

    def test_root_local_config_wins_over_the_user_config(self):
        xdg = os.path.join(self.root, "xdg")
        os.makedirs(os.path.join(xdg, "blastdb-download"))
        with open(os.path.join(xdg, "blastdb-download", "config.toml"), "w") as fh:
            fh.write("[general]\nkeep_snapshots = 9\n")
        data = json.loads(self.run_cli("config", expect=0,
                                       env={"XDG_CONFIG_HOME": xdg}).stdout)
        self.assertEqual(data["keep_snapshots"], 9)
        # the mirror-local file takes precedence over the user level one
        with open(os.path.join(self.root, "blastdb-download.toml"), "w") as fh:
            fh.write("[general]\nkeep_snapshots = 7\n")
        data = json.loads(self.run_cli("config", expect=0,
                                       env={"XDG_CONFIG_HOME": xdg}).stdout)
        self.assertEqual(data["keep_snapshots"], 7)
        self.assertEqual(data["config_file"],
                         os.path.join(self.root, "blastdb-download.toml"))

    def test_showall(self):
        self.fake.add("testdb", make_revision())
        self.fake.add("otherdb", make_revision(dbname="otherdb"))
        names = self.run_cli("showall", expect=0).stdout.split()
        self.assertEqual(names, ["otherdb", "testdb"])
        rows = json.loads(self.run_cli("showall", "--format", "json",
                                       expect=0).stdout)
        self.assertEqual(len(rows["databases"]), 2)
        self.assertIn("2026-07", self.run_cli("showall", "--format", "pretty",
                                              expect=0).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
