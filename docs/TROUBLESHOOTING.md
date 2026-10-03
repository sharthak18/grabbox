# Troubleshooting

## `ERROR: unable to download video data: HTTP Error 403: Forbidden`

This is the error that started this page. Here is the real log it is based on:

```
WARNING: Your yt-dlp version (2026.03.13) is older than 90 days!
[youtube] OfS1jFck8YQ: Downloading webpage
[youtube] OfS1jFck8YQ: Downloading android vr player API JSON
[info] OfS1jFck8YQ: Downloading 1 format(s): 251
ERROR: unable to download video data: HTTP Error 403: Forbidden
```

### What is actually happening

Nothing is wrong with your link, your folder, or ffmpeg. Two things combine:

1. **Your yt-dlp is old.** `2026.03.13` is 186 days old as of 2026-09-15; the
   newest stable release is `2026.08.19`.
2. **That old version asks YouTube through a client YouTube now blocks.** The
   line `Downloading android vr player API JSON` means yt-dlp picked the
   `android_vr` player client. YouTube started returning 403 for the signed
   stream URLs it hands out. It was reported as
   [yt-dlp issue #17456](https://github.com/yt-dlp/yt-dlp/issues/17456) and
   fixed by removing `android_vr` from the default clients in
   [PR #17461](https://github.com/yt-dlp/yt-dlp/pull/17461), which shipped in
   release **2026.08.19** — the release notes literally say
   *"youtube: Remove `android_vr` from default clients"*.

In 2026.08.19 the defaults are `visionos, web` instead of `android_vr, web`.
So the same link that 403s for you works on a current build.

### Fix 1 — update yt-dlp (fixes this case)

Your warning says *"You installed yt-dlp with pip or using the wheel from
PyPi; Use that to update."* So:

```bash
python -m pip install -U yt-dlp        # Windows: py -m pip install -U yt-dlp
yt-dlp --version                       # must print 2026.08.19 or newer
```

If you installed the standalone `yt-dlp.exe` instead, use its own updater:

```bash
yt-dlp -U
```

Got `error: externally-managed-environment`? Then one of these:

```bash
python -m pip install -U --user --break-system-packages yt-dlp
# or, cleaner:
python -m pip install pipx && python -m pipx install yt-dlp
```

Note that `apt`/`brew` copies of yt-dlp are often months behind. If
`yt-dlp --version` still prints an old date after updating, a package-manager
copy is earlier on your `PATH` and is the one being run.

`ytgrab.py` does this for you: pick **u** in the menu, or run
`python3 scripts/ytgrab.py --update`.

### Fix 2 — install a JavaScript runtime (deno)

Recent yt-dlp only enables **deno** by default. With no JS runtime it warns
*"No supported JavaScript runtime could be found"* and falls back to a single
player client, which is much easier for YouTube to block.

```bash
# Windows (PowerShell)
irm https://deno.land/install.ps1 | iex
# macOS
brew install deno
# Linux / macOS
curl -fsSL https://deno.land/install.sh | sh
```

Already have **node** or **bun**? They work, but yt-dlp will not use them
unless you ask. `ytgrab.py` detects them and passes the flag automatically:

```bash
yt-dlp --js-runtimes node:/usr/bin/node "URL"
```

### Fix 3 — retry with a different player client (the PO-token problem)

Since mid-2026 YouTube also *withholds* HD formats (or 403s) when a request has
no **PO token** - a proof-of-origin value only its own browser JS can make. The
symptom is a download that "succeeds" at 360p, or a 403 on the good formats.
The community-verified order that works now:

```bash
# 1. the tv client needs NO PO token and no account - fixes most cases
yt-dlp --extractor-args "youtube:player_client=tv" -f "bv*+ba/b" "URL"
# 2. skip the dead android clients, add web_safari (no "default," prefix —
#    a leading "-name" already means "remove from the default set")
yt-dlp --extractor-args "youtube:player_client=-android_vr,web_safari" "URL"
# 3. the embedded/tv mix some builds need
yt-dlp --extractor-args "youtube:player_client=web_embedded,web,tv" "URL"
```

`-name` means "remove this client from the default set", so
`-android_vr,web_safari` is "the normal defaults, minus android_vr, plus
web_safari". (Writing `default,-android_vr` is invalid — "default" is not
itself a client name.)

To get the *web* clients at full quality you need a PO-token provider. GrabBox
vendors the `bgutil-ytdlp-pot-provider` plugin (it registers with yt-dlp
automatically), and the retry ladder above is exactly this order.

The clients that exist in 2026.08.19 are:
`tv, tv_downgraded, tv_simply, web, web_safari, web_embedded, web_music,
web_creator, mweb, android, android_vr, ios, visionos`.

> Older blog posts tell you to use `player_client=default,-android_sdkless`.
> That client no longer exists in 2026.08.19 — you will just get
> `WARNING: Skipping unsupported client "android_sdkless"`.

`ytgrab.py` walks this ladder on its own: it tries the defaults, then
`-android_vr + web_safari`, then the TV clients, then IPv4, and tells you what
each attempt was doing.

### Fix 4 — pass your browser cookies

Age-restricted, members-only, or bot-flagged videos need a real session:

```bash
yt-dlp --cookies-from-browser chrome  "URL"   # or firefox / edge / brave / safari
```

Close the browser first, and make sure the video plays in it. In `ytgrab.py`
this is menu option **c**.

### Fix 5 — stop forcing one format

If the 403 only hits format `251`/`400`, let yt-dlp choose again, or check what
is actually on offer:

```bash
yt-dlp -F "URL"        # list formats
yt-dlp    "URL"        # no -f: let yt-dlp pick
```

### Fix 6 — it may be your network

If it works on a phone hotspot but not on your Wi‑Fi, the IP or the route is
the problem, not yt-dlp:

```bash
yt-dlp -4 "URL"        # force IPv4
```

Turn off VPN/proxy, or wait — 429 (`Too Many Requests`) means you need to slow
down, not retry harder.

### Still broken?

Get the full log and file it upstream — that is how these get fixed in days
instead of months:

```bash
yt-dlp -vU "URL"
```

Paste the whole output into <https://github.com/yt-dlp/yt-dlp/issues>.
Meanwhile, `https://github.com/yt-dlp/yt-dlp-nightly-builds/releases` usually
has the fix before the stable release does.

---

## Other errors you will meet

| Message | Cause | Fix |
|---|---|---|
| `'yt-dlp' is not recognized` / `command not found` | Not on PATH | `pip install -U yt-dlp`, or run `python -m yt_dlp` |
| `ffmpeg not found` / `ffprobe not found` | ffmpeg missing | Windows: `winget install Gyan.FFmpeg`; macOS: `brew install ffmpeg`; Linux: `sudo apt install ffmpeg` |
| `Requested format is not available` | You forced a format the video does not have | `yt-dlp -F "URL"`, then pick a real one |
| `Sign in to confirm you're not a bot` | IP/session flagged | `--cookies-from-browser chrome` |
| `HTTP Error 429` | Rate limited | Wait 15+ min, download fewer items per run |
| `WARNING: Your yt-dlp version is older than 90 days` | Stale build | `python -m pip install -U yt-dlp` |
| File plays on PC but not on the phone | Container mismatch | Choose mp4 (1), m4a (4) or mp3 (5) — not opus/flac |

`python3 scripts/ytgrab.py --doctor` checks yt-dlp, ffmpeg and the JS runtime in one
go and tells you which of the above apply to you.

---

## Android: "Engine failed to start — failed to initialize"

The app opens fine, but the red banner is already there and the **Grab**
button is greyed out. Nothing about the phone is wrong: the engine is
yt-dlp plus a Python runtime, both shipped inside the APK as native
libraries, and the app has to unpack them before it can run anything.

### What is actually happening

`youtubedl-android` stores the Python runtime as `libpython.zip.so` and
ffmpeg as `libffmpeg.so`, and during `init()` it reads them straight out of
`ApplicationInfo.nativeLibraryDir`. If the APK stored those `.so` files
*uncompressed* — which is what Android Gradle Plugin 8 does by default —
the installer never extracts them, `nativeLibraryDir` is empty on disk,
and `init()` throws. The library catches it and rethrows a bare
`failed to initialize`, which is the whole message the app ever had to show.

The library's README lists this as a required app-side setting; GrabBox had
it missing.

### Fix — build with extracted native libs

`android/app/build.gradle.kts`:

```kotlin
android {
    packaging {
        jniLibs {
            useLegacyPackaging = true
        }
    }
}
```

and, to keep it enforced no matter what a future plugin does, in
`android/app/src/main/AndroidManifest.xml`:

```xml
<application
    android:extractNativeLibs="true"
    ... >
```

Either one is enough on its own; the app ships both.

### Checking it worked

```bash
# inside the APK: the payload is there either way
unzip -l app-arm64-v8a-debug.apk | grep libpython

# on the device, after install: this is what actually matters
adb shell run-as app.grabbox ls /data/app/*/app.grabbox*/lib/arm64
```

If that listing is empty, the fix is not in the build yet. The app banner
prints the real cause chain underneath the generic line, and **Retry**
re-runs the boot without reinstalling.

---

## Two honest notes

**MP3 is a re-encode.** YouTube only ever serves lossy audio (opus or aac).
Choosing mp3 or flac converts it *again*, so it can only lose quality, never
gain it. `flac` in particular gives you a huge file with no extra detail.
Choose **m4a (4)** or **opus (2)** if you want the best sound per megabyte;
choose mp3 only for old devices that cannot play anything else.

**Keep it personal.** This is for keeping things you have the right to keep —
your own uploads, Creative-Commons material, things you bought, offline copies
of content you are allowed to store. Downloading other people's copyright
material breaks YouTube's terms and, depending on where you live, the law.
