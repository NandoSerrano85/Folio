# Folio Portal — Design Specification

Source of truth: the approved Claude design **"Image Library Portal Mockup"**
(`claude.ai/design/p/1aa4c2c1-e1de-4751-a6e5-0510c363bf64`), imported via the
claude_design MCP. The raw design-canvas file is checked in at
`design/folio-mockup.dc.html`. This document is the human-readable distillation
the production frontend (`services/portal/portal/templates` + `static`) must match
**pixel-for-pixel in layout, type, color, spacing, and interaction**.

The mockup is a self-contained reactive prototype (`<x-dc>` runtime). The
production app reproduces the same DOM/CSS but wires the `{{ }}` bindings to the
real FastAPI JSON API instead of the in-prototype mock data.

---

## 1. Foundations

### Fonts (Google Fonts)
- **Hanken Grotesk** (400/500/600/700) — UI / body. Base size 14px, line-height 1.5.
- **Newsreader** (400/500, incl. italic, optical 6..72) — display headings, dates (italic), brand wordmark.
- **IBM Plex Mono** (400/500) — labels, counts, metadata keys, filenames, eyebrow text, range/pos labels, toasts.

### Theme tokens (CSS custom properties, set on the root element by JS; persisted to `localStorage['folio.theme']`)
Light (default):
```
--bg:#f7f5f2  --surface:#ffffff  --surface-2:#f1eee9
--ink:#1b1916 --ink-soft:#514c45 --muted:#8a847a
--line:#e8e4dd --line-strong:#d9d3ca --field:#ffffff
--shadow:0 1px 2px rgba(40,34,26,.05), 0 10px 26px -14px rgba(40,34,26,.2)
--overlay:rgba(28,24,20,.5)
```
Dark:
```
--bg:#15130f  --surface:#1d1a16  --surface-2:#25211b
--ink:#f2ede4 --ink-soft:#b8b0a4 --muted:#837c6f
--line:#2c2822 --line-strong:#3a352d --field:#211e19
--shadow:0 1px 2px rgba(0,0,0,.4), 0 12px 30px -14px rgba(0,0,0,.7)
--overlay:rgba(8,6,4,.74)
```
Also set `document.body.style.background = --bg` and `documentElement.style.colorScheme`.

### Accent system (`--accent` / `--accent-contrast`); default **Graphite**
| name | light | dark | contrast(light) | contrast(dark) |
|---|---|---|---|---|
| Graphite | `#1b1916` | `#f2ede4` | `#ffffff` | `#1b1916` |
| Blue | `#2f6fed` | `#6f9bff` | `#ffffff` | `#0e1320` |
| Emerald | `#0f9d77` | `#34c79b` | `#ffffff` | `#06120e` |
| Violet | `#7c5cff` | `#9d86ff` | `#ffffff` | `#120e22` |
| Amber | `#bd6f1c` | `#e7a23f` | `#ffffff` | `#1c1305` |

Production: ship Graphite as default; expose Light/Dark toggle in the header (the
accent + density selectors are prototype "props" — keep Graphite/Comfortable, but
structure CSS so accent is a single token swap).

### Density (prototype prop "density": Comfortable | Compact)
Grid min column / gap: Comfortable `minmax(248px,1fr)` gap `16px`; Compact `minmax(188px,1fr)` gap `12px`. Ship Comfortable.

### Global details
- Rounded corners everywhere (cards 13px, panels 14–18px, buttons 9–11px, inputs 8–10px, pills 999px).
- Custom scrollbar: 11px, thumb `rgba(120,110,95,.30)` with 3px transparent border, radius 9px.
- `::selection` `rgba(0,0,0,.10)`.
- Keyframes: `folioIn` (fade+6px rise, .12s), `toastIn` (fade+rise from bottom, .2s), `lbIn` (fade .15s).
- Hover affordances use a `style-hover` pattern → in production use real CSS `:hover` (e.g. buttons `filter:brightness(.94)`, ghost rows `background:var(--surface-2)`).

---

## 2. Screens

