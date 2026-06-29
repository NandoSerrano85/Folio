// demo-data.js — embedded, offline dataset for static/demo.html.
//
// Active only when window.FOLIO_DEMO === true. It implements the SAME response
// shapes as the real FastAPI endpoints so the production CSS/JS render the full
// UI with no backend. Thumbnails/full images are inline SVG data URIs (no
// network). Only the data source differs from production — zero styling drift.

// ----------------------------------------------------------------- seeded -- //
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const iso = (d) => d.toISOString().slice(0, 10);
function svgImg(seed, w, h) {
  let x = 5381;
  for (let i = 0; i < seed.length; i++) x = ((x * 33) ^ seed.charCodeAt(i)) >>> 0;
  const h1 = x % 360;
  const h2 = (h1 + 25 + ((x >>> 9) % 70)) % 360;
  const s = `<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'>` +
    `<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>` +
    `<stop offset='0' stop-color='hsl(${h1},42%,60%)'/>` +
    `<stop offset='1' stop-color='hsl(${h2},38%,44%)'/></linearGradient></defs>` +
    `<rect width='100%' height='100%' fill='url(#g)'/></svg>`;
  return "data:image/svg+xml," + encodeURIComponent(s);
}

// --------------------------------------------------------------- reference -- //
const VENDORS = [
  { id: 1, name: "Northwind Supply", domain: "northwind-supply.com", adapter_key: "northwind", login_required: false, notes: null, sender: "orders@northwind-supply.com" },
  { id: 2, name: "Atelier Mori", domain: "ateliermori.com", adapter_key: "mori", login_required: true, notes: null, sender: "studio@ateliermori.com" },
  { id: 3, name: "Cedar & Pine", domain: "cedarandpine.co", adapter_key: "cedar", login_required: false, notes: null, sender: "hello@cedarandpine.co" },
  { id: 4, name: "Halcyon Studio", domain: "halcyonstudio.com", adapter_key: "halcyon", login_required: false, notes: null, sender: "sales@halcyonstudio.com" },
  { id: 5, name: "Meridian Goods", domain: "meridiangoods.com", adapter_key: "meridian", login_required: false, notes: null, sender: "team@meridiangoods.com" },
  { id: 6, name: "Brightleaf", domain: "brightleaf.co", adapter_key: "brightleaf", login_required: false, notes: null, sender: "press@brightleaf.co" },
  { id: 7, name: "Verde Lab", domain: "verdelab.io", adapter_key: "verde", login_required: true, notes: null, sender: "lab@verdelab.io" },
];
const VMAP = {}; VENDORS.forEach((v) => { VMAP[v.id] = v; });

const ACCOUNTS = [
  { id: 1, provider: "gmail", email: "purchasing@folio.studio", label: "Purchasing", status: "active" },
  { id: 2, provider: "gmail", email: "design@folio.studio", label: "Design", status: "active" },
  { id: 3, provider: "gmail", email: "ops@folio.studio", label: "Ops", status: "active" },
  { id: 4, provider: "drive", email: "marketing@folio.studio", label: "Drive · Marketing", status: "active" },
  { id: 5, provider: "drive", email: "vendors@folio.studio", label: "Drive · Vendors", status: "active" },
  { id: 6, provider: "drive", email: "archive@folio.studio", label: "Drive · Archive", status: "paused" },
];
const AMAP = {}; ACCOUNTS.forEach((a) => { AMAP[a.id] = a; });

// Folder ids 1..6 (the synthetic "All Images" lives client-side, not here).
const FOLDERS = [
  { id: 1, name: "Recent Purchases", parent_id: null, sort_order: 0 },
  { id: 2, name: "Receipts & Invoices", parent_id: null, sort_order: 1 },
  { id: 3, name: "Marketing Assets", parent_id: null, sort_order: 2 },
  { id: 4, name: "Vendor Lookbooks", parent_id: null, sort_order: 3 },
  { id: 5, name: "Project Atlas", parent_id: null, sort_order: 4 },
  { id: 6, name: "Archive", parent_id: null, sort_order: 5 },
];

