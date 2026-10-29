package app.grabbox.work

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.max
import kotlin.math.min

/**
 * The horse, native edition: work-stealing segmented direct-file
 * downloads — the same engine as the Python core's `grabbox/horse.py`
 * (see docs/HORSE.md for the shared spec). One behaviour, three stacks.
 *
 * The file starts as ONE segment. A free connection either takes an
 * orphaned segment whole, or splits the segment that will finish last —
 * proportional to measured speeds (design credit: DownloadCraft's MIT
 * engine, see NOTICE.md) — so a connection that turns out slow keeps a
 * small share and never holds the whole download hostage, and no
 * connection idles while work remains.
 *
 * Progress lives in `<file>.horse.json` beside the `<file>.part` work
 * file, guarded by size + ETag/Last-Modified, so pause / crash / reboot
 * resumes mid-segment. Servers that lie about Range support degrade
 * quietly to a single stream that still resumes by appending.
 */
object Horse {

    private const val USER_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    private const val CHUNK = 256 * 1024        // per-connection read/write block
    private const val MIN_SEGMENT = 512L * 1024 // don't slice thinner than this
    private const val SPLIT_MIN = 256L * 1024   // smallest slice worth stealing
    private const val MAX_REFRESHES = 2         // expired-link re-resolutions
    private const val STATE_SUFFIX = ".horse.json"
    private const val PART_SUFFIX = ".part"
    private const val TIMEOUT_MS = 30_000

    /** A connection that delivers no new bytes for this long is reopened. */
    var stallSeconds: Double = 20.0

    /** Download failed; the .part + state stay on disk so a retry resumes. */
    open class HorseError(message: String) : Exception(message)

    /** Stopped on purpose; resume continues from the saved segment map. */
    class HorsePaused : HorseError("paused")

    /** This URL is not horse-shaped (e.g. a web page). Use yt-dlp instead. */
    class HorseFallback(message: String) : HorseError(message)

    /** Server advertised ranges, then answered 200. Degrades internally. */
    private class RangeIgnored : HorseError("server ignored Range")

    /** 429/503: back off and retry the segment — never a hard failure. */
    private class Throttled : HorseError("throttled")

    /**
     * 403/410 mid-download: the (usually signed) URL went stale.
     * download() re-resolves the source URL and continues the same
     * segment map under the fresh URL — bytes are kept.
     */
    private class LinkExpired : HorseError("link expired")

    /** 'bytes 100-199/1234' -> (100, 199, 1234); total null when '*'. */
    private fun parseContentRange(value: String?): Triple<Long, Long, Long?>? {
        if (value == null) return null
        return try {
            val v = value.trim()
            val sp = v.indexOf(' ')
            if (sp < 0 || !v.substring(0, sp).equals("bytes", ignoreCase = true)) {
                return null
            }
            val rest = v.substring(sp + 1)
            val span = rest.substringBefore('/')
            val total = rest.substringAfter('/', "").trim()
            val start = span.substringBefore('-').trim().toLong()
            val end = span.substringAfter('-').trim().toLong()
            Triple(start, end, total.toLongOrNull())
        } catch (e: Exception) {
            null
        }
    }

    /** True when the body's first bytes are plainly an HTML page. */
    private fun looksLikeHtml(first: ByteArray): Boolean {
        var i = 0
        if (first.size >= 3 && first[0] == 0xEF.toByte() &&
            first[1] == 0xBB.toByte() && first[2] == 0xBF.toByte()
        ) {
            i = 3 // UTF-8 BOM
        }
        while (i < first.size && first[i].toInt().toChar() in " \t\r\n") i++
        val head = String(first, i, min(9, first.size - i), Charsets.US_ASCII)
            .lowercase()
        return head.startsWith("<!doctype") || head.startsWith("<html") ||
            head.startsWith("<head") || head.startsWith("<body")
    }

    // ------------------------------------------------------------ plumbing --

    private fun nowSec(): Double = System.nanoTime() / 1_000_000_000.0

    /** Shared token bucket: the *aggregate* job rate, split over segments. */
    private class RateLimiter(bytesPerSec: Long) {
        private val rate = bytesPerSec.toDouble()
        private var tokens = rate
        private var last = nowSec()
        private val lock = Any()

