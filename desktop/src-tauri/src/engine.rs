//! The engine: finds the sidecar binaries, runs yt-dlp, and knows what the
//! Python server knows — the retry ladder, the diagnostics, the HEAD sniffer.

use serde_json::{json, Map, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use tauri::Manager as _;

use crate::config::Config;

pub const STALE_AFTER_DAYS: i64 = 45;
const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) \
    AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

#[derive(Clone)]
pub struct Engine {
    resource_dir: PathBuf,
    pub last_clip: Arc<Mutex<Option<String>>>,
}

use std::sync::Arc;

impl Engine {
    pub fn new(app: &tauri::AppHandle) -> Self {
        let resource_dir = app
            .path()
            .resource_dir()
            .unwrap_or_else(|_| std::env::current_dir().unwrap_or_default());
        Engine {
            resource_dir,
            last_clip: Arc::new(Mutex::new(None)),
        }
    }

    // --------------------------------------------------------- binaries --

    /// Locate a sidecar/external binary. Order: bundled (renamed with the
    /// target triple by Tauri), plain name next to the bundle, then PATH so
    /// developers can run against their system install.
    fn find_binary(&self, name: &str) -> Option<PathBuf> {
        static CACHE: OnceLock<Mutex<std::collections::HashMap<String, Option<PathBuf>>>> =
            OnceLock::new();
        let cache = CACHE.get_or_init(|| Mutex::new(std::collections::HashMap::new()));
        if let Some(hit) = cache.lock().unwrap().get(name) {
            return hit.clone();
        }
        let found = self.find_binary_uncached(name);
        cache
            .lock()
            .unwrap()
            .insert(name.to_string(), found.clone());
        found
    }

    fn find_binary_uncached(&self, name: &str) -> Option<PathBuf> {
        let exe = exe_name(name);
        let tripled = exe_name(&format!("{}-{}", name, target_triple()));
        for dir in self.binary_dirs() {
            for cand in [&tripled, &exe] {
                let p = dir.join(cand);
                if p.is_file() {
                    return Some(p);
                }
            }
        }
        which(&exe).or_else(|| which(name))
    }

    fn binary_dirs(&self) -> Vec<PathBuf> {
        let mut dirs = vec![self.resource_dir.clone(), self.resource_dir.join("binaries")];
        if let Ok(bin) = std::env::current_exe() {
            if let Some(parent) = bin.parent() {
                dirs.push(parent.to_path_buf());
                dirs.push(parent.join("binaries"));
            }
        }
        if let Ok(cwd) = std::env::current_dir() {
            dirs.push(cwd.join("desktop").join("src-tauri").join("binaries"));
        }
        dirs
    }

    fn ytdlp(&self) -> Option<PathBuf> {
        self.find_binary("yt-dlp")
    }

