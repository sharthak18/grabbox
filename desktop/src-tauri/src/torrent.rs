//! Torrents — a first-class path in the same download list, powered by
//! librqbit (Apache-2.0, github.com/ikatson/rqbit). MDM does this as well,
//! but its engine is GPL, so GrabBox integrates the permissively licensed
//! librqbit instead.
//!
//! Design notes:
//! * The session is created lazily on first use — a user who never touches a
//!   torrent pays nothing (no bound ports, no DHT). On startup we restore a
//!   session only when a previous torrent state exists.
//! * Torrents live in `~/.grabbox/torrents/` (session persistence + fastresume),
//!   downloads land in the configured download folder like everything else.
//! * Jobs are merged into the regular `jobs` list with ids `bt-<n>`, so the
//!   UI, polling and notifications all work unchanged. Pause = pause the
//!   torrent; "clear finished" removes finished/canceled torrents from the
//!   session (files are always kept).

use librqbit::api::TorrentIdOrHash;
use librqbit::{
    AddTorrent, AddTorrentOptions, DhtSessionConfig, ListenerOptions, ManagedTorrent, Session,
    SessionOptions, SessionPersistenceConfig, TorrentStatsState,
};
// `TorrentId` and the `ManagedTorrentHandle` alias live on private `session`
// paths in librqbit; both are `usize` / `Arc<ManagedTorrent>` under the hood.
type TorrentId = usize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

const ID_PREFIX: &str = "bt-";
const MAX_TORRENT_FILE: usize = 64 * 1024 * 1024; // 64 MiB of bencode is plenty
const MAX_MAGNET: usize = 8192;

pub fn parse_torrent_id(s: &str) -> Option<usize> {
    s.strip_prefix(ID_PREFIX)?.parse().ok()
}

fn state_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".grabbox")
        .join("torrents")
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub struct TorrentManager {
    session: tokio::sync::OnceCell<Arc<Session>>,
    /// Ids the user stopped: shown as `canceled`, retried with resume.
    cancelled: Mutex<HashSet<TorrentId>>,
    /// First-seen timestamps for job rows; restored torrents get "now".
    seen: Mutex<HashMap<TorrentId, f64>>,
}

impl TorrentManager {
    pub fn new() -> Self {
        TorrentManager {
            session: tokio::sync::OnceCell::new(),
            cancelled: Mutex::new(HashSet::new()),
            seen: Mutex::new(HashMap::new()),
        }
    }

    /// Cheap probe so app startup can restore the session only when the user
    /// has torrents to resume.
    pub fn has_persisted_state() -> bool {
        let dir = state_dir().join("session");
        std::fs::read_dir(&dir)
            .map(|mut it| it.next().is_some())
            .unwrap_or(false)
    }

    async fn session(&self) -> anyhow::Result<Arc<Session>> {
        self.session
            .get_or_try_init(|| async {
                let dir = state_dir().join("session");
                std::fs::create_dir_all(&dir)?;
                let output = dirs::home_dir()
                    .map(|h| h.join("Downloads").join("GrabBox"))
                    .unwrap_or_else(|| PathBuf::from("."));
                let opts = SessionOptions {
                    fastresume: true,
                    persistence: Some(SessionPersistenceConfig::Json {
                        folder: Some(dir),
                    }),
                    // Random TCP port for incoming peers; we do not force UPnP.
                    listen: Some(ListenerOptions::default()),
                    // DHT, but no config-directory pollution (kept in memory).
                    dht: Some(DhtSessionConfig {
                        bootstrap_addrs: None,
                        port: None,
                        persistence: None,
                    }),
                    ..Default::default()
                };
                Session::new_with_opts(output, opts).await
            })
            .await
            .cloned()
    }

    fn handle(session: &Session, id: TorrentId) -> Option<Arc<ManagedTorrent>> {
        session.with_torrents(|it| {
            let mut hit = None;
            for (tid, handle) in it {
                if tid == id {
                    hit = Some(handle.clone());
                    break;
                }
            }
            hit
        })
    }

    /// Create the session if the user has persisted torrent state (fastresume
    /// makes the engine pick downloads up where they left off).
    pub async fn ensure_session(&self) {
        let _ = self.session().await;
    }

    fn mark_seen(&self, id: TorrentId) {
        self.seen.lock().unwrap().entry(id).or_insert_with(now_secs);
    }

    fn seen_at(&self, id: TorrentId) -> f64 {
        *self.seen.lock().unwrap().entry(id).or_insert_with(now_secs)
    }