### A. Login (`showLogin`)
Centered card, `max-width:940px`, `min-height:558px`, two panes, radius 18px, `box-shadow:var(--shadow)`.
- **Left pane** (flex 1.05): dark `#171511` cover with a cover photo at `opacity:.4` + dark gradient overlay; padded 36px; top = brand lockup (30px rounded `#f2ede4` "F" tile + "Folio" in Newsreader 21px); middle = Newsreader 35px/500 headline _"Every image your team was ever sent, in one quiet place."_ + 13.5px `#cdc5b8` subcopy; bottom = mono eyebrow `Self-hosted · 6 sources · running on NAS`.
- **Right pane** (flex 1): centered form, `max-width:336px`. Newsreader 28px "Sign in"; subcopy. **Access-token field** (label "Access token", mono input, placeholder `folio_sk_live_…`, 44px tall, with an in-field **Show/Hide** toggle button). Helper text "Paste the token issued by your Folio admin. It's stored on this device only." Row: "Remember this device" checkbox (accent) + "Where do I find this?" link. Primary full-width button **"Unlock library"** (`--accent`). Footer mono eyebrow `Protected area · token auth`. Enter submits.

> **Auth model note:** the design uses **token-based** sign-in, not username/password.
> Production reconciliation: the token is verified server-side against an **Argon2id**
> hash (admin issues the token; server stores only the hash). This honors both the
> mockup UX and the plan's "Argon2 hash" requirement. See `services/portal` auth.

### B. App shell header (`showApp`) — sticky, 60px, `--surface`, bottom border `--line`
- Left: brand lockup (28px accent "F" tile + "Folio" Newsreader 19px) then nav tabs **Library** / **Senders** (active tab gets a 2px accent underline at the bottom edge).
- Center: search field, `max-width:520px`, 38px, `--field`, magnifier icon, placeholder _"Search filenames, vendors, senders…"_, clear (×) button when non-empty. Typing resets to page 1.
- Right: **Light/Dark** segmented toggle (active segment = `--surface` + subtle shadow; inactive = transparent `--muted`); divider; **user menu** button (28px round avatar initial + name + chevron). Menu (folioIn) shows name + "Admin · all sources", "Senders & sources", "Log out".

### C. Library (`isLibrary`) — sidebar + main
**Left sidebar** (`aside`), sticky under header, width 256px, right border, scrolls independently:
- **Collections** (mono eyebrow): list of folders; each row = name + mono count, right-aligned. Active row: `--surface-2` fill + 3px accent left bar (radius `0 3px 3px 0`).
- **Source account** (eyebrow): subgroups **Gmail** then **Google Drive** (bold 11px subheads). Each account row = custom checkbox (16px, accent fill + check when on) + name (ellipsized) + mono count. Toggling filters + resets page.
- **Vendor** (eyebrow): rows = checkbox + **vendor color dot** (8px) + name + mono count.
- **Date range** (eyebrow): preset chips **All time / Last 90 days / 2025 / 2024** (active = accent fill); then **From** / **To** native date inputs (34px). Editing a date sets preset = "custom".
- **Reset all filters** link appears when any filter is active.

**Main** (`main`, padding 24/28/64):
- Newsreader 27px collection **title** + `--ink-soft` description + mono eyebrow `N images · filtered`.
- **Toolbar** (top+bottom hairline borders): left = "Select all on page" checkbox; when selection exists → `N selected` + primary **"Download selected (zip)"** (download icon) + "Clear" link. Right = **Sort** select (Newest first / Oldest first / Name A–Z / Vendor / Source account) + **Show** select (25 / 50 / 100 / 200). Both are custom-styled selects with a chevron.
- **Grid**: `repeat(auto-fill, minmax(248px,1fr))`, gap 16px. Each **tile**: `aspect-ratio:4/3`, radius 13px, `box-shadow:var(--shadow)`, `cursor:zoom-in`. Layers: gradient placeholder bg (per-image `linear-gradient(135deg,…)`) → lazy `<img object-fit:cover>` → bottom gradient caption (vendor color dot + vendor name left; Newsreader-italic 12.5px **true date** right). **On hover OR when selected**: show a 24px checkbox top-left; **on hover**: show a white **"Open at vendor ↗"** pill top-right. Selected tile: 2.5px accent border + inset white ring. Clicking the tile body opens the lightbox; checkbox/vendor clicks `stopPropagation`.
- **Empty state**: Newsreader-italic _"Nothing matches these filters."_ + reset button.
- **Pagination** (when results): centered `‹ Prev` / numbered page buttons with `…` gaps (model: show all if ≤7 pages, else `1 … [p-1 p p+1] … last`) / `Next ›`. Active page = accent fill, weight 700. Disabled prev/next = muted + opacity .5. Below: mono `Showing a–b of N`.

