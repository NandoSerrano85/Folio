// rules.js — "Automation rules": the v3 rule engine UI.
//
// A rule is ONE condition + up to two actions. The condition is a field
// (sender|domain|filename|subject|account) plus either a text value or, for
// `account`, an account id. The actions assign a vendor and/or file the image
// into a collection — at least one is required. On every sync (and when the
// user hits "Run all rules now") matching images get the vendor assigned and/or
// are filed into the collection. The contract lives in folio_core.rules; this
// screen is a thin builder + list over the /api/collection-rules endpoints.
//
// All rendering goes through el()/textContent, so API-provided names (vendors,
// collections, accounts, and the user's own rule values) can never inject markup.

import { el, clear, icons, vendorColor } from "./util.js";
import { state, toast } from "./state.js";
import * as api from "./api.js";
import { flattenFolders } from "./folders.js";
import { loadReference } from "./gallery.js";

let root = null;
let rulesData = [];

// Builder working state, kept at module scope so an in-progress rule survives a
// re-render (e.g. toggling another rule) instead of being dropped.
let draft = { field: "sender", value: "", accountId: "", vendorId: "", folderId: "" };

const FIELD_OPTIONS = [
  { value: "sender", label: "Sender" },
  { value: "domain", label: "Email domain" },
  { value: "filename", label: "Filename" },
  { value: "subject", label: "Subject" },
  { value: "account", label: "Source account" },
];

const VALUE_LABEL = {
  sender: "Sender contains", domain: "Domain",
  filename: "Filename contains", subject: "Subject contains", account: "Account",
};
const VALUE_PLACEHOLDER = {
  sender: "orders@vendor.com", domain: "@vendor.com",
  filename: "invoice", subject: "lookbook",
};

// Grid shared by the rules header + rows: When · Assigns · Matches · On.
const RULE_GRID = "1.5fr 1.3fr 80px 104px";

export function mountRules() {
  if (!root) root = el("div", { class: "rules-page" });
  load();
  return root;
}

async function load() {
  renderSkeleton();
  try {
    if (!state.vendorsList.length || !state.accountsList.length) {
      const [vends, accts] = await Promise.all([api.vendors(), api.accounts()]);
      state.vendorsList = vends || state.vendorsList;
      state.accountsList = accts || state.accountsList;
    }
    if (!state.foldersFlat.length) {
      try { state.foldersFlat = flattenFolders((await api.folders()) || []); } catch (_) { /* leave empty */ }
    }
    rulesData = (await api.collectionRules()) || [];
  } catch (_) {
    rulesData = [];
    toast("Could not load rules.");
  }
  render();
}

function renderSkeleton() {
  clear(root);
  root.appendChild(el("h1", { class: "page-title", text: "Automation rules" }));
  root.appendChild(el("div", { class: "loading-note", style: { textAlign: "left" }, text: "Loading…" }));
}

function render() {
  clear(root);

  root.appendChild(el("h1", { class: "page-title", text: "Automation rules" }));
  root.appendChild(el("p", {
    class: "page-intro",
    text: "Rules tag images automatically as they're imported. When an image matches a condition, Folio assigns the vendor and drops it into the collection you choose — so the library stays organized without manual sorting.",
  }));

  const onCount = rulesData.filter((r) => r.enabled).length;
  root.appendChild(el("div", { class: "rules-stats" }, [
    el("span", { text: `${rulesData.length} rule${rulesData.length === 1 ? "" : "s"}` }),
    el("span", { text: "·" }),
    el("span", { text: `${onCount} on` }),
  ]));

  root.appendChild(buildBuilder());
  root.appendChild(buildActiveRules());

  root.appendChild(el("p", {
    class: "section-note",
    text: "Rules run on each import (hourly). “Matches” counts images in the library that currently fit the condition. Turning a rule off stops future tagging; images already tagged keep their vendor and collection.",
  }));
}

// ---------------------------------------------------------------- builder -- //
function selectWrap(selectEl) {
  const wrap = el("div", { class: "select-wrap rule-select" }, [selectEl]);
  const chev = el("span", { class: "select-chevron" });
  chev.appendChild(icons.chevronDown(12));
  wrap.appendChild(chev);
  return wrap;
}

function field(capText, controlNode, modifier) {
  return el("div", { class: `rule-field${modifier ? ` ${modifier}` : ""}` }, [
    el("div", { class: "rule-cap", text: capText }),
    controlNode,
  ]);
}

