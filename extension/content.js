/*
 * GrabBox content script.
 *
 * Jobs:
 *   1. Count downloadable media on the page and tell the background (toolbar
 *      icon lights up). This must find <video> and <audio> elements even when
 *      they live inside a Shadow Root (TikTok, Instagram, X, most modern
 *      players), inside <iframe>s (YouTube embeds), or are attached by JS
 *      after load.
 *   2. A small ⬇ hover button on top of every video/audio/img/file-link, even
 *      inside Shadow DOM, so you never need a share button to copy a link.
 *   3. The Grab Dialog — an IDM-style in-page card: click the hover button,
 *      hit Alt+G, or right-click, and a card pops up right there, probes the
 *      URL via the local app, and lets you pick quality and download without
 *      leaving the page.
 *   4. A floating "Grab playing video" pill that appears whenever a <video>
 *      is actually playing, even on sites with no share/copy-link affordance.
 */
"use strict";

(() => {
  // Avoid double-injecting if the script gets loaded twice.
  if (window.__grabboxLoaded) return;
  window.__grabboxLoaded = true;

  const FILE_EXT = /\.(mp4|mkv|webm|mov|avi|m4v|flv|ts|mpg|mpeg|3gp|wmv|mp3|m4a|aac|opus|ogg|oga|flac|wav|wma|aiff|jpg|jpeg|png|gif|webp|bmp|svg|avif|exe|msi|apk|dmg|pkg|deb|rpm|appimage|zip|rar|7z|tar|gz|tgz|bz2|xz|iso|pdf|epub)(\?|#|$)/i;

  /* -------------------------------------------------------- collection --- */

  /** Recursively walk a root (document or shadowRoot) to find media. */
  function* walkMedia(root) {
    if (!root) return;
    // Direct elements in this root.
    for (const el of root.querySelectorAll("video, audio, img, a[href]")) yield el;
    // Elements with an attached Shadow Root.
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.shadowRoot) yield* walkMedia(el.shadowRoot);
      if (el.tagName === "VIDEO" || el.tagName === "AUDIO") {
        // <source> children — only useful when the parent has no src itself.
        for (const s of el.querySelectorAll("source")) yield s;
      }
    }
  }

  function srcOf(el) {
    if (!el) return null;
    // <source> element uses src attribute
    if (el.tagName === "SOURCE") return el.currentSrc || el.getAttribute("src");
    return el.currentSrc || el.src || el.getAttribute("src");
  }

  function isUsableUrl(u) {
    if (!u || typeof u !== "string") return false;
    if (!u.startsWith("http")) return false;
    // Blob URLs are ephemeral and yt-dlp cannot fetch them — but most of the
    // time they're a front for HLS/DASH we can sniff from <source> or the
    // player's own network; skip blobs here so we don't offer dead links.
    if (u.startsWith("blob:")) return false;
    return true;
  }

  function collect() {
    const items = [];
    const seen = new Set();
    const push = (url, kind, label, el) => {
      if (!isUsableUrl(url)) return;
      if (seen.has(url)) return;
      seen.add(url);
      items.push({ url, kind, label: (label || "").trim() || url, el });
    };

    for (const el of walkMedia(document)) {
      const tag = el.tagName;
      if (tag === "VIDEO") {
        // The currentSrc attribute is the resolved, playing source (works for
        // HLS/DASH/mp4). Fall back to src.
        const u = srcOf(el);
        if (isUsableUrl(u)) push(u, "video", el.title || el.getAttribute("title") || document.title, el);
        // Also surface any HLS/DASH playlist <source> children.
        for (const s of el.querySelectorAll("source[type*='application'], source[src*='.m3u8'], source[src*='.mpd']")) {
          const su = srcOf(s);
          if (isUsableUrl(su)) push(su, "video", "HLS/DASH stream", el);
        }
      } else if (tag === "AUDIO") {
        const u = srcOf(el);
        if (isUsableUrl(u)) push(u, "audio", el.title || document.title, el);
      } else if (tag === "IMG") {
        // Skip tiny UI icons, tracking pixels, sprites — they're noise.
        const w = el.naturalWidth || el.width || 0;
        const h = el.naturalHeight || el.height || 0;
        if (w < 80 || h < 60) continue;
        push(el.currentSrc || el.src, "image", el.alt || "image", el);
      } else if (tag === "A") {
        if (FILE_EXT.test(el.href)) push(el.href, "file", el.textContent.trim() || el.href, el);
      } else if (tag === "SOURCE") {
        // Fall through: handled by parent VIDEO/AUDIO walk above.
      }
    }

    // Look for og:video / twitter:player meta tags — useful for embedded
    // players where the <video> lives in a cross-origin iframe we can't see.
    for (const sel of ['meta[property="og:video:secure_url"]', 'meta[property="og:video:url"]',
                       'meta[property="og:video"]', 'meta[name="twitter:player:stream"]']) {
      const m = document.querySelector(sel);
      const u = m && m.getAttribute("content");
      if (isUsableUrl(u) && !seen.has(u)) push(u, "video", document.title, null);
    }

    return items.slice(0, 200);
  }

  let _reportTimer = null;
  function report() {
    clearTimeout(_reportTimer);
    _reportTimer = setTimeout(() => {
      const items = collect();
      const playingVideo = [...walkMedia(document)].find((el) =>
        el.tagName === "VIDEO" && isUsableUrl(srcOf(el)) && !el.paused && el.duration > 0
      );
      updateFloatingPill(playingVideo, items);
      chrome.runtime.sendMessage({ type: "media-count", count: items.length }).catch(() => {});
    }, 200);
  }

  /* ============================================================== dialog == */

  const DIALOG_CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    .card { position: fixed; top: 64px; left: 50%; transform: translateX(-50%);
      width: 360px; max-width: calc(100vw - 32px); z-index: 2147483647;
      background: #171a21; color: #e7ecf3;
      border: 1px solid #2b3342; border-radius: 18px;
      box-shadow: 0 18px 60px rgba(0,0,0,.55), 0 4px 16px rgba(0,0,0,.4);
      overflow: hidden; animation: pop .18s ease; }
    @keyframes pop { from { opacity: 0; transform: translateX(-50%) translateY(-8px) scale(.98); } }
    .head { display:flex; align-items:center; gap:8px; padding:12px 14px;
      border-bottom:1px solid #232a36; background:#1b2029; }
    .head b { font-size:13.5px; font-weight:650; flex:1; letter-spacing:.2px; }
    .x { width:26px; height:26px; border:0; border-radius:50%; cursor:pointer;
      background:transparent; color:#8d99ab; font-size:13px; }
    .x:hover { background:#2a3240; color:#fff; }
    .body { padding:14px; display:grid; gap:10px; }
    .status { font-size:13px; color:#8d99ab; padding:18px 4px; text-align:center; }
    .meta { display:flex; gap:11px; align-items:center; min-height:0; }
    .thumb { width:104px; aspect-ratio:16/9; object-fit:cover; border-radius:9px;
      background:#0f131a; flex:none; }
    .thumb.broken { display:none; }
    .tblock { min-width:0; }
    .title { font-size:13px; font-weight:600; line-height:1.35;
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
      overflow:hidden; overflow-wrap:anywhere; }
    .sub { font-size:11.5px; color:#8d99ab; margin-top:4px; }
    select, input { width:100%; background:#0f131a; color:#e7ecf3;
      border:1px solid #2b3342; border-radius:10px; padding:9px 11px;
      font-size:13px; outline:none; }
    select:focus, input:focus { border-color:#4f8cff; }
    .row2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    label { font-size:10.5px; color:#8d99ab; text-transform:uppercase;
      letter-spacing:.7px; display:block; margin:0 0 4px 2px; font-weight:600; }
    .dl { border:0; border-radius:11px; cursor:pointer; padding:11px;
      background:linear-gradient(140deg,#6e74ff,#8e5ef4);
      color:#fff; font-size:14px; font-weight:650; }
    .dl:hover { filter:brightness(1.1); }
    .dl:disabled { opacity:.45; cursor:default; }
    .foot { display:flex; justify-content:space-between; align-items:center; font-size:11.5px; }
    .link { color:#4f8cff; cursor:pointer; text-decoration:none; }
    .link:hover { text-decoration:underline; }
    .err { font-size:12.5px; color:#ff9aa4; background:#2a171c; border:1px solid #4d2229;
      border-radius:10px; padding:9px 11px; overflow-wrap:anywhere; }
    .hint { font-size:12px; color:#f6c453; }
    .ok { font-size:13px; color:#7bd88f; text-align:center; padding:10px 0 2px; }
    .hidden { display:none !important; }
    .detected { margin-top:4px; font-size:12px; color:#8d99ab;
      display:flex; flex-wrap:wrap; gap:6px; }
    .detected button { background:#0f131a; color:#9ab0ef; border:1px solid #2b3342;
      border-radius:8px; padding:4px 9px; font-size:11.5px; cursor:pointer; }
    .detected button:hover { border-color:#4f8cff; color:#fff; }
  `;

  const QUALITIES = {
    video: [["best","Best available"],["2160","2160p (4K)"],["1440","1440p"],
            ["1080","1080p"],["720","720p"],["480","480p"],["360","360p (small)"]],
    audio: [["m4a","m4a — plays everywhere"],["mp3","mp3 — max compatibility"],
            ["opus","opus — best size/quality"],["flac","flac — lossless, big"],
            ["best","original stream, no re-encode"]],
    file:  [["original","original file, as served"]],
  };
  let server = "http://127.0.0.1:8765";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function fmtBytes(n) {
    if (!n) return "";
    const u = ["B","KB","MB","GB","TB"]; let i=0;
    while (n >= 1024 && i < u.length-1) { n/=1024; i++; }
    return `${n.toFixed(i?1:0)} ${u[i]}`;
  }
  function fmtDur(s) {
    s = Math.round(s); const m = Math.floor(s/60);
    return `${m}:${String(s%60).padStart(2,"0")}`;
  }
  const rpc = (msg) => new Promise((resolve) => {
    try { chrome.runtime.sendMessage(msg, (res) => {
      if (chrome.runtime.lastError) resolve(null); else resolve(res);
    }); } catch (e) { resolve(null); }
  });

  let host = null, cardEl = null, dialogEls = null, currentProbe = null, chosenKind = "file";

  function closeDialog() {
    if (host) { host.remove(); host = null; cardEl = null; dialogEls = null; currentProbe = null; }
    window.removeEventListener("keydown", onEsc, true);
    window.removeEventListener("mousedown", onOutside, true);
  }
  function onEsc(e) { if (e.key === "Escape") { e.stopPropagation(); closeDialog(); } }
  function onOutside(e) {
    if (host && cardEl && !e.composedPath().includes(cardEl)) closeDialog();
  }

  function openDialog(url, opts={}) {
    closeDialog();
    host = document.createElement("div");
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style"); style.textContent = DIALOG_CSS;
    const card = el("div", "card"); cardEl = card;

    const head = el("div","head");
    head.appendChild(el("b",null,"Grab with GrabBox"));
    const x = el("button","x","✕"); x.addEventListener("click", closeDialog);
    head.appendChild(x);

    const body = el("div","body");
    const status = el("div","status","Reading link…");
    const result = el("div","hidden");
    const errorBox = el("div","err hidden");
    const okBox = el("div","ok hidden");
    // List of other detected media on the page so the user can switch targets
    // without leaving the dialog (handy for one-page players).
    const detectedBox = el("div","detected hidden");
    body.append(status, detectedBox, result, errorBox, okBox);
    card.append(head, body);
    root.append(style, card);
    document.documentElement.appendChild(host);

    window.addEventListener("keydown", onEsc, true);
    setTimeout(() => window.addEventListener("mousedown", onOutside, true), 50);

    dialogEls = { status, result, errorBox, okBox, detectedBox };
    // If there are multiple media items on the page, list them.
    const all = collect();
    if (all.length > 1) {
      detectedBox.appendChild(el("span",null,"Also on this page:"));
      all.slice(0,6).filter((i)=>i.url!==url).forEach((it) => {
        const b = el("button",null, (it.kind==="video"?"🎬":it.kind==="audio"?"🎵":it.kind==="image"?"🖼️":"⬇️") + " " + (it.label.slice(0,34)||"item"));
        b.title = it.url;
        b.addEventListener("click", () => { closeDialog(); openDialog(it.url); });
        detectedBox.appendChild(b);
      });
      detectedBox.classList.remove("hidden");
    }
    probeInto(url, dialogEls);
  }

  async function probeInto(url, els) {
    const res = await rpc({ type:"probe", url });
    if (!dialogEls) return;
    if (!res) {
      els.status.textContent = "";
      els.errorBox.classList.remove("hidden");
      els.errorBox.textContent = "The GrabBox app isn't running. Start it, then try again — the app does the actual downloading.";
      addOpenAppRow(els);
      const retry = el("button","dl","Retry");
      retry.addEventListener("click", () => openDialog(url));
      els.status.replaceWith(retry);
      return;
    }
    if (!res.ok) {
      els.status.classList.add("hidden");
      els.errorBox.classList.remove("hidden");
      els.errorBox.textContent = res.error || "Could not read that link.";
      if (res.hint) {
        const hint = el("div","hint","💡 " + res.hint);
        els.errorBox.appendChild(hint);
      }
      addOpenAppRow(els);
      return;
    }
    currentProbe = res;
    els.status.classList.add("hidden");
    buildResult(res, els, url);
  }

  function addOpenAppRow(els) {
    const foot = el("div","foot");
    const link = el("a","link","Open the GrabBox app →");
    link.addEventListener("click", () => rpc({ type:"open-app" }));
    foot.append(link, el("span",null,""));
    els.result.parentElement.appendChild(foot);
  }

  function buildResult(res, els, url) {
    const r = els.result;
    r.classList.remove("hidden"); r.style.display = "grid"; r.style.gap = "10px";
    const meta = el("div","meta");
    const img = document.createElement("img");
    img.className = "thumb broken"; img.alt = "";
    img.addEventListener("error", () => img.classList.add("broken"));
    if (res.thumb) { img.src = res.thumb; img.classList.remove("broken"); }
    const tblock = el("div","tblock");
    tblock.appendChild(el("div","title", res.title || url));
    const bits = [];
    if (res.uploader) bits.push(res.uploader);
    if (res.duration) bits.push(fmtDur(res.duration));
    if (res.direct && res.direct.size) bits.push(fmtBytes(res.direct.size));
    if (res.is_playlist) bits.push("playlist");
    tblock.appendChild(el("div","sub", bits.join("  ·  ") || KIND_NAME(res.kind)));
    meta.append(img, tblock); r.appendChild(meta);

    const kinds = kindsFor(res);
    chosenKind = kinds.includes(res.kind) ? res.kind : kinds[0];
    const kindSel = document.createElement("select");
    KIND_NAMES().forEach(([k,label]) => {
      if (!kinds.includes(k)) return;
      const o = document.createElement("option"); o.value=k; o.textContent=label; kindSel.appendChild(o);
    });
    kindSel.value = chosenKind;
    const qSel = document.createElement("select");
    const fillQ = () => {
      const group = (chosenKind === "video" || chosenKind === "audio") ? chosenKind : "file";
      let opts = QUALITIES[group];
      if (group === "video") {
        const hs = (res.formats||[]).filter((f)=>f.kind==="video"&&f.height).map((f)=>f.height);
        if (hs.length) { const maxH = Math.max(...hs);
          opts = opts.filter(([v])=>v==="best"||parseInt(v,10)<=maxH*1.02); }
      }
      qSel.innerHTML = "";
      opts.forEach(([v,label]) => { const o = document.createElement("option");
        o.value=v; o.textContent=label; qSel.appendChild(o); });
    };
    fillQ();
    kindSel.addEventListener("change", () => { chosenKind = kindSel.value; fillQ(); });
    const row = el("div","row2");
    const kwrap = el("div"); kwrap.append(el("label",null,"Grab as"), kindSel);
    const qwrap = el("div"); qwrap.append(el("label",null,"Quality"), qSel);
    row.append(kwrap, qwrap); r.appendChild(row);

    const nameWrap = el("div");
    nameWrap.appendChild(el("label",null,"Rename (optional)"));
    const nameInput = document.createElement("input");
    nameInput.placeholder = cleanName((res.direct && res.direct.filename) || res.title || "");
    nameWrap.appendChild(nameInput);
    r.appendChild(nameWrap);

    const dl = el("button","dl","Download");
    dl.addEventListener("click", async () => {
      dl.disabled = true; dl.textContent = "Adding…";
      const out = await rpc({ type:"download", body: {
        url, kind: chosenKind, quality: qSel.value,
        playlist: !!res.is_playlist, filename: nameInput.value.trim()||null,
      }});
      if (out && out.job) {
        els.result.classList.add("hidden");
        els.okBox.classList.remove("hidden");
        els.okBox.textContent = "✓ Queued — the GrabBox app is on it.";
        setTimeout(closeDialog, 1800);
      } else {
        dl.disabled = false; dl.textContent = "Download";
        els.errorBox.classList.remove("hidden");
        els.errorBox.textContent = (out && out.error) || "Could not start the download — is the app running?";
      }
    });
    r.appendChild(dl);

    const foot = el("div","foot");
    const link = el("a","link","open full app");
    link.addEventListener("click", () => rpc({ type:"open-app" }));
    foot.append(link);
    const sizeNote = el("span",null, res.direct && res.direct.size ? "~"+fmtBytes(res.direct.size) : "");
    foot.append(sizeNote); r.appendChild(foot);
  }

  function KIND_NAMES() {
    return [["video","Video"],["audio","Audio only"],["image","Image"],
            ["app","File"],["archive","Archive"],["file","File"]];
  }
  function KIND_NAME(k) {
    return { video:"Video", audio:"Audio", image:"Image", app:"Software",
             archive:"Archive", document:"Document", other:"File" }[k] || "File";
  }
  function kindsFor(res) {
    const set = new Set();
    if (res.kind) set.add(res.kind === "other" ? "file" : res.kind);
    const fmts = res.formats || [];
    if (fmts.some((f)=>f.kind==="video")) set.add("video");
    if (fmts.some((f)=>f.kind==="audio")) set.add("audio");
    if (res.direct) set.add(res.direct.kind === "other" ? "file" : res.direct.kind);
    if (!set.size) set.add("file");
    return [...set];
  }
  function cleanName(s) {
    return (s||"").replace(/[\\/:*?"<>|]+/g," ").replace(/\s+/g," ").trim();
  }

  /* ============================================== hover button + triggers = */

  let btn = null, btnTarget = null;
  function makeButton() {
    btn = document.createElement("div");
    btn.className = "grabbox-btn";
    btn.title = "Grab with GrabBox";
    btn.innerHTML = "&#8681;";
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); e.preventDefault();
      if (btnTarget) openDialog(btnTarget);
      hideButton();
    });
    // Attach the button to the top-level document so it isn't clipped by
    // overflow:hidden ancestors inside Shadow DOM.
    (document.body || document.documentElement).appendChild(btn);
  }
  function positionFor(el) {
    const r = el.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) return;
    btn.style.top = (r.top + window.scrollY + 8) + "px";
    btn.style.left = Math.max(4, r.left + window.scrollX + 8) + "px";
    btn.classList.add("show");
  }
  function hideButton() { if (btn) btn.classList.remove("show"); btnTarget = null; }

  function targetOf(el) {
    if (!el || !el.closest) return null;
    // Walk up from the hovered element to find the nearest media/link.
    // We walk the *composed* path so elements inside Shadow DOM resolve to
    // their host, which lets us catch video players that live in a shadow root
    // (TikTok, Instagram, X, YouTube's modern player).
    const path = (el.composedPath && el.composedPath()) || [el];
    for (const node of path) {
      if (!(node instanceof Element)) continue;
      if (node.tagName === "VIDEO" || node.tagName === "AUDIO") {
        const u = srcOf(node);
        if (isUsableUrl(u)) return { url: u, el: node };
      }
      if (node.tagName === "IMG") {
        if ((node.naturalWidth||node.width) >= 80) return { url: node.currentSrc||node.src, el: node };
      }
      if (node.tagName === "A" && node.href && FILE_EXT.test(node.href)) {
        return { url: node.href, el: node };
      }
    }
    return null;
  }

  document.addEventListener("mouseover", (e) => {
    const hit = targetOf(e.target);
    if (!hit) return hideButton();
    if (!btn) makeButton();
    if (btnTarget !== hit.url) { btnTarget = hit.url; positionFor(hit.el); }
  });
  document.addEventListener("scroll", hideButton, { passive:true });
  window.addEventListener("blur", hideButton);

  /* ----------------------------------------------- floating "Grab playing" pill */

  let pillEl = null;
  function ensurePill() {
    if (pillEl) return pillEl;
    pillEl = document.createElement("div");
    pillEl.className = "grabbox-float-pill";
    pillEl.innerHTML = '<span class="gbx-dot"></span><span class="gbx-txt">Grab playing video</span>';
    pillEl.addEventListener("click", () => {
      const v = [...walkMedia(document)].find((el) =>
        el.tagName === "VIDEO" && isUsableUrl(srcOf(el)) && !el.paused);
      if (v) openDialog(srcOf(v));
    });
    Object.assign(pillEl.style, {
      position: "fixed", top: "14px", right: "14px", zIndex: 2147483645,
      display: "none", alignItems: "center", gap: "8px", padding: "8px 14px",
      borderRadius: "999px", background: "rgba(23,26,33,.92)", color: "#e7ecf3",
      border: "1px solid rgba(120,160,255,.45)", font: "600 12.5px/1 system-ui,sans-serif",
      cursor: "pointer", boxShadow: "0 6px 20px rgba(0,0,0,.45)",
      backdropFilter: "blur(8px)",
    });
    const dot = pillEl.querySelector(".gbx-dot");
    Object.assign(dot.style, {
      width: "8px", height: "8px", borderRadius: "50%",
      background: "#ff6b6b", boxShadow: "0 0 0 0 rgba(255,107,107,.6)",
      animation: "gbxpulse 1.4s infinite",
    });
    const style = document.createElement("style");
    style.textContent = "@keyframes gbxpulse{0%{box-shadow:0 0 0 0 rgba(255,107,107,.6)}70%{box-shadow:0 0 0 10px rgba(255,107,107,0)}100%{box-shadow:0 0 0 0 rgba(255,107,107,0)}}";
    document.head.appendChild(style);
    (document.body||document.documentElement).appendChild(pillEl);
    return pillEl;
  }
  function updateFloatingPill(videoEl) {
    const pill = ensurePill();
    pill.style.display = videoEl ? "flex" : "none";
  }

  /* ----------------------------------------------- background / popup msgs */

  chrome.runtime.onMessage.addListener((msg, sender, send) => {
    if (!msg || !msg.type) return false;
    if (msg.type === "open-dialog" && msg.url) { openDialog(msg.url); send({ok:true}); return false; }
    if (msg.type === "grab-playing") {
      const v = [...walkMedia(document)].find((el) =>
        el.tagName === "VIDEO" && isUsableUrl(srcOf(el)) && !el.paused)
        || [...walkMedia(document)].find((el) => el.tagName==="VIDEO" && isUsableUrl(srcOf(el)));
      if (v) { openDialog(srcOf(v)); send({ok:true}); }
      else send({ok:false, error:"No playing video found on this page"});
      return false;
    }
    if (msg.type === "list") { send({ items: collect().map((i)=>({url:i.url,kind:i.kind,label:i.label})) }); return false; }
    return false;
  });

  rpc({ type:"get-server" }).then((s)=>{ if (s && s.server) server = s.server; });

  // Initial report + watch for DOM changes / video play events.
  report();
  const mo = new MutationObserver(() => { clearTimeout(mo._t); mo._t = setTimeout(report, 900); });
  mo.observe(document.documentElement, { childList:true, subtree:true, attributes:true, attributeFilter:["src","currentSrc"] });
  document.addEventListener("play", () => report(), true);
  document.addEventListener("pause", () => report(), true);
  document.addEventListener("loadeddata", () => report(), true);
  window.addEventListener("focus", report);
  setInterval(report, 4000);
})();