### D. Senders & sources (`isSenders`) — single column, `max-width:980px`
- Newsreader 28px "Senders & sources" + intro paragraph.
- **Email senders** section: header + mono stat `T discovered · I included · O off`; helper line; **add row** = text input (placeholder `name@vendor.com   or   @vendor.com`) + primary **"Add source"**. Validates email or `@domain`; Enter adds; dupes/invalid raise a toast. **Table** (radius 14px, header row `--surface-2`): columns `Sender | Image emails (right) | Mapped vendor | Include (right)`. Each row: mono address (+ "Added manually" tag when manual), mono count, a **vendor `<select>`** (`— Unmapped —` + vendor options), and an **iOS-style toggle** (40×23 track, 19px knob, accent when on). Off rows render at opacity .5. Sorted by count desc. Footer note: "Changes apply on the next sync (hourly)…".
- **Google Drive sources** section: header + mono stat `T connected · I on`; helper; add row = folder/drive text input + **"Add drive"** + ghost **"Connect a Google account ↗"**. **Table** columns `Drive / folder | Account | Images (right) | Include (right)`: name + kind tag (e.g. "Shared drive"/"Folder") + optional "Added manually" tag; mono account; mono count; toggle. Footer note about Google authorization + read-only image access.

### E. Lightbox (`lightboxOpen`) — fixed overlay (`--overlay`), lbIn
Modal `max-width:1180px`, `max-height:760px`, radius 16px, two columns.
- **Left**: image stage — gradient bg + the full image (`background-size:contain`). Round prev/next nav buttons (when available); bottom-left mono `i of N` pill. Esc closes, ←/→ navigate.
- **Right** (344px): scrollable metadata —
  - mono **filename** (break-all).
  - **True source date** eyebrow → Newsreader-italic 23px long date; under it `Added to library · <nasDate>`.
  - hairline. **Source account**: name + type tag (Gmail / Google Drive).
  - if email: **From** (mono sender) + “subject” in quotes. if drive: **Drive path** (mono).
  - **Vendor**: color dot + name.
  - **In collections**: chips (pill, `--surface-2`) or "Not in any collection yet."
  - **Footer** (top border): **Download** (accent) + **Open at vendor ↗** (ghost) side by side; full-width **"+ Add to folder"** ghost button → popover (folioIn) listing collections with a check when the image is a member (toggles membership).
- Round **close** (×) button top-right of the modal.

### F. Toast (`toastOpen`)
Fixed bottom-center, `--ink` bg / `--bg` text, mono 13px, radius 11px, toastIn, auto-dismiss ~2.4s. Used for: download prep/done, open-at-vendor, membership changes, add-source results, validation errors, connect-Google.

---

## 3. Domain shape the UI expects (map to real API)
Each **image**: `id, filename, vendorName, vendorColor, account{id,name,type:'gmail'|'drive'}, trueDate, nasDate (ingested), sender?, subject?, drivePath?, vendorUrl, thumb, full, bg(gradient), folders[]`.
- **Sort keys**: `newest`(trueDate desc, default), `oldest`, `name`(A–Z), `vendor`, `account`.
- **Filters**: folder, enabled accounts (set), enabled vendors (set), dateFrom/dateTo (ISO yyyy-mm-dd compared against image ISO date), free-text `q` across filename+vendor+sender+subject+account+drivePath.
- **Vendor color dot** comes from a per-vendor color; production should store/derive a stable color per vendor (see `vendors.*` — prototype palette: northwind `#6b8f9c`, mori `#b07d62`, cedar `#6f8f6a`, halcyon `#8a7fb0`, meridian `#c2a14e`, brightleaf `#5fa08a`, verde `#a36b78`).
- **Pagination** is server-side; page size ∈ {25,50,100,200}; range label `Showing a–b of N`.

## 4. Production wiring notes (do NOT copy mock behavior literally)
- Replace `picsum`/gradient mock thumbs with real `GET /api/images/{id}/thumb` (gradient bg stays as a loading placeholder behind the `<img>`).
- Lightbox full image → `GET /api/images/{id}/file`.
- "Download selected (zip)" / lightbox "Download" → `POST /api/download`.
- "Open at vendor ↗" → open `vendor_url` in a new tab.
- Collections add/remove → folder membership endpoints; counts come from the API, not recomputed client-side.
- Senders/drives tables → senders/accounts endpoints; toggles persist server-side; "discovered" populates known senders.
- Theme/accent/density are client-only (localStorage); they are NOT server state.
