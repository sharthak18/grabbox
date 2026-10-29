"""Horse engine tests: correctness, pause/resume, and the speed claim.

The local server throttles *per connection* — exactly the situation where
IDM-style segmented downloading wins. Run: python3 -m unittest discover -s tests
"""

import os
import random
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from grabbox import horse  # noqa: E402


def payload(size, seed=42):
    return random.Random(seed).randbytes(size)


class _Handler(BaseHTTPRequestHandler):
    payload_bytes = b""
    etag = '"v1"'
    per_conn_bps = 0          # 0 = unthrottled
    support_ranges = True
    range_log = []            # every Range header seen, in order
    range_count = 0           # non-probe Range GETs, for the special paths
    count_lock = threading.Lock()

    # /slowfirst.bin: the FIRST connection is throttled hard, the rest fly.
    # /flaky429.bin: the first two segment requests get HTTP 429.
    # /fake.mp4: an HTML error page lying about being a media file.
    # /wrongrange.bin: 206s whose Content-Range starts at the wrong byte.
    # /stable.bin: 302 -> /signed.bin?t=N (fresh N per hit); a token dies
    #   after 3 segment requests (410) — only re-resolving refreshes it.
    # /trickle.bin: the first connection sends 32 KB then goes silent.
    # /max2.bin: more than 2 concurrent connections get HTTP 429.
    SLOW_BPS = 60 * 1024
    FAST_BPS = 900 * 1024
    token_counter = 0
    token_uses = 0
    trickled = False
    inflight = 0

    def log_message(self, *a):
        pass

    def _send_all(self, data, rate):
        chunk = 16 * 1024
        sent = 0
        try:
            while sent < len(data):
                part = data[sent:sent + chunk]
                self.wfile.write(part)
                self.wfile.flush()
                sent += len(part)
                if rate:
                    time.sleep(len(part) / float(rate))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/fake.mp4":
            body = (b"<html><head><title>403 Forbidden</title></head>"
                    b"<body>nginx</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/stable.bin":
            with type(self).count_lock:
                type(self).token_counter += 1
                type(self).token_uses = 0
                tok = type(self).token_counter
            self.send_response(302)
            self.send_header("Location", "/signed.bin?t=%d" % tok)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/signed.bin"):
            tok = int(self.path.rsplit("t=", 1)[-1])
            rng = self.headers.get("Range")
            expired = tok != type(self).token_counter
            if rng and rng != "bytes=0-0":
                with type(self).count_lock:
                    type(self).token_uses += 1
                    if type(self).token_uses > 3:
                        expired = True
            if expired:
                self.send_response(410)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # fall through to the normal ranged file serving below
        if self.path == "/trickle.bin":
            rng = self.headers.get("Range")
            data = type(self).payload_bytes
            if rng and rng != "bytes=0-0" and not type(self).trickled:
                type(self).trickled = True
                start = int(re.match(r"bytes=(\d+)-", rng).group(1))
                body = data[start:]
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", type(self).etag)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, len(data) - 1,
                                                     len(data)))
                self.end_headers()
                try:
                    self.wfile.write(body[:32 * 1024])
                    self.wfile.flush()
                    time.sleep(4)          # ...then silence.
                    self.wfile.write(body[32 * 1024:])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
        if self.path == "/max2.bin":
            rng = self.headers.get("Range")
            if rng and rng != "bytes=0-0":
                with type(self).count_lock:
                    type(self).inflight += 1
                    over = type(self).inflight > 2
                if over:
                    with type(self).count_lock:
                        type(self).inflight -= 1
                    self.send_response(429)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    time.sleep(0.3)        # hold the slot so rivals collide
                    return self._serve_ranged()
                finally:
                    with type(self).count_lock:
                        type(self).inflight -= 1
            return self._serve_ranged()
        if self.path == "/wrongrange.bin":
            return self._serve_ranged(wrong=True)
        return self._serve_ranged()

    def _serve_ranged(self, wrong=False):
        data = type(self).payload_bytes
        rate = type(self).per_conn_bps
        rng = self.headers.get("Range")
        if rng:
            type(self).range_log.append(rng)
        if rng and rng != "bytes=0-0":   # bytes=0-0 is the engine's probe
            with type(self).count_lock:
                type(self).range_count += 1
                n = type(self).range_count
            if self.path == "/flaky429.bin" and n <= 2:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/slowfirst.bin":
                rate = type(self).SLOW_BPS if n == 1 else type(self).FAST_BPS
        start, end, status = 0, len(data) - 1, 200
        if rng and type(self).support_ranges:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                status = 206
                if wrong and rng != "bytes=0-0":
                    start += 100   # the lie: bytes from the wrong offset
        body = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", type(self).etag)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, len(data)))
        self.end_headers()
        chunk = 16 * 1024
        sent = 0
        try:
            while sent < len(body):
                part = body[sent:sent + chunk]
                self.wfile.write(part)
                self.wfile.flush()
                sent += len(part)
                if rate:
                    time.sleep(len(part) / float(rate))
        except (BrokenPipeError, ConnectionResetError):
            pass


