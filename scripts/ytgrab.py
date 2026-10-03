#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ytgrab - a small, stubborn wrapper around yt-dlp.

Why this exists
---------------
YouTube changes its streaming plumbing often. When it does, an *older* yt-dlp
starts handing out stream URLs that YouTube now refuses with:

    ERROR: unable to download video data: HTTP Error 403: Forbidden

ytgrab fixes that class of problem for you instead of just failing:

  1. It checks how old your yt-dlp is and offers to update it (the #1 cause).
  2. It checks that ffmpeg and a JavaScript runtime (deno/node/bun) are present.
  3. If a download still dies with 403/429, it automatically retries with a
     different YouTube player client and with IPv4, instead of giving up.
  4. If it still fails, it tells you the *one* next thing to try (browser
     cookies) instead of dumping a stack trace on you.

Nothing here is magic: it just runs yt-dlp with the right flags, in the right
order, and keeps going when the first try is blocked.

Usage
-----
    python3 scripts/ytgrab.py                 # interactive menu (easiest)
    python3 scripts/ytgrab.py URL             # menu for one URL
    python3 scripts/ytgrab.py URL --format 5  # straight to mp3, no menu
    python3 scripts/ytgrab.py --doctor        # check yt-dlp / ffmpeg / deno
    python3 scripts/ytgrab.py --update        # update yt-dlp
    python3 scripts/ytgrab.py --list URL      # show available formats

Works on Windows, macOS and Linux. Standard library only.
"""

import argparse
import datetime as _dt
import os
import platform
import re
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------
# settings you may want to tweak
# --------------------------------------------------------------------------

#: how many days old yt-dlp may be before we nag you. yt-dlp itself nags at
#: 90; we nag earlier because a stale build is the most common cause of 403s.
STALE_AFTER_DAYS = 45

#: where files land when you do not pass --dir
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")

#: keep a record of what has already been downloaded so playlists and
#: re-runs never download the same track twice
ARCHIVE_NAME = ".ytgrab-archive.txt"

# --------------------------------------------------------------------------
# tiny helpers
# --------------------------------------------------------------------------

IS_WINDOWS = os.name == "nt"


def say(msg=""):
    print(msg, flush=True)


def rule(char="-", width=64):
    say(char * width)


def find_tool(name):
    """Return the full path to an executable, or None."""
    return shutil.which(name)


def find_ytdlp():
    """
    Return the argv prefix used to call yt-dlp, e.g. ['yt-dlp'] or
    ['python3', '-m', 'yt_dlp']. Returns None if yt-dlp is not installed.
    """
    exe = find_tool("yt-dlp") or find_tool("yt-dlp.exe")
    if exe:
        return [exe]
    # pip-installed but the script directory is not on PATH: fall back to the
    # module form, which works as long as the package is importable.
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def run(argv, **kw):
    """subprocess.run with sane defaults (never raises on non-zero exit)."""
    return subprocess.run(argv, **kw)


def capture(argv):
    """Run and return (returncode, combined_output)."""
    try:
        p = run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True)
        return p.returncode, (p.stdout or "")
    except OSError as exc:  # pragma: no cover - only if the binary vanishes
        return 127, "could not run %s: %s" % (argv[0], exc)


# --------------------------------------------------------------------------
# version / environment checks
# --------------------------------------------------------------------------

VERSION_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")


def ytdlp_version(ytdlp):
    """Return the installed version string, or None."""
    rc, out = capture(ytdlp + ["--version"])
    if rc != 0:
        return None
    return out.strip().splitlines()[-1].strip() if out.strip() else None


def version_age_days(version, today=None):
    """
    Age in days of a YYYY.MM.DD yt-dlp version. Returns None if the version
    string is not a date (e.g. a nightly tag we cannot parse).
    """
    m = VERSION_RE.search(version or "")
    if not m:
        return None
    try:
        stamp = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    today = today or _dt.date.today()
    return (today - stamp).days


def find_js_runtime():
    """
    yt-dlp needs a JavaScript runtime for YouTube (deno by default; node and
    bun also work). Returns (name, path) or (None, None).

    Checks PATH first, then the usual install locations that do not get added
    to PATH by the official installers.
    """
    home = os.path.expanduser("~")
    candidates = ["deno", "node", "bun"]
    for name in candidates:
        path = find_tool(name)
        if path:
            return name, path
    extras = [
        os.path.join(home, ".deno", "bin", "deno.exe" if IS_WINDOWS else "deno"),
        os.path.join(home, ".bun", "bin", "bun.exe" if IS_WINDOWS else "bun"),
        r"C:\Program Files\nodejs\node.exe",
    ]
    for path in extras:
        if os.path.exists(path):
            return os.path.basename(path).split(".")[0], path
    return None, None


def find_ffmpeg():
    return find_tool("ffmpeg") or find_tool("ffmpeg.exe")


def js_runtime_flags():
    """
    Flags that make yt-dlp actually use the JavaScript runtime we found.

    This matters more than it looks: yt-dlp only enables *deno* by default. If
    you have node or bun installed it still reports "JS runtimes: none" and
    YouTube extraction falls back to a single player client (visionos), which
    is far more likely to be blocked with 403. Verified behaviour:

        $ yt-dlp -v ...                              -> JS runtimes: none
        $ yt-dlp -v --js-runtimes node:/path ...     -> JS runtimes: node-22.22.3

    Returns [] when no runtime is present.
    """
    name, path = find_js_runtime()
    if not name:
        return []
    return ["--js-runtimes", "%s:%s" % (name, path)]


def check_environment(ytdlp, quiet=False):
    """
    Print a health report. Returns a dict so callers can react to it.
    """
    report = {"ytdlp": None, "ytdlp_age": None, "ffmpeg": None,
              "js": None, "js_path": None, "problems": []}

    version = ytdlp_version(ytdlp)
    report["ytdlp"] = version
    report["ytdlp_age"] = version_age_days(version) if version else None

    ffmpeg = find_ffmpeg()
    report["ffmpeg"] = ffmpeg

    js_name, js_path = find_js_runtime()
    report["js"] = js_name
    report["js_path"] = js_path

    if quiet:
        return report

    rule("=")
    say("  ytgrab doctor")
    rule("=")
    say("  yt-dlp : %s" % (version or "NOT FOUND"))
    if report["ytdlp_age"] is not None:
        verdict = "fresh" if report["ytdlp_age"] <= STALE_AFTER_DAYS else "STALE"
        say("           %d day(s) old (%s - update if older than %d days)"
            % (report["ytdlp_age"], verdict, STALE_AFTER_DAYS))
    say("  ffmpeg : %s" % (ffmpeg or "NOT FOUND"))
    say("  JS     : %s" % ("%s (%s)" % (js_name, js_path) if js_name else "NOT FOUND"))
    if js_name and js_name != "deno":
        say("           yt-dlp only enables deno by default, so ytgrab passes")
        say("           --js-runtimes %s automatically." % js_name)
    rule("=")

    if version is None:
        report["problems"].append("ytdlp-missing")
        say("  [X] yt-dlp is not installed.")
        say("      install:  python3 -m pip install -U yt-dlp")
    elif report["ytdlp_age"] is not None and report["ytdlp_age"] > STALE_AFTER_DAYS:
        report["problems"].append("ytdlp-stale")
        say("  [!] Your yt-dlp is %d days old. This is the usual cause of"
            % report["ytdlp_age"])
        say('      "HTTP Error 403: Forbidden". Run:  python3 scripts/ytgrab.py --update')

    if not ffmpeg:
        report["problems"].append("ffmpeg-missing")
        say("  [X] ffmpeg not found - audio conversion/merging will fail.")
        if IS_WINDOWS:
            say("      winget install Gyan.FFmpeg   (or drop ffmpeg.exe next to yt-dlp)")
        elif platform.system() == "Darwin":
            say("      brew install ffmpeg")
        else:
            say("      sudo apt install ffmpeg   /   sudo dnf install ffmpeg")

    if not js_name:
        report["problems"].append("js-missing")
        say("  [!] No JavaScript runtime found. YouTube works without one, but")
        say("      yt-dlp falls back to a single player client, which is more")
        say("      likely to be blocked. Install deno:")
        if IS_WINDOWS:
            say("      powershell:  irm https://deno.land/install.ps1 | iex")
        elif platform.system() == "Darwin":
            say("      brew install deno   (or: curl -fsSL https://deno.land/install.sh | sh)")
        else:
            say("      curl -fsSL https://deno.land/install.sh | sh")

    if not report["problems"]:
        say("  [OK] Everything looks good.")
    return report


def update_ytdlp(ytdlp):
    """Try to update yt-dlp the same way it was installed. Returns True on success."""
    say("Updating yt-dlp...")
    before = ytdlp_version(ytdlp)

    # 1. yt-dlp's own updater (works for the standalone .exe / zip / pipx)
    rc, out = capture(ytdlp + ["--update"])
    say(out.strip())
    if rc == 0:
        after = ytdlp_version(ytdlp)
        if after and after != before:
            say("Updated: %s -> %s" % (before, after))
            return True

    # 2. pip fallback (the warning "You installed yt-dlp with pip" means this)
    say("Trying pip...")
    pip_cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    p = run(pip_cmd)
    if p.returncode != 0 and "externally-managed-environment" in (out or ""):
        p = run(pip_cmd + ["--break-system-packages"])
    after = ytdlp_version(ytdlp)
    if after and after != before:
        say("Updated: %s -> %s" % (before, after))
        return True

    say("Could not update automatically (still %s)." % after)
    say("Manual fix:")
    say("  pip install:  python3 -m pip install -U yt-dlp")
    say("  standalone:   download the newest yt-dlp.exe from")
    say("                https://github.com/yt-dlp/yt-dlp/releases/latest")
    return False


# --------------------------------------------------------------------------
# format choices
# --------------------------------------------------------------------------

#: menu number -> (label, extra yt-dlp flags)
#:
#: Note on "flac": YouTube only ever serves *lossy* audio (opus or aac).
#: Putting it in a FLAC container does not bring the lost detail back, so it is
#: labelled honestly below. Use m4a or opus unless you need the .flac filename.
FORMATS = {
    "1": ("Best video + audio (mp4, works on any phone)",
          ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]),
    "2": ("Audio - opus (best quality per MB, small files)",
          ["-f", "bestaudio/best", "-x", "--audio-format", "opus"]),
    "3": ("Audio - flac (lossless container, big files)",
          ["-f", "bestaudio/best", "-x", "--audio-format", "flac"]),
    "4": ("Audio - m4a (great quality, plays everywhere)",
          ["-f", "bestaudio/best", "-x", "--audio-format", "m4a"]),
    "5": ("Audio - mp3 (maximum compatibility)",
          ["-f", "bestaudio/best", "-x", "--audio-format", "mp3",
           "--audio-quality", "0"]),
}

#: what to show in the menu
MENU_ORDER = ["1", "2", "3", "4", "5"]


def is_youtube(url):
    return bool(re.search(
        r"(youtube\.com|youtu\.be|youtube-nocookie\.com|music\.youtube\.com)",
        url or "", re.I))


def base_flags(fmt, playlist, out_dir):
    """Flags that are the same for every attempt."""
    flags = [
        "--no-update",            # we do our own version check, keep output clean
        "--no-playlist" if not playlist else "--yes-playlist",
        "-P", out_dir,
        "-o", "%(playlist_index)02d - %(title)s.%(ext)s" if playlist
              else "%(title)s.%(ext)s",
        "--embed-metadata",
        "--embed-thumbnail",
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "fragment:linear=2:5:15",
    ]
    # Windows only: elsewhere this replaces every non-ASCII character and every
    # space with "_", so real titles come out as My_Song / a row of underscores.
    if os.name == "nt":
        flags += ["--windows-filenames"]
    # make yt-dlp use node/bun too - it only enables deno on its own
    flags += js_runtime_flags()
    if playlist:
        # one bad video should not kill a whole album
        flags += ["--ignore-errors", "--no-abort-on-error",
                  "--download-archive", os.path.join(out_dir, ARCHIVE_NAME)]
    return flags + FORMATS[fmt][1]


# --------------------------------------------------------------------------
# the retry ladder
# --------------------------------------------------------------------------

def build_ladder(url):
    """
    Ordered list of (explanation, extra_flags) attempts.

    Only YouTube needs the client juggling; every other site gets one attempt.
    """
    if not is_youtube(url):
        return [("default settings", [])]
    return [
        ("default player clients", []),
        # The tv client needs no PO token - the most common current fix.
        ("tv client (no PO token needed)",
         ["--extractor-args", "youtube:player_client=tv"]),
        # Skipping the dead android clients and adding web_safari is the older fix.
        # ("default" is not a literal client name — "-android_vr" already means
        # "the default set, minus android_vr".)
        ("skip android clients, add web_safari",
         ["--extractor-args", "youtube:player_client=-android_vr,web_safari"]),
        ("web_embedded + web + tv",
         ["--extractor-args", "youtube:player_client=web_embedded,web,tv"]),
        # Sometimes the CDN route is the problem, not the client.
        ("IPv4 only + tv client",
         ["--force-ipv4", "--extractor-args", "youtube:player_client=tv"]),
    ]


BLOCKED_RE = re.compile(r"403|429|Forbidden|Too Many Requests|"
                        r"Sign in to confirm|not a bot|requested format is not available",
                        re.I)


def diagnose(text):
    """Turn yt-dlp's output into one short, actionable sentence."""
    t = text or ""
    if re.search(r"HTTP Error 403|Forbidden", t):
        return ("YouTube refused the stream URL (403). Cause is almost always an "
                "outdated yt-dlp - update it first, then retry.")
    if re.search(r"HTTP Error 429|Too Many Requests", t):
        return ("Rate limited (429). Wait 10-20 minutes, then download fewer "
                "items at a time.")
    if re.search(r"Sign in to confirm|not a bot", t, re.I):
        return ("YouTube wants a logged-in browser session. Retry with cookies "
                "(menu option 'c').")
    if re.search(r"Requested format is not available", t, re.I):
        return ("That format is not offered for this video. Try m4a (4) or "
                "let yt-dlp pick by choosing 1.")
    if re.search(r"ffmpeg.*(not found|not installed)|Unable to locate ffmpeg", t, re.I):
        return "ffmpeg is missing or not on PATH - see 'python3 scripts/ytgrab.py --doctor'."
    if re.search(r"No supported JavaScript runtime", t, re.I):
        return ("No JS runtime: install deno (see --doctor). YouTube still works "
                "without it but has fewer usable formats.")
    if re.search(r"Video unavailable|Private video|members-only|age-restricted", t, re.I):
        return ("This video is private, age-restricted or members-only. Open it "
                "in a browser, then retry with cookies (menu option 'c').")
    if re.search(r"Unable to download webpage|TLS|SSL|timed out|Temporary failure", t, re.I):
        return "Network problem reaching YouTube - check your connection/VPN/proxy."
    return ""


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

#: rolling buffer of the last lines yt-dlp printed, used to classify failures
_ERROR_TAIL = []


def _last_error_text():
    return "\n".join(_ERROR_TAIL)


def tee_download(ytdlp, argv):
    """
    Run yt-dlp with its output on screen, but remember the last ~40 lines so we
    can classify the failure. Returns the exit code.
    """
    _ERROR_TAIL[:] = []
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, universal_newlines=True,
                            bufsize=1)
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        _ERROR_TAIL.append(line.rstrip("\n"))
        del _ERROR_TAIL[:-40]
    proc.wait()
    return proc.returncode


