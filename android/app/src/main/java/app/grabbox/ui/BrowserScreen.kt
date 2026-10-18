package app.grabbox.ui

import android.annotation.SuppressLint
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.ArrowForward
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * The VidMate move: browse inside the app, and when a page holds something
 * downloadable — a <video>, a media file, a download link — the Grab button
 * lights up and hands the URL to the normal probe-and-pick flow. Nothing
 * here downloads by itself; it only *finds*, the engine does the rest.
 */
private val MEDIA_EXTS = listOf(
    ".mp4", ".webm", ".mkv", ".mov", ".mp3", ".m4a", ".opus", ".ogg",
    ".flac", ".wav", ".m3u8", ".mpd", ".zip", ".apk",
)

private fun isMediaUrl(url: String): Boolean {
    val path = url.lowercase().substringBefore("?").substringBefore("#")
    return MEDIA_EXTS.any { path.endsWith(it) }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun BrowserScreen(onGrab: (String) -> Unit) {
    var urlText by remember { mutableStateOf("https://m.youtube.com") }
    var currentUrl by remember { mutableStateOf("https://m.youtube.com") }
    var detected by remember { mutableStateOf<String?>(null) }
    var progress by remember { mutableStateOf(0) }
    var webView by remember { mutableStateOf<WebView?>(null) }

    fun navigate(raw: String) {
        var u = raw.trim()
        if (u.isEmpty()) return
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            u = if (u.contains(".") && !u.contains(" ")) {
                "https://$u"
            } else {
                "https://duckduckgo.com/?q=" + android.net.Uri.encode(u)
            }
        }
        detected = null
        webView?.loadUrl(u)
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 6.dp),
        ) {
            IconButton(onClick = { if (webView?.canGoBack() == true) webView?.goBack() }) {
                Icon(Icons.Default.ArrowBack, contentDescription = "Back")
            }
            IconButton(onClick = { if (webView?.canGoForward() == true) webView?.goForward() }) {
                Icon(Icons.Default.ArrowForward, contentDescription = "Forward")
            }
            OutlinedTextField(
                value = urlText,
                onValueChange = { urlText = it },
                modifier = Modifier.weight(1f),
                singleLine = true,
                shape = RoundedCornerShape(16.dp),
                placeholder = { Text("Search or paste an address") },
            )
            IconButton(onClick = { navigate(urlText) }) {
                Icon(Icons.Default.PlayArrow, contentDescription = "Go")
            }
            IconButton(onClick = { webView?.reload() }) {
                Icon(Icons.Default.Refresh, contentDescription = "Reload")
            }
        }

        if (progress in 1..99) {
            LinearProgressIndicator(
                progress = { progress / 100f },
                modifier = Modifier.fillMaxWidth(),
            )
        }

        Box(Modifier.weight(1f).fillMaxWidth()) {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { ctx ->
                    WebView(ctx).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.mediaPlaybackRequiresUserGesture = false
                        webViewClient = object : WebViewClient() {
                            override fun shouldInterceptRequest(
                                view: WebView?,
                                request: WebResourceRequest?,
                            ): WebResourceResponse? {
                                val u = request?.url?.toString().orEmpty()
                                if (u.isNotEmpty() && isMediaUrl(u)) detected = u
                                return null
                            }

                            override fun onPageStarted(
                                view: WebView?,
                                url: String?,
                                favicon: android.graphics.Bitmap?,
                            ) {
                                if (url != null) {
                                    currentUrl = url
                                    urlText = url
                                }
                                detected = null
                            }

                            override fun onPageFinished(view: WebView?, url: String?) {
                                if (url != null) {
                                    currentUrl = url
                                    urlText = url
                                }
                                // Ask the page itself what is playing.
                                view?.evaluateJavascript(
                                    "(function(){var v=document.querySelector('video');" +
                                        "return v?(v.currentSrc||v.src||''):'';})()"
                                ) { result ->
                                    val u = result.trim().trim('"')
                                    if (u.startsWith("http")) detected = u
                                }
                            }
                        }
                        webChromeClient = object : WebChromeClient() {
                            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                                progress = newProgress
                            }
                        }
                        // A tapped download link *is* the grab gesture
                        // (this is how VidMate/Snaptube feel instant).
                        setDownloadListener { url, _, _, _, _ -> onGrab(url) }
                        loadUrl(currentUrl)
                        webView = this
                    }
                },
                update = { webView = it },
            )

            detected?.let { media ->
                Surface(
                    color = MaterialTheme.colorScheme.secondaryContainer,
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier
                        .align(Alignment.TopCenter)
                        .padding(10.dp),
                ) {
                    Text(
                        "🎬 Found: ${media.substringAfterLast('/').ifBlank { media }}",
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                    )
                }
            }

            ExtendedFloatingActionButton(
                onClick = { onGrab(detected ?: currentUrl) },
                icon = { Icon(Icons.Default.PlayArrow, contentDescription = null) },
                text = { Text(if (detected != null) "Grab this media 🐎" else "Grab this page") },
                modifier = Modifier
                    .align(Alignment.BottomEnd)
                    .padding(16.dp),
            )
        }
    }
}
