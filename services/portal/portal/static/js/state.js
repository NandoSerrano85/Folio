// state.js — a tiny reactive store plus the library filter/query model.
//
// Modules call setState(patch); subscribers receive (state, changedKeys) and
// update only what they own. The image query is derived here so the gallery and
// the lightbox build identical /api/images requests.

const PAGE_SIZES = [25, 50, 100, 200];
const SORTS = ["newest", "oldest", "name", "vendor", "account"];

export const state = {
  // session / chrome
  username: null, // null => show login
  theme: "light",
  tab: "library", // 'library' | 'senders'
  userMenuOpen: false,
  toast: null,

  // library filters (account & vendor are single-id to match the API params)
  search: "",
  folder: null, // active folder id; null = All Images
  account: null, // single account id filter; null = all
  vendor: null, // single vendor id filter; null = all
  dateFrom: "",
  dateTo: "",
  datePreset: "all", // all | last90 | 2025 | 2024 | custom
  sort: "newest",
  pageSize: 25,
  page: 1,

  // library results
  items: [],
  total: 0,
  pages: 0,
  loading: true,

  // selection (image ids)
  selected: new Set(),

  // reference data
  foldersFlat: [], // [{id,name,parent_id,image_count,depth}]
  accountsList: [], // AccountOut[]
  vendorsList: [], // VendorOut[]
  vendorCountByName: {}, // {name: count} from /api/stats
  statsTotal: 0,

  // lightbox: null or { index } where index is a GLOBAL index into the
  // current filtered result set (0..total-1)
  lightbox: null,
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function setState(patch) {
  const keys = new Set(Object.keys(patch));
  Object.assign(state, patch);
  for (const fn of Array.from(listeners)) fn(state, keys);
}

/** Queue a bottom-center toast message (app.js renders + auto-dismisses). */
export function toast(msg) {
  setState({ toast: { msg: String(msg), id: Date.now() } });
}

// --------------------------------------------------------- query helpers -- //
/** Build the /api/images query params from current filter state. */
export function imageQuery(overrides = {}) {
  const s = { ...buildBase(), ...overrides };
  return s;
}

function buildBase() {
  const p = {
    page: state.page,
    page_size: state.pageSize,
    sort: state.sort,
  };
  if (state.search.trim()) p.q = state.search.trim();
  if (state.folder != null) p.folder = state.folder;
  if (state.account != null) p.account = state.account;
  if (state.vendor != null) p.vendor = state.vendor;
  if (state.dateFrom) p.date_from = state.dateFrom;
  if (state.dateTo) p.date_to = state.dateTo;
  return p;
}

export function filtersActive() {
  return (
    state.folder != null ||
    state.account != null ||
    state.vendor != null ||
    !!state.dateFrom ||
    !!state.dateTo ||
    !!state.search.trim()
  );
}

export function resetFilters() {
  setState({
    folder: null,
    account: null,
    vendor: null,
    dateFrom: "",
    dateTo: "",
    datePreset: "all",
    search: "",
    page: 1,
  });
}

export function isValidPageSize(n) {
  return PAGE_SIZES.includes(n);
}
export function isValidSort(s) {
  return SORTS.includes(s);
}

export { PAGE_SIZES, SORTS };
