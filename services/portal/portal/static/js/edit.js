// edit.js — the Edit modal (SINGLE + BULK), faithful to the approved v2 design
// (design/folio-mockup.dc.html → editOpen block + its dc-script).
//
// Self-registers a store subscription like lightbox.js: setting state.edit to
// { mode, ids } opens it; clearing it closes. The modal owns its own working
// state in `ctx` (selected vendor, checked collections, per-collection bulk
// actions, inline-input values) and updates sections imperatively — it never
// pushes that working state through the global store.
//
// SECURITY: every vendor/folder/file name is rendered via el()'s `text`
// (textContent) — never innerHTML.

import { el, clear, icons, checkSvg, vendorColor } from "./util.js";
import { state, setState, subscribe, toast } from "./state.js";
import * as api from "./api.js";
import { getImageFolders, invalidateMembership } from "./folders.js";
import { loadReference, refreshGrid } from "./gallery.js";
import { refreshLightbox } from "./lightbox.js";

let initialized = false;
let overlay = null;
let openToken = 0; // guards against stale async after close/reopen

const ctx = {
  token: 0,
  mode: null, // 'single' | 'bulk'
  ids: [],
  // SINGLE working state
  vendorId: null, // selected vendor id (number) or null
  initialVendorId: null, // snapshot for diffing on save
  checkedCols: new Set(), // folder ids currently checked
  initialCols: new Set(), // snapshot for diffing on save
  // BULK working state
  bulkVendorId: "", // "" => keep current vendor; else vendor id (string)
  colActions: {}, // folderId -> 'add' | 'remove' (absent = no change)
  membershipCount: {}, // folderId -> how many selected images are already in it
  // DOM refs
  subtitleEl: null,
  bodyEl: null,
  vendorListEl: null,
  colListEl: null,
  vendorInput: null,
  colInput: null,
  saveBtn: null,
};

// ------------------------------------------------------------- public API -- //
/** Open the SINGLE editor for one image id. */
export function openSingleEdit(imageId) {
  setState({ edit: { mode: "single", ids: [imageId] }, userMenuOpen: false });
}

/** Open the BULK editor for many image ids. */
export function openBulkEdit(ids) {
  const list = (ids || []).slice();
  if (!list.length) return;
  setState({ edit: { mode: "bulk", ids: list }, userMenuOpen: false });
}

/** From the current selection: 1 → single editor, >1 → bulk editor. */
export function openEditFromSelection() {
  const ids = Array.from(state.selected);
  if (ids.length === 1) openSingleEdit(ids[0]);
  else if (ids.length > 1) openBulkEdit(ids);
}

function closeEdit() {
  setState({ edit: null });
}

// --------------------------------------------------------------- lifecycle -- //
function init() {
  if (initialized) return;
  initialized = true;
  subscribe((s, keys) => {
    if (!keys.has("edit")) return;
    if (s.edit == null) teardown();
    else show(s.edit);
  });
}
init();

function onKey(e) {
  if (!state.edit) return;
  if (e.key === "Escape") closeEdit();
}

function ensureOverlay() {
  if (overlay) return;
  overlay = el("div", {
    class: "edit-overlay",
    onClick: (e) => { if (e.target === overlay) closeEdit(); },
  });
  document.body.appendChild(overlay);
  document.addEventListener("keydown", onKey);
}

function teardown() {
  if (overlay) { overlay.remove(); overlay = null; }
  document.removeEventListener("keydown", onKey);
  ctx.mode = null;
  ctx.ids = [];
}

async function show(payload) {
  const token = ++openToken;
  ctx.token = token;
  ctx.mode = payload.mode;
  ctx.ids = payload.ids.slice();
  // reset working state
  ctx.vendorId = null;
  ctx.initialVendorId = null;
  ctx.checkedCols = new Set();
  ctx.initialCols = new Set();
  ctx.bulkVendorId = "";
  ctx.colActions = {};
  ctx.membershipCount = {};

  ensureOverlay();
  renderShell();
  renderBody();

  // Make sure reference data (vendors + folders) is available.
  if (!state.vendorsList.length || !state.foldersFlat.length) {
    try { await loadReference(); } catch (_) { /* keep going */ }
    if (ctx.token !== token) return;
  }

  if (ctx.mode === "single") await loadSingle(token);
  else await loadBulk(token);
}

