"""
Shared yt-dlp environment knowledge.

Everything in here exists because of a specific, reproducible yt-dlp behaviour:

* yt-dlp only enables **deno** as a JavaScript runtime by default. With node or
  bun installed it still reports "JS runtimes: none", YouTube extraction falls
  back to a single player client, and 403s get much more likely.
* YouTube began rejecting the ``android_vr`` player client; yt-dlp 2026.08.19
  removed it from the defaults (PR #17461 / issue #17456). Older builds still
  ask for it and get "HTTP Error 403: Forbidden".
"""

import datetime as _dt
import os
import re
import shutil
import sys

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

#: nag about a build older than this
STALE_AFTER_DAYS = 45

VERSION_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")


def installed_version():
    """Version of the importable yt_dlp package, or None."""
    try:
        from yt_dlp.version import __version__ as v
    except Exception:
        return None
    return v


def version_age_days(version, today=None):
    """Age in days of a YYYY.MM.DD version string, or None if unparseable."""
    m = VERSION_RE.search(version or "")
    if not m:
        return None
    try:
        stamp = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return ((today or _dt.date.today()) - stamp).days


def find_tool(name):
    return shutil.which(name) or shutil.which(name + ".exe")


def find_ffmpeg():
    """
    Path to an ffmpeg binary: PATH first, then the static copy bundled with
    the (optional) ``imageio-ffmpeg`` package, which we vendor so the app can
    convert audio even on machines without a system ffmpeg.
    """
    path = find_tool("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _ffmpeg_binary_name():
    return "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"


def _ffprobe_binary_name():
    return "ffprobe.exe" if IS_WINDOWS else "ffprobe"


def ffmpeg_location():
    """
    A directory yt-dlp can use as ``ffmpeg_location``: it expects a directory
    containing binaries literally named ``ffmpeg`` and ``ffprobe``. When the
    binary we found has some other name (e.g. the static builds shipped by
    ``imageio-ffmpeg`` are named ``ffmpeg-linux-x86_64-v7.0.2``), we create
    same-name symlinks (the static build is multi-call: invoked as ``ffprobe``
    it behaves as ffprobe). If symlinking fails (Windows without Developer
    Mode, or a read-only filesystem), we copy the binary instead. Returns
    None when there is no ffmpeg at all.
    """
    real = find_ffmpeg()
    if not real:
        return None
    real_dir = os.path.dirname(os.path.abspath(real)) or None
    want = _ffmpeg_binary_name()
    if os.path.basename(real) == want:
        # Already a well-named binary; its directory is the right answer as
        # long as ffprobe is next to it. On many distros (and in the imageio
        # vendor dir) it is. If not, we still want the shim.
        ffprobe = os.path.join(real_dir, _ffprobe_binary_name())
        if os.path.exists(ffprobe) or _is_multicall_ffmpeg(real):
            return real_dir
    shim = os.path.join(os.path.expanduser("~"), ".grabbox", "bin")
    try:
        os.makedirs(shim, exist_ok=True)
        for name in (want, _ffprobe_binary_name()):
            link = os.path.join(shim, name)
            if os.path.exists(link):
                continue
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                # Symlinks aren't always available (Windows). Copy instead.
                import shutil as _shutil
                try:
                    _shutil.copy2(real, link)
                    try:
                        os.chmod(link, 0o755)
                    except OSError:
                        pass
                except OSError:
                    return real_dir
        return shim
    except OSError:
        return real_dir


def _is_multicall_ffmpeg(path):
    """Best-effort: imageio-ffmpeg builds are multi-call (ffmpeg/ffprobe)."""
    try:
        bn = os.path.basename(path).lower()
        return bn.startswith("ffmpeg-") or "imageio_ffmpeg" in path.replace("\\", "/").lower()
    except Exception:
        return False


def find_js_runtime():
    """
    Return (name, path) for a JavaScript runtime, or (None, None).

    PATH first, then the locations the official installers use but do not add
    to PATH (``~/.deno/bin``, ``~/.bun/bin``, ``C:\\Program Files\\nodejs``).
    """
    home = os.path.expanduser("~")
    for name in ("deno", "node", "bun"):
        path = find_tool(name)
        if path:
            return name, path
    for path in (
        os.path.join(home, ".deno", "bin", "deno.exe" if IS_WINDOWS else "deno"),
        os.path.join(home, ".bun", "bin", "bun.exe" if IS_WINDOWS else "bun"),
        r"C:\Program Files\nodejs\node.exe",
    ):
        if os.path.exists(path):
            return os.path.basename(path).split(".")[0], path
    return None, None


def js_runtime_args():
    """
    ``YoutubeDL`` option fragment that makes yt-dlp use the runtime we found.

    The Python API takes a dict of ``{runtime: {config}}`` - not the
    ``--js-runtimes name:path`` string form used on the command line (that
    raises "Invalid js_runtimes format"). Returns ``{}`` when no runtime is
    installed.
    """
    name, path = find_js_runtime()
    if not name:
        return {}
    return {"js_runtimes": {name: {"path": path}}}


def js_runtime_cli():
    """Same thing as command-line flags (used by the bundled ytgrab.py CLI)."""
    name, path = find_js_runtime()
    if not name:
        return []
    return ["--js-runtimes", "%s:%s" % (name, path)]


def pot_provider():
    """
    Name of a vendored PO-token provider plugin, or None.

    YouTube now withholds HD formats (and 403s some clients) unless a
    proof-of-origin token is supplied. The bgutil plugin, when present on
    sys.path, registers itself with yt-dlp automatically - we just report it.
    """
    try:
        import importlib
        importlib.import_module("yt_dlp_plugins.extractor.getpot_bgutil")
        return "bgutil"
    except Exception:
        return None


#: Player clients that exist in yt-dlp 2026.08.19 (verified against
#: INNERTUBE_CLIENTS). ``android_sdkless`` was removed long ago; old blog
#: advice to use ``player_client=default,-android_sdkless`` now only prints a
#: warning. Note that ``default`` is NOT itself a client — leading entries
#: that start with ``-`` are applied on top of the default client set, so
#: ``-android_vr,web_safari`` means "defaults, minus android_vr, plus
#: web_safari" and ``default,-android_vr`` is invalid (it tries to select a
#: client literally named "default" which does not exist).
KNOWN_CLIENTS = (
    "tv", "tv_downgraded", "tv_simply", "web", "web_safari", "web_embedded",
    "web_music", "web_creator", "mweb", "android", "android_vr", "ios",
    "visionos",
)


def is_youtube(url):
    return bool(re.search(
        r"(youtube\.com|youtu\.be|youtube-nocookie\.com|music\.youtube\.com)",
        url or "", re.I))


def client_ladder(url):
    """
    Ordered list of ``(label, extractor_args, extra_opts)`` rungs to try when a
    site blocks the request. A leading ``-name`` means "drop this client from
    the default set" (no ``default`` literal is needed), so ``-android_vr``
    means "the normal defaults, minus android_vr".
    """
    if not is_youtube(url):
        return [("default", {}, {})]
    # Order follows what the community verified working in Aug-Sep 2026
    # (r/youtubedl, yt-dlp#17456, yt-dlp.net): the tv client needs no PO token,
    # and the bgutil POT provider (vendored here) lets the web clients work at
    # full quality when it can run.
    return [
        ("default clients (PO-token plugin active if installed)", {}, {}),
        ("tv client - needs no PO token",
         {"youtube": {"player_client": ["tv"]}}, {}),
        ("skip android clients, add web_safari",
         {"youtube": {"player_client": ["-android_vr", "web_safari"]}}, {}),
        ("web_embedded + web + tv",
         {"youtube": {"player_client": ["web_embedded", "web", "tv"]}}, {}),
        ("IPv4 only, tv client",
         {"youtube": {"player_client": ["tv"]}},
         {"force_ipv4": True}),
    ]


def diagnose(text):
    """One actionable sentence for the failure yt-dlp just reported."""
    t = text or ""
    if re.search(r"SABR|missing a url|PO Token|po_token", t, re.I):
        return ("YouTube is withholding formats (SABR / PO token). GrabBox "
                "retries with token-free clients; the vendored POT plugin "
                "restores full web quality where it can run.")
    if re.search(r"HTTP Error 403|Forbidden", t):
        return ("YouTube refused the stream URL (403). Almost always an "
                "outdated yt-dlp - update it, then retry.")
    if re.search(r"HTTP Error 429|Too Many Requests", t):
        return "Rate limited (429). Wait 10-20 minutes and download less at once."
    if re.search(r"Sign in to confirm|not a bot", t, re.I):
        return "YouTube wants a logged-in session. Enable browser cookies and retry."
    if re.search(r"Requested format is not available", t, re.I):
        return "That format is not offered for this item. Pick another quality."
    if re.search(r"ffmpeg.*(not found|not installed)|Unable to locate ffmpeg", t, re.I):
        return "ffmpeg is missing - audio conversion and merging need it."
    if re.search(r"Video unavailable|Private video|members-only|age-restricted", t, re.I):
        return "Private, age-restricted or members-only. Enable browser cookies."
    if re.search(r"Unable to download webpage|TLS|SSL|timed out|Temporary failure", t, re.I):
        return "Network problem reaching the site - check connection/VPN/proxy."
    return ""
