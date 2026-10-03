"""
Download engine: wraps the yt-dlp Python API in jobs the UI can watch.

One job = one URL. A job walks the same retry ladder the CLI uses, so a 403 on
one player client is not the end of the download - it just moves to the next
rung. Everything runs on background threads; the UI polls /api/jobs.
"""

import itertools
import os
import threading
import time
import traceback
import uuid

from . import sniff, ytdlp_env as env

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError, sanitize_filename
except ImportError:  # pragma: no cover - surfaced through /api/health
    yt_dlp = None
    DownloadError = Exception

    def sanitize_filename(s, **kw):
        return s


_counter = itertools.count(1)


class Job(object):
    """A single download, with everything the UI needs to render it."""

    __slots__ = ("id", "url", "title", "kind", "status", "percent", "speed",
                 "eta", "filename", "path", "error", "hint", "attempt",
                 "attempts_total", "log", "created", "finished", "playlist",
                 "items_done", "items_total", "cancel", "thumb", "size",
                 "direct")

    def __init__(self, url, kind="file", playlist=False):
        self.id = "j%d-%s" % (next(_counter), uuid.uuid4().hex[:6])
        self.url = url
        self.title = url
        self.kind = kind
        self.status = "queued"        # queued|running|done|error|canceled
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.filename = None
        self.path = None
        self.error = None
        self.hint = None
        self.attempt = 1
        self.attempts_total = 1
        self.log = []
        self.created = time.time()
        self.finished = None
        self.playlist = playlist
        self.items_done = 0
        self.items_total = 0
        self.cancel = False
        self.thumb = None
        self.size = None
        self.direct = None      # cached sniff.probe() result

    def note(self, msg):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
        del self.log[:-40]

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__ if k != "cancel"}


def _format_selector(kind, quality):
    """Turn the UI's choice into a yt-dlp format selector + post-processing."""
    if kind == "audio":
        fmt = "bestaudio/best"
        opts = {"format": fmt, "extract_audio": True}
        # "best" = keep the original audio stream; do not re-encode, so
        # don't pass audioformat (yt-dlp rejects audioformat="best").
        if quality and quality != "best":
            opts["audioformat"] = quality
        else:
            opts["audioformat"] = "m4a"
        if quality == "mp3":
            opts["audioquality"] = "0"
        return opts
    if kind == "video":
        if quality in ("2160", "1440", "1080", "720", "480", "360"):
            h = quality
            selector = ("bv*[height<=%s]+ba/b[height<=%s]/bv*+ba/b" % (h, h))
        else:
            selector = "bv*+ba/b"
        return {"format": selector, "merge_output_format": "mp4"}
    # "file": let yt-dlp pick, which is right for direct links and archives
    return {"format": "b"}


def _looks_like_final_name(name, kind):
    """True when ``name`` already ends in a plausible extension for ``kind``."""
    if not name:
        return False
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    if not ext:
        return False
    # Direct-file kind: if the user picked any real-looking extension, keep it.
    if kind == "file":
        return ext in _ALL_KNOWN_EXTS
    # For audio/video, only treat known matching extensions as final.
    if kind == "audio":
        return ext in ("mp3", "m4a", "aac", "opus", "ogg", "oga", "flac",
                       "wav", "wma", "aiff")
    if kind == "video":
        return ext in ("mp4", "mkv", "webm", "mov", "avi", "m4v", "flv",
                       "ts", "mpg", "mpeg", "3gp", "wmv")
    return False


_ALL_KNOWN_EXTS = {
    "mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts", "mpg", "mpeg",
    "3gp", "wmv", "mp3", "m4a", "aac", "opus", "ogg", "oga", "flac", "wav",
    "wma", "aiff", "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif",
    "tif", "tiff", "ico", "heic", "exe", "msi", "apk", "dmg", "pkg", "deb",
    "rpm", "appimage", "flatpakref", "jar", "ipa", "snap", "zip", "rar",
    "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "img", "pdf",
    "epub", "mobi", "txt", "md", "csv", "json", "xml", "srt", "vtt",
}


def direct_filename(url, kind, cached=None):
    """
    Exact filename for a plain file link, or None.

    yt-dlp's generic extractor cannot know that
    ``.../six-1.17.0.tar.gz`` served as ``binary/octet-stream`` should keep
    that name - left alone it writes ``six-1.17.0.tar.unknown_video``. The
    server already told us the real name during the HTTP probe, so use it.
    """
    if kind != "file":
        return None
    info = cached or sniff.probe(url)
    name = (info or {}).get("filename")
    if name and os.path.splitext(name)[1]:
        return sanitize_filename(name)
    return None


