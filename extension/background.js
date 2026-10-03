/*
 * GrabBox background service worker (Manifest V3).
 *
 * - Toolbar icon stays FADED until a content script reports downloadable media,
 *   then it lights up and shows a count badge.
 * - Alt+G grabs the currently-playing video without leaving the page.
 * - Right-click "Grab with GrabBox" opens the in-page Grab Dialog. Pages where
 *   a content script cannot run fall back to opening the app.
 * - Proxies probe/download calls to the local app so the dialog never hits CORS.
 */
"use strict";

const API = chrome.runtime;
let serverUrl = "http://127.0.0.1:8765";

chrome.storage.local.get({ server: serverUrl }, (s) => {
  serverUrl = s.server;
});

const ICONS_OFF = { 16:"icons/icon-faded-16.png", 32:"icons/icon-faded-32.png", 48:"icons/icon-faded-48.png", 128:"icons/icon-faded-128.png" };
const ICONS_ON = { 16:"icons/icon-16.png", 32:"icons/icon-32.png", 48:"icons/icon-48.png", 128:"icons/icon-128.png" };

/* ------------------------------------------------------------- context menu */
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id:"grab-link", title:"Grab this link with GrabBox", contexts:["link"] });
    chrome.contextMenus.create({ id:"grab-media", title:"Grab with GrabBox", contexts:["video","audio","image"] });
    chrome.contextMenus.create({ id:"grab-page", title:"Grab video on this page with GrabBox", contexts:["page"] });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  let url = null;
  if (info.menuItemId === "grab-link") url = info.linkUrl;
  else if (info.menuItemId === "grab-media") url = info.srcUrl;
  else if (info.menuItemId === "grab-page") {
    // "Grab this page" means ask the content script for the playing video.
    if (tab && tab.id != null) {
      chrome.tabs.sendMessage(tab.id, { type:"grab-playing" }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          // No content script (or no video) — fall back to opening app.
          if (info.pageUrl) grab(info.pageUrl);
        }
      });
      return;
    }
    url = info.pageUrl || (tab && tab.url);
  }
  if (url && tab && tab.id != null) openDialogOrApp(tab.id, url);
});

/** Keyboard shortcut (Alt+G): grab the video currently playing. */
chrome.commands.onCommand.addListener((command) => {
  if (command !== "grab-current") return;
  chrome.tabs.query({ active:true, currentWindow:true }, (tabs) => {
    const tab = tabs[0];
    if (!tab || tab.id == null) return;
    // Ask the content script to find and open the dialog for the playing video.
    chrome.tabs.sendMessage(tab.id, { type:"grab-playing" }, (resp) => {
      if (chrome.runtime.lastError || !resp || !resp.ok) {
        // Fallback: also try all frames.
        chrome.webNavigation && chrome.webNavigation.getAllFrames &&
        chrome.webNavigation.getAllFrames({ tabId: tab.id }, (frames) => {
          if (!frames) { chrome.tabs.create({ url: serverUrl }); return; }
          // Try the top-level first; if no luck, open app.
          if (!resp || !resp.ok) {
            // If there's a URL in the address bar, open app with it.
            if (tab.url && /^https?:/.test(tab.url)) grab(tab.url);
          }
        });
        if (!chrome.webNavigation) {
          if (tab.url && /^https?:/.test(tab.url)) grab(tab.url);
        }
      }
    });
  });
});

/** Try the in-page dialog; fall back to opening the app tab. */
function openDialogOrApp(tabId, url) {
  chrome.tabs.sendMessage(tabId, { type:"open-dialog", url }, () => {
    if (chrome.runtime.lastError) grab(url);
  });
}

/* --------------------------------------------------------- content messages */
API.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab && sender.tab.id;

  if (msg.type === "media-count" && tabId != null) {
    setTabState(tabId, msg.count | 0);
    sendResponse({ ok:true });
    return;
  }
  if (msg.type === "grab") { grab(msg.url); sendResponse({ok:true}); return; }
  if (msg.type === "probe") {
    post("/api/probe", { url: msg.url })
      .then((res) => sendResponse(res))
      .catch(() => sendResponse(null));
    return true;
  }
  if (msg.type === "download") {
    post("/api/download", msg.body || {})
      .then((res) => sendResponse(res))
      .catch(() => sendResponse(null));
    return true;
  }
  if (msg.type === "open-app") {
    chrome.tabs.create({ url: serverUrl });
    sendResponse({ ok:true });
    return;
  }
  if (msg.type === "health") { checkHealth().then((h)=>sendResponse(h)); return true; }
  if (msg.type === "get-server") { sendResponse({ server: serverUrl }); return; }
  if (msg.type === "set-server") {
    serverUrl = msg.server || serverUrl;
    chrome.storage.local.set({ server: serverUrl });
    sendResponse({ ok:true });
    return;
  }
});

function setTabState(tabId, count) {
  const on = count > 0;
  chrome.action.setIcon({ tabId, path: on ? ICONS_ON : ICONS_OFF }).catch(()=>{});
  if (on) chrome.action.setBadgeText({ tabId, text: String(count) });
  else chrome.action.setBadgeText({ tabId, text: "" });
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#6e74ff" }).catch(()=>{});
}

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading") setTabState(tabId, 0);
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
  // Reset for newly activated tabs until their content script reports in.
  // Don't blank the count for tabs that already reported.
});

/* ----------------------------------------------------------------- grabbing */
function grab(url) {
  if (!url) return;
  chrome.tabs.create({ url: serverUrl.replace(/\/$/, "") + "/?url=" + encodeURIComponent(url) });
}

/* ------------------------------------------------------------- server calls */
async function post(path, body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120000);
  try {
    const res = await fetch(serverUrl + path, {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    return await res.json();
  } finally { clearTimeout(timer); }
}

async function checkHealth() {
  try {
    const res = await fetch(serverUrl + "/api/health");
    return await res.json();
  } catch (e) {
    return { ok:false, error:"GrabBox app is not running on " + serverUrl };
  }
}
