package app.grabbox.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.MailOutline
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.grabbox.GrabBoxApp
import app.grabbox.GrabViewModel
import app.grabbox.work.Engine
import app.grabbox.work.JobStore
import coil.compose.AsyncImage

private val DoneGreen = Color(0xFF7BD88F)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    viewModel: GrabViewModel,
    engineState: GrabBoxApp.EngineState,
    onRetryEngine: () -> Unit,
) {
    val context = LocalContext.current
    val engineOk = engineState.ready
    val jobs by viewModel.jobs.collectAsState()
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(viewModel.statusText) {
        viewModel.statusText?.let {
            snackbar.showSnackbar(it)
            viewModel.clearStatus()
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            CenterAlignedTopAppBar(
                title = { Text("GrabBox", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = { viewModel.sendFeedback(context) }) {
                        Icon(Icons.Default.MailOutline, contentDescription = "Feedback")
                    }
                    IconButton(onClick = { viewModel.settingsOpen = true }) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            contentPadding = PaddingValues(bottom = 32.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            when {
                engineState.starting -> item {
                    BannerCard(
                        title = "Starting the engine…",
                        body = "Unpacking yt-dlp and its Python runtime. " +
                            "This only takes a moment, and only on first launch.",
                        containerColor = MaterialTheme.colorScheme.secondaryContainer,
                        contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
                    )
                }
                !engineOk -> item {
                    BannerCard(
                        title = "Engine failed to start",
                        body = engineState.error
                            ?: "yt-dlp could not unpack on this device. Reinstall the APK.",
                        action = { TextButton(onClick = onRetryEngine) { Text("Retry") } },
                    )
                }
            }

            // Hero: paste + grab (the 3-tap rule lives here)
            item {
                OutlinedTextField(
                    value = viewModel.urlInput,
                    onValueChange = { viewModel.urlInput = it },
                    modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("Paste a link — video, song, image, installer…") },
                    singleLine = true,
                    shape = RoundedCornerShape(18.dp),
                )
                Spacer(Modifier.height(10.dp))
                Button(
                    onClick = { viewModel.analyze() },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = engineOk && !viewModel.probing,
                ) { Text(if (viewModel.probing) "Reading link…" else "Grab") }
            }

            viewModel.probeError?.let { err ->
                item {
                    BannerCard("Could not read that link", err, viewModel.probeHint)
                    Row { TextButton(onClick = { viewModel.dismissProbe() }) { Text("Dismiss") } }
                }
            }

            viewModel.probe?.let { p ->
                item { ProbeCard(viewModel, p) }
            }

            // Queue
            item {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Queue", style = MaterialTheme.typography.titleSmall)
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { viewModel.clearFinished() }) { Text("Clear finished") }
                }
            }
            if (jobs.isEmpty()) {
                item(contentType = "empty") {
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(
                            "Nothing yet — paste a link, or use Share → GrabBox from any app.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(vertical = 8.dp),
                        )
                        Text(
                            "Tip: in YouTube, Instagram, TikTok, etc. tap Share then Copy link and paste above.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            } else {
                items(jobs, key = { it.id }, contentType = { "job" }) { job ->
                    JobCard(
                        job = job,
                        onCancel = { viewModel.cancelJob(context, job.id) },
                        onOpen = { viewModel.openFile(context, job) },
                    )
                }
            }

            item {
                Text(
                    "Runs 100% on your device · engine: yt-dlp",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }

    if (viewModel.settingsOpen) {
        SettingsDialog(viewModel)
    }
}

@Composable
private fun BannerCard(
    title: String,
    body: String,
    hint: String? = null,
    containerColor: Color = MaterialTheme.colorScheme.errorContainer,
    contentColor: Color = MaterialTheme.colorScheme.onErrorContainer,
    action: (@Composable () -> Unit)? = null,
) {
    Surface(
        color = containerColor,
        contentColor = contentColor,
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, fontWeight = FontWeight.SemiBold, color = contentColor)
            Text(body, style = MaterialTheme.typography.bodySmall, color = contentColor)
            hint?.let {
                Text("💡 $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
            }
            action?.invoke()
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ProbeCard(vm: GrabViewModel, p: Engine.ProbeInfo) {
    val context = LocalContext.current
    ElevatedCard(shape = RoundedCornerShape(24.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            // Thumbnail + meta
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                AsyncImage(
                    model = p.thumb,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .width(150.dp)
                        .aspectRatio(16f / 9f)
                        .clip(RoundedCornerShape(12.dp)),
                )
                Column {
                    Text(
                        p.title,
                        style = MaterialTheme.typography.titleSmall,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(4.dp))
                    val meta = buildString {
                        p.uploader?.let { append(it) }
                        p.durationSec?.let {
                            if (isNotEmpty()) append("  ·  ")
                            append(fmtDuration(it))
                        }
                        p.directSize?.let {
                            if (isNotEmpty()) append("  ·  ")
                            append(humanSize(it))
                        }
                        if (p.isPlaylist) {
                            if (isNotEmpty()) append("  ·  ")
                            append("playlist · ${p.playlistCount}+ items")
                        }
                    }
                    if (meta.isNotEmpty()) {
                        Text(meta, style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }

            // Kind + quality
            val kinds = availableKinds(p)
            if (kinds.size > 1) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    kinds.forEach { (k, label) ->
                        FilterChip(
                            selected = vm.chosenKind == k,
                            onClick = {
                                vm.chosenKind = k
                                vm.chosenQuality = if (k == "audio") "m4a" else if (k == "video") "best" else "original"
                            },
                            label = { Text(label) },
                        )
                    }
                }
            }

            val qualities = qualitiesFor(vm.chosenKind, p)
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                qualities.forEach { (q, label) ->
                    FilterChip(
                        selected = vm.chosenQuality == q,
                        onClick = { vm.chosenQuality = q },
                        label = { Text(label) },
                    )
                }
            }

            p.note?.let {
                Surface(
                    color = MaterialTheme.colorScheme.tertiaryContainer,
                    shape = RoundedCornerShape(12.dp),
                ) {
                    Text(
                        "⚠ $it",
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(10.dp),
                    )
                }
            }

            OutlinedTextField(
                value = vm.customName,
                onValueChange = { vm.customName = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Rename (optional)") },
                singleLine = true,
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { vm.dismissProbe() }) { Text("Cancel") }
                Spacer(Modifier.weight(1f))
                Button(onClick = { vm.startDownload(context) }) {
                    Text("Download")
                }
            }
        }
    }
}

@Composable
private fun JobCard(job: JobStore.Job, onCancel: () -> Unit, onOpen: () -> Unit) {
    // Animate progress + status color so updating doesn't stutter.
    val animatedProgress by androidx.compose.animation.core.animateFloatAsState(
        targetValue = (job.percent / 100f).coerceIn(0f, 1f),
        animationSpec = tween(durationMillis = 400),
        label = "jobProgress",
    )
    val statusColor by animateColorAsState(
        targetValue = when (job.status) {
            "done" -> DoneGreen
            "error", "canceled" -> MaterialTheme.colorScheme.error
            else -> MaterialTheme.colorScheme.primary
        },
        animationSpec = tween(200),
        label = "statusColor",
    )
    Card(
        shape = RoundedCornerShape(18.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(
                        job.filename ?: job.title.ifBlank { job.url },
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    val sub = buildString {
                        append(job.status)
                        if (job.percent >= 0) append(" · ${job.percent.toInt()}%")
                        if (job.etaSec > 0 && job.status == "running") append(" · ${job.etaSec.toLong()}s left")
                        if (job.attempts > 1 && job.status == "running") append(" · attempt ${job.attempt}/${job.attempts}")
                    }
                    Text(sub, style = MaterialTheme.typography.bodySmall, color = statusColor)
                }
                if (job.active) {
                    IconButton(onClick = onCancel) {
                        Icon(Icons.Default.Close, contentDescription = "Stop")
                    }
                } else if (job.status == "done" && job.filePath != null) {
                    IconButton(onClick = onOpen) {
                        Icon(Icons.Default.PlayArrow, contentDescription = "Open")
                    }
                }
            }
            if (job.status == "running" || job.status == "done") {
                LinearProgressIndicator(
                    progress = { animatedProgress },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            job.error?.let {
                Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
            }
            job.hint?.let {
                Text("💡 $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
            }
        }
    }
}

@Composable
private fun SettingsDialog(vm: GrabViewModel) {
    val context = LocalContext.current
    AlertDialog(
        onDismissRequest = { vm.settingsOpen = false },
        confirmButton = {
            TextButton(onClick = { vm.settingsOpen = false }) { Text("Done") }
        },
        title = { Text("Settings") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Download folder", style = MaterialTheme.typography.titleSmall)
                vm.dirOptions.forEach { (path, label) ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(
                            selected = vm.downloadDir == path,
                            onClick = { vm.setDir(path) },
                        )
                        Text(label, style = MaterialTheme.typography.bodyMedium)
                    }
                }
                FilledTonalButton(
                    onClick = { vm.updateEngine(context) },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Icon(Icons.Default.Refresh, contentDescription = null)
                    Spacer(Modifier.width(8.dp))
                    Text("Update yt-dlp engine")
                }
                Text(
                    "Refresh the engine when YouTube changes something. " +
                        "This usually fixes 403 errors instantly.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
    )
}

// ------------------------------------------------------------- helpers --

private fun availableKinds(p: Engine.ProbeInfo): List<Pair<String, String>> = buildList {
    if (p.hasVideo || p.kind == "video") add("video" to "Video")
    if (p.hasVideo || p.hasAudio || p.kind == "audio") add("audio" to "Audio")
    if (isEmpty()) add("file" to "File")
}

private fun qualitiesFor(kind: String, p: Engine.ProbeInfo): List<Pair<String, String>> = when (kind) {
    "video" -> {
        val maxH = p.heights.maxOrNull()
        Engine.VIDEO_QUALITIES.filter { (q, _) ->
            q == "best" || maxH == null || q.toInt() <= maxH * 1.02
        }
    }
    "audio" -> Engine.AUDIO_QUALITIES
    else -> listOf("original" to "Original file")
}

private fun fmtDuration(sec: Double): String {
    val s = sec.toInt()
    val h = s / 3600
    val m = (s % 3600) / 60
    val rest = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, rest) else "%d:%02d".format(m, rest)
}

private fun humanSize(n: Long): String {
    val units = listOf("B", "KB", "MB", "GB", "TB")
    var size = n.toDouble()
    var i = 0
    while (size >= 1024 && i < units.size - 1) {
        size /= 1024
        i++
    }
    return "%.1f %s".format(size, units[i])
}