// --------------------------------------------------------------- data load -- //
function imgFromState(id) {
  return (state.items || []).find((it) => it.id === id) || null;
}

async function loadSingle(token) {
  const id = ctx.ids[0];
  let detail = null;
  try { detail = await api.imageDetail(id); } catch (_) { /* shell stays */ }
  if (ctx.token !== token) return;

  const stItem = imgFromState(id);
  const filename = (detail && detail.filename) || (stItem && stItem.filename) || "";
  setSubtitle(filename);

  if (detail && Array.isArray(detail.sources)) {
    const src = detail.sources.find((s) => s.vendor_id != null);
    ctx.vendorId = src ? src.vendor_id : null;
  }
  ctx.initialVendorId = ctx.vendorId;

  let membership = new Set();
  try {
    membership = await getImageFolders({ id, filename }, state.foldersFlat);
  } catch (_) { /* empty */ }
  if (ctx.token !== token) return;
  ctx.checkedCols = new Set(membership);
  ctx.initialCols = new Set(membership);

  renderBody();
}

async function loadBulk(token) {
  const ids = ctx.ids;
  const sets = await Promise.all(ids.map((id) => membershipForId(id)));
  if (ctx.token !== token) return;
  const counts = {};
  for (const f of state.foldersFlat) {
    counts[f.id] = sets.reduce((acc, s) => acc + (s.has(f.id) ? 1 : 0), 0);
  }
  ctx.membershipCount = counts;
  // Full re-render (not just the collection list): if the modal opened before
  // reference data loaded, the bulk vendor <select> rendered without its
  // "Set all to X" options — re-render so they appear.
  renderBody();
}

async function membershipForId(id) {
  let img = imgFromState(id);
  if (!img) {
    try {
      const d = await api.imageDetail(id);
      img = { id, filename: d.filename };
    } catch (_) {
      img = { id };
    }
  }
  try {
    return await getImageFolders(img, state.foldersFlat);
  } catch (_) {
    return new Set();
  }
}

// ------------------------------------------------------------------ render -- //
function renderShell() {
  clear(overlay);

  const n = ctx.ids.length;
  const isSingle = ctx.mode === "single";

  const title = isSingle ? "Edit image" : `Edit ${n} images`;
  const subtitle = isSingle ? "" : `Changes apply to all ${n} selected images.`;
  const saveLabel = isSingle ? "Save changes" : `Apply to ${n} images`;

  ctx.subtitleEl = el("div", { class: "edit-subtitle", text: subtitle });
  const head = el("div", { class: "edit-head" }, [
    el("h2", { class: "edit-title", text: title }),
    ctx.subtitleEl,
  ]);

  ctx.bodyEl = el("div", { class: "edit-body" });

  const cancelBtn = el("button", { class: "edit-cancel", text: "Cancel", onClick: closeEdit });
  ctx.saveBtn = el("button", { class: "btn-primary edit-save", text: saveLabel, onClick: onSave });
  const foot = el("div", { class: "edit-foot" }, [cancelBtn, ctx.saveBtn]);

  const closeBtn = el("button", { class: "edit-close", onClick: closeEdit });
  closeBtn.appendChild(icons.x(14));

  const modal = el("div", { class: "edit-modal" }, [head, ctx.bodyEl, foot, closeBtn]);
  overlay.appendChild(modal);
}

function setSubtitle(text) {
  if (ctx.subtitleEl) ctx.subtitleEl.textContent = text || "";
}

