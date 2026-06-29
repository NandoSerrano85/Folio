// senders.js — "Senders & sources": the Gmail sender allow-list (enable/disable,
// map sender -> vendor, add address/domain) and a read-only Google Drive sources
// table built from connected drive accounts.
//
// Wiring notes: senders persist via /api/senders (POST/PATCH). Drive accounts
// have no enable/connect endpoint, so that table is informational — its toggle
// and the "Add drive"/"Connect" actions explain that drives are configured
// server-side (matching the contract; documented in the report).

import { el, clear, icons, vendorColor } from "./util.js";
import { state, toast } from "./state.js";
import * as api from "./api.js";
import { flattenFolders } from "./folders.js";
import { loadReference } from "./gallery.js";

let root = null;
let sendersData = [];
let rulesData = [];

// Rule-builder working state. Kept at module scope so a re-render of the page
// (e.g. toggling a sender) restores an in-progress rule instead of dropping it.
let builderFolderId = "";
let builderName = "";
let builderConds = [{ field: "vendor", value: "" }];

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const DOMAIN_RE = /^@[^@\s]+\.[^@\s]+$/;

export function mountSenders() {
  if (!root) root = el("div", { class: "senders-page" });
  load();
  return root;
}

async function load() {
  renderSkeleton();
  try {
    // Ensure reference data is present (vendors/accounts).
    if (!state.vendorsList.length || !state.accountsList.length) {
      const [vends, accts] = await Promise.all([api.vendors(), api.accounts()]);
      state.vendorsList = vends || state.vendorsList;
      state.accountsList = accts || state.accountsList;
    }
    sendersData = (await api.senders()) || [];
  } catch (_) {
    sendersData = [];
    toast("Could not load senders.");
  }
  // Collection rules need the folder list (target collections + name resolution)
  // and their own data; load them independently so a failure here doesn't blank
  // the senders tables above.
  if (!state.foldersFlat.length) {
    try { state.foldersFlat = flattenFolders((await api.folders()) || []); } catch (_) { /* leave empty */ }
  }
  try { rulesData = (await api.collectionRules()) || []; } catch (_) { rulesData = []; }
  render();
}

function renderSkeleton() {
  clear(root);
  root.appendChild(el("h1", { class: "page-title", text: "Senders & sources" }));
  root.appendChild(el("div", { class: "loading-note", style: { textAlign: "left" }, text: "Loading…" }));
}

function render() {
  clear(root);

  root.appendChild(el("h1", { class: "page-title", text: "Senders & sources" }));
  root.appendChild(el("p", {
    class: "page-intro",
    text: "These control which sources Folio pulls images from — the mailboxes it scans for attachments, and the Google Drives it watches for new files.",
  }));

  root.appendChild(buildEmailSection());
  root.appendChild(buildDriveSection());
  root.appendChild(buildVendorsSection());
  root.appendChild(buildRulesSection());
}

// ----------------------------------------------------------- email senders -- //
function buildEmailSection() {
  const frag = document.createDocumentFragment();

  const included = sendersData.filter((s) => s.enabled).length;
  const off = sendersData.length - included;

  const head = el("div", { class: "section-head" }, [
    el("h2", { text: "Email senders" }),
    el("span", { class: "section-stat", text: `${sendersData.length} discovered · ${included} included · ${off} off` }),
  ]);
  frag.appendChild(head);
  frag.appendChild(el("p", {
    class: "section-help",
    text: "Turn a sender off to stop importing its attachments, or add an address or a whole domain to include.",
  }));

  frag.appendChild(buildAddRow());

  const table = el("div", { class: "table" });
  const thead = el("div", { class: "thead grid-senders" }, [
    el("span", { text: "Sender" }),
    el("span", { class: "th-r", text: "Image emails" }),
    el("span", { class: "th-vendor", text: "Mapped vendor" }),
    el("span", { class: "th-r", text: "Include" }),
  ]);
  table.appendChild(thead);

  const rows = sendersData.slice().sort((a, b) =>
    (b.discovered_count - a.discovered_count) || a.address.localeCompare(b.address));

  if (!rows.length) {
    table.appendChild(el("div", { class: "empty-table", text: "No senders yet. Add an address or domain above." }));
  } else {
    rows.forEach((s) => table.appendChild(senderRow(s)));
  }
  frag.appendChild(table);

  frag.appendChild(el("p", {
    class: "section-note",
    text: "Changes apply on the next sync (hourly). Turning a sender off stops importing new attachments; images already in the library stay.",
  }));
  return frag;
}

