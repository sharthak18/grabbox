package app.grabbox

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.webkit.MimeTypeMap
import android.widget.Toast
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.FileProvider
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import app.grabbox.work.DownloadService
import app.grabbox.work.Engine
import app.grabbox.work.JobStore
import com.yausername.youtubedl_android.YoutubeDL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** All screen state + the glue between UI, JobStore and DownloadService. */
class GrabViewModel : ViewModel() {

    var urlInput by mutableStateOf("")
    var probing by mutableStateOf(false)
    var probe by mutableStateOf<Engine.ProbeInfo?>(null)
    var probeError by mutableStateOf<String?>(null)
    var probeHint by mutableStateOf<String?>(null)

    var chosenKind by mutableStateOf("video")
    var chosenQuality by mutableStateOf("best")
    var customName by mutableStateOf("")

    var downloadDir by mutableStateOf(Engine.defaultDownloadDir().absolutePath)
    var settingsOpen by mutableStateOf(false)
    var statusText by mutableStateOf<String?>(null)

    /** Which face is showing: the grabber ("home") or the browser ("browse"). */
    var tab by mutableStateOf("home")

    // The horse, persisted — same spec as the desktop engines.
    var connections by mutableStateOf(Engine.connections)
    var speedLimitKbps by mutableStateOf(Engine.speedLimitKbps)
    var turbo by mutableStateOf(Engine.turbo)

    val jobs = JobStore.jobs

    /** Restore settings (folder + horse) once, at app start. */
    fun loadSettings() {
        val prefs = GrabBoxApp.instance.getSharedPreferences("grabbox", Context.MODE_PRIVATE)
        downloadDir = prefs.getString("download_dir", downloadDir) ?: downloadDir
        connections = prefs.getInt("connections", 8).coerceIn(1, 16)
        speedLimitKbps = prefs.getInt("speed_limit_kbps", 0).coerceAtLeast(0)
        turbo = prefs.getBoolean("turbo", true)
        applyHorse()
    }

    private fun applyHorse() {
        Engine.connections = connections
        Engine.speedLimitKbps = speedLimitKbps
        Engine.turbo = turbo
        GrabBoxApp.instance.getSharedPreferences("grabbox", Context.MODE_PRIVATE)
            .edit()
            .putString("download_dir", downloadDir)
            .putInt("connections", connections)
            .putInt("speed_limit_kbps", speedLimitKbps)
            .putBoolean("turbo", turbo)
            .apply()
    }

    fun setConnections(value: Int) {
        connections = value.coerceIn(1, 16)
        applyHorse()
    }

    fun setSpeedLimitKbps(value: Int) {
        speedLimitKbps = value.coerceAtLeast(0)
        applyHorse()
    }

    fun setTurbo(value: Boolean) {
        turbo = value
        applyHorse()
    }

    /** A link grabbed in the browser: hand it to the probe-and-pick flow. */
    fun grabFromBrowser(url: String) {
        tab = "home"
        pasteAndAnalyze(url)
    }

    private var probeJob: Job? = null

    // ------------------------------------------------------------- probe --

    fun pasteAndAnalyze(url: String) {
        urlInput = url.trim()
        analyze()
    }

    fun analyze() {
        val url = urlInput.trim()
        if (url.isEmpty()) return
        probeJob?.cancel()
        probing = true
        probe = null
        probeError = null
        probeHint = null
        probeJob = viewModelScope.launch(Dispatchers.IO) {
            val result = Engine.probe(url)
            withContext(Dispatchers.Main) {
                probing = false
                if (result.ok) {
                    probe = result
                    chosenKind = when {
                        result.kind == "audio" -> "audio"
                        result.hasVideo || result.kind == "video" -> "video"
                        else -> "file"
                    }
                    chosenQuality = when (chosenKind) {
                        "audio" -> "m4a"
                        "video" -> "best"
                        else -> "original"
                    }
                    customName = ""
                } else {
                    probeError = result.error ?: "Could not read that link"
                    probeHint = result.hint
                }
            }
        }
    }

    fun dismissProbe() {
        probe = null
        probeError = null
        probeHint = null
    }

