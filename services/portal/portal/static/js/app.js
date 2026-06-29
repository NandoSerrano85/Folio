// app.js — application entry point. Owns the auth gate, the login screen, the
// persistent header (brand, tabs, search, Light/Dark, user menu), theme
// persistence, the toast, and top-level routing between Library and Senders.

import { el, clear, icons, initial, debounce } from "./util.js";
import { state, setState, subscribe, toast } from "./state.js";
import * as api from "./api.js";
import { mountLibrary, loadReference } from "./gallery.js";
import { mountSenders } from "./senders.js";
import { mountRules } from "./rules.js";
import "./lightbox.js"; // self-registers its store subscription + key handlers
import "./edit.js"; // self-registers the Edit modal (single + bulk)

const THEME_KEY = "folio.theme";

let rootEl = null;
const refs = {}; // persistent DOM references (header, content, etc.)

// ------------------------------------------------------------------- boot -- //
function boot() {
  rootEl = document.getElementById("root") || document.body;
  applyTheme(loadTheme());

  subscribe(onStateChange);

  // Decide initial screen from the session.
  api.me()
    .then((res) => { setState({ username: res.username }); })
    .catch(() => { setState({ username: null }); })
    .finally(renderScreen);
}

function onStateChange(s, keys) {
  if (keys.has("username")) renderScreen();
  if (keys.has("theme")) syncThemeUI();
  if (keys.has("toast")) showToast(s.toast);
  if (keys.has("tab")) syncTab();
  if (keys.has("userMenuOpen")) syncUserMenu();
  if (keys.has("search")) syncSearch();
}

// ------------------------------------------------------------------ theme -- //
function loadTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (t === "dark" || t === "light") return t;
  } catch (_) { /* ignore */ }
  return "light"; // mockup default
}

function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.style.colorScheme = theme === "dark" ? "dark" : "light";
}

function setTheme(theme) {
  applyTheme(theme);
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* ignore */ }
  setState({ theme });
}

function syncThemeUI() {
  if (refs.segLight) refs.segLight.classList.toggle("active", state.theme === "light");
  if (refs.segDark) refs.segDark.classList.toggle("active", state.theme === "dark");
}

// ----------------------------------------------------------------- screen -- //
function renderScreen() {
  clear(rootEl);
  refs.contentEl = null;
  if (state.username) {
    rootEl.appendChild(buildApp());
    loadReference();
  } else {
    rootEl.appendChild(buildLogin());
  }
}