        fun take(n: Int) {
            if (rate <= 0) return
            while (true) {
                val wait = synchronized(lock) {
                    val now = nowSec()
                    tokens = min(rate, tokens + (now - last) * rate)
                    last = now
                    if (tokens >= n) {
                        tokens -= n
                        return
                    }
                    (n - tokens) / rate
                }
                Thread.sleep(min(wait * 1000.0, 250.0).toLong().coerceAtLeast(1))
            }
        }
    }

    /** Thread-safe counters + an EMA speed, reported ~4x per second. */
    private class Progress(
        private val total: Long?,
        done0: Long,
        private val callback: ((Long, Long?, Double) -> Unit)?,
    ) {
        private val lock = Any()
        var done: Long = done0
            private set
        private var speed = 0.0
        private var lastT = nowSec()
        private var lastDone = done0
        private var lastPush = 0.0

        fun setDone(v: Long) = synchronized(lock) {
            done = v
            lastDone = v
        }

        fun add(n: Int) = synchronized(lock) {
            done += n
            val now = nowSec()
            val dt = now - lastT
            if (dt >= 0.25) {
                val inst = (done - lastDone) / dt
                speed = if (speed == 0.0) inst else 0.6 * speed + 0.4 * inst
                lastT = now
                lastDone = done
            }
        }

        fun push(force: Boolean = false) {
            val cb = callback ?: return
            val snap = synchronized(lock) {
                val now = nowSec()
                if (!force && now - lastPush < 0.25) return
                lastPush = now
                Triple(done, total, speed)
            }
            cb(snap.first, snap.second, snap.third)
        }
    }

    private fun checkStop(shouldStop: (() -> String?)?) {
        when (shouldStop?.invoke()) {
            "pause" -> throw HorsePaused()
            "cancel" -> throw HorseError("canceled")
        }
    }

    private fun open(url: String, range: Pair<Long, Long>?): HttpURLConnection {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = TIMEOUT_MS
        conn.readTimeout = TIMEOUT_MS
        conn.instanceFollowRedirects = true
        conn.setRequestProperty("User-Agent", USER_AGENT)
        if (range != null) {
            conn.setRequestProperty("Range", "bytes=${range.first}-${range.second}")
        }
        return conn
    }

    // --------------------------------------------------------- state file --

    private data class SegRow(val start: Long, val end: Long, val done: Long)

    private data class StateJson(
        val url: String,
        val size: Long?,
        val etag: String?,
        val lastModified: String?,
        val segments: List<SegRow>,
    )

    private data class ProbeInfo(
        val size: Long?,
        val ranges: Boolean,
        val etag: String?,
        val lastModified: String?,
        val status: Int,
        val finalUrl: String,
    )

    private fun stateLoad(file: File): StateJson? = try {
        val json = JSONObject(file.readText())
        val rows = mutableListOf<SegRow>()
        val arr = json.optJSONArray("segments") ?: JSONArray()
        for (i in 0 until arr.length()) {
            val r = arr.optJSONObject(i) ?: continue
            rows.add(SegRow(r.optLong("start"), r.optLong("end"), r.optLong("done")))
        }
        StateJson(
            url = json.optString("url"),
            size = if (json.isNull("size")) null else json.optLong("size"),
            etag = if (json.isNull("etag")) null else json.optString("etag").ifBlank { null },
            lastModified = if (json.isNull("last_modified")) null
            else json.optString("last_modified").ifBlank { null },
            segments = rows,
        )
    } catch (e: Exception) {
        null
    }

    private fun stateSave(file: File, data: StateJson) {
        try {
            val rows = JSONArray()
            for (s in data.segments) {
                rows.put(JSONObject().put("start", s.start).put("end", s.end).put("done", s.done))
            }
            val json = JSONObject()
                .put("url", data.url)
                .put("size", data.size ?: JSONObject.NULL)
                .put("etag", data.etag ?: JSONObject.NULL)
                .put("last_modified", data.lastModified ?: JSONObject.NULL)
                .put("segments", rows)
            val tmp = File(file.path + ".tmp")
            tmp.writeText(json.toString())
            // rename(2) replaces atomically on Android's filesystems.
            if (!tmp.renameTo(file)) {
                tmp.copyTo(file, overwrite = true)
                tmp.delete()
            }
        } catch (e: Exception) {
            // Persistence is best-effort; the download matters more.
        }
    }

