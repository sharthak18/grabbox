"""Remote ZIP peek tests: list a ZIP's contents in 1-2 range requests."""

import io
import os
import re
import sys
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from grabbox import zipeek  # noqa: E402

FILES = {
    "hello.txt": b"hello world" * 40,
    "docs/readme.md": b"# GrabBox\n" * 25,
    "song.mp3": bytes(range(256)) * 64,
}


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in FILES.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    payload_bytes = b""
    request_count = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).request_count += 1
        data = type(self).payload_bytes
        rng = self.headers.get("Range")
        start, end, status = 0, len(data) - 1, 200
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                status = 206
        body = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, len(data)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class ZipPeekTest(unittest.TestCase):
    def setUp(self):
        _Handler.payload_bytes = make_zip()
        _Handler.request_count = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/bundle.zip" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()

    def test_lists_entries_with_sizes_in_few_requests(self):
        entries = zipeek.list_remote(self.url)
        self.assertIsNotNone(entries)
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(set(by_name), set(FILES))
        for name, data in FILES.items():
            self.assertEqual(by_name[name]["size"], len(data))
        # probe + tail (+ maybe central directory) — never the whole file
        self.assertLessEqual(_Handler.request_count, 3)

    def test_non_zip_returns_none(self):
        _Handler.payload_bytes = b"definitely not a zip" * 64
        self.assertIsNone(zipeek.list_remote(self.url))

    def test_unreachable_returns_none(self):
        self.assertIsNone(
            zipeek.list_remote("http://127.0.0.1:1/nope.zip", timeout=2))


if __name__ == "__main__":
    unittest.main()
