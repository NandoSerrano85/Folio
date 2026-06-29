// gallery.js — the Library view: filter sidebar + main grid/toolbar/pagination.
//
// Server-side pagination/sort/filter via /api/images. Sidebar counts are
// LIBRARY TOTALS (folders/accounts from their endpoints, vendor counts joined
// from /api/stats) — they are not re-filtered, matching the mockup.
//
// API limitation honored deliberately: /api/images accepts a SINGLE account and
// a SINGLE vendor id, so the account/vendor checkboxes behave as single-select
// with an "all" default (all boxes checked = no filter). See fidelity notes.

import {
  el, clear, icons, checkSvg, vendorColor, gradientFor, fmtMedium, isHttpUrl,
} from "./util.js";
import {
  state, setState, subscribe, imageQuery, filtersActive, resetFilters,
  PAGE_SIZES, toast,
} from "./state.js";
import * as api from "./api.js";
import { flattenFolders } from "./folders.js";
import { openLightbox } from "./lightbox.js";

const QUERY_KEYS = ["folder", "account", "vendor", "search", "dateFrom", "dateTo", "sort", "pageSize", "page"];
const SIDEBAR_KEYS = ["foldersFlat", "accountsList", "vendorsList", "vendorCountByName", "statsTotal", "folder", "account", "vendor", "datePreset", "dateFrom", "dateTo", "search"];

let _reqId = 0;
let mounted = false;

const View = {
  root: null,
  sidebarEl: null,
  mainEl: null,
  tileEls: new Map(),
};

/** Fetch all reference data (folders, accounts, vendors, stats) into state. */
export async function loadReference() {
  try {
    const [folders, accts, vends, st] = await Promise.all([
      api.folders(), api.accounts(), api.vendors(), api.stats(),
    ]);
    const vmap = {};
    (st.by_vendor || []).forEach((r) => { vmap[r.name] = r.count; });
    setState({
      foldersFlat: flattenFolders(folders),
      accountsList: accts || [],
      vendorsList: vends || [],
      vendorCountByName: vmap,
      statsTotal: st.total_images || 0,
    });
  } catch (e) {
    toast("Could not load library data.");
  }
}

/** Build (once) and return the Library root element. */
export function mountLibrary() {
  if (!View.root) build();
  if (!mounted) {
    mounted = true;
    subscribe(onChange);
    loadReference();
    refresh();
  }
  return View.root;
}

function build() {
  View.sidebarEl = el("aside", { class: "sidebar" });
  View.mainEl = el("main", { class: "main" });
  View.root = el("div", { class: "library" }, [View.sidebarEl, View.mainEl]);
  renderSidebar();
  renderMain();
}

function onChange(s, keys) {
  if (!View.root) return;
  if (intersect(keys, QUERY_KEYS)) refresh();
  if (intersect(keys, SIDEBAR_KEYS)) renderSidebar();
  if (keys.has("items") || keys.has("loading") || keys.has("total")) renderMain();
  if (keys.has("selected")) updateSelection();
}

function intersect(set, list) {
  for (const k of list) if (set.has(k)) return true;
  return false;
}

// ----------------------------------------------------------------- fetch -- //
async function refresh() {
  const id = ++_reqId;
  setState({ loading: true });
  try {
    const res = await api.listImages(imageQuery());
    if (id !== _reqId) return; // stale
    const page = res.page || 1;
    setState({
      items: res.items || [],
      total: res.total || 0,
      pages: res.pages || 0,
      loading: false,
      ...(page !== state.page ? { page } : {}),
    });
  } catch (e) {
    if (id !== _reqId) return;
    setState({ items: [], total: 0, pages: 0, loading: false });
    toast("Could not load images.");
  }
}

