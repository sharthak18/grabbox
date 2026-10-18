//! GrabBox desktop — Tauri shell.
//!
//! One Rust backend driving the *standalone* yt-dlp / ffmpeg / deno binaries
//! shipped as sidecars (yt-dlp's official builds embed Python, so there is
//! nothing for the user to install). The web UI in `grabbox/web` talks to the
//! commands below through `window.__TAURI__.core.invoke` — the exact same
//! shapes the Python server returns over HTTP, so one UI serves both.

mod config;
mod engine;
mod fsys;
mod jobs;

use config::Config;
use engine::Engine;
use jobs::Manager;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use tauri::{Emitter, Manager as _};

pub struct AppState {
    engine: Engine,
    config: Arc<Config>,
    manager: Manager,
    app: tauri::AppHandle,
    /// A URL handed to a *second* instance (`grabbox <url>` or the extension),
    /// waiting for the window to pick it up.
    pending_url: Arc<Mutex<Option<String>>>,
}

// ---------------------------------------------------------------- commands

#[tauri::command]
async fn grabbox_health(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let engine = state.engine.clone();
    let cfg = state.config.clone();
    let app = state.app.clone();
    tauri::async_runtime::spawn_blocking(move || engine.health(&cfg, &app))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn grabbox_probe(
    state: tauri::State<'_, AppState>,
    url: String,
    cookies: Option<String>,
) -> Result<Value, String> {
    let engine = state.engine.clone();
    tauri::async_runtime::spawn_blocking(move || engine.probe(&url, cookies.as_deref()))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e)
}