function renderBody() {
  if (!ctx.bodyEl) return;
  clear(ctx.bodyEl);
  const isSingle = ctx.mode === "single";

  // Vendor section
  ctx.bodyEl.appendChild(el("div", { class: "edit-eyebrow", text: "Vendor" }));
  ctx.vendorListEl = el("div", { class: isSingle ? "edit-list" : "edit-select-wrap" });
  ctx.bodyEl.appendChild(ctx.vendorListEl);
  renderVendorList();

  // Inline add-vendor row
  ctx.vendorInput = el("input", {
    class: "edit-inline-input", placeholder: "Add a vendor not in the list…",
    onKeyDown: (e) => { if (e.key === "Enter") addInlineVendor(); },
  });
  ctx.bodyEl.appendChild(el("div", { class: "edit-inline" }, [
    ctx.vendorInput,
    el("button", { class: "edit-inline-add", text: "+ Add", onClick: addInlineVendor }),
  ]));

  ctx.bodyEl.appendChild(el("div", { class: "edit-sep" }));

  // Collections section
  ctx.bodyEl.appendChild(el("div", { class: "edit-eyebrow cols", text: "Collections" }));
  ctx.colListEl = el("div", { class: isSingle ? "edit-list" : "edit-bulk-list" });
  ctx.bodyEl.appendChild(ctx.colListEl);
  renderColList();

  // Inline create-collection row
  ctx.colInput = el("input", {
    class: "edit-inline-input", placeholder: "Create a new collection…",
    onKeyDown: (e) => { if (e.key === "Enter") addInlineCollection(); },
  });
  ctx.bodyEl.appendChild(el("div", { class: "edit-inline cols" }, [
    ctx.colInput,
    el("button", { class: "edit-inline-add", text: "+ Add", onClick: addInlineCollection }),
  ]));
}

function renderVendorList() {
  const wrap = ctx.vendorListEl;
  if (!wrap) return;
  clear(wrap);

  if (ctx.mode === "single") {
    for (const v of state.vendorsList) {
      const selected = ctx.vendorId === v.id;
      const radio = el("span", { class: "edit-radio" });
      if (selected) radio.appendChild(el("span", { class: "edit-radio-dot" }));
      wrap.appendChild(el("div", {
        class: "edit-vrow",
        onClick: () => { ctx.vendorId = v.id; renderVendorList(); },
      }, [
        radio,
        el("span", { class: "edit-vdot", style: { background: vendorColor(v.name) } }),
        el("span", { class: "edit-vname", text: v.name }),
      ]));
    }
    return;
  }

  // BULK: a <select> with "keep current" + "set all to <vendor>" options.
  const sel = el("select", {
    class: "edit-select",
    onChange: (e) => { ctx.bulkVendorId = e.target.value; },
  });
  const keep = el("option", { value: "", text: "— Keep current vendor —" });
  if (ctx.bulkVendorId === "") keep.selected = true;
  sel.appendChild(keep);
  for (const v of state.vendorsList) {
    const opt = el("option", { value: String(v.id), text: `Set all to ${v.name}` });
    if (ctx.bulkVendorId === String(v.id)) opt.selected = true;
    sel.appendChild(opt);
  }
  wrap.appendChild(sel);
  const chev = el("span", { class: "edit-select-chevron" });
  chev.appendChild(icons.chevronDown(12));
  wrap.appendChild(chev);
}

function renderColList() {
  const wrap = ctx.colListEl;
  if (!wrap) return;
  clear(wrap);

  const folders = state.foldersFlat;
  if (!folders.length) {
    wrap.appendChild(el("div", { class: "edit-empty", text: "No collections yet." }));
    return;
  }

  if (ctx.mode === "single") {
    for (const f of folders) {
      const checked = ctx.checkedCols.has(f.id);
      const box = el("span", { class: "edit-cbox" });
      if (checked) {
        const fill = el("span", { class: "edit-cbox-fill" });
        fill.appendChild(checkSvg(12, 2.2));
        box.appendChild(fill);
      }
      wrap.appendChild(el("div", {
        class: "edit-crow",
        onClick: () => {
          if (ctx.checkedCols.has(f.id)) ctx.checkedCols.delete(f.id);
          else ctx.checkedCols.add(f.id);
          renderColList();
        },
      }, [box, el("span", { class: "edit-cname", text: f.name })]));
    }
    return;
  }

  // BULK: tri-state [ — / Add / Remove ] per collection, with an "in X of N" hint.
  const n = ctx.ids.length;
  for (const f of folders) {
    const action = ctx.colActions[f.id] || "none";
    const inCount = ctx.membershipCount[f.id] || 0;
    const info = el("div", { class: "edit-bulk-cinfo" }, [
      el("div", { class: "edit-bulk-cname", text: f.name }),
      el("div", { class: "edit-bulk-chint", text: `${inCount} of ${n} now in` }),
    ]);
    const pills = el("div", { class: "edit-pills" }, [
      pill("—", "none", action, () => setBulkCol(f.id, "none")),
      pill("Add", "add", action, () => setBulkCol(f.id, "add")),
      pill("Remove", "remove", action, () => setBulkCol(f.id, "remove")),
    ]);
    wrap.appendChild(el("div", { class: "edit-bulk-crow" }, [info, pills]));
  }
}