def download_attempt(ytdlp, url, fmt, out_dir, playlist=False, extra=None,
                     cookies=None):
    """Build the command, show it, stream the output. Returns the exit code."""
    argv = list(ytdlp) + base_flags(fmt, playlist, out_dir)
    if cookies:
        argv += ["--cookies-from-browser", cookies]
    argv += (extra or []) + ["--", url]

    say("")
    say("  $ " + " ".join(a if " " not in a else '"%s"' % a for a in argv[1:]))
    rule()
    return tee_download(ytdlp, argv)


def grab(ytdlp, url, fmt, out_dir, playlist=False):
    """Full download flow with the retry ladder. Returns True on success."""
    os.makedirs(out_dir, exist_ok=True)
    age = version_age_days(ytdlp_version(ytdlp))
    if age is not None and age > STALE_AFTER_DAYS:
        say("")
        say("!! Heads up: your yt-dlp is %d days old. A stale build is the" % age)
        say("   most common cause of 403 errors, so if every attempt below")
        say("   fails, update it (menu 'u') and try again.")
    ladder = build_ladder(url)
    for i, (why, extra) in enumerate(ladder, 1):
        say("")
        say(">> attempt %d/%d: %s" % (i, len(ladder), why))
        rc = download_attempt(ytdlp, url, fmt, out_dir, playlist, extra)
        if rc == 0:
            say("")
            rule("=")
            say("  Done. Files are in:  %s" % out_dir)
            rule("=")
            return True
        hint = diagnose(_last_error_text())
        say("")
        say("!! attempt %d failed.%s" % (i, ("  " + hint) if hint else ""))
    return False