function buildAddRow() {
  const input = el("input", {
    class: "add-input", placeholder: "name@vendor.com   or   @vendor.com",
    onKeyDown: (e) => { if (e.key === "Enter") submitAdd(input, acctSelect); },
  });

  const gmailAccounts = state.accountsList.filter((a) => a.provider === "gmail");
  let acctSelect = null;
  const row = el("div", { class: "add-row" }, [input]);

  // Only surface an account picker when the target is ambiguous (>1 mailbox).
  if (gmailAccounts.length > 1) {
    acctSelect = el("select", { class: "select add-account-select" });
    gmailAccounts.forEach((a) => acctSelect.appendChild(el("option", { value: String(a.id), text: a.label || a.email })));
    const wrap = el("div", { class: "select-wrap" }, [acctSelect]);
    const chev = el("span", { class: "select-chevron" }); chev.appendChild(icons.chevronDown(12));
    wrap.appendChild(chev);
    row.appendChild(wrap);
  }

  row.appendChild(el("button", {
    class: "btn-primary btn-add", text: "Add source",
    onClick: () => submitAdd(input, acctSelect),
  }));
  return row;
}

async function submitAdd(input, acctSelect) {
  const raw = (input.value || "").trim();
  if (!raw) return;
  const isEmail = EMAIL_RE.test(raw);
  const isDomain = DOMAIN_RE.test(raw);
  if (!isEmail && !isDomain) {
    toast("Enter an email (name@vendor.com) or a domain (@vendor.com).");
    return;
  }
  if (sendersData.some((s) => s.address.toLowerCase() === raw.toLowerCase())) {
    toast("That source is already in the list.");
    return;
  }
  const gmailAccounts = state.accountsList.filter((a) => a.provider === "gmail");
  if (!gmailAccounts.length) {
    toast("Connect a Gmail account before adding senders.");
    return;
  }
  const accountId = acctSelect ? parseInt(acctSelect.value, 10) : gmailAccounts[0].id;

  const payload = { account_id: accountId, enabled: true };
  if (isDomain) payload.domain = raw.replace(/^@/, "");
  else payload.address = raw;

  try {
    const created = await api.createSender(payload);
    sendersData.push(created);
    input.value = "";
    toast(`Added ${raw} · will include on next sync.`);
    render();
  } catch (e) {
    if (e && e.status === 409) toast("That source is already in the list.");
    else toast("Could not add that source.");
  }
}

function senderRow(s) {
  const wrap = el("div", { class: `trow${s.enabled ? "" : " off"}` });

  // Sender + manual tag (heuristic: no discovered emails => manually added).
  const addrCell = el("div", { style: { minWidth: "0" } }, [
    el("div", { class: "sender-addr", text: s.address }),
  ]);
  if (!s.discovered_count) addrCell.appendChild(el("span", { class: "tag", text: "Added manually" }));

  // Vendor select
  const vsel = el("select", { class: "select-sm", onChange: (e) => updateVendor(s, e.target.value) });
  vsel.appendChild(el("option", { value: "", text: "— Unmapped —" }));
  for (const v of state.vendorsList) {
    const opt = el("option", { value: String(v.id), text: v.name });
    if (s.vendor_id === v.id) opt.selected = true;
    vsel.appendChild(opt);
  }
  const vwrap = el("div", { class: "select-wrap", style: { display: "block" } }, [vsel]);
  const chev = el("span", { class: "select-chevron", style: { right: "9px" } }); chev.appendChild(icons.chevronDown(11));
  vwrap.appendChild(chev);

  // Toggle
  const toggle = el("button", {
    class: `toggle${s.enabled ? " on" : ""}`, onClick: () => updateEnabled(s, wrap, toggle),
  }, [el("span", { class: "toggle-knob" })]);

  wrap.appendChild(el("div", { class: "tcells grid-senders" }, [
    addrCell,
    el("div", { class: "cell-num", text: String(s.discovered_count || 0) }),
    el("div", { class: "cell-vendor" }, [vwrap]),
    el("div", { class: "cell-toggle" }, [toggle]),
  ]));
  return wrap;
}

