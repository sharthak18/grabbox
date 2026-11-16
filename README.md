<h1 align="center">GrabBox</h1>

<p align="center">
  <b>Paste a link. Pick a quality. Download.</b><br>
  Music, videos, whole playlists — from YouTube and 1800+ other sites — saved on <i>your</i> device.<br>
  Everything is bundled inside the app. Nothing else to install.
</p>

<p align="center">
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Windows-Setup.exe"><img alt="Download for Windows" src="https://img.shields.io/badge/Windows-Download-0078D6?style=for-the-badge&logoColor=white&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI%2BPHBhdGggZmlsbD0iI2ZmZiIgZD0iTTAgMGgxMXYxMUgwem0xMyAwaDExdjExSDEzek0wIDEzaDExdjExSDB6bTEzIDBoMTF2MTFIMTN6Ii8%2BPC9zdmc%2B"></a>
  &nbsp;
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-macOS-AppleSilicon.dmg"><img alt="Download for macOS" src="https://img.shields.io/badge/macOS-Download-000000?style=for-the-badge&logo=apple&logoColor=white"></a>
  &nbsp;
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Linux.AppImage"><img alt="Download for Linux" src="https://img.shields.io/badge/Linux-Download-FCC624?style=for-the-badge&logo=linux&logoColor=black"></a>
  &nbsp;
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Android.apk"><img alt="Download for Android" src="https://img.shields.io/badge/Android-Download-3DDC84?style=for-the-badge&logo=android&logoColor=black"></a>
</p>

<p align="center">
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest"><img alt="Latest version" src="https://img.shields.io/github/v/release/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-?label=latest&style=flat-square"></a>
  <img alt="Downloads" src="https://img.shields.io/github/downloads/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/total?style=flat-square">
  <a href="https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases"><img alt="All releases" src="https://img.shields.io/badge/all%20downloads-Releases%20page-555?style=flat-square"></a>
</p>

---

## Download

Every file lives on the **[Releases page](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest)**.
The links below always point at the newest version.

