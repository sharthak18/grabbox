# Torrents in GrabBox

Paste a `magnet:` link into the grab bar — or hit the paperclip button and pick a
`.torrent` file — and the download joins the same list as everything else.

## What works (desktop)

- **Magnet links** and **.torrent files** (up to 64 MiB of bencode). Paste a
  magnet or drop/pick a `.torrent` — you get a file picker listing everything
  in the torrent (name + size, select all/none), nothing downloads until you
  hit **Download**. For magnets the metadata is resolved up front and the
  torrent is added *paused*; canceling the picker deletes it again.
- **File selection** end to end: inspect adds paused → engine reports its
  files → confirm applies `only_files` and unpauses.
- Same list, same controls: pause / resume / retry, live speed and ETA,
  percent, aggregate into the sidebar badges and the combined ↓ speed readout.
- **Persistent across restarts** — session state lives in `~/.grabbox/torrents/`
  (librqbit JSON persistence + fastresume). On next launch GrabBox restores the
  session automatically, but **only if that folder exists** — users who never
  touch torrents pay zero cost (no bound ports, no DHT).
- Downloads land in your configured folder (multi-file torrents get their
  usual per-torrent subfolder).
- **Clear finished** removes completed / canceled torrent rows from the list.
  Files are never deleted by GrabBox.
- DHT is enabled (no persistence, in-memory only); incoming peers on an
  ephemeral TCP port; no UPnP by default.

## What doesn't (yet)

- **Python server / browser build** and **Android**: no torrent engine. The UI
  hides the paperclip and says plainly that torrents need the desktop app
  instead of failing silently. A future Android path would bind librqbit over
  FFI, or implement the protocol natively in Kotlin.
- **Sequential-download preference**, bandwidth caps per torrent, and a
  piece-level progress view. The engine supports all of these (`ratelimits`,
  piece stats); they are follow-ups.
- **RSS auto-download of torrents**, debrid-service unlocking. Planned as a
  separate milestone; the queue hooks are already in place.
- **Seeding control / ratio limits** — GrabBox keeps finished torrents paused
  in the session; removal via "Clear finished". Seeding UX is on the roadmap.

## Why librqbit

MDM (Mister Download Manager) ships torrents too, but its engine is GPL-3.0 and
GrabBox is MIT. `librqbit` (github.com/ikatson/rqbit, Apache-2.0) is the
permissively licensed Rust engine behind the rqbit client: actively maintained,
async, library-first, with session persistence. GrabBox uses the public API
only; nothing was copied. See `NOTICE.md`.

## Architecture

```
grab bar paste ──► isMagnet()? ──► invoke grabbox_torrent_inspect ─┐
.torrent drop/pick ──► FileReader(b64) ──► invoke grabbox_torrent_inspect_file ─┤
                                                                   ▼
                              paused torrent + file list (nothing downloads yet)
                                                                   ▼
             user confirms ──► invoke grabbox_torrent_select {only_files, unpause}
             user cancels  ──► invoke grabbox_torrent_discard (removes the paused torrent)
                                          │
                                          ▼
                            TorrentManager (torrent.rs)
                            session: OnceCell<Arc<Session>>
                              · fastresume + JSON persistence
                              · ~/.grabbox/torrents/session
                                          │
grabbox_jobs ◄── snapshot() merged rows ◄─┘   id = "bt-<n>"
grabbox_cancel / pause / resume ──► parse_torrent_id()? bt-… → session
```

Job rows carry the same fields as yt-dlp jobs (`kind: "torrent"`,
`status: running|done|paused|queued|canceled|error`), so the queue UI,
filter tabs, sidebar badges and notifications work unchanged. "Canceled" maps
to *paused in the engine + a tombstone in GrabBox*; **Retry** resumes it.