    private fun stateDict(url: String, info: ProbeInfo, segments: List<SegRow>) =
        StateJson(url, info.size, info.etag, info.lastModified, segments)

    /** Saved state may be reused only for the same URL and same file body. */
    private fun validResume(state: StateJson, url: String, info: ProbeInfo): Boolean {
        if (state.url != url) return false
        if (info.size != null && state.size != null && info.size != state.size) return false
        if (info.etag != null && state.etag != null && info.etag != state.etag) return false
        if (info.lastModified != null && state.lastModified != null &&
            info.lastModified != state.lastModified
        ) return false
        return state.segments.isNotEmpty()
    }

    // --------------------------------------------------------------- probe --

    /**
     * One 1-byte ranged GET tells us everything: total size, whether
     * ranges are honoured, and the validators a later resume must match.
     */
    private fun probeRanges(url: String): ProbeInfo {
        val conn = open(url, 0L to 0L)
        try {
            val status = conn.responseCode
            if (status >= 400) {
                return ProbeInfo(null, false, null, null, status, url)
            }
            val finalUrl = conn.url.toString()
            var size: Long? = null
            val crange = conn.getHeaderField("Content-Range")
            if (crange != null && '/' in crange) {
                size = crange.substringAfterLast('/').trim().toLongOrNull()
            }
            if (size == null && status == 200) {
                size = conn.getHeaderField("Content-Length")?.trim()?.toLongOrNull()
            }
            val ranges = status == 206 ||
                (conn.getHeaderField("Accept-Ranges") ?: "").contains("bytes")
            val ctype = (conn.getHeaderField("Content-Type") ?: "").lowercase()
            // Keep the first bytes: enough to sniff an HTML error page
            // out from behind a lying Content-Type before saving any.
            var firstBytes = ByteArray(0)
            try {
                val buf = ByteArray(1024)
                val got = conn.inputStream.read(buf)
                if (got > 0) firstBytes = buf.copyOf(got)
            } catch (e: Exception) {
                // The headers already told us everything.
            }
            if (status == 200 && ctype.startsWith("text/html") && size == null) {
                throw HorseFallback("server returned a web page, not a file")
            }
            if (status == 200 && looksLikeHtml(firstBytes)) {
                // Magic-byte check, DownloadCraft's rule: an error page
                // is an error page even with a Content-Length set.
                throw HorseFallback("server returned a web page, not a file")
            }
            return ProbeInfo(
                size = size,
                ranges = ranges,
                etag = conn.getHeaderField("ETag"),
                lastModified = conn.getHeaderField("Last-Modified"),
                status = status,
                finalUrl = finalUrl,
            )
        } catch (e: HorseFallback) {
            throw e
        } catch (e: Exception) {
            throw HorseError(e.message ?: "probe failed")
        } finally {
            conn.disconnect()
        }
    }

    // ------------------------------------------------------------ download --