// ----------------------------------------------------------------- images -- //
const IMAGES = [];
const MEMBERSHIP = {}; // imageId -> Set(folderId)
(function build() {
  const rng = mulberry32(20260627);
  const pick = (arr) => arr[Math.floor(rng() * arr.length)];
  const start = Date.UTC(2024, 0, 1), end = Date.UTC(2026, 5, 15);
  const gmail = ACCOUNTS.filter((a) => a.provider === "gmail");
  const drive = ACCOUNTS.filter((a) => a.provider === "drive");
  const kinds = ["lookbook", "product", "catalog", "swatch", "hero", "detail", "press", "set", "frame", "plate"];
  const subj = ["SS26 lookbook assets", "Updated product photography", "New season swatches", "Catalogue spreads — hi-res", "Press kit images", "Reorder — product shots", "Campaign selects"];
  const dpaths = ["Marketing/2026/Campaign", "Vendors/Lookbooks", "Archive/2024", "Marketing/Web/Hero", "Vendors/Receipts", "Product/Studio"];

  for (let i = 0; i < 40; i++) {
    const vendor = pick(VENDORS);
    const isEmail = rng() < 0.62;
    const account = isEmail ? pick(gmail) : pick(drive);
    const t = start + Math.floor(rng() * (end - start));
    const trueDate = new Date(t);
    let nasT = t + (2 + Math.floor(rng() * 30)) * 86400000;
    if (nasT > end + 4 * 86400000) nasT = end;
    const nasDate = new Date(nasT);
    const isReceipt = isEmail && rng() < 0.22;
    const num = 1000 + Math.floor(rng() * 9000);
    const kind = pick(kinds);
    const ext = pick(["jpg", "jpg", "jpg", "png"]);
    const filename = isReceipt ? `receipt-${num}.png` : `${kind}-${("0" + (1 + Math.floor(rng() * 98))).slice(-2)}.${ext}`;
    let sender = null, subject = null, drivePath = null;
    if (isEmail) {
      if (isReceipt) { sender = "receipts@squareup.com"; subject = `Receipt for order #${num}`; }
      else if (rng() < 0.1) { sender = "newsletter@designweekly.com"; subject = "This week in design — assets enclosed"; }
      else { sender = vendor.sender; subject = pick(subj); }
    } else {
      drivePath = `${pick(dpaths)}/${filename}`;
    }
    const vendorUrl = isEmail
      ? `https://${vendor.domain}/products/${kind}-${num}`
      : `https://drive.google.com/file/d/demo${i}${num}/view`;
    const w = 1280, h = 960;
    const bytes = 240000 + Math.floor(rng() * 5_000_000);
    IMAGES.push({
      id: i + 1, filename, ext, bytes, width: w, height: h,
      vendorId: vendor.id, vendorName: vendor.name,
      accountId: account.id, accountEmail: account.email, accountLabel: account.label,
      sourceType: isEmail ? "email" : "drive",
      sourceDate: trueDate.toISOString(), sourceDateOrigin: isEmail ? "email_date" : "drive_created",
      ingestedAt: nasDate.toISOString(),
      sender, subject, drivePath, vendorUrl,
      thumb: svgImg(`t${i}-${num}`, 560, 420), full: svgImg(`t${i}-${num}`, 1280, 960),
    });

    const f = new Set();
    const age = (end - t) / 86400000;
    if (!isEmail) { /* drive */ } else if (age < 150) f.add(1);
    if (filename.indexOf("receipt") === 0) f.add(2);
    if (account.id === 4 || (drivePath && drivePath.indexOf("Marketing") === 0)) f.add(3);
    if (vendor.id === 2 || vendor.id === 4 || vendor.id === 3) f.add(4);
    if (i % 5 === 0) f.add(5);
    if (trueDate.getUTCFullYear() < 2025) f.add(6);
    MEMBERSHIP[i + 1] = f;
  }
})();

function folderCount(fid) {
  let n = 0;
  for (const id in MEMBERSHIP) if (MEMBERSHIP[id].has(fid)) n++;
  return n;
}
function accountImageCount(aid) {
  return IMAGES.filter((im) => im.accountId === aid).length;
}
function vendorImageCount(vid) {
  return IMAGES.filter((im) => im.vendorId === vid).length;
}