    fn ffmpeg(&self) -> Option<PathBuf> {
        static PREPARED: OnceLock<Option<PathBuf>> = OnceLock::new();
        PREPARED
            .get_or_init(|| {
                let raw = self.find_binary("ffmpeg")?;
                // yt-dlp insists on a file literally called "ffmpeg". Sidecars
                // arrive renamed with the target triple, so expose a same-name
                // hardlink (fallback: copy) in the app data dir.
                if raw.file_name().map(|n| n.to_string_lossy().starts_with("ffmpeg"))
                    == Some(true)
                    && !raw.file_name().unwrap().to_string_lossy().contains('-')
                {
                    return Some(raw);
                }
                let dir = data_dir().join("bin");
                fs::create_dir_all(&dir).ok()?;
                let link = dir.join(exe_name("ffmpeg"));
                if !link.exists() {
                    if fs::hard_link(&raw, &link).is_err() {
                        fs::copy(&raw, &link).ok()?;
                    }
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let _ = fs::set_permissions(&link, fs::Permissions::from_mode(0o755));
                    }
                }
                Some(link)
            })
            .clone()
    }

    fn js_runtime(&self) -> Option<(String, PathBuf)> {
        static JS: OnceLock<Option<(String, PathBuf)>> = OnceLock::new();
        JS.get_or_init(|| {
            for name in ["deno", "node", "bun"] {
                if let Some(p) = self.find_binary(name) {
                    return Some((name.to_string(), p));
                }
                if let Some(p) = which(&exe_name(name)) {
                    return Some((name.to_string(), p));
                }
            }
            if let Some(home) = dirs::home_dir() {
                for (name, rel) in [
                    ("deno", ".deno/bin/deno"),
                    ("bun", ".bun/bin/bun"),
                ] {
                    let p = home.join(exe_name(rel));
                    if p.is_file() {
                        return Some((name.to_string(), p));
                    }
                }
            }
            None
        })
        .clone()
    }

    // ----------------------------------------------------------- health --

    pub fn health(&self, config: &Config, _app: &tauri::AppHandle) -> Result<Value, String> {
        let version = self.ytdlp_version();
        let age = version.as_deref().and_then(version_age_days);
        let ffmpeg = self.ffmpeg();
        let js = self.js_runtime();
        let mut problems = Vec::new();
        if version.is_none() {
            problems.push("ytdlp-missing");
        }
        if let Some(a) = age {
            if a > STALE_AFTER_DAYS {
                problems.push("ytdlp-stale");
            }
        }
        if ffmpeg.is_none() {
            problems.push("ffmpeg-missing");
        }
        Ok(json!({
            "app": APP_VERSION,
            "python": "sidecar",
            "platform": std::env::consts::OS,
            "ytdlp": version,
            "ytdlp_age": age,
            "stale_after": STALE_AFTER_DAYS,
            "ffmpeg": ffmpeg.map(|p| p.to_string_lossy().into_owned()),
            "js_runtime": js.as_ref().map(|j| j.0.clone()),
            "js_path": js.as_ref().map(|j| j.1.to_string_lossy().into_owned()),
            "pot_provider": Value::Null,
            "download_dir": config.download_dir(),
            "problems": problems,
            "update_hint": "GrabBox updates yt-dlp itself — press Update yt-dlp in Settings",
        }))
    }

    fn ytdlp_version(&self) -> Option<String> {
        static V: OnceLock<Option<String>> = OnceLock::new();
        V.get_or_init(|| {
            let bin = self.ytdlp()?;
            let out = Command::new(bin).arg("--version").output().ok()?;
            let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if v.is_empty() {
                None
            } else {
                Some(v)
            }
        })
        .clone()
    }

    pub fn update_ytdlp(&self) -> Result<Value, String> {
        match self.ytdlp() {
            Some(bin) => {
                let out = Command::new(bin)
                    .arg("-U")
                    .output()
                    .map_err(|e| e.to_string())?;
                Ok(json!({ "ok": out.status.success(), "hint": Value::Null }))
            }
            None => Ok(json!({ "ok": false, "hint": "yt-dlp sidecar missing — reinstall GrabBox" })),
        }
    }

    // ------------------------------------------------------ sidecar args -

    /// Args every yt-dlp call shares: JS runtime, ffmpeg location, cookies.
    fn common_args(&self, cookies: Option<&str>) -> Vec<String> {
        let mut args = Vec::new();
        if let Some((name, path)) = self.js_runtime() {
            args.push("--js-runtimes".into());
            args.push(format!("{}:{}", name, path.to_string_lossy()));
        }
        if let Some(ff) = self.ffmpeg() {
            if let Some(dir) = ff.parent() {
                args.push("--ffmpeg-location".into());
                args.push(dir.to_string_lossy().into_owned());
            }
        }
        if let Some(browser) = cookies.filter(|c| !c.is_empty()) {
            args.push("--cookies-from-browser".into());
            args.push(browser.to_string());
        }
        args
    }

    // ------------------------------------------------------------ probe --

    pub fn probe(&self, url: &str, cookies: Option<&str>) -> Result<Value, String> {
        // 1. HEAD sniff settles direct files without invoking an extractor.
        let direct = sniff_head(url);
        if direct.ok && matches!(direct.kind.as_str(), "video" | "audio" | "image" | "app" | "archive") {
            let mut res = base_probe(url);
            res["ok"] = json!(true);
            res["kind"] = json!(direct.kind);
            res["direct"] = direct.to_json();
            res["title"] = json!(direct.filename.clone().unwrap_or_else(|| url.to_string()));
            return Ok(res);
        }

        // 2. Hand it to yt-dlp.
        let bin = match self.ytdlp() {
            Some(b) => b,
            None => {
                let mut res = base_probe(url);
                res["error"] = json!("yt-dlp engine is missing");
                res["hint"] = json!("Reinstall GrabBox — the engine ships inside it");
                return Ok(res);
            }
        };
        let mut cmd = Command::new(bin);
        cmd.args([
            "-J",
            "--flat-playlist",
            "--playlist-items",
            "1-100",
            "--no-warnings",
            "--no-check-formats",
            url,
        ])
        .args(self.common_args(cookies))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
        let out = cmd.output().map_err(|e| e.to_string())?;
        let mut res = base_probe(url);

        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
            res["error"] = json!(first_line(&err));
            res["hint"] = json!(diagnose(&err));
            if direct.ok {
                res["ok"] = json!(true);
                res["kind"] = json!(direct.kind);
                res["direct"] = direct.to_json();
                res["title"] = json!(direct.filename.unwrap_or_else(|| url.to_string()));
                res["error"] = Value::Null;
            }
            return Ok(res);
        }

        let info: Value = serde_json::from_slice(&out.stdout).map_err(|e| e.to_string())?;

        if info.get("_type").and_then(Value::as_str) == Some("playlist") {
            let entries: Vec<Value> = info
                .get("entries")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .filter(|e| !e.is_null())
                .take(100)
                .map(|e| {
                    json!({
                        "title": e.get("title").cloned().unwrap_or(Value::Null),
                        "url": e.get("url").or_else(|| e.get("webpage_url")).cloned().unwrap_or(Value::Null),
                        "duration": e.get("duration").cloned().unwrap_or(Value::Null),
                    })
                })
                .collect();
            res["ok"] = json!(true);
            res["is_playlist"] = json!(true);
            res["kind"] = json!("video");
            res["title"] = info.get("title").cloned().unwrap_or(json!(url));
            res["entries"] = json!(entries);
            return Ok(res);
        }

        let mut formats = Vec::new();
        let mut has_video = false;
        let mut has_audio_only = false;
        if let Some(list) = info.get("formats").and_then(Value::as_array) {
            for f in list {
                let vcodec = f.get("vcodec").and_then(Value::as_str).unwrap_or("none");
                let acodec = f.get("acodec").and_then(Value::as_str).unwrap_or("none");
                let video = vcodec != "none";
                let audio = acodec != "none";
                if video {
                    has_video = true;
                } else if audio {
                    has_audio_only = true;
                }
                formats.push(json!({
                    "format_id": f.get("format_id").cloned().unwrap_or(Value::Null),
                    "ext": f.get("ext").cloned().unwrap_or(Value::Null),
                    "kind": if video { "video" } else if audio { "audio" } else { "other" },
                    "height": f.get("height").cloned().unwrap_or(Value::Null),
                    "note": f.get("format_note").or_else(|| f.get("format")).cloned().unwrap_or(Value::Null),
                    "size": f.get("filesize").or_else(|| f.get("filesize_approx")).cloned().unwrap_or(Value::Null),
                    "vcodec": vcodec, "acodec": acodec,
                    "abr": f.get("abr").cloned().unwrap_or(Value::Null),
                    "tbr": f.get("tbr").cloned().unwrap_or(Value::Null),
                }));
            }
        }

        // The SABR / PO-token warning we pioneered server-side.
        let mut note = Value::Null;
        if is_youtube(url) && has_video {
            let max_h = formats
                .iter()
                .filter_map(|f| f.get("height").and_then(Value::as_i64))
                .max()
                .unwrap_or(0);
            if max_h > 0 && max_h <= 360 {
                note = json!(format!(
                    "Only {}p was offered - YouTube is withholding HD formats \
                     (PO token / SABR). GrabBox retries with token-free clients \
                     and uses fresh player clients on its own.",
                    max_h
                ));
            }
        }

        res["ok"] = json!(true);
        res["note"] = note;
        res["title"] = info.get("title").cloned().unwrap_or(json!(url));
        res["kind"] = json!(if has_video {
            "video"
        } else if has_audio_only {
            "audio"
        } else {
            "other"
        });
        res["formats"] = json!(formats);
        res["thumb"] = info.get("thumbnail").cloned().unwrap_or(Value::Null);
        res["duration"] = info.get("duration").cloned().unwrap_or(Value::Null);
        res["uploader"] = info
            .get("uploader")
            .or_else(|| info.get("channel"))
            .cloned()
            .unwrap_or(Value::Null);
        Ok(res)
    }

    // ------------------------------------------------------- the ladder --

    /// Same rungs as the Python engine: each returns (label, extra CLI args).
    pub fn ladder(url: &str) -> Vec<(String, Vec<String>)> {
        if !is_youtube(url) {
            return vec![("default".to_string(), Vec::new())];
        }
        vec![
            ("default clients".into(), vec![]),
            (
                "tv client - needs no PO token".into(),
                vec!["--extractor-args".into(), "youtube:player_client=tv".into()],
            ),
            (
                "skip android clients, add web_safari".into(),
                vec![
                    "--extractor-args".into(),
                    "youtube:player_client=-android_vr,web_safari".into(),
                ],
            ),
            (
                "web_embedded + web + tv".into(),
                vec![
                    "--extractor-args".into(),
                    "youtube:player_client=web_embedded,web,tv".into(),
                ],
            ),
            (
                "IPv4 only, tv client".into(),
                vec![
                    "--extractor-args".into(),
                    "youtube:player_client=tv".into(),
                    "-4".into(),
                ],
            ),
        ]
    }

    /// The subprocess invocation for one rung of the ladder.
    pub fn spawn_download(
        &self,
        url: &str,
        kind: &str,
        quality: Option<&str>,
        playlist: bool,
        outdir: &Path,
        cookies: Option<&str>,
        filename: Option<&str>,
        rung_args: &[String],
        archive: bool,
    ) -> std::io::Result<std::process::Child> {
        let bin = self
            .ytdlp()
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "yt-dlp missing"))?;
        let mut args: Vec<String> = vec![
            "--newline".into(),
            "--no-warnings".into(),
            "--retries".into(),
            "10".into(),
            "--fragment-retries".into(),
            "10".into(),
            "--progress-template".into(),
            "GBX|%(progress.percent|0)s|%(progress.speed|0)s|%(progress.eta|0)s|%(progress.filename)s".into(),
            "-P".into(),
            outdir.to_string_lossy().into_owned(),
            "--embed-metadata".into(),
        ];
        // Only on Windows. Everywhere else the flag costs us the real filename:
        // yt-dlp then replaces every non-ASCII character and every space with
        // "_", so "My Song" becomes "My_Song" and a non-Latin title becomes a
        // row of underscores. macOS and Linux have none of the restrictions it
        // exists to work around.
        if cfg!(windows) {
            args.push("--windows-filenames".into());
        }

        let outtmpl = if let Some(name) = filename.filter(|s| !s.is_empty()) {
            let safe = sanitize_filename(name);
            if looks_like_final_name(&safe, kind) {
                safe
            } else {
                format!("{}.%(ext)s", safe)
            }
        } else if playlist {
            "%(playlist_index)02d - %(title)s.%(ext)s".to_string()
        } else {
            "%(title)s.%(ext)s".to_string()
        };
        args.push("-o".into());
        args.push(outtmpl);

        if playlist {
            args.push("--yes-playlist".into());
            if archive {
                args.push("--download-archive".into());
                args.push(outdir.join(".grabbox-archive.txt").to_string_lossy().into_owned());
            }
        } else {
            args.push("--no-playlist".into());
        }

        match kind {
            "audio" => {
                args.push("-f".into());
                args.push("bestaudio/best".into());
                args.push("-x".into());
                let fmt = quality.unwrap_or("m4a");
                // "best" / "original" = keep the original stream (no re-encode);
                // yt-dlp rejects --audio-format best, so only pass the flag
                // when the user picked an actual format.
                if fmt != "best" && fmt != "original" {
                    args.push("--audio-format".into());
                    args.push(fmt.to_string());
                }
                if fmt == "mp3" {
                    args.push("--audio-quality".into());
                    args.push("0".into());
                }
                args.push("--embed-thumbnail".into());
            }
            "video" => {
                let selector = match quality {
                    Some(h) if h.chars().all(|c| c.is_ascii_digit()) => format!(
                        "bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
                        h = h
                    ),
                    _ => "bv*+ba/b".to_string(),
                };
                args.push("-f".into());
                args.push(selector);
                args.push("--merge-output-format".into());
                args.push("mp4".into());
                args.push("--embed-thumbnail".into());
            }
            _ => {
                args.push("-f".into());
                args.push("b".into());
            }
        }

        args.push("--ignore-errors".into());
        args.extend(self.common_args(cookies));
        args.extend(rung_args.iter().cloned());
        args.push(url.to_string());

        Command::new(bin)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
    }
}