    /**
     * Download [url] to [dest] (atomically, via `dest + ".part"`).
     * [shouldStop] returns "pause", "cancel" or null, polled between
     * chunks. [progress] receives (doneBytes, totalBytesOrNull, speedBps).
     * Returns the final file. Raises [HorsePaused] / [HorseError] /
     * [HorseFallback]; on HorseError the partial state is kept for resume.
     */
    fun download(
        url: String,
        dest: File,
        connections: Int,
        rateLimitBps: Long = 0,
        shouldStop: (() -> String?)? = null,
        progress: ((Long, Long?, Double) -> Unit)? = null,
    ): File {
        val conns = connections.coerceIn(1, 16)
        val info = probeRanges(url)
        if (info.status >= 400) throw HorseError("HTTP ${info.status}")
        // Segments fetch the URL the probe landed on (redirects resolved
        // once, not per request). `url` stays the source: re-resolving it
        // is how an expired signed link gets refreshed below.
        var current = info.finalUrl.ifEmpty { url }

        val work = File(dest.path + PART_SUFFIX)
        val stateFile = File(dest.path + STATE_SUFFIX)
        var saved = stateLoad(stateFile)
        val resume = saved != null && validResume(saved, current, info) && work.exists()
        if (!resume) {
            stateFile.delete()
            saved = null
            work.delete()
        }

        val size = info.size
        if (size != null && info.ranges && conns > 1 && size >= MIN_SEGMENT * 2) {
            var refreshes = 0
            while (true) {
                try {
                    return downloadSegmented(
                        current, dest, work, stateFile, saved, info, size, conns,
                        rateLimitBps, shouldStop, progress,
                    )
                } catch (e: RangeIgnored) {
                    saved = null // ranged claims were a lie; start the stream clean
                    break
                } catch (e: LinkExpired) {
                    refreshes++
                    if (refreshes > MAX_REFRESHES) {
                        throw HorseError("link expired and could not be refreshed")
                    }
                    val (newUrl, newSaved) = refresh(url, current, info, stateFile)
                    current = newUrl
                    saved = newSaved
                }
            }
        }
        return downloadSingle(
            current, dest, work, stateFile, saved, info, rateLimitBps, shouldStop, progress,
        )
    }

    /**
     * Re-resolve the source URL after a 403/410. Returns the fresh URL
     * and the segment map re-pointed at it — but only when the fresh
     * probe describes the *same file* (same size, non-conflicting
     * validators), so bytes already on disk stay valid.
     */
    private fun refresh(
        sourceUrl: String,
        oldUrl: String,
        origInfo: ProbeInfo,
        stateFile: File,
    ): Pair<String, StateJson?> {
        val fresh = probeRanges(sourceUrl)
        if (fresh.status >= 400) throw HorseError("link expired (HTTP ${fresh.status})")
        val newUrl = fresh.finalUrl.ifEmpty { sourceUrl }
        if (newUrl == oldUrl) {
            throw HorseError(
                "link expired (HTTP 403) and re-resolving the source gave the same URL"
            )
        }
        if (fresh.size != origInfo.size) {
            throw HorseError("refreshed link points at a different file")
        }
        if (origInfo.etag != null && fresh.etag != null && origInfo.etag != fresh.etag) {
            throw HorseError("refreshed link points at a different file")
        }
        if (origInfo.lastModified != null && fresh.lastModified != null &&
            origInfo.lastModified != fresh.lastModified
        ) {
            throw HorseError("refreshed link points at a different file")
        }
        val saved = stateLoad(stateFile)?.copy(url = newUrl)
        return newUrl to saved
    }

    // ---------------------------------------------------------- segmented --

    private class Segment(val start: Long, var pos: Long, var end: Long) {
        var owner: Int? = null
        var fails = 0
        fun done(): Long = min(pos, end) - start
    }