| Your device | Download | Good to know |
|---|---|---|
| **Windows** 10 / 11 | [GrabBox-Windows-Setup.exe](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Windows-Setup.exe) | Prefer an MSI? [GrabBox-Windows.msi](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Windows.msi) |
| **Mac** — Apple Silicon (M1 · M2 · M3 · M4) | [GrabBox-macOS-AppleSilicon.dmg](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-macOS-AppleSilicon.dmg) | Any Mac sold since late 2020 |
| **Mac** — Intel | [GrabBox-macOS-Intel.dmg](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-macOS-Intel.dmg) | Not sure? Go Apple menu → *About This Mac* → *Chip* |
| **Linux** | [GrabBox-Linux.AppImage](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Linux.AppImage) | No install needed. Ubuntu / Debian / Mint can use the [.deb](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Linux.deb) instead |
| **Android** 7.0 or newer | [GrabBox-Android.apk](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Android.apk) | Very old 32-bit phone? [GrabBox-Android-32bit.apk](https://github.com/sharthak18/A-WAY-TO-DOWNLOAD-YOUTUBE-AUDIO-/releases/latest/download/GrabBox-Android-32bit.apk) |

**yt-dlp, ffmpeg and a JavaScript runtime are packed inside** the app. Install it, open it, done.
If YouTube changes something months from now, *Settings → Update yt-dlp* fixes it without reinstalling.

### First launch — the one warning you will see

The app is free and open source but not (yet) code-signed, so your OS asks once:

* **Windows** — SmartScreen says "Windows protected your PC": click **More info → Run anyway**.
* **macOS** — "cannot be opened because Apple cannot check it": **right-click the app → Open → Open**
  (or *System Settings → Privacy & Security → Open Anyway*).
* **Android** — tap **Settings** on the "Install unknown apps" prompt, allow it for your browser, go back, tap **Install**.
* **Linux** — AppImage: make it executable first (`chmod +x GrabBox-Linux.AppImage`, or right-click → *Properties → Allow executing*).

## How to use it

1. **Open GrabBox** and **paste a link** — a video, a song, a playlist, an album.
   On Android you can also use **Share → GrabBox** from YouTube or your browser.
2. GrabBox reads the link and shows what it is. **Pick what you want**: video (MP4), or audio as
   MP3 / M4A / Opus / FLAC, and the quality. Rename it if you like.
3. Hit **Download**. Progress shows in the queue; files land in **Downloads → GrabBox**
   (changeable in Settings) with the title as the file name, cover art and artist info embedded.

Playlists work the same way — paste the playlist link and every track is numbered in order.

## Also in the box

<details>
<summary><b>Torrents</b> — magnet links and .torrent files in the same queue</summary>

Paste a `magnet:` link into the grab bar (or hit the paperclip to pick a `.torrent` file)
and it joins the same list as everything else — pause, resume, retry, top speed, piece
count, all live. Downloads land in your usual GrabBox folder, state persists across
restarts, and **“Clear finished”** tidies completed rows without ever deleting files.

*Desktop app first* (powered by [librqbit](https://github.com/ikatson/rqbit), Apache-2.0).
The Python server and Android build can't do torrents yet — they'll say so plainly instead
of failing silently. Details: [docs/TORRENTS.md](docs/TORRENTS.md).
</details>

<details>
<summary><b>Browser extension</b> — download from the page you are on</summary>

A toolbar icon that lights up when a page has something worth grabbing, a right-click
"Grab with GrabBox", and a hover button on videos / images / file links. It hands the
link to the desktop app, so install that first.

In the desktop app open *Settings → Add the browser extension*; it shows the folder to
load. Or manually: `chrome://extensions` → *Developer mode* → *Load unpacked* → the
`extension/` folder of this repo. Firefox: `about:debugging#/runtime/this-firefox` →
*Load Temporary Add-on*. Details in [docs/APP.md](docs/APP.md#2-browser-extension-chrome--edge--brave--firefox).
</details>

<details>
<summary><b>Run it from source</b> — Python, no installer</summary>

```bash
python3 -m pip install -U yt-dlp        # plus ffmpeg from your package manager
python3 -m grabbox                      # opens the same UI in your browser
```

`python3 -m grabbox --host 0.0.0.0` makes it reachable from every phone and tablet on
your Wi-Fi. Double-click launchers for Windows / macOS / Linux are in
[`scripts/`](scripts/). Full guide: [docs/APP.md](docs/APP.md).
</details>

<details>
<summary><b>Terminal tools</b> — <code>ytgrab.py</code> menu and raw <code>yt-dlp</code> commands</summary>

`python3 scripts/ytgrab.py` is a terminal menu that checks your setup, retries automatically when
YouTube says 403, and names files cleanly. The original step-by-step `yt-dlp` guide that
this repo started as — folder, `cmd`, the "magic command", playlists, metadata — is
preserved in **[docs/MANUAL.md](docs/MANUAL.md)**, and the copy-paste cheat sheet in
[docs/CHEATSHEET.md](docs/CHEATSHEET.md).
</details>

<details>
<summary><b>For developers</b> — building it yourself</summary>

* Desktop app: [desktop/README.md](desktop/README.md) (Tauri v2, Rust).
* Android app: `android/` (Kotlin + Jetpack Compose, `gradle assembleDebug`).
* Why it is built the way it is: [docs/RESEARCH.md](docs/RESEARCH.md).
* Repo layout: [docs/README.md](docs/README.md).
* Cutting a release: **Actions → release → Run workflow** → type a version such as `0.2.1`.
  CI builds every installer, stamps the version into them, and publishes a GitHub Release
  with the file names used on this page. Pushing a tag `v0.2.1` does the same.
</details>

## Something not working?

* **`HTTP Error 403: Forbidden`** on YouTube almost always means the engine is outdated:
  *Settings → Update yt-dlp*, then retry. GrabBox also retries other player clients on its own.
* Every other error, explained in plain English: **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.
* Still stuck, or have an idea? Email **feedit18@gmail.com**, or use the 💬 button inside the
  app — it pre-fills everything needed to help.

## Privacy & legality

100 % local. The only network traffic is between your device and the site you download
from. Use it for your own uploads, Creative-Commons material, things you bought, or
offline copies you are allowed to keep — downloading other people's copyrighted material
breaks YouTube's terms and, depending on where you live, the law.