// --------------------------------------------------------------- sidebar -- //
function renderSidebar() {
  const aside = View.sidebarEl;
  if (!aside) return;
  clear(aside);

  // Collections
  aside.appendChild(el("div", { class: "side-eyebrow", text: "Collections" }));
  const collList = el("div", { class: "side-list" });
  collList.appendChild(folderRow({ id: null, name: "All Images", count: state.statsTotal, depth: 0 }));
  for (const f of state.foldersFlat) {
    collList.appendChild(folderRow({ id: f.id, name: f.name, count: f.image_count, depth: f.depth }));
  }
  // "+ New collection" — sits at the foot of the Collections list and reuses the
  // folder-row affordance (same hover/padding/radius) so it reads as part of the
  // list. Muted text marks it as an action rather than a real collection.
  collList.appendChild(el("button", {
    class: "folder-row add-collection",
    onClick: createCollection,
  }, [el("span", { class: "folder-name", style: { color: "var(--muted)" }, text: "+ New collection" })]));
  aside.appendChild(collList);

  // Source account
  aside.appendChild(el("div", { class: "side-sep" }));
  aside.appendChild(el("div", { class: "side-eyebrow", text: "Source account" }));
  const gmail = state.accountsList.filter((a) => a.provider === "gmail");
  const drive = state.accountsList.filter((a) => a.provider === "drive");
  aside.appendChild(el("div", { class: "side-subhead", text: "Gmail" }));
  aside.appendChild(accountGroup(gmail));
  aside.appendChild(el("div", { class: "side-subhead", text: "Google Drive" }));
  aside.appendChild(accountGroup(drive));

  // Vendor
  aside.appendChild(el("div", { class: "side-sep" }));
  aside.appendChild(el("div", { class: "side-eyebrow", text: "Vendor" }));
  const venList = el("div", { class: "side-list" });
  for (const v of state.vendorsList) {
    const checked = state.vendor == null || state.vendor === v.id;
    const row = el("div", {
      class: "opt-row",
      onClick: () => toggleVendor(v.id),
    }, [
      checkbox(checked),
      el("span", { class: "vendor-dot", style: { background: vendorColor(v.name) } }),
      el("span", { class: "opt-name", text: v.name }),
      el("span", { class: "opt-count", text: String(state.vendorCountByName[v.name] || 0) }),
    ]);
    venList.appendChild(row);
  }
  aside.appendChild(venList);

  // Date range
  aside.appendChild(el("div", { class: "side-sep" }));
  aside.appendChild(el("div", { class: "side-eyebrow", text: "Date range" }));
  aside.appendChild(presetRow());
  aside.appendChild(dateRow());

  // Reset
  if (filtersActive()) {
    aside.appendChild(el("button", {
      class: "reset-link", text: "Reset all filters", onClick: () => resetFilters(),
    }));
  }
}

function folderRow({ id, name, count, depth }) {
  const active = state.folder === id;
  const children = [];
  if (active) {
    children.push(el("span", { class: "folder-fill" }));
    children.push(el("span", { class: "folder-bar" }));
  }
  children.push(el("span", { class: "folder-name", text: name }));
  children.push(el("span", { class: "folder-count", text: String(count ?? 0) }));
  return el("button", {
    class: `folder-row${active ? " active" : ""}${depth ? ` depth-${Math.min(depth, 2)}` : ""}`,
    onClick: () => setFolder(id),
  }, children);
}

function accountGroup(list) {
  const wrap = el("div", { class: "side-group" });
  if (!list.length) {
    wrap.appendChild(el("div", { class: "opt-row", style: { cursor: "default" } }, [
      el("span", { class: "opt-name", style: { color: "var(--muted)" }, text: "None connected" }),
    ]));
    return wrap;
  }
  for (const a of list) {
    const checked = state.account == null || state.account === a.id;
    wrap.appendChild(el("div", {
      class: "opt-row", onClick: () => toggleAccount(a.id),
    }, [
      checkbox(checked),
      el("span", { class: "opt-name", text: a.label || a.email }),
      el("span", { class: "opt-count", text: String(a.image_count || 0) }),
    ]));
  }
  return wrap;
}

function checkbox(on, big = false) {
  const box = el("span", { class: `cbox${big ? " lg" : ""}` });
  if (on) {
    const fill = el("span", { class: "cbox-fill" });
    fill.appendChild(checkSvg());
    box.appendChild(fill);
  }
  return box;
}

function presetRow() {
  const row = el("div", { class: "presets" });
  const defs = [["all", "All time"], ["last90", "Last 90 days"], ["2025", "2025"], ["2024", "2024"]];
  for (const [k, label] of defs) {
    row.appendChild(el("button", {
      class: `preset${state.datePreset === k ? " active" : ""}`,
      text: label, onClick: () => setPreset(k),
    }));
  }
  return row;
}