class Manager(object):
    """Owns the job table and the worker threads."""

    def __init__(self, config):
        self.config = config
        self.jobs = {}
        self._lock = threading.Lock()
        self._concurrency = int(config.get("concurrency", 2))
        self._sem = threading.Semaphore(self._concurrency)

    def set_concurrency(self, n):
        """Adjust the semaphore so at most ``n`` jobs run in parallel.

        Threading.Semaphore doesn't expose a setter, so we swap it. Already
        running jobs keep their slot (they hold a permit on the old semaphore,
        which they release when they finish — harmless).
        """
        n = max(1, int(n or 1))
        with self._lock:
            self._concurrency = n
            self._sem = threading.Semaphore(n)

    # ------------------------------------------------------------------ api

    def add(self, url, kind="file", quality=None, playlist=False,
            directory=None, cookies=None, filename=None):
        job = Job(url, kind, playlist)
        job.attempts_total = len(env.client_ladder(url))
        with self._lock:
            self.jobs[job.id] = job
        args = (job, quality, directory, cookies, filename)
        t = threading.Thread(target=self._worker, args=args,
                             name="grabbox-" + job.id, daemon=True)
        t.start()
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            job.cancel = True
            job.note("cancel requested")
            return True
        return False

    def list(self):
        with self._lock:
            return [j.to_dict() for j in self.jobs.values()]

    def clear_finished(self):
        with self._lock:
            for jid in [j.id for j in self.jobs.values()
                        if j.status in ("done", "error", "canceled")]:
                del self.jobs[jid]

    # ------------------------------------------------------------- internal

    def _worker(self, job, quality, directory, cookies, filename):
        with self._sem:
            if job.cancel:
                job.status = "canceled"
                job.finished = time.time()
                return
            job.status = "running"
            outdir = directory or self.config.download_dir()
            ladder = env.client_ladder(job.url)
            job.attempts_total = len(ladder)
            last_error = None
            for i, (label, extractor_args, extra) in enumerate(ladder, 1):
                job.attempt = i
                job.note("attempt %d/%d: %s" % (i, len(ladder), label))
                try:
                    path = self._run(job, quality, outdir, cookies, filename,
                                     extractor_args, extra)
                except _Canceled:
                    job.status = "canceled"
                    job.finished = time.time()
                    return
                except Exception as exc:
                    last_error = exc
                    text = "%s\n%s" % (exc, traceback.format_exc())
                    job.error = str(exc)
                    job.hint = env.diagnose(text)
                    job.note("attempt %d failed: %s" % (i, str(exc)[:200]))
                    continue
                job.status = "done"
                job.percent = 100.0
                job.path = path
                job.finished = time.time()
                job.error = None
                job.hint = None
                job.note("saved: %s" % path)
                return
            job.status = "error"
            job.finished = time.time()
            job.note("all attempts failed")

    def _base_opts(self, job, quality, outdir, cookies, filename,
                   extractor_args, extra):
        playlist = job.playlist
        outtmpl = ("%(playlist_index)02d - %(title)s.%(ext)s" if playlist
                   else "%(title)s.%(ext)s")
        if filename:
            safe = sanitize_filename(filename)
            # If the user already gave a name that ends in a known extension
            # for this kind, trust it and leave %(ext)s off so we don't end up
            # with "song.mp3.mp3". Otherwise let yt-dlp append the real ext.
            if _looks_like_final_name(safe, job.kind):
                outtmpl = safe
            else:
                outtmpl = safe + ".%(ext)s"
        elif job.kind == "file":
            if job.direct is None:
                job.direct = sniff.probe(job.url)   # one probe, then cached
            literal = direct_filename(job.url, job.kind, job.direct)
            if literal:
                outtmpl = literal

        opts = {
            "paths": {"home": outdir},
            "outtmpl": {"default": outtmpl},
            "restrictfilenames": False,
            # Windows only - elsewhere it replaces every non-ASCII character
            # and every space with "_", mangling real titles into My_Song and
            # non-Latin ones into a row of underscores.
            "windowsfilenames": os.name == "nt",
            "noplaylist": not playlist,
            "ignoreerrors": "only_download" if playlist else False,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 10,
            "fragment_retries": 10,
            "embedmetadata": True,
            "embedthumbnail": job.kind != "file",
            "progress_hooks": [self._hook(job)],
            "postprocessor_hooks": [self._pp_hook(job)],
        }
        opts.update(_format_selector(job.kind, quality))
        opts.update(env.js_runtime_args())
        if extractor_args:
            opts["extractor_args"] = extractor_args
        opts.update(extra or {})
        if cookies:
            opts["cookiesfrombrowser"] = (cookies,)
        if playlist:
            archive = os.path.join(outdir, ".grabbox-archive.txt")
            opts["download_archive"] = archive
        loc = env.ffmpeg_location()
        if loc:
            opts["ffmpeg_location"] = loc
        return opts

    def _run(self, job, quality, outdir, cookies, filename, extractor_args,
             extra):
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        os.makedirs(outdir, exist_ok=True)
        opts = self._base_opts(job, quality, outdir, cookies, filename,
                               extractor_args, extra)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job.url, download=True)
        if not info:
            raise RuntimeError("no information returned for %s" % job.url)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            paths = [e.get("filepath") for e in entries if e and e.get("filepath")]
            return paths[-1] if paths else outdir
        title = info.get("title") or job.url
        job.title = title
        # For plain file links yt-dlp returns no "filepath" at all; the
        # MoveFiles post-processor hook is what knows the final name, and it
        # has already stored it on the job.
        for candidate in (info.get("filepath"), job.path):
            if candidate and os.path.exists(candidate):
                return candidate
        return info.get("filepath") or job.path or outdir

    # --------------------------------------------------------------- hooks

    def _hook(self, job):
        def hook(d):
            if job.cancel:
                raise _Canceled()
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                if total:
                    job.percent = round(100.0 * done / total, 1)
                    job.size = total
                job.speed = d.get("speed")
                job.eta = d.get("eta")
                job.filename = os.path.basename(d.get("filename") or "") or job.filename
            elif status == "finished":
                job.percent = 100.0
                job.speed = None
                job.eta = None
                if d.get("filename"):
                    job.filename = os.path.basename(d["filename"])
                    job.path = d["filename"]
        return hook

    def _pp_hook(self, job):
        def hook(d):
            if d.get("status") == "finished" and d.get("info_dict"):
                path = d["info_dict"].get("filepath")
                if path:
                    job.path = path
                    job.filename = os.path.basename(path)
            if job.playlist:
                info = d.get("info_dict") or {}
                if info.get("playlist_index"):
                    job.items_done = max(job.items_done, info["playlist_index"])
                if info.get("n_entries"):
                    job.items_total = info["n_entries"]
        return hook


