//! The horse, native edition: work-stealing segmented direct-file
//! downloads — the same engine as the Python core's `grabbox/horse.py`
//! (see docs/HORSE.md for the shared spec). One behaviour, three stacks.
//!
//! The file starts as ONE segment. A free connection either takes an
//! orphaned segment whole, or splits the segment that will finish last —
//! proportional to measured speeds (design credit: DownloadCraft's MIT
//! engine, see NOTICE.md) — so a connection that turns out slow keeps a
//! small share and never holds the whole download hostage, and no
//! connection idles while work remains.
//!
//! Progress lives in `<file>.horse.json` beside the `<file>.part` work
//! file, guarded by size + ETag/Last-Modified, so pause / crash / reboot
//! resumes mid-segment. Servers that lie about Range support degrade
//! quietly to a single stream that still resumes by appending.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use ureq::{Agent, AgentBuilder};

const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) \
    AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

const CHUNK: u64 = 256 * 1024; // per-connection read/write block
const MIN_SEGMENT: u64 = 512 * 1024; // don't slice thinner than this
const SPLIT_MIN: u64 = 256 * 1024; // smallest slice worth stealing
const STATE_SUFFIX: &str = ".horse.json";
const PART_SUFFIX: &str = ".part";
const TIMEOUT: Duration = Duration::from_secs(30);

// ------------------------------------------------------------- outcomes --

#[derive(Clone, Copy, PartialEq)]
pub enum Stop {
    Pause,
    Cancel,
}

/// What the caller (jobs.rs) needs to know when a download ends early.
pub enum HorseError {
    /// Stopped on purpose; resume continues from the saved segment map.
    Paused,
    /// Not horse-shaped (e.g. a web page). Use yt-dlp instead.
    Fallback(String),
    /// Failed; the .part + state stay on disk so a retry resumes.
    Failed(String),
}

/// Internal error vocabulary (adds the two states the public API hides).
#[derive(Clone, Debug)]
enum HErr {
    Paused,
    Fallback(String),
    Failed(String),
    /// Server advertised ranges, then answered 200. Degrades to one stream.
    RangeIgnored,
    /// 429/503: back off and retry the segment — never a hard failure.
    Throttled,
}

type StopFn = dyn Fn() -> Option<Stop> + Send + Sync;
type ProgressFn = dyn Fn(u64, Option<u64>, f64) + Send + Sync;

// ------------------------------------------------------------- helpers --

fn check_stop(stop: &Option<Arc<StopFn>>) -> Result<(), HErr> {
    if let Some(f) = stop {
        match f() {
            Some(Stop::Pause) => return Err(HErr::Paused),
            Some(Stop::Cancel) => return Err(HErr::Failed("canceled".into())),
            None => {}
        }
    }
    Ok(())
}

fn current_stop(stop: &Option<Arc<StopFn>>) -> Option<Stop> {
    stop.as_ref().and_then(|f| f())
}

struct RateLimiter {
    rate: f64, // bytes/sec, 0 = unlimited
    m: Mutex<(f64, Instant)>,
}

impl RateLimiter {
    fn new(bytes_per_sec: u64) -> Self {
        RateLimiter {
            rate: bytes_per_sec as f64,
            m: Mutex::new((bytes_per_sec as f64, Instant::now())),
        }
    }

    /// Shared token bucket: the *aggregate* job rate, split over segments.
    fn take(&self, n: u64) {
        if self.rate <= 0.0 {
            return;
        }
        loop {
            let wait = {
                let mut g = self.m.lock().unwrap();
                let now = Instant::now();
                g.0 = (self.rate).min(g.0 + now.duration_since(g.1).as_secs_f64() * self.rate);
                g.1 = now;
                if g.0 >= n as f64 {
                    g.0 -= n as f64;
                    return;
                }
                (n as f64 - g.0) / self.rate
            };
            std::thread::sleep(Duration::from_secs_f64(wait.min(0.25)));
        }
    }
}