    // ---------------------------------------------------------- download --

    fun startDownload(context: Context) {
        val p = probe ?: return
        val job = JobStore.newJob(
            url = p.url,
            kind = chosenKind,
            quality = chosenQuality,
            playlist = p.isPlaylist,
            outDir = downloadDir,
            customName = customName.takeIf { it.isNotBlank() }?.let { Engine.sanitizeFilename(it) },
        )
        JobStore.update(job.id) {
            it.copy(
                title = p.title,
                filename = p.directFilename ?: p.title,
            )
        }
        DownloadService.start(context, job.id)
        dismissProbe()
        urlInput = ""
        statusText = "Queued — watch the Queue below (or the notification)"
    }

    fun cancelJob(context: Context, id: String) = DownloadService.cancel(context, id)

    fun pauseJob(context: Context, id: String) = DownloadService.pause(context, id)

    fun resumeJob(context: Context, id: String) = DownloadService.resume(context, id)

    /** Retry is resume: both engines continue partial files. */
    fun retryJob(context: Context, id: String) = DownloadService.resume(context, id)

    fun clearFinished() = JobStore.clearFinished()

    fun openFile(context: Context, job: JobStore.Job) {
        val path = job.filePath ?: return
        val file = File(path)
        if (!file.exists()) {
            toast(context, "File not found")
            return
        }
        val uri: Uri = try {
            FileProvider.getUriForFile(context, "app.grabbox.provider", file)
        } catch (e: Exception) {
            toast(context, "Cannot share this path")
            return
        }
        val mime = MimeTypeMap.getSingleton()
            .getMimeTypeFromExtension(file.extension.lowercase()) ?: "*/*"
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            context.startActivity(intent)
        } catch (e: ActivityNotFoundException) {
            toast(context, "No app can open this file")
        }
    }

    // ---------------------------------------------------------- settings --

    val dirOptions: List<Pair<String, String>>
        get() {
            val dl = android.os.Environment.getExternalStoragePublicDirectory(
                android.os.Environment.DIRECTORY_DOWNLOADS
            )
            val music = android.os.Environment.getExternalStoragePublicDirectory(
                android.os.Environment.DIRECTORY_MUSIC
            )
            val movies = android.os.Environment.getExternalStoragePublicDirectory(
                android.os.Environment.DIRECTORY_MOVIES
            )
            return listOf(
                File(dl, "GrabBox").absolutePath to "Downloads/GrabBox",
                dl.absolutePath to "Downloads",
                File(music, "GrabBox").absolutePath to "Music/GrabBox",
                File(movies, "GrabBox").absolutePath to "Movies/GrabBox",
            )
        }

    fun setDir(path: String) {
        downloadDir = path
        File(path).mkdirs()
    }

    fun updateEngine(context: Context) {
        viewModelScope.launch(Dispatchers.IO) {
            val msg = try {
                YoutubeDL.getInstance().updateYoutubeDL(context.applicationContext)
                "Engine updated — fresh yt-dlp is now active"
            } catch (e: Exception) {
                "Update failed: ${(e.message ?: "unknown").take(100)}"
            }
            withContext(Dispatchers.Main) { statusText = msg }
        }
    }

    fun sendFeedback(context: Context) {
        val subject = "GrabBox Android feedback (v${BuildConfig.VERSION_NAME})"
        val body = buildString {
            append("Hi! GrabBox feedback:\n\n\n\n----\n")
            append("(auto: Android ").append(android.os.Build.VERSION.RELEASE)
            append(" · app ").append(BuildConfig.VERSION_NAME).append(")")
        }
        val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:feedit18@gmail.com"))
            .putExtra(Intent.EXTRA_SUBJECT, subject)
            .putExtra(Intent.EXTRA_TEXT, body)
        try {
            context.startActivity(intent)
        } catch (e: ActivityNotFoundException) {
            toast(context, "No email app found — write to feedit18@gmail.com")
        }
    }

    fun clearStatus() { statusText = null }

    private fun toast(context: Context, text: String) =
        Toast.makeText(context, text, Toast.LENGTH_SHORT).show()
}
