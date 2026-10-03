/*
 * GrabBox UI v2 — no frameworks, no build step.
 *
 * One UI, two transports ("driver layer"):
 *   · served by the Python server  -> HTTP fetch to /api/*
 *   · running inside Tauri         -> window.__TAURI__ invoke()
 * Both return the exact same shapes, so nothing below the driver cares.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const on = (el, ev, fn) => el.addEventListener(ev, fn);

/* ============================================================ driver === */

function httpDriver() {
  const get = (p) => fetch(p).then((r) => r.json());
  const post = (p, body) => fetch(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());
  return {
    name: "server",
    health: () => get("/api/health"),
    probe: (url, cookies) => post("/api/probe", { url, cookies }),
    download: (b) => post("/api/download", b),
    jobs: () => get("/api/jobs"),
    cancel: (id) => post(`/api/jobs/${id}/cancel`),
    clearJobs: () => post("/api/jobs/clear"),
    files: () => get("/api/files"),
    fileAction: (path, action) => post("/api/files/open", { path, action }),
    getConfig: () => get("/api/config"),
    setConfig: (cfg) => post("/api/config", cfg),
    clipboard: () => get("/api/clipboard"),
    updateYtdlp: () => post("/api/update-ytdlp"),
    pickFolder: null,               // browsers cannot pick folders
    openExtensionFolder: null,
  };
}

function tauriDriver() {
  const invoke = window.__TAURI__.core.invoke;
  return {
    name: "tauri",
    health: () => invoke("grabbox_health"),
    probe: (url, cookies) => invoke("grabbox_probe", { url, cookies: cookies || null }),
    download: (b) => invoke("grabbox_download", b),
    jobs: () => invoke("grabbox_jobs").then((jobs) => ({ jobs })),
    cancel: (id) => invoke("grabbox_cancel", { id }),
    clearJobs: () => invoke("grabbox_clear_jobs"),
    files: () => invoke("grabbox_files"),
    fileAction: (path, action) => invoke("grabbox_file_action", { path, action }),
    getConfig: () => invoke("grabbox_get_config"),
    setConfig: (cfg) => invoke("grabbox_set_config", { config: cfg }),
    clipboard: () => invoke("grabbox_clipboard"),
    updateYtdlp: () => invoke("grabbox_update_ytdlp"),
    pickFolder: () => invoke("grabbox_pick_folder"),
    openExtensionFolder: () => invoke("grabbox_open_extension_folder"),
    takePendingUrl: () => invoke("grabbox_take_pending_url"),
    onGrabUrl: (cb) => window.__TAURI__.event.listen("grab-url", (e) => cb(e.payload)),
  };
}

const driver = (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke)
  ? tauriDriver() : httpDriver();

/* ============================================================ theme ==== */
/* Material-adaptive: one accent seed -> full tonal palette -> CSS vars. */

