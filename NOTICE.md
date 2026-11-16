# Notices

GrabBox is MIT-licensed (see `LICENSE`). It builds on ideas — and, where
noted, design details — from other open-source projects:

## DownloadCraft (MIT)

- Project: https://github.com/hungry-detective/downloadcraft
- License: MIT, Copyright (c) 2026 hungry-detective
- What we adopted: the **work-stealing segmentation** design from its
  engine crate (`crates/downloadcraft-engine/src/segments.rs`) — a free
  connection takes orphaned work or splits the segment with the longest
  expected finish time, proportionally to measured per-connection
  speeds — reimplemented in Python in `grabbox/horse.py`
  (`_download_segmented`), with the same spirit in its 429/503
  back-off behavior. Proven by
  `tests/test_horse.py::test_work_stealing_beats_the_slow_first_connection`.

Thank you to the DownloadCraft author for publishing a clean,
readable engine.

## UI design references (no code copied)

The desktop/web UI redesign (sidebar shell, task cards, settings page)
was informed by studying these projects' interfaces. All GrabBox UI code
is original, written from scratch against GrabBox's own driver layer:

- **Motrix Next** (https://github.com/AnInsomniacy/motrix-next, MIT) —
  sidebar destinations with live counts, task-list card language,
  preference page split into cards, M3 tonal palette approach.
- **AB Download Manager** (https://github.com/amir1376/ab-download-manager,
  Apache-2.0) — IDM-style status categories, dense file rows with
  inline actions.
- **Gopeed** (https://github.com/GopeedLab/gopeed, GPL-3.0) — permanent
  "add link" bar instead of a buried add dialog.
- **Rayburst** (https://github.com/AnInsomniacy/rayburst, MIT) —
  speed-first status presentation.

Thank you to their authors for proving what a good download UI feels like.

## Torrent support — librqbit (Apache-2.0)

- Project: https://github.com/ikatson/rqbit (crate `librqbit`)
- License: Apache-2.0, Copyright (c) 2021 Igor Katson
- What we use: the `librqbit` library as the BitTorrent engine inside the
  desktop app (`desktop/src-tauri/src/torrent.rs`) — session persistence,
  DHT, fastresume, incoming peers on an ephemeral TCP port. GrabBox drives it
  through its public API; the engine, its design (and rqbit's) are credited
  here per the license. Our glue code and UI are our own.
- Mister Download Manager (GPL-3.0) was studied for feature scope only; no
  code from it appears in GrabBox.