    private class Shared(
        val url: String,
        val info: ProbeInfo,
        val stateFile: File,
        val segments: MutableList<Segment>,
        val prog: Progress,
        val limiter: RateLimiter,
        val shouldStop: (() -> String?)?,
    ) {
        val lock = Any()
        val speeds = mutableMapOf<Int, Double>()          // worker -> EMA bytes/sec
        val speedBytes = mutableMapOf<Int, Pair<Long, Double>>()
        var failure: HorseError? = null

        // Self-healing: how many connections may fly at once (a 429/503
        // halves it) and how many claims are in flight right now.
        var cap: Int = 1
        var active: Int = 0

        /** claim() sentinel: work remains but the connection cap is full. */
        val waitSentinel = Segment(-1, -1, -1)

        /** Hand a segment back and free its connection slot. */
        fun release(seg: Segment) = synchronized(lock) {
            seg.owner = null
            if (active > 0) active--
        }

        fun persist() {
            val rows = synchronized(lock) {
                segments.sortedBy { it.start }
                    .map { SegRow(it.start, it.end, it.done()) }
            }
            stateSave(stateFile, stateDict(url, info, rows))
        }

        fun recordFailure(e: HorseError) = synchronized(lock) {
            if (failure == null) failure = e
        }

        fun noteSpeed(worker: Int, n: Long) {
            // Call only while holding lock.
            val now = nowSec()
            val (bytes, since) = speedBytes[worker] ?: (0L to now)
            val total = bytes + n
            val dt = now - since
            if (dt >= 0.25) {
                val inst = total / max(dt, 0.001)
                val old = speeds[worker] ?: 0.0
                speeds[worker] = if (old == 0.0) inst else 0.6 * old + 0.4 * inst
                speedBytes[worker] = 0L to now
            } else {
                speedBytes[worker] = total to since
            }
        }

        /** Take orphaned work, or split the segment finishing last. */
        fun claim(worker: Int): Segment? = synchronized(lock) {
            val unfinished = segments.any { it.end > it.pos }
            if (unfinished && active >= cap) {
                return@synchronized waitSentinel // cap full — park, stay ready
            }
            val orphans = segments.filter { it.owner == null && it.end > it.pos }
            if (orphans.isNotEmpty()) {
                val s = orphans.maxWithOrNull(
                    compareBy<Segment> { it.end - it.pos }.thenByDescending { it.start }
                )!!
                s.owner = worker
                active++
                return@synchronized s
            }
            val owned = segments.filter {
                it.owner != null && it.end - it.pos >= 2 * SPLIT_MIN
            }
            if (owned.isEmpty()) {
                return@synchronized if (unfinished) waitSentinel else null
            }

            fun eta(s: Segment): Double {
                val v = speeds[s.owner] ?: 0.0
                val rem = (s.end - s.pos).toDouble()
                return if (v > 0) rem / v else rem
            }
            val s = owned.maxWithOrNull(
                compareBy<Segment> { eta(it) }.thenBy { it.end - it.pos }
            )!!
            val rem = s.end - s.pos
            val ownerV = speeds[s.owner] ?: 0.0
            val measured = speeds.values.filter { it > 0 }
            val avg = if (measured.isEmpty()) 0.0 else measured.average()
            // Proportional split: the owner keeps the share it can finish
            // in the same time the newcomer needs for the rest.
            var keep = if (ownerV > 0 && avg > 0) {
                (rem * ownerV / (ownerV + avg)).toLong()
            } else {
                rem / 2
            }
            keep = keep.coerceIn(SPLIT_MIN, rem - SPLIT_MIN)
            var mid = s.pos + keep
            val aligned = mid - mid % (64 * 1024)
            if (aligned >= s.pos + SPLIT_MIN && s.end - aligned >= SPLIT_MIN) {
                mid = aligned
            }
            val fresh = Segment(mid, mid, s.end)
            fresh.owner = worker
            s.end = mid
            segments.add(fresh)
            active++
            fresh
        }
    }

    @Suppress("LongParameterList")
    private fun downloadSegmented(
        url: String,
        dest: File,
        work: File,
        stateFile: File,
        saved: StateJson?,
        info: ProbeInfo,
        size: Long,
        connections: Int,
        rateLimitBps: Long,
        shouldStop: (() -> String?)?,
        progress: ((Long, Long?, Double) -> Unit)?,
    ): File {
        val segments = mutableListOf<Segment>()
        if (saved != null) {
            val raw = saved.segments.sortedBy { it.start }
            val ok = raw.isNotEmpty() && raw.first().start == 0L &&
                raw.last().end == size &&
                raw.zipWithNext().all { (a, b) -> a.end == b.start }
            if (ok) {
                for (s in raw) {
                    segments.add(Segment(s.start, s.start + s.done, s.end))
                }
            }
        }
        if (segments.isEmpty()) {
            RandomAccessFile(work, "rw").use { it.setLength(size) }
            segments.add(Segment(0, 0, size))
        }

        val sh = Shared(
            url = url,
            info = info,
            stateFile = stateFile,
            segments = segments,
            prog = Progress(size, segments.sumOf { it.done() }, progress),
            limiter = RateLimiter(rateLimitBps),
            shouldStop = shouldStop,
        )
        sh.cap = max(1, connections)
        sh.prog.push(force = true)

        val threads = (0 until max(1, connections)).map { wid ->
            Thread { workerLoop(sh, work, wid) }.apply { start() }
        }
        threads.forEach { it.join() }

        sh.persist()
        sh.prog.push(force = true)
        synchronized(sh.lock) {
            sh.failure?.let { throw it }
        }
        if (sh.prog.done != size) {
            throw HorseError("incomplete: ${sh.prog.done} of $size bytes")
        }
        if (!work.renameTo(dest)) {
            work.copyTo(dest, overwrite = true)
            work.delete()
        }
        stateFile.delete()
        return dest
    }

