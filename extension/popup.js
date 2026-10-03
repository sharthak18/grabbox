"use strict";

const $ = (s) => document.querySelector(s);
const ICON = { video:"🎬", audio:"🎵", image:"🖼️", file:"⬇️" };

let server = "http://127.0.0.1:8765";
let activeTabId = null;

/** Ask the page to show the Grab Dialog; fall back to sending to background. */
function openGrab(url) {
  if (!url) return;
  if (activeTabId != null) {
    chrome.tabs.sendMessage(activeTabId, { type:"open-dialog", url }, () => {
      if (chrome.runtime.lastError) chrome.runtime.sendMessage({ type:"grab", url });
      window.close();
    });
  } else {
    chrome.runtime.sendMessage({ type:"grab", url });
    window.close();
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

async function init() {
  const s = await chrome.runtime.sendMessage({ type:"get-server" });
  server = (s && s.server) || server;
  $("#server").value = server;

  const h = await chrome.runtime.sendMessage({ type:"health" });
  const pill = $("#health");
  if (h && h.ytdlp) {
    pill.textContent = "ready · yt-dlp " + h.ytdlp;
    pill.className = "pill ok";
  } else {
    pill.textContent = "app offline";
    pill.className = "pill bad";
  }

  const tabs = await chrome.tabs.query({ active:true, currentWindow:true });
  const tab = tabs[0];
  let tabId = tab ? tab.id : null;
  activeTabId = tabId;
  let items = [];
  let playingItem = null;
  if (tabId != null) {
    // Ask the content script for media items AND to identify anything playing.
    try {
      const res = await chrome.tabs.sendMessage(tabId, { type:"list" });
      items = (res && res.items) || [];
    } catch (e) { /* no content script on this page */ }
    // Detect playing video via chrome.tabs message (best-effort: same content
    // script). We infer playing by looking for items whose URL looks like video.
    if (items.length) playingItem = items.find((i)=>i.kind==="video") || items[0];
  }
  $("#none").style.display = items.length ? "none" : "block";

  // Big "Grab playing video" quick action — the thing users reach for when
  // there's no share button.
  if (playingItem) {
    const q = $("#quick");
    q.classList.remove("hidden");
    q.querySelector(".q-label").textContent = playingItem.label.length>50
      ? playingItem.label.slice(0,47)+"…" : playingItem.label;
    q.addEventListener("click", () => openGrab(playingItem.url));
  }

  $("#items").innerHTML = items.slice(0,30).map((it) =>
    `<div class="item"><span class="k">${ICON[it.kind]||"⬇️"}</span>`+
    `<span class="t" title="${esc(it.url)}">${esc(it.label||it.url)}</span>`+
    `<button data-url="${esc(it.url)}">grab</button></div>`).join("");
  $("#items").querySelectorAll("button[data-url]").forEach((b) =>
    b.addEventListener("click", () => openGrab(b.dataset.url)));

  $("#grab").addEventListener("click", () => {
    const u = $("#url").value.trim(); if (u) openGrab(u);
  });
  $("#url").addEventListener("keydown", (e) => {
    if (e.key==="Enter" && $("#url").value.trim()) openGrab($("#url").value.trim());
  });
  $("#open").addEventListener("click", () => {
    chrome.tabs.create({ url: server }); window.close();
  });
  $("#server").addEventListener("change", () => {
    const v = $("#server").value.trim().replace(/\/$/, "");
    if (v) { chrome.runtime.sendMessage({ type:"set-server", server:v }); server = v; }
  });

  // Focus the URL input so you can paste+enter instantly.
  setTimeout(() => $("#url").focus(), 50);
}

init();