const SEEDS = {
  blue: "#4f8cff", violet: "#8b5cf6", teal: "#14b8a6",
  green: "#3ecf8e", orange: "#f59e0b", pink: "#ec4899",
};
const THEME_KEY = "grabbox:theme";

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHex([r, g, b]) {
  const c = (v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0");
  return "#" + c(r) + c(g) + c(b);
}
function mix(a, b, t) {
  const A = hexToRgb(a), B = hexToRgb(b);
  return rgbToHex(A.map((v, i) => v + (B[i] - v) * t));
}
function hueShift(hex, deg) {
  const [r, g, b] = hexToRgb(hex).map((v) => v / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0; const l = (max + min) / 2, d = max - min;
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  h = (h + deg + 360) % 360;
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  let rgb = [0, 0, 0];
  if (h < 60) rgb = [c, x, 0]; else if (h < 120) rgb = [x, c, 0];
  else if (h < 180) rgb = [0, c, x]; else if (h < 240) rgb = [0, x, c];
  else if (h < 300) rgb = [x, 0, c]; else rgb = [c, 0, x];
  return rgbToHex(rgb.map((v) => (v + m) * 255));
}

let theme = { mode: "auto", seed: "blue" };
try { Object.assign(theme, JSON.parse(localStorage.getItem(THEME_KEY) || "{}")); } catch (e) {}

function applyTheme() {
  const seed = SEEDS[theme.seed] || SEEDS.blue;
  const dark = theme.mode === "dark" ||
    (theme.mode === "auto" && matchMedia("(prefers-color-scheme: light)").matches === false);
  const W = "#ffffff", K = "#000000";
  const v = dark ? {
    primary: mix(seed, W, 0.55), onPrimary: mix(seed, K, 0.85),
    primaryContainer: mix(seed, K, 0.72), onPrimaryContainer: mix(seed, W, 0.82),
    surface: mix("#101216", seed, 0.05), low: mix("#15171c", seed, 0.05),
    container: mix("#1a1d23", seed, 0.06), high: mix("#23262e", seed, 0.06),
    highest: mix("#2b2f38", seed, 0.06),
    onSurface: mix("#e4e6ec", seed, 0.03), onSurfaceVariant: mix("#9aa0ac", seed, 0.06),
    outline: mix("#3c4048", seed, 0.08), outlineVariant: mix("#2a2d34", seed, 0.06),
    error: "#f2b8b5",
  } : {
    primary: mix(seed, K, 0.22), onPrimary: W,
    primaryContainer: mix(seed, W, 0.80), onPrimaryContainer: mix(seed, K, 0.80),
    surface: mix("#f7f8fc", seed, 0.04), low: mix("#f1f2f8", seed, 0.05),
    container: mix("#e9ebf2", seed, 0.05), high: mix("#e1e3ec", seed, 0.06),
    highest: mix("#d8dae4", seed, 0.06),
    onSurface: "#1a1c21", onSurfaceVariant: "#5a5f6a",
    outline: "#7a7f8a", outlineVariant: "#d4d6df",
    error: "#b3261e",
  };
  const root = document.documentElement.style;
  const set = (k, val) => root.setProperty(k, val);
  set("--sc-primary", v.primary); set("--sc-on-primary", v.onPrimary);
  set("--sc-primary-container", v.primaryContainer);
  set("--sc-on-primary-container", v.onPrimaryContainer);
  set("--sc-surface", v.surface); set("--sc-surface-low", v.low);
  set("--sc-surface-container", v.container); set("--sc-surface-high", v.high);
  set("--sc-surface-highest", v.highest);
  set("--sc-on-surface", v.onSurface); set("--sc-on-surface-variant", v.onSurfaceVariant);
  set("--sc-outline", v.outline); set("--sc-outline-variant", v.outlineVariant);
  set("--sc-error", v.error);
  set("--sc-grad", `linear-gradient(140deg, ${hueShift(seed, -14)}, ${hueShift(seed, 26)})`);
  document.body.classList.toggle("light", !dark);
  let meta = document.querySelector("meta[name=theme-color]");
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = v.surface;
  renderThemeDialog();
}

function renderThemeDialog() {
  const modes = $("#tModes");
  if (!modes) return;
  modes.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("on", b.dataset.mode === theme.mode));
  const box = $("#tSeeds");
  box.innerHTML = Object.entries(SEEDS).map(([name, hex]) =>
    `<button class="swatch${name === theme.seed ? " on" : ""}" data-seed="${name}"
       style="background:${hex}" title="${name}"><svg><use href="#i-check"/></svg></button>`
  ).join("");
  box.querySelectorAll(".swatch").forEach((b) => on(b, "click", () => {
    theme.seed = b.dataset.seed;
    localStorage.setItem(THEME_KEY, JSON.stringify(theme));
    applyTheme();
  }));
}

/* ============================================================ state ==== */

const KIND_LABEL = {
  video: "Video", audio: "Audio", image: "Image", app: "Software",
  archive: "Archive", document: "Document", other: "File", file: "File",
};
const QUALITIES = {
  video: [
    ["best", "Best", "auto"], ["2160", "4K", "2160p"], ["1440", "2K", "1440p"],
    ["1080", "1080p", "Full HD"], ["720", "720p", "HD"],
    ["480", "480p", "SD"], ["360", "360p", "small"],
  ],
  audio: [
    ["m4a", "M4A", "plays everywhere"], ["mp3", "MP3", "most compatible"],
    ["opus", "Opus", "best size/quality"], ["flac", "FLAC", "lossless · big"],
    ["best", "Original", "no re-encode"],
  ],
  file: [["original", "Original", "exactly as served"]],
};

let current = null;          // last probe result
let chosenKind = "file";
let chosenQuality = "best";
let health = null;
let config = {};

/* ------------------------------------------------------------- health --- */

async function loadHealth() {
  try { health = await driver.health(); }
  catch (e) {
    setPill("server unreachable", "bad");
    return;
  }
  const p = health.problems || [];
  if (p.includes("ytdlp-missing")) setPill("engine missing", "bad");
  else if (p.includes("ytdlp-stale")) setPill(`yt-dlp ${health.ytdlp} · update it`, "warn");
  else if (p.length) setPill(`yt-dlp ${health.ytdlp} · needs setup`, "warn");
  else setPill(`yt-dlp ${health.ytdlp} · ready`, "ok");

  const banner = $("#setupBanner");
  if (p.includes("ytdlp-missing")) {
    $("#setupText").textContent = "yt-dlp is missing — almost nothing will download without it.";
    banner.classList.remove("hidden");
  } else if (p.includes("ytdlp-stale")) {
    $("#setupText").textContent = `yt-dlp is ${health.ytdlp_age ?? "many"} days old — that is the #1 cause of failed downloads.`;
    banner.classList.remove("hidden");
  } else if (p.includes("ffmpeg-missing")) {
    $("#setupText").textContent = "ffmpeg is missing — needed for mp3 conversion and merging video+audio.";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

function setPill(text, cls) {
  const pill = $("#health");
  pill.textContent = text;
  pill.className = "pill " + cls;
  if (!health) return;
  pill.title = [
    `yt-dlp ${health.ytdlp || "?"} (${health.ytdlp_age ?? "?"} days old)`,
    `ffmpeg: ${health.ffmpeg || "MISSING"}`,
    `JS runtime: ${health.js_runtime || "MISSING"} ${health.js_path || ""}`.trim(),
    `Downloads: ${health.download_dir}`,
    health.update_hint,
  ].join("\n");
}

/* -------------------------------------------------------------- probe --- */

async function analyze(url) {
  url = (url || "").trim();
  if (!url) return toast("Paste a link first");

  $("#result").classList.remove("hidden");
  $("#result").scrollIntoView({ behavior: "smooth", block: "nearest" });
  $("#rBadge").textContent = "Analyzing";
  $("#rTitle").textContent = "Reading that link…";
  $("#rSub").textContent = url;
  $("#rThumb").removeAttribute("src");
  $("#rKinds").innerHTML = "";
  $("#rQuality").innerHTML = "";
  $("#rWarn").classList.add("hidden");
  $("#rOkWrap").classList.add("hidden");
  $("#rErrWrap").classList.add("hidden");
  $("#rPlaylist").classList.add("hidden");
  $("#rDownload").disabled = true;
  $("#rSize").textContent = "";

  let res;
  try { res = await driver.probe(url); }
  catch (e) { res = { ok: false, url, error: String(e) }; }
  current = res;

  if (!res.ok) {
    $("#rBadge").textContent = "Problem";
    $("#rTitle").textContent = "Could not read that link";
    $("#rSub").textContent = "";
    $("#rErr").textContent = res.error || "unknown error";
    $("#rErrHint").textContent = res.hint ? "💡 " + res.hint : "";
    $("#rErrHint").classList.toggle("hidden", !res.hint);
    $("#rErrWrap").classList.remove("hidden");
    return;
  }

  chosenKind = res.kind || "file";
  $("#rBadge").textContent = (KIND_LABEL[chosenKind] || "File") + (res.is_playlist ? " · playlist" : "");
  $("#rTitle").textContent = res.title || url;
  const bits = [];
  if (res.uploader) bits.push(res.uploader);
  if (res.duration) bits.push(fmtDuration(res.duration));
  if (res.direct) bits.push(`direct ${res.direct.kind} · ${fmtBytes(res.direct.size) || "size ?"}`);
  if (res.is_playlist) bits.push(`${(res.entries || []).length}+ items`);
  $("#rSub").textContent = bits.join("  ·  ") || url;
  if (res.thumb) $("#rThumb").src = res.thumb;

  if (res.note) {
    $("#rWarn").textContent = "⚠ " + res.note;
    $("#rWarn").classList.remove("hidden");
  }

  buildKindTabs(res);
  $("#rName").value = "";
  $("#rName").placeholder = cleanName((res.direct && res.direct.filename) || res.title || "");
  $("#rDownload").disabled = false;
  $("#rOkWrap").classList.remove("hidden");

  if (res.is_playlist) {
    const list = (res.entries || []).slice(0, 25);
    $("#rPlaylistList").innerHTML = list.map((e, i) =>
      `<li><span class="idx">${String(i + 1).padStart(2, "0")}</span><span>${escapeHtml(e.title || e.url || "")}</span></li>`
    ).join("");
    $("#rPlaylist").classList.remove("hidden");
  }
}

function buildKindTabs(res) {
  const set = new Set();
  set.add(res.kind || "file");
  const hasVideo = (res.formats || []).some((f) => f.kind === "video");
  const hasAudio = (res.formats || []).some((f) => f.kind === "audio");
  if (hasVideo) set.add("video");
  if (hasAudio) set.add("audio");
  if (res.direct) set.add(res.direct.kind);
  if (!set.size) set.add("file");
  const kinds = [...set].slice(0, 4);
  $("#rKinds").innerHTML = kinds.map((k) =>
    `<button data-kind="${k}" class="${k === chosenKind ? "on" : ""}">${KIND_LABEL[k] || k}</button>`
  ).join("");
  $("#rKinds").querySelectorAll("button").forEach((b) => on(b, "click", () => {
    chosenKind = b.dataset.kind;
    $("#rKinds").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    buildQualityChips();
  }));
  buildQualityChips();
}

function chipGroup(kind) {
  return (kind === "video" || kind === "audio") ? kind : "file";
}

function buildQualityChips() {
  const group = chipGroup(chosenKind);
  let opts = QUALITIES[group];
  if (group === "video") {
    const heights = (current.formats || []).filter((f) => f.kind === "video" && f.height).map((f) => f.height);
    if (heights.length) {
      const maxH = Math.max(...heights);
      opts = opts.filter(([v]) => v === "best" || parseInt(v, 10) <= maxH * 1.02);
    }
  }
  if (!opts.some(([v]) => v === chosenQuality)) chosenQuality = opts[0][0];
  $("#rQuality").innerHTML = opts.map(([v, label, sub]) => {
    const est = estimateSize(chosenKind, v);
    return `<button class="qchip${v === chosenQuality ? " on" : ""}" data-q="${v}">
      <span class="q-label">${label}</span>
      <span class="q-sub">${sub}${est ? " · ~" + fmtBytes(est) : ""}</span>
    </button>`;
  }).join("");
  $("#rQuality").querySelectorAll(".qchip").forEach((c) => on(c, "click", () => {
    chosenQuality = c.dataset.q;
    $("#rQuality").querySelectorAll(".qchip").forEach((x) => x.classList.toggle("on", x === c));
    updateSizeHint();
  }));
  updateSizeHint();
}

/* Best-effort size estimate from probed formats. */
function estimateSize(kind, q) {
  if (!current || !current.formats || !current.duration) {
    if (current && current.direct && current.direct.size) return current.direct.size;
    return null;
  }
  const dur = current.duration;
  const fmts = current.formats;
  const vids = fmts.filter((f) => f.kind === "video");
  const auds = fmts.filter((f) => f.kind === "audio");
  const sizeOf = (f) => f.size || (f.tbr ? (f.tbr * 1000 / 8) * dur : null);
  if (currKindIsAudio(kind)) {
    const a = auds[auds.length - 1];
    return a ? sizeOf(a) : null;
  }
  if (kind !== "video") return null;
  let pool = vids;
  if (q !== "best") {
    const h = parseInt(q, 10);
    const under = vids.filter((f) => f.height && f.height <= h);
    if (under.length) pool = under;
  }
  const bestV = pool[pool.length - 1];
  const bestA = auds[auds.length - 1];
  const vs = bestV ? sizeOf(bestV) : null, as = bestA ? sizeOf(bestA) : null;
  return vs || as ? (vs || 0) + (as || 0) : null;
}
function currKindIsAudio(k) { return k === "audio"; }

function updateSizeHint() {
  const est = estimateSize(chosenKind, chosenQuality);
  const dir = (config.download_dir || "").split(/[\\/]/).filter(Boolean).pop();
  $("#rSize").textContent = est ? `~${fmtBytes(est)}${dir ? " → " + dir : ""}` : (dir ? `saving to ${dir}` : "");
}

/* ----------------------------------------------------------- download --- */

async function startDownload() {
  if (!current || !current.url) return;
  maybeAskNotify();
  const body = {
    url: current.url,
    kind: chosenKind,
    quality: chosenQuality,
    playlist: !!current.is_playlist,
    filename: $("#rName").value.trim() || null,
  };
  if ($("#rCookies").checked) body.cookies = config.cookies_browser || "chrome";
  let res;
  try { res = await driver.download(body); }
  catch (e) { return toast(String(e)); }
  if (res.job) {
    toast(`Queued · ${KIND_LABEL[chosenKind] || chosenKind}`);
    $("#result").classList.add("hidden");
    $("#url").value = "";
    refreshJobs(true);
  } else {
    toast(res.error || "could not start the download");
  }
}

/* --------------------------------------------------------------- jobs --- */

let lastJobState = "";
let jobsScheduled = false;
const notified = new Set();

async function refreshJobs(force) {
  // Coalesce rapid calls into one animation-frame paint to keep 60fps even
  // when many jobs update at once or the poll fires while a render is pending.
  if (!force && jobsScheduled) return;
  jobsScheduled = true;
  await new Promise((r) => requestAnimationFrame(r));
  jobsScheduled = false;
  let data;
  try { data = await driver.jobs(); } catch (e) { return; }
  const jobs = (data.jobs || []).slice().reverse();
  const key = JSON.stringify(jobs.map((j) => [j.id, j.status, Math.round(j.percent*10)/10, j.filename, j.attempt, j.items_done, j.items_total]));
  if (!force && key === lastJobState) return;
  const hadRunning = lastJobState.includes('"running"');
  lastJobState = key;

  const box = $("#jobs");
  // Preserve focus if a button inside the box is currently active, so cancel
  // clicks don't get eaten by a re-render mid-click.
  const activeId = document.activeElement && document.activeElement.dataset
    && document.activeElement.dataset.cancel;
  if (!jobs.length) {
    box.innerHTML = `<div class="empty">Nothing downloading yet.<br>Paste a link above — it shows up here with live progress.</div>`;
  } else {
    box.innerHTML = jobs.map(jobCard).join("");
    box.querySelectorAll("[data-cancel]").forEach((b) =>
      on(b, "click", async () => { await driver.cancel(b.dataset.cancel); refreshJobs(true); }));
    box.querySelectorAll("[data-show]").forEach((b) =>
      on(b, "click", () => driver.fileAction(b.dataset.show, "reveal")));
    box.querySelectorAll("[data-play]").forEach((b) =>
      on(b, "click", () => driver.fileAction(b.dataset.play, "open")));
    if (activeId) {
      const btn = box.querySelector(`[data-cancel="${activeId}"]`);
      if (btn) btn.focus();
    }
  }
  jobs.forEach((j) => {
    if (j.status === "done" && !notified.has(j.id)) {
      notified.add(j.id);
      notifyDone(j);
    }
  });
  if (hadRunning && !jobs.some((j) => j.status === "running")) loadFiles();
}

function jobCard(j) {
  const cls = j.status === "done" ? "done" : (j.status === "error" || j.status === "canceled") ? "error" : "";
  const pct = j.percent ? `${Math.max(0, Math.min(100, j.percent)).toFixed(0)}%` : "";
  const parts = [];
  if (j.speed) parts.push(`${fmtBytes(j.speed)}/s`);
  if (j.eta) parts.push(`${Math.round(j.eta)}s left`);
  if (j.items_total) parts.push(`item ${j.items_done || 1}/${j.items_total}`);
  if (j.attempts_total > 1) parts.push(`attempt ${j.attempt}/${j.attempts_total}`);
  const w = Math.max(0, Math.min(100, j.percent || 0));
  return `
  <div class="job ${cls}">
    <div class="job-top">
      <div class="job-title" title="${escapeAttr(j.filename || j.title || j.url)}">${escapeHtml(j.filename || j.title || j.url)}</div>
      <div class="job-actions">
        ${j.status === "running" || j.status === "queued" ? `<button class="icon-btn" data-cancel="${j.id}" title="Stop" aria-label="Stop"><svg><use href="#i-close"/></svg></button>` : ""}
        ${j.status === "done" && j.path ? `<button class="icon-btn" data-play="${escapeAttr(j.path)}" title="Play / open" aria-label="Open"><svg><use href="#i-play"/></svg></button>
          <button class="icon-btn" data-show="${escapeAttr(j.path)}" title="Show in folder" aria-label="Show in folder"><svg><use href="#i-folder"/></svg></button>` : ""}
      </div>
    </div>
    <div class="job-sub"><span class="job-status">${j.status}</span>${pct ? " · " + pct : ""}${parts.length ? " · " + parts.join(" · ") : ""}</div>
    <div class="bar"><i style="width:${w}%"></i></div>
    ${j.status === "error" && j.error ? `<div class="err">${escapeHtml(j.error)}</div>` : ""}
    ${j.hint ? `<div class="hint">💡 ${escapeHtml(j.hint)}</div>` : ""}
  </div>`;
}

/* --------------------------------------------------------------- files -- */

async function loadFiles() {
  let data;
  try { data = await driver.files(); } catch (e) { return; }
  const box = $("#files");
  const list = data.files || [];
  const dir = data.dir || "";
  $("#dirName").textContent = dir.split(/[\\/]/).filter(Boolean).pop() || dir || "—";
  $("#dirChip").title = dir;
  if (!list.length) {
    box.innerHTML = `<div class="empty">Nothing here yet — finished files land in<br><b>${escapeHtml(dir)}</b></div>`;
    return;
  }
  box.innerHTML = list.map((f) => `
    <div class="file">
      <span class="ico">${f.icon}</span>
      <div class="meta">
        <div class="name" title="${escapeAttr(f.path)}">${escapeHtml(f.name)}</div>
        <div class="sub">${f.size_h} · ${new Date(f.mtime * 1000).toLocaleString()}</div>
      </div>
      <div class="acts">
        <button class="icon-btn" data-open="${escapeAttr(f.path)}" title="Open"><svg><use href="#i-play"/></svg></button>
        <button class="icon-btn" data-reveal="${escapeAttr(f.path)}" title="Show in folder"><svg><use href="#i-folder"/></svg></button>
        <button class="icon-btn danger" data-del="${escapeAttr(f.path)}" title="Delete"><svg><use href="#i-trash"/></svg></button>
      </div>
    </div>`).join("");
  box.querySelectorAll("[data-open]").forEach((b) =>
    on(b, "click", () => driver.fileAction(b.dataset.open, "open")));
  box.querySelectorAll("[data-reveal]").forEach((b) =>
    on(b, "click", () => driver.fileAction(b.dataset.reveal, "reveal")));
  box.querySelectorAll("[data-del]").forEach((b) =>
    on(b, "click", async () => {
      if (!confirm("Delete this file?")) return;
      await driver.fileAction(b.dataset.del, "delete");
      loadFiles();
    }));
  loadConfigOnce();
}

let configLoaded = false;
async function loadConfigOnce() {
  if (configLoaded) return;
  configLoaded = true;
  try { config = await driver.getConfig(); } catch (e) {}
}

/* ------------------------------------------------------------ settings -- */

async function openSettings() {
  config = await driver.getConfig();
  $("#sDir").value = config.download_dir || "";
  $("#sConc").value = String(config.concurrency || 2);
  $("#sCookies").value = config.cookies_browser || "";
  $("#sClip").checked = !!config.watch_clipboard;
  if (driver.pickFolder) $("#sBrowse").classList.remove("hidden");
  const h = await driver.health();
  $("#sHealth").textContent = [
    `yt-dlp      ${h.ytdlp || "MISSING"}  (${h.ytdlp_age ?? "?"} days old)`,
    `ffmpeg      ${h.ffmpeg || "MISSING"}`,
    `JS runtime  ${h.js_runtime || "MISSING"} ${h.js_path || ""}`.trim(),
    `folder      ${h.download_dir}`,
    ``,
    h.pot_provider ? `POT plugin  ${h.pot_provider}` : null,
    `update      ${h.update_hint}`,
  ].filter((l) => l !== null).join("\n");
  openDlg("settings");
}

async function saveSettings() {
  await driver.setConfig({
    download_dir: $("#sDir").value.trim(),
    concurrency: Math.max(1, parseInt($("#sConc").value || "2", 10)),
    cookies_browser: $("#sCookies").value,
    watch_clipboard: $("#sClip").checked,
  });
  closeDlg("settings");
  toast("Settings saved");
  loadHealth();
  loadFiles();
}

/* ----------------------------------------------------- extension guide -- */

const EXT_STEPS = {
  chrome: [
    `Open <code>chrome://extensions</code> (Edge: <code>edge://extensions</code>, Brave: <code>brave://extensions</code>).`,
    `Turn on <b>Developer mode</b> (top right).`,
    `Click <b>Load unpacked</b> and choose the <b>extension</b> folder from your GrabBox download/repo.`,
    `Keep the GrabBox app running — that is where downloads happen.`,
  ],
  firefox: [
    `Open <code>about:debugging#/runtime/this-firefox</code>.`,
    `Click <b>Load Temporary Add-on…</b> and pick <b>manifest.json</b> inside the <b>extension</b> folder.`,
    `Firefox unloads temporary add-ons on restart — reloading takes one click.`,
    `Keep the GrabBox app running — that is where downloads happen.`,
  ],
};

function openExtensionGuide() {
  const tabs = $("#extBrowserTabs");
  const paint = (which) => {
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.b === which));
    $("#extSteps").innerHTML = EXT_STEPS[which].map((s) => `<li>${s}</li>`).join("");
  };
  tabs.querySelectorAll("button").forEach((b) => on(b, "click", () => paint(b.dataset.b)));
  paint("chrome");
  if (driver.openExtensionFolder) $("#extOpenFolder").classList.remove("hidden");
  openDlg("extDlg");
}

/* ----------------------------------------------------------- clipboard -- */

async function pollClipboard() {
  let d;
  try { d = await driver.clipboard(); } catch (e) { return; }
  if (d.item && d.item.url) {
    const u = d.item.url;
    $("#clipUrl").textContent = u.length > 56 ? u.slice(0, 53) + "…" : u;
    const hint = $("#clipHint");
    hint.classList.remove("hidden");
    hint.dataset.url = u;
  }
}

/* ------------------------------------------------------- notifications -- */

function maybeAskNotify() {
  try {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  } catch (e) {}
}
function notifyDone(job) {
  try {
    toast(`Done · ${job.filename || job.title}`);
    if (driver.name === "tauri") return; // Rust sends the OS notification
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    if (!document.hidden) return;
    new Notification("GrabBox — download complete", { body: job.filename || job.title || "" });
  } catch (e) {}
}

/* ------------------------------------------------------------- dialogs -- */

function openDlg(id) { $("#" + id).classList.remove("hidden"); }
function closeDlg(id) { $("#" + id).classList.add("hidden"); }

/* ------------------------------------------------------------- helpers -- */

function fmtBytes(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtDuration(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
function cleanName(s) {
  return (s || "").replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim();
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }

/* ------------------------------------------------------------ feedback -- */

const FEEDBACK_EMAIL = "feedit18@gmail.com";

function sendFeedback() {
  const h = health || {};
  const subject = `GrabBox feedback (app ${h.app || "?"})`;
  const body = [
    "Hi! GrabBox feedback:",
    "",
    "",
    "",
    "----",
    `(auto-included: app ${h.app || "?"} · ${h.platform || "?"} · yt-dlp ${h.ytdlp || "?"} · js ${h.js_runtime || "?"})`,
  ].join("\n");
  const href = `mailto:${FEEDBACK_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  const tauri = window.__TAURI__;
  if (tauri && tauri.opener && typeof tauri.opener.openUrl === "function") {
    tauri.opener.openUrl(href).catch(() => { location.href = href; });
  } else {
    location.href = href;
  }
  toast("Opening your mail app…");
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

/* --------------------------------------------------------- drag & drop -- */

function setupDragDrop() {
  let depth = 0;
  const overlay = $("#dropOverlay");
  window.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer ? e.dataTransfer.types : [])].some((t) => t.includes("text"))) return;
    depth++;
    overlay.classList.remove("hidden");
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.classList.add("hidden");
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    overlay.classList.add("hidden");
    const text = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "";
    const url = text.split("\n").map((l) => l.trim()).find((l) => /^https?:\/\//.test(l));
    if (url) {
      $("#url").value = url;
      analyze(url);
    } else {
      toast("That did not look like a link");
    }
  });
}

/* ---------------------------------------------------------------- init -- */

document.addEventListener("DOMContentLoaded", () => {
  applyTheme();
  matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", applyTheme);

  loadHealth();
  loadFiles();
  refreshJobs(true);
  setInterval(refreshJobs, 1000);
  setInterval(loadFiles, 5000);
  setInterval(pollClipboard, 1500);

  on($("#go"), "click", () => analyze($("#url").value));
  on($("#url"), "keydown", (e) => { if (e.key === "Enter") analyze($("#url").value); });
  on($("#url"), "paste", () => setTimeout(() => analyze($("#url").value), 60));
  on($("#rDownload"), "click", startDownload);
  on($("#rClose"), "click", () => $("#result").classList.add("hidden"));
  on($("#clearJobs"), "click", async () => { await driver.clearJobs(); lastJobState = ""; refreshJobs(true); });
  on($("#openDir"), "click", () => driver.fileAction(null, "reveal"));
  on($("#settingsBtn"), "click", openSettings);
  on($("#health"), "click", openSettings);
  on($("#setupGo"), "click", openSettings);
  on($("#sSave"), "click", saveSettings);
  on($("#themeBtn"), "click", () => { renderThemeDialog(); openDlg("themeDlg"); });
  on($("#feedbackBtn"), "click", sendFeedback);
  on($("#sFeedback"), "click", sendFeedback);
  on($("#sExtension"), "click", openExtensionGuide);
  on($("#sUpdate"), "click", async () => {
    const r = await driver.updateYtdlp();
    toast(r.hint ? "Run: " + r.hint : (r.ok ? "Updated" : "Update failed"));
    loadHealth();
  });
  on($("#extOpenFolder"), "click", () => driver.openExtensionFolder && driver.openExtensionFolder());
  on($("#sBrowse"), "click", async () => {
    if (!driver.pickFolder) return;
    const dir = await driver.pickFolder();
    if (dir) $("#sDir").value = dir;
  });
  on($("#clipHint"), "click", () => {
    const u = $("#clipHint").dataset.url;
    if (!u) return;
    $("#clipHint").classList.add("hidden");
    $("#url").value = u;
    analyze(u);
  });
  on($("#tModes"), "click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    theme.mode = b.dataset.mode;
    localStorage.setItem(THEME_KEY, JSON.stringify(theme));
    applyTheme();
  });

  document.querySelectorAll(".scrim").forEach((scrim) =>
    on(scrim, "click", (e) => { if (e.target === scrim) scrim.classList.add("hidden"); }));
  document.querySelectorAll("[data-close]").forEach((b) =>
    on(b, "click", () => closeDlg(b.dataset.close)));
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".scrim").forEach((s) => s.classList.add("hidden"));
  });

  setupDragDrop();

  // The browser extension opens /?url=... — keep that entry point working.
  const q = new URLSearchParams(location.search);
  const urlParam = q.get("url") || q.get("u");
  if (urlParam) {
    $("#url").value = urlParam;
    analyze(urlParam);
    history.replaceState({}, "", location.pathname);
  }

  // Tauri: links forwarded into a running app (CLI arg / second instance).
  if (driver.takePendingUrl) {
    driver.takePendingUrl().then((r) => {
      const u = r && r.url;
      if (u) { $("#url").value = u; analyze(u); }
    }).catch(() => {});
  }
  if (driver.onGrabUrl) {
    driver.onGrabUrl((u) => {
      if (u && typeof u === "string") { $("#url").value = u; analyze(u); }
    });
  }
});