function dateRow() {
  const from = el("input", {
    type: "date", class: "date-input", value: state.dateFrom,
    onChange: (e) => setState({ dateFrom: e.target.value, datePreset: "custom", page: 1 }),
  });
  const to = el("input", {
    type: "date", class: "date-input", value: state.dateTo,
    onChange: (e) => setState({ dateTo: e.target.value, datePreset: "custom", page: 1 }),
  });
  return el("div", { class: "date-row" }, [
    el("div", { class: "date-col" }, [el("div", { class: "date-cap", text: "From" }), from]),
    el("div", { class: "date-col" }, [el("div", { class: "date-cap", text: "To" }), to]),
  ]);
}

// -------------------------------------------------------- collection CRUD -- //
/**
 * Prompt for a name and create a top-level collection, then refresh the tree.
 * Uses the FROZEN api.createFolder contract (POST /api/folders). The new folder
 * is created at the root (no parent); nesting/re-parenting stays a server-side
 * concern. loadReference() re-fetches folders+counts, which re-renders the
 * sidebar via the foldersFlat state key.
 */
async function createCollection() {
  // window.prompt returns the raw string (or null on cancel). The name is only
  // ever rendered via textContent (el's `text`), never innerHTML, so it cannot
  // inject markup; the server also enforces length bounds.
  const raw = window.prompt("Name your new collection");
  if (raw == null) return; // cancelled
  const name = raw.trim();
  if (!name) return; // empty/whitespace -> no-op (server would 422 anyway)
  try {
    await api.createFolder(name);
    await loadReference();
    toast(`Created collection · ${name}`);
  } catch (e) {
    toast("Could not create collection.");
  }
}

// --------------------------------------------------------- filter actions -- //
function setFolder(id) {
  setState({ folder: id, page: 1, userMenuOpen: false });
}
function toggleAccount(id) {
  setState({ account: state.account === id ? null : id, page: 1 });
}
function toggleVendor(id) {
  setState({ vendor: state.vendor === id ? null : id, page: 1 });
}
function setPreset(k) {
  let from = "", to = "";
  const now = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  if (k === "2025") { from = "2025-01-01"; to = "2025-12-31"; }
  else if (k === "2024") { from = "2024-01-01"; to = "2024-12-31"; }
  else if (k === "last90") { from = iso(new Date(now.getTime() - 90 * 86400000)); to = iso(now); }
  setState({ datePreset: k, dateFrom: from, dateTo: to, page: 1 });
}

// ------------------------------------------------------------------ main -- //
function collectionMeta() {
  if (state.folder == null) {
    return { name: "All Images", desc: "Everything across all your connected sources." };
  }
  const f = state.foldersFlat.find((x) => x.id === state.folder);
  return { name: f ? f.name : "Collection", desc: "A curated collection of images." };
}

function renderMain() {
  const main = View.mainEl;
  if (!main) return;
  clear(main);
  View.tileEls = new Map();

  const meta = collectionMeta();
  main.appendChild(el("h1", { class: "coll-title", text: meta.name }));
  main.appendChild(el("p", { class: "coll-desc", text: meta.desc }));

  const metaRow = el("div", { class: "coll-meta" }, [
    el("span", { text: `${state.total} images` }),
  ]);
  if (filtersActive()) metaRow.appendChild(el("span", { text: "· filtered" }));
  main.appendChild(metaRow);

  main.appendChild(buildToolbar());

  if (state.loading && !state.items.length) {
    main.appendChild(el("div", { class: "loading-note", text: "Loading images…" }));
    return;
  }

  if (state.total === 0) {
    main.appendChild(buildEmpty());
    return;
  }

  main.appendChild(buildGrid());
  main.appendChild(buildPager());
}