/// Thread-safe counters + an EMA speed, reported ~4x per second.
struct Progress {
    total: Option<u64>,
    cb: Option<Arc<ProgressFn>>,
    m: Mutex<ProgInner>,
}

struct ProgInner {
    done: u64,
    speed: f64,
    last_t: Instant,
    last_done: u64,
    last_push: Instant,
}

impl Progress {
    fn new(total: Option<u64>, done: u64, cb: Option<Arc<ProgressFn>>) -> Self {
        Progress {
            total,
            cb,
            m: Mutex::new(ProgInner {
                done,
                speed: 0.0,
                last_t: Instant::now(),
                last_done: done,
                last_push: Instant::now() - Duration::from_secs(1),
            }),
        }
    }

    fn done(&self) -> u64 {
        self.m.lock().unwrap().done
    }

    fn set_done(&self, v: u64) {
        let mut g = self.m.lock().unwrap();
        g.done = v;
        g.last_done = v;
    }

    fn add(&self, n: u64) {
        let mut g = self.m.lock().unwrap();
        g.done += n;
        let now = Instant::now();
        let dt = now.duration_since(g.last_t).as_secs_f64();
        if dt >= 0.25 {
            let inst = (g.done - g.last_done) as f64 / dt;
            g.speed = if g.speed == 0.0 { inst } else { 0.6 * g.speed + 0.4 * inst };
            g.last_t = now;
            g.last_done = g.done;
        }
    }

    fn push(&self, force: bool) {
        let Some(cb) = &self.cb else { return };
        let (done, total, speed) = {
            let mut g = self.m.lock().unwrap();
            let now = Instant::now();
            if !force && now.duration_since(g.last_push).as_secs_f64() < 0.25 {
                return;
            }
            g.last_push = now;
            (g.done, self.total, g.speed)
        };
        cb(done, total, speed);
    }
}

// --------------------------------------------------------- state file --

#[derive(Serialize, Deserialize, Clone)]
struct SegRow {
    start: u64,
    end: u64,
    done: u64,
}

#[derive(Serialize, Deserialize, Clone)]
struct StateJson {
    url: String,
    size: Option<u64>,
    etag: Option<String>,
    last_modified: Option<String>,
    segments: Vec<SegRow>,
}

fn state_load(path: &Path) -> Option<StateJson> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn state_save(path: &Path, data: &StateJson) {
    let tmp = suffix_path(path, ".tmp");
    if let Ok(text) = serde_json::to_string(data) {
        if std::fs::write(&tmp, text).is_ok() {
            replace_file(&tmp, path);
        }
    }
}

fn state_drop(path: &Path) {
    let _ = std::fs::remove_file(path);
}

/// os.replace semantics that also work on Windows (rename refuses to
/// overwrite an existing destination there).
fn replace_file(from: &Path, to: &Path) {
    if std::fs::rename(from, to).is_err() {
        let _ = std::fs::remove_file(to);
        let _ = std::fs::rename(from, to);
    }
}

fn suffix_path(p: &Path, suffix: &str) -> PathBuf {
    let mut s = p.as_os_str().to_owned();
    s.push(suffix);
    PathBuf::from(s)
}

/// Saved state may be reused only for the same URL and same file body.
fn valid_resume(state: &StateJson, url: &str, info: &ProbeInfo) -> bool {
    if state.url != url {
        return false;
    }
    if let (Some(want), Some(have)) = (info.size, state.size) {
        if want != have {
            return false;
        }
    }
    for (fresh, saved) in [
        (&info.etag, &state.etag),
        (&info.last_modified, &state.last_modified),
    ] {
        if let (Some(a), Some(b)) = (fresh, saved) {
            if a != b {
                return false;
            }
        }
    }
    !state.segments.is_empty()
}

// --------------------------------------------------------------- probe --

#[derive(Clone)]
struct ProbeInfo {
    size: Option<u64>,
    ranges: bool,
    etag: Option<String>,
    last_modified: Option<String>,
    status: u16,
}

