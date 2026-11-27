/* The atlas: a live map of a crawl, drawn on a canvas.

   Layout
   - The start page sits at the centre. Each link-depth is a dashed contour ring.
   - The circle is split into sectors, one per site section (first path segment, or the
     subdomain), sized by how many pages each section has.
   - Documents cluster around the page that links to them (a sunflower spiral), so a page
     that hosts many files visibly "blooms".
   - Nodes ease toward their positions as the layout grows; reduced-motion snaps instead.

   All text drawn here comes from crawled sites and is painted with fillText (never parsed as HTML). */
(function () {
  "use strict";

  const RING = 130;                  // world units between depth rings
  // Drawing every node of a huge scan would make the map unusably slow. Beyond these the map stops
  // adding nodes and says so; the tables and exports still have everything.
  const MAX_MAP_PAGES = 3000;
  const MAX_MAP_DOCS = 20000;
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function parse(url) {
    try { return new URL(url); } catch (e) { return null; }
  }

  function stripWww(host) { return host.startsWith("www.") ? host.slice(4) : host; }

  class Atlas {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.tooltip = opts.tooltip || null;
      this.onSelectPage = opts.onSelectPage || (() => {});
      this.onSelectDocument = opts.onSelectDocument || (() => {});
      this.view = { x: 0, y: 0, k: 1 };
      this.userMoved = false;
      this.colors = {};
      this._raf = 0;
      this._bindEvents();
      this._resizeObserver = new ResizeObserver(() => this._resize());
      this._resizeObserver.observe(canvas);
      this.reset(null);
    }

    /* ------------------------------------------------------------ data */

    reset(rootUrl) {
      this.root = rootUrl ? parse(rootUrl) : null;
      this.pages = new Map();     // url -> node
      this.docs = [];             // doc nodes
      this.docsByPage = new Map();
      this.sections = new Map();  // key -> { key, label, order, pages: [] }
      this.maxDepth = 0;
      this.truncated = false;     // true once the map stopped adding nodes
      this.selected = null;       // selected page url
      this.hover = null;
      this.flashUntil = new Map();
      this.lastFlashEnd = 0;      // latest flash end time, so frames don't scan every node
      this.userMoved = false;
      this.view = { x: 0, y: 0, k: 1 };
      this.idle = !rootUrl;
      this._refreshColors();
      this.fit(false);
      this._requestDraw(true);
    }

    _sectionOf(url) {
      const u = parse(url);
      if (!u || !this.root) return { key: "", label: "" };
      if (stripWww(u.hostname) !== stripWww(this.root.hostname)) {
        return { key: "host:" + u.hostname, label: u.hostname };
      }
      const seg = u.pathname.split("/").filter(Boolean)[0] || "";
      return { key: "path:" + seg.toLowerCase(), label: seg ? "/" + seg : "/" };
    }

    _isRoot(url) {
      if (!this.root) return false;
      const u = parse(url);
      return !!u && stripWww(u.hostname) === stripWww(this.root.hostname) &&
        (u.pathname === "/" || u.pathname === "") && !u.search;
    }

    addPages(pages) {
      for (const p of pages) {
        if (this.pages.has(p.url)) continue;
        if (this.pages.size >= MAX_MAP_PAGES) { this.truncated = true; break; }
        const sec = this._sectionOf(p.url);
        if (!this.sections.has(sec.key)) {
          this.sections.set(sec.key, { key: sec.key, label: sec.label, order: this.sections.size, pages: [] });
        }
        const node = {
          kind: "page", url: p.url, title: p.title || "", depth: p.depth || 0, foundOn: p.found_on || "",
          docsHere: 0, rendered: !!p.rendered, isRoot: this._isRoot(p.url), section: sec.key,
          x: 0, y: 0, tx: 0, ty: 0, born: performance.now(),
        };
        this.sections.get(sec.key).pages.push(node);
        this.pages.set(p.url, node);
        this.maxDepth = Math.max(this.maxDepth, node.depth);
        this.flashUntil.set(p.url, performance.now() + 1400);
        this.lastFlashEnd = performance.now() + 1400;
      }
      if (pages.length) { this.idle = false; this._layout(); }
    }

    addDocuments(docs) {
      for (const d of docs) {
        if (this.docs.length >= MAX_MAP_DOCS) { this.truncated = true; break; }
        const host = d.source_page && this.pages.has(d.source_page) ? d.source_page : null;
        const node = {
          kind: "doc", url: d.final_url || d.url, title: d.title || "", type: d.doc_type || "",
          unverified: d.detected_by === "extension-unverified", page: host, raw: d,
          x: 0, y: 0, tx: 0, ty: 0, born: performance.now(),
        };
        this.docs.push(node);
        const key = host || "__orphans__";
        if (!this.docsByPage.has(key)) this.docsByPage.set(key, []);
        this.docsByPage.get(key).push(node);
        if (host) this.pages.get(host).docsHere += 1;
      }
      if (docs.length) { this.idle = false; this._layout(); }
    }

    /* Documents can arrive before the page that links to them (documents are checked first).
       Re-home them once their page shows up. */
    _rehomeOrphans() {
      const orphans = this.docsByPage.get("__orphans__");
      if (!orphans || !orphans.length) return;
      const still = [];
      for (const d of orphans) {
        const src = d.raw.source_page;
        if (src && this.pages.has(src)) {
          d.page = src;
          if (!this.docsByPage.has(src)) this.docsByPage.set(src, []);
          this.docsByPage.get(src).push(d);
          this.pages.get(src).docsHere += 1;
        } else {
          still.push(d);
        }
      }
      this.docsByPage.set("__orphans__", still);
    }

    _layout() {
      this._rehomeOrphans();
      // A section holding only the start page gets no slice of its own: the start page is the centre.
      const sections = [...this.sections.values()]
        .filter((s) => s.pages.some((p) => !p.isRoot))
        .sort((a, b) => a.order - b.order);
      const weights = sections.map((s) => Math.max(1, s.pages.filter((p) => !p.isRoot).length));
      const total = weights.reduce((a, b) => a + b, 0) || 1;
      const gap = sections.length > 1 ? 0.04 : 0;
      let angle = -Math.PI / 2;

      for (let i = 0; i < sections.length; i++) {
        const sec = sections[i];
        const span = (Math.PI * 2) * (weights[i] / total) - gap;
        sec.a0 = angle + gap / 2;
        sec.a1 = sec.a0 + Math.max(span, 0.02);
        angle += (Math.PI * 2) * (weights[i] / total);

        const byDepth = new Map();
        for (const p of sec.pages) {
          if (p.isRoot) { p.tx = 0; p.ty = 0; continue; }
          const d = p.depth;
          if (!byDepth.has(d)) byDepth.set(d, []);
          byDepth.get(d).push(p);
        }
        for (const [depth, list] of byDepth) {
          list.sort((a, b) => (a.url < b.url ? -1 : 1));
          const r = depth === 0 ? RING * 0.55 : RING * depth;
          list.forEach((p, j) => {
            const t = (j + 1) / (list.length + 1);
            const a = sec.a0 + (sec.a1 - sec.a0) * t;
            p.tx = Math.cos(a) * r;
            p.ty = Math.sin(a) * r;
          });
        }
      }

      // Document blooms around their page; orphans (e.g. from the sitemap) around the centre.
      for (const [key, docs] of this.docsByPage) {
        const anchor = key === "__orphans__" ? { tx: 0, ty: 0 } : this.pages.get(key);
        if (!anchor) continue;
        const spacing = docs.length > 200 ? 2.2 : docs.length > 40 ? 3 : 4.2;
        docs.forEach((d, k) => {
          const a = k * GOLDEN;
          const r = 9 + spacing * Math.sqrt(k + 1);
          d.tx = anchor.tx + Math.cos(a) * r;
          d.ty = anchor.ty + Math.sin(a) * r;
        });
      }

      // New nodes start at their parent (or the centre) so they visibly grow out of it.
      const now = performance.now();
      for (const p of this.pages.values()) {
        if (p.placed) continue;
        const parent = this.pages.get(p.foundOn);
        p.x = parent && parent.placed ? parent.x : 0;
        p.y = parent && parent.placed ? parent.y : 0;
        p.placed = true;
        p.born = now;
      }
      for (const d of this.docs) {
        if (d.placed) continue;
        const parent = d.page ? this.pages.get(d.page) : null;
        d.x = parent ? parent.x : 0;
        d.y = parent ? parent.y : 0;
        d.placed = true;
      }
      if (!this.userMoved) this.fit(false);
      this._requestDraw(true);
    }

    /* ------------------------------------------------------------ selection */

    select(url) {
      this.selected = url && this.pages.has(url) ? url : null;
      this._requestDraw();
    }

    /* ------------------------------------------------------------ view */

    _bounds() {
      let minX = -RING * 0.8, maxX = RING * 0.8, minY = -RING * 0.8, maxY = RING * 0.8;
      const all = [...this.pages.values(), ...this.docs];
      for (const n of all) {
        minX = Math.min(minX, n.tx); maxX = Math.max(maxX, n.tx);
        minY = Math.min(minY, n.ty); maxY = Math.max(maxY, n.ty);
      }
      const ring = RING * Math.max(1, this.maxDepth) + 40;
      return {
        minX: Math.min(minX, -ring), maxX: Math.max(maxX, ring),
        minY: Math.min(minY, -ring), maxY: Math.max(maxY, ring),
      };
    }

    fit(userAction = true) {
      if (userAction) this.userMoved = false;
      const { w, h } = this._size();
      if (!w || !h) return;
      const b = this._bounds();
      const padX = 78, padY = 40;   // room for section labels beside the outer ring
      const k = Math.min((w - padX * 2) / (b.maxX - b.minX), (h - padY * 2) / (b.maxY - b.minY));
      this.view.k = Math.max(0.15, Math.min(k, 2.2));
      this.view.x = w / 2 - ((b.minX + b.maxX) / 2) * this.view.k;
      this.view.y = h / 2 - ((b.minY + b.maxY) / 2) * this.view.k;
      this._requestDraw();
    }

    zoom(factor, cx, cy) {
      const { w, h } = this._size();
      cx = cx ?? w / 2; cy = cy ?? h / 2;
      const k = Math.max(0.1, Math.min(this.view.k * factor, 8));
      const f = k / this.view.k;
      this.view.x = cx - (cx - this.view.x) * f;
      this.view.y = cy - (cy - this.view.y) * f;
      this.view.k = k;
      this.userMoved = true;
      this._requestDraw();
    }

    _size() {
      const r = this.canvas.getBoundingClientRect();
      return { w: r.width, h: r.height };
    }

    _resize() {
      const { w, h } = this._size();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.dpr = dpr;
      if (!this.userMoved) this.fit(false);
      this._requestDraw();
    }

    _refreshColors() {
      this.colors = {
        ink: css("--ink"), soft: css("--ink-soft"), rule: css("--rule"), contour: css("--contour"),
        sea: css("--sea"), amber: css("--amber"), sheet: css("--sheet"), red: css("--red"),
      };
    }

    toScreen(x, y) { return [x * this.view.k + this.view.x, y * this.view.k + this.view.y]; }
    toWorld(sx, sy) { return [(sx - this.view.x) / this.view.k, (sy - this.view.y) / this.view.k]; }

    /* ------------------------------------------------------------ drawing */

    _requestDraw(animate = false) {
      if (animate) this._animating = true;
      if (!this._raf) this._raf = requestAnimationFrame(() => this._frame());
    }

    _frame() {
      this._raf = 0;
      let moving = false;
      if (this._animating) {
        const snap = reduceMotion.matches;
        const step = (n) => {
          const dx = n.tx - n.x, dy = n.ty - n.y;
          if (snap || Math.abs(dx) + Math.abs(dy) < 0.3) { n.x = n.tx; n.y = n.ty; return; }
          n.x += dx * 0.16; n.y += dy * 0.16; moving = true;
        };
        this.pages.forEach(step);
        this.docs.forEach(step);
        if (!moving) this._animating = false;
      }
      this._draw();
      if (moving || this.lastFlashEnd > performance.now()) this._requestDraw();
    }

    _draw() {
      const ctx = this.ctx, c = this.colors, k = this.view.k;
      const { w, h } = this._size();
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(this.view.x, this.view.y);
      ctx.scale(k, k);

      // Contour rings, one per depth.
      const rings = this.idle ? 3 : Math.max(1, this.maxDepth);
      ctx.lineWidth = 1 / k;
      ctx.setLineDash([4 / k, 5 / k]);
      ctx.strokeStyle = c.contour;
      for (let d = 1; d <= rings; d++) {
        ctx.beginPath(); ctx.arc(0, 0, RING * d, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.setLineDash([]);
      if (!this.idle) {
        ctx.fillStyle = c.soft;
        ctx.font = `500 ${11 / k}px Archivo, sans-serif`;
        ctx.textAlign = "left";
        for (let d = 1; d <= rings; d++) ctx.fillText(`depth ${d}`, 6 / k, -RING * d - 5 / k);
      }

      if (this.idle) { ctx.restore(); return; }

      // Sector dividers and labels.
      const sections = [...this.sections.values()];
      const outer = RING * Math.max(1, this.maxDepth) + 28;
      const placed = sections.filter((s) => s.a0 !== undefined && s.pages.some((p) => !p.isRoot));
      if (placed.length > 1) {
        // Short ticks just outside the outer ring mark where one section ends and the next begins.
        ctx.strokeStyle = c.contour;
        ctx.lineWidth = 1.5 / k;
        for (const s of placed) {
          ctx.beginPath();
          ctx.moveTo(Math.cos(s.a0) * (outer - 14), Math.sin(s.a0) * (outer - 14));
          ctx.lineTo(Math.cos(s.a0) * (outer + 2), Math.sin(s.a0) * (outer + 2));
          ctx.stroke();
        }
      }
      ctx.font = `600 ${12 / k}px Archivo, sans-serif`;
      ctx.fillStyle = c.ink;
      for (const s of placed) {
        if ((s.a1 - s.a0) * outer * k < 34) continue;   // too thin to label
        const mid = (s.a0 + s.a1) / 2;
        const lx = Math.cos(mid) * (outer + 10), ly = Math.sin(mid) * (outer + 10);
        ctx.textAlign = Math.cos(mid) > 0.25 ? "left" : Math.cos(mid) < -0.25 ? "right" : "center";
        ctx.textBaseline = "middle";
        ctx.fillText(truncate(s.label, 28), lx, ly);
      }
      ctx.textBaseline = "alphabetic";

      // Links between pages (found-on -> page).
      ctx.strokeStyle = "rgba(36, 105, 138, 0.22)";
      ctx.lineWidth = 1 / k;
      for (const p of this.pages.values()) {
        const parent = this.pages.get(p.foundOn);
        if (!parent) continue;
        ctx.beginPath();
        ctx.moveTo(parent.x, parent.y);
        ctx.quadraticCurveTo((parent.x + p.x) * 0.42, (parent.y + p.y) * 0.42, p.x, p.y);
        ctx.stroke();
      }

      // Documents.
      const ds = Math.max(2.6, 3.4 / Math.sqrt(k));
      const selected = this.selected;
      for (const d of this.docs) {
        const dim = selected && d.page !== selected;
        ctx.globalAlpha = dim ? 0.25 : 1;
        ctx.save();
        ctx.translate(d.x, d.y);
        ctx.rotate(Math.PI / 4);
        if (d.unverified) {
          ctx.strokeStyle = c.amber; ctx.lineWidth = 1.2 / k;
          ctx.strokeRect(-ds / 2, -ds / 2, ds, ds);
        } else {
          ctx.fillStyle = c.amber;
          ctx.fillRect(-ds / 2, -ds / 2, ds, ds);
        }
        ctx.restore();
      }
      ctx.globalAlpha = 1;

      // Pages.
      const now = performance.now();
      for (const p of this.pages.values()) {
        const r = p.isRoot ? 9 : 3.5 + Math.min(6, Math.sqrt(p.docsHere) * 1.1);
        const dim = selected && selected !== p.url && !p.isRoot;
        ctx.globalAlpha = dim ? 0.35 : 1;
        if ((this.flashUntil.get(p.url) || 0) > now && !reduceMotion.matches) {
          const t = 1 - ((this.flashUntil.get(p.url) - now) / 1400);
          ctx.beginPath(); ctx.arc(p.x, p.y, r + (4 + 10 * t) / k, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(212, 149, 26, ${0.35 * (1 - t)})`; ctx.fill();
        }
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fillStyle = p.isRoot ? c.sea : c.ink;
        ctx.fill();
        if (p.rendered) {
          ctx.beginPath(); ctx.arc(p.x, p.y, r + 2.5 / k, 0, Math.PI * 2);
          ctx.strokeStyle = c.sea; ctx.lineWidth = 1.2 / k; ctx.stroke();
        }
        if (selected === p.url) {
          ctx.beginPath(); ctx.arc(p.x, p.y, r + 5 / k, 0, Math.PI * 2);
          ctx.strokeStyle = c.amber; ctx.lineWidth = 2.5 / k; ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;

      // Label for the hovered or selected page.
      const labelled = this.hover && this.hover.kind === "page" ? this.hover : this.pages.get(selected);
      if (labelled) {
        const u = parse(labelled.url);
        const text = truncate(u ? u.pathname + u.search : labelled.url, 48);
        ctx.font = `600 ${12 / k}px Archivo, sans-serif`;
        ctx.textAlign = "left";
        const tw = ctx.measureText(text).width;
        const x = labelled.x + 10 / k, y = labelled.y - 10 / k;
        ctx.fillStyle = "rgba(246, 248, 250, 0.92)";
        ctx.fillRect(x - 4 / k, y - 13 / k, tw + 8 / k, 18 / k);
        ctx.fillStyle = c.ink;
        ctx.fillText(text, x, y);
      }
      ctx.restore();
    }

    /* ------------------------------------------------------------ interaction */

    _hit(sx, sy) {
      const [wx, wy] = this.toWorld(sx, sy);
      const tol = 8 / this.view.k;
      let best = null, bestD = Infinity;
      for (const p of this.pages.values()) {
        const r = (p.isRoot ? 9 : 6) + tol;
        const d = Math.hypot(p.x - wx, p.y - wy);
        if (d < r && d < bestD) { best = p; bestD = d; }
      }
      if (best) return best;
      const dt = 5 / this.view.k;
      for (const d of this.docs) {
        const dist = Math.hypot(d.x - wx, d.y - wy);
        if (dist < dt && dist < bestD) { best = d; bestD = dist; }
      }
      return best;
    }

    _bindEvents() {
      const cv = this.canvas;
      let drag = null;

      cv.addEventListener("pointerdown", (e) => {
        drag = { x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y, moved: false };
        cv.setPointerCapture(e.pointerId);
      });
      cv.addEventListener("pointermove", (e) => {
        const r = cv.getBoundingClientRect();
        const sx = e.clientX - r.left, sy = e.clientY - r.top;
        if (drag) {
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
          if (drag.moved) {
            this.view.x = drag.vx + dx; this.view.y = drag.vy + dy;
            this.userMoved = true;
            cv.classList.add("dragging");
            this._hideTooltip();
            this._requestDraw();
            return;
          }
        }
        const hit = this._hit(sx, sy);
        if (hit !== this.hover) { this.hover = hit; this._requestDraw(); }
        cv.classList.toggle("pointing", !!hit);
        if (hit) this._showTooltip(hit, sx, sy); else this._hideTooltip();
      });
      const end = (e) => {
        if (drag && !drag.moved) {
          const r = cv.getBoundingClientRect();
          const hit = this._hit(e.clientX - r.left, e.clientY - r.top);
          if (hit && hit.kind === "page") {
            const url = this.selected === hit.url ? null : hit.url;
            this.select(url);
            this.onSelectPage(url);
          } else if (hit && hit.kind === "doc") {
            this.onSelectDocument(hit.raw);
          } else if (this.selected) {
            this.select(null);
            this.onSelectPage(null);
          }
        }
        drag = null;
        cv.classList.remove("dragging");
      };
      cv.addEventListener("pointerup", end);
      cv.addEventListener("pointercancel", () => { drag = null; cv.classList.remove("dragging"); });
      cv.addEventListener("pointerleave", () => {
        if (this.hover) { this.hover = null; this._requestDraw(); }
        this._hideTooltip();
      });
      cv.addEventListener("wheel", (e) => {
        e.preventDefault();
        const r = cv.getBoundingClientRect();
        this.zoom(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
      }, { passive: false });
    }

    _showTooltip(node, sx, sy) {
      const tip = this.tooltip;
      if (!tip) return;
      tip.replaceChildren();
      const strong = document.createElement("strong");
      const line = document.createElement("span");
      if (node.kind === "page") {
        const u = parse(node.url);
        strong.textContent = node.title || (u ? u.pathname : node.url);
        const files = `${node.docsHere} ${node.docsHere === 1 ? "file" : "files"}`;
        line.textContent = `${u ? u.hostname + u.pathname : node.url}\nDepth ${node.depth}, ${files}` +
          (node.rendered ? ", read with a browser" : "");
      } else {
        strong.textContent = node.title;
        line.textContent = (node.type || "file").toUpperCase() + (node.unverified ? ", not confirmed" : "");
      }
      tip.append(strong, line);
      tip.hidden = false;
      const { w } = this._size();
      const left = Math.min(sx + 14, w - tip.offsetWidth - 8);
      tip.style.left = `${Math.max(8, left)}px`;
      tip.style.top = `${sy + 16}px`;
    }

    _hideTooltip() { if (this.tooltip) this.tooltip.hidden = true; }
  }

  function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  Atlas.MAX_MAP_PAGES = MAX_MAP_PAGES;
  Atlas.MAX_MAP_DOCS = MAX_MAP_DOCS;
  window.Atlas = Atlas;
})();