class HorseTest(unittest.TestCase):
    def setUp(self):
        _Handler.payload_bytes = payload(3 * 1024 * 1024)
        _Handler.etag = '"v1"'
        _Handler.per_conn_bps = 0
        _Handler.support_ranges = True
        _Handler.range_log = []
        _Handler.range_count = 0
        _Handler.token_counter = 0
        _Handler.token_uses = 0
        _Handler.trickled = False
        _Handler.inflight = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/file.bin" % self.server.server_port
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "file.bin")

    def tearDown(self):
        self.server.shutdown()

    def read_dest(self):
        with open(self.dest, "rb") as fh:
            return fh.read()

    def test_segmented_download_is_byte_exact(self):
        # A light per-connection throttle: unthrottled loopback can let
        # the first connection finish before its siblings even spawn,
        # which made the segment-count assertion below racy.
        _Handler.per_conn_bps = 2 * 1024 * 1024
        out = horse.download(self.url, self.dest, connections=8)
        self.assertEqual(out, self.dest)
        self.assertEqual(self.read_dest(), _Handler.payload_bytes)
        self.assertFalse(os.path.exists(self.dest + horse.PART_SUFFIX))
        self.assertFalse(os.path.exists(self.dest + horse.STATE_SUFFIX))
        ranged = [r for r in _Handler.range_log if not r.startswith("bytes=0-0")]
        self.assertGreaterEqual(len(ranged), 4, "expected multiple segments")

    def test_pause_then_resume_continues_not_restarts(self):
        _Handler.per_conn_bps = 400 * 1024
        stop = {"v": None}
        seen = []

        def progress(done, total, speed):
            seen.append(done)
            if done > 300 * 1024:
                stop["v"] = "pause"

        with self.assertRaises(horse.HorsePaused):
            horse.download(self.url, self.dest, connections=4,
                           should_stop=lambda: stop["v"], progress=progress)
        self.assertTrue(os.path.exists(self.dest + horse.PART_SUFFIX))
        self.assertTrue(os.path.exists(self.dest + horse.STATE_SUFFIX))
        partial = max(seen)
        self.assertGreater(partial, 0)
        self.assertLess(partial, len(_Handler.payload_bytes))

        _Handler.range_log = []
        out = horse.download(self.url, self.dest, connections=4,
                             should_stop=lambda: None)
        self.assertEqual(out, self.dest)
        self.assertEqual(self.read_dest(), _Handler.payload_bytes)
        resumed = [r for r in _Handler.range_log
                   if r.startswith("bytes=") and not r.startswith("bytes=0-")]
        self.assertTrue(resumed, "resume should request non-zero ranges: %r"
                        % (_Handler.range_log,))

    def test_changed_file_restarts_cleanly(self):
        _Handler.per_conn_bps = 400 * 1024
        stop = {"v": None}

        def progress(done, total, speed):
            if done > 200 * 1024:
                stop["v"] = "pause"

        with self.assertRaises(horse.HorsePaused):
            horse.download(self.url, self.dest, connections=4,
                           should_stop=lambda: stop["v"], progress=progress)
        # The file changes on the server: new body, new validator.
        _Handler.payload_bytes = payload(3 * 1024 * 1024, seed=7)
        _Handler.etag = '"v2"'
        _Handler.per_conn_bps = 0
        horse.download(self.url, self.dest, connections=4,
                       should_stop=lambda: None)
        self.assertEqual(self.read_dest(), _Handler.payload_bytes)

    def test_work_stealing_beats_the_slow_first_connection(self):
        # THE DownloadCraft scenario: connection #1 is throttled to
        # 60 KB/s, the rest run at 900 KB/s. Fixed split would park a
        # quarter of the file (1.5 MB) on the slow connection: ~25s.
        # Work-stealing splits its segment away; the whole file must
        # land far sooner, byte-exact.
        _Handler.payload_bytes = payload(6 * 1024 * 1024, seed=11)
        url = "http://127.0.0.1:%d/slowfirst.bin" % self.server.server_port
        dest = os.path.join(self.dir, "slowfirst.bin")
        t0 = time.monotonic()
        horse.download(url, dest, connections=4)
        elapsed = time.monotonic() - t0
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)
        fixed_split_floor = (len(_Handler.payload_bytes) / 4.0) \
            / _Handler.SLOW_BPS
        print("\n[horse steal] slow-first: %.2fs (fixed split >= %.0fs)"
              % (elapsed, fixed_split_floor))
        self.assertLess(elapsed, fixed_split_floor / 2.0)

    def test_html_error_page_is_never_saved_as_a_file(self):
        # Validation (DownloadCraft's rule): the server answers 200 with
        # an HTML error page, a lying Content-Type, and a Content-Length.
        url = "http://127.0.0.1:%d/fake.mp4" % self.server.server_port
        dest = os.path.join(self.dir, "fake.mp4")
        with self.assertRaises(horse.HorseFallback):
            horse.download(url, dest, connections=4)
        self.assertFalse(os.path.exists(dest))

    def test_wrong_content_range_is_rejected_not_written(self):
        # Every 206 starts 100 bytes late. The engine must notice the
        # Content-Range mismatch and fail — never glue wrong bytes in.
        url = "http://127.0.0.1:%d/wrongrange.bin" % self.server.server_port
        dest = os.path.join(self.dir, "wrongrange.bin")
        with self.assertRaises(horse.HorseError):
            horse.download(url, dest, connections=4)
        self.assertFalse(os.path.exists(dest))

    def test_expired_signed_link_refreshes_and_keeps_bytes(self):
        # The signed token dies after 3 segment requests (410). The
        # engine must re-resolve /stable.bin, verify it's the same file,
        # and finish byte-exact under the fresh token. Per-connection
        # throttling guarantees a 4th request actually happens.
        _Handler.per_conn_bps = 300 * 1024
        url = "http://127.0.0.1:%d/stable.bin" % self.server.server_port
        dest = os.path.join(self.dir, "stable.bin")
        horse.download(url, dest, connections=4)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)
        self.assertGreaterEqual(_Handler.token_counter, 2,
                                "expected at least one link refresh")

    def test_stalled_connection_is_reopened(self):
        old = horse.STALL_SECONDS
        horse.STALL_SECONDS = 1.0
        try:
            url = "http://127.0.0.1:%d/trickle.bin" % self.server.server_port
            dest = os.path.join(self.dir, "trickle.bin")
            t0 = time.monotonic()
            horse.download(url, dest, connections=4)
            elapsed = time.monotonic() - t0
        finally:
            horse.STALL_SECONDS = old
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)
        self.assertLess(elapsed, 15)

    def test_connection_cap_halves_under_429_pressure(self):
        # The server refuses a 3rd concurrent connection with 429. The
        # engine must back its cap down (4 -> 2) and still finish.
        url = "http://127.0.0.1:%d/max2.bin" % self.server.server_port
        dest = os.path.join(self.dir, "max2.bin")
        horse.download(url, dest, connections=4)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)

    def test_429_is_retried_not_fatal(self):
        url = "http://127.0.0.1:%d/flaky429.bin" % self.server.server_port
        dest = os.path.join(self.dir, "flaky.bin")
        horse.download(url, dest, connections=4)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)

    def test_no_range_server_falls_back_to_single_stream(self):
        _Handler.support_ranges = False
        horse.download(self.url, self.dest, connections=8)
        self.assertEqual(self.read_dest(), _Handler.payload_bytes)

    def test_segments_beat_one_connection_when_throttled_per_conn(self):
        _Handler.payload_bytes = payload(1200 * 1024, seed=3)
        _Handler.per_conn_bps = 300 * 1024  # ~4s single-stream

        t0 = time.monotonic()
        horse.download(self.url, os.path.join(self.dir, "one.bin"),
                       connections=1)
        single = time.monotonic() - t0

        t0 = time.monotonic()
        horse.download(self.url, os.path.join(self.dir, "many.bin"),
                       connections=6)
        multi = time.monotonic() - t0

        print("\n[horse bench] 1 conn: %.2fs · 6 conns: %.2fs (%.1fx)"
              % (single, multi, single / max(multi, 0.01)))
        self.assertLess(multi, single * 0.6)


if __name__ == "__main__":
    unittest.main()