fn probe_ranges(agent: &Agent, url: &str) -> Result<ProbeInfo, HErr> {
    // One 1-byte ranged GET tells us everything: total size, whether
    // ranges are honoured, and the validators a later resume must match.
    let resp = match agent.get(url).set("Range", "bytes=0-0").call() {
        Ok(r) => r,
        Err(ureq::Error::Status(code, _)) => {
            return Ok(ProbeInfo {
                size: None,
                ranges: false,
                etag: None,
                last_modified: None,
                status: code,
            })
        }
        Err(e) => return Err(HErr::Failed(e.to_string())),
    };
    let status = resp.status();
    let crange = resp.header("Content-Range").map(|s| s.to_string());
    let length = resp.header("Content-Length").map(|s| s.to_string());
    let accept = resp.header("Accept-Ranges").map(|s| s.to_string());
    let ctype = resp
        .header("Content-Type")
        .map(|s| s.to_lowercase())
        .unwrap_or_default();
    let etag = resp.header("ETag").map(|s| s.to_string());
    let last_modified = resp.header("Last-Modified").map(|s| s.to_string());
    // Drain a byte so the connection can be reused, then drop it.
    let mut reader = resp.into_reader();
    let mut buf = [0u8; 1024];
    let _ = reader.read(&mut buf);

    let mut size = None;
    if let Some(cr) = &crange {
        if let Some(tail) = cr.rsplit('/').next() {
            if let Ok(n) = tail.trim().parse::<u64>() {
                size = Some(n);
            }
        }
    }
    if size.is_none() && status == 200 {
        if let Some(l) = &length {
            if let Ok(n) = l.trim().parse::<u64>() {
                size = Some(n);
            }
        }
    }
    let ranges = status == 206 || accept.map(|a| a.contains("bytes")).unwrap_or(false);
    if status == 200 && ctype.starts_with("text/html") && size.is_none() {
        return Err(HErr::Fallback(
            "server returned a web page, not a file".into(),
        ));
    }
    Ok(ProbeInfo {
        size,
        ranges,
        etag,
        last_modified,
        status,
    })
}

// ------------------------------------------------------------ download --

/// Download `url` to `dest` (atomically, via `dest + ".part"`).
/// Returns the final path; on failure the partial state is kept for resume.
pub fn download(
    url: &str,
    dest: &Path,
    connections: i64,
    rate_limit_bps: u64,
    should_stop: Option<Arc<StopFn>>,
    progress: Option<Arc<ProgressFn>>,
) -> Result<PathBuf, HorseError> {
    let connections = connections.clamp(1, 16);
    let agent = AgentBuilder::new()
        .timeout_connect(TIMEOUT)
        .timeout_read(TIMEOUT)
        .user_agent(USER_AGENT)
        .build();
    match download_inner(&agent, url, dest, connections, rate_limit_bps, &should_stop, &progress) {
        Ok(p) => Ok(p),
        Err(HErr::Paused) => Err(HorseError::Paused),
        Err(HErr::Fallback(m)) => Err(HorseError::Fallback(m)),
        Err(HErr::Failed(m)) => Err(HorseError::Failed(m)),
        Err(HErr::RangeIgnored) => Err(HorseError::Failed("server ignored Range".into())),
        Err(HErr::Throttled) => Err(HorseError::Failed("throttled".into())),
    }
}

fn download_inner(
    agent: &Agent,
    url: &str,
    dest: &Path,
    connections: i64,
    rate_limit_bps: u64,
    should_stop: &Option<Arc<StopFn>>,
    progress: &Option<Arc<ProgressFn>>,
) -> Result<PathBuf, HErr> {
    let info = probe_ranges(agent, url)?;
    if info.status >= 400 {
        return Err(HErr::Failed(format!("HTTP {}", info.status)));
    }
    let work = suffix_path(dest, PART_SUFFIX);
    let state_path = suffix_path(dest, STATE_SUFFIX);
    let mut saved = state_load(&state_path);
    let resume = saved
        .as_ref()
        .map(|s| valid_resume(s, url, &info) && work.exists())
        .unwrap_or(false);
    if !resume {
        state_drop(&state_path);
        saved = None;
        let _ = std::fs::remove_file(&work);
    }

    if let Some(size) = info.size {
        if info.ranges && connections > 1 && size >= MIN_SEGMENT * 2 {
            match download_segmented(
                agent, url, dest, &work, &state_path, saved.clone(), &info, size,
                connections as usize, rate_limit_bps, should_stop, progress,
            ) {
                Ok(p) => return Ok(p),
                Err(HErr::RangeIgnored) => {
                    saved = None; // ranged claims were a lie; start clean
                }
                Err(e) => return Err(e),
            }
        }
    }
    download_single(
        agent, url, dest, &work, &state_path, saved, &info, rate_limit_bps, should_stop, progress,
    )
}

