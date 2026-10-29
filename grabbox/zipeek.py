"""
Peek inside a remote ZIP without downloading it.

A ZIP's table of contents (the central directory) sits at the very end
of the file, so with HTTP Range support the whole listing costs one or
two small requests — the tail first (the End-Of-Central-Directory
record is in the last 64 KB), then the directory itself if it starts
earlier. Idea credit: DownloadCraft's `zip::list_remote` (MIT, see
NOTICE.md); reimplemented here in stdlib Python.

Best-effort by design: anything unexpected (no ranges, ZIP64,
multi-disk, a lying server) returns None and the caller just shows a
plain archive probe. Listing a ZIP must never break downloading it.
"""

import struct
import urllib.error
import urllib.request

from . import horse

EOCD_SIG = b"PK\x05\x06"
CD_SIG = b"PK\x01\x02"
TAIL_MAX = 22 + 65535          # EOCD record + longest possible comment
CD_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")   # 46 bytes, fixed part


def _fetch_range(url, start, end, timeout):
    """GET exactly [start, end]; None unless the server honoured it."""
    req = urllib.request.Request(
        url, headers={"User-Agent": horse.USER_AGENT,
                      "Range": "bytes=%d-%d" % (start, end)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 206:
                return None
            return resp.read(end - start + 1)
    except Exception:
        return None


def list_remote(url, timeout=15, max_entries=500):
    """
    [{name, size, compressed}, ...] for the ZIP at `url`, or None when
    it can't be listed remotely. At most `max_entries` rows.
    """
    try:
        info = horse.probe_ranges(url, timeout=timeout)
    except Exception:
        return None
    size = info.get("size")
    if not size or not info.get("ranges") or size < 22:
        return None

    tail_len = min(size, TAIL_MAX)
    tail = _fetch_range(url, size - tail_len, size - 1, timeout)
    if not tail:
        return None
    eocd = tail.rfind(EOCD_SIG)
    if eocd < 0 or eocd + 22 > len(tail):
        return None
    (_disk, _cd_disk, _disk_entries, total, cd_size, cd_offset,
     _comment) = struct.unpack_from("<HHHHIIH", tail, eocd + 4)
    # ZIP64 / multi-disk archives keep their real numbers elsewhere;
    # bow out politely instead of guessing.
    if _disk or _cd_disk or total == 0xFFFF \
            or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        return None

    tail_base = size - tail_len
    if cd_offset >= tail_base and cd_offset + cd_size <= size:
        cd = tail[cd_offset - tail_base: cd_offset - tail_base + cd_size]
    else:
        cd = _fetch_range(url, cd_offset, cd_offset + cd_size - 1, timeout)
    if not cd:
        return None

    entries = []
    off = 0
    while off + CD_HEADER.size <= len(cd) and len(entries) < max_entries:
        fields = CD_HEADER.unpack_from(cd, off)
        if fields[0] != 0x02014B50:      # 'PK\x01\x02'
            break
        (_sig, _vmade, _vneed, _flags, _method, _mt, _md, _crc,
         csize, usize, nlen, xlen, clen) = fields[:13]
        name = cd[off + CD_HEADER.size:
                  off + CD_HEADER.size + nlen].decode("utf-8", "replace")
        entries.append({"name": name, "size": usize, "compressed": csize})
        off += CD_HEADER.size + nlen + xlen + clen
    return entries or None