    private fun workerLoop(sh: Shared, work: File, wid: Int) {
        while (true) {
            synchronized(sh.lock) {
                if (sh.failure != null) return // a sibling already decided
            }
            val seg = sh.claim(wid) ?: return
            if (seg === sh.waitSentinel) {
                Thread.sleep(100) // cap full or owners busy; stay ready
                continue
            }
            try {
                runClaim(sh, work, wid, seg)
                sh.release(seg)
            } catch (e: Throttled) {
                sh.release(seg)
                synchronized(sh.lock) {
                    // 429/503: the server asked for fewer of us.
                    sh.cap = max(1, sh.cap / 2)
                }
                Thread.sleep(2000) // ease off, keep the bytes
            } catch (e: HorseError) {
                sh.release(seg)
                when (e) {
                    is HorsePaused, is RangeIgnored, is LinkExpired, is HorseFallback -> {
                        sh.recordFailure(e)
                        return
                    }
                    else -> {
                        when (sh.shouldStop?.invoke()) {
                            "pause" -> {
                                sh.recordFailure(HorsePaused())
                                return
                            }
                            "cancel" -> {
                                sh.recordFailure(HorseError("canceled"))
                                return
                            }
                        }
                        val bad = synchronized(sh.lock) {
                            seg.fails += 1
                            seg.fails >= 6
                        }
                        if (bad) {
                            sh.recordFailure(e)
                            return
                        }
                    }
                }
            } catch (e: Exception) {
                sh.release(seg)
                when (sh.shouldStop?.invoke()) {
                    "pause" -> {
                        sh.recordFailure(HorsePaused())
                        return
                    }
                    "cancel" -> {
                        sh.recordFailure(HorseError("canceled"))
                        return
                    }
                }
                val bad = synchronized(sh.lock) {
                    seg.fails += 1
                    seg.fails >= 6
                }
                if (bad) {
                    sh.recordFailure(HorseError(e.message ?: "segment failed"))
                    return
                }
            }
        }
    }

    private fun runClaim(sh: Shared, work: File, wid: Int, seg: Segment) {
        val (frm, end0) = synchronized(sh.lock) { seg.pos to seg.end }
        val conn = open(sh.url, frm to end0 - 1)
        try {
            when (val code = conn.responseCode) {
                429, 503 -> throw Throttled()
                403, 410 -> throw LinkExpired()
                416 -> throw HorseError("range not satisfiable (HTTP 416)")
                else -> if (code >= 400) throw HorseError("HTTP $code")
            }
            if (conn.responseCode != 206) throw RangeIgnored()
            // Validation: the bytes must start where we asked, and
            // belong to the file we probed — never write a misaligned
            // or foreign body at a good file's offsets.
            val crange = parseContentRange(conn.getHeaderField("Content-Range"))
            if (crange != null) {
                if (crange.first != frm) {
                    throw HorseError(
                        "server sent the wrong range (asked $frm, got ${crange.first})"
                    )
                }
                val total = crange.third
                val sz = sh.info.size
                if (total != null && sz != null && total != sz) {
                    throw HorseError("file changed mid-download")
                }
            }
            val stream: InputStream = conn.inputStream
            RandomAccessFile(work, "rw").use { fh ->
                val buf = ByteArray(CHUNK)
                var lastAdvance = nowSec()
                while (true) {
                    checkStop(sh.shouldStop)
                    val (pos, end) = synchronized(sh.lock) { seg.pos to seg.end }
                    if (pos >= end) return
                    val want = min(CHUNK.toLong(), end - pos).toInt()
                    val n = stream.read(buf, 0, want)
                    if (n < 0) throw HorseError("connection dropped mid-segment")
                    if (n == 0) continue
                    if (nowSec() - lastAdvance > stallSeconds) {
                        // Self-healing: bytes stopped arriving on an open
                        // connection. Reopen it (a segment retry) instead
                        // of letting it hold the download hostage.
                        throw HorseError("connection stalled — reopening")
                    }
                    // Our bound may have shrunk (a split) mid-read: only
                    // the bytes up to the current end are ours.
                    val allowed = synchronized(sh.lock) {
                        val a = min(n.toLong(), max(0L, seg.end - pos)).toInt()
                        if (a > 0) seg.pos = pos + a
                        a
                    }
                    if (allowed > 0) {
                        lastAdvance = nowSec()
                        sh.limiter.take(allowed)
                        fh.seek(pos)
                        fh.write(buf, 0, allowed)
                        synchronized(sh.lock) { sh.noteSpeed(wid, allowed.toLong()) }
                        sh.prog.add(allowed)
                        sh.prog.push()
                        sh.persist()
                    }
                    if (allowed < n) return // shrunk past the stream; done
                }
            }
        } finally {
            conn.disconnect()
        }
    }

