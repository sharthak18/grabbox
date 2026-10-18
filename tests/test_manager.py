"""Manager tests: a real job through the horse path — pause keeps bytes,
resume finishes it, and the queue survives a (simulated) restart."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_horse import _Handler, payload  # noqa: E402
from grabbox.config import Config  # noqa: E402
from grabbox.downloader import Manager  # noqa: E402


def wait_for(pred, timeout=30.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class ManagerTest(unittest.TestCase):
    def setUp(self):
        _Handler.payload_bytes = payload(int(1.5 * 1024 * 1024), seed=11)
        _Handler.etag = '"v1"'
        _Handler.per_conn_bps = 200 * 1024
        _Handler.support_ranges = True
        _Handler.range_log = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/file.bin" % self.server.server_port
        self.dir = tempfile.mkdtemp()
        self.config = Config(path=os.path.join(self.dir, "config.json"))
        self.config.data["download_dir"] = os.path.join(self.dir, "dl")
        self.config.data["connections"] = 2   # slow enough to catch mid-flight
        self.manager = Manager(self.config)

    def tearDown(self):
        self.server.shutdown()

    def test_pause_resume_and_restart_restore(self):
        job = self.manager.add(self.url, kind="file")
        self.assertTrue(wait_for(lambda: job.status == "running"))
        self.assertTrue(wait_for(lambda: job.downloaded > 100 * 1024),
                        "no bytes flowed (status=%s)" % job.status)
        self.assertTrue(self.manager.pause(job.id))
        self.assertTrue(wait_for(lambda: job.status == "paused"),
                        "job never paused (status=%s)" % job.status)
        kept = job.downloaded
        self.assertGreater(kept, 0)
        self.assertLess(kept, len(_Handler.payload_bytes))

        self.assertTrue(self.manager.resume(job.id))
        self.assertTrue(wait_for(lambda: job.status == "done"),
                        "job never finished (status=%s err=%s)"
                        % (job.status, job.error))
        with open(job.path, "rb") as fh:
            self.assertEqual(fh.read(), _Handler.payload_bytes)

        # The queue persisted to disk next to the config...
        jobs_file = os.path.join(self.dir, "jobs.json")
        self.assertTrue(os.path.exists(jobs_file))
        # ...and a fresh Manager (the "restart") restores it.
        manager2 = Manager(self.config)
        restored = manager2.get(job.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "done")
        self.assertEqual(restored.path, job.path)

    def test_interrupted_job_restores_as_paused(self):
        jobs_file = os.path.join(self.dir, "jobs.json")
        with open(jobs_file, "w", encoding="utf-8") as fh:
            json.dump([{
                "id": "jX-dead01", "url": self.url, "kind": "file",
                "status": "running", "percent": 42.0,
            }], fh)
        manager = Manager(self.config)
        job = manager.get("jX-dead01")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "paused")   # resumable, not lost
        self.assertEqual(job.percent, 42.0)


if __name__ == "__main__":
    unittest.main()