// ============================================================ LOGIN screen == //
function buildLogin() {
  let tab = "token"; // 'token' | 'password'
  let tokenVisible = false;
  let pwdVisible = false;

  const wrap = el("div", { class: "login-wrap" });
  const formPane = el("div", { class: "login-pane" });
  const formCol = el("div", { class: "login-form" });
  formPane.appendChild(formCol);

  const cover = el("div", { class: "login-cover" }, [
    el("div", { class: "cover-overlay" }),
    el("div", { class: "cover-content" }, [
      el("div", { class: "cover-brand" }, [
        el("div", { class: "cover-mark", text: "F" }),
        el("span", { class: "cover-word", text: "Folio" }),
      ]),
      el("div", {}, [
        el("div", { class: "cover-headline", text: "Every image your team was ever sent, in one quiet place." }),
        el("p", { class: "cover-sub", text: "Folio gathers pictures from your mailboxes and drives, keeps each one's true source date, and makes the whole archive searchable." }),
      ]),
      el("div", { class: "cover-eyebrow", text: "Self-hosted · running on NAS" }),
    ]),
  ]);

  const card = el("div", { class: "login-card" }, [cover, formPane]);
  wrap.appendChild(card);

  const errorEl = el("div", { class: "login-error" });

  function field(labelText, inputAttrs, withToggle, getVisible, setVisible) {
    const fwrap = el("div", { class: "field-wrap" });
    const input = el("input", inputAttrs);
    if (withToggle) input.classList.add("has-toggle");
    fwrap.appendChild(input);
    if (withToggle) {
      const btn = el("button", {
        type: "button", class: "show-toggle", text: getVisible() ? "Hide" : "Show",
        onClick: () => {
          const v = !getVisible();
          setVisible(v);
          input.type = v ? "text" : "password";
          btn.textContent = v ? "Hide" : "Show";
        },
      });
      fwrap.appendChild(btn);
    }
    return { fwrap, input, labelText };
  }

  // Build inputs once so values persist across tab switches.
  const tokenInput = el("input", {
    type: "password", class: "auth-input mono has-toggle", autocomplete: "off",
    spellcheck: "false", placeholder: "folio_sk_live_…",
    onKeyDown: (e) => { if (e.key === "Enter") submit(); },
  });
  const tokenToggle = el("button", {
    type: "button", class: "show-toggle", text: "Show",
    onClick: () => {
      tokenVisible = !tokenVisible;
      tokenInput.type = tokenVisible ? "text" : "password";
      tokenToggle.textContent = tokenVisible ? "Hide" : "Show";
    },
  });

  const userInput = el("input", {
    type: "text", class: "auth-input", autocomplete: "username",
    placeholder: "Username",
    onKeyDown: (e) => { if (e.key === "Enter") submit(); },
  });
  const pwdInput = el("input", {
    type: "password", class: "auth-input has-toggle", autocomplete: "current-password",
    placeholder: "Password",
    onKeyDown: (e) => { if (e.key === "Enter") submit(); },
  });
  const pwdToggle = el("button", {
    type: "button", class: "show-toggle", text: "Show",
    onClick: () => {
      pwdVisible = !pwdVisible;
      pwdInput.type = pwdVisible ? "text" : "password";
      pwdToggle.textContent = pwdVisible ? "Hide" : "Show";
    },
  });

  const fieldsArea = el("div");

  function renderFields() {
    clear(fieldsArea);
    if (tab === "token") {
      fieldsArea.appendChild(el("label", { class: "field-label", text: "Access token" }));
      fieldsArea.appendChild(el("div", { class: "field-wrap" }, [tokenInput, tokenToggle]));
      fieldsArea.appendChild(el("p", { class: "field-help", text: "Paste the token issued by your Folio admin. It's stored on this device only." }));
      setTimeout(() => tokenInput.focus(), 0);
    } else {
      fieldsArea.appendChild(el("label", { class: "field-label", text: "Username" }));
      fieldsArea.appendChild(el("div", { class: "field-wrap" }, [userInput]));
      const pl = el("label", { class: "field-label", style: { marginTop: "14px" }, text: "Password" });
      fieldsArea.appendChild(pl);
      fieldsArea.appendChild(el("div", { class: "field-wrap" }, [pwdInput, pwdToggle]));
      setTimeout(() => userInput.focus(), 0);
    }
  }

  const tabBtns = {};
  function setTab(t) {
    tab = t;
    errorEl.textContent = "";
    tabBtns.token.classList.toggle("active", t === "token");
    tabBtns.password.classList.toggle("active", t === "password");
    renderFields();
  }
  tabBtns.token = el("button", { type: "button", class: "login-tab active", text: "Token", onClick: () => setTab("token") });
  tabBtns.password = el("button", { type: "button", class: "login-tab", text: "Password", onClick: () => setTab("password") });

  const unlockBtn = el("button", { type: "button", class: "btn-primary btn-unlock", text: "Unlock library", onClick: () => submit() });

  async function submit() {
    errorEl.textContent = "";
    let payload;
    if (tab === "token") {
      const token = tokenInput.value.trim();
      if (!token) { errorEl.textContent = "Enter your access token."; return; }
      payload = { token };
    } else {
      const username = userInput.value.trim();
      const password = pwdInput.value;
      if (!username || !password) { errorEl.textContent = "Enter your username and password."; return; }
      payload = { username, password };
    }
    unlockBtn.disabled = true;
    unlockBtn.textContent = "Unlocking…";
    try {
      const res = await api.login(payload);
      setState({ username: res.username });
    } catch (e) {
      errorEl.textContent = "That didn't work. Check your details and try again.";
      unlockBtn.disabled = false;
      unlockBtn.textContent = "Unlock library";
    }
  }

  // Assemble the form column.
  formCol.appendChild(el("h1", { class: "login-title", text: "Sign in" }));
  formCol.appendChild(el("p", { class: "login-subtitle", text: "Open your library with a token or your account." }));
  formCol.appendChild(el("div", { class: "login-tabs" }, [tabBtns.token, tabBtns.password]));
  formCol.appendChild(fieldsArea);
  formCol.appendChild(el("div", { class: "remember-row" }, [
    el("label", { class: "remember" }, [
      el("input", { type: "checkbox", checked: true }),
      document.createTextNode("Remember this device"),
    ]),
    el("button", { type: "button", class: "link-btn", text: "Where do I find this?", onClick: () => toast("Generate a token in Folio → Settings → Access tokens.") }),
  ]));
  formCol.appendChild(errorEl);
  formCol.appendChild(unlockBtn);
  formCol.appendChild(el("p", { class: "login-eyebrow", text: "Protected area · token auth" }));

  renderFields();
  return wrap;
}

// ============================================================== APP shell == //
function buildApp() {
  const header = buildHeader();
  refs.contentEl = el("div", { class: "content" });
  const shell = el("div", { class: "app" }, [header, refs.contentEl]);
  syncTab();
  return shell;
}