class _Canceled(Exception):
    pass


# ------------------------------------------------------------------ probing

#: kinds a bare HTTP probe can settle on its own; anything else (a web page,
#: an unknown stream) goes to yt-dlp's extractors.
DIRECT_KINDS = ("video", "audio", "image", "app", "archive")


def probe_url(url, cookies=None):
    """
    Everything the UI needs to show before downloading: what this link is, and
    which qualities exist. Never raises - failures come back as a dict.
    """
    result = {"ok": False, "url": url, "kind": "other", "title": url,
              "formats": [], "entries": [], "is_playlist": False, "thumb": None,
              "duration": None, "uploader": None, "direct": None,
              "error": None, "hint": None, "note": None}

    # A HEAD request settles direct files (images, installers, archives) without
    # touching an extractor, and tells us the size up front.
    direct = sniff.probe(url)
    if direct["ok"] and direct["kind"] in DIRECT_KINDS:
        result.update(ok=True, kind=direct["kind"], direct=direct,
                      title=direct["filename"] or url)
        return result

    if yt_dlp is None:
        result["error"] = "yt-dlp is not installed"
        result["hint"] = "python3 -m pip install -U yt-dlp"
        return result

    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "extract_flat": "in_playlist", "skip_download": True,
            "playlist_items": "1-100"}
    opts.update(env.js_runtime_args())
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        result["error"] = str(exc)
        result["hint"] = env.diagnose(str(exc))
        if direct["ok"]:      # yt-dlp could not handle it, but HTTP could
            result.update(ok=True, kind=direct["kind"], direct=direct,
                          title=direct["filename"] or url)
        return result

    if not info:
        result["error"] = "no information for this URL"
        if direct["ok"]:
            result.update(ok=True, kind=direct["kind"], direct=direct,
                          title=direct["filename"] or url)
        return result

    if info.get("_type") == "playlist":
        entries = []
        for e in list(info.get("entries") or [])[:100]:
            if not e:
                continue
            entries.append({"title": e.get("title") or e.get("id"),
                            "url": e.get("url") or e.get("webpage_url"),
                            "duration": e.get("duration")})
        result.update(ok=True, is_playlist=True, kind="video",
                      title=info.get("title") or url, entries=entries)
        return result

    formats = []
    for f in info.get("formats") or []:
        vcodec, acodec = f.get("vcodec"), f.get("acodec")
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "kind": "video" if has_video else ("audio" if has_audio else "other"),
            "height": f.get("height"),
            "note": f.get("format_note") or f.get("format"),
            "size": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": vcodec, "acodec": acodec,
            "abr": f.get("abr"), "tbr": f.get("tbr"),
        })
    best_audio = [f for f in formats if f["kind"] == "audio"]
    has_video = any(f["kind"] == "video" for f in formats)

    # If YouTube only offered <=360p for a video, the HD formats are being
    # withheld behind a PO token (SABR). Warn the UI; the retry ladder and the
    # vendored POT provider are the recovery path.
    note = None
    if env.is_youtube(url) and has_video:
        heights = [f["height"] for f in formats if f["height"]]
        if heights and max(heights) <= 360:
            note = ("Only %dp was offered - YouTube is withholding HD formats "
                    "(PO token / SABR). GrabBox retries with token-free clients "
                    "and uses the bundled POT plugin where possible."
                    % max(heights))

    result.update(
        ok=True,
        note=note,
        title=info.get("title") or url,
        kind="video" if has_video else ("audio" if best_audio else "other"),
        formats=formats,
        thumb=info.get("thumbnail"),
        duration=info.get("duration"),
        uploader=info.get("uploader") or info.get("channel"),
    )
    return result
