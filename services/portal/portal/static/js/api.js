// api.js — typed fetch client for the FROZEN Folio JSON API.
//
// Every path and query-param name below is part of the contract — do not rename.
// When `window.FOLIO_DEMO` is set (static/demo.html), all calls are served by an
// embedded in-memory dataset (demo-data.js) so the full UI renders with NO
// backend. Only the data source differs; the CSS/JS are identical to production.

const DEMO = typeof window !== "undefined" && window.FOLIO_DEMO === true;

let _demo = null;
async function demo() {
  if (!_demo) _demo = await import("./demo-data.js");
  return _demo;
}

/** HTTP error carrying status + parsed body for inline handling. */
export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function req(method, path, body, { raw = false } = {}) {
  const opts = {
    method,
    credentials: "same-origin",
    headers: {},
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (raw) return res;
  if (res.status === 204) return null;
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await res.json().catch(() => null);
  }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : null;
    throw new ApiError(res.status, detail);
  }
  return data;
}

function qs(params) {
  const u = new URLSearchParams();
  for (const k in params) {
    const v = params[k];
    if (v === undefined || v === null || v === "") continue;
    u.set(k, v);
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ------------------------------------------------------------------ auth -- //
export async function me() {
  if (DEMO) return (await demo()).api.me();
  return req("GET", "/api/auth/me");
}
/** payload is either {token} or {username, password}. */
export async function login(payload) {
  if (DEMO) return (await demo()).api.login(payload);
  return req("POST", "/api/auth/login", payload);
}
export async function logout() {
  if (DEMO) return (await demo()).api.logout();
  return req("POST", "/api/auth/logout", {});
}

// ---------------------------------------------------------------- images -- //
export async function listImages(params) {
  if (DEMO) return (await demo()).api.listImages(params);
  return req("GET", `/api/images${qs(params)}`);
}
export async function imageDetail(id) {
  if (DEMO) return (await demo()).api.imageDetail(id);
  return req("GET", `/api/images/${id}`);
}
export function thumbUrl(id, size) {
  // The list already returns absolute thumb_url; this is a convenience builder.
  return `/api/images/${id}/thumb${size ? `?size=${size}` : ""}`;
}
export function fileUrl(id) {
  return `/api/images/${id}/file`;
}
/**
 * Bulk-assign (or clear) the vendor on every source of the given images.
 * Pass {vendorId} to assign an existing vendor, {vendorName} to get-or-create
 * by name, or neither (both null) to CLEAR the vendor. Matches the FROZEN
 * POST /api/images/vendor contract.
 */
export async function setImagesVendor(imageIds, { vendorId = null, vendorName = null } = {}) {
  if (DEMO) {
    const d = await demo();
    if (d.api.setImagesVendor) return d.api.setImagesVendor(imageIds, { vendorId, vendorName });
  }
  return req("POST", "/api/images/vendor", {
    image_ids: imageIds,
    vendor_id: vendorId,
    vendor_name: vendorName,
  });
}

// --------------------------------------------------------------- folders -- //
export async function folders() {
  if (DEMO) return (await demo()).api.folders();
  return req("GET", "/api/folders");
}
export async function createFolder(name, parentId) {
  if (DEMO) return (await demo()).api.createFolder(name, parentId);
  return req("POST", "/api/folders", { name, parent_id: parentId ?? null });
}
export async function addImagesToFolder(folderId, imageIds) {
  if (DEMO) return (await demo()).api.addImagesToFolder(folderId, imageIds);
  return req("POST", `/api/folders/${folderId}/images`, { image_ids: imageIds });
}
export async function removeImageFromFolder(folderId, imageId) {
  if (DEMO) return (await demo()).api.removeImageFromFolder(folderId, imageId);
  return req("DELETE", `/api/folders/${folderId}/images/${imageId}`);
}
/**
 * Bulk-remove images from a folder (symmetric to addImagesToFolder). Hits the
 * DELETE /api/folders/{id}/images endpoint with a JSON body of {image_ids}.
 * 404 if the folder is missing; ids without a membership row are no-ops.
 * Resolves to {removed:int}.
 */
export async function removeImagesFromFolder(folderId, imageIds) {
  if (DEMO) {
    const d = await demo();
    if (d.api.removeImagesFromFolder) return d.api.removeImagesFromFolder(folderId, imageIds);
  }
  return req("DELETE", `/api/folders/${folderId}/images`, { image_ids: imageIds });
}

// --------------------------------------------------------------- senders -- //
export async function senders(accountId) {
  if (DEMO) return (await demo()).api.senders(accountId);
  return req("GET", `/api/senders${qs({ account: accountId })}`);
}
export async function createSender(payload) {
  if (DEMO) return (await demo()).api.createSender(payload);
  return req("POST", "/api/senders", payload);
}
export async function updateSender(id, patch) {
  if (DEMO) return (await demo()).api.updateSender(id, patch);
  return req("PATCH", `/api/senders/${id}`, patch);
}

// --------------------------------------------------------------- vendors -- //
export async function vendors() {
  if (DEMO) return (await demo()).api.vendors();
  return req("GET", "/api/vendors");
}
export async function createVendor(payload) {
  if (DEMO) {
    const d = await demo();
    if (d.api.createVendor) return d.api.createVendor(payload);
  }
  return req("POST", "/api/vendors", payload);
}
export async function updateVendor(id, patch) {
  if (DEMO) {
    const d = await demo();
    if (d.api.updateVendor) return d.api.updateVendor(id, patch);
  }
  return req("PATCH", `/api/vendors/${id}`, patch);
}
export async function deleteVendor(id) {
  if (DEMO) {
    const d = await demo();
    if (d.api.deleteVendor) return d.api.deleteVendor(id);
  }
  return req("DELETE", `/api/vendors/${id}`);
}

// ------------------------------------------------------- collection rules -- //
// Auto-filing rules: a target collection (folder_id) + a list of ANDed
// conditions {field, op, value}. Matching images are filed into the folder on
// every sync; /apply files already-matching images on demand. The server
// validates conditions (422 on bad field/op/value). In DEMO mode (no backend)
// the GET resolves to an empty list and mutations resolve to benign stubs so
// the screen still renders.
export async function collectionRules() {
  if (DEMO) {
    const d = await demo();
    if (d.api.collectionRules) return d.api.collectionRules();
    return [];
  }
  return req("GET", "/api/collection-rules");
}
export async function createCollectionRule(payload) {
  if (DEMO) {
    const d = await demo();
    if (d.api.createCollectionRule) return d.api.createCollectionRule(payload);
    return { id: Date.now(), match_count: 0, ...payload };
  }
  return req("POST", "/api/collection-rules", payload);
}
export async function updateCollectionRule(id, patch) {
  if (DEMO) {
    const d = await demo();
    if (d.api.updateCollectionRule) return d.api.updateCollectionRule(id, patch);
    return { id, ...patch };
  }
  return req("PATCH", `/api/collection-rules/${id}`, patch);
}
export async function deleteCollectionRule(id) {
  if (DEMO) {
    const d = await demo();
    if (d.api.deleteCollectionRule) return d.api.deleteCollectionRule(id);
    return { status: "ok" };
  }
  return req("DELETE", `/api/collection-rules/${id}`);
}
export async function applyCollectionRules() {
  if (DEMO) {
    const d = await demo();
    if (d.api.applyCollectionRules) return d.api.applyCollectionRules();
    return { applied: {}, total_added: 0 };
  }
  return req("POST", "/api/collection-rules/apply", {});
}

// -------------------------------------------------------------- accounts -- //
export async function accounts() {
  if (DEMO) return (await demo()).api.accounts();
  return req("GET", "/api/accounts");
}

// ----------------------------------------------------------------- stats -- //
export async function stats() {
  if (DEMO) return (await demo()).api.stats();
  return req("GET", "/api/stats");
}

// -------------------------------------------------------------- download -- //
// POST /api/download returns a single file or a streamed zip. The contract is
// JSON-body POST (so a plain anchor/form navigation can't express it); we fetch
// the response and hand the browser a blob download. The server streams the zip
// from a temp file, so server memory stays flat; the client holds one blob.
export async function downloadImages(imageIds) {
  if (DEMO) return (await demo()).api.downloadImages(imageIds);
  const res = await req("POST", "/api/download", { image_ids: imageIds }, { raw: true });
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json()).detail; } catch (_) { /* ignore */ }
    throw new ApiError(res.status, detail);
  }
  const blob = await res.blob();
  const name = filenameFromDisposition(res.headers.get("content-disposition"))
    || (imageIds.length > 1 ? "folio_images.zip" : "folio-image");
  triggerBlobDownload(blob, name);
  return { ok: true, count: imageIds.length };
}

function filenameFromDisposition(header) {
  if (!header) return null;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) { try { return decodeURIComponent(star[1].trim().replace(/"/g, "")); } catch (_) {} }
  const m = /filename="?([^";]+)"?/i.exec(header);
  return m ? m[1].trim() : null;
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export const isDemo = DEMO;
