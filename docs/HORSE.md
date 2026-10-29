# 🐎 The Horse — how GrabBox downloads like it means it

GrabBox used to be a polite single-stream wrapper. This document is the
spec for the re-engineered engine — one behaviour, implemented in all
three stacks (Python core, Rust/Tauri desktop, Kotlin Android), modelled
on **IDM** (desktop download managers) and **VidMate/Snaptube** (mobile):

> **Fast like a horse, stubborn like one too: segmented speed up front,
> and a queue that never loses its place.**

## 1. Turbo (the speed half)

| Trick | Where | How |
|---|---|---|
| Multi-connection segments | Direct files (all three stacks) | Up to **16 connections** pull byte ranges into a pre-sized `.part` — `grabbox/horse.py`, `desktop/src-tauri/src/horse.rs`, `android/.../work/Horse.kt`, same algorithm. A server that throttles *per connection* suddenly serves N× faster. Servers that lie about Range support degrade quietly to one stream. When the native path declines (web page, HTTP error), yt-dlp takes over as before. |
| Work-stealing splits | Direct files (all three stacks) | Segmentation is **dynamic**, not fixed (design credit: DownloadCraft's MIT engine — see `NOTICE.md`): one segment to start; a free connection takes orphaned work or **splits the segment that would finish last**, proportionally to measured speeds. A connection that turns out slow keeps a small share instead of parking a fixed quarter of the file. 429/503 answers back off and retry instead of failing the download. |
| Concurrent fragments | Everything yt-dlp serves (all stacks) | `--concurrent-fragments N` + `--http-chunk-size 10M` + 1 MB buffers — HLS/DASH (YouTube & friends) downloads its pieces in parallel instead of one by one. |
| Patient retries | yt-dlp path | 10 retries with **exponential backoff** sleeps instead of hammering a throttling server into a longer ban. |

Measured on a per-connection-throttled local server (the IDM home turf):
**3.97 s single-connection → 1.29 s with 6 segments (3.1×)** —
`python3 -m unittest tests.test_horse -v` reproduces it. And when the
*first* connection is the slow one (60 KB/s vs 900 KB/s for the rest),
work-stealing lands a 6 MB file in **~6 s where fixed split needs ≥26 s**.

Settings (shared keys in `~/.grabbox/config.json`, same file every
engine reads):

- `connections` — segments / concurrent fragments per file, 1–16 (default 8)
- `speed_limit_kbps` — optional cap, 0 = unlimited
- `turbo` — master switch for the two tricks above (default on)
- `concurrency` — how many *jobs* run at once (as before)

## 2. Never dies (the stubborn half)

- **Pause / Resume on every job.** Pausing kills the transfer, not the
  progress: yt-dlp keeps its `.part`; the horse keeps
  `<file>.horse.json`, a segment map (`done` bytes per segment, guarded
  by size + ETag/Last-Modified — a changed file restarts cleanly instead
  of gluing two versions together).
- **Retry = Resume.** Both engines continue partial files, so the Retry
  button on a failed job picks up where it died instead of starting over.
- **The queue persists.** Jobs are snapshotted to `jobs.json` (beside the
  config on desktop/Python, in the app files dir on Android) on every
  state change. After a restart — or a phone reboot — whatever was
  mid-flight comes back **paused**, one tap from continuing. Nothing is
  silently lost, nothing auto-restarts behind your back.
- Android additionally caps itself at **2 parallel downloads**
  (VidMate-style), so one playlist can't eat the whole radio.

## 3. Batch (the VidMate half)

Paste or drop **many links at once** in the desktop/web UI and they all
queue with the defaults — no probe sheet per link. (`POST /api/download`
accepts `{"urls": [...]}`; the Tauri driver and Android share sheet feed
the same shape.) Playlists and single links keep the pick-quality-first
flow.

## 4. Mobile face — the in-app browser

Android gets a **Browse** tab: a WebView with an address/search bar that

- sniffs media requests (`.mp4`, `.m3u8`, `.mp3`, …) as pages load and
  asks the page what `<video>` is actually playing,
- shows a floating **Grab** button (and intercepts tapped download
  links), handing the URL to the same probe → quality → queue flow.

Browsing only *finds*; the engine above does the downloading, so the
browser inherits turbo, pause/resume, and persistence for free.

## 5. What to run to believe it

```bash
python3 -m unittest discover -s tests -v   # horse + manager, incl. the 3.1× bench
python3 -m grabbox                          # the real thing
```

The Rust and Kotlin ports implement the same spec (same config keys,
same yt-dlp flags, same jobs.json shape — the Python and desktop files
are even interchangeable — and now the same native horse: same segment
map on disk, same claim/split algorithm, so a `.part` paused by one
stack can be resumed by another). CI compiles those two; the Python
core is the reference implementation and carries the tests.
