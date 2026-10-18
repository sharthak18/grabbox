//! Job table: one job per URL, background worker threads, live progress.
//! Mirrors the Python `Manager` so the UI cannot tell the difference.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::Config;
use crate::engine::{diagnose, Engine};

static COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct JobSnapshot {
    pub id: String,
    pub url: String,
    pub title: String,
    pub kind: String,
    pub status: String, // queued | running | done | error | canceled
    pub percent: f64,
    pub speed: Option<f64>,
    pub eta: Option<f64>,
    pub filename: Option<String>,
    pub path: Option<String>,
    pub error: Option<String>,
    pub hint: Option<String>,
    pub attempt: u32,
    pub attempts_total: u32,
    pub log: Vec<String>,
    pub created: f64,
    pub finished: Option<f64>,
    pub playlist: bool,
    pub items_done: u32,
    pub items_total: u32,
    pub thumb: Option<String>,
    pub size: Option<u64>,
    pub direct: Value,
    pub downloaded: u64,
    pub segments: u32,
}

/// One row of ~/.grabbox/jobs.json — the queue, persisted like the Python
/// core's, so a restart restores jobs instead of losing them.
#[derive(Serialize, Deserialize)]
struct PersistedJob {
    id: String,
    url: String,
    kind: String,
    quality: Option<String>,
    playlist: bool,
    #[serde(default)]
    directory: String,
    cookies: Option<String>,
    filename_req: Option<String>,
    status: String,
    percent: f64,
    filename: Option<String>,
    path: Option<String>,
    error: Option<String>,
    hint: Option<String>,
    created: f64,
    finished: Option<f64>,
    items_done: u32,
    items_total: u32,
}

fn jobs_file() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".grabbox").join("jobs.json"))
}

#[derive(Clone, Default)]
struct JobState {
    id: String,
    url: String,
    kind: String,
    status: String,
    percent: f64,
    speed: Option<f64>,
    eta: Option<f64>,
    filename: Option<String>,
    path: Option<String>,
    error: Option<String>,
    hint: Option<String>,
    attempt: u32,
    attempts_total: u32,
    log: Vec<String>,
    created: f64,
    finished: Option<f64>,
    playlist: bool,
    items_done: u32,
    items_total: u32,
    cancel: bool,
    // The original request, kept so resume/retry (and a restart) can rerun it.
    quality: Option<String>,
    directory: String,
    cookies: Option<String>,
    filename_req: Option<String>,
    pause_requested: bool,
    segments: u32,
}

impl JobState {
    fn snapshot(&self) -> JobSnapshot {
        JobSnapshot {
            id: self.id.clone(),
            url: self.url.clone(),
            title: self.url.clone(),
            kind: self.kind.clone(),
            status: self.status.clone(),
            percent: self.percent,
            speed: self.speed,
            eta: self.eta,
            filename: self.filename.clone(),
            path: self.path.clone(),
            error: self.error.clone(),
            hint: self.hint.clone(),
            attempt: self.attempt,
            attempts_total: self.attempts_total,
            log: self.log.clone(),
            created: self.created,
            finished: self.finished,
            playlist: self.playlist,
            items_done: self.items_done,
            items_total: self.items_total,
            thumb: None,
            size: None,
            direct: Value::Null,
            downloaded: 0,
            segments: self.segments,
        }
    }

