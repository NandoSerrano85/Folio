// folders.js — folder-tree flattening and per-image membership.
//
// The API exposes a folder tree (with counts) and membership-mutation endpoints,
// but no "which folders contain image X" endpoint. We detect membership by
// querying each non-empty folder narrowed by the image's filename (`q`), which
// keeps the target on the first page in practice, then scanning by id. Results
// are cached per image and invalidated on add/remove.

import * as api from "./api.js";

/** Depth-first flatten of the folder tree into rows with a `depth` field. */
export function flattenFolders(tree) {
  const out = [];
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      out.push({
        id: n.id,
        name: n.name,
        parent_id: n.parent_id ?? null,
        image_count: n.image_count ?? 0,
        depth,
      });
      if (n.children && n.children.length) walk(n.children, depth + 1);
    }
  };
  walk(tree || [], 0);
  return out;
}

// imageId -> Set(folderId)
const _membershipCache = new Map();

/**
 * Determine which folders contain the given image.
 * @param {object} image - needs { id, filename }
 * @param {Array} foldersFlat - flattened folder rows
 * @returns {Promise<Set<number>>}
 */
export async function getImageFolders(image, foldersFlat) {
  if (_membershipCache.has(image.id)) return _membershipCache.get(image.id);

  const candidates = (foldersFlat || []).filter((f) => f.image_count > 0);
  const filename = image.filename || "";

  const checks = candidates.map(async (f) => {
    try {
      const params = filename
        ? { folder: f.id, q: filename, page_size: 25, sort: "newest" }
        : { folder: f.id, page_size: 200, sort: "newest" };
      const res = await api.listImages(params);
      const hit = (res.items || []).some((it) => it.id === image.id);
      return hit ? f.id : null;
    } catch (_) {
      return null;
    }
  });

  const ids = (await Promise.all(checks)).filter((x) => x != null);
  const set = new Set(ids);
  _membershipCache.set(image.id, set);
  return set;
}

/** Toggle membership and update the cache; returns the new Set for the image. */
export async function toggleMembership(image, folderId, currentlyMember) {
  if (currentlyMember) {
    await api.removeImageFromFolder(folderId, image.id);
  } else {
    await api.addImagesToFolder(folderId, [image.id]);
  }
  const set = _membershipCache.get(image.id) || new Set();
  if (currentlyMember) set.delete(folderId);
  else set.add(folderId);
  _membershipCache.set(image.id, set);
  return set;
}

/** Drop cached membership (e.g. after bulk changes or folder refresh). */
export function invalidateMembership(imageId) {
  if (imageId == null) _membershipCache.clear();
  else _membershipCache.delete(imageId);
}