function buildHeader() {
  // Brand + tabs
  const tabLibrary = el("button", { class: "tab", text: "Library", onClick: () => setState({ tab: "library", userMenuOpen: false }) });
  const tabSenders = el("button", { class: "tab", text: "Senders", onClick: () => setState({ tab: "senders", userMenuOpen: false }) });
  const tabRules = el("button", { class: "tab", text: "Rules", onClick: () => setState({ tab: "rules", userMenuOpen: false }) });
  refs.tabLibrary = tabLibrary;
  refs.tabSenders = tabSenders;
  refs.tabRules = tabRules;

  const brandGroup = el("div", { class: "brand-group" }, [
    el("div", { class: "brand" }, [
      el("div", { class: "brand-mark", text: "F" }),
      el("span", { class: "brand-word", text: "Folio" }),
    ]),
    el("nav", { class: "tabs" }, [tabLibrary, tabSenders, tabRules]),
  ]);

  // Search
  const searchInput = el("input", {
    class: "search-input", placeholder: "Search filenames, vendors, senders…",
    onInput: debounce((e) => setState({ search: e.target.value, page: 1 }), 280),
  });
  refs.searchInput = searchInput;
  const clearBtn = el("button", { class: "search-clear", hidden: true, onClick: clearSearch });
  clearBtn.appendChild(icons.x(14));
  refs.searchClear = clearBtn;
  const searchIcon = el("span", { class: "search-icon" }); searchIcon.appendChild(icons.search());
  const searchArea = el("div", { class: "search-area" }, [
    el("div", { class: "search-box" }, [searchIcon, searchInput, clearBtn]),
  ]);

  // Light/Dark segmented
  refs.segLight = el("button", { class: `seg-btn${state.theme === "light" ? " active" : ""}`, text: "Light", onClick: () => setTheme("light") });
  refs.segDark = el("button", { class: `seg-btn${state.theme === "dark" ? " active" : ""}`, text: "Dark", onClick: () => setTheme("dark") });
  const seg = el("div", { class: "seg" }, [refs.segLight, refs.segDark]);

  // User menu
  const userBtn = el("button", { class: "user-btn", onClick: () => setState({ userMenuOpen: !state.userMenuOpen }) }, [
    el("div", { class: "avatar", text: initial(state.username) }),
    el("span", { class: "user-name", text: state.username || "" }),
  ]);
  const chev = el("span", { class: "user-chevron" }); chev.appendChild(icons.chevronDown(12));
  userBtn.appendChild(chev);
  refs.userMenuWrap = el("div", { class: "user-menu" }, [userBtn]);

  const headerRight = el("div", { class: "header-right" }, [
    seg, el("div", { class: "divider" }), refs.userMenuWrap,
  ]);

  return el("header", { class: "topbar" }, [brandGroup, searchArea, headerRight]);
}

function clearSearch() {
  if (refs.searchInput) refs.searchInput.value = "";
  setState({ search: "", page: 1 });
}

function syncSearch() {
  if (refs.searchInput && refs.searchInput.value !== state.search) {
    refs.searchInput.value = state.search;
  }
  if (refs.searchClear) refs.searchClear.hidden = !state.search;
}

function syncTab() {
  if (!refs.contentEl) return;
  // Tab underline
  if (refs.tabLibrary) {
    refs.tabLibrary.classList.toggle("active", state.tab === "library");
    refs.tabSenders.classList.toggle("active", state.tab === "senders");
    refs.tabRules.classList.toggle("active", state.tab === "rules");
    refreshUnderline(refs.tabLibrary, state.tab === "library");
    refreshUnderline(refs.tabSenders, state.tab === "senders");
    refreshUnderline(refs.tabRules, state.tab === "rules");
  }
  // Swap content
  clear(refs.contentEl);
  let view;
  if (state.tab === "senders") view = mountSenders();
  else if (state.tab === "rules") view = mountRules();
  else view = mountLibrary();
  refs.contentEl.appendChild(view);
}

function refreshUnderline(btn, on) {
  const existing = btn.querySelector(".tab-underline");
  if (on && !existing) btn.appendChild(el("span", { class: "tab-underline" }));
  else if (!on && existing) existing.remove();
}

function syncUserMenu() {
  const wrap = refs.userMenuWrap;
  if (!wrap) return;
  const existing = wrap.querySelector(".menu");
  if (state.userMenuOpen && !existing) {
    const menu = el("div", { class: "menu" }, [
      el("div", { class: "menu-head" }, [
        el("div", { class: "menu-name", text: state.username || "" }),
        el("div", { class: "menu-sub", text: "Admin · all sources" }),
      ]),
      el("div", { class: "menu-sep" }),
      el("button", { class: "menu-item", text: "Senders & sources", onClick: () => setState({ tab: "senders", userMenuOpen: false }) }),
      el("button", { class: "menu-item", text: "Log out", onClick: doLogout }),
    ]);
    wrap.appendChild(menu);
  } else if (!state.userMenuOpen && existing) {
    existing.remove();
  }
}

// Close the user menu on outside click.
document.addEventListener("click", (e) => {
  if (!state.userMenuOpen) return;
  if (refs.userMenuWrap && !refs.userMenuWrap.contains(e.target)) {
    setState({ userMenuOpen: false });
  }
});

async function doLogout() {
  try { await api.logout(); } catch (_) { /* ignore */ }
  setState({ username: null, userMenuOpen: false, tab: "library", selected: new Set(), lightbox: null });
}

// ------------------------------------------------------------------ toast -- //
let toastEl = null;
let toastTimer = null;
function showToast(t) {
  if (!t) return;
  if (toastEl) toastEl.remove();
  toastEl = el("div", { class: "toast", text: t.msg });
  document.body.appendChild(toastEl);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (toastEl) { toastEl.remove(); toastEl = null; }
  }, 2400);
}

// ------------------------------------------------------------------- start -- //
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
