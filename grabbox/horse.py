"""
Horse: the segmented direct-file downloader (the IDM trick, in stdlib).

yt-dlp is brilliant at *finding* media, but for a plain HTTP file it pulls
one connection, one stream. IDM's whole reputation is the opposite move:
open N connections, give each a byte range, write them into one pre-sized
file in parallel — a server that throttles *per connection* suddenly serves
N times faster, and a dropped connection only loses its own segment.

This module is that move, with the never-dies half built in:

* progress is kept in a sidecar (``<file>.horse.json``) next to the
  ``<file>.part`` work file, so pause / crash / reboot resumes mid-segment
  instead of starting over;
* the validator (size + ETag / Last-Modified) is checked before a resume,
  so a changed file restarts cleanly instead of gluing two versions;
* servers without Range support get an honest single stream that still
  resumes by appending when it can.

Stdlib only, like the rest of the core. Used by ``downloader.Manager`` for
direct files; yt-dlp (with concurrent fragments) handles everything else.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CHUNK = 256 * 1024            # per-connection read/write block
MIN_SEGMENT = 512 * 1024      # don't slice thinner than this
SPLIT_MIN = 256 * 1024        # smallest slice worth stealing from a slow
                              # connection (see _download_segmented)
STATE_SUFFIX = ".horse.json"
PART_SUFFIX = ".part"


class HorseError(Exception):
    """Download failed; the .part + state stay on disk so a retry resumes."""


class HorsePaused(HorseError):
    """Stopped on purpose; resume continues from the saved segment map."""


class HorseFallback(HorseError):
    """This URL is not horse-shaped (e.g. a web page). Use yt-dlp instead."""


class _RangeIgnored(HorseError):
    """Server advertised ranges, then answered 200. Handled internally:
    segmented mode quietly degrades to the single-stream path."""


class _RateLimiter(object):
    """Shared token bucket: the *aggregate* job rate, split over segments."""

    def __init__(self, bytes_per_sec):
        self.rate = float(bytes_per_sec or 0)
        self._tokens = self.rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n):
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.rate,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self.rate
            time.sleep(min(wait, 0.25))


class _Progress(object):
    """Thread-safe counters + an EMA speed, reported ~4x per second."""

    def __init__(self, total, done, callback):
        self.total = total
        self.done = done
        self.callback = callback
        self.speed = 0.0
        self._lock = threading.Lock()
        self._last_t = time.monotonic()
        self._last_done = done
        self._last_push = 0.0

    def add(self, n, force=False):
        with self._lock:
            self.done += n
            now = time.monotonic()
            dt = now - self._last_t
            if dt >= 0.25:
                inst = (self.done - self._last_done) / dt
                self.speed = inst if self.speed == 0 else 0.6 * self.speed + 0.4 * inst
                self._last_t, self._last_done = now, self.done
            return self.done

    def push(self, force=False):
        if not self.callback:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_push < 0.25:
                return
            self._last_push = now
            done, total, speed = self.done, self.total, self.speed
        self.callback(done, total, speed)


def _request(url, headers, byte_range=None):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    if byte_range is not None:
        hdrs["Range"] = "bytes=%d-%d" % byte_range
    return urllib.request.Request(url, headers=hdrs)


def probe_ranges(url, headers=None, timeout=30):
    """
    One 1-byte ranged GET tells us everything: total size, whether ranges
    are honoured, and the validators a later resume must match.

    Returns dict(size, ranges, etag, last_modified, final_url, status).
    """
    try:
        req = _request(url, headers, (0, 0))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            hdrs = resp.headers
            final_url = resp.geturl()
            try:
                resp.read(1024)
            except Exception:
                pass
    except urllib.error.HTTPError as exc:
        return {"size": None, "ranges": False, "etag": None,
                "last_modified": None, "final_url": url,
                "status": exc.code}
    except Exception as exc:
        raise HorseError(str(exc))

    size = None
    crange = hdrs.get("Content-Range") or ""
    if "/" in crange:
        tail = crange.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            size = int(tail)
    if size is None and status == 200:
        length = hdrs.get("Content-Length")
        if length and length.isdigit():
            size = int(length)
    ranges = status == 206 or "bytes" in (hdrs.get("Accept-Ranges") or "")
    ctype = (hdrs.get("Content-Type") or "").lower()
    if status == 200 and ctype.startswith("text/html") and size is None:
        raise HorseFallback("server returned a web page, not a file")
    return {"size": size, "ranges": bool(ranges),
            "etag": hdrs.get("ETag"),
            "last_modified": hdrs.get("Last-Modified"),
            "final_url": final_url, "status": status}


class _State(object):
    """The on-disk segment map. ``done`` counts bytes *inside* each segment."""

    def __init__(self, path):
        self.path = path
        self.data = None

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (IOError, OSError, ValueError):
            self.data = None
        return self.data

    def save(self, data):
        self.data = data
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def drop(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.data = None


def _valid_resume(state, url, info):
    """Saved state may be reused only for the same URL and same file body."""
    if not state or state.get("url") != url:
        return False
    if info.get("size") is not None and state.get("size") != info["size"]:
        return False
    for key in ("etag", "last_modified"):
        if info.get(key) and state.get(key) and info[key] != state[key]:
            return False
    return bool(state.get("segments"))


def download(url, dest, connections=8, rate_limit_bps=0, headers=None,
             should_stop=None, progress=None, timeout=30):
    """
    Download ``url`` to ``dest`` (atomically, via ``dest + '.part'``).

    ``should_stop``: optional callable returning ``"pause"``, ``"cancel"``
    or None — polled between chunks. ``progress``: optional callable
    ``(done_bytes, total_bytes_or_None, speed_bps)``.

    Returns the final path. Raises HorsePaused / HorseError /
    HorseFallback; on HorseError the partial state is kept for resume.
    """
    connections = max(1, min(16, int(connections or 1)))
    info = probe_ranges(url, headers=headers, timeout=timeout)
    if info["status"] and info["status"] >= 400:
        raise HorseError("HTTP %s" % info["status"])

    work = dest + PART_SUFFIX
    state_file = _State(dest + STATE_SUFFIX)
    saved = state_file.load()
    resume = _valid_resume(saved, url, info) and os.path.exists(work)
    if not resume:
        state_file.drop()
        saved = None
        try:
            os.remove(work)
        except OSError:
            pass

    if info["size"] is not None and info["ranges"] and connections > 1 \
            and info["size"] >= MIN_SEGMENT * 2:
        try:
            return _download_segmented(url, dest, work, state_file, saved,
                                       info, connections, rate_limit_bps,
                                       headers, should_stop, progress,
                                       timeout)
        except _RangeIgnored:
            saved = None  # ranged claims were a lie; start the stream clean
    return _download_single(url, dest, work, state_file, saved, info,
                            rate_limit_bps, headers, should_stop, progress,
                            timeout)


def _check_stop(should_stop):
    if should_stop is None:
        return
    verdict = should_stop()
    if verdict == "pause":
        raise HorsePaused("paused")
    if verdict == "cancel":
        raise HorseError("canceled")


def _download_segmented(url, dest, work, state_file, saved, info, connections,
                        rate_limit_bps, headers, should_stop, progress,
                        timeout):
    """
    Work-stealing segmentation (design credit: DownloadCraft's MIT engine,
    crates/downloadcraft-engine — see NOTICE.md).

    The file starts as ONE segment. A free connection either takes an
    orphaned segment whole, or splits the segment that will finish last —
    proportional to measured speeds, so a slow connection keeps a small
    share and never holds the whole download hostage, and no connection
    idles while work remains. Our old fixed split could do neither.
    """
    size = info["size"]
    lock = threading.Lock()
    segments = []          # {start, pos, end, owner, fails}
    if saved:
        raw = sorted(saved["segments"], key=lambda s: s["start"])
        ok = raw and raw[0]["start"] == 0 and raw[-1]["end"] == size and \
            all(raw[i]["end"] == raw[i + 1]["start"]
                for i in range(len(raw) - 1))
        if ok:
            for s in raw:
                segments.append({"start": s["start"],
                                 "pos": s["start"] + s["done"],
                                 "end": s["end"], "owner": None, "fails": 0})
    if not segments:
        fh = open(work, "wb")
        try:
            fh.truncate(size)     # pre-size: segments write in place
        finally:
            fh.close()
        segments.append({"start": 0, "pos": 0, "end": size,
                         "owner": None, "fails": 0})

    def seg_done(s):
        return max(0, min(s["pos"], s["end"]) - s["start"])

    done0 = sum(seg_done(s) for s in segments)
    prog = _Progress(size, done0, progress)
    prog.push(force=True)
    limiter = _RateLimiter(rate_limit_bps)
    speeds = {}            # worker -> EMA bytes/sec
    speed_bytes = {}       # worker -> [bytes at last sample, monotonic t]
    failure = []
    throttled = {"hit": False}

    def persist():
        with lock:
            rows = [{"start": s["start"], "end": s["end"],
                     "done": seg_done(s)}
                    for s in sorted(segments, key=lambda s: s["start"])]
            state_file.save(_state_dict(url, info, rows))

    def note_speed(worker, n):
        now = time.monotonic()
        acc = speed_bytes.setdefault(worker, [0, now])
        acc[0] += n
        if now - acc[1] >= 0.25:
            inst = acc[0] / max(now - acc[1], 0.001)
            old = speeds.get(worker, 0.0)
            speeds[worker] = inst if old == 0 else 0.6 * old + 0.4 * inst
            speed_bytes[worker] = [0, now]

    def speed_of(worker):
        return speeds.get(worker, 0.0)

    def claim(worker):
        """Take orphaned work, or split the segment finishing last."""
        with lock:
            orphans = [s for s in segments
                       if s["owner"] is None and s["end"] - s["pos"] > 0]
            if orphans:
                s = max(orphans,
                        key=lambda s: (s["end"] - s["pos"], -s["start"]))
                s["owner"] = worker
                return s
            owned = [s for s in segments
                     if s["owner"] is not None
                     and s["end"] - s["pos"] >= 2 * SPLIT_MIN]
            if not owned:
                return None

            def eta(s):
                v = speed_of(s["owner"])
                rem = s["end"] - s["pos"]
                return rem / v if v > 0 else float(rem)

            s = max(owned, key=lambda s: (eta(s), s["end"] - s["pos"]))
            rem = s["end"] - s["pos"]
            owner_v = speed_of(s["owner"])
            measured = [v for v in speeds.values() if v > 0]
            avg = sum(measured) / len(measured) if measured else 0.0
            # Proportional split: the owner keeps the share it can finish
            # in the same time the newcomer needs for the rest.
            keep = rem * owner_v / (owner_v + avg) \
                if owner_v > 0 and avg > 0 else rem / 2
            keep = int(max(SPLIT_MIN, min(keep, rem - SPLIT_MIN)))
            mid = s["pos"] + keep
            aligned = mid - mid % (64 * 1024)
            if aligned >= s["pos"] + SPLIT_MIN \
                    and s["end"] - aligned >= SPLIT_MIN:
                mid = aligned
            new = {"start": mid, "pos": mid, "end": s["end"],
                   "owner": worker, "fails": 0}
            s["end"] = mid
            segments.append(new)
            return new

    def run_claim(worker, seg):
        frm = seg["pos"]
        req = _request(url, headers, (frm, seg["end"] - 1))
        try:
            resp_ctx = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # urllib raises for every non-2xx; translate the interesting ones.
            if exc.code in (429, 503):
                raise _Throttled("HTTP %s" % exc.code)
            if exc.code == 416:
                raise HorseError("range not satisfiable (HTTP 416)")
            raise HorseError("HTTP %s" % exc.code)
        with resp_ctx as resp:
            if resp.status != 206:
                raise _RangeIgnored("server ignored Range (HTTP %s)"
                                    % resp.status)
            with open(work, "r+b") as fh:
                while True:
                    _check_stop(should_stop)
                    with lock:
                        pos, end = seg["pos"], seg["end"]
                    if pos >= end:
                        return
                    block = resp.read(min(CHUNK, end - pos))
                    if not block:
                        raise HorseError("connection dropped mid-segment")
                    with lock:
                        # Our bound may have shrunk (a split) mid-read:
                        # only the bytes up to the current end are ours.
                        allowed = max(0, min(len(block), seg["end"] - pos))
                        if allowed:
                            seg["pos"] = pos + allowed
                    if allowed:
                        limiter.take(allowed)
                        fh.seek(pos)
                        fh.write(block[:allowed])
                        fh.flush()
                        with lock:
                            note_speed(worker, allowed)
                        prog.add(allowed)
                        prog.push()
                        persist()
                    if allowed < len(block):
                        return   # shrunk past the stream; segment done

    def worker_loop(worker):
        try:
            while True:
                seg = claim(worker)
                if seg is None:
                    return
                try:
                    run_claim(worker, seg)
                    with lock:
                        seg["owner"] = None
                except _Throttled:
                    throttled["hit"] = True
                    with lock:
                        seg["owner"] = None
                    time.sleep(2)      # 429/503: ease off, keep the bytes
                except (HorsePaused, _RangeIgnored):
                    raise
                except HorseError:
                    if should_stop and should_stop():
                        raise
                    with lock:
                        seg["owner"] = None
                        seg["fails"] += 1
                        if seg["fails"] >= 6 and not failure:
                            failure.append(HorseError(
                                "segment failed repeatedly"))
                            return
                except Exception as exc:  # dropped socket, timeout, ...
                    with lock:
                        seg["owner"] = None
                        seg["fails"] += 1
                        bad = seg["fails"] >= 6
                    if bad:
                        with lock:
                            if not failure:
                                failure.append(HorseError(str(exc)))
                        return
        except BaseException as exc:  # noqa: BLE001 - re-raised on join
            with lock:
                if not failure:
                    failure.append(exc)

    threads = []
    for wid in range(max(1, connections)):
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    persist()
    prog.push(force=True)
    if failure:
        raise failure[0]

    if prog.done != size:
        raise HorseError("incomplete: %d of %d bytes" % (prog.done, size))
    os.replace(work, dest)
    state_file.drop()
    return dest


class _Throttled(HorseError):
    """429/503: back off and retry the segment — never a hard failure."""


def _state_dict(url, info, segments):
    return {"url": url, "size": info["size"], "etag": info.get("etag"),
            "last_modified": info.get("last_modified"), "segments": segments}


def _download_single(url, dest, work, state_file, saved, info,
                     rate_limit_bps, headers, should_stop, progress, timeout):
    """One stream; still resumes by appending when the server allows it."""
    size = info["size"]
    # Single-stream state is one segment starting at 0, so its ``done`` is
    # exactly the contiguous prefix we already hold. (State left behind by a
    # segmented run also starts at 0; only that first segment's bytes are a
    # usable prefix, the rest are stranded at their offsets — ignore them.)
    have = 0
    if saved and saved.get("segments"):
        have = int(saved["segments"][0].get("done") or 0)
    if not os.path.exists(work):
        have = 0
    else:
        have = min(have, os.path.getsize(work))
    if have and not size:
        have = 0  # cannot verify a resume point without a total; restart

    byte_range = (have, size - 1) if (have and size and info["ranges"]) else None
    prog = _Progress(size, have, progress)
    prog.push(force=True)
    limiter = _RateLimiter(rate_limit_bps)
    try:
        req = _request(url, headers, byte_range)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if byte_range and resp.status != 206:
                have = 0                      # server ignored our resume point
                prog.done = 0
            mode = "ab" if have else "wb"
            with open(work, mode) as fh:
                while True:
                    _check_stop(should_stop)
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    limiter.take(len(block))
                    fh.write(block)
                    have += len(block)
                    prog.add(len(block))
                    prog.push()
                    state_file.save({
                        "url": url, "size": size, "etag": info.get("etag"),
                        "last_modified": info.get("last_modified"),
                        "segments": [{"start": 0, "end": size or have,
                                      "done": have}],
                    })
    except (HorsePaused, HorseError):
        raise
    except Exception as exc:
        raise HorseError(str(exc))

    if size is not None and have != size:
        raise HorseError("incomplete: %d of %d bytes" % (have, size))
    prog.push(force=True)
    os.replace(work, dest)
    state_file.drop()
    return dest


def cleanup(dest):
    """Remove the partial + segment map (an explicit user discard)."""
    for path in (dest + PART_SUFFIX, dest + STATE_SUFFIX):
        try:
            os.remove(path)
        except OSError:
            pass