function pill(label, kind, active, onClick) {
  return el("button", {
    class: `edit-pill${active === kind ? ` on-${kind}` : ""}`,
    text: label,
    onClick,
  });
}

function setBulkCol(fid, action) {
  if (action === "none") delete ctx.colActions[fid];
  else ctx.colActions[fid] = action;
  renderColList();
}

// --------------------------------------------------------------- inline add -- //
async function addInlineVendor() {
  const name = (ctx.vendorInput && ctx.vendorInput.value || "").trim();
  if (!name) return;
  try {
    const v = await api.createVendor({ name });
    await loadReference(); // refresh vendor list so the new id is present everywhere
    if (ctx.mode === "single") ctx.vendorId = v.id;
    else ctx.bulkVendorId = String(v.id);
    if (ctx.vendorInput) ctx.vendorInput.value = "";
    renderVendorList();
    toast(`Added vendor · ${name}`);
  } catch (_) {
    toast("Could not add that vendor.");
  }
}

async function addInlineCollection() {
  const name = (ctx.colInput && ctx.colInput.value || "").trim();
  if (!name) return;
  try {
    const f = await api.createFolder(name);
    await loadReference(); // refresh folder tree so the new row renders
    if (ctx.mode === "single") {
      ctx.checkedCols.add(f.id);
    } else {
      ctx.colActions[f.id] = "add";
      if (ctx.membershipCount[f.id] == null) ctx.membershipCount[f.id] = 0;
    }
    if (ctx.colInput) ctx.colInput.value = "";
    renderColList();
    toast(`Created collection · ${name}`);
  } catch (_) {
    toast("Could not create that collection.");
  }
}

// ------------------------------------------------------------------- save -- //
async function onSave() {
  if (ctx.saveBtn) ctx.saveBtn.disabled = true;
  try {
    if (ctx.mode === "single") await saveSingle();
    else await saveBulk();
  } catch (_) {
    if (ctx.saveBtn) ctx.saveBtn.disabled = false;
    toast("Could not save changes.");
    return;
  }
  // Refresh everything that may have changed.
  try { await loadReference(); } catch (_) { /* ignore */ }
  try { refreshGrid(); } catch (_) { /* ignore */ }
  if (state.lightbox) { try { refreshLightbox(); } catch (_) { /* ignore */ } }
  closeEdit();
}

async function saveSingle() {
  const id = ctx.ids[0];
  const ops = [];

  if (ctx.vendorId !== ctx.initialVendorId) {
    ops.push(api.setImagesVendor([id], { vendorId: ctx.vendorId }));
  }
  const toAdd = [...ctx.checkedCols].filter((fid) => !ctx.initialCols.has(fid));
  const toRemove = [...ctx.initialCols].filter((fid) => !ctx.checkedCols.has(fid));
  for (const fid of toAdd) ops.push(api.addImagesToFolder(fid, [id]));
  for (const fid of toRemove) ops.push(api.removeImageFromFolder(fid, id));

  await Promise.all(ops);
  invalidateMembership(id);

  const stItem = imgFromState(id);
  const filename = (stItem && stItem.filename) || (ctx.subtitleEl && ctx.subtitleEl.textContent) || "";
  toast(filename ? `Saved changes · ${filename}` : "Saved changes");
}

async function saveBulk() {
  const ids = ctx.ids.slice();
  const ops = [];

  if (ctx.bulkVendorId !== "") {
    ops.push(api.setImagesVendor(ids, { vendorId: Number(ctx.bulkVendorId) }));
  }
  for (const fid of Object.keys(ctx.colActions)) {
    const action = ctx.colActions[fid];
    const folderId = Number(fid);
    if (action === "add") ops.push(api.addImagesToFolder(folderId, ids));
    else if (action === "remove") ops.push(api.removeImagesFromFolder(folderId, ids));
  }

  await Promise.all(ops);
  ids.forEach((id) => invalidateMembership(id));
  toast(`Updated ${ids.length} image${ids.length > 1 ? "s" : ""}`);
}