    async fn add_inner(&self, add: AddTorrent<'_>, dir: &str) -> Result<Value, String> {
        if dir.trim().is_empty() {
            return Err("set a download folder first".to_string());
        }
        std::fs::create_dir_all(dir).map_err(|e| format!("Can't use that folder — {e}"))?;
        let session = self
            .session()
            .await
            .map_err(|e| format!("torrent engine failed to start: {e}"))?;
        let opts = AddTorrentOptions {
            overwrite: true,
            output_folder: Some(dir.to_string()),
            ..Default::default()
        };
        let resp = session
            .add_torrent(add, Some(opts))
            .await
            .map_err(|e| format!("torrent rejected: {e}"))?;
        let handle = resp
            .into_handle()
            .ok_or_else(|| "torrent was only listed".to_string())?;
        let id = handle.shared.id;
        self.mark_seen(id);
        Ok(json!({
            "id": format!("{ID_PREFIX}{id}"),
            "name": handle.name(),
        }))
    }

    fn decode_b64_torrent(b64: &str) -> Result<Vec<u8>, String> {
        use base64::{engine::general_purpose::STANDARD, Engine as _};
        let bytes = STANDARD
            .decode(b64.as_bytes())
            .map_err(|e| format!("could not read that .torrent ({e})"))?;
        if bytes.is_empty() || bytes.len() > MAX_TORRENT_FILE {
            return Err("that doesn't look like a .torrent file".to_string());
        }
        Ok(bytes)
    }

    /// File listing for a torrent, added paused so the user can pick what to
    /// download before anything is written. Magnet links resolve through the
    /// same DHT/tracker path as a real add; the torrent stays paused until
    /// `select` is called.
    async fn inspect_inner(&self, add: AddTorrent<'_>, dir: &str) -> Result<Value, String> {
        if dir.trim().is_empty() {
            return Err("set a download folder first".to_string());
        }
        std::fs::create_dir_all(dir).map_err(|e| format!("Can't use that folder — {e}"))?;
        let session = self
            .session()
            .await
            .map_err(|e| format!("torrent engine failed to start: {e}"))?;
        let opts = AddTorrentOptions {
            overwrite: true,
            paused: true,
            output_folder: Some(dir.to_string()),
            ..Default::default()
        };
        let resp = session
            .add_torrent(add, Some(opts))
            .await
            .map_err(|e| format!("torrent rejected: {e}"))?;
        let handle = resp
            .into_handle()
            .ok_or_else(|| "torrent was only listed".to_string())?;
        let id = handle.shared.id;
        let (name, files, total_size) = handle
            .with_metadata(|m| {
                let name = m
                    .info
                    .name()
                    .map(|c| c.into_owned())
                    .unwrap_or_else(|| format!("torrent {id}"));
                let mut total = 0u64;
                let files: Vec<Value> = m
                    .info
                    .iter_file_details()
                    .enumerate()
                    .map(|(i, fd)| {
                        total += fd.len;
                        json!({
                            "index": i,
                            "path": fd.filename.to_pathbuf().to_string_lossy(),
                            "length": fd.len,
                        })
                    })
                    .collect();
                (name, files, total)
            })
            .map_err(|e| format!("couldn't read the torrent's file list: {e}"))?;
        self.mark_seen(id);
        Ok(json!({
            "id": format!("{ID_PREFIX}{id}"),
            "name": name,
            "files": files,
            "total_size": total_size,
        }))
    }

    pub async fn inspect_magnet(&self, magnet: &str, dir: &str) -> Result<Value, String> {
        let magnet = magnet.trim();
        if !magnet.starts_with("magnet:") {
            return Err("that is not a magnet link".to_string());
        }
        if magnet.len() > MAX_MAGNET {
            return Err("magnet link too long".to_string());
        }
        self.inspect_inner(AddTorrent::from_url(magnet.to_string()), dir)
            .await
    }

    pub async fn inspect_torrent_file(&self, b64: &str, dir: &str) -> Result<Value, String> {
        let bytes = Self::decode_b64_torrent(b64)?;
        self.inspect_inner(AddTorrent::from_bytes(bytes), dir).await
    }