// ---------------------------------------------------------------- helpers

fn base_probe(url: &str) -> Value {
    let mut m = Map::new();
    m.insert("ok".into(), json!(false));
    m.insert("url".into(), json!(url));
    m.insert("kind".into(), json!("other"));
    m.insert("title".into(), json!(url));
    m.insert("formats".into(), json!([]));
    m.insert("entries".into(), json!([]));
    m.insert("is_playlist".into(), json!(false));
    m.insert("thumb".into(), Value::Null);
    m.insert("duration".into(), Value::Null);
    m.insert("uploader".into(), Value::Null);
    m.insert("direct".into(), Value::Null);
    m.insert("error".into(), Value::Null);
    m.insert("hint".into(), Value::Null);
    m.insert("note".into(), Value::Null);
    Value::Object(m)
}

pub fn is_youtube(url: &str) -> bool {
    let u = url.to_lowercase();
    ["youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"]
        .iter()
        .any(|h| u.contains(h))
}

pub fn diagnose(text: &str) -> String {
    let t = text.to_lowercase();
    let has = |needles: &[&str]| needles.iter().any(|n| t.contains(n));
    if has(&["sabr", "missing a url", "po token", "po_token"]) {
        return "YouTube is withholding formats (SABR / PO token). GrabBox \
            retries with token-free clients; updating yt-dlp usually restores \
            full quality."
            .into();
    }
    if has(&["http error 403", "forbidden"]) {
        return "YouTube refused the stream URL (403). Almost always an \
            outdated yt-dlp - press Update yt-dlp in Settings, then retry."
            .into();
    }
    if has(&["http error 429", "too many requests"]) {
        return "Rate limited (429). Wait 10-20 minutes and download less at once.".into();
    }
    if has(&["sign in to confirm", "not a bot"]) {
        return "YouTube wants a logged-in session. Enable browser cookies and retry.".into();
    }
    if has(&["requested format is not available"]) {
        return "That format is not offered for this item. Pick another quality.".into();
    }
    if has(&["ffmpeg"]) && has(&["not found", "not installed", "unable to locate"]) {
        return "ffmpeg is missing - audio conversion and merging need it.".into();
    }
    if has(&["private video", "members-only", "age-restricted", "video unavailable"]) {
        return "Private, age-restricted or members-only. Enable browser cookies.".into();
    }
    if has(&["unable to download webpage", "tls", "ssl", "timed out", "temporary failure"]) {
        return "Network problem reaching the site - check connection/VPN/proxy.".into();
    }
    String::new()
}