#[tauri::command]
async fn grabbox_download(
    state: tauri::State<'_, AppState>,
    url: String,
    kind: Option<String>,
    quality: Option<String>,
    playlist: Option<bool>,
    dir: Option<String>,
    cookies: Option<String>,
    filename: Option<String>,
) -> Result<Value, String> {
    let engine = state.engine.clone();
    let cfg = state.config.clone();
    let manager = state.manager.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let job = manager.add(
            engine,
            cfg,
            &url,
            kind.as_deref().unwrap_or("file"),
            quality.as_deref(),
            playlist.unwrap_or(false),
            dir.as_deref(),
            cookies.as_deref(),
            filename.as_deref(),
        );
        Ok::<Value, String>(json!({ "job": job.snapshot() }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn grabbox_jobs(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    Ok(json!(state.manager.list()))
}

#[tauri::command]
fn grabbox_cancel(state: tauri::State<'_, AppState>, id: String) -> Result<Value, String> {
    Ok(json!({ "ok": state.manager.cancel(&id) }))
}

#[tauri::command]
fn grabbox_pause(state: tauri::State<'_, AppState>, id: String) -> Result<Value, String> {
    Ok(json!({ "ok": state.manager.pause(&id) }))
}

#[tauri::command]
async fn grabbox_resume(
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<Value, String> {
    let engine = state.engine.clone();
    let cfg = state.config.clone();
    let manager = state.manager.clone();
    tauri::async_runtime::spawn_blocking(move || {
        Ok::<Value, String>(json!({ "ok": manager.resume(engine, cfg, &id) }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn grabbox_download_batch(
    state: tauri::State<'_, AppState>,
    urls: Vec<String>,
) -> Result<Value, String> {
    let engine = state.engine.clone();
    let cfg = state.config.clone();
    let manager = state.manager.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut jobs = Vec::new();
        for url in &urls {
            let url = url.trim();
            if url.is_empty() {
                continue;
            }
            let job = manager.add(
                engine.clone(),
                cfg.clone(),
                url,
                "file",
                None,
                false,
                None,
                None,
                None,
            );
            jobs.push(job.snapshot());
        }
        Ok::<Value, String>(json!({ "jobs": jobs }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn grabbox_clear_jobs(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    state.manager.clear_finished();
    Ok(json!({ "ok": true }))
}

#[tauri::command]
async fn grabbox_files(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let cfg = state.config.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let dir = cfg.download_dir();
        Ok::<Value, String>(json!({ "dir": dir, "files": fsys::list_files(&dir) }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn grabbox_file_action(
    state: tauri::State<'_, AppState>,
    path: Option<String>,
    action: Option<String>,
) -> Result<Value, String> {
    let root = state.config.download_dir();
    let target = path.unwrap_or_else(|| root.clone());
    let action = action.unwrap_or_else(|| "reveal".to_string());
    let ok = match action.as_str() {
        "delete" => fsys::delete(&target, &root),
        "open" => fsys::open_file(&state.app, &target),
        _ => fsys::reveal(&state.app, &target),
    };
    Ok(json!({ "ok": ok }))
}

#[tauri::command]
fn grabbox_get_config(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    Ok(state.config.to_json())
}

#[tauri::command]
fn grabbox_set_config(state: tauri::State<'_, AppState>, config: Value) -> Result<Value, String> {
    state.config.update(&config);
    Ok(state.config.to_json())
}

#[tauri::command]
fn grabbox_clipboard(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    let watching = state.config.get_bool("watch_clipboard");
    let mut item: Value = Value::Null;
    if watching {
        if let Ok(text) = state.app.clipboard().read_text() {
            if let Some(url) = engine::first_url(&text) {
                let mut last = state.engine.last_clip.lock().unwrap();
                if last.as_deref() != Some(url.as_str()) {
                    *last = Some(url.clone());
                    item = json!({ "url": url });
                }
            }
        }
    }
    Ok(json!({ "item": item, "watching": watching }))
}

#[tauri::command]
async fn grabbox_update_ytdlp(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let engine = state.engine.clone();
    tauri::async_runtime::spawn_blocking(move || engine.update_ytdlp())
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn grabbox_pick_folder(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    use tauri_plugin_dialog::DialogExt;
    let picked = state
        .app
        .dialog()
        .file()
        .set_title("Choose where downloads go")
        .blocking_pick_folder();
    match picked {
        Some(p) => Ok(Value::String(p.to_string())),
        None => Ok(Value::Null),
    }
}

#[tauri::command]
fn grabbox_open_extension_folder(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    Ok(json!({ "ok": fsys::open_extension_folder(&state.app) }))
}

/// The UI polls this once at startup (and listens for `grab-url` events after).
#[tauri::command]
fn grabbox_take_pending_url(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    let url = state.pending_url.lock().unwrap().take();
    match url {
        Some(u) => Ok(json!({ "url": u })),
        None => Ok(json!({ "url": Value::Null })),
    }
}

// ------------------------------------------------------------- tray etc. --

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

fn setup_tray(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, TrayIconBuilder, TrayIconEvent};

    let show = MenuItem::with_id(app, "show", "Show GrabBox", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    let mut tray = TrayIconBuilder::with_id("main-tray").menu(&menu);
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.on_menu_event(|app, event| match event.id.as_ref() {
        "show" => show_main_window(app),
        "quit" => app.exit(0),
        _ => {}
    })
    .on_tray_icon_event(|tray, event| {
        if let TrayIconEvent::DoubleClick {
            button: MouseButton::Left,
            ..
        } = event
        {
            show_main_window(tray.app_handle());
        }
    })
    .build(app)?;
    Ok(())
}

// ------------------------------------------------------------------- main

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Second launch: forward the URL (if any) to the running window.
            show_main_window(app);
            if let Some(url) = args.iter().find_map(|a| engine::first_url_arg(a)) {
                if let Some(state) = app.try_state::<AppState>() {
                    *state.pending_url.lock().unwrap() = Some(url.clone());
                }
                let _ = app.emit("grab-url", url);
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            let engine = Engine::new(app.handle());
            let config = Arc::new(Config::load());
            let manager = Manager::new(
                config.get_i64("concurrency", 2).max(1) as usize,
                Some(app.handle().clone()),
            );
            let pending_url = Arc::new(Mutex::new(
                std::env::args().find_map(|a| engine::first_url_arg(&a)),
            ));
            app.manage(AppState {
                engine,
                config,
                manager,
                app: app.handle().clone(),
                pending_url,
            });

            setup_tray(app.handle())?;

            // Close button hides to the tray instead of killing downloads.
            if let Some(win) = app.get_webview_window("main") {
                let hide_window = win.clone();
                win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        let _ = hide_window.hide();
                        api.prevent_close();
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            grabbox_health,
            grabbox_probe,
            grabbox_download,
            grabbox_jobs,
            grabbox_cancel,
            grabbox_pause,
            grabbox_resume,
            grabbox_download_batch,
            grabbox_clear_jobs,
            grabbox_files,
            grabbox_file_action,
            grabbox_get_config,
            grabbox_set_config,
            grabbox_clipboard,
            grabbox_update_ytdlp,
            grabbox_pick_folder,
            grabbox_open_extension_folder,
            grabbox_take_pending_url,
        ])
        .run(tauri::generate_context!())
        .expect("error while running GrabBox");
}

fn main() {
    run();
}
