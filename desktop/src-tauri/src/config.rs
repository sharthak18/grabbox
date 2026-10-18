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

    pub fn update(&self, patch: &Value) {
        let mut data = self.data.lock().unwrap();
        if let Value::Object(map) = patch {
            for (key, value) in map {
                if data.contains_key(key) {
                    data.insert(key.clone(), value.clone());
                }
            }
        }
        drop(data);
        self.save();
    }

    pub fn to_json(&self) -> Value {
        Value::Object(self.data.lock().unwrap().clone())
    }

    pub fn download_dir(&self) -> String {
        let mut dir = self.get_string("download_dir");
        if dir.is_empty() {
            dir = default_map()["download_dir"]
                .as_str()
                .unwrap_or("")
                .to_string();
        }
        if let Some(rest) = dir.strip_prefix("~/") {
            if let Some(home) = dirs::home_dir() {
                dir = home.join(rest).to_string_lossy().into_owned();
            }
        }
        let _ = fs::create_dir_all(&dir);
        dir
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
    m
}
