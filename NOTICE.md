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
