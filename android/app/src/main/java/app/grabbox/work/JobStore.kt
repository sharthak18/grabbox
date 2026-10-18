package app.grabbox.work

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.abs

/**
 * The job table the service writes and the UI reads — persisted to
 * jobs.json in the app's files dir, so the queue survives the app being
 * swiped away or the phone rebooting. Anything mid-flight comes back
 * *paused*: its .part file is still on disk and Resume continues it.
 */
object JobStore {

    data class Job(
        val id: String,
        val url: String,
        val title: String = "",
        val kind: String = "file",
        val quality: String = "best",
        val playlist: Boolean = false,
        val status: String = "queued",   // queued|running|paused|done|error|canceled
        val percent: Float = 0f,         // 0..100, -1 = unknown
        val etaSec: Long = 0L,
        val attempt: Int = 1,
        val attempts: Int = 1,
        val filename: String? = null,
        val filePath: String? = null,
        val outDir: String,
        val error: String? = null,
        val hint: String? = null,
        val customName: String? = null,
        val createdAt: Long = System.currentTimeMillis(),
    ) {
        val active: Boolean get() = status == "queued" || status == "running"
    }

    private val counter = AtomicInteger(1)
    private val _jobs = MutableStateFlow<List<Job>>(emptyList())
    val jobs: StateFlow<List<Job>> = _jobs.asStateFlow()

    @Volatile
    private var storeFile: File? = null

    /** Load the saved queue once, at app start. */
    fun init(context: Context) {
        if (storeFile != null) return
        val file = File(context.filesDir, "jobs.json")
        storeFile = file
        try {
            if (!file.exists()) return
            val rows = JSONArray(file.readText())
            val restored = mutableListOf<Job>()
            for (i in 0 until rows.length()) {
                val r = rows.optJSONObject(i) ?: continue
                val status = r.optString("status", "paused")
                restored.add(
                    Job(
                        id = r.optString("id"),
                        url = r.optString("url"),
                        title = r.optString("title"),
                        kind = r.optString("kind", "file"),
                        quality = r.optString("quality", "best"),
                        playlist = r.optBoolean("playlist"),
                        // Interrupted mid-flight: resumable, not lost.
                        status = if (status == "running" || status == "queued") {
                            "paused"
                        } else {
                            status
                        },
                        percent = r.optDouble("percent", 0.0).toFloat(),
                        filename = r.optString("filename").ifBlank { null },
                        filePath = r.optString("filePath").ifBlank { null },
                        outDir = r.optString("outDir"),
                        customName = r.optString("customName").ifBlank { null },
                        createdAt = r.optLong("createdAt", System.currentTimeMillis()),
                    )
                )
            }
            _jobs.value = restored
        } catch (e: Exception) {
            // A corrupt jobs.json must never take the app down with it.
        }
    }

    private fun save() {
        val file = storeFile ?: return
        try {
            val rows = JSONArray()
            for (j in _jobs.value) {
                rows.put(
                    JSONObject()
                        .put("id", j.id)
                        .put("url", j.url)
                        .put("title", j.title)
                        .put("kind", j.kind)
                        .put("quality", j.quality)
                        .put("playlist", j.playlist)
                        .put("status", j.status)
                        .put("percent", j.percent.toDouble())
                        .put("filename", j.filename ?: "")
                        .put("filePath", j.filePath ?: "")
                        .put("outDir", j.outDir)
                        .put("customName", j.customName ?: "")
                        .put("createdAt", j.createdAt)
                )
            }
            val tmp = File(file.parentFile, "jobs.json.tmp")
            tmp.writeText(rows.toString())
            tmp.renameTo(file)
        } catch (e: Exception) {
            // Persistence is best-effort; the download matters more.
        }
    }

    fun newJob(
        url: String, kind: String, quality: String, playlist: Boolean,
        outDir: String, customName: String?,
    ): Job {
        val job = Job(
            id = "j${counter.getAndIncrement()}-${UUID.randomUUID().toString().take(6)}",
            url = url, kind = kind, quality = quality, playlist = playlist,
            outDir = outDir, customName = customName,
            attempts = Engine.ladder(url).size,
        )
        _jobs.update { listOf(job) + it }
        save()
        return job
    }

    fun update(id: String, transform: (Job) -> Job) {
        var shouldSave = false
        _jobs.update { list ->
            list.map {
                if (it.id == id) {
                    val n = transform(it)
                    if (n.status != it.status || n.filePath != it.filePath ||
                        abs(n.percent - it.percent) >= 2f
                    ) {
                        shouldSave = true
                    }
                    n
                } else {
                    it
                }
            }
        }
        if (shouldSave) save()
    }

    fun get(id: String): Job? = _jobs.value.firstOrNull { it.id == id }

    fun hasActive(): Boolean = _jobs.value.any { it.active }

    fun clearFinished() {
        _jobs.update { list -> list.filter { it.active } }
        save()
    }
}