pub fn first_url(text: &str) -> Option<String> {
    for word in text.split_whitespace() {
        let w = word.trim_matches(|c| c == '"' || c == '\'' || c == '<' || c == '>');
        if w.starts_with("http://") || w.starts_with("https://") {
            return Some(w.to_string());
        }
    }
    None
}

/// A whole CLI argument that *is* a URL (`grabbox <url>` invocations).
pub fn first_url_arg(arg: &str) -> Option<String> {
    let a = arg.trim().trim_matches('"');
    if a.starts_with("http://") || a.starts_with("https://") {
        Some(a.to_string())
    } else {
        None
    }
}

fn first_line(s: &str) -> String {
    s.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("unknown error").to_string()
}

fn exe_name(name: &str) -> String {
    if cfg!(windows) && !name.ends_with(".exe") {
        format!("{}.exe", name)
    } else {
        name.to_string()
    }
}

fn target_triple() -> &'static str {
    #[cfg(all(target_os = "windows", target_arch = "x86_64"))]
    return "x86_64-pc-windows-msvc";
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    return "x86_64-apple-darwin";
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return "aarch64-apple-darwin";
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    return "x86_64-unknown-linux-gnu";
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    return "aarch64-unknown-linux-gnu";
    #[cfg(not(any(
        all(target_os = "windows", target_arch = "x86_64"),
        target_os = "macos",
        target_os = "linux"
    )))]
    return "unknown";
}