async function updateEnabled(s, rowEl, toggleEl) {
  const next = !s.enabled;
  try {
    await api.updateSender(s.id, { enabled: next });
    s.enabled = next;
    toggleEl.classList.toggle("on", next);
    rowEl.classList.toggle("off", !next);
    // Update the header stat by re-rendering the section quietly.
    render();
  } catch (_) {
    toast("Could not update sender.");
  }
}

async function updateVendor(s, value) {
  const vendorId = value ? parseInt(value, 10) : null;
  try {
    await api.updateSender(s.id, { vendor_id: vendorId });
    s.vendor_id = vendorId;
    toast(vendorId ? "Vendor mapping saved." : "Vendor mapping cleared.");
  } catch (_) {
    toast("Could not update vendor mapping.");
  }
}

// ------------------------------------------------------------ drive sources -- //
function buildDriveSection() {
  const frag = document.createDocumentFragment();
  const drives = state.accountsList.filter((a) => a.provider === "drive");
  const on = drives.filter((a) => a.status === "active").length;

  frag.appendChild(el("div", { class: "section-head tight" }, [
    el("h2", { text: "Google Drive sources" }),
    el("span", { class: "section-stat", text: `${drives.length} connected · ${on} on` }),
  ]));
  frag.appendChild(el("p", {
    class: "section-help",
    text: "Shared drives and folders Folio scans for new image files. Connect another Google account, or add a specific folder to watch.",
  }));

  const input = el("input", {
    class: "add-input", placeholder: "Shared drive or folder path (e.g. Vendors/Lookbooks)",
    onKeyDown: (e) => { if (e.key === "Enter") addDrive(input); },
  });
  const connectBtn = el("button", {
    class: "btn-ghost btn-connect",
    onClick: () => toast("Connecting a Google account is configured on the server."),
  }, [document.createTextNode("Connect a Google account "), el("span", { style: { fontSize: "13px" }, text: "↗" })]);
  frag.appendChild(el("div", { class: "add-row" }, [
    input,
    el("button", { class: "btn-primary btn-add", text: "Add drive", onClick: () => addDrive(input) }),
    connectBtn,
  ]));

  const table = el("div", { class: "table" });
  table.appendChild(el("div", { class: "thead grid-drive" }, [
    el("span", { text: "Drive / folder" }),
    el("span", { text: "Account" }),
    el("span", { class: "th-r", text: "Images" }),
    el("span", { class: "th-r", text: "Include" }),
  ]));

  if (!drives.length) {
    table.appendChild(el("div", { class: "empty-table", text: "No Google Drive accounts connected." }));
  } else {
    drives.forEach((a) => table.appendChild(driveRow(a)));
  }
  frag.appendChild(table);

  frag.appendChild(el("p", {
    class: "section-note",
    text: "Connecting a Google account opens Google's authorization. Folio only reads image files in the drives and folders you add here.",
  }));
  return frag;
}

function driveRow(a) {
  const active = a.status === "active";
  const wrap = el("div", { class: `trow${active ? "" : " off"}` });

  const nameCell = el("div", { class: "cell-drivename" }, [
    el("div", { class: "drive-name", text: a.label || a.email }),
    el("div", { class: "drive-tags" }, [el("span", { class: "tag", text: "Drive account" })]),
  ]);

  const toggle = el("button", {
    class: `toggle${active ? " on" : ""}`,
    onClick: () => toast("Google Drive sources are managed in the server settings."),
  }, [el("span", { class: "toggle-knob" })]);

  wrap.appendChild(el("div", { class: "tcells grid-drive" }, [
    nameCell,
    el("div", { class: "cell-account", text: a.email }),
    el("div", { class: "cell-num", text: String(a.image_count || 0) }),
    el("div", { class: "cell-toggle" }, [toggle]),
  ]));
  return wrap;
}

