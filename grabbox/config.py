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
    # torrents (desktop only; the browser build ignores these)
    "torrent_download_kbps": 0,
    "torrent_upload_kbps": 0,
    "torrent_seed_when_done": False,
}

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".grabbox")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def normalize_dir(raw):
    """Untrusted folder setting -> a usable absolute path, or None.

    Trims quotes/space, expands ~, creates the folder if needed and checks
    it is a directory. Empty input means "the default location".
    """
    text = (raw or "").strip().strip('"').strip("'").strip()
    if not text:
        return DEFAULT_DIR
    text = os.path.abspath(os.path.expanduser(text.rstrip("/\\") or text))
    try:
        os.makedirs(text, exist_ok=True)
    except OSError:
        return None
    return text if os.path.isdir(text) else None


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
            if key not in DEFAULTS:
                continue
            if key == "download_dir":
                norm = normalize_dir(value)
                if norm is None:
                    raise ValueError("can't use that folder — it doesn't exist and can't be created")
                value = norm
            self.data[key] = value
        self.save()

    def download_dir(self):
        d = self.get("download_dir") or DEFAULT_DIR
        return normalize_dir(d) or DEFAULT_DIR