fn state_dict(url: &str, info: &ProbeInfo, segments: Vec<SegRow>) -> StateJson {
    StateJson {
        url: url.to_string(),
        size: info.size,
        etag: info.etag.clone(),
        last_modified: info.last_modified.clone(),
        segments,
    }
}

// ---------------------------------------------------------- segmented --

#[derive(Clone)]
struct Segment {
    start: u64,
    pos: u64,
    end: u64,
    owner: Option<usize>,
    fails: u32,
}

impl Segment {
    fn done(&self) -> u64 {
        self.pos.min(self.end).saturating_sub(self.start)
    }
}

struct Shared {
    inner: Mutex<Inner>,
    prog: Progress,
    limiter: RateLimiter,
    state_path: PathBuf,
    url: String,
    info: ProbeInfo,
    stop: Option<Arc<StopFn>>,
}

struct Inner {
    segments: Vec<Segment>,
    speeds: HashMap<usize, f64>, // worker -> EMA bytes/sec
    speed_bytes: HashMap<usize, (u64, Instant)>,
    failure: Option<HErr>,
}

impl Shared {
    fn persist(&self) {
        let rows = {
            let g = self.inner.lock().unwrap();
            let mut segs: Vec<&Segment> = g.segments.iter().collect();
            segs.sort_by_key(|s| s.start);
            segs.iter()
                .map(|s| SegRow {
                    start: s.start,
                    end: s.end,
                    done: s.done(),
                })
                .collect::<Vec<_>>()
        };
        state_save(&self.state_path, &state_dict(&self.url, &self.info, rows));
    }

    fn set_failure(&self, e: HErr) {
        let mut g = self.inner.lock().unwrap();
        if g.failure.is_none() {
            g.failure = Some(e);
        }
    }

    fn speed_of(g: &Inner, worker: usize) -> f64 {
        g.speeds.get(&worker).copied().unwrap_or(0.0)
    }

    /// Take orphaned work, or split the segment finishing last.
    fn claim(&self, worker: usize) -> Option<usize> {
        let mut g = self.inner.lock().unwrap();
        // 1. Orphaned segments first: biggest one wins.
        let mut best: Option<usize> = None;
        for (i, s) in g.segments.iter().enumerate() {
            if s.owner.is_none() && s.end > s.pos {
                match best {
                    None => best = Some(i),
                    Some(b) => {
                        let cur = &g.segments[b];
                        if (s.end - s.pos, std::cmp::Reverse(s.start))
                            > (cur.end - cur.pos, std::cmp::Reverse(cur.start))
                        {
                            best = Some(i);
                        }
                    }
                }
            }
        }
        if let Some(i) = best {
            g.segments[i].owner = Some(worker);
            return Some(i);
        }
        // 2. Split the segment with the longest expected finish time.
        let mut pick: Option<usize> = None;
        let mut pick_key = (f64::MIN, 0u64);
        for (i, s) in g.segments.iter().enumerate() {
            if s.owner.is_some() && s.end - s.pos >= 2 * SPLIT_MIN {
                let rem = s.end - s.pos;
                let v = Self::speed_of(&g, s.owner.unwrap_or(usize::MAX));
                let eta = if v > 0.0 { rem as f64 / v } else { rem as f64 };
                let key = (eta, rem);
                if key > pick_key {
                    pick_key = key;
                    pick = Some(i);
                }
            }
        }
        let i = pick?;
        let (pos, end, owner) = {
            let s = &g.segments[i];
            (s.pos, s.end, s.owner.unwrap_or(usize::MAX))
        };
        let rem = end - pos;
        let owner_v = Self::speed_of(&g, owner);
        let measured: Vec<f64> = g.speeds.values().copied().filter(|v| *v > 0.0).collect();
        let avg = if measured.is_empty() {
            0.0
        } else {
            measured.iter().sum::<f64>() / measured.len() as f64
        };
        // Proportional split: the owner keeps the share it can finish in
        // the same time the newcomer needs for the rest.
        let keep_f = if owner_v > 0.0 && avg > 0.0 {
            rem as f64 * owner_v / (owner_v + avg)
        } else {
            rem as f64 / 2.0
        };
        let keep = (keep_f as u64).clamp(SPLIT_MIN, rem - SPLIT_MIN);
        let mut mid = pos + keep;
        let aligned = mid - mid % (64 * 1024);
        if aligned >= pos + SPLIT_MIN && end - aligned >= SPLIT_MIN {
            mid = aligned;
        }
        let idx = g.segments.len();
        g.segments[i].end = mid;
        g.segments.push(Segment {
            start: mid,
            pos: mid,
            end,
            owner: Some(worker),
            fails: 0,
        });
        Some(idx)
    }

