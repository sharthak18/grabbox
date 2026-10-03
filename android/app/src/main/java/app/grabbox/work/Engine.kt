package app.grabbox.work

import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Everything the app knows about talking to yt-dlp — the same semantics as the
 * desktop engines (sniff direct files first, quality selectors, the YouTube
 * client retry ladder, and plain-English failure hints).
 */
object Engine {

    // ------------------------------------------------------------- models --

    data class ProbeInfo(
        val ok: Boolean,
        val url: String,
        val kind: String = "file",          // video | audio | image | app | archive | file
        val title: String = "",
        val thumb: String? = null,
        val durationSec: Double? = null,
        val uploader: String? = null,
        val isPlaylist: Boolean = false,
        val playlistCount: Int = 0,
        val heights: List<Int> = emptyList(),
        val hasVideo: Boolean = false,
        val hasAudio: Boolean = false,
        val directSize: Long? = null,
        val directFilename: String? = null,
        val note: String? = null,
        val error: String? = null,
        val hint: String? = null,
    )

    // Friendly quality labels, keyed by what gets passed to buildRequest().
    val VIDEO_QUALITIES = listOf(
        "best" to "Best",
        "2160" to "4K",
        "1440" to "2K",
        "1080" to "1080p",
        "720" to "720p",
        "480" to "480p",
        "360" to "360p",
    )
    val AUDIO_QUALITIES = listOf(
        "m4a" to "M4A · plays everywhere",
        "mp3" to "MP3 · compatible",
        "opus" to "Opus · small files",
        "flac" to "FLAC · lossless",
        "best" to "Original stream",
    )

    // ------------------------------------------------------------ probing --

    fun probe(url: String): ProbeInfo {
        // 1. Plain file links (images, installers, archives) don't need yt-dlp.
        sniff(url)?.let { direct ->
            if (direct.kind in listOf("video", "audio", "image", "app", "archive")) {
                return direct
            }
        }

        // 2. yt-dlp probe.
        val request = YoutubeDLRequest(url).apply {
            addOption("-J")
            addOption("--flat-playlist")
            addOption("--playlist-items", "1-100")
            addOption("--no-warnings")
            addOption("--socket-timeout", "30")
        }
        val out = try {
            YoutubeDL.getInstance().execute(request).out
        } catch (e: Exception) {
            return ProbeInfo(
                ok = false, url = url,
                error = firstLine(e.message ?: "probe failed"),
                hint = diagnose(e.message ?: ""),
            )
        }

        return try {
            mapInfo(url, JSONObject(out))
        } catch (e: Exception) {
            ProbeInfo(ok = false, url = url, error = "Could not parse info for this link", hint = diagnose(out))
        }
    }

    private fun mapInfo(url: String, info: JSONObject): ProbeInfo {
        if (info.optString("_type") == "playlist") {
            val entries = info.optJSONArray("entries")
            return ProbeInfo(
                ok = true, url = url, kind = "video",
                title = info.optString("title", url),
                thumb = info.optString("thumbnail").ifBlank { null },
                isPlaylist = true,
                playlistCount = entries?.length() ?: 0,
            )
        }

        val formats = info.optJSONArray("formats")
        val heights = mutableListOf<Int>()
        var hasVideo = false
        var hasAudio = false
        if (formats != null) {
            for (i in 0 until formats.length()) {
                val f = formats.optJSONObject(i) ?: continue
                val video = f.optString("vcodec", "none") != "none"
                val audio = f.optString("acodec", "none") != "none"
                if (video) {
                    hasVideo = true
                    val h = f.optInt("height", 0)
                    if (h > 0) heights.add(h)
                } else if (audio) {
                    hasAudio = true
                }
            }
        }

        val kind = when {
            hasVideo -> "video"
            hasAudio -> "audio"
            else -> "file"
        }
        val maxH = heights.maxOrNull()
        val note = if (isYoutube(url) && maxH != null && maxH in 1..360) {
            "Only ${maxH}p was offered — YouTube is withholding HD formats " +
                "(PO token / SABR). GrabBox retries token-free clients on its own."
        } else {
            null
        }

        return ProbeInfo(
            ok = true, url = url, kind = kind,
            title = info.optString("title", url),
            thumb = info.optString("thumbnail").ifBlank { null },
            durationSec = info.optDouble("duration").takeIf { it > 0 },
            uploader = info.optString("uploader", info.optString("channel", "")).ifBlank { null },
            heights = heights.distinct().sortedDescending(),
            hasVideo = hasVideo, hasAudio = hasAudio,
            note = note,
        )
    }

    // ---------------------------------------------------------- download --