function buildBuilder() {
  const panel = el("div", { class: "rule-builder" });
  panel.appendChild(el("div", { class: "rule-builder-head", text: "New rule" }));

  // --- Row 1: condition (field + value/account) ---
  const fieldSel = el("select", { class: "select", onChange: (e) => { draft.field = e.target.value; draft.value = ""; render(); } });
  FIELD_OPTIONS.forEach((o) => {
    const opt = el("option", { value: o.value, text: o.label });
    if (o.value === draft.field) opt.selected = true;
    fieldSel.appendChild(opt);
  });
  const fieldCol = field("When an image's…", selectWrap(fieldSel), "w-when");

  let valueCol;
  if (draft.field === "account") {
    const accSel = el("select", { class: "select", onChange: (e) => { draft.accountId = e.target.value; } });
    const accounts = state.accountsList || [];
    if (!accounts.length) {
      accSel.appendChild(el("option", { value: "", text: "No accounts connected" }));
    } else if (!draft.accountId) {
      draft.accountId = String(accounts[0].id);
    }
    accounts.forEach((a) => {
      const opt = el("option", { value: String(a.id), text: a.label || a.email });
      if (String(a.id) === String(draft.accountId)) opt.selected = true;
      accSel.appendChild(opt);
    });
    valueCol = field(VALUE_LABEL.account, selectWrap(accSel), "grow");
  } else {
    const valInput = el("input", {
      class: "rule-input", placeholder: VALUE_PLACEHOLDER[draft.field] || "",
      value: draft.value,
      onInput: (e) => { draft.value = e.target.value; },
      onKeyDown: (e) => { if (e.key === "Enter") submitRule(); },
    });
    valueCol = field(VALUE_LABEL[draft.field] || "Value", valInput, "grow");
  }

  panel.appendChild(el("div", { class: "rule-row" }, [fieldCol, valueCol]));

  // --- Row 2: actions (vendor + collection) + Add ---
  const vendorSel = el("select", { class: "select", onChange: (e) => { draft.vendorId = e.target.value; } });
  vendorSel.appendChild(el("option", { value: "", text: "— No vendor —" }));
  (state.vendorsList || []).forEach((v) => {
    const opt = el("option", { value: String(v.id), text: v.name });
    if (String(v.id) === String(draft.vendorId)) opt.selected = true;
    vendorSel.appendChild(opt);
  });

  const folderSel = el("select", { class: "select", onChange: (e) => { draft.folderId = e.target.value; } });
  folderSel.appendChild(el("option", { value: "", text: "— No collection —" }));
  (state.foldersFlat || []).forEach((f) => {
    const opt = el("option", { value: String(f.id), text: `${"— ".repeat(f.depth || 0)}${f.name}` });
    if (String(f.id) === String(draft.folderId)) opt.selected = true;
    folderSel.appendChild(opt);
  });

  const addBtn = el("button", { class: "btn-primary btn-rule", text: "Add rule", onClick: submitRule });

  panel.appendChild(el("div", { class: "rule-row rule-row-actions" }, [
    field("Assign vendor", selectWrap(vendorSel), "w-assign"),
    field("Add to collection", selectWrap(folderSel), "w-assign"),
    el("div", { class: "rule-spacer" }),
    el("div", { class: "rule-submit" }, [addBtn]),
  ]));

  return panel;
}

async function submitRule() {
  const f = draft.field;
  const isAccount = f === "account";

  const payload = { field: f, enabled: true };
  if (isAccount) {
    const accountId = parseInt(draft.accountId, 10);
    if (!accountId) { toast("Pick a source account to match on."); return; }
    payload.account_id = accountId;
  } else {
    const value = (draft.value || "").trim();
    if (!value) { toast("Enter a value to match on."); return; }
    payload.value = value;
  }

  const vendorId = draft.vendorId ? parseInt(draft.vendorId, 10) : null;
  const folderId = draft.folderId ? parseInt(draft.folderId, 10) : null;
  if (!vendorId && !folderId) { toast("Pick a vendor and/or collection to assign."); return; }
  if (vendorId) payload.vendor_id = vendorId;
  if (folderId) payload.folder_id = folderId;

  try {
    const created = await api.createCollectionRule(payload);
    rulesData.push(created);
    draft = { field: "sender", value: "", accountId: "", vendorId: "", folderId: "" };
    const n = created && typeof created.match_count === "number" ? created.match_count : null;
    toast(n == null
      ? "Rule added · runs on the next import."
      : `Rule added · ${n} image${n === 1 ? "" : "s"} match. Run it now to tag them.`);
    render();
  } catch (e) {
    if (e && e.status === 422) toast("That rule is incomplete — check the condition and actions.");
    else toast("Could not add that rule.");
  }
}