function addDrive(input) {
  const raw = (input.value || "").trim();
  if (!raw) return;
  toast("Drive folders are configured on the server (read-only here).");
  input.value = "";
}

// ----------------------------------------------------------------- vendors -- //
// Column layout shared by the vendors header + rows (name · image count · row
// actions). Defined inline so it matches the senders/drive tables without a new
// CSS grid class.
const VENDOR_GRID = "1fr 116px 150px";

function buildVendorsSection() {
  const frag = document.createDocumentFragment();
  const vendors = state.vendorsList || [];

  frag.appendChild(el("div", { class: "section-head tight" }, [
    el("h2", { text: "Vendors" }),
    el("span", { class: "section-stat", text: `${vendors.length} vendor${vendors.length === 1 ? "" : "s"}` }),
  ]));
  frag.appendChild(el("p", {
    class: "section-help",
    text: "Designers/brands you assign to images. Add them here, or create them on the fly when bulk-tagging in the library.",
  }));

  const input = el("input", {
    class: "add-input", placeholder: "Vendor or designer name",
    onKeyDown: (e) => { if (e.key === "Enter") submitVendor(input); },
  });
  frag.appendChild(el("div", { class: "add-row" }, [
    input,
    el("button", { class: "btn-primary btn-add", text: "Add vendor", onClick: () => submitVendor(input) }),
  ]));

  const table = el("div", { class: "table" });
  table.appendChild(el("div", { class: "thead", style: { display: "grid", gridTemplateColumns: VENDOR_GRID } }, [
    el("span", { text: "Vendor" }),
    el("span", { class: "th-r", text: "Images" }),
    el("span", { class: "th-r", text: "Actions" }),
  ]));

  const rows = vendors.slice().sort((a, b) => a.name.localeCompare(b.name));
  if (!rows.length) {
    table.appendChild(el("div", { class: "empty-table", text: "No vendors yet. Add one above." }));
  } else {
    rows.forEach((v) => table.appendChild(vendorRow(v)));
  }
  frag.appendChild(table);

  frag.appendChild(el("p", {
    class: "section-note",
    text: "Deleting a vendor unassigns it from images — the images and their files stay in the library.",
  }));
  return frag;
}

