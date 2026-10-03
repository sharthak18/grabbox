package app.grabbox.work

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import app.grabbox.GrabBoxApp
import app.grabbox.MainActivity
import app.grabbox.R
import com.yausername.youtubedl_android.YoutubeDL
import java.io.File

/**
 * Foreground service that actually runs the downloads: one thread per job,
 * walking the retry ladder, reporting progress into JobStore and into the
 * notification shade. Downloads keep going when the app is swiped away.
 */
class DownloadService : Service() {

    companion object {
        const val ACTION_START = "app.grabbox.action.START_DOWNLOAD"
        const val ACTION_CANCEL = "app.grabbox.action.CANCEL_DOWNLOAD"
        const val EXTRA_JOB_ID = "jobId"
        private const val NOTIFY_ACTIVE = 1001

        fun start(context: Context, jobId: String) {
            val intent = Intent(context, DownloadService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_JOB_ID, jobId)
            context.startForegroundService(intent)
        }

        fun cancel(context: Context, jobId: String) {
            val intent = Intent(context, DownloadService::class.java)
                .setAction(ACTION_CANCEL)
                .putExtra(EXTRA_JOB_ID, jobId)
            context.startService(intent)
        }
    }

    private val running = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    private val canceled = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CANCEL -> {
                val id = intent.getStringExtra(EXTRA_JOB_ID) ?: return START_NOT_STICKY
                canceled.add(id)
                YoutubeDL.getInstance().destroyProcessById(id)
                JobStore.update(id) {
                    if (it.active) it.copy(status = "canceled") else it
                }
                if (!JobStore.hasActive()) stopIfIdle()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                val id = intent.getStringExtra(EXTRA_JOB_ID) ?: return START_NOT_STICKY
                if (!running.contains(id)) {
                    running.add(id)
                    startInForeground(id)
                    Thread { runJob(id) }.start()
                }
                return START_STICKY
            }
        }
        return START_NOT_STICKY
    }

    private fun startInForeground(jobId: String) {
        val notification = progressNotification(jobId, "Getting ready…", -1f)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFY_ACTIVE, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFY_ACTIVE, notification)
        }
    }

    // ------------------------------------------------------------- worker --

    private fun runJob(jobId: String) {
        val job = JobStore.get(jobId) ?: return stopIfIdle()
        val outDir = File(job.outDir).apply { mkdirs() }
        val ladder = Engine.ladder(job.url)
        JobStore.update(jobId) { it.copy(status = "running", attempts = ladder.size) }

        var lastError = ""
        var lastHint = ""
        ladder.forEachIndexed { index, (_, rungArgs) ->
            if (jobId in canceled) return finish(jobId)
            val attempt = index + 1
            JobStore.update(jobId) { it.copy(attempt = attempt, percent = -1f) }
            val request = Engine.buildRequest(
                job.url, job.kind, job.quality, outDir, job.playlist,
                job.customName, rungArgs,
            )
            var lastNotify = 0L
            try {
                YoutubeDL.getInstance().execute(request, jobId) { progress, eta, line ->
                    val now = System.currentTimeMillis()
                    // youtubedl-android reports progress in 0..1; convert to percent.
                    val pct = when {
                        progress.isNaN() || progress < 0f -> -1f
                        progress <= 1f -> progress * 100f
                        else -> progress
                    }
                    JobStore.update(jobId) { it.copy(percent = pct, etaSec = eta) }
                    // final filename often arrives on output lines
                    if (line != null) {
                        extractPath(line)?.let { path ->
                            JobStore.update(jobId) {
                                it.copy(filePath = path, filename = File(path).name)
                            }
                        }
                    }
                    if (now - lastNotify > 1200) {
                        lastNotify = now
                        notifyProgress(jobId)
                    }
                }
                // execute() throws on non-zero exit; reaching here = success
                val final = JobStore.get(jobId)?.filePath
                    ?: newestFile(outDir)?.absolutePath
                    ?: outDir.absolutePath
                JobStore.update(jobId) {
                    it.copy(status = "done", percent = 100f, filePath = final,
                        filename = File(final).name, error = null, hint = null)
                }
                notifyDone(jobId)
                return finish(jobId)
            } catch (e: Exception) {
                if (jobId in canceled) {
                    JobStore.update(jobId) { it.copy(status = "canceled") }
                    return finish(jobId)
                }
                lastError = Engine.firstLine(e.message ?: "download failed")
                lastHint = Engine.diagnose(e.message ?: "")
                JobStore.update(jobId) { it.copy(error = lastError, hint = lastHint.ifBlank { null }) }
            }
        }

        // Every rung failed.
        JobStore.update(jobId) { it.copy(status = "error", error = lastError, hint = lastHint.ifBlank { null }) }
        notifyFailed(jobId, lastError)
        finish(jobId)
    }

    private fun finish(jobId: String) {
        running.remove(jobId)
        canceled.remove(jobId)
        stopIfIdle()
    }

    private fun stopIfIdle() {
        if (running.isEmpty()) {
            // REMOVE, not DETACH: DETACH leaves the "Getting ready…" progress
            // notification stranded in the shade next to the done/failed one.
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    // ---------------------------------------------------- path extraction --

    /** yt-dlp tells us the real file on lines like `[download] Destination: X`. */
    private fun extractPath(line: String): String? {
        val prefixes = listOf("[download] Destination: ", "[ExtractAudio] Destination: ")
        prefixes.forEach { p ->
            if (line.startsWith(p)) return line.removePrefix(p).trim()
        }
        if (line.startsWith("[Merger]") || line.startsWith("[MoveFiles]")) {
            val start = line.indexOf('"')
            val end = line.lastIndexOf('"')
            if (start in 0 until end) return line.substring(start + 1, end)
        }
        return null
    }

    private fun newestFile(dir: File): File? =
        dir.listFiles()?.filter { it.isFile && !it.name.startsWith(".") }
            ?.maxByOrNull { it.lastModified() }

    // -------------------------------------------------------- notification --

    private fun progressNotification(jobId: String, text: String, percent: Float): Notification {
        val job = JobStore.get(jobId)
        val title = job?.filename ?: job?.title ?: "GrabBox download"
        val contentIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, GrabBoxApp.CHANNEL_DOWNLOADS)
            .setSmallIcon(R.drawable.ic_stat_grabbox)
            .setContentTitle(title)
            .setContentText(text)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setContentIntent(contentIntent)
            .apply {
                if (percent >= 0) setProgress(100, percent.toInt(), false)
                else setProgress(100, 0, true)
            }
            .build()
    }

    private fun notifyProgress(jobId: String) {
        val job = JobStore.get(jobId) ?: return
        val text = if (job.percent >= 0)
            "${job.percent.toInt()}% · attempt ${job.attempt}/${job.attempts}"
        else "attempt ${job.attempt}/${job.attempts}"
        getSystemService(android.app.NotificationManager::class.java)
            .notify(NOTIFY_ACTIVE, progressNotification(jobId, text, job.percent))
    }

    private fun notifyDone(jobId: String) {
        val job = JobStore.get(jobId) ?: return
        val n = NotificationCompat.Builder(this, GrabBoxApp.CHANNEL_DOWNLOADS)
            .setSmallIcon(R.drawable.ic_stat_grabbox)
            .setContentTitle("Download complete")
            .setContentText(job.filename ?: job.title)
            .setAutoCancel(true)
            .build()
        getSystemService(android.app.NotificationManager::class.java)
            .notify(jobId.hashCode(), n)
    }

    private fun notifyFailed(jobId: String, error: String) {
        val n = NotificationCompat.Builder(this, GrabBoxApp.CHANNEL_DOWNLOADS)
            .setSmallIcon(R.drawable.ic_stat_grabbox)
            .setContentTitle("Download failed")
            .setContentText(error.take(120))
            .setAutoCancel(true)
            .build()
        getSystemService(android.app.NotificationManager::class.java)
            .notify(jobId.hashCode(), n)
    }
}
