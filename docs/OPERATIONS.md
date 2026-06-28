# Folio Operations (Day-2)

Operational reference for running Folio after it is deployed. For first-time
NAS deployment (QNAP TBS-464, Container Station), see
**[../DEPLOY-QNAP.md](../DEPLOY-QNAP.md)**.

Everything below reflects the actual on-disk CLI (`services/worker/worker/main.py`),
`docker-compose.yml`, `.env.example`, and the `folio_core` shared package. No
invented flags.

---

## 1. The moving parts

| Service | Role | Default command | Memory cap |
| --- | --- | --- | --- |
| `db` | Postgres 16, single source of record | (postgres) | 1.5g |
| `worker` | Ingestion CLI + APScheduler loop | `schedule` | 2.5g |
| `portal` | FastAPI JSON API + UI, port 8080 | (uvicorn) | 1g |

The worker's container **entrypoint** runs `alembic -c $FOLIO_ALEMBIC_INI
upgrade head` on every start (so the schema is always current), then execs the
requested command. With no override, that command is `schedule` — the
long-running APScheduler loop. All other CLI verbs are run on demand via
`docker compose run --rm worker <verb>`.

Schema is owned **solely by Alembic** (`packages/folio_core/migrations`). The
portal never migrates; it only reads/writes rows.

---

## 2. Worker CLI — what each command does

Run any of these as `docker compose run --rm worker <command>` (a `--rm`
one-shot container that shares the same image, env, and volumes as the running
worker). Several have `make` shortcuts.

| Command | Args | What it does |
| --- | --- | --- |
| `init-db` | — | `alembic upgrade head` + seed the admin user from `ADMIN_USERNAME`/`ADMIN_PASSWORD` (idempotent). Implemented in the spine. |
| `auth-drive` | `--account <email>` (required) | Installed-app OAuth (`drive.readonly`); saves the Fernet-encrypted refresh token under `TOKEN_DIR`; upserts the `accounts` row (`provider='drive'`). |
| `auth-gmail` | `--account <email>` (required) | Same for Gmail (`gmail.readonly`, `provider='gmail'`). |
| `sync-drive` | `[--account <email>] [--full]` | Recursive Drive ingestion. Incremental by default; `--full` forces a complete re-scan. Omitting `--account` processes **all** Drive accounts. |
| `discover-senders` | `[--account <email>]` | Scans Gmail and upserts `senders` rows for the allow-list dropdown. All accounts if `--account` omitted. |
| `sync-gmail` | `[--account <email>]` | Vendor-browser email image ingestion. **Framework stub in this build** — wired through the CLI but the leaf logic lands in Phase-2. |
| `reconcile` | `[--account <email>]` | Compares upstream item counts vs imported counts and records the result in `ingest_runs`. |
| `schedule` | — | Runs the in-container `BlockingScheduler`. Default worker command. See §4. |

`make` shortcuts (see the `Makefile`): `make init-db`,
`make auth-drive ACCOUNT=...`, `make auth-gmail ACCOUNT=...`,
`make sync-drive [ACCOUNT=...]`, `make discover [ACCOUNT=...]`,
`make reconcile [ACCOUNT=...]`. (There is intentionally no `make` target for
`sync-gmail` or `schedule`; the scheduler runs as the worker's default command.)

> Resilience: every CLI verb **lazy-imports** its Phase-2 leaf module inside the
> command body. A missing or broken leaf only breaks that one command — the rest
> of the CLI (including `init-db`) keeps working. Keep the leaf `run_*`
> signatures exactly as the manifest specifies.

---

## 3. Incremental vs full sync

**Drive sync** is the main recurring ingestion path.

- **Incremental (default):** `sync-drive` uses a stored cursor in `sync_state`
  (`cursor_type='drive_change_token'`, one row per account) to fetch only what
  changed since the last run. This is what the scheduler runs continuously and
  what you want 99% of the time — fast and cheap on the Google API.
- **Full:** `sync-drive --full` ignores the cursor and re-walks the entire Drive
  tree. Use it for:
  - the **first** sync of a newly-authed account,
  - recovery after a suspected missed/expired change token,
  - after broadening which folders/files should be in scope.

Either way ingestion is **idempotent**: each item is keyed on
`image_sources UNIQUE(account_id, source_type, source_id)` (where `source_id`
is the Drive `fileId`). Re-running never duplicates an image — it only adds
genuinely new items and updates source metadata.

**Resumability:** long runs record progress in `ingest_runs`
(`last_page_token`, `items_seen/imported/skipped/failed`, `status`,
`source_count`, `errors`). An interrupted run can resume from its
`last_page_token` rather than restarting from zero.

**Gmail:** `discover-senders` is the discovery half (populates the allow-list);
the actual email-image `sync-gmail` is a Phase-2 framework stub in this build.
When implemented it keys idempotency on the Gmail `messageId` via the same
`image_sources` unique constraint and uses a `gmail_history_id` cursor in
`sync_state`.

---

## 4. How scheduling works (APScheduler, not cron)

The worker's default `schedule` command starts an APScheduler
`BlockingScheduler` (timezone = `TIMEZONE`, default `UTC`) with three interval
jobs:

| Job id | Calls | Interval env key | Default |
| --- | --- | --- | --- |
| `sync-drive` | `run_drive_sync(None, False)` | `SYNC_DRIVE_INTERVAL_MINUTES` | 30 min |
| `discover-senders` | `run_discover_senders(None)` | `DISCOVER_SENDERS_INTERVAL_MINUTES` | 720 min (12h) |
| `reconcile` | `run_reconcile(None)` | `RECONCILE_INTERVAL_MINUTES` | 1440 min (24h) |

Notes:
- Each job runs across **all** accounts (`account=None`), incrementally
  (`full=False` for Drive).
- Jobs are configured `max_instances=1` + `coalesce=True`, so a slow run never
  stacks up overlapping executions; a missed tick coalesces into one run.
- Each job is wrapped so **a single failure is logged and isolated** — one bad
  account or transient API error never kills the scheduler loop.
- `sync-gmail` is **not** on the scheduler in this build (it's a stub). Run
  ad-hoc syncs and `--full` Drive crawls manually with `docker compose run`.
- Changing an interval: edit the value in `.env`, then
  `docker compose up -d` (or restart the worker) so the new interval takes
  effect. There is no live reload.

This in-container scheduler is deliberate — Folio does **not** use system cron.
OS-level jobs on the NAS (e.g. nightly `pg_dump`) belong in the QTS Task
Scheduler instead (see DEPLOY-QNAP §12).

---

## 5. Dedup and date preservation (how identity works)

These two invariants are the heart of Folio; don't fight them.

**Identity = sha256 of the ORIGINAL bytes, computed BEFORE EXIF stamping.**
Order of operations during ingest:

1. Download the original bytes.
2. `folio_core.hashing.sha256_file` / `sha256_bytes` → the `images.sha256`
   (a `CHAR(64)`, UNIQUE). This is the dedup key.
3. Write the file under `MEDIA_ROOT`.
4. `folio_core.exif.stamp_source_date(path, source_date)` writes
   `DateTimeOriginal` + `CreateDate` via `exiftool`.

Because the hash is taken in step 2 (pre-stamp), re-stamping or metadata
rewrites in step 4 **never change identity** — dedup stays stable across
re-ingests. The same image arriving from two places creates **one** `images`
row and **multiple** `image_sources` rows (one image → many sources).

**Source date = the authoritative acquisition date and the default sort key.**

- Drive files: `source_date = createdTime`, `source_date_origin='drive_created'`.
- Email images: `source_date = the message Date header`, `source_date_origin='email_date'`.

`images.source_date` is what the portal sorts by (newest = `source_date DESC` is
the default), and it is stamped into the file's EXIF so the correct date travels
with the file into Apple Photos / anywhere else. The DB value is authoritative
even when a format can't hold EXIF: `stamp_source_date` returns `False` and logs
`exif.stamp_no_update` for formats like some PNG/GIF — the image is still
imported and sorts correctly; only the in-file tag is skipped.

**Stored path layout** (`folio_core.paths.build_stored_path`), relative to
`MEDIA_ROOT`:

```
<account>/<YYYY>/<YYYY-MM-DD>_<vendor-or-drive>_<sanitized-name>.<ext>
```

The year folder and date prefix come from `source_date`, so the on-disk tree
mirrors the real acquisition date. Names are sanitized to safe ASCII and
collision-suffixed (`-1`, `-2`, …) when `media_root` is passed for the on-disk
check. **Folio never moves files on disk to satisfy virtual folders** — portal
folders are pure metadata (`folders` + `folder_images`).

---

## 6. Reconciliation

`reconcile` (manual `make reconcile`, or the scheduled 24h job) compares the
**upstream `source_count`** (how many items Google says exist for an account)
against how many Folio actually imported, and records the comparison in
`ingest_runs` (`kind='reconcile'`). Use it to answer "did I miss anything?"
after a big crawl or a suspected interruption.

Inspect recent runs:

```bash
docker compose exec db psql -U folio -d folio -c \
  "SELECT id, account_id, kind, status, source_count, items_seen, items_imported, \
          items_skipped, items_failed, started_at, finished_at \
   FROM ingest_runs ORDER BY started_at DESC LIMIT 20;"
