package app.grabbox

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import android.util.Log
import app.grabbox.work.JobStore
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Boots the bundled engine: Python runtime + yt-dlp + ffmpeg, all inside the APK.
 *
 * First launch unzips ~40 MB of Python into the app's files dir, so the work
 * happens on a background thread and the UI shows "starting" meanwhile. The
 * whole cause chain is kept, not just the top message — the engine's own
 * "failed to initialize" tells nobody anything on its own.
 */
class GrabBoxApp : Application() {

    data class EngineState(
        val ready: Boolean = false,
        val starting: Boolean = true,
        val error: String? = null,
    )

    private val _engineState = MutableStateFlow(EngineState())

    /** Observed by the UI: drives the banner and whether "Grab" is enabled. */
    val engineState: StateFlow<EngineState> = _engineState.asStateFlow()

    @Volatile
    private var booting = false

    override fun onCreate() {
        super.onCreate()
        instance = this
        JobStore.init(this)   // restore the persisted queue before the UI reads it
        startEngine()
        createNotificationChannel()
    }

    /** Safe to call from the UI: a second tap while a boot is in flight is a no-op. */
    fun startEngine() {
        synchronized(this) {
            if (booting || _engineState.value.ready) return
            booting = true
        }
        _engineState.value = EngineState(starting = true)
        Thread({ bootEngine() }, "grabbox-engine-boot").start()
    }

    private fun bootEngine() {
        try {
            // Each init() is idempotent and short-circuits once it succeeded,
            // so a retry after a partial failure just picks up where it broke.
            YoutubeDL.getInstance().init(this)
            FFmpeg.getInstance().init(this)
            _engineState.value = EngineState(ready = true, starting = false)
        } catch (e: Exception) {
            val detail = describe(e)
            Log.e("GrabBox", "engine init failed", e)
            _engineState.value = EngineState(starting = false, error = detail)
        } finally {
            synchronized(this) { booting = false }
        }
    }

    /**
     * Flattens an exception into something worth reading in a banner. The
     * engine wraps every failure in a literal "failed to initialize", so the
     * useful text is always further down the cause chain.
     */
    private fun describe(e: Exception): String {
        val lines = mutableListOf<String>()
        val seen = mutableSetOf<String>()
        var cause: Throwable? = e
        var depth = 0
        while (cause != null && depth < 5) {
            val message = cause.message?.takeIf { it.isNotBlank() } ?: cause.toString()
            if (seen.add(message)) lines.add(message)
            cause = if (cause.cause === cause) null else cause.cause
            depth++
        }
        val text = lines.joinToString("\n")
        return if (text.contains("failed to initialize", ignoreCase = true)) {
            "$text\n\nThe bundled engine was not extracted. This APK was built " +
                "without `packaging { jniLibs { useLegacyPackaging = true } }`, " +
                "so nativeLibraryDir is empty on install."
        } else {
            text
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_DOWNLOADS,
                getString(R.string.channel_downloads),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.channel_downloads_desc)
            }
            getSystemService(NotificationManager::class.java)
                ?.createNotificationChannel(channel)
        }
    }

    companion object {
        const val CHANNEL_DOWNLOADS = "grabbox-downloads"
        lateinit var instance: GrabBoxApp
            private set
    }
}