function buildToolbar() {
  const left = el("div", { class: "toolbar-left" });

  const pageIds = state.items.map((i) => i.id);
  const allSelected = pageIds.length > 0 && pageIds.every((id) => state.selected.has(id));
  left.appendChild(el("div", { class: "select-all", onClick: selectAllPage }, [
    checkbox(allSelected, true),
    el("span", { text: "Select all on page" }),
  ]));

  if (state.selected.size > 0) {
    const dl = el("button", { class: "btn-primary btn-download", onClick: downloadSelected });
    dl.appendChild(icons.download(14));
    dl.appendChild(document.createTextNode("Download selected (zip)"));
    left.appendChild(el("div", { class: "selection-info" }, [
      el("span", { class: "sel-count", text: `${state.selected.size} selected` }),
      dl,
      bulkMenuButton("Add to collection", buildCollectionMenu),
      bulkMenuButton("Set vendor", buildVendorMenu),
      el("button", {
        class: "link-btn", text: "Clear",
        onClick: () => { closeBulkMenu(); setState({ selected: new Set() }); },
      }),
    ]));
  }

  const right = el("div", { class: "toolbar-right" }, [
    control("Sort", selectEl(
      [
        ["newest", "Newest first"], ["oldest", "Oldest first"], ["name", "Name A–Z"],
        ["vendor", "Vendor"], ["account", "Source account"],
      ],
      state.sort,
      (v) => setState({ sort: v, page: 1 }),
    )),
    control("Show", selectEl(
      PAGE_SIZES.map((n) => [String(n), String(n)]),
      String(state.pageSize),
      (v) => setState({ pageSize: parseInt(v, 10), page: 1 }),
    )),
  ]);

  return el("div", { class: "toolbar" }, [left, right]);
}

function control(label, selectNode) {
  return el("div", { class: "control" }, [
    el("span", { class: "control-label", text: label }),
    selectNode,
  ]);
}

function selectEl(options, value, onChange) {
  const sel = el("select", {
    class: "select",
    onChange: (e) => onChange(e.target.value),
  });
  for (const [val, label] of options) {
    const opt = el("option", { value: val, text: label });
    if (val === value) opt.selected = true;
    sel.appendChild(opt);
  }
  const wrap = el("div", { class: "select-wrap" }, [sel]);
  const chev = el("span", { class: "select-chevron" });
  chev.appendChild(icons.chevronDown(12));
  wrap.appendChild(chev);
  return wrap;
}

function buildGrid() {
  const grid = el("div", { class: "grid" });
  state.items.forEach((item, idx) => grid.appendChild(buildTile(item, idx)));
  return grid;
}

function buildTile(item, localIndex) {
  const selected = state.selected.has(item.id);
  const globalIndex = (state.page - 1) * state.pageSize + localIndex;

  const bg = el("div", { class: "tile-bg", style: { background: gradientFor(item.id) } });

  const img = el("img", {
    class: "tile-img", alt: item.filename || "", loading: "lazy",
    src: item.thumb_url || api.thumbUrl(item.id),
  });
  img.addEventListener("load", () => img.classList.add("loaded"));
  img.addEventListener("error", () => { img.style.display = "none"; });

  const dot = el("span", { class: "vendor-dot", style: { background: vendorColor(item.vendor || "") } });
  const caption = el("div", { class: "tile-caption" }, [
    el("div", { class: "cap-left" }, [dot, el("span", { class: "cap-vendor", text: item.vendor || "Unknown vendor" })]),
    el("span", { class: "cap-date", text: fmtMedium(item.source_date) }),
  ]);

  const ring = el("span", { class: "tile-ring", hidden: !selected });

  // top: checkbox + open-at-vendor
  const checkFill = el("span", { class: "tile-check-fill", hidden: !selected });
  checkFill.appendChild(checkSvg(13, 2.2));
  const checkBtn = el("button", {
    class: "tile-check",
    onClick: (e) => { e.stopPropagation(); toggleSelect(item.id); },
  }, [checkFill]);

  const openBtn = el("button", {
    class: "tile-open",
    onClick: (e) => { e.stopPropagation(); openAtVendor(item); },
  }, [document.createTextNode("Open at vendor "), el("span", { style: { fontSize: "12px" }, text: "↗" })]);

  const top = el("div", { class: "tile-top" }, [
    el("div", {}, [checkBtn]),
    el("div", {}, [openBtn]),
  ]);

  const tile = el("div", {
    class: `tile${selected ? " selected" : ""}`,
    onClick: () => openLightbox(globalIndex),
  }, [bg, img, caption, ring, top]);

  View.tileEls.set(item.id, { tile, ring, checkFill });
  return tile;
}

function buildEmpty() {
  return el("div", { class: "empty" }, [
    el("div", { class: "empty-title", text: "Nothing matches these filters." }),
    el("p", { class: "empty-text", text: "Try clearing a filter or widening the date range." }),
    el("button", { class: "btn-primary empty-btn", text: "Reset filters", onClick: () => resetFilters() }),
  ]);
}