```

A persistent gap between `source_count` and `items_imported` (with
`items_failed > 0` or entries in the `errors` JSONB) is the signal to run a
`sync-drive --full` for that account and check the worker logs.

---

## 7. Adding a vendor (and the future browser adapter)

**Vendors** classify where an image came from (e.g. a print lab, a photographer)
and drive the email allow-list and, later, browser adapters. The schema:

- `vendors`: `name`, optional `domain`, **`adapter_key`** (UNIQUE — the future
  browser-adapter slug), `login_required`, `notes`.
- `senders`: a Gmail allow-list entry (`account_id`, `address`, `domain`,
  `display_name`, `vendor_id?`, `enabled`). Only `enabled` senders are ingested.

Day-2 flow to onboard a vendor:

1. Create the vendor — via the portal **Vendors** screen, or the API:
   `POST /api/vendors {name, domain?, adapter_key, login_required?}`. Pick a
   stable, unique `adapter_key` slug (it's the contract for a future adapter).
2. Run `discover-senders` so the vendor's sending addresses show up as
   candidate `senders`.
3. In the portal **Senders** screen, **enable** the relevant senders and
   optionally link each to the vendor (`vendor_id`). Disabled senders are
   ignored by Gmail ingestion.

**Future adapter (not in this build):** the platform is x86_64, so the planned
Chromium/Playwright vendor-browser adapters (for vendors whose images live
behind a login rather than arriving by email) will run natively. An adapter is
keyed to its vendor by `vendors.adapter_key`; `login_required` flags vendors
that need an authenticated browser session. No adapter code ships in the core
build — this is the data model it will plug into.

---

## 8. Logs, status, and inspection

**Logs** go to **stdout/stderr** of each container (structured via
`folio_core.logging`; level set by `LOG_LEVEL`, default `INFO`). There is no log
file on disk — view them through Docker / Container Station:

```bash
docker compose logs -f --tail=200            # all services (make logs)
docker compose logs -f worker                # worker only (ingestion + scheduler)
docker compose logs -f portal                # API/UI
docker compose logs -f db                    # Postgres
```

Useful log markers: `scheduler.start ...`, `scheduler.run job=<id>`,
`scheduler.job_failed job=<id>` (worker scheduler); `migrations.upgrade ...`,
`seed_admin.created/exists/skipped` (init); `exif.stamp_no_update` (EXIF
skips); `router.included/missing module=...` and `admin.created` (portal boot).

**Service status / health:**

```bash
docker compose ps                            # up/healthy state
docker stats --no-stream                     # live memory vs the caps
curl -s http://NAS-IP:8080/health            # {"status":"ok"}
```

**Database (the source of record):**

```bash
docker compose exec db psql -U folio -d folio        # or: make psql

