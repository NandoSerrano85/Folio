// lightbox.js — full-image viewer with an archival metadata panel.
//
// Navigates the ENTIRE filtered result set (not just the current page): the
// index is global (0..total-1) and neighboring pages are fetched on demand and
// cached locally so prev/next never disturb the grid's page state.
// Keyboard: Esc closes, ←/→ navigate. Image uses the original file (file_url).

import { el, clear, icons, vendorColor, gradientFor, fmtLong, fmtBytes, isHttpUrl } from "./util.js";
import { state, setState, subscribe, imageQuery, toast } from "./state.js";
import * as api from "./api.js";
import { getImageFolders } from "./folders.js";
import { openSingleEdit } from "./edit.js";

let initialized = false;
let overlay = null;
const ctx = { index: -1, pageCache: new Map() };

/** Open the lightbox at a global index into the current filtered list. */
export function openLightbox(globalIndex) {
  ctx.pageCache = new Map();
  ctx.pageCache.set(state.page, state.items);
  setState({ lightbox: { index: globalIndex }, userMenuOpen: false });
}

export function closeLightbox() {
  setState({ lightbox: null });
}

/**
 * Re-render the open lightbox from fresh data. Called by the Edit modal after a
 * save so the vendor / collections shown here stay in sync. The page cache is
 * reset (vendor or membership may have changed) and reseeded with the current
 * grid page so neighbouring pages re-fetch lazily on the next navigation.
 */
export function refreshLightbox() {
  if (!state.lightbox || ctx.index < 0) return;
  ctx.pageCache = new Map();
  ctx.pageCache.set(state.page, state.items);
  show(ctx.index);
}

function init() {
  if (initialized) return;
  initialized = true;
  subscribe((s, keys) => {
    if (!keys.has("lightbox")) return;
    if (s.lightbox == null) teardown();
    else show(s.lightbox.index);
  });
  document.addEventListener("keydown", (e) => {
    // While the Edit modal is open (on top of the lightbox) it owns the keyboard.
    if (!state.lightbox || state.edit) return;
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowRight") nav(1);
    else if (e.key === "ArrowLeft") nav(-1);
  });
}
init();

function teardown() {
  if (overlay) { overlay.remove(); overlay = null; }
  ctx.index = -1;
}

function nav(delta) {
  const N = state.total;
  const next = Math.max(0, Math.min(ctx.index + delta, N - 1));
  if (next !== ctx.index) show(next);
}

// Resolve the image at a global index, fetching/caching its page if needed.
async function itemAt(globalIndex) {
  const ps = state.pageSize;
  const pageNum = Math.floor(globalIndex / ps) + 1;
  const offset = (pageNum - 1) * ps;
  const localIdx = globalIndex - offset;
  if (ctx.pageCache.has(pageNum)) {
    return ctx.pageCache.get(pageNum)[localIdx] || null;
  }
  try {
    const res = await api.listImages(imageQuery({ page: pageNum }));
    ctx.pageCache.set(pageNum, res.items || []);
    return (res.items || [])[localIdx] || null;
  } catch (_) {
    return null;
  }
}

async function show(globalIndex) {
  const N = state.total;
  if (N === 0) { closeLightbox(); return; }
  const g = Math.max(0, Math.min(globalIndex, N - 1));
  ctx.index = g;

  const item = await itemAt(g);
  if (ctx.index !== g) return; // navigated away while loading
  if (!item) { closeLightbox(); return; }

  renderShell(item, g, N);

  // Enrich with detail + folder membership.
  let detail = null;
  let membership = new Set();
  try {
    [detail, membership] = await Promise.all([
      api.imageDetail(item.id),
      getImageFolders(item, state.foldersFlat),
    ]);
  } catch (_) { /* keep shell */ }
  if (ctx.index !== g) return;
  renderPanel(item, detail, membership);
}