// ------------------------------------------------------------ pagination -- //
function pageModel(page, count) {
  const out = [];
  const add = (n) => out.push({ page: n, active: n === page });
  const gap = () => out.push({ gap: true });
  if (count <= 7) { for (let i = 1; i <= count; i++) add(i); return out; }
  add(1);
  if (page > 3) gap();
  const s = Math.max(2, page - 1), e = Math.min(count - 1, page + 1);
  for (let i = s; i <= e; i++) add(i);
  if (page < count - 2) gap();
  add(count);
  return out;
}

function buildPager() {
  const pc = state.pages || 1;
  const page = Math.min(state.page, pc);
  const row = el("div", { class: "pager-row" });

  const prevDisabled = page <= 1;
  const prev = el("button", {
    class: `nav-btn${prevDisabled ? " disabled" : ""}`, text: "‹ Prev",
    onClick: () => { if (!prevDisabled) setState({ page: page - 1 }); },
  });
  row.appendChild(prev);

  for (const m of pageModel(page, pc)) {
    if (m.gap) { row.appendChild(el("span", { class: "page-gap", text: "…" })); continue; }
    row.appendChild(el("button", {
      class: `page-btn${m.active ? " active" : ""}`, text: String(m.page),
      onClick: () => { if (!m.active) setState({ page: m.page }); },
    }));
  }

  const nextDisabled = page >= pc;
  row.appendChild(el("button", {
    class: `nav-btn${nextDisabled ? " disabled" : ""}`, text: "Next ›",
    onClick: () => { if (!nextDisabled) setState({ page: page + 1 }); },
  }));

  const ps = state.pageSize;
  const start = (page - 1) * ps;
  const label = state.total === 0
    ? "No images"
    : `Showing ${start + 1}–${Math.min(start + ps, state.total)} of ${state.total}`;

  return el("div", { class: "pager" }, [row, el("div", { class: "range-label", text: label })]);
}

// ------------------------------------------------------------- selection -- //
function toggleSelect(id) {
  const next = new Set(state.selected);
  next.has(id) ? next.delete(id) : next.add(id);
  setState({ selected: next });
}

function selectAllPage() {
  const ids = state.items.map((i) => i.id);
  const all = ids.length > 0 && ids.every((id) => state.selected.has(id));
  const next = new Set(state.selected);
  ids.forEach((id) => { all ? next.delete(id) : next.add(id); });
  setState({ selected: next });
}

function updateSelection() {
  // Update each visible tile in place (no grid rebuild).
  for (const [id, refs] of View.tileEls) {
    const on = state.selected.has(id);
    refs.tile.classList.toggle("selected", on);
    refs.ring.hidden = !on;
    refs.checkFill.hidden = !on;
  }
  // Rebuild only the toolbar (selection bar + select-all box).
  const main = View.mainEl;
  if (!main) return;
  const oldToolbar = main.querySelector(".toolbar");
  if (oldToolbar) oldToolbar.replaceWith(buildToolbar());
}

async function downloadSelected() {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  toast(`Preparing download of ${ids.length} image${ids.length > 1 ? "s" : ""}…`);
  try {
    await api.downloadImages(ids);
    toast(`Downloaded ${ids.length} image${ids.length > 1 ? "s" : ""}.`);
  } catch (e) {
    toast("Download failed.");
  }
}

async function openAtVendor(item) {
  try {
    const detail = await api.imageDetail(item.id);
    const src = (detail.sources || []).find((s) => s.vendor_url && isHttpUrl(s.vendor_url));
    if (src) {
      window.open(src.vendor_url, "_blank", "noopener");
    } else {
      toast("No vendor link for this image.");
    }
  } catch (e) {
    toast("Could not open vendor link.");
  }
}

// --------------------------------------------------------- bulk selection -- //
// Two popover actions on the current selection, shown in the selection toolbar.
// They reuse the lightbox "Add to folder" popover pattern (.add-popover /
// .add-item / .add-empty) so the design matches. All API-supplied names are
// rendered via el()'s `text` (textContent) — never innerHTML.

let activeBulkMenu = null; // the wrapper currently showing a popover

