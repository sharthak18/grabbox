"""
Recognise "anything downloadable" from a bare URL.

yt-dlp already knows ~1800 sites, but a plain link to a .zip, a .jpg or an
installer is just an HTTP file. This module asks the server what it is serving
(HEAD, falling back to a ranged GET) and classifies it, so the UI can offer
the right thing instead of guessing.
"""

import mimetypes
import os
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EXT_KIND = {
    # video
    "mp4": "video", "mkv": "video", "webm": "video", "mov": "video",
    "avi": "video", "m4v": "video", "flv": "video", "ts": "video",
    "mpg": "video", "mpeg": "video", "3gp": "video", "wmv": "video",
    # audio
    "mp3": "audio", "m4a": "audio", "aac": "audio", "opus": "audio",
    "ogg": "audio", "oga": "audio", "flac": "audio", "wav": "audio",
    "wma": "audio", "aiff": "audio",
    # images
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "bmp": "image", "svg": "image", "avif": "image",
    "tif": "image", "tiff": "image", "ico": "image", "heic": "image",
    # software / installers
    "exe": "app", "msi": "app", "apk": "app", "dmg": "app", "pkg": "app",
    "deb": "app", "rpm": "app", "appimage": "app", "flatpakref": "app",
    "jar": "app", "ipa": "app", "snap": "app",
    # archives
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive",
    "gz": "archive", "tgz": "archive", "bz2": "archive", "xz": "archive",
    "zst": "archive", "iso": "archive", "img": "archive",
    # documents / other data
    "pdf": "document", "epub": "document", "mobi": "document",
    "txt": "document", "md": "document", "csv": "document",
    "json": "document", "xml": "document", "srt": "document",
    "vtt": "document", "torrent": "other",
}

TYPE_PREFIX_KIND = {
    "video": "video", "audio": "audio", "image": "image",
    "application/pdf": "document",
    "application/zip": "archive", "application/x-7z-compressed": "archive",
    "application/x-rar-compressed": "archive", "application/gzip": "archive",
    "application/x-tar": "archive", "application/x-iso9660-image": "archive",
    "application/vnd.android.package-archive": "app",
    "application/x-msdownload": "app", "application/x-apple-diskimage": "app",
    "application/java-archive": "app", "application/octet-stream": None,
    "text": "document",
}

KIND_ICON = {
    "video": "🎬", "audio": "🎵", "image": "🖼️", "app": "📦",
    "archive": "🗜️", "document": "📄", "other": "⬇️",
}


def ext_of(name):
    """Extension of a filename *or* a URL (query string and fragment ignored)."""
    name = (name or "").split("?", 1)[0].split("#", 1)[0]
    return os.path.splitext(name)[1].lstrip(".").lower()


def kind_of(filename=None, content_type=None):
    """Classify by extension first (it is more reliable than Content-Type)."""
    kind = EXT_KIND.get(ext_of(filename))
    if kind:
        return kind
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct:
        return "other"
    if ct in TYPE_PREFIX_KIND:
        return TYPE_PREFIX_KIND[ct] or "other"
    for prefix, kind in (("video/", "video"), ("audio/", "audio"),
                         ("image/", "image"), ("text/", "document")):
        if ct.startswith(prefix):
            return kind
    return "other"


def filename_from(url, content_disposition=None, content_type=None):
    """Best guess at a filename for a direct link."""
    if content_disposition:
        m = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)", content_disposition, re.I)
        if m:
            return urllib.parse.unquote(m.group(1).strip())
        m = re.search(r'filename="?([^";]+)"?', content_disposition, re.I)
        if m:
            return m.group(1).strip()
    path = urllib.parse.urlparse(url).path
    name = posixpath.basename(path)
    if name and ext_of(name):
        return urllib.parse.unquote(name)
    if name:
        guess = mimetypes.guess_extension((content_type or "").split(";")[0])
        return urllib.parse.unquote(name) + (guess or "")
    return "download"


def _open(url, method, timeout, headers=None):
    """Open a request, returning (status, headers, final_url) or raising.

    Uses a ranged GET for the fallback so we only pull the first byte or so —
    enough to read headers without downloading a whole file.
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    if method == "RANGE":
        req = urllib.request.Request(url, headers=dict(hdrs, Range="bytes=0-0"))
    else:
        req = urllib.request.Request(url, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Consume any tiny body so the connection closes cleanly.
        try:
            resp.read(1024)
        except Exception:
            pass
        return resp.status, resp.headers, resp.geturl()


def probe(url, timeout=15):
    """
    Ask the server about a direct link. Returns a dict; never raises.

    Tries HEAD first; if the server refuses HEAD (403/405/501) or drops the
    connection, falls back to a ranged GET (bytes=0-0) so we can read the
    response headers without pulling a whole file.

    ``{'ok', 'url', 'final_url', 'status', 'kind', 'filename', 'size',
       'content_type'}``
    """
    out = {"ok": False, "url": url, "final_url": url, "status": None,
           "kind": "other", "filename": None, "size": None, "content_type": None,
           "error": None}
    headers = None
    last_err = None
    for method in ("HEAD", "RANGE"):
        try:
            status, headers, final_url = _open(url, method, timeout)
            out.update(status=status, final_url=final_url)
            break
        except urllib.error.HTTPError as exc:
            # Read headers from the error response too — e.g. S3 returns 403
            # but still sets Content-Type/Length.
            headers = exc.headers
            out.update(status=exc.code, final_url=exc.url or url)
            if exc.code in (403, 405, 501) and method == "HEAD":
                # HEAD refused — try the ranged GET fallback, but keep the
                # headers we did get if the fallback also fails.
                last_err = "HTTP %s" % exc.code
                continue
            out["error"] = "HTTP %s" % exc.code
            break
        except Exception as exc:
            last_err = str(exc)
            if method == "HEAD":
                continue
            out["error"] = last_err
            return out
    if headers is None:
        out["error"] = out["error"] or last_err or "no response"
        return out

    ctype = headers.get("Content-Type")
    cdisp = headers.get("Content-Disposition")
    length = headers.get("Content-Length")
    # For ranged GET, Content-Range "bytes 0-0/12345" tells us the real size.
    crange = headers.get("Content-Range")
    out["content_type"] = ctype
    out["filename"] = filename_from(out["final_url"], cdisp, ctype)
    out["kind"] = kind_of(out["filename"], ctype)
    size = None
    try:
        size = int(length) if length else None
    except ValueError:
        size = None
    if size is None and crange:
        m = re.search(r"/(\d+)", crange)
        if m:
            try:
                size = int(m.group(1))
            except ValueError:
                size = None
    out["size"] = size
    out["ok"] = out["status"] is not None and out["status"] < 400
    return out


def looks_like_page(url):
    """True when the URL is probably a web page rather than a file."""
    if re.search(r"\.(%s)(\?|#|$)" % "|".join(EXT_KIND), url, re.I):
        return False
    host = urllib.parse.urlparse(url).netloc.lower()
    return not host.startswith(("cdn.", "dl.", "download.", "files."))


def human_size(n):
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0
    return ""
