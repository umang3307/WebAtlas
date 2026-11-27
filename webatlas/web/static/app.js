/* WebAtlas console.
   Security note: everything shown here can come from crawled websites, so text is always set with
   textContent / createElement - never innerHTML - and links only accept http(s) addresses. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 900;
  const PAGE_SIZE = 200;

  const PRESETS = {
    quick: { max_depth: 1, max_pages: 25, max_minutes: 3, max_doc_checks: 200 },
    standard: { max_depth: 2, max_pages: 100, max_minutes: 10, max_doc_checks: 1000 },
    deep: { max_depth: 3, max_pages: 500, max_minutes: 30, max_doc_checks: 5000 },
  };
  // Limits that can be switched off with their "No limit" box (sent to the server as null).
  const LIFTABLE = ["max_depth", "max_pages", "max_minutes", "max_doc_checks"];
  const NUMBER_FIELDS = ["max_depth", "max_pages", "max_minutes", "max_doc_checks", "per_host_delay", "concurrency"];
  const BOOL_FIELDS = ["use_sitemaps", "include_subdomains", "include_offsite_documents", "download_documents",
    "render_js"];
  // How each document was confirmed (see webatlas/detect.py).
  const DETECTED = {
    "content-type": "Content type",
    "content-disposition": "Download name",
    "magic-bytes": "File signature",
    "extension": "File extension",
    "extension-unverified": "Not confirmed",
  };
  const DETECTED_HELP = {
    "content-type": "The server's Content-Type header says it's this kind of file.",
    "content-disposition": "The server sent it as a download with this file name.",
    "magic-bytes": "The first bytes of the file match this format, whatever the link says.",
    "extension": "The server gave no useful type, so the file extension was used.",
    "extension-unverified": "Listed from its link only: the limit on files to confirm was reached.",
  };
  const TYPE_GROUPS = {
    pdf: "pdf", doc: "word", docx: "word", odt: "word", rtf: "word", txt: "word", "office-legacy": "word",
    xls: "sheet", xlsx: "sheet", ods: "sheet", csv: "sheet",
    ppt: "slides", pptx: "slides", odp: "slides",
    zip: "archive", rar: "archive", "7z": "archive", epub: "archive",
  };
  const TYPE_LABEL = { "office-legacy": "OFFICE" };

  /* ---------------------------------------------------------------- state */

  let info = { render_available: false, defaults: {} };
  let scan = null;          // current scan state
  let pollTimer = 0;
  let pollToken = 0;
  let renderQueued = false;
  const atlas = new window.Atlas($("atlas"), {
    tooltip: $("tooltip"),
    onSelectPage: (url) => setPageFilter(url),
    onSelectDocument: (doc) => focusDocument(doc),
  });

  function newScanState(id) {
    return {
      id, target: "", status: "", summary: {}, maxPages: 0,
      documents: [], pages: [], issues: [], events: [],
      cursors: { events: 0, documents: 0, pages: 0, issues: 0 },
      filter: { q: "", types: new Set(), page: null }, docLimit: PAGE_SIZE, pageLimit: PAGE_SIZE,
    };
  }

  /* ---------------------------------------------------------------- helpers */

  async function api(path, opts = {}) {
    const init = { headers: {}, ...opts };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      throw new Error("Can't reach the WebAtlas server. Is it still running?");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }

  function safeUrl(u) {
    try { const p = new URL(u); return p.protocol === "http:" || p.protocol === "https:" ? p.href : null; }
    catch (e) { return null; }
  }

  function link(url, text, cls) {
    const href = safeUrl(url);
    if (!href) return el("span", { class: cls, text });
    return el("a", { class: cls, href, target: "_blank", rel: "noopener noreferrer", text });
  }

  function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
  function shortPath(u) {
    try {
      const p = new URL(u);
      const rootHost = scan ? hostOf(scan.target) : "";
      const prefix = p.hostname.replace(/^www\./, "") === rootHost ? "" : p.hostname;
      return prefix + (p.pathname + p.search || "/");
    } catch (e) { return u; }
  }

  function duration(sec) {
    sec = Math.round(sec || 0);
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    return m < 60 ? `${m}m ${String(s).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  function ago(iso) {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  function fmt(n) { return (n || 0).toLocaleString(); }

  let toastTimer = 0;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
  }

  function store(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ } }
  function load(key) { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } }

  /* ---------------------------------------------------------------- settings */

  function lifted(f) { return LIFTABLE.includes(f) && $(`${f}_off`).checked; }

  function readSettings() {
    const v = (id) => $(id).value;
    const s = {};
    for (const f of NUMBER_FIELDS) {
      if (lifted(f)) { s[f] = null; continue; }
      const n = Number(v(f));
      if (v(f) !== "" && Number.isFinite(n)) s[f] = n;
    }
    for (const f of BOOL_FIELDS) s[f] = $(f).checked;
    const out = { ...s, max_duration: s.max_minutes === null ? null : (s.max_minutes || 10) * 60 };
    delete out.max_minutes;
    return out;
  }

  function syncLimitInputs() {
    for (const f of LIFTABLE) $(f).disabled = lifted(f);
    const s = readSettings();
    const warn = [];
    if (s.max_depth === null || s.max_pages === null) {
      warn.push("Without a depth or page limit, a large site can mean thousands of pages and hours of crawling.");
    }
    if (s.max_duration === null) warn.push("With no time limit, the scan runs until it's finished or you stop it.");
    if (s.max_doc_checks === null) warn.push("Every file is confirmed with its own request, which slows big scans.");
    if (warn.length) warn.push("Requests stay polite, and Stop keeps everything found so far.");
    $("limitWarning").textContent = warn.join(" ");
    $("limitWarning").hidden = warn.length === 0;
  }

  function applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    for (const [k, v] of Object.entries(p)) $(k).value = v;
    for (const f of LIFTABLE) $(`${f}_off`).checked = false;
    syncLimitInputs();
    updatePresetSummary();
  }

  function updatePresetSummary() {
    const s = readSettings();
    const depth = s.max_depth === null ? "Follows every link on the site" :
      s.max_depth === 0 ? "Reads only the start pages" :
      `Follows links ${s.max_depth} ${s.max_depth === 1 ? "step" : "steps"} deep`;
    const pages = s.max_pages === null ? "reads every page it finds" : `reads up to ${fmt(s.max_pages)} pages`;
    const time = s.max_duration === null ? "runs until it's done or you stop it" :
      `stops after ${Math.round(s.max_duration / 60)} minutes`;
    $("presetSummary").textContent = `${depth}, ${pages}, ${time}.`;
  }

  function saveSettings() {
    const preset = document.querySelector("input[name=preset]:checked").value;
    const values = {};
    for (const f of NUMBER_FIELDS) values[f] = $(f).value;
    for (const f of BOOL_FIELDS) values[f] = $(f).checked;
    const off = LIFTABLE.filter(lifted);
    store("webatlas.settings", { preset, values, off });
  }

  function initSettings() {
    const d = info.defaults || {};
    $("per_host_delay").value = d.per_host_delay ?? 0.5;
    $("concurrency").value = d.concurrency ?? 4;
    for (const f of BOOL_FIELDS) $(f).checked = !!d[f];
    applyPreset("standard");

    const saved = load("webatlas.settings");
    if (saved && saved.values) {
      for (const f of NUMBER_FIELDS) if (saved.values[f] !== undefined && saved.values[f] !== "") $(f).value = saved.values[f];
      for (const f of BOOL_FIELDS) if (typeof saved.values[f] === "boolean") $(f).checked = saved.values[f];
      const radio = document.querySelector(`input[name=preset][value="${saved.preset}"]`);
      if (radio) radio.checked = true;
      for (const f of LIFTABLE) $(`${f}_off`).checked = Array.isArray(saved.off) && saved.off.includes(f);
    }
    syncLimitInputs();
    if (!info.render_available) {
      $("render_js").checked = false;
      $("render_js").disabled = true;
      const t = $("renderToggle");
      t.classList.add("unavailable");
      t.title = 'Install it with: pip install "webatlas[render]" && playwright install chromium';
      t.querySelector("span").textContent = "Use a browser for JavaScript pages (not installed)";
    }
    updatePresetSummary();

    document.querySelectorAll("input[name=preset]").forEach((r) => r.addEventListener("change", () => {
      if (r.value !== "custom") applyPreset(r.value);
      saveSettings();
    }));
    for (const f of LIFTABLE) {
      $(`${f}_off`).addEventListener("change", () => {
        document.querySelector("input[name=preset][value=custom]").checked = true;
        syncLimitInputs();
        updatePresetSummary();
        saveSettings();
        if (!$(`${f}_off`).checked) $(f).focus();
      });
    }
    for (const f of [...NUMBER_FIELDS, ...BOOL_FIELDS]) {
      $(f).addEventListener("input", () => {
        if (NUMBER_FIELDS.includes(f) && f in PRESETS.standard) {
          document.querySelector("input[name=preset][value=custom]").checked = true;
        }
        updatePresetSummary();
        saveSettings();
      });
    }
    $("settingsToggle").addEventListener("click", () => {
      const open = $("settings").hidden;
      $("settings").hidden = !open;
      $("settingsToggle").setAttribute("aria-expanded", String(open));
      $("settingsToggle").textContent = open ? "Hide settings" : "Adjust settings";
    });
  }

  /* ---------------------------------------------------------------- start / sections */

  function formError(msg) { $("formError").textContent = msg || ""; }

  function currentUrl() {
    const url = $("urlInput").value.trim();
    if (!url) { formError("Enter a website address to scan."); $("urlInput").focus(); return null; }
    formError("");
    return url;
  }

  async function startScan(seeds) {
    const url = currentUrl();
    if (!url) return;
    const btn = $("startBtn");
    btn.disabled = true;
    try {
      const { id } = await api("/api/scans", { method: "POST", body: { url, seeds: seeds || [], settings: readSettings() } });
      $("sectionsPanel").hidden = true;
      location.hash = `#/scan/${id}`;
      refreshHistory();
    } catch (e) {
      formError(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function chooseSections() {
    const url = currentUrl();
    if (!url) return;
    const btn = $("sectionsBtn");
    btn.disabled = true;
    btn.textContent = "Reading homepage…";
    try {
      const data = await api("/api/discover", { method: "POST", body: { url } });
      const list = $("sectionsList");
      list.replaceChildren();
      for (const s of data.sections) {
        const id = "sec-" + list.children.length;
        list.append(el("li", {}, el("label", { for: id },
          el("input", { type: "checkbox", id, value: s, checked: true }), el("span", { text: shortPathFor(s, data.url) }))));
      }
      const n = data.sections.length;
      $("sectionsNote").textContent = n
        ? `The homepage links to ${n} ${n === 1 ? "page" : "pages"} on this site` +
          (data.documents.length ? ` and ${data.documents.length} ${data.documents.length === 1 ? "file" : "files"}.` : ".") +
          " The scan starts from the ones you keep selected."
        : "The homepage has no links to other pages on this site. Start a normal scan instead; the sitemap may still list pages.";
      $("sectionsStart").disabled = n === 0;
      $("sectionsPanel").hidden = false;
      $("sectionsTitle").focus();
    } catch (e) {
      formError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Choose sections…";
    }
  }

  function shortPathFor(u, root) {
    try {
      const p = new URL(u), r = new URL(root);
      return (p.hostname === r.hostname ? "" : p.hostname) + p.pathname + p.search;
    } catch (e) { return u; }
  }

  /* ---------------------------------------------------------------- routing */

  function route() {
    const m = location.hash.match(/^#\/scan\/([a-f0-9]{6,32})$/);
    if (m) openScan(m[1]); else showIdle();
  }

  function stopPolling() { clearTimeout(pollTimer); pollToken += 1; }

  function showIdle() {
    stopPolling();
    scan = null;
    atlas.reset(null);
    $("scanHead").hidden = true;
    $("stats").hidden = true;
    $("progress").hidden = true;
    $("stoppedNote").hidden = true;
    $("mapNote").hidden = true;
    $("results").hidden = true;
    $("atlasEmpty").hidden = false;
    $("atlasCaption").hidden = true;
    document.querySelector(".panes").classList.add("idle");
    markHistory(null);
    document.title = "WebAtlas";
  }

  function openScan(id) {
    stopPolling();
    scan = newScanState(id);
    atlas.reset(null);
    for (const k of ["docRows", "pageRows", "issueRows", "activity", "typeChips"]) $(k).replaceChildren();
    $("docSearch").value = "";
    setPageFilter(null, false);
    $("scanHead").hidden = false;
    $("stats").hidden = false;
    $("results").hidden = false;
    $("atlasEmpty").hidden = true;
    $("atlasCaption").hidden = false;
    document.querySelector(".panes").classList.remove("idle");
    $("exportCsv").href = `/api/scans/${id}/export?format=csv`;
    $("exportJson").href = `/api/scans/${id}/export?format=json`;
    resetDelete();
    markHistory(id);
    poll(pollToken);
  }

  /* ---------------------------------------------------------------- polling */

  async function poll(token) {
    if (!scan || token !== pollToken) return;
    const c = scan.cursors;
    let data;
    try {
      data = await api(`/api/scans/${scan.id}?events=${c.events}&documents=${c.documents}&pages=${c.pages}&issues=${c.issues}`);
    } catch (e) {
      if (token !== pollToken) return;
      if (/No scan/.test(e.message)) { toast("That scan no longer exists."); location.hash = "#/"; return; }
      toast(e.message);
      pollTimer = setTimeout(() => poll(token), POLL_MS * 3);
      return;
    }
    if (token !== pollToken) return;

    const first = !scan.target;
    scan.target = data.target;
    scan.maxPages = data.max_pages;
    scan.summary = data.summary || {};
    if (first) atlas.reset(data.target);

    scan.pages.push(...data.pages);
    scan.documents.push(...data.documents);
    scan.issues.push(...data.issues);
    scan.events.push(...data.events);
    if (scan.events.length > 1500) scan.events.splice(0, scan.events.length - 1500);
    c.pages += data.pages.length;
    c.documents += data.documents.length;
    c.issues += data.issues.length;
    c.events = data.events_total;

    atlas.addPages(data.pages);
    atlas.addDocuments(data.documents);
    const statusChanged = scan.status !== data.status;
    scan.status = data.status;
    renderHeader(data);
    if (data.events.length) appendActivity(data.events);
    scheduleRender();

    if (data.status === "running") {
      pollTimer = setTimeout(() => poll(token), POLL_MS);
    } else if (statusChanged && !first) {
      refreshHistory();
      const found = `${fmt(scan.documents.length)} ${scan.documents.length === 1 ? "document" : "documents"} found`;
      toast(data.status === "error" ? "The scan failed." :
        data.status === "stopped" ? `Scan stopped: ${found}.` : `Scan finished: ${found}.`);
    }
  }

  /* ---------------------------------------------------------------- header + stats */

  const STATUS_TEXT = { running: "Scanning", done: "Finished", stopped: "Stopped", error: "Failed",
    interrupted: "Interrupted (server restarted)" };

  function renderHeader(data) {
    const s = scan.summary;
    $("scanHost").textContent = hostOf(scan.target);
    document.title = `${hostOf(scan.target)} – WebAtlas`;
    const st = $("scanStatus");
    st.className = `status ${scan.status}`;
    st.textContent = STATUS_TEXT[scan.status] || scan.status;
    const started = new Date(data.started_at);
    $("scanWhen").textContent = isNaN(started) ? "" :
      `Started ${started.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`;

    $("statDocs").textContent = fmt(scan.documents.length);
    $("statPages").textContent = fmt(scan.pages.length);
    $("statPagesOf").textContent = scan.maxPages ? ` / ${fmt(scan.maxPages)}` : "";
    $("statIssues").textContent = fmt(scan.issues.length);
    $("statRequests").textContent = fmt(s.requests);
    $("statTime").textContent = duration(s.elapsed);

    const running = scan.status === "running";
    $("progress").hidden = !running;
    $("progress").classList.toggle("open-ended", running && !scan.maxPages);
    if (running && scan.maxPages) {
      $("progressBar").style.width = `${Math.min(100, (scan.pages.length / scan.maxPages) * 100)}%`;
    }
    const mapNote = $("mapNote");
    mapNote.hidden = !atlas.truncated;
    if (atlas.truncated) {
      mapNote.textContent = `The map shows the first ${fmt(window.Atlas.MAX_MAP_PAGES)} pages and ` +
        `${fmt(window.Atlas.MAX_MAP_DOCS)} documents to stay fast. The tables and exports include everything.`;
    }
    const stop = $("stopBtn");
    stop.hidden = !running;
    if (!running) { stop.disabled = false; stop.textContent = "Stop scan"; }
    $("deleteBtn").hidden = running;

    const note = $("stoppedNote");
    note.classList.toggle("error", scan.status === "error" || scan.status === "interrupted");
    if (scan.status === "error") {
      note.textContent = `The scan failed: ${data.error || "unknown error"}.`;
      note.hidden = false;
    } else if (scan.status === "interrupted") {
      note.textContent = "The server stopped while this scan was running, so its results weren't saved. Start it again.";
      note.hidden = false;
    } else if (s.stopped_early && !running) {
      const why = s.stopped_early === "stopped by user" ? "You stopped this scan" :
        `This scan stopped early because the ${s.stopped_early.replace(/ reached$/, "")} was reached`;
      note.textContent = `${why}. The results below are everything it found up to that point.`;
      note.hidden = false;
    } else {
      note.hidden = true;
    }
    $("countDocs").textContent = fmt(scan.documents.length);
    $("countPages").textContent = fmt(scan.pages.length);
    $("countIssues").textContent = fmt(scan.issues.length);
  }

  /* ---------------------------------------------------------------- tables */

  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      renderChips();
      renderDocuments();
      renderPages();
      renderIssues();
    });
  }

  function typeBadge(t) {
    const group = TYPE_GROUPS[t] || "other";
    return el("span", { class: `type t-${group}`, text: TYPE_LABEL[t] || (t || "?").toUpperCase() });
  }

  function renderChips() {
    const counts = new Map();
    for (const d of scan.documents) counts.set(d.doc_type, (counts.get(d.doc_type) || 0) + 1);
    const wrap = $("typeChips");
    const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const key = sorted.map(([t, n]) => `${t}:${n}`).join(",") + "|" + [...scan.filter.types].join(",");
    if (wrap.dataset.key === key) return;
    wrap.dataset.key = key;
    wrap.replaceChildren(...sorted.map(([t, n]) => {
      const b = el("button", { type: "button", class: "chip", "aria-pressed": String(scan.filter.types.has(t)),
        text: `${TYPE_LABEL[t] || t.toUpperCase()} ${fmt(n)}` });
      b.addEventListener("click", () => {
        if (scan.filter.types.has(t)) scan.filter.types.delete(t); else scan.filter.types.add(t);
        scan.docLimit = PAGE_SIZE;
        scheduleRender();
      });
      return b;
    }));
  }

  function filteredDocuments() {
    const { q, types, page } = scan.filter;
    const needle = q.toLowerCase();
    return scan.documents.filter((d) =>
      (!types.size || types.has(d.doc_type)) &&
      (!page || d.source_page === page) &&
      (!needle || (d.title || "").toLowerCase().includes(needle) || (d.final_url || "").toLowerCase().includes(needle)));
  }

  function renderDocuments() {
    const rows = filteredDocuments();
    const shown = rows.slice(0, scan.docLimit);
    $("docRows").replaceChildren(...shown.map((d) => {
      const tr = el("tr", { "data-url": d.final_url },
        el("td", {}, typeBadge(d.doc_type)),
        el("td", {}, el("div", { class: "cell-title", text: d.title || "Untitled" }),
          link(d.final_url, shortPath(d.final_url), "cell-url")),
        el("td", {}, d.source_page ? link(d.source_page, shortPath(d.source_page), "cell-src")
          : el("span", { class: "cell-muted", text: "Sitemap" })),
        el("td", { class: d.detected_by === "extension-unverified" ? "cell-muted unverified" : "cell-muted",
          title: DETECTED_HELP[d.detected_by] || "", text: DETECTED[d.detected_by] || d.detected_by }));
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        atlas.select(d.source_page || null);
      });
      return tr;
    }));
    const empty = $("docEmpty");
    if (!scan.documents.length) {
      empty.textContent = scan.status === "running" ? "No documents yet. They appear here as they're found."
        : scan.status === "stopped" ? "The scan was stopped before it found any documents."
        : "This scan found no documents. Try a deeper scan, or check the Issues tab for pages that failed.";
    } else if (!rows.length) {
      empty.textContent = "No documents match these filters.";
    } else {
      empty.textContent = "";
    }
    const more = $("docMore");
    more.hidden = rows.length <= shown.length;
    more.textContent = `Show more (${fmt(rows.length - shown.length)} left)`;
  }

  function renderPages() {
    const shown = scan.pages.slice(0, scan.pageLimit);
    $("pageRows").replaceChildren(...shown.map((p) => {
      const tr = el("tr", {},
        el("td", {}, el("div", { class: "cell-title", text: p.title || shortPath(p.url) }),
          link(p.url, shortPath(p.url), "cell-url")),
        el("td", {}, el("span", { class: "depth-pill", text: String(p.depth) })),
        el("td", { class: "num", text: fmt(p.links_found) }),
        el("td", { class: "num" }, String(p.documents_found),
          p.rendered ? el("span", { class: "page-flag", text: "rendered", title: "Read with a browser" }) : null));
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        setPageFilter(p.url);
        atlas.select(p.url);
        selectTab("documents");
      });
      return tr;
    }));
    $("pageEmpty").textContent = scan.pages.length ? "" :
      scan.status === "running" ? "Reading the first pages…" : "No pages could be read.";
    const more = $("pageMore");
    more.hidden = scan.pages.length <= shown.length;
    more.textContent = `Show more (${fmt(scan.pages.length - shown.length)} left)`;
  }

  function renderIssues() {
    const shown = scan.issues.slice(0, 1000);
    $("issueRows").replaceChildren(...shown.map((i) => el("tr", {},
      el("td", {}, link(i.url, shortPath(i.url), "cell-url")),
      el("td", {}, i.reason, el("span", { class: "issue-kind",
        text: i.kind === "robots" ? "Skipped on purpose" : "Couldn't use this address" })))));
    $("issueEmpty").textContent = scan.issues.length ? "" : "No problems so far.";
  }

  function eventText(e) {
    switch (e.type) {
      case "page": return `Read ${shortPath(e.url)} (${e.links} links, ${e.documents} files)`;
      case "document": return `Found ${(e.doc_type || "file").toUpperCase()}: ${e.title}`;
      case "failure": return `Couldn't use ${shortPath(e.url)}: ${e.reason}`;
      case "skipped": return `Skipped ${shortPath(e.url)} because robots.txt disallows it`;
      case "info": return e.message.charAt(0).toUpperCase() + e.message.slice(1);
      case "done": return `Finished: ${e.pages} pages, ${e.documents} documents`;
      default: return e.type;
    }
  }

  function appendActivity(events) {
    const list = $("activity");
    const frag = document.createDocumentFragment();
    for (const e of events) {
      frag.prepend(el("li", { class: `k-${e.type}` }, el("time", { text: duration(e.t) }), el("span", { text: eventText(e) })));
    }
    list.prepend(frag);
    while (list.children.length > 800) list.lastElementChild.remove();
  }

  function setPageFilter(url, rerender = true) {
    if (!scan) return;
    scan.filter.page = url || null;
    $("pageFilterNote").hidden = !url;
    if (url) $("pageFilterName").textContent = shortPath(url);
    scan.docLimit = PAGE_SIZE;
    if (url) selectTab("documents");
    if (rerender) scheduleRender();
  }

  function focusDocument(doc) {
    selectTab("documents");
    $("docSearch").value = doc.title || doc.final_url;
    scan.filter.q = $("docSearch").value;
    scheduleRender();
  }

  /* ---------------------------------------------------------------- tabs */

  const TABS = ["documents", "pages", "issues", "activity"];
  function selectTab(name, focus = false) {
    for (const t of TABS) {
      const tab = $(`tab-${t}`), on = t === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`panel-${t}`).hidden = !on;
      if (on && focus) tab.focus();
    }
  }
  function initTabs() {
    TABS.forEach((t, i) => {
      const tab = $(`tab-${t}`);
      tab.addEventListener("click", () => selectTab(t));
      tab.addEventListener("keydown", (e) => {
        const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (e.key === "Home") { e.preventDefault(); selectTab(TABS[0], true); }
        else if (e.key === "End") { e.preventDefault(); selectTab(TABS[TABS.length - 1], true); }
        else if (dir) { e.preventDefault(); selectTab(TABS[(i + dir + TABS.length) % TABS.length], true); }
      });
    });
  }

  /* ---------------------------------------------------------------- history */

  async function refreshHistory() {
    let data;
    try { data = await api("/api/scans"); } catch (e) { return; }
    const list = $("history");
    list.replaceChildren(...data.scans.map((s) => {
      const docs = s.summary && s.summary.documents !== undefined ? fmt(s.summary.documents) : "";
      const a = el("a", { href: `#/scan/${s.id}`, title: s.target },
        el("span", { class: `dot ${s.status}`, title: STATUS_TEXT[s.status] || s.status }),
        el("span", { class: "host", text: hostOf(s.target) }),
        el("span", { class: "when", text: s.status === "running" ? "Scanning now" :
          `${ago(s.started_at)}${docs ? `, ${docs} ${s.summary.documents === 1 ? "document" : "documents"}` : ""}` }));
      a.dataset.id = s.id;
      return el("li", {}, a);
    }));
    $("historyEmpty").hidden = data.scans.length > 0;
    markHistory(scan && scan.id);
  }

  function markHistory(id) {
    document.querySelectorAll("#history a").forEach((a) => {
      if (a.dataset.id === id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
  }

  /* ---------------------------------------------------------------- stop / delete */

  let deleteArmed = 0;
  function resetDelete() {
    clearTimeout(deleteArmed);
    deleteArmed = 0;
    const b = $("deleteBtn");
    b.textContent = "Delete";
    b.className = "btn btn-quiet";
  }

  async function onDelete() {
    const b = $("deleteBtn");
    if (!deleteArmed) {
      b.textContent = "Confirm delete";
      b.className = "btn btn-confirm";
      deleteArmed = setTimeout(resetDelete, 4000);
      return;
    }
    resetDelete();
    try {
      await api(`/api/scans/${scan.id}`, { method: "DELETE", body: {} });
      toast("Scan deleted.");
      location.hash = "#/";
      refreshHistory();
    } catch (e) { toast(e.message); }
  }

  async function onStop() {
    const b = $("stopBtn");
    b.disabled = true;
    b.textContent = "Stopping…";
    try { await api(`/api/scans/${scan.id}/stop`, { method: "POST", body: {} }); }
    catch (e) { toast(e.message); b.disabled = false; b.textContent = "Stop scan"; }
  }

  /* ---------------------------------------------------------------- wiring */

  function bind() {
    $("scanForm").addEventListener("submit", (e) => { e.preventDefault(); startScan(); });
    $("sectionsBtn").addEventListener("click", chooseSections);
    $("sectionsStart").addEventListener("click", () => {
      const seeds = [...$("sectionsList").querySelectorAll("input:checked")].map((i) => i.value);
      if (!seeds.length) { formError("Select at least one section, or start a normal scan."); return; }
      startScan(seeds);
    });
    $("sectionsCancel").addEventListener("click", () => { $("sectionsPanel").hidden = true; });
    $("sectionsAll").addEventListener("click", () => $("sectionsList").querySelectorAll("input").forEach((i) => { i.checked = true; }));
    $("sectionsNone").addEventListener("click", () => $("sectionsList").querySelectorAll("input").forEach((i) => { i.checked = false; }));
    $("newScanBtn").addEventListener("click", () => { location.hash = "#/"; $("urlInput").focus(); });
    $("stopBtn").addEventListener("click", onStop);
    $("deleteBtn").addEventListener("click", onDelete);
    $("zoomIn").addEventListener("click", () => atlas.zoom(1.3));
    $("zoomOut").addEventListener("click", () => atlas.zoom(1 / 1.3));
    $("zoomFit").addEventListener("click", () => atlas.fit());
    $("clearPageFilter").addEventListener("click", () => { atlas.select(null); setPageFilter(null); });
    let searchTimer = 0;
    $("docSearch").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { scan.filter.q = $("docSearch").value.trim(); scan.docLimit = PAGE_SIZE; scheduleRender(); }, 150);
    });
    $("docMore").addEventListener("click", () => { scan.docLimit += PAGE_SIZE; scheduleRender(); });
    $("pageMore").addEventListener("click", () => { scan.pageLimit += PAGE_SIZE; scheduleRender(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("sectionsPanel").hidden) $("sectionsPanel").hidden = true;
    });
    window.addEventListener("hashchange", route);
    // Keep "x min ago" fresh in the sidebar.
    setInterval(refreshHistory, 60000);
  }

  async function init() {
    try { info = await api("/api/info"); } catch (e) { toast(e.message); }
    $("version").textContent = info.version ? `v${info.version}` : "";
    initSettings();
    initTabs();
    bind();
    await refreshHistory();
    route();
  }

  init();
})();