// ---------------------------------------------------------------- senders -- //
const SENDERS = [];
(function buildSenders() {
  let id = 1;
  const countFor = (addr) => IMAGES.filter((im) => im.sender === addr).length;
  VENDORS.forEach((v) => {
    SENDERS.push({ id: id++, account_id: 1, address: v.sender, domain: v.sender.split("@")[1], display_name: v.name, vendor_id: v.id, enabled: true, discovered_count: countFor(v.sender), last_seen_at: null });
  });
  SENDERS.push({ id: id++, account_id: 1, address: "receipts@squareup.com", domain: "squareup.com", display_name: "Square", vendor_id: null, enabled: true, discovered_count: countFor("receipts@squareup.com"), last_seen_at: null });
  SENDERS.push({ id: id++, account_id: 1, address: "newsletter@designweekly.com", domain: "designweekly.com", display_name: "Design Weekly", vendor_id: null, enabled: false, discovered_count: countFor("newsletter@designweekly.com"), last_seen_at: null });
})();

// ------------------------------------------------------------------- rules -- //
// v3 automation rules: ONE condition (sender/domain/filename/subject/account)
// that assigns a vendor and/or files into a collection. Mirrors the backend.
const RULES = [
  { id: 1, field: "domain", value: "@northwind-supply.com", account_id: null, vendor_id: 1, folder_id: 1, enabled: true },
  { id: 2, field: "filename", value: "receipt", account_id: null, vendor_id: null, folder_id: 2, enabled: true },
  { id: 3, field: "subject", value: "lookbook", account_id: null, vendor_id: 2, folder_id: 4, enabled: true },
];
function ruleMatch(im, r) {
  if (r.field === "account") return im.accountId === r.account_id;
  const v = (r.value || "").toLowerCase().trim();
  if (!v) return false;
  if (r.field === "sender") return (im.sender || "").toLowerCase().includes(v);
  if (r.field === "domain") return (im.sender || "").toLowerCase().includes("@" + v.replace(/^@/, ""));
  if (r.field === "filename") return (im.filename || "").toLowerCase().includes(v);
  if (r.field === "subject") return (im.subject || "").toLowerCase().includes(v);
  return false;
}
function ruleMatchCount(r) { return IMAGES.reduce((n, im) => n + (ruleMatch(im, r) ? 1 : 0), 0); }
function ruleOut(r) {
  const v = r.vendor_id ? VMAP[r.vendor_id] : null;
  const f = r.folder_id ? FOLDERS.find((x) => x.id === r.folder_id) : null;
  const a = r.account_id ? AMAP[r.account_id] : null;
  return {
    id: r.id, field: r.field, value: r.value, enabled: r.enabled,
    account_id: r.account_id, account_name: a ? a.email : null,
    vendor_id: r.vendor_id, vendor_name: v ? v.name : null,
    folder_id: r.folder_id, folder_name: f ? f.name : null,
    match_count: ruleMatchCount(r),
  };
}
function applyOneRule(r) {
  let vendored = 0, filed = 0;
  if (!r.enabled) return { vendored, filed };
  const matched = IMAGES.filter((im) => ruleMatch(im, r));
  if (r.vendor_id) {
    const v = VMAP[r.vendor_id];
    matched.forEach((im) => { im.vendorId = r.vendor_id; im.vendorName = v ? v.name : im.vendorName; });
    vendored = matched.length;
  }
  if (r.folder_id) {
    matched.forEach((im) => { const m = MEMBERSHIP[im.id]; if (m && !m.has(r.folder_id)) { m.add(r.folder_id); filed++; } });
  }
  return { vendored, filed };
}