function ensureOverlay() {
  if (overlay) return;
  overlay = el("div", {
    class: "lb-overlay",
    onClick: (e) => { if (e.target === overlay) closeLightbox(); },
  });
  document.body.appendChild(overlay);
}

function renderShell(item, g, N) {
  ensureOverlay();
  clear(overlay);

  const stage = el("div", { class: "lb-stage" });
  stage.appendChild(el("div", { class: "lb-bg", style: { background: gradientFor(item.id) } }));
  stage.appendChild(el("div", {
    class: "lb-img",
    style: { backgroundImage: `url("${api.fileUrl(item.id)}")` },
  }));

  if (g > 0) {
    const prev = el("button", { class: "lb-nav lb-prev", onClick: () => nav(-1) });
    prev.appendChild(icons.arrowLeft(17));
    stage.appendChild(prev);
  }
  if (g < N - 1) {
    const next = el("button", { class: "lb-nav lb-next", onClick: () => nav(1) });
    next.appendChild(icons.arrowRight(17));
    stage.appendChild(next);
  }
  stage.appendChild(el("div", { class: "lb-pos", text: `${g + 1} of ${N}` }));

  const panel = el("div", { class: "lb-panel" });
  panel.appendChild(el("div", { class: "lb-scroll", id: "lb-scroll" }, [
    el("div", { class: "lb-filename", text: item.filename || "Untitled" }),
    el("div", { class: "loading-note", style: { textAlign: "left", padding: "24px 0" }, text: "Loading details…" }),
  ]));

  const modal = el("div", { class: "lb-modal" }, [stage, panel]);
  const close = el("button", { class: "lb-close", onClick: () => closeLightbox() });
  close.appendChild(icons.x(15));
  modal.appendChild(close);

  overlay.appendChild(modal);
  ctx.panelEl = panel;
}

function accountTypeLabel(sourceType) {
  return sourceType === "email" ? "Gmail" : sourceType === "drive" ? "Google Drive" : (sourceType || "");
}

