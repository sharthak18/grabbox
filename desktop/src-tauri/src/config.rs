//! Settings, persisted in ~/.grabbox/config.json — the same file the Python
//! server uses, so switching between desktop app and server keeps your setup.

use serde_json::{json, Map, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

pub struct Config {
    path: PathBuf,
    data: Mutex<Map<String, Value>>,
}

/// Untrusted folder setting -> a usable absolute path, or an error message.
/// Trims quotes/space, expands `~/`, creates the folder if needed.
pub fn normalize_dir(raw: &str) -> Result<String, String> {
    let trimmed = raw.trim().trim_matches('"').trim_matches('\'').trim();
    if trimmed.is_empty() {
        return Ok(default_map()["download_dir"]
            .as_str()
            .unwrap_or(".")
            .to_string());
    }
    let expanded = match trimmed.strip_prefix("~/") {
        Some(rest) => match dirs::home_dir() {
            Some(home) => home.join(rest).to_string_lossy().into_owned(),
            None => trimmed.to_string(),
        },
        None => trimmed.to_string(),
    };
    std::fs::create_dir_all(&expanded)
        .map_err(|e| format!("Can't use that folder — {e}"))?;
    if !std::path::Path::new(&expanded).is_dir() {
        return Err("That path is not a folder.".to_string());
    }
    Ok(expanded)
}

impl Config {
    pub fn load() -> Self {
        let path = dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join(".grabbox")
            .join("config.json");
        let mut data = default_map();
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(Value::Object(stored)) = serde_json::from_str::<Value>(&text) {
                for key in data.clone().keys() {
                    if let Some(v) = stored.get(key) {
                        data.insert(key.clone(), v.clone());
                    }
                }
            }
        }
        Config {
            path,
            data: Mutex::new(data),
        }
    }

    fn save(&self) {
        let data = self.data.lock().unwrap();
        if let Some(parent) = self.path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(text) = serde_json::to_string_pretty(&*data) {
            let _ = fs::write(&self.path, text);
        }
    }

    pub fn get_string(&self, key: &str) -> String {
        self.data
            .lock()
            .unwrap()
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    }

    pub fn get_i64(&self, key: &str, fallback: i64) -> i64 {
        self.data
            .lock()
            .unwrap()
            .get(key)
            .and_then(Value::as_i64)
            .unwrap_or(fallback)
    }

    pub fn get_bool(&self, key: &str) -> bool {
        self.data
            .lock()
            .unwrap()
            .get(key)
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }

    /// Returns an error (and changes nothing) if a patch value is unusable.
    pub fn update(&self, patch: &Value) -> Result<(), String> {
        let mut patched = patch.clone();
        if let Some(raw) = patched.get("download_dir") {
            let text = raw.as_str().ok_or_else(|| "download_dir must be text".to_string())?;
            let norm = normalize_dir(text)?;
            patched["download_dir"] = json!(norm);
        }
        let mut data = self.data.lock().unwrap();
        if let Value::Object(map) = &patched {
            for (key, value) in map {
                if data.contains_key(key) {
                    data.insert(key.clone(), value.clone());
                }
            }
        }
        drop(data);
        self.save();
        Ok(())
    }

    pub fn to_json(&self) -> Value {
        Value::Object(self.data.lock().unwrap().clone())
    }

    pub fn download_dir(&self) -> String {
        let dir = self.get_string("download_dir");
        match normalize_dir(&dir) {
            Ok(d) => d,
            Err(_) => default_map()["download_dir"]
                .as_str()
                .unwrap_or(".")
                .to_string(),
        }
    }
}

fn default_map() -> Map<String, Value> {
    let default_dir = dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Downloads")
        .join("GrabBox")
        .to_string_lossy()
        .into_owned();
    let mut m = Map::new();
    m.insert("download_dir".into(), json!(default_dir));
    m.insert("concurrency".into(), json!(2));
    // The horse (shared with the Python core, same config file).
    m.insert("connections".into(), json!(8));
    m.insert("speed_limit_kbps".into(), json!(0));
    m.insert("turbo".into(), json!(true));
    m.insert("cookies_browser".into(), json!(""));
    m.insert("watch_clipboard".into(), json!(false));
    m.insert("port".into(), json!(8765));
    m.insert("open_browser".into(), json!(true));
    // Torrents (desktop only; the Python side ignores these keys).
    m.insert("torrent_download_kbps".into(), json!(0));  // 0 = unlimited
    m.insert("torrent_upload_kbps".into(), json!(0));    // 0 = unlimited
    m.insert("torrent_seed_when_done".into(), json!(false));
    m
}
