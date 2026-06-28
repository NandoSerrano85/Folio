// util.js — dependency-free DOM, formatting, and color helpers.
//
// SECURITY: all rendering goes through createElement + textContent (never
// innerHTML with API data). The `el()` helper sets text via textContent and
// builds children as real nodes, so user-controlled strings (filenames, email
// subjects/senders, drive paths, vendor/folder names) can never inject markup.

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Create an element.
 * @param {string} tag
 * @param {object} [attrs] - className, dataset, style(object), text, html-free.
 *                           Event handlers as on* (onClick -> click).
 * @param {(Node|string|null|undefined|Array)} [children]
 */
export function el(tag, attrs = {}, children = null) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  appendChildren(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  for (const key in attrs) {
    const val = attrs[key];
    if (val == null || val === false) continue;
    if (key === "text") {
      node.textContent = String(val);
    } else if (key === "class" || key === "className") {
      node.className = val;
    } else if (key === "style" && typeof val === "object") {
      Object.assign(node.style, val);
    } else if (key === "dataset" && typeof val === "object") {
      Object.assign(node.dataset, val);
    } else if (key.startsWith("on") && typeof val === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), val);
    } else if (key === "value") {
      node.value = val;
    } else if (key === "checked" || key === "disabled" || key === "selected") {
      node[key] = !!val;
    } else {
      node.setAttribute(key, String(val));
    }
  }
}

function appendChildren(node, children) {
  if (children == null) return;
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
}

/** Remove all children of a node. */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Build an inline SVG from a compact spec. */
export function svg(paths, { size = 16, stroke = 1.6, fill = "none" } = {}) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("viewBox", `0 0 16 16`);
  s.setAttribute("fill", fill);
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", stroke);
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  for (const p of paths) {
    const e = document.createElementNS(SVG_NS, p.t);
    for (const k in p) {
      if (k === "t") continue;
      e.setAttribute(k, p[k]);
    }
    s.appendChild(e);
  }
  return s;
}

/** A small check-mark svg sized for a 12x12 viewBox. */
export function checkSvg(size = 11, w = 2.2) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("viewBox", "0 0 12 12");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", w);
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS(SVG_NS, "polyline");
  p.setAttribute("points", "2.5,6.4 5,8.8 9.5,3.4");
  s.appendChild(p);
  return s;
}

// Named icons used across the UI (all 16x16 viewBox unless noted).
export const icons = {
  search: () => svg([{ t: "circle", cx: 7, cy: 7, r: 4.3 }, { t: "line", x1: 10.6, y1: 10.6, x2: 14, y2: 14 }]),
  x: (size = 14) => svg([{ t: "line", x1: 3.5, y1: 3.5, x2: 12.5, y2: 12.5 }, { t: "line", x1: 12.5, y1: 3.5, x2: 3.5, y2: 12.5 }], { size, stroke: 1.7 }),
  chevronDown: (size = 12) => svg([{ t: "polyline", points: "4,6 8,10 12,6" }], { size, stroke: 1.7 }),
  download: (size = 14) => svg([
    { t: "line", x1: 8, y1: 2.5, x2: 8, y2: 10.5 },
    { t: "polyline", points: "4.5,7 8,10.6 11.5,7" },
    { t: "line", x1: 3, y1: 13.2, x2: 13, y2: 13.2 },
  ], { size, stroke: 1.7 }),
  arrowLeft: (size = 17) => svg([{ t: "polyline", points: "10,3 5,8 10,13" }], { size, stroke: 1.8 }),
  arrowRight: (size = 17) => svg([{ t: "polyline", points: "6,3 11,8 6,13" }], { size, stroke: 1.8 }),
};

// ----------------------------------------------------------------- dates -- //
const MON_S = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MON_L = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function toDate(value) {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return isNaN(d.getTime()) ? null : d;
}

/** "27 Jun 2026" — uses UTC to keep source dates stable across timezones. */
export function fmtMedium(value) {
  const d = toDate(value);
  if (!d) return "";
  return `${d.getUTCDate()} ${MON_S[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** "27 June 2026" */
export function fmtLong(value) {
  const d = toDate(value);
  if (!d) return "";
  return `${d.getUTCDate()} ${MON_L[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** Human bytes, e.g. "1.4 MB". */
export function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

// --------------------------------------------------------- vendor colors -- //
// The API has no per-vendor color, so we derive a stable, muted, warm-leaning
// hue from the vendor name. Deterministic: same name -> same dot everywhere.
const _vendorColorCache = new Map();
export function vendorColor(name) {
  const key = name || "";
  if (_vendorColorCache.has(key)) return _vendorColorCache.get(key);
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const hue = (h >>> 0) % 360;
  // Muted saturation/lightness in the same family as the mockup palette.
  const sat = 34 + ((h >>> 8) % 10); // 34–43%
  const light = 52 + ((h >>> 16) % 12); // 52–63%
  const color = `hsl(${hue} ${sat}% ${light}%)`;
  _vendorColorCache.set(key, color);
  return color;
}

/** Two-tone gradient placeholder derived from a stable id/seed. */
export function gradientFor(seed) {
  const s = String(seed);
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = (h * 33) ^ s.charCodeAt(i);
  const h1 = (h >>> 0) % 360;
  const h2 = (h1 + 25 + ((h >>> 9) % 70)) % 360;
  return `linear-gradient(135deg,hsl(${h1} 42% 60%),hsl(${h2} 38% 44%))`;
}

/** First non-space character, uppercased, for avatars. */
export function initial(str) {
  const s = (str || "").trim();
  return s ? s[0].toUpperCase() : "?";
}

/** Trailing-edge debounce. */
export function debounce(fn, ms = 280) {
  let t = null;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

/**
 * Whether `url` is a safe external link to hand to window.open — only http(s)
 * is allowed, so an API-provided `javascript:`/`data:` value can never become a
 * navigation/script target. Returns false on anything unparseable or off-scheme.
 */
export function isHttpUrl(url) {
  if (!url) return false;
  try {
    const u = new URL(url, window.location.href);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch (_) {
    return false;
  }
}
