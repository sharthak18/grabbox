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

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = type(self).payload_bytes
        rate = type(self).per_conn_bps
        rng = self.headers.get("Range")
        if rng:
            type(self).range_log.append(rng)
        start, end, status = 0, len(data) - 1, 200
        if rng and type(self).support_ranges:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                status = 206
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