# quick library stats:
#   SELECT count(*) FROM images;
#   SELECT provider, email, status FROM accounts ORDER BY id;
#   SELECT * FROM sync_state ORDER BY account_id, cursor_type;
#   SELECT max(source_date) FROM images;     -- newest acquisition date
```

The portal also exposes `GET /api/stats`
(`total_images`, `by_account`, `by_vendor`, `latest_source_date`,
`library_bytes`) once that Phase-2 router is in place.

---

## 9. Common day-2 tasks — quick recipes

```bash
# Force a full re-crawl of one Drive account (e.g. after broadening scope):
docker compose run --rm worker sync-drive --account drive1@example.com --full

# Refresh sender candidates for all Gmail accounts, then enable in the portal:
docker compose run --rm worker discover-senders        # make discover

# Check what was/ wasn't imported recently:
docker compose run --rm worker reconcile               # make reconcile

# Re-auth an account whose token expired (Testing-mode 7-day expiry, etc.):
docker compose run --rm worker auth-drive --account drive1@example.com

# Change how often Drive syncs: edit SYNC_DRIVE_INTERVAL_MINUTES in .env, then:
docker compose up -d                                   # restart worker w/ new interval

# Reclaim thumbnail-cache space (regenerated on demand):
docker compose run --rm worker sh -c 'rm -rf /data/thumbnails/*'
```

For backups, redeploys/updates, memory tuning, and the OAuth/headless flow, see
**[../DEPLOY-QNAP.md](../DEPLOY-QNAP.md)** (sections 12, 13, 11, and 7).