    // ------------------------------------------------------------- single --

    @Suppress("LongParameterList")
    private fun downloadSingle(
        url: String,
        dest: File,
        work: File,
        stateFile: File,
        saved: StateJson?,
        info: ProbeInfo,
        rateLimitBps: Long,
        shouldStop: (() -> String?)?,
        progress: ((Long, Long?, Double) -> Unit)?,
    ): File {
        val size = info.size
        // Single-stream state is one segment starting at 0, so its `done`
        // is exactly the contiguous prefix we already hold.
        var have = saved?.segments?.firstOrNull()?.done ?: 0L
        have = if (work.exists()) min(have, work.length()) else 0L
        if (have > 0 && size == null) {
            have = 0 // cannot verify a resume point without a total; restart
        }

        val byteRange = if (have > 0 && size != null && info.ranges) {
            have to size - 1
        } else {
            null
        }

        val prog = Progress(size, have, progress)
        prog.push(force = true)
        val limiter = RateLimiter(rateLimitBps)

        fun pump(conn: HttpURLConnection, append: Boolean) {
            val stream = conn.inputStream
            RandomAccessFile(work, "rw").use { fh ->
                if (append) {
                    fh.seek(have)
                } else {
                    fh.setLength(0)
                }
                val buf = ByteArray(CHUNK)
                while (true) {
                    checkStop(shouldStop)
                    val n = stream.read(buf)
                    if (n < 0) break
                    if (n == 0) continue
                    limiter.take(n)
                    fh.write(buf, 0, n)
                    have += n
                    prog.add(n)
                    prog.push()
                    stateSave(
                        stateFile,
                        stateDict(url, info, listOf(SegRow(0, size ?: have, have))),
                    )
                }
            }
        }

        val conn = open(url, byteRange)
        try {
            val code = conn.responseCode
            if (code >= 400) throw HorseError("HTTP $code")
            // Validate the resume before trusting a byte of it: a 200
            // (Range ignored) or a 206 starting anywhere else would
            // glue wrong bytes onto our good prefix.
            var untrusted = false
            if (byteRange != null) {
                val crange = parseContentRange(conn.getHeaderField("Content-Range"))
                if (code != 206 || (crange != null && crange.first != have)) {
                    untrusted = true
                }
            }
            if (untrusted) {
                have = 0
                prog.setDone(0)
                conn.disconnect()
                // Fresh, honest stream from byte 0 — the response above
                // was discarded unread rather than glued onto our prefix.
                val fresh = open(url, null)
                try {
                    val code2 = fresh.responseCode
                    if (code2 >= 400) throw HorseError("HTTP $code2")
                    pump(fresh, append = false)
                } finally {
                    fresh.disconnect()
                }
            } else {
                pump(conn, append = have > 0)
            }
        } catch (e: HorseError) {
            throw e
        } catch (e: Exception) {
            throw HorseError(e.message ?: "download failed")
        } finally {
            conn.disconnect()
        }

        if (size != null && have != size) {
            throw HorseError("incomplete: $have of $size bytes")
        }
        prog.push(force = true)
        if (!work.renameTo(dest)) {
            work.copyTo(dest, overwrite = true)
            work.delete()
        }
        stateFile.delete()
        return dest
    }
}
