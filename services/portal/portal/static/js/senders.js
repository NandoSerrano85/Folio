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

let root = null;
let sendersData = [];

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
