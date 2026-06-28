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

---

## 10. Backups & restore (`pg_dump` custom format)

Folio ships a first-class database backup path that is independent of the
DEPLOY-QNAP §12 `pg_backup.sh` recipe (which uses plain SQL `pg_dump`). This one
produces a **custom-format** (`pg_dump -Fc`) archive, which restores with
`pg_restore` and supports selective restore.

### Taking a backup — `backup-db`

The `worker backup-db` verb is implemented in `services/worker/worker/backup.py`
and wired to a profile-gated one-shot **`backup`** compose service:

```bash
# Preferred: the dedicated one-shot service (mem_limit 512m, off the always-on stack)
docker compose run --rm backup

# Equivalent verb on the worker image:
docker compose run --rm worker backup-db
```

What it does:

- Runs `pg_dump --format=custom` against `DATABASE_URL` and writes
  `folio-<UTC-timestamp>.dump` (e.g. `folio-20260627T010000Z.dump`) into
  **`BACKUP_DIR`** (default `/data/backups`), which is the **`backups`** named
  volume (worker + backup containers mount it read-write).
- Prunes archives older than **`BACKUP_RETENTION_DAYS`** (default `14`).
- **Never logs credentials** — only the `host:port/db` target and the output
  filename/size are logged. (The connection URL with the password is passed as a
  `pg_dump` argv and is briefly visible in the container process list during the
  dump; it is never written to logs.)

Schedule it nightly with the **QTS Task Scheduler** (a `docker compose run --rm
backup` user-defined script), the same way as the §12 recipe — Folio's
APScheduler handles *app* jobs only, not OS-level backups.

### Inspecting / listing archives

```bash
docker compose run --rm --no-deps --entrypoint sh backup -c 'ls -1t /data/backups/folio-*.dump'
# or:
scripts/restore.sh --list
```

### Restoring — `scripts/restore.sh`

Because the dump is custom format it is restored with `pg_restore`, **not**
`psql`. Use the helper:

```bash
scripts/restore.sh --list                          # show dumps in the backups volume
scripts/restore.sh folio-20260627T010000Z.dump     # restore a dump from the volume
scripts/restore.sh /share/Backups/folio/x.dump     # restore a dump file on the host
scripts/restore.sh --yes <name|path>               # skip the typed confirmation
```

The script streams the chosen dump into `pg_restore --clean --if-exists
--no-owner` inside the running `db` container. It is **destructive**
(`--clean` drops existing objects first), so it requires you to type the
database name to proceed. Harmless `... does not exist, skipping` messages on the
first `--clean` pass of a fresh DB are expected. Stop the readers/writers first
for a clean restore:

```bash
docker compose stop portal worker
scripts/restore.sh folio-20260627T010000Z.dump
docker compose up -d
```

> Rolling back a bad migration: down-migrations are not part of normal ops —
> restore the most recent pre-upgrade `.dump` instead (DEPLOY-QNAP §13).

### Getting the backups OFF the NAS

The `backups` volume lives on the NAS. **Include `/data/backups` (the `backups`
volume) in the QNAP backup job** (Hybrid Backup Sync / HBS 3), alongside
`media/`, `.env`, and `tokens/` (DEPLOY-QNAP §12). A backup that never leaves the
box does not survive a disk failure. To park dumps directly on a QNAP-backed
share instead of the named volume, point `BACKUP_DIR` at a bind-mounted path.

---

## 11. Scheduler — the off-hours `sync-gmail` job (update to §4)

§4 listed three interval jobs and noted `sync-gmail` was a stub left off the
scheduler. As of Phase-2 the `schedule` command (`services/worker/worker/main.py`)
registers a **fourth** job:

| Job id | Calls | Interval env key | Default |
| --- | --- | --- | --- |
| `sync-gmail` | `run_gmail_sync(None)` | `GMAIL_SYNC_INTERVAL_MINUTES` | 360 min (6h) |

This is the **vendor-browser** email-image ingestion path and it is RAM-heavy
(headless Chromium). It is therefore **double-gated** and its body self-skips
unless **both** hold at the interval tick:

1. **`BROWSER_ENABLED=true`** — the master switch (skips with
   `scheduler.skip job=sync-gmail reason=browser_disabled` when off).
2. The local hour (in `TIMEZONE`) is inside the off-hours window
   **`[BROWSER_OFFHOURS_START, BROWSER_OFFHOURS_END)`** (defaults `1`–`6`, i.e.
   01:00–06:00). Outside it: `scheduler.skip ... reason=outside_offhours`.

Like the other jobs it is `max_instances=1` + `coalesce=True`. Combined with
**`VENDOR_BROWSER_MAX_JOBS=1`** (which **must stay `1`** on the 8 GB box) this
guarantees **one** browser job runs at a time, off-hours only — the worker's
2.5 g cap and the compose `shm_size: 512mb` are sized around that single tab.

Tuning: edit `GMAIL_SYNC_INTERVAL_MINUTES`, `BROWSER_ENABLED`,
`BROWSER_OFFHOURS_START`/`_END`, or `TIMEZONE` in `.env`, then
`docker compose up -d` (no live reload). To run an ad-hoc vendor-browser sync
outside the window: `docker compose run --rm worker sync-gmail`.

---

## 12. The Assist queue (semi-automated CAPTCHA / login fallback)

Some vendor emails can't be auto-ingested — there's no browser adapter for the
vendor, the download sits behind a CAPTCHA, or a login failed. Rather than drop
those, `run_gmail_sync` records an **`assist_tasks`** row (status `pending`) so a
human can finish the job. Rows are idempotent on
`UNIQUE(account_id, email_message_id, vendor_url)`, so re-runs never duplicate a
task.

**Lifecycle:** `pending` → (`in_progress` when a worker/browser claims it) →
terminal `resolved` / `failed` / `skipped`. `reason` records why it landed
(`no_adapter`, `captcha`, `login_failed`).

**Day-2 flow:**

```bash
# 1. See what needs a human (pending tasks: id, vendor, subject, sender, url):
docker compose run --rm worker assist-list

# 2. Open the vendor URL from the task, download the ORIGINAL image yourself,
#    and copy/mount it where the worker can read it (e.g. under a mounted path).

# 3. Resolve the task by ingesting that original for the specific task id:
docker compose run --rm worker assist-resolve --id 42 --file /data/tmp/order-original.jpg
```

`assist-resolve` runs the supplied file through the normal pipeline
(`source_type=email`, `source_id=` the Gmail message id, `source_date=` the email
Date header), then sets the task to `resolved` with `resolved_image_id` and
`resolved_at`. Ingestion stays **idempotent** and **date-preserving** exactly as
in §5 — the manually-fetched original is deduped on its sha256 and EXIF-stamped
like any other image.

> Implementation note: `assist-list` / `assist-resolve` lazily import
> `worker.assist`, following the same lazy-leaf pattern as the other verbs (§2).
> Until that leaf module ships they raise `ImportError` and only those two
> commands are affected — the rest of the CLI keeps working.