def ask_yes_no(prompt, default=False):
    while True:
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            ans = input(prompt + suffix).strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


# --------------------------------------------------------------------------
# interface
# --------------------------------------------------------------------------

BANNER = r"""
================================
  ytgrab  -  keep your media local
================================"""


def print_menu():
    say("Choose format:")
    for key in MENU_ORDER:
        say("  %s) %s" % (key, FORMATS[key][0]))
    say("  p) Same thing, but for a whole playlist / album")
    say("  c) Retry the last URL with browser cookies (fixes most 403s)")
    say("  l) List available formats for a URL")
    say("  u) Update yt-dlp")
    say("  d) Doctor - check yt-dlp / ffmpeg / deno")
    say("  q) Quit")


def interactive(ytdlp, args):
    out_dir = args.dir
    last_url = None
    last_fmt = "4"

    say(BANNER)
    env = check_environment(ytdlp)
    if "ytdlp-stale" in env["problems"]:
        if ask_yes_no("\nUpdate yt-dlp now? (recommended)", default=True):
            update_ytdlp(ytdlp)
            say("")
    elif "ytdlp-missing" in env["problems"]:
        say("\nyt-dlp is not installed - install it, then run this again.")
        return 1

    while True:
        say("")
        url = last_url or ask("Paste your URL: ")
        if url.lower() in ("q", "quit", "exit"):
            return 0
        if not url:
            continue
        last_url = url

        say("")
        print_menu()
        choice = ask("Choice [1-5]: ").lower()

        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "d":
            check_environment(ytdlp)
            last_url = None
            continue
        if choice == "u":
            update_ytdlp(ytdlp)
            last_url = None
            continue
        if choice == "l":
            run(ytdlp + ["--no-update", "-F", "--", url])
            last_url = None
            continue
        if choice == "c":
            browser = ask("Which browser are you logged into YouTube with? "
                          "(chrome/firefox/edge/brave/safari) ") or "chrome"
            rc = download_attempt(ytdlp, url, last_fmt, out_dir, False,
                                  cookies=browser)
            say("")
            if rc == 0:
                say("  Files in: %s" % out_dir)
            else:
                say("  That did not work either - see docs/TROUBLESHOOTING.md.")
            last_url = None
            continue
        if choice == "p":
            choice = ask("Format for the whole playlist [1-5]: ") or "4"
            if choice not in FORMATS:
                say("  Unknown choice.")
                continue
            last_fmt = choice
            grab(ytdlp, url, choice, out_dir, playlist=True)
            last_url = None
            continue
        if choice not in FORMATS:
            say("  Please type a number between 1 and 5.")
            last_url = None
            continue

        last_fmt = choice
        grab(ytdlp, url, choice, out_dir, playlist=False)
        last_url = None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ytgrab",
        description="Download audio/video with yt-dlp, with automatic retries "
                    "when YouTube blocks a request (403).")
    parser.add_argument("url", nargs="?", help="video or playlist URL")
    parser.add_argument("--format", "-f", dest="fmt", choices=sorted(FORMATS),
                        help="1=mp4 video, 2=opus, 3=flac, 4=m4a, 5=mp3")
    parser.add_argument("--dir", "-o", default=DEFAULT_DIR,
                        help="output folder (default: %(default)s)")
    parser.add_argument("--playlist", "-p", action="store_true",
                        help="treat the URL as a playlist / album")
    parser.add_argument("--cookies", help="browser to take cookies from, e.g. chrome")
    parser.add_argument("--doctor", action="store_true",
                        help="check yt-dlp, ffmpeg and the JS runtime, then exit")
    parser.add_argument("--update", action="store_true",
                        help="update yt-dlp, then exit")
    parser.add_argument("--list", action="store_true",
                        help="list available formats for the URL, then exit")
    args = parser.parse_args(argv)

    ytdlp = find_ytdlp()
    if ytdlp is None:
        say("yt-dlp is not installed.")
        say("  python3 -m pip install -U yt-dlp")
        say("  (or download yt-dlp.exe from "
            "https://github.com/yt-dlp/yt-dlp/releases/latest)")
        return 1

    if args.doctor:
        return 0 if not check_environment(ytdlp)["problems"] else 1
    if args.update:
        return 0 if update_ytdlp(ytdlp) else 1

    if args.url and args.fmt:
        if args.list:
            return run(ytdlp + ["--no-update", "-F", "--", args.url]).returncode
        ok = grab(ytdlp, args.url, args.fmt, args.dir, playlist=args.playlist)
        if not ok and args.cookies is None:
            say("")
            say("Still blocked? The next thing that usually works is your "
                "browser's cookies:")
            say('  python3 scripts/ytgrab.py "%s" --format %s --cookies chrome'
                % (args.url, args.fmt))
        return 0 if ok else 1

    if args.url and args.list:
        return run(ytdlp + ["--no-update", "-F", "--", args.url]).returncode

    return interactive(ytdlp, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nCancelled.")
        sys.exit(130)