// ----------------------------------------------------------------- mapping -- //
function listItem(im) {
  return {
    id: im.id, filename: im.filename, source_date: im.sourceDate, source_date_origin: im.sourceDateOrigin,
    vendor: im.vendorName, account: im.accountEmail, thumb_url: im.thumb,
    ext: im.ext, bytes: im.bytes, width: im.width, height: im.height,
  };
}
function detailOf(im) {
  return {
    id: im.id, sha256: "demo" + im.id, filename: im.filename, stored_path: `/demo/${im.filename}`,
    ext: im.ext, mime: im.ext === "png" ? "image/png" : "image/jpeg", bytes: im.bytes,
    width: im.width, height: im.height, source_date: im.sourceDate, source_date_origin: im.sourceDateOrigin,
    ingested_at: im.ingestedAt, thumb_url: im.thumb, file_url: im.full,
    sources: [{
      id: im.id, source_type: im.sourceType, source_id: "demo-" + im.id, account_id: im.accountId,
      account: im.accountEmail, vendor_id: im.vendorId, vendor: im.vendorName, vendor_url: im.vendorUrl,
      email_subject: im.subject, email_sender: im.sender, email_message_id: null,
      drive_folder_path: im.drivePath, drive_created_time: im.sourceType === "drive" ? im.sourceDate : null,
      drive_modified_time: null, drive_owner: im.sourceType === "drive" ? im.accountEmail : null,
      created_at: im.ingestedAt,
    }],
  };
}
function delay(v) { return new Promise((r) => setTimeout(() => r(v), 60)); }