function closeBulkMenu() {
  if (activeBulkMenu) {
    const pop = activeBulkMenu.querySelector(".add-popover");
    if (pop) pop.remove();
    activeBulkMenu = null;
  }
  document.removeEventListener("click", onBulkOutside, true);
}

function onBulkOutside(e) {
  if (activeBulkMenu && !activeBulkMenu.contains(e.target)) closeBulkMenu();
}

/** A toolbar button that toggles an anchored popover built by `build(pop)`. */
function bulkMenuButton(label, build) {
  const wrap = el("div", {
    class: "bulk-menu",
    style: { position: "relative", display: "inline-flex" },
  });
  const btn = el("button", {
    class: "link-btn", text: label,
    onClick: (e) => {
      e.stopPropagation();
      const wasOpen = !!wrap.querySelector(".add-popover");
      closeBulkMenu();
      if (wasOpen) return; // toggle off
      const pop = el("div", { class: "add-popover" });
      build(pop);
      wrap.appendChild(pop);
      activeBulkMenu = wrap;
      document.addEventListener("click", onBulkOutside, true);
    },
  });
  wrap.appendChild(btn);
  return wrap;
}

function buildCollectionMenu(pop) {
  if (!state.foldersFlat.length) {
    pop.appendChild(el("div", { class: "add-empty", text: "No collections yet." }));
  }
  for (const f of state.foldersFlat) {
    pop.appendChild(el("div", {
      class: "add-item",
      style: f.depth ? { paddingLeft: `${12 + Math.min(f.depth, 2) * 12}px` } : null,
      onClick: () => addSelectedToCollection(f.id, f.name),
    }, [el("span", { text: f.name })]));
  }
  pop.appendChild(el("div", {
    class: "add-item add-new",
    onClick: createCollectionAndAdd,
  }, [el("span", { style: { color: "var(--muted)" }, text: "+ New collection…" })]));
}

function buildVendorMenu(pop) {
  if (!state.vendorsList.length) {
    pop.appendChild(el("div", { class: "add-empty", text: "No vendors yet." }));
  }
  for (const v of state.vendorsList) {
    pop.appendChild(el("div", {
      class: "add-item",
      onClick: () => setSelectedVendor({ vendorId: v.id }, v.name),
    }, [
      el("span", { class: "vendor-dot", style: { background: vendorColor(v.name) } }),
      el("span", { text: v.name }),
    ]));
  }
  pop.appendChild(el("div", {
    class: "add-item add-new",
    onClick: createVendorAndSet,
  }, [el("span", { style: { color: "var(--muted)" }, text: "+ New vendor…" })]));
}

async function addSelectedToCollection(folderId, name) {
  closeBulkMenu();
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  try {
    await api.addImagesToFolder(folderId, ids);
    toast(`Added ${ids.length} to ${name}`);
    await loadReference(); // refresh folder counts
    if (state.folder === folderId) await refresh(); // viewing target -> show new members
    setState({ selected: new Set() });
  } catch (e) {
    toast("Could not add to collection.");
  }
}

async function createCollectionAndAdd() {
  closeBulkMenu();
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  const raw = window.prompt("Name your new collection");
  if (raw == null) return; // cancelled
  const name = raw.trim();
  if (!name) return;
  try {
    const folder = await api.createFolder(name);
    await api.addImagesToFolder(folder.id, ids);
    toast(`Added ${ids.length} to ${name}`);
    await loadReference();
    setState({ selected: new Set() });
  } catch (e) {
    toast("Could not add to collection.");
  }
}

async function setSelectedVendor(opts, name) {
  closeBulkMenu();
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  try {
    await api.setImagesVendor(ids, opts);
    toast(`Set vendor on ${ids.length}`);
    await loadReference();   // refresh vendor list + counts
    await refresh();         // re-fetch grid so the new vendor label shows
    setState({ selected: new Set() });
  } catch (e) {
    toast("Could not set vendor.");
  }
}

async function createVendorAndSet() {
  closeBulkMenu();
  if (!state.selected.size) return;
  const raw = window.prompt("Name the new vendor");
  if (raw == null) return; // cancelled
  const name = raw.trim();
  if (!name) return;
  setSelectedVendor({ vendorName: name }, name);
}

/** Called by the lightbox after membership changes to refresh folder counts. */
export function refreshReference() {
  loadReference();
}
