"""
GrabBox local server.

One small HTTP server, standard library only. The browser UI, the extension and
the Android app all talk to the same JSON API, so there is exactly one place
where downloads happen and one place to fix.

    GET  /                     the web UI
    GET  /api/health           yt-dlp / ffmpeg / JS runtime status
    POST /api/probe            what is this link? (kind, formats, size)
    POST /api/download         start a job
    GET  /api/jobs             job list with progress
    POST /api/jobs/<id>/cancel
    POST /api/jobs/clear
    GET  /api/files            what is already in the download folder
    POST /api/files/open       open a file / reveal it / delete it
    GET  /api/clipboard        newest URL seen on the clipboard
    GET|POST /api/config       settings

Run it:  python3 -m grabbox   (or a double-click launcher from scripts/)
"""

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__, clipboard, downloader, files, sniff, ytdlp_env as env
from .config import Config

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ALLOWED_ORIGIN_RE = re.compile(
    r"^(chrome-extension|moz-extension|safari-web-extension)://"
    r"|^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)


class Handler(BaseHTTPRequestHandler):
    server_version = "GrabBox/%s" % __version__
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("[grabbox] %s\n" % (fmt % args))

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and ALLOWED_ORIGIN_RE.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    # Set by do_HEAD around a do_GET call so _send skips the body write.
    _head_only = False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self._head_only:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        # BaseHTTPRequestHandler does not implement HEAD. Treat it like GET
        # but swallow the body - that way static file headers (Content-Type,
        # Content-Length) and API CORS pre-flight HEAD probes both work.
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    # --------------------------------------------------------------- routing

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "":
            return self._static("index.html")
        if path == "/api/health":
            return self._send(200, self._health())
        if path == "/api/jobs":
            return self._send(200, {"jobs": self.manager.list()})
        if path == "/api/files":
            return self._send(200, {
                "dir": self.config.download_dir(),
                "files": files.list_files(self.config.download_dir()),
            })
        if path == "/api/clipboard":
            return self._send(200, {"item": self.watcher.take(),
                                    "watching": self.watcher.enabled})
        if path == "/api/config":
            return self._send(200, self.config.data)
        if path.startswith("/api/"):
            return self._send(404, {"error": "unknown endpoint"})
        return self._static(path.lstrip("/"))

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._json_body()

        if path == "/api/probe":
            url = (body.get("url") or "").strip()
            if not url:
                return self._send(400, {"error": "no url"})
            return self._send(200, downloader.probe_url(
                url, cookies=self.config.get("cookies_browser") or None))

        if path == "/api/download":
            url = (body.get("url") or "").strip()
            if not url:
                return self._send(400, {"error": "no url"})
            job = self.manager.add(
                url,
                kind=body.get("kind") or "file",
                quality=body.get("quality"),
                playlist=bool(body.get("playlist")),
                directory=body.get("dir") or None,
                cookies=body.get("cookies") or self.config.get("cookies_browser")
                        or None,
                filename=(body.get("filename") or "").strip() or None,
            )
            return self._send(200, {"job": job.to_dict()})

        if path == "/api/jobs/clear":
            self.manager.clear_finished()
            return self._send(200, {"ok": True})

        m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if m:
            return self._send(200, {"ok": self.manager.cancel(m.group(1))})

        if path == "/api/files/open":
            target = body.get("path") or self.config.download_dir()
            action = body.get("action") or "reveal"
            if action == "delete":
                ok = files.delete(target, self.config.download_dir())
            elif action == "open":
                ok = files.open_file(target)
            else:
                ok = files.open_in_file_manager(target)
            return self._send(200, {"ok": ok})

        if path == "/api/config":
            prev_conc = int(self.config.get("concurrency", 2))
            self.config.update(body)
            new_conc = int(self.config.get("concurrency", 2))
            # If concurrency changed, rebuild the manager's semaphore so the
            # new limit takes effect for subsequent jobs.
            if new_conc != prev_conc and self.manager is not None:
                self.manager.set_concurrency(new_conc)
            if body.get("watch_clipboard"):
                self.watcher.start()
            elif "watch_clipboard" in body:
                self.watcher.stop()
            return self._send(200, self.config.data)

        if path == "/api/update-ytdlp":
            return self._send(200, {"ok": False, "hint": _update_hint()})

        return self._send(404, {"error": "unknown endpoint"})

    # -------------------------------------------------------------- helpers

    def _health(self):
        version = env.installed_version()
        age = env.version_age_days(version)
        js_name, js_path = env.find_js_runtime()
        ffmpeg = env.find_ffmpeg()
        problems = []
        if not version:
            problems.append("ytdlp-missing")
        if age is not None and age > env.STALE_AFTER_DAYS:
            problems.append("ytdlp-stale")
        if not ffmpeg:
            problems.append("ffmpeg-missing")
        if not js_name:
            problems.append("js-missing")
        return {
            "app": __version__,
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "ytdlp": version,
            "ytdlp_age": age,
            "stale_after": env.STALE_AFTER_DAYS,
            "ffmpeg": ffmpeg,
            "js_runtime": js_name,
            "js_path": js_path,
            "pot_provider": env.pot_provider(),
            "download_dir": self.config.download_dir(),
            "problems": problems,
            "update_hint": _update_hint(),
        }

    def _static(self, relpath):
        relpath = relpath.split("?")[0]
        if relpath in ("", "/"):
            relpath = "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, relpath))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)

    # injected by serve()
    manager = None
    config = None
    watcher = None


def _update_hint():
    if os.name == "nt":
        return "py -m pip install -U yt-dlp"
    return "python3 -m pip install -U yt-dlp"


def serve(host="127.0.0.1", port=None, open_browser=None, verbose=False,
          config=None):
    """Start the server. Blocks until interrupted."""
    config = config or Config()
    port = int(port or config.get("port") or 8765)

    manager = downloader.Manager(config)
    Handler.manager = manager
    Handler.config = config
    Handler.watcher = clipboard.Watcher()
    if config.get("watch_clipboard"):
        Handler.watcher.start()

    # If the requested port is in use, fall back to a free one instead of
    # crashing — that's what double-clicking the launcher twice expects.
    httpd = None
    last_err = None
    for attempt_port in (port,):
        try:
            httpd = ThreadingHTTPServer((host, attempt_port), Handler)
            port = attempt_port
            break
        except OSError as e:
            last_err = e
    if httpd is None:
        chosen = None
        for fallback in range(port + 1, port + 50):
            try:
                httpd = ThreadingHTTPServer((host, fallback), Handler)
                port = fallback
                chosen = True
                break
            except OSError as e:
                last_err = e
        if not chosen:
            sys.stderr.write(
                "Could not bind to %s:%s (%s) and no nearby port was free.\n"
                % (host, port, last_err))
            return 1
    httpd.daemon_threads = True
    httpd.verbose = verbose
    # Remember the port we actually ended up on so config reflects reality.
    config.set("port", port)

    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    print("GrabBox %s  ->  %s" % (__version__, url))
    print("Downloads go to: %s" % config.download_dir())
    print(_quick_health(config))
    print("Ctrl+C to stop.")

    should_open = config.get("open_browser") if open_browser is None else open_browser
    if should_open:
        threading.Timer(0.6, lambda: _safe_open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GrabBox.")
    finally:
        httpd.server_close()
    return 0


def _quick_health(config):
    version = env.installed_version()
    age = env.version_age_days(version)
    bits = ["yt-dlp %s" % (version or "MISSING")]
    if age is not None:
        bits[-1] += " (%d days old%s)" % (
            age, "" if age <= env.STALE_AFTER_DAYS else " - UPDATE IT")
    bits.append("ffmpeg %s" % ("ok" if env.find_ffmpeg() else "MISSING"))
    js_name, _ = env.find_js_runtime()
    bits.append("js %s" % (js_name or "MISSING"))
    return "  " + " | ".join(bits)


def _safe_open(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


def free_port(host="127.0.0.1"):
    """An unused port, used when the configured one is taken."""
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="grabbox",
                                description="GrabBox - local download manager")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (0.0.0.0 to reach it from other devices)")
    p.add_argument("--port", "-p", type=int, default=None)
    p.add_argument("--dir", default=None, help="download folder")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window")
    p.add_argument("--watch-clipboard", action="store_true",
                   help="pick up copied links automatically")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    config = Config()
    if args.dir:
        config.set("download_dir", os.path.abspath(os.path.expanduser(args.dir)))
    if args.watch_clipboard:
        config.set("watch_clipboard", True)
    return serve(host=args.host, port=args.port,
                 open_browser=False if args.no_browser else None,
                 verbose=args.verbose, config=config)


if __name__ == "__main__":
    sys.exit(main())