    /** The YouTube retry ladder: (label, extra CLI args). Same as desktop. */
    fun ladder(url: String): List<Pair<String, List<String>>> {
        if (!isYoutube(url)) return listOf("default" to emptyList())
        return listOf(
            "default clients" to emptyList(),
            "tv client (no PO token needed)" to
                listOf("--extractor-args", "youtube:player_client=tv"),
            "skip android clients, add web_safari" to
                listOf("--extractor-args", "youtube:player_client=-android_vr,web_safari"),
            "web_embedded + web + tv" to
                listOf("--extractor-args", "youtube:player_client=web_embedded,web,tv"),
            "IPv4 only, tv client" to
                listOf("--extractor-args", "youtube:player_client=tv", "--force-ipv4"),
        )
    }

    fun buildRequest(
        url: String,
        kind: String,
        quality: String,
        outDir: File,
        playlist: Boolean,
        filename: String?,
        rungArgs: List<String>,
    ): YoutubeDLRequest {
        val safe = if (!filename.isNullOrBlank()) sanitizeFilename(filename) else ""
        val template = when {
            safe.isNotBlank() -> if (looksLikeFinalName(safe, kind)) safe else "$safe.%(ext)s"
            playlist -> "%(playlist_index)02d - %(title)s.%(ext)s"
            else -> "%(title)s.%(ext)s"
        }
        val request = YoutubeDLRequest(url)
        request.addOption("--no-warnings")
        request.addOption("--retries", "10")
        request.addOption("--fragment-retries", "10")
        request.addOption("--paths", "home:${outDir.absolutePath}")
        request.addOption("-o", template)
        // No --windows-filenames here: it makes yt-dlp replace every
        // non-ASCII character *and* every space with "_", so a title like
        // "My Song" lands as "My_Song" and a Bangla or Japanese one becomes a
        // row of underscores. Android's filesystem has none of the Windows
        // restrictions that flag exists for.
        request.addOption("--embed-metadata")
        if (playlist) {
            request.addOption("--yes-playlist")
            request.addOption("--download-archive", File(outDir, ".grabbox-archive.txt").absolutePath)
        } else {
            request.addOption("--no-playlist")
        }
        when (kind) {
            "audio" -> {
                request.addOption("-f", "bestaudio/best")
                request.addOption("-x")
                // "best" = original stream, no re-encode (don't pass --audio-format,
                // since "best" isn't a real format and yt-dlp would error).
                if (quality != "best" && quality != "original") {
                    request.addOption("--audio-format", quality)
                }
                if (quality == "mp3") request.addOption("--audio-quality", "0")
                request.addOption("--embed-thumbnail")
            }
            "video" -> {
                val selector = if (quality.all { it.isDigit() }) {
                    "bv*[height<=$quality]+ba/b[height<=$quality]/bv*+ba/b"
                } else {
                    "bv*+ba/b"
                }
                request.addOption("-f", selector)
                request.addOption("--merge-output-format", "mp4")
                request.addOption("--embed-thumbnail")
            }
            else -> request.addOption("-f", "b")
        }
        if (playlist) request.addOption("--ignore-errors")
        var i = 0
        while (i < rungArgs.size) {
            val arg = rungArgs[i]
            val next = rungArgs.getOrNull(i + 1)
            if (next != null && !next.startsWith("--")) {
                request.addOption(arg, next)
                i += 2
            } else {
                request.addOption(arg)
                i += 1
            }
        }
        return request
    }

    // ------------------------------------------------------------ helpers --

    fun isYoutube(url: String): Boolean {
        val u = url.lowercase()
        return listOf("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")
            .any { u.contains(it) }
    }

    fun diagnose(text: String): String {
        val t = text.lowercase()
        fun has(vararg needles: String) = needles.any { t.contains(it) }
        return when {
            has("sabr", "missing a url", "po token", "po_token") ->
                "YouTube is withholding formats (SABR / PO token). Retrying with token-free clients; updating yt-dlp restores full quality."
            has("http error 403", "forbidden") ->
                "YouTube refused the stream URL (403). Almost always an outdated yt-dlp — update it in Settings, then retry."
            has("http error 429", "too many requests") ->
                "Rate limited (429). Wait 10–20 minutes and download less at once."
            has("sign in to confirm", "not a bot") ->
                "YouTube wants a logged-in session — nothing we can do from a phone app without cookies yet."
            has("requested format is not available") ->
                "That format is not offered for this item. Pick another quality."
            has("private video", "members-only", "age-restricted", "video unavailable") ->
                "Private, age-restricted or members-only content."
            has("unable to download webpage", "tls", "ssl", "timed out", "temporary failure") ->
                "Network problem reaching the site — check connection or VPN."
            else -> ""
        }
    }