// -------------------------------------------------------------------- api -- //
export const api = {
  async me() { throw new Error("demo: not authenticated"); }, // show login first
  async login(_payload) { return delay({ username: "Avery Cole" }); },
  async logout() { return delay({ status: "ok" }); },

  async listImages(params = {}) {
    let list = IMAGES.slice();
    const num = (v) => (v == null ? null : parseInt(v, 10));
    const folder = num(params.folder), account = num(params.account), vendor = num(params.vendor);
    if (folder != null) list = list.filter((im) => MEMBERSHIP[im.id].has(folder));
    if (account != null) list = list.filter((im) => im.accountId === account);
    if (vendor != null) list = list.filter((im) => im.vendorId === vendor);
    if (params.sender) { const p = String(params.sender).toLowerCase(); list = list.filter((im) => (im.sender || "").toLowerCase().includes(p)); }
    if (params.date_from) list = list.filter((im) => im.sourceDate.slice(0, 10) >= params.date_from);
    if (params.date_to) list = list.filter((im) => im.sourceDate.slice(0, 10) <= params.date_to);
    if (params.q) {
      const q = String(params.q).toLowerCase();
      list = list.filter((im) => `${im.filename} ${im.vendorName} ${im.sender || ""} ${im.subject || ""} ${im.accountEmail} ${im.drivePath || ""}`.toLowerCase().includes(q));
    }
    const cmp = {
      newest: (a, b) => new Date(b.sourceDate) - new Date(a.sourceDate),
      oldest: (a, b) => new Date(a.sourceDate) - new Date(b.sourceDate),
      name: (a, b) => a.filename.localeCompare(b.filename),
      vendor: (a, b) => a.vendorName.localeCompare(b.vendorName) || new Date(b.sourceDate) - new Date(a.sourceDate),
      account: (a, b) => a.accountEmail.localeCompare(b.accountEmail) || new Date(b.sourceDate) - new Date(a.sourceDate),
    }[params.sort] || ((a, b) => new Date(b.sourceDate) - new Date(a.sourceDate));
    list.sort(cmp);

    const pageSize = [25, 50, 100, 200].includes(num(params.page_size)) ? num(params.page_size) : 25;
    const total = list.length;
    const pages = total ? Math.ceil(total / pageSize) : 0;
    let page = num(params.page) || 1;
    if (pages && page > pages) page = pages;
    const offset = (page - 1) * pageSize;
    return delay({ items: list.slice(offset, offset + pageSize).map(listItem), total, page, page_size: pageSize, pages });
  },

  async imageDetail(id) {
    const im = IMAGES.find((x) => x.id === parseInt(id, 10));
    if (!im) throw new Error("not found");
    return delay(detailOf(im));
  },

  async folders() {
    return delay(FOLDERS.map((f) => ({ ...f, image_count: folderCount(f.id), children: [] })));
  },
  async createFolder(name, parentId) {
    const id = Math.max(0, ...FOLDERS.map((f) => f.id)) + 1;
    const f = { id, name, parent_id: parentId ?? null, sort_order: FOLDERS.length };
    FOLDERS.push(f);
    return delay(f);
  },
  async addImagesToFolder(folderId, imageIds) {
    imageIds.forEach((id) => { if (MEMBERSHIP[id]) MEMBERSHIP[id].add(parseInt(folderId, 10)); });
    return delay({ status: "ok" });
  },
  async removeImageFromFolder(folderId, imageId) {
    if (MEMBERSHIP[imageId]) MEMBERSHIP[imageId].delete(parseInt(folderId, 10));
    return delay({ status: "ok" });
  },

  async senders(accountId) {
    const list = accountId ? SENDERS.filter((s) => s.account_id === parseInt(accountId, 10)) : SENDERS;
    return delay(list.map((s) => ({ ...s })));
  },
  async createSender(payload) {
    const id = Math.max(0, ...SENDERS.map((s) => s.id)) + 1;
    const address = payload.address || `@${payload.domain}`;
    const s = { id, account_id: payload.account_id, address, domain: payload.domain || (address.split("@")[1] || null), display_name: null, vendor_id: payload.vendor_id ?? null, enabled: payload.enabled !== false, discovered_count: 0, last_seen_at: null };
    SENDERS.push(s);
    return delay({ ...s });
  },
  async updateSender(id, patch) {
    const s = SENDERS.find((x) => x.id === parseInt(id, 10));
    if (!s) throw new Error("not found");
    if (patch.enabled !== undefined) s.enabled = patch.enabled;
    if (patch.vendor_id !== undefined) s.vendor_id = patch.vendor_id;
    return delay({ ...s });
  },

  async collectionRules() { return delay(RULES.map(ruleOut)); },
  async createCollectionRule(payload) {
    const id = Math.max(0, ...RULES.map((r) => r.id)) + 1;
    const r = {
      id, field: payload.field, value: payload.value ?? null,
      account_id: payload.field === "account" ? (payload.account_id ?? null) : null,
      vendor_id: payload.vendor_id ?? null, folder_id: payload.folder_id ?? null,
      enabled: payload.enabled !== false,
    };
    RULES.push(r); applyOneRule(r);
    return delay(ruleOut(r));
  },
  async updateCollectionRule(id, patch) {
    const r = RULES.find((x) => x.id === parseInt(id, 10));
    if (!r) throw new Error("not found");
    Object.assign(r, patch);
    if (patch.enabled) applyOneRule(r);
    return delay(ruleOut(r));
  },
  async deleteCollectionRule(id) {
    const i = RULES.findIndex((x) => x.id === parseInt(id, 10));
    if (i >= 0) RULES.splice(i, 1);
    return delay({ status: "ok" });
  },
  async applyCollectionRules() {
    const applied = {}; let total_filed = 0, total_vendored = 0;
    RULES.filter((r) => r.enabled).forEach((r) => {
      const c = applyOneRule(r); applied[r.id] = c;
      total_filed += c.filed; total_vendored += c.vendored;
    });
    return delay({ applied, total_filed, total_vendored });
  },

  async vendors() { return delay(VENDORS.map(({ sender, ...v }) => v)); },

  async accounts() {
    return delay(ACCOUNTS.map((a) => ({ ...a, image_count: accountImageCount(a.id), source_count: accountImageCount(a.id) })));
  },

  async stats() {
    const byAccount = ACCOUNTS.map((a) => ({ name: a.email, count: accountImageCount(a.id) })).filter((r) => r.count).sort((a, b) => b.count - a.count);
    const byVendor = VENDORS.map((v) => ({ name: v.name, count: vendorImageCount(v.id) })).filter((r) => r.count).sort((a, b) => b.count - a.count);
    const latest = IMAGES.reduce((m, im) => (im.sourceDate > m ? im.sourceDate : m), IMAGES[0].sourceDate);
    const bytes = IMAGES.reduce((s, im) => s + im.bytes, 0);
    return delay({ total_images: IMAGES.length, by_account: byAccount, by_vendor: byVendor, latest_source_date: latest, library_bytes: bytes });
  },

  async downloadImages(imageIds) {
    // Offline demo: no real bytes to stream; resolve so the toast flow runs.
    return delay({ ok: true, count: imageIds.length });
  },
};