    fn note(&mut self, msg: &str) {
        self.log.push(format!("{}  {}", now_hms(), msg));
        if self.log.len() > 40 {
            let extra = self.log.len() - 40;
            self.log.drain(0..extra);
        }
    }
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn now_hms() -> String {
    let secs = (now() as u64) % 86400;
    format!(
        "{:02}:{:02}:{:02}",
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// A counting semaphore so `concurrency` jobs run at once.
struct Sem {
    count: Mutex<usize>,
    cvar: Condvar,
}
impl Sem {
    fn new(n: usize) -> Self {
        Sem {
            count: Mutex::new(n),
            cvar: Condvar::new(),
        }
    }
    fn acquire(&self) {
        let mut c = self.count.lock().unwrap();
        while *c == 0 {
            c = self.cvar.wait(c).unwrap();
        }
        *c -= 1;
    }
    fn release(&self) {
        let mut c = self.count.lock().unwrap();
        *c += 1;
        self.cvar.notify_one();
    }
}

#[derive(Clone)]
pub struct JobHandle {
    state: Arc<Mutex<JobState>>,
}

impl JobHandle {
    pub fn snapshot(&self) -> JobSnapshot {
        self.state.lock().unwrap().snapshot()
    }
}

#[derive(Clone)]
pub struct Manager {
    jobs: Arc<Mutex<HashMap<String, Arc<Mutex<JobState>>>>>,
    children: Arc<Mutex<HashMap<String, Arc<Mutex<Option<Child>>>>>>,
    order: Arc<Mutex<Vec<String>>>,
    sem: Arc<Sem>,
    app: Option<tauri::AppHandle>,
}

impl Manager {
    pub fn new(concurrency: usize, app: Option<tauri::AppHandle>) -> Self {
        let m = Manager {
            jobs: Arc::new(Mutex::new(HashMap::new())),
            children: Arc::new(Mutex::new(HashMap::new())),
            order: Arc::new(Mutex::new(Vec::new())),
            sem: Arc::new(Sem::new(concurrency)),
            app,
        };
        m.restore();
        m
    }

    /// Snapshot the queue to ~/.grabbox/jobs.json (same file the Python
    /// core writes; best-effort, atomic replace).
    fn persist(&self) {
        let Some(path) = jobs_file() else { return };
        let order = self.order.lock().unwrap().clone();
        let jobs = self.jobs.lock().unwrap();
        let mut rows = Vec::new();
        for id in &order {
            if let Some(j) = jobs.get(id) {
                let st = j.lock().unwrap();
                rows.push(PersistedJob {
                    id: st.id.clone(),
                    url: st.url.clone(),
                    kind: st.kind.clone(),
                    quality: st.quality.clone(),
                    playlist: st.playlist,
                    directory: st.directory.clone(),
                    cookies: st.cookies.clone(),
                    filename_req: st.filename_req.clone(),
                    status: st.status.clone(),
                    percent: st.percent,
                    filename: st.filename.clone(),
                    path: st.path.clone(),
                    error: st.error.clone(),
                    hint: st.hint.clone(),
                    created: st.created,
                    finished: st.finished,
                    items_done: st.items_done,
                    items_total: st.items_total,
                });
            }
        }
        drop(jobs);
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(text) = serde_json::to_string(&rows) {
            let tmp = path.with_extension("json.tmp");
            if std::fs::write(&tmp, text).is_ok() {
                let _ = std::fs::rename(&tmp, &path);
            }
        }
    }

    /// Reload after a restart: nothing auto-starts; mid-flight jobs come
    /// back paused, their .part files still on disk for Resume.
    fn restore(&self) {
        let Some(path) = jobs_file() else { return };
        let Ok(text) = std::fs::read_to_string(&path) else { return };
        let Ok(rows) = serde_json::from_str::<Vec<PersistedJob>>(&text) else {
            return;
        };
        for row in rows {
            let mut st = JobState::default();
            st.id = row.id.clone();
            st.url = row.url;
            st.kind = row.kind;
            st.quality = row.quality;
            st.playlist = row.playlist;
            st.directory = row.directory;
            st.cookies = row.cookies;
            st.filename_req = row.filename_req;
            st.filename = row.filename;
            st.path = row.path;
            st.error = row.error;
            st.hint = row.hint;
            st.created = row.created;
            st.finished = row.finished;
            st.items_done = row.items_done;
            st.items_total = row.items_total;
            st.percent = row.percent;
            st.status = if row.status == "running" || row.status == "queued" {
                "paused".into()
            } else {
                row.status
            };
            self.jobs
                .lock()
                .unwrap()
                .insert(row.id.clone(), Arc::new(Mutex::new(st)));
            self.order.lock().unwrap().push(row.id.clone());
            self.children
                .lock()
                .unwrap()
                .insert(row.id, Arc::new(Mutex::new(None)));
        }
    }

    pub fn add(
        &self,
        engine: Engine,
        config: Arc<Config>,
        url: &str,
        kind: &str,
        quality: Option<&str>,
        playlist: bool,
        directory: Option<&str>,
        cookies: Option<&str>,
        filename: Option<&str>,
    ) -> JobHandle {
        let id = format!("j{}-{:x}", COUNTER.fetch_add(1, Ordering::SeqCst), now() as u64 & 0xffffff);
        let mut st = JobState::default();
        st.id = id.clone();
        st.url = url.to_string();
        st.kind = kind.to_string();
        st.status = "queued".into();
        st.attempt = 1;
        st.attempts_total = 1; // set properly in worker
        st.created = now();
        st.playlist = playlist;
        let state = Arc::new(Mutex::new(st));
        self.jobs.lock().unwrap().insert(id.clone(), state.clone());
        self.order.lock().unwrap().push(id.clone());
        let child_slot = Arc::new(Mutex::new(None::<Child>));
        self.children
            .lock()
            .unwrap()
            .insert(id.clone(), child_slot.clone());

        let manager = self.clone();
        let quality = quality.map(|s| s.to_string());
        let directory = directory
            .map(|s| s.to_string())
            .unwrap_or_else(|| config.download_dir());
        let cookies = cookies
            .filter(|c| !c.is_empty())
            .map(|s| s.to_string())
            .or_else(|| {
                let c = config.get_string("cookies_browser");
                if c.is_empty() {
                    None
                } else {
                    Some(c)
                }
            });
        let filename = filename.map(|s| s.to_string());
        let url_owned = url.to_string();
        let kind_owned = kind.to_string();

        // Keep the request on the job so resume/retry (and a restart) can
        // rerun it; turbo settings ride along to the yt-dlp invocation.
        let connections = config.get_i64("connections", 8);
        let speed_limit_kbps = config.get_i64("speed_limit_kbps", 0);
        let turbo = config.get_bool("turbo");
        {
            let mut st = state.lock().unwrap();
            st.quality = quality.clone();
            st.directory = directory.clone();
            st.cookies = cookies.clone();
            st.filename_req = filename.clone();
        }
        self.persist();

        let worker_state = Arc::clone(&state);
        std::thread::spawn(move || {
            manager.worker(
                engine, worker_state, child_slot, url_owned, kind_owned, quality, playlist,
                directory, cookies, filename, connections, speed_limit_kbps, turbo,
            );
        });
        JobHandle { state }
    }

    pub fn cancel(&self, id: &str) -> bool {
        let Some(job) = self.jobs.lock().unwrap().get(id).cloned() else {
            return false;
        };
        {
            let mut st = job.lock().unwrap();
            if st.status != "queued" && st.status != "running" && st.status != "paused" {
                return false;
            }
            st.cancel = true;
            st.pause_requested = false;
            st.note("cancel requested");
        }
        // Kill the underlying process if there is one.
        if let Some(slot) = self.children.lock().unwrap().get(id) {
            let child = slot.lock().unwrap().take();
            if let Some(mut c) = child {
                let _ = c.kill();
            }
        }
        let mut st = job.lock().unwrap();
        if st.status != "done" && st.status != "error" {
            st.status = "canceled".into();
            st.finished = Some(now());
        }
        drop(st);
        self.persist();
        true
    }

    /// Pause keeps every downloaded byte: the child is killed, the .part
    /// file stays, and Resume spawns a fresh worker that continues it.
    pub fn pause(&self, id: &str) -> bool {
        let Some(job) = self.jobs.lock().unwrap().get(id).cloned() else {
            return false;
        };
        {
            let mut st = job.lock().unwrap();
            if st.status != "queued" && st.status != "running" {
                return false;
            }
            st.pause_requested = true;
            st.note("pause requested");
        }
        if let Some(slot) = self.children.lock().unwrap().get(id) {
            let child = slot.lock().unwrap().take();
            if let Some(mut c) = child {
                let _ = c.kill();
            }
        }
        true
    }

    /// Resume a paused job, or retry a failed/canceled one — same thing,
    /// because both engines continue partial files instead of restarting.
    pub fn resume(&self, engine: Engine, config: Arc<Config>, id: &str) -> bool {
        let Some(job) = self.jobs.lock().unwrap().get(id).cloned() else {
            return false;
        };
        let (url, kind, quality, playlist, directory, cookies, filename) = {
            let mut st = job.lock().unwrap();
            if st.status != "paused" && st.status != "error" && st.status != "canceled" {
                return false;
            }
            st.pause_requested = false;
            st.cancel = false;
            st.status = "queued".into();
            st.finished = None;
            st.error = None;
            st.hint = None;
            st.note("resumed");
            let dir = if st.directory.is_empty() {
                config.download_dir()
            } else {
                st.directory.clone()
            };
            (
                st.url.clone(),
                st.kind.clone(),
                st.quality.clone(),
                st.playlist,
                dir,
                st.cookies.clone(),
                st.filename_req.clone(),
            )
        };
        self.persist();
        let connections = config.get_i64("connections", 8);
        let speed_limit_kbps = config.get_i64("speed_limit_kbps", 0);
        let turbo = config.get_bool("turbo");
        let child_slot = Arc::new(Mutex::new(None::<Child>));
        self.children
            .lock()
            .unwrap()
            .insert(id.to_string(), child_slot.clone());
        let manager = self.clone();
        std::thread::spawn(move || {
            manager.worker(
                engine, job, child_slot, url, kind, quality, playlist, directory, cookies,
                filename, connections, speed_limit_kbps, turbo,
            );
        });
        true
    }

    pub fn list(&self) -> Vec<JobSnapshot> {
        let order = self.order.lock().unwrap();
        let jobs = self.jobs.lock().unwrap();
        order
            .iter()
            .filter_map(|id| jobs.get(id))
            .map(|j| j.lock().unwrap().snapshot())
            .collect()
    }

    pub fn clear_finished(&self) {
        let mut jobs = self.jobs.lock().unwrap();
        let done: Vec<String> = jobs
            .values()
            .filter(|j| {
                let s = &j.lock().unwrap().status;
                s == "done" || s == "error" || s == "canceled"
            })
            .map(|j| j.lock().unwrap().id.clone())
            .collect();
        for id in &done {
            jobs.remove(id);
        }
        self.order.lock().unwrap().retain(|id| !done.contains(id));
        self.children.lock().unwrap().retain(|id, _| !done.contains(id));
        self.persist();
    }

    // ------------------------------------------------------------ worker --

    #[allow(clippy::too_many_arguments)]
    fn worker(
        &self,
        engine: Engine,
        state: Arc<Mutex<JobState>>,
        child_slot: Arc<Mutex<Option<Child>>>,
        url: String,
        kind: String,
        quality: Option<String>,
        playlist: bool,
        outdir: String,
        cookies: Option<String>,
        filename: Option<String>,
        connections: i64,
        speed_limit_kbps: i64,
        turbo: bool,
    ) {
        self.sem.acquire();
        {
            let mut st = state.lock().unwrap();
            if st.cancel {
                st.status = "canceled".into();
                st.finished = Some(now());
                drop(st);
                self.persist();
                self.sem.release();
                return;
            }
            if st.pause_requested {
                st.pause_requested = false;
                st.status = "paused".into();
                drop(st);
                self.persist();
                self.sem.release();
                return;
            }
            st.status = "running".into();
            st.segments = if turbo { connections.max(0) as u32 } else { 0 };
        }
        let outdir_path = PathBuf::from(&outdir);
        let _ = std::fs::create_dir_all(&outdir_path);
        let ladder = Engine::ladder(&url);
        {
            let mut st = state.lock().unwrap();
            st.attempts_total = ladder.len() as u32;
        }
        let mut last_error = String::new();
        for (i, (label, rung_args)) in ladder.iter().enumerate() {
            {
                let mut st = state.lock().unwrap();
                if st.cancel {
                    st.status = "canceled".into();
                    st.finished = Some(now());
                    drop(st);
                    self.persist();
                    self.sem.release();
                    return;
                }
                if st.pause_requested {
                    st.pause_requested = false;
                    st.status = "paused".into();
                    drop(st);
                    self.persist();
                    self.sem.release();
                    return;
                }
                st.attempt = (i + 1) as u32;
                st.note(&format!("attempt {}/{}: {}", i + 1, ladder.len(), label));
            }
            match engine.spawn_download(
                &url,
                &kind,
                quality.as_deref(),
                playlist,
                &outdir_path,
                cookies.as_deref(),
                filename.as_deref(),
                rung_args,
                playlist,
                connections,
                speed_limit_kbps,
                turbo,
            ) {
                Ok(child) => {
                    *child_slot.lock().unwrap() = Some(child);
                    match self.follow_child(&engine, &state, child_slot.clone()) {
                        Ok(final_path) => {
                            let final_path = if final_path.is_empty() {
                                outdir.clone()
                            } else {
                                final_path
                            };
                            let mut st = state.lock().unwrap();
                            st.status = "done".into();
                            st.percent = 100.0;
                            st.path = Some(final_path.clone());
                            st.finished = Some(now());
                            st.error = None;
                            st.hint = None;
                            st.note(&format!("saved: {}", final_path));
                            let name = st
                                .filename
                                .clone()
                                .unwrap_or_else(|| "download".to_string());
                            drop(st);
                            self.persist();
                            notify_done(&self.app, &name);
                            self.sem.release();
                            return;
                        }
                        Err(err) => {
                            last_error = err.clone();
                            let mut st = state.lock().unwrap();
                            if st.pause_requested && !st.cancel {
                                st.pause_requested = false;
                                st.status = "paused".into();
                                st.finished = None;
                                st.error = None;
                                st.note("paused — resume keeps every byte so far");
                                drop(st);
                                self.persist();
                                self.sem.release();
                                return;
                            }
                            if st.cancel {
                                st.status = "canceled".into();
                                st.finished = Some(now());
                                self.sem.release();
                                return;
                            }
                            st.error = Some(first_line(&err, 400));
                            let hint = diagnose(&err);
                            st.hint = if hint.is_empty() { None } else { Some(hint) };
                            st.note(&format!(
                                "attempt {} failed: {}",
                                i + 1,
                                first_line(&err, 200)
                            ));
                        }
                    }
                }
                Err(e) => {
                    last_error = e.to_string();
                    let mut st = state.lock().unwrap();
                    st.error = Some(last_error.clone());
                    st.hint = Some("yt-dlp engine is missing - reinstall GrabBox".into());
                    st.note("could not start the engine");
                    break;
                }
            }
        }
        {
            let mut st = state.lock().unwrap();
            st.status = "error".into();
            st.finished = Some(now());
            if st.error.is_none() && !last_error.is_empty() {
                st.error = Some(first_line(&last_error, 400));
            }
            st.note("all attempts failed");
            drop(st);
            self.persist();
        }
        self.sem.release();
    }

    /// Stream the child's stdout, updating progress; returns the final path.
    fn follow_child(
        &self,
        _engine: &Engine,
        state: &Arc<Mutex<JobState>>,
        child_slot: Arc<Mutex<Option<Child>>>,
    ) -> Result<String, String> {
        let mut child = child_slot.lock().unwrap().take().ok_or("no child")?;
        let stdout = child.stdout.take().ok_or("no stdout")?;
        let mut reader = BufReader::new(stdout);
        // Drain stderr in a side thread so the child never blocks on a full pipe.
        let stderr_buf = Arc::new(Mutex::new(String::new()));
        let stderr_buf2 = stderr_buf.clone();
        if let Some(stderr) = child.stderr.take() {
            std::thread::spawn(move || {
                let mut r = BufReader::new(stderr);
                let mut line = String::new();
                loop {
                    line.clear();
                    match r.read_line(&mut line) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let mut buf = stderr_buf2.lock().unwrap();
                            buf.push_str(&line);
                            if buf.len() > 64_000 {
                                let keep = buf.split_off(48_000);
                                *buf = keep;
                            }
                        }
                    }
                }
            });
        }

        let mut final_path: Option<String> = None;
        let mut line = String::new();
        loop {
            line.clear();
            let n = reader
                .read_line(&mut line)
                .map_err(|e| format!("read: {}", e))?;
            if n == 0 {
                break;
            }
            let l = line.trim();
            if l.is_empty() {
                continue;
            }
            let mut st = state.lock().unwrap();
            if st.cancel {
                drop(st);
                let _ = child.kill();
                return Err("canceled".to_string());
            }
            if let Some(rest) = l.strip_prefix("GBX|") {
                let mut it = rest.splitn(4, '|');
                let percent = it.next().and_then(parse_number);
                let speed = it.next().and_then(parse_number);
                let eta = it.next().and_then(parse_number);
                let fname = it.next().map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
                if let Some(p) = percent {
                    st.percent = p.min(100.0);
                }
                st.speed = speed.filter(|s| *s > 0.0);
                st.eta = eta.filter(|e| *e > 0.0);
                if let Some(f) = fname {
                    st.filename = Some(
                        f.rsplit(['/', '\\'])
                            .next()
                            .unwrap_or(&f)
                            .to_string(),
                    );
                }
            } else if let Some(rest) = l.strip_prefix("[download] Destination: ") {
                st.filename = Some(basename(rest));
                final_path = Some(rest.trim().to_string());
            } else if l.starts_with("[Merger]") || l.starts_with("[VideoConvertor]") || l.starts_with("[VideoRemuxer]") {
                if let Some(path) = quoted_after_into(l) {
                    st.filename = Some(basename(&path));
                    final_path = Some(path);
                }
            } else if let Some(rest) = l.strip_prefix("[ExtractAudio] Destination: ") {
                st.filename = Some(basename(rest));
                final_path = Some(rest.trim().to_string());
            } else if l.starts_with("[download] Downloading item ") || l.starts_with("[download] Downloading video ") {
                if let Some((done, total)) = parse_item_of(l) {
                    st.items_done = done;
                    st.items_total = total;
                }
            } else if l.starts_with("[MoveFiles]") {
                if let Some(path) = quoted_after_into(l) {
                    st.filename = Some(basename(&path));
                    final_path = Some(path);
                }
            }
        }
        let status = child.wait().map_err(|e| format!("wait: {}", e))?;
        if status.success() {
            let st = state.lock().unwrap();
            if let Some(p) = st.path.clone() {
                return Ok(p);
            }
            if let Some(p) = final_path {
                return Ok(p);
            }
            // Fallback: newest file in the folder would be fsys's job; report dir.
            return Ok(String::new());
        }
        let err = stderr_buf.lock().unwrap().clone();
        if err.trim().is_empty() {
            Err(format!("yt-dlp exited with {}", status))
        } else {
            Err(err)
        }
    }
}

/// OS notification when a download completes (graceful if the OS refuses).
fn notify_done(app: &Option<tauri::AppHandle>, name: &str) {
    let Some(app) = app else { return };
    use tauri_plugin_notification::NotificationExt;
    let _ = app
        .notification()
        .builder()
        .title("GrabBox — download complete")
        .body(name)
        .show();
}

fn parse_number(s: &str) -> Option<f64> {
    let s = s.trim().trim_end_matches('%').trim();
    let v: f64 = s.parse().ok()?;
    if v.is_finite() && v > 0.0 {
        Some(v)
    } else {
        None
    }
}

fn basename(p: &str) -> String {
    p.trim()
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(p.trim())
        .to_string()
}

/// Pull the "..." path out of lines like `[Merger] Merging formats into "X"`.
fn quoted_after_into(line: &str) -> Option<String> {
    let start = line.find('"')? + 1;
    let end = line.rfind('"')?;
    if end > start {
        Some(line[start..end].to_string())
    } else {
        None
    }
}

/// "[download] Downloading item 3 of 12" -> (3, 12)
fn parse_item_of(line: &str) -> Option<(u32, u32)> {
    let words: Vec<&str> = line.split_whitespace().collect();
    let of_pos = words.iter().position(|w| *w == "of")?;
    let done: u32 = words.get(of_pos.wrapping_sub(1))?.parse().ok()?;
    let total: u32 = words.get(of_pos + 1)?.parse().ok()?;
    Some((done, total))
}

fn first_line(s: &str, max: usize) -> String {
    let first = s
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .last()
        .unwrap_or("unknown error");
    if first.len() > max {
        format!("{}…", &first[..max])
    } else {
        first.to_string()
    }
}