fn data_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".grabbox")
}

fn which(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|dir| dir.join(name))
            .find(|p| p.is_file())
    })
}

// ---- version age without a date crate (Howard Hinnant's civil math) ----

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

pub fn version_age_days(version: &str) -> Option<i64> {
    let mut parts = version.trim().split('.');
    let (y, m, d) = (
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
    );
    let then = days_from_civil(y, m, d);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs() as i64
        / 86400;
    Some(now - then)
}

fn sanitize_filename(name: &str) -> String {
    let bad = ['\\', '/', ':', '*', '?', '"', '<', '>', '|'];
    name.chars()
        .map(|c| if bad.contains(&c) { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// True when the sanitized name already ends in a plausible extension for the
/// chosen kind so we don't append another one ("song.mp3.mp3").
fn looks_like_final_name(name: &str, kind: &str) -> bool {
    let ext = match name.rfind('.') {
        Some(i) if i > 0 && i + 1 < name.len() => name[i + 1..].to_ascii_lowercase(),
        _ => return false,
    };
    let audio = ["mp3", "m4a", "aac", "opus", "ogg", "oga", "flac", "wav", "wma", "aiff"];
    let video = ["mp4", "mkv", "webm", "mov", "avi", "m4v", "flv", "ts", "mpg", "mpeg",
                 "3gp", "wmv"];
    let file = ["mp4","mkv","webm","mov","avi","m4v","flv","ts","mpg","mpeg","3gp","wmv",
                "mp3","m4a","aac","opus","ogg","oga","flac","wav","wma","aiff",
                "jpg","jpeg","png","gif","webp","bmp","svg","avif","tif","tiff","ico","heic",
                "exe","msi","apk","dmg","pkg","deb","rpm","appimage","jar","ipa","snap",
                "zip","rar","7z","tar","gz","tgz","bz2","xz","zst","iso","img",
                "pdf","epub","mobi","txt","md","csv","json","xml","srt","vtt"];
    match kind {
        "audio" => audio.contains(&ext.as_str()),
        "video" => video.contains(&ext.as_str()),
        "file" => file.contains(&ext.as_str()),
        _ => file.contains(&ext.as_str()),
    }
}

// ----------------------------------------------------------- HEAD sniff --

pub struct SniffResult {
    pub ok: bool,
    pub kind: String,
    pub filename: Option<String>,
    pub size: Option<u64>,
    pub content_type: Option<String>,
    pub status: u16,
}

impl SniffResult {
    fn to_json(&self) -> Value {
        json!({
            "ok": self.ok,
            "kind": self.kind,
            "filename": self.filename,
            "size": self.size,
            "content_type": self.content_type,
            "status": self.status,
        })
    }
}

fn sniff_head(url: &str) -> SniffResult {
    let mut out = SniffResult {
        ok: false,
        kind: "other".into(),
        filename: None,
        size: None,
        content_type: None,
        status: 0,
    };
    let agent = ureq::AgentBuilder::new()
        .timeout(std::time::Duration::from_secs(15))
        .user_agent(USER_AGENT)
        .build();
    let resp = agent.head(url).call();
    let (status, headers): (u16, ureq::Response) = match resp {
        Ok(r) => (r.status(), r),
        Err(ureq::Error::Status(code, r)) if code == 403 || code == 405 || code == 501 => (code, r),
        _ => return out,
    };
    out.status = status;
    let ctype = headers.header("Content-Type").map(|s| {
        s.split(';').next().unwrap_or("").trim().to_lowercase()
    });
    let cdisp = headers.header("Content-Disposition").map(|s| s.to_string());
    let size = headers.header("Content-Length").and_then(|s| s.trim().parse().ok());
    out.content_type = ctype.clone();
    out.filename = Some(filename_from(url, cdisp.as_deref(), ctype.as_deref()));
    out.size = size;
    out.kind = kind_of(out.filename.as_deref().unwrap_or(""), ctype.as_deref());
    out.ok = status < 400;
    out
}

fn ext_of(name: &str) -> String {
    let name = name.split('?').next().unwrap_or("").split('#').next().unwrap_or("");
    match name.rfind('.') {
        Some(i) if i > 0 => name[i + 1..].to_lowercase(),
        _ => String::new(),
    }
}

fn kind_of(filename: &str, content_type: Option<&str>) -> String {
    let ext = ext_of(filename);
    let by_ext = match ext.as_str() {
        "mp4" | "mkv" | "webm" | "mov" | "avi" | "m4v" | "flv" | "ts" | "mpg" | "mpeg"
        | "3gp" | "wmv" => "video",
        "mp3" | "m4a" | "aac" | "opus" | "ogg" | "oga" | "flac" | "wav" | "wma" | "aiff" => "audio",
        "jpg" | "jpeg" | "png" | "gif" | "webp" | "bmp" | "svg" | "avif" | "tif" | "tiff"
        | "ico" | "heic" => "image",
        "exe" | "msi" | "apk" | "dmg" | "pkg" | "deb" | "rpm" | "appimage" | "jar" | "ipa" => "app",
        "zip" | "rar" | "7z" | "tar" | "gz" | "tgz" | "bz2" | "xz" | "zst" | "iso" | "img" => "archive",
        "pdf" | "epub" | "mobi" | "txt" | "md" | "csv" | "json" | "xml" | "srt" | "vtt" => "document",
        _ => "",
    };
    if !by_ext.is_empty() {
        return by_ext.to_string();
    }
    let ct = content_type.unwrap_or("");
    match ct {
        "application/pdf" => "document",
        "application/zip" | "application/x-7z-compressed" | "application/x-rar-compressed"
        | "application/gzip" | "application/x-tar" => "archive",
        "application/vnd.android.package-archive" | "application/x-msdownload"
        | "application/x-apple-diskimage" | "application/java-archive" => "app",
        c if c.starts_with("video/") => "video",
        c if c.starts_with("audio/") => "audio",
        c if c.starts_with("image/") => "image",
        c if c.starts_with("text/") => "document",
        _ => "other",
    }
    .to_string()
}

/// Public wrapper for the files view.
pub fn kind_of_public(filename: &str) -> String {
    kind_of(filename, None)
}

fn filename_from(url: &str, content_disposition: Option<&str>, _content_type: Option<&str>) -> String {
    if let Some(cd) = content_disposition {
        for part in cd.split(';') {
            let part = part.trim();
            let lower = part.to_lowercase();
            if lower.starts_with("filename") {
                if let Some(eq) = part.find('=') {
                    let name = part[eq + 1..]
                        .trim()
                        .trim_matches('"')
                        .trim_start_matches("UTF-8''");
                    if !name.is_empty() {
                        return pct_unquote(name);
                    }
                }
            }
        }
    }
    let path = url.split('?').next().unwrap_or(url);
    let name = path.rsplit('/').next().unwrap_or("");
    if !name.is_empty() && !ext_of(name).is_empty() {
        return pct_unquote(name);
    }
    if !name.is_empty() {
        return pct_unquote(name);
    }
    "download".to_string()
}

fn pct_unquote(s: &str) -> String {
    // Minimal %XX decoding, enough for file names.
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(hex) = std::str::from_utf8(&bytes[i + 1..i + 3]) {
                if let Ok(v) = u8::from_str_radix(hex, 16) {
                    out.push(v);
                    i += 3;
                    continue;
                }
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