    fun firstLine(text: String, max: Int = 300): String {
        val first = text.trim().lines().firstOrNull { it.isNotBlank() } ?: text
        return if (first.length > max) first.take(max) + "…" else first.trim()
    }

    fun sanitizeFilename(name: String): String {
        val bad = charArrayOf('\\', '/', ':', '*', '?', '"', '<', '>', '|')
        return name.map { if (it in bad) ' ' else it }
            .joinToString("")
            .trim()
            .replace(Regex("\\s+"), " ")
    }

    private fun looksLikeFinalName(name: String, kind: String): Boolean {
        val ext = name.substringAfterLast('.', "").lowercase()
        if (ext.isBlank()) return false
        val audio = setOf("mp3", "m4a", "aac", "opus", "ogg", "oga", "flac", "wav", "wma", "aiff")
        val video = setOf("mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts", "mpg",
            "mpeg", "3gp", "wmv")
        val file = mutableSetOf<String>().apply {
            addAll(audio); addAll(video)
            addAll(listOf("jpg", "jpeg", "png", "gif", "webp", "bmp", "svg",
                "avif", "tif", "tiff", "ico", "heic", "exe", "msi", "apk", "dmg", "pkg", "deb",
                "rpm", "appimage", "jar", "ipa", "zip", "rar", "7z", "tar", "gz", "tgz", "bz2",
                "xz", "zst", "iso", "img", "pdf", "epub", "mobi", "txt", "md", "csv", "json",
                "xml", "srt", "vtt"))
        }
        return when (kind) {
            "audio" -> ext in audio
            "video" -> ext in video
            "file" -> ext in file
            else -> ext in file
        }
    }

    /** Default download home: the shared Downloads folder, GrabBox subdir. */
    fun defaultDownloadDir(): File {
        val dir = File(
            android.os.Environment.getExternalStoragePublicDirectory(
                android.os.Environment.DIRECTORY_DOWNLOADS
            ),
            "GrabBox",
        )
        dir.mkdirs()
        return dir
    }

    // ------------------------------------------------------- HEAD sniff --

    private data class Sniff(val kind: String, val filename: String?, val size: Long?)

    private val EXT_KIND = mapOf(
        "mp4" to "video", "mkv" to "video", "webm" to "video", "mov" to "video",
        "avi" to "video", "m4v" to "video", "flv" to "video", "ts" to "video",
        "mp3" to "audio", "m4a" to "audio", "aac" to "audio", "opus" to "audio",
        "ogg" to "audio", "flac" to "audio", "wav" to "audio", "wma" to "audio",
        "jpg" to "image", "jpeg" to "image", "png" to "image", "gif" to "image",
        "webp" to "image", "bmp" to "image", "svg" to "image", "avif" to "image",
        "heic" to "image",
        "exe" to "app", "msi" to "app", "apk" to "app", "dmg" to "app",
        "pkg" to "app", "deb" to "app", "rpm" to "app", "appimage" to "app",
        "zip" to "archive", "rar" to "archive", "7z" to "archive", "tar" to "archive",
        "gz" to "archive", "tgz" to "archive", "bz2" to "archive", "xz" to "archive",
        "zst" to "archive", "iso" to "archive",
    )

    private fun sniff(url: String): ProbeInfo? {
        return try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "HEAD"
                connectTimeout = 8000
                readTimeout = 8000
                instanceFollowRedirects = true
                setRequestProperty(
                    "User-Agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                )
            }
            conn.connect()
            val finalUrl = conn.url.toString()
            val cd = conn.getHeaderField("Content-Disposition")
            val size = conn.contentLengthLong.takeIf { it > 0 }
            conn.disconnect()

            val filename = filenameFrom(finalUrl, cd)
            val ext = filename.substringAfterLast('.', "").lowercase()
            val kind = EXT_KIND[ext] ?: return null
            ProbeInfo(
                ok = true, url = url, kind = kind,
                title = filename, directSize = size, directFilename = filename,
            )
        } catch (e: Exception) {
            null
        }
    }

    private fun filenameFrom(url: String, contentDisposition: String?): String {
        if (contentDisposition != null) {
            val m = Regex("filename\\*?=(?:UTF-8'')?\"?([^\";]+)", RegexOption.IGNORE_CASE)
                .find(contentDisposition)
            if (m != null) {
                return java.net.URLDecoder.decode(m.groupValues[1].trim(), "UTF-8")
            }
        }
        val path = url.substringBefore('?').substringBefore('#')
        val name = path.substringAfterLast('/')
        return if (name.isNotBlank()) java.net.URLDecoder.decode(name, "UTF-8") else "download"
    }
}