// ----------------------------------------------------------- active rules -- //
function buildActiveRules() {
  const frag = document.createDocumentFragment();

  const head = el("div", { class: "section-head tight" }, [
    el("h2", { text: "Active rules" }),
    el("button", { class: "btn-ghost btn-run-rules", text: "Run all rules now", onClick: applyRulesNow }),
  ]);
  frag.appendChild(head);

  const table = el("div", { class: "table" });
  table.appendChild(el("div", { class: "thead", style: { display: "grid", gridTemplateColumns: RULE_GRID } }, [
    el("span", { text: "When" }),
    el("span", { text: "Assigns" }),
    el("span", { class: "th-r", text: "Matches" }),
    el("span", { class: "th-r", text: "On" }),
  ]));

  if (!rulesData.length) {
    table.appendChild(el("div", { class: "empty-table", text: "No rules yet. Build one above to tag images automatically." }));
  } else {
    rulesData.forEach((r) => table.appendChild(ruleRow(r)));
  }
  frag.appendChild(table);
  return frag;
}

// A rule's condition rendered as a readable phrase.
function conditionText(r) {
  if (r.field === "account") {
    return `Source account is ${r.account_name || `account #${r.account_id}`}`;
  }
  const v = r.value || "";
  switch (r.field) {
    case "domain": return `Domain ${v.startsWith("@") ? v : `@${v}`}`;
    case "filename": return `Filename contains “${v}”`;
    case "sender": return `Sender contains “${v}”`;
    case "subject": return `Subject contains “${v}”`;
    default: return `${r.field} “${v}”`;
  }
}

function ruleRow(r) {
  const wrap = el("div", { class: `trow${r.enabled ? "" : " off"}` });

  // When (condition)
  const condCell = el("div", { class: "rule-cond", text: conditionText(r) });

  // Assigns (vendor chip + collection chip)
  const assigns = el("div", { class: "rule-assigns" });
  if (r.vendor_id != null && r.vendor_name) {
    assigns.appendChild(el("span", { class: "rule-chip" }, [
      el("span", { class: "vendor-dot", style: { background: vendorColor(r.vendor_name) } }),
      el("span", { text: r.vendor_name }),
    ]));
  }
  if (r.folder_id != null && r.folder_name) {
    assigns.appendChild(el("span", { class: "rule-chip", text: `+ ${r.folder_name}` }));
  }
  if (!assigns.childNodes.length) {
    assigns.appendChild(el("span", { class: "rule-noassign", text: "—" }));
  }

  // Matches
  const matches = typeof r.match_count === "number" ? r.match_count : 0;
  const matchCell = el("div", { class: "cell-num", text: String(matches) });

  // On (toggle + delete)
  const toggle = el("button", {
    class: `toggle${r.enabled ? " on" : ""}`,
    onClick: () => toggleRule(r, wrap, toggle),
  }, [el("span", { class: "toggle-knob" })]);
  const del = el("button", { class: "rule-del", title: "Delete rule", onClick: () => removeRule(r) });
  del.appendChild(icons.trash(15));
  const onCell = el("div", { class: "rule-oncell" }, [toggle, del]);

  wrap.appendChild(el("div", { class: "tcells", style: { display: "grid", gridTemplateColumns: RULE_GRID } }, [
    condCell, assigns, matchCell, onCell,
  ]));
  return wrap;
}

// Toggle in place (no full re-render) so the in-progress builder is undisturbed.
async function toggleRule(r, rowEl, toggleEl) {
  const next = !r.enabled;
  try {
    await api.updateCollectionRule(r.id, { enabled: next });
    r.enabled = next;
    toggleEl.classList.toggle("on", next);
    rowEl.classList.toggle("off", !next);
    toast(next ? "Rule on · tags new imports." : "Rule off · already-tagged images stay.");
    // Refresh the header "{n} on" count quietly.
    const stats = root.querySelector(".rules-stats span:last-child");
    if (stats) stats.textContent = `${rulesData.filter((x) => x.enabled).length} on`;
  } catch (_) {
    toast("Could not update rule.");
  }
}

async function removeRule(r) {
  if (!window.confirm("Delete this rule? Images already tagged keep their vendor and collection.")) return;
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
    const filed = (res && res.total_filed) || 0;
    const vendored = (res && res.total_vendored) || 0;
    if (!filed && !vendored) {
      toast("Rules ran · nothing new to tag.");
    } else {
      const parts = [];
      if (vendored) parts.push(`assigned vendor to ${vendored} image${vendored === 1 ? "" : "s"}`);
      if (filed) parts.push(`filed ${filed} into collections`);
      toast(`Rules ran · ${parts.join(" · ")}.`);
    }
    await loadReference(); // refresh collection/vendor counts in the shared sidebar state
    try { rulesData = (await api.collectionRules()) || rulesData; } catch (_) { /* keep current */ }
    render();
  } catch (_) {
    toast("Could not run rules.");
  }
}
