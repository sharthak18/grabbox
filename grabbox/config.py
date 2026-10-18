"""Persisted settings, kept in ~/.grabbox/config.json."""

import json
import os
import threading

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "GrabBox")

DEFAULTS = {
    "download_dir": DEFAULT_DIR,
    "concurrency": 2,
    # --- the horse ---------------------------------------------------
    "connections": 8,            # segments / concurrent fragments, 1..16
    "speed_limit_kbps": 0,       # 0 = unlimited
    "turbo": True,               # segmented direct downloads + fragments
    "cookies_browser": "",       # "" | chrome | firefox | edge | brave | safari
    "watch_clipboard": False,
    "port": 8765,
    "open_browser": True,
}

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".grabbox")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


class Config(object):
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key in DEFAULTS:
                    if key in stored:
                        self.data[key] = stored[key]
        except (IOError, OSError, ValueError):
            pass
        return self.data

    def save(self):
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, indent=2, sort_keys=True)
                return True
            except (IOError, OSError):
                return False

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError("unknown setting: %s" % key)
        self.data[key] = value
        self.save()

    def update(self, mapping):
        for key, value in (mapping or {}).items():
            if key in DEFAULTS:
                self.data[key] = value
        self.save()

    def download_dir(self):
        d = self.get("download_dir") or DEFAULT_DIR
        d = os.path.expanduser(d)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