function vendorRow(v) {
  const wrap = el("div", { class: "trow" });

  const nameCell = el("div", { style: { display: "flex", alignItems: "center", gap: "9px", minWidth: "0" } }, [
    el("span", { class: "vendor-dot", style: { background: vendorColor(v.name) } }),
    el("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: v.name }),
  ]);

  const actions = el("div", { style: { display: "flex", justifyContent: "flex-end", gap: "14px" } }, [
    el("button", { class: "link-btn", text: "Rename", onClick: () => renameVendor(v) }),
    el("button", { class: "link-btn", text: "Delete", onClick: () => removeVendor(v) }),
  ]);

  wrap.appendChild(el("div", { class: "tcells", style: { display: "grid", gridTemplateColumns: VENDOR_GRID } }, [
    nameCell,
    el("div", { class: "cell-num", text: String(v.image_count || 0) }),
    actions,
  ]));
  return wrap;
}

// Re-fetch vendors into shared state and re-render the screen so the list, the
// "N vendors" stat, and the sender vendor-mapping dropdowns all stay in sync.
async function refreshVendors() {
  try {
    state.vendorsList = (await api.vendors()) || [];
  } catch (_) {
    toast("Could not refresh vendors.");
  }
  render();
}

async function submitVendor(input) {
  const name = (input.value || "").trim();
  if (!name) return;
  try {
    await api.createVendor({ name });
    input.value = "";
    toast(`Added vendor ${name}.`);
    await refreshVendors();
  } catch (e) {
    if (e && e.status === 409) toast("That vendor already exists");
    else toast("Could not add that vendor.");
  }
}

async function renameVendor(v) {
  const next = window.prompt("Rename vendor", v.name);
  if (next == null) return;
  const name = next.trim();
  if (!name || name === v.name) return;
  try {
    await api.updateVendor(v.id, { name });
    toast("Vendor renamed.");
    await refreshVendors();
  } catch (_) {
    toast("Could not rename vendor.");
  }
}

async function removeVendor(v) {
  if (!window.confirm(`Delete vendor ${v.name}? Images keep their files; they just lose this vendor.`)) return;
  try {
    await api.deleteVendor(v.id);
    toast("Vendor deleted.");
    await refreshVendors();
  } catch (_) {
    toast("Could not delete vendor.");
  }
}

// -------------------------------------------------------- collection rules -- //
// Auto-filing rules: each rule targets a collection (folder) and carries a list
// of conditions that are ANDed; on every sync, images matching ALL conditions
// are filed into the folder. The contract lives in folio_core.rules; this UI is
// a thin builder/list over the /api/collection-rules endpoints.
//
// Column layout for the rules list (conditions · target collection · enabled ·
// delete). Inline grid so it matches the senders/drive/vendor tables without a
// new CSS grid class.
const RULE_GRID = "1fr 168px 56px 72px";

// Field -> the single operator it uses (operators are implied by field, per the
// contract) + its human label. Order here is the order in the field picker.
const RULE_FIELDS = [
  { field: "vendor", op: "is", label: "Vendor" },
  { field: "account", op: "is", label: "Account" },
  { field: "source_type", op: "is", label: "Source type" },
  { field: "folder_path", op: "contains", label: "Folder path" },
  { field: "filename", op: "contains", label: "Filename" },
  { field: "date", op: "within_days", label: "Date added" },
];

function ruleFieldSpec(field) {
  return RULE_FIELDS.find((f) => f.field === field) || RULE_FIELDS[0];
}

function opLabel(op) {
  // 'within_days' reads better as "within … days"; the others read literally.
  return op === "within_days" ? "within" : op;
}

// ---- name resolution (ids -> readable names from shared reference state) ---- //
function vendorNameById(id) {
  const v = (state.vendorsList || []).find((x) => x.id === Number(id));
  return v ? v.name : `vendor #${id}`;
}
function accountNameById(id) {
  const a = (state.accountsList || []).find((x) => x.id === Number(id));
  return a ? (a.label || a.email) : `account #${id}`;
}
function folderNameById(id) {
  const f = (state.foldersFlat || []).find((x) => x.id === Number(id));
  return f ? f.name : `collection #${id}`;
}

// A single condition rendered as a readable phrase (used in the rules list).
function conditionText(c) {
  switch (c.field) {
    case "vendor": return `Vendor is ${vendorNameById(c.value)}`;
    case "account": return `Account is ${accountNameById(c.value)}`;
    case "source_type": return `Source is ${c.value === "drive" ? "Google Drive" : "Email"}`;
    case "folder_path": return `Folder path contains "${c.value}"`;
    case "filename": return `Filename contains "${c.value}"`;
    case "date": return `Added within ${c.value} day${String(c.value) === "1" ? "" : "s"}`;
    default: return `${c.field} ${c.op} ${c.value}`;
  }
}

// Conditions -> a row of pills joined by mono "and" separators. All text goes in
// via textContent (el()), so resolved names can never inject markup.
function conditionsSummary(conditions) {
  const wrap = el("div", { class: "rule-summary" });
  if (!conditions || !conditions.length) {
    wrap.appendChild(el("span", { class: "rule-empty", text: "No conditions — matches nothing" }));
    return wrap;
  }
  conditions.forEach((c, i) => {
    if (i) wrap.appendChild(el("span", { class: "rule-and", text: "and" }));
    wrap.appendChild(el("span", { class: "rule-pill", text: conditionText(c) }));
  });
  return wrap;
}

// Shared chevron-wrapped select (mirrors the email-section pattern).
function ruleSelectWrap(selectEl) {
  const wrap = el("div", { class: "select-wrap rule-select-wrap" }, [selectEl]);
  const chev = el("span", { class: "select-chevron" });
  chev.appendChild(icons.chevronDown(12));
  wrap.appendChild(chev);
  return wrap;
}

function buildRulesSection() {
  const frag = document.createDocumentFragment();

  frag.appendChild(el("div", { class: "section-head tight" }, [
    el("h2", { text: "Collection rules" }),
    el("span", { class: "section-stat", text: `${rulesData.length} rule${rulesData.length === 1 ? "" : "s"}` }),
  ]));
  frag.appendChild(el("p", {
    class: "section-help",
    text: "Rules automatically file matching images into a collection on every sync. An image is filed when it matches all of a rule's conditions, and it can match several rules.",
  }));

  // Apply now — file already-matching images immediately.
  frag.appendChild(el("div", { class: "add-row" }, [
    el("button", { class: "btn-ghost btn-add", text: "Apply rules now", onClick: applyRulesNow }),
    el("span", { class: "rule-apply-note", text: "Files existing images that already match. New images are filed automatically each sync." }),
  ]));

  // Existing rules.
  const table = el("div", { class: "table" });
  table.appendChild(el("div", { class: "thead", style: { display: "grid", gridTemplateColumns: RULE_GRID } }, [
    el("span", { text: "Conditions" }),
    el("span", { text: "Files into" }),
    el("span", { class: "th-r", text: "On" }),
    el("span", { class: "th-r", text: "" }),
  ]));

  if (!rulesData.length) {
    table.appendChild(el("div", { class: "empty-table", text: "No rules yet. Build one below to start auto-filing images." }));
  } else {
    rulesData.forEach((r) => table.appendChild(ruleRow(r)));
  }
  frag.appendChild(table);

  // Builder.
  frag.appendChild(buildRuleBuilder());

  frag.appendChild(el("p", {
    class: "section-note",
    text: "Disabling or deleting a rule stops future auto-filing; images already filed stay in the collection.",
  }));
  return frag;
}

function ruleRow(r) {
  const wrap = el("div", { class: `trow${r.enabled ? "" : " off"}` });

  // Conditions cell (optional rule name above the pills, optional match count below).
  const condCell = el("div", { style: { minWidth: "0" } });
  if (r.name) condCell.appendChild(el("div", { class: "rule-name", text: r.name }));
  condCell.appendChild(conditionsSummary(r.conditions));
  if (typeof r.match_count === "number") {
    condCell.appendChild(el("div", {
      class: "rule-match",
      text: `${r.match_count} image${r.match_count === 1 ? "" : "s"} match`,
    }));
  }

  const folderCell = el("div", { class: "rule-folder" }, [
    el("span", { class: "rule-folder-name", text: r.folder_name || folderNameById(r.folder_id) }),
  ]);

  const toggle = el("button", {
    class: `toggle${r.enabled ? " on" : ""}`,
    onClick: () => toggleRuleEnabled(r, wrap, toggle),
  }, [el("span", { class: "toggle-knob" })]);

  const del = el("button", { class: "link-btn", text: "Delete", onClick: () => removeRule(r) });

  wrap.appendChild(el("div", { class: "tcells", style: { display: "grid", gridTemplateColumns: RULE_GRID } }, [
    condCell,
    folderCell,
    el("div", { class: "cell-toggle" }, [toggle]),
    el("div", { style: { display: "flex", justifyContent: "flex-end" } }, [del]),
  ]));
  return wrap;
}

// Toggle in place (no full re-render) so an in-progress builder is undisturbed.
async function toggleRuleEnabled(r, rowEl, toggleEl) {
  const next = !r.enabled;
  try {
    await api.updateCollectionRule(r.id, { enabled: next });
    r.enabled = next;
    toggleEl.classList.toggle("on", next);
    rowEl.classList.toggle("off", !next);
    toast(next ? "Rule enabled." : "Rule disabled · already-filed images stay.");
  } catch (_) {
    toast("Could not update rule.");
  }
}

async function removeRule(r) {
  const label = r.name ? `"${r.name}"` : "this rule";
  if (!window.confirm(`Delete ${label}? Images already filed stay in the collection.`)) return;
  try {
    await api.deleteCollectionRule(r.id);
    rulesData = rulesData.filter((x) => x.id !== r.id);
    toast("Rule deleted.");
    render();
  } catch (_) {
    toast("Could not delete rule.");
  }
}

async function applyRulesNow() {
  try {
    const res = await api.applyCollectionRules();
    const n = (res && res.total_added) || 0;
    toast(n ? `Filed ${n} image${n === 1 ? "" : "s"} into collections.` : "No new images to file.");
    await loadReference(); // refresh collection counts in the shared sidebar state
    try { rulesData = (await api.collectionRules()) || rulesData; } catch (_) { /* keep current */ }
    render();
  } catch (_) {
    toast("Could not apply rules.");
  }
}

// ----------------------------------------------------------- rule builder -- //
function buildRuleBuilder() {
  const panel = el("div", { class: "rule-builder" });
  panel.appendChild(el("div", { class: "rule-builder-head", text: "New rule" }));

  // Target collection + optional name.
  const folderSel = el("select", {
    class: "select rule-folder-select",
    onChange: (e) => { builderFolderId = e.target.value; },
  });
  folderSel.appendChild(el("option", { value: "", text: "Choose a collection…" }));
  (state.foldersFlat || []).forEach((f) => {
    const opt = el("option", { value: String(f.id), text: `${"— ".repeat(f.depth)}${f.name}` });
    if (String(f.id) === String(builderFolderId)) opt.selected = true;
    folderSel.appendChild(opt);
  });

  const nameInput = el("input", {
    class: "add-input rule-name-input", placeholder: "Rule name (optional)",
    value: builderName, onInput: (e) => { builderName = e.target.value; },
  });

  panel.appendChild(el("div", { class: "rule-target-row" }, [
    el("span", { class: "rule-field-label", text: "File into" }),
    ruleSelectWrap(folderSel),
    nameInput,
  ]));

  // Conditions (ANDed).
  panel.appendChild(el("div", { class: "rule-field-label rule-when", text: "When an image matches all of:" }));
  const condList = el("div", { class: "rule-cond-list" });
  panel.appendChild(condList);

  const renderConds = () => {
    clear(condList);
    builderConds.forEach((c, i) => condList.appendChild(builderCondRow(c, i, renderConds)));
  };
  renderConds();

  panel.appendChild(el("div", { class: "rule-builder-actions" }, [
    el("button", {
      class: "link-btn rule-add-cond", text: "+ Add condition",
      onClick: () => { builderConds.push({ field: "vendor", value: "" }); renderConds(); },
    }),
    el("button", { class: "btn-primary btn-add rule-submit", text: "Add rule", onClick: submitRule }),
  ]));
  return panel;
}

function builderCondRow(c, index, renderConds) {
  const row = el("div", { class: "rule-cond-row" });
  if (index) row.appendChild(el("span", { class: "rule-and", text: "and" }));

  // Field picker — changing the field resets the value (the control type differs).
  const fieldSel = el("select", {
    class: "select rule-cond-field",
    onChange: (e) => { c.field = e.target.value; c.value = ""; renderConds(); },
  });
  RULE_FIELDS.forEach((f) => {
    const opt = el("option", { value: f.field, text: f.label });
    if (f.field === c.field) opt.selected = true;
    fieldSel.appendChild(opt);
  });
  row.appendChild(ruleSelectWrap(fieldSel));

  // Implied operator.
  row.appendChild(el("span", { class: "rule-op", text: opLabel(ruleFieldSpec(c.field).op) }));

  // Value control appropriate to the field.
  row.appendChild(valueControl(c));

  // Remove (only when more than one condition).
  if (builderConds.length > 1) {
    const rm = el("button", {
      class: "rule-cond-remove", title: "Remove condition",
      onClick: () => { builderConds.splice(index, 1); renderConds(); },
    });
    rm.appendChild(icons.x(13));
    row.appendChild(rm);
  }
  return row;
}

function valueControl(c) {
  if (c.field === "vendor") {
    const sel = el("select", { class: "select rule-cond-val", onChange: (e) => { c.value = e.target.value; } });
    sel.appendChild(el("option", { value: "", text: "Choose vendor…" }));
    (state.vendorsList || []).forEach((v) => {
      const opt = el("option", { value: String(v.id), text: v.name });
      if (String(v.id) === String(c.value)) opt.selected = true;
      sel.appendChild(opt);
    });
    return ruleSelectWrap(sel);
  }
  if (c.field === "account") {
    const sel = el("select", { class: "select rule-cond-val", onChange: (e) => { c.value = e.target.value; } });
    sel.appendChild(el("option", { value: "", text: "Choose account…" }));
    (state.accountsList || []).forEach((a) => {
      const opt = el("option", { value: String(a.id), text: a.label || a.email });
      if (String(a.id) === String(c.value)) opt.selected = true;
      sel.appendChild(opt);
    });
    return ruleSelectWrap(sel);
  }
  if (c.field === "source_type") {
    const sel = el("select", { class: "select rule-cond-val", onChange: (e) => { c.value = e.target.value; } });
    [["", "Choose…"], ["drive", "Google Drive"], ["email", "Email"]].forEach(([val, text]) => {
      const opt = el("option", { value: val, text });
      if (val === c.value) opt.selected = true;
      sel.appendChild(opt);
    });
    return ruleSelectWrap(sel);
  }
  if (c.field === "date") {
    const wrap = el("div", { class: "rule-date-val" });
    wrap.appendChild(el("input", {
      type: "number", min: "1", class: "add-input rule-num-input", placeholder: "30",
      value: c.value, onInput: (e) => { c.value = e.target.value; },
    }));
    wrap.appendChild(el("span", { class: "rule-op", text: "days" }));
    return wrap;
  }
  // folder_path | filename — free text.
  return el("input", {
    class: "add-input rule-text-input",
    placeholder: c.field === "filename" ? "text in filename" : "text in folder path",
    value: c.value, onInput: (e) => { c.value = e.target.value; },
  });
}

async function submitRule() {
  const folderId = parseInt(builderFolderId, 10);
  if (!folderId) { toast("Pick a collection to file matches into."); return; }

  // Collect only complete conditions; skip half-filled rows. Cast ids/days to
  // ints and source_type to its enum so the payload matches the contract types.
  const conditions = [];
  for (const c of builderConds) {
    const op = ruleFieldSpec(c.field).op;
    if (c.field === "vendor" || c.field === "account") {
      const id = parseInt(c.value, 10);
      if (id) conditions.push({ field: c.field, op, value: id });
    } else if (c.field === "date") {
      const n = parseInt(c.value, 10);
      if (n && n >= 1) conditions.push({ field: c.field, op, value: n });
    } else if (c.field === "source_type") {
      if (c.value === "drive" || c.value === "email") conditions.push({ field: c.field, op, value: c.value });
    } else {
      const v = (c.value || "").trim();
      if (v) conditions.push({ field: c.field, op, value: v });
    }
  }

  if (!conditions.length) {
    toast("Add at least one complete condition — an empty rule matches nothing.");
    return;
  }

  const payload = { folder_id: folderId, enabled: true, conditions };
  const name = builderName.trim();
  if (name) payload.name = name;

  try {
    const created = await api.createCollectionRule(payload);
    rulesData.push(created);
    builderFolderId = "";
    builderName = "";
    builderConds = [{ field: "vendor", value: "" }];
    toast("Rule added · matching images file on the next sync.");
    render();
  } catch (e) {
    if (e && e.status === 422) toast("That rule has an invalid condition.");
    else toast("Could not add that rule.");
  }
}