    /// Start a torrent that was added through `inspect_inner`, limited to
    /// `files` (an empty list means everything).
    pub async fn select(&self, id: TorrentId, files: Vec<usize>) -> Result<(), String> {
        let session = self.session().await.map_err(|e| e.to_string())?;
        let handle = Self::handle(&session, id).ok_or_else(|| "unknown torrent".to_string())?;
        if !files.is_empty() {
            let set: HashSet<usize> = files.into_iter().collect();
            // A just-added torrent may still be in its storage initialisation
            // step; the engine refuses to update files then — wait briefly.
            let mut attempt = 0;
            loop {
                match session.update_only_files(&handle, &set).await {
                    Ok(()) => break,
                    Err(e) => {
                        attempt += 1;
                        if attempt >= 5 || !e.to_string().contains("initializing") {
                            return Err(format!("couldn't set the file selection: {e}"));
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
                    }
                }
            }
        }
        self.cancelled.lock().unwrap().remove(&id);
        session.unpause(&handle).await.map_err(|e| e.to_string())
    }

    /// Forget a paused inspection torrent the user walked away from.
    pub async fn discard(&self, id: TorrentId) -> Result<(), String> {
        if let Some(session) = self.session.get() {
            let _ = session.delete(TorrentIdOrHash::Id(id), /* delete_files */ false).await;
        }
        self.cancelled.lock().unwrap().remove(&id);
        self.seen.lock().unwrap().remove(&id);
        Ok(())
    }

    pub async fn add_magnet(&self, magnet: &str, dir: &str) -> Result<Value, String> {
        let magnet = magnet.trim();
        if !magnet.starts_with("magnet:") {
            return Err("that is not a magnet link".to_string());
        }
        if magnet.len() > MAX_MAGNET {
            return Err("magnet link too long".to_string());
        }
        self.add_inner(AddTorrent::from_url(magnet.to_string()), dir).await
    }

    pub async fn add_torrent_file(&self, b64: &str, dir: &str) -> Result<Value, String> {
        let bytes = Self::decode_b64_torrent(b64)?;
        self.add_inner(AddTorrent::from_bytes(bytes), dir).await
    }

    pub async fn pause(&self, id: TorrentId) -> Result<(), String> {
        self.cancelled.lock().unwrap().insert(id);
        if let Some(session) = self.session.get() {
            if let Some(handle) = Self::handle(session, id) {
                session.pause(&handle).await.map_err(|e| e.to_string())?;
            }
        }
        Ok(())
    }

    pub async fn unpause(&self, id: TorrentId) -> Result<(), String> {
        self.cancelled.lock().unwrap().remove(&id);
        let session = self.session().await.map_err(|e| e.to_string())?;
        let handle = Self::handle(&session, id).ok_or_else(|| "unknown torrent".to_string())?;
        session.unpause(&handle).await.map_err(|e| e.to_string())
    }

    /// The engine has no "canceled" concept; rows are kept as paused and
    /// cleared by "clear finished", files are never deleted.
    pub async fn clear_finished(&self) {
        let Some(session) = self.session.get() else {
            return;
        };
        let doomed: Vec<TorrentId> = {
            let cancelled = self.cancelled.lock().unwrap();
            session.with_torrents(|it| {
                it.filter_map(|(id, h)| {
                    let stats = h.stats();
                    if stats.finished || cancelled.contains(&id) {
                        Some(id)
                    } else {
                        None
                    }
                })
                .collect()
            })
        };
        for id in doomed {
            let _ = session
                .delete(TorrentIdOrHash::Id(id), /* delete_files */ false)
                .await;
            self.cancelled.lock().unwrap().remove(&id);
            self.seen.lock().unwrap().remove(&id);
        }
    }

    /// Live job-shaped snapshots for the UI (same fields the yt-dlp jobs use).
    pub fn snapshot(&self) -> Vec<Value> {
        let Some(session) = self.session.get() else {
            return Vec::new();
        };
        let cancelled = self.cancelled.lock().unwrap().clone();
        session.with_torrents(|it| {
            it.map(|(id, handle)| {
                let stats = handle.stats();
                let name = handle
                    .name()
                    .unwrap_or_else(|| format!("torrent {id}"));
                let total = stats.total_bytes;
                let percent = if total > 0 {
                    (stats.progress_bytes as f64 / total as f64 * 100.0).min(100.0)
                } else {
                    0.0
                };
                let speed = stats
                    .live
                    .as_ref()
                    .map(|l| l.download_speed.as_bytes())
                    .unwrap_or(0);
                let eta = if speed > 0 && total > stats.progress_bytes {
                    Some((total - stats.progress_bytes) / speed)
                } else {
                    None
                };
                let status = if cancelled.contains(&id) {
                    "canceled"
                } else if stats.finished {
                    "done"
                } else {
                    match stats.state {
                        TorrentStatsState::Initializing { .. } => "queued",
                        TorrentStatsState::Live => "running",
                        TorrentStatsState::Paused => "paused",
                        TorrentStatsState::Error => "error",
                    }
                };
                let mut job = json!({
                    "id": format!("{ID_PREFIX}{id}"),
                    "url": name,
                    "title": name,
                    "kind": "torrent",
                    "status": status,
                    "percent": percent,
                    "speed": speed,
                    "filename": name,
                    "path": handle.output_folder().to_string_lossy(),
                    "error": stats.error,
                    "segments": 0,
                    "size": total,
                    "downloaded": stats.progress_bytes,
                    "attempt": 1,
                    "attempts_total": 1,
                    "playlist": false,
                    "items_done": null,
                    "items_total": null,
                    "created": self.seen_at(id),
                    "finished": null,
                });
                if let Some(secs) = eta {
                    job["eta"] = json!(secs);
                }
                job
            })
            .collect()
        })
    }
}

impl Default for TorrentManager {
    fn default() -> Self {
        Self::new()
    }
}