    fn note_speed(g: &mut Inner, worker: usize, n: u64) {
        let now = Instant::now();
        let acc = g.speed_bytes.entry(worker).or_insert((0, now));
        acc.0 += n;
        let dt = now.duration_since(acc.1).as_secs_f64();
        if dt >= 0.25 {
            let inst = acc.0 as f64 / dt.max(0.001);
            let old = Self::speed_of(g, worker);
            g.speeds.insert(
                worker,
                if old == 0.0 { inst } else { 0.6 * old + 0.4 * inst },
            );
            g.speed_bytes.insert(worker, (0, now));
        }
    }
}

fn run_claim(sh: &Shared, agent: &Agent, worker: usize, idx: usize, work: &Path) -> Result<(), HErr> {
    let frm = {
        let g = sh.inner.lock().unwrap();
        g.segments[idx].pos
    };
    let end0 = {
        let g = sh.inner.lock().unwrap();
        g.segments[idx].end
    };
    let req = agent
        .get(&sh.url)
        .set("Range", format!("bytes={}-{}", frm, end0.saturating_sub(1)));
    let resp = match req.call() {
        Ok(r) => r,
        Err(ureq::Error::Status(429, _)) | Err(ureq::Error::Status(503, _)) => {
            return Err(HErr::Throttled)
        }
        Err(ureq::Error::Status(416, _)) => {
            return Err(HErr::Failed("range not satisfiable (HTTP 416)".into()))
        }
        Err(ureq::Error::Status(code, _)) => return Err(HErr::Failed(format!("HTTP {}", code))),
        Err(e) => return Err(HErr::Failed(e.to_string())),
    };
    if resp.status() != 206 {
        return Err(HErr::RangeIgnored);
    }
    let mut reader = resp.into_reader();
    let mut fh = OpenOptions::new()
        .read(true)
        .write(true)
        .open(work)
        .map_err(|e| HErr::Failed(e.to_string()))?;
    loop {
        check_stop(&sh.stop)?;
        let (pos, end) = {
            let g = sh.inner.lock().unwrap();
            (g.segments[idx].pos, g.segments[idx].end)
        };
        if pos >= end {
            return Ok(());
        }
        let mut block = vec![0u8; CHUNK.min(end - pos) as usize];
        let n = reader
            .read(&mut block)
            .map_err(|e| HErr::Failed(e.to_string()))?;
        if n == 0 {
            return Err(HErr::Failed("connection dropped mid-segment".into()));
        }
        block.truncate(n);
        // Our bound may have shrunk (a split) mid-read: only the bytes up
        // to the current end are ours to write and count.
        let allowed = {
            let mut g = sh.inner.lock().unwrap();
            let seg = &mut g.segments[idx];
            let allowed = (block.len() as u64).min(seg.end.saturating_sub(pos));
            if allowed > 0 {
                seg.pos = pos + allowed;
            }
            allowed
        };
        if allowed > 0 {
            sh.limiter.take(allowed);
            fh.seek(SeekFrom::Start(pos)).map_err(|e| HErr::Failed(e.to_string()))?;
            fh.write_all(&block[..allowed as usize])
                .map_err(|e| HErr::Failed(e.to_string()))?;
            {
                let mut g = sh.inner.lock().unwrap();
                Shared::note_speed(&mut g, worker, allowed);
            }
            sh.prog.add(allowed);
            sh.prog.push(false);
            sh.persist();
        }
        if (allowed as usize) < block.len() {
            return Ok(()); // shrunk past the stream; segment done
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn download_segmented(
    agent: &Agent,
    url: &str,
    dest: &Path,
    work: &Path,
    state_path: &Path,
    saved: Option<StateJson>,
    info: &ProbeInfo,
    size: u64,
    connections: usize,
    rate_limit_bps: u64,
    should_stop: &Option<Arc<StopFn>>,
    progress: &Option<Arc<ProgressFn>>,
) -> Result<PathBuf, HErr> {
    let mut segments: Vec<Segment> = Vec::new();
    if let Some(state) = &saved {
        let mut raw: Vec<&SegRow> = state.segments.iter().collect();
        raw.sort_by_key(|s| s.start);
        let ok = !raw.is_empty()
            && raw[0].start == 0
            && raw[raw.len() - 1].end == size
            && raw.windows(2).all(|w| w[0].end == w[1].start);
        if ok {
            for s in raw {
                segments.push(Segment {
                    start: s.start,
                    pos: s.start + s.done,
                    end: s.end,
                    owner: None,
                    fails: 0,
                });
            }
        }
    }
    if segments.is_empty() {
        let fh = File::create(work).map_err(|e| HErr::Failed(e.to_string()))?;
        fh.set_len(size).map_err(|e| HErr::Failed(e.to_string()))?;
        drop(fh);
        segments.push(Segment {
            start: 0,
            pos: 0,
            end: size,
            owner: None,
            fails: 0,
        });
    }
    let done0: u64 = segments.iter().map(|s| s.done()).sum();

    let sh = Arc::new(Shared {
        inner: Mutex::new(Inner {
            segments,
            speeds: HashMap::new(),
            speed_bytes: HashMap::new(),
            failure: None,
        }),
        prog: Progress::new(Some(size), done0, progress.clone()),
        limiter: RateLimiter::new(rate_limit_bps),
        state_path: state_path.to_path_buf(),
        url: url.to_string(),
        info: info.clone(),
        stop: should_stop.clone(),
    });
    sh.prog.push(true);

    std::thread::scope(|scope| {
        for wid in 0..connections.max(1) {
            let sh = Arc::clone(&sh);
            scope.spawn(move || worker_loop(&sh, agent, wid, work, should_stop));
        }
    });

    sh.persist();
    sh.prog.push(true);
    {
        let g = sh.inner.lock().unwrap();
        if let Some(e) = &g.failure {
            return Err(e.clone());
        }
    }
    if sh.prog.done() != size {
        return Err(HErr::Failed(format!(
            "incomplete: {} of {} bytes",
            sh.prog.done(),
            size
        )));
    }
    replace_file(work, dest);
    state_drop(state_path);
    Ok(dest.to_path_buf())
}

fn worker_loop(sh: &Shared, agent: &Agent, wid: usize, work: &Path, stop: &Option<Arc<StopFn>>) {
    loop {
        let Some(idx) = sh.claim(wid) else { return };
        match run_claim(sh, agent, wid, idx, work) {
            Ok(()) => {
                let mut g = sh.inner.lock().unwrap();
                g.segments[idx].owner = None;
            }
            Err(HErr::Throttled) => {
                {
                    let mut g = sh.inner.lock().unwrap();
                    g.segments[idx].owner = None;
                }
                std::thread::sleep(Duration::from_secs(2)); // ease off, keep the bytes
            }
            Err(e @ (HErr::Paused | HErr::RangeIgnored | HErr::Fallback(_))) => {
                sh.set_failure(e);
                return;
            }
            Err(HErr::Failed(msg)) => {
                match current_stop(stop) {
                    Some(Stop::Pause) => {
                        sh.set_failure(HErr::Paused);
                        return;
                    }
                    Some(Stop::Cancel) => {
                        sh.set_failure(HErr::Failed("canceled".into()));
                        return;
                    }
                    None => {}
                }
                let bad = {
                    let mut g = sh.inner.lock().unwrap();
                    g.segments[idx].owner = None;
                    g.segments[idx].fails += 1;
                    g.segments[idx].fails >= 6
                };
                if bad {
                    sh.set_failure(HErr::Failed(msg));
                    return;
                }
            }
        }
    }
}

// ------------------------------------------------------------- single --

#[allow(clippy::too_many_arguments)]
fn download_single(
    agent: &Agent,
    url: &str,
    dest: &Path,
    work: &Path,
    state_path: &Path,
    saved: Option<StateJson>,
    info: &ProbeInfo,
    rate_limit_bps: u64,
    should_stop: &Option<Arc<StopFn>>,
    progress: &Option<Arc<ProgressFn>>,
) -> Result<PathBuf, HErr> {
    let size = info.size;
    // Single-stream state is one segment starting at 0, so its `done` is
    // exactly the contiguous prefix we already hold.
    let mut have: u64 = 0;
    if let Some(state) = &saved {
        if let Some(first) = state.segments.first() {
            have = first.done;
        }
    }
    match std::fs::metadata(work) {
        Ok(md) => have = have.min(md.len()),
        Err(_) => have = 0,
    }
    if have > 0 && size.is_none() {
        have = 0; // cannot verify a resume point without a total; restart
    }

    let byte_range = if have > 0 {
        match size {
            Some(sz) if info.ranges => Some((have, sz.saturating_sub(1))),
            _ => None,
        }
    } else {
        None
    };

    let prog = Progress::new(size, have, progress.clone());
    prog.push(true);
    let limiter = RateLimiter::new(rate_limit_bps);

    let mut req = agent.get(url);
    if let Some((a, b)) = byte_range {
        req = req.set("Range", format!("bytes={}-{}", a, b));
    }
    let resp = match req.call() {
        Ok(r) => r,
        Err(ureq::Error::Status(code, _)) => {
            return Err(HErr::Failed(format!("HTTP {}", code)))
        }
        Err(e) => return Err(HErr::Failed(e.to_string())),
    };
    if byte_range.is_some() && resp.status() != 206 {
        have = 0; // server ignored our resume point
        prog.set_done(0);
    }
    let mut reader = resp.into_reader();
    {
        let mut fh = if have > 0 {
            OpenOptions::new()
                .append(true)
                .open(work)
                .map_err(|e| HErr::Failed(e.to_string()))?
        } else {
            File::create(work).map_err(|e| HErr::Failed(e.to_string()))?
        };
        loop {
            check_stop(should_stop)?;
            let mut block = vec![0u8; CHUNK as usize];
            let n = reader
                .read(&mut block)
                .map_err(|e| HErr::Failed(e.to_string()))?;
            if n == 0 {
                break;
            }
            block.truncate(n);
            limiter.take(n as u64);
            fh.write_all(&block).map_err(|e| HErr::Failed(e.to_string()))?;
            have += n as u64;
            prog.add(n as u64);
            prog.push(false);
            state_save(
                state_path,
                &state_dict(
                    url,
                    info,
                    vec![SegRow {
                        start: 0,
                        end: size.unwrap_or(have),
                        done: have,
                    }],
                ),
            );
        }
    }

    if let Some(sz) = size {
        if have != sz {
            return Err(HErr::Failed(format!(
                "incomplete: {} of {} bytes",
                have, sz
            )));
        }
    }
    prog.push(true);
    replace_file(work, dest);
    state_drop(state_path);
    Ok(dest.to_path_buf())
}