function renderPanel(item, detail, membership) {
  const panel = ctx.panelEl;
  if (!panel) return;
  clear(panel);

  const sources = (detail && detail.sources) || [];
  const primary = sources.find((s) => s.vendor) || sources[0] || null;
  const vendorName = (primary && primary.vendor) || item.vendor || null;
  const vendorUrl = (sources.find((s) => s.vendor_url) || {}).vendor_url || null;

  const scroll = el("div", { class: "lb-scroll" });

  scroll.appendChild(el("div", { class: "lb-filename", text: (detail && detail.filename) || item.filename || "Untitled" }));

  // True source date
  const dateBlock = el("div", { class: "lb-section" });
  dateBlock.appendChild(el("div", { class: "lb-eyebrow", text: "True source date" }));
  dateBlock.appendChild(el("div", { class: "lb-truedate", text: fmtLong((detail && detail.source_date) || item.source_date) }));
  const ingested = detail && detail.ingested_at;
  if (ingested) dateBlock.appendChild(el("div", { class: "lb-added", text: `Added to library · ${fmtLong(ingested)}` }));
  scroll.appendChild(dateBlock);

  scroll.appendChild(el("div", { class: "lb-sep" }));

  // Source account
  const acctEmail = (primary && primary.account) || item.account || null;
  if (acctEmail) {
    const block = el("div", { class: "lb-block" });
    block.appendChild(el("div", { class: "lb-eyebrow", text: "Source account" }));
    const row = el("div", { class: "lb-rowflex" }, [el("span", { class: "lb-acctname", text: acctEmail })]);
    if (primary && primary.source_type) row.appendChild(el("span", { class: "tag", style: { marginTop: "0" }, text: accountTypeLabel(primary.source_type) }));
    block.appendChild(row);
    scroll.appendChild(block);
  }

  // Email From/subject OR Drive path
  if (primary && primary.source_type === "email" && (primary.email_sender || primary.email_subject)) {
    const block = el("div", { class: "lb-block" });
    block.appendChild(el("div", { class: "lb-eyebrow", text: "From" }));
    if (primary.email_sender) block.appendChild(el("div", { class: "lb-mono", text: primary.email_sender }));
    if (primary.email_subject) block.appendChild(el("div", { class: "lb-subject", text: `“${primary.email_subject}”` }));
    scroll.appendChild(block);
  } else if (primary && primary.source_type === "drive" && primary.drive_folder_path) {
    const block = el("div", { class: "lb-block" });
    block.appendChild(el("div", { class: "lb-eyebrow", text: "Drive path" }));
    block.appendChild(el("div", { class: "lb-mono", text: primary.drive_folder_path }));
    scroll.appendChild(block);
  }

  // Vendor
  if (vendorName) {
    const block = el("div", { class: "lb-block" });
    block.appendChild(el("div", { class: "lb-eyebrow", text: "Vendor" }));
    block.appendChild(el("div", { class: "lb-rowflex" }, [
      el("span", { class: "lb-vendor-dot", style: { background: vendorColor(vendorName) } }),
      el("span", { class: "lb-vendorname", text: vendorName }),
    ]));
    scroll.appendChild(block);
  }

  // File facts (size/dimensions) — extra archival detail when available.
  const facts = [];
  if (detail && detail.width && detail.height) facts.push(`${detail.width}×${detail.height}`);
  if (detail && detail.bytes) facts.push(fmtBytes(detail.bytes));
  if (facts.length) {
    const block = el("div", { class: "lb-block" });
    block.appendChild(el("div", { class: "lb-eyebrow", text: "File" }));
    block.appendChild(el("div", { class: "lb-mono", text: facts.join(" · ") }));
    scroll.appendChild(block);
  }

  // In collections
  const chipsBlock = el("div", { style: { marginBottom: "6px" } });
  chipsBlock.appendChild(el("div", { class: "lb-eyebrow", style: { marginBottom: "7px" }, text: "In collections" }));
  const names = state.foldersFlat.filter((f) => membership.has(f.id)).map((f) => f.name);
  if (names.length) {
    const chips = el("div", { class: "chips" });
    names.forEach((n) => chips.appendChild(el("span", { class: "chip", text: n })));
    chipsBlock.appendChild(chips);
  } else {
    chipsBlock.appendChild(el("span", { class: "lb-none", text: "Not in any collection yet." }));
  }
  scroll.appendChild(chipsBlock);

  // Footer actions
  const footer = el("div", { class: "lb-footer" });

  const dlBtn = el("button", { class: "btn-primary btn-lb-download", onClick: () => download(item) });
  dlBtn.appendChild(icons.download(14));
  dlBtn.appendChild(document.createTextNode("Download"));

  const actions = el("div", { class: "lb-actions" }, [dlBtn]);
  if (vendorUrl && isHttpUrl(vendorUrl)) {
    const vBtn = el("button", {
      class: "btn-ghost btn-lb-vendor",
      onClick: () => window.open(vendorUrl, "_blank", "noopener"),
    }, [document.createTextNode("Open at vendor "), el("span", { style: { fontSize: "13px" }, text: "↗" })]);
    actions.appendChild(vBtn);
  }
  footer.appendChild(actions);

  // Edit details → opens the SINGLE editor for this image (on top of the
  // lightbox). On save the editor calls refreshLightbox() to re-sync this panel.
  const editBtn = el("button", { class: "btn-ghost btn-lb-edit", onClick: () => openSingleEdit(item.id) });
  editBtn.appendChild(icons.pencil(14));
  editBtn.appendChild(document.createTextNode("Edit details"));
  footer.appendChild(editBtn);

  panel.appendChild(scroll);
  panel.appendChild(footer);
}

async function download(item) {
  toast("Preparing download…");
  try {
    await api.downloadImages([item.id]);
    toast(`Saved ${item.filename || "image"}.`);
  } catch (_) {
    toast("Download failed.");
  }
}
