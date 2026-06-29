# Deploying Folio on a QNAP TBS-464 (Container Station)

This is the operator runbook for deploying Folio on a QNAP TBS-464 NAS running
QTS + Container Station. Follow the numbered sections in order. Every command,
service name, port, env key, and volume path below matches the repository's
`docker-compose.yml`, `.env.example`, and the worker CLI on disk — do not
invent flags.

> Conventions used here:
> - **NAS shell** = an SSH session on the QNAP (Control Panel → Network &
>   File Services → enable SSH, then `ssh admin@NAS-IP`).
> - `$FOLIO` = the directory on the NAS that holds the cloned repo (the
>   directory containing `docker-compose.yml`). Examples use
>   `/share/CACHEDEV1_DATA/Container/folio` — adjust to your volume name.
> - `docker compose` (v2 plugin) ships with current Container Station. If your
>   build only has the legacy `docker-compose` binary, substitute it.

---

## 0. Quick start (scripts)

In a hurry, or already familiar with the box? The repo ships helper scripts that
collapse the manual steps below. The detailed sections (Google Cloud setup, NAS
storage, networking, backups, troubleshooting) are still worth reading once.

```bash
cd $FOLIO
scripts/bootstrap.sh                 # generate secrets, scaffold .env, make dirs
# ... drop secrets/google_client_secret.json in place (section 3) ...
docker compose up -d --build         # build + start db, worker, portal
docker compose run --rm worker init-db
scripts/oauth.sh                     # OAuth the 3 Drive + 3 Gmail accounts
scripts/verify.sh                    # health-check the deployment
```

`make bootstrap`, `make oauth`, `make verify` are equivalents. The end-to-end
copy-paste walkthrough lives in **[docs/QUICKSTART.md](docs/QUICKSTART.md)**.
The numbered sections below remain the authoritative reference.

---

## 1. Prerequisites & hardware note

| Item | Requirement |
| --- | --- |
| Model | QNAP TBS-464 (Intel Celeron N5105, **x86_64**) |
| RAM | **8 GB, NON-EXPANDABLE** — this is the binding constraint |
| Storage | all-NVMe (good random IO; fine for Postgres + thumbnails) |
| QTS apps | **Container Station** installed (provides Docker + Compose) |
| Access | SSH enabled, or Container Station GUI access |

Memory budget: the three containers are capped in `docker-compose.yml` at
**db 1.5g + worker 2.5g + portal 1g = 5 GB**, intentionally leaving ~3 GB for
QTS itself and disk cache. **Do not raise these caps** on this box; if anything,
the worker is the largest because Drive ingestion buffers image bytes and shells
out to `exiftool`. See section 11.

x86_64 matters: Folio runs natively (no emulation), and the future
Chromium/Playwright vendor-browser adapters will run natively too. No ARM image
juggling.

You will also need, before you finish:
- A Google account with access to the Google Cloud Console (section 3).
- The 3 Gmail + 3 Drive account emails you intend to connect (section 7).

---

## 2. Create NAS storage for media / thumbnails / tokens

Folio's `docker-compose.yml` declares **named Docker volumes**:

```yaml
volumes:
  pgdata:        # Postgres data (precious — back this up)
  media:         # original image library (worker rw, portal ro)
  thumbnails:    # generated thumbnail cache (rebuildable)
  tokens:        # Fernet-encrypted OAuth tokens (precious)
```

and one **bind mount** for the Google client secret:

```yaml
- ./secrets:/data/secrets:ro   # in the worker service
```

In-container paths (from `.env.example`, do not change unless you also change
the compose mounts):

| Volume | In-container path | Env key | Access |
| --- | --- | --- | --- |
| `media` | `/data/media` | `MEDIA_ROOT` | worker rw, portal ro |
| `thumbnails` | `/data/thumbnails` | `THUMBNAIL_ROOT` | both rw |
| `tokens` | `/data/tokens` | `TOKEN_DIR` | worker rw, portal ro |
| `pgdata` | `/var/lib/postgresql/data` | (postgres) | db rw |
| `./secrets` (bind) | `/data/secrets` | `GOOGLE_CLIENT_SECRETS_FILE` dir | worker ro |

**Option A — keep the default named volumes (simplest).** Docker manages them
under Container Station's storage. Nothing to create; they appear on first
`up`. Downside: you cannot browse `media/` directly from QTS File Station.

**Option B — bind `media` (and optionally `thumbnails`) to a QNAP shared
folder so you can browse the library in File Station / back it up with the QNAP
backup app (recommended for `media`).**

1. In QTS, Control Panel → Privilege → **Shared Folders** → create a folder,
   e.g. `Folio` on your NVMe volume. Note its host path, e.g.
   `/share/CACHEDEV1_DATA/Folio`.
2. Create subdirectories from the NAS shell:
   ```bash
   mkdir -p /share/CACHEDEV1_DATA/Folio/{media,thumbnails,tokens}
   ```
3. Edit the `volumes:` mapping for the `worker` and `portal` services in
   `docker-compose.yml` to point at those host paths instead of the named
   volume, e.g. replace `media:/data/media` with
   `/share/CACHEDEV1_DATA/Folio/media:/data/media` (keep the `:ro` on the
   portal side). Keep `pgdata` as a named volume — Postgres data should NOT
   live on an SMB-backed share.

> Recommendation for this box: bind `media` (browsable + easy QNAP backup),
> leave `pgdata` and `thumbnails` as named volumes, and treat `tokens` as
> precious (named volume is fine, just include it in backups — section 12).

---

## 3. Google Cloud setup (Gmail + Drive, read-only, installed-app OAuth)

Folio uses **installed-app (Desktop) OAuth** with **read-only** scopes only:
`gmail.readonly` and `drive.readonly` (see `GOOGLE_OAUTH_SCOPES` in
`.env.example`). It never modifies your Google data.

1. Go to <https://console.cloud.google.com/> and **create a project**, e.g.
   `folio-nas`.
2. **Enable APIs**: APIs & Services → Library → enable both:
   - **Gmail API**
   - **Google Drive API**
3. **OAuth consent screen**: APIs & Services → OAuth consent screen.
   - User type: **External** (unless you have a Google Workspace org, in which
     case **Internal** is simpler — no verification needed).
   - App name (e.g. `Folio`), your support email, developer email.
   - **Scopes**: you can leave the scope list empty here; Folio requests its
     read-only scopes at auth time. If you add them, add only
     `.../auth/gmail.readonly` and `.../auth/drive.readonly`.
   - **Test users**: while the app is in **Testing** mode, add **every Gmail
     account you will connect** (all 3) as test users. Without this, OAuth will
     refuse the account. (Testing-mode refresh tokens can expire after 7 days —
     see the troubleshooting table; for a permanent setup either publish the
     app or use an Internal/Workspace project.)
4. **Create OAuth client**: APIs & Services → Credentials → Create Credentials →
   **OAuth client ID** → Application type **Desktop app**. Name it `folio-desktop`.
5. **Download** the client JSON (the "Download JSON" button on the client).
6. Place it on the NAS at `$FOLIO/secrets/google_client_secret.json`:
   ```bash
   mkdir -p $FOLIO/secrets
   # copy the downloaded file there, then:
   chmod 600 $FOLIO/secrets/google_client_secret.json
   ```
   This matches `GOOGLE_CLIENT_SECRETS_FILE=/data/secrets/google_client_secret.json`
   and the compose mount `./secrets:/data/secrets:ro`. The `secrets/*.json`
   path is gitignored, so it never lands in version control.

---

## 4. Prepare the `.env` file

From `$FOLIO`:

```bash
cp .env.example .env
```

Generate the two **required** secrets (run on the NAS, or anywhere with Python /
openssl, then paste the values into `.env`):

```bash
# FERNET_KEY (encrypts OAuth tokens at rest):
python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())"

# PORTAL_SECRET_KEY (signs the portal session cookie):
python3 -c "import secrets; print('PORTAL_SECRET_KEY=' + secrets.token_urlsafe(48))"
```

No Python handy on the NAS? Generate inside a throwaway container:

```bash
docker run --rm python:3.12-slim sh -c \
  "pip -q install cryptography >/dev/null 2>&1; \
   python -c \"from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())\"; \
   python -c \"import secrets; print('PORTAL_SECRET_KEY=' + secrets.token_urlsafe(48))\""
```

`openssl` alternative for the portal cookie (Fernet still needs the Python
one-liner above):

```bash
echo "PORTAL_SECRET_KEY=$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=')"
```

Now edit `.env` and set, at minimum:

| Key | Set to |
| --- | --- |
| `FERNET_KEY` | the generated Fernet key (**never lose this** — losing it makes stored tokens undecryptable) |
| `PORTAL_SECRET_KEY` | the generated cookie secret |
| `ADMIN_USERNAME` | your portal login (default `admin`) |
| `ADMIN_PASSWORD` | a strong password — **required** to seed the admin user |
| `POSTGRES_PASSWORD` | a strong DB password (and keep `DATABASE_URL` in sync) |
| `DATABASE_URL` | must match `POSTGRES_USER/PASSWORD/DB`; default `postgresql+psycopg://folio:folio@db:5432/folio` |

Leave the `MEDIA_ROOT` / `THUMBNAIL_ROOT` / `TOKEN_DIR` /
`GOOGLE_CLIENT_SECRETS_FILE` defaults as-is unless you changed the compose
mounts in section 2. Scheduler intervals and `LOG_LEVEL` have working defaults.

> If `DATABASE_URL` and the `POSTGRES_*` values disagree, the db container
> initializes one user but the app connects as another and fails. Keep them
> consistent.

Protect the file:

```bash
chmod 600 $FOLIO/.env
```

---

## 5. Get the code onto the NAS and deploy

Put the repository on the NAS (git clone over SSH, or copy the project folder
into a shared folder via File Station). End state: `$FOLIO/docker-compose.yml`
exists, plus `$FOLIO/.env` and `$FOLIO/secrets/google_client_secret.json`.

The compose **build context is the repo root** for both images, so always run
compose from `$FOLIO`.

**Option A — Container Station GUI.** Container Station → **Applications** →
**Create** → choose *Create from docker-compose.yml* (a.k.a. "Application").
Paste / point it at `$FOLIO/docker-compose.yml`. Container Station builds the
worker + portal images and starts all three services. (The `.env` next to the
compose file is read automatically.)

**Option B — CLI (recommended; clearer logs).** From the NAS shell:

```bash
docker compose -f $FOLIO/docker-compose.yml --project-directory $FOLIO up -d --build
```

or simply, when your shell is already in `$FOLIO`:

```bash
docker compose up -d --build      # or: make build && make up
```

The worker entrypoint runs `alembic upgrade head` automatically on every start,
so the schema is created/upgraded before the scheduler launches. The portal also
seeds the admin user on startup. Confirm:

```bash
docker compose ps                 # all three Up; db should be healthy
docker compose logs -f --tail=100 # or: make logs
```

---

## 6. One-time database init (migrate + seed admin)

Although the worker entrypoint already migrates on boot, run the explicit
init once to migrate **and** seed the portal admin user deterministically:

```bash
docker compose run --rm worker init-db      # or: make init-db
```

Expected log lines: `migrations.upgrade ...`, then either
`seed_admin.created username=<ADMIN_USERNAME>` or `seed_admin.exists ...`.
If you see `seed_admin.skipped reason=ADMIN_PASSWORD not set`, fix `ADMIN_PASSWORD`
in `.env` and re-run.

---

## 7. Connect each account via OAuth (3 Drive + 3 Gmail)

OAuth is **interactive** and **per account**. Drive and Gmail are authorized
separately — even when an email is the same Google identity, Folio keeps a
distinct account row per provider. Use `docker compose run --rm` (it gives the
command a TTY for the copy/paste flow). Do **not** use `docker compose exec` for
this (the long-running worker is busy with the scheduler).

Drive accounts:

```bash
docker compose run --rm worker auth-drive --account drive1@example.com
docker compose run --rm worker auth-drive --account drive2@example.com
docker compose run --rm worker auth-drive --account drive3@example.com
# or: make auth-drive ACCOUNT=drive1@example.com
```

Gmail accounts:

```bash
docker compose run --rm worker auth-gmail --account gmail1@example.com
docker compose run --rm worker auth-gmail --account gmail2@example.com
docker compose run --rm worker auth-gmail --account gmail3@example.com
# or: make auth-gmail ACCOUNT=gmail1@example.com
```

**Headless consent (the NAS has no browser):** the installed-app flow prints a
Google **consent URL** to the terminal. Copy that URL into a browser on your
laptop/phone, sign in **as the exact account you passed in `--account`**, grant
the read-only Gmail/Drive access, and Google returns an **authorization code**.
Paste that code back into the waiting worker prompt. On success Folio writes a
Fernet-encrypted token to `TOKEN_DIR` (one `*.token` file per account) and
upserts the `accounts` row. Repeat for all six.

> If a browser tries to redirect to `http://localhost...` and fails, that's
> expected on a headless box — just copy the `code=...` value from the URL the
> browser was redirected to, and paste it back. (The exact prompt wording comes
> from the Phase-2 auth module; the copy-the-URL / paste-the-code shape is
> fixed.)

Verify tokens landed:

```bash
docker compose run --rm worker python -c "import os; print(sorted(os.listdir('/data/tokens')))"
```

---

## 8. First Drive sync + watching logs + reconcile

Kick off an initial **full** crawl per Drive account (the first run should be
full; afterwards the scheduler runs incrementals automatically):

```bash
docker compose run --rm worker sync-drive --account drive1@example.com --full
docker compose run --rm worker sync-drive --account drive2@example.com --full
docker compose run --rm worker sync-drive --account drive3@example.com --full
# all accounts at once (omit --account):  worker sync-drive --full
# or: make sync-drive ACCOUNT=drive1@example.com   (incremental)
```

Watch progress live in another terminal:

```bash
docker compose logs -f worker        # or: make logs
```

What happens per image: Folio downloads the original bytes, computes the
`sha256` of those **original** bytes (identity / dedup), writes the file under
`MEDIA_ROOT` at `<account>/<YYYY>/<YYYY-MM-DD>_<vendor-or-drive>_<name>.<ext>`,
stamps `DateTimeOriginal`/`CreateDate` with the Drive `createdTime` via
`exiftool`, and records `images` + `image_sources` rows. Re-running is
idempotent (keyed on `UNIQUE(account_id, source_type, source_id)`), so a second
sync only adds new files.

Reconcile (compare upstream item counts vs what was imported, recorded in
`ingest_runs`):

```bash
docker compose run --rm worker reconcile        # or: make reconcile
```

---

## 9. Gmail sender discovery + the Senders screen

Gmail ingestion is **allow-list based**: Folio only pulls images from senders
you have enabled. First discover candidate senders:

```bash
docker compose run --rm worker discover-senders --account gmail1@example.com
# all accounts:  worker discover-senders      (or: make discover)
```

This scans Gmail and upserts `senders` rows (address, domain, display name,
`discovered_count`, `last_seen_at`). Then, in the **portal → Senders** screen
(section 10), review the discovered list and **enable** the senders/vendors you
want to ingest from. Only enabled senders are pulled by the (Phase-2) Gmail
sync. `discover-senders` also runs on the scheduler at
`DISCOVER_SENDERS_INTERVAL_MINUTES` (default 720 = 12h).

---

## 10. Access the portal, log in, and network guidance

The portal listens on container port **8080**, published to the host on
`PORTAL_PORT` (default `8899` — QTS's own admin UI uses 8080, so publishing the
host on 8080 fails with "address already in use"). Open:

```
http://NAS-IP:8899
```

Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`. The library is
sorted **newest first by source date** by default. Health check (no auth):
`http://NAS-IP:8899/health` → `{"status":"ok"}`.

**Network safety:** Folio has a single admin login over a session cookie and
serves your original images. Keep it **LAN-only** or behind a **VPN**:

- Do **not** port-forward the portal (`PORTAL_PORT`, default 8899) to the public internet.
- For remote access, use the QNAP VPN (QVPN / WireGuard / Tailscale) and reach
  `http://NAS-IP:8899` over the tunnel.
- If you must expose it, put it behind QNAP's reverse proxy / a TLS terminator
  with HTTPS, and set the portal cookie to secure (Phase-2 / proxy concern).
  The cookie is `same_site=lax`, `https_only=false` by default — fine on a LAN,
  not for raw internet exposure.

---

## 11. Memory caps / Container Station resource limits

The caps live in `docker-compose.yml` and are already tuned for 8 GB:

```yaml
db:     mem_limit: 1.5g
worker: mem_limit: 2.5g
portal: mem_limit: 1g
```

If you deployed via the Container Station GUI and it shows per-container limits,
leave them matching the compose values. Guidance for this box:

- Total container memory **must stay ≈5 GB** so QTS keeps ~3 GB. Do not bump
  caps to "use all the RAM" — Postgres + QTS rely on free RAM for page cache.
- If the worker gets OOM-killed during large syncs, prefer **per-account
  incremental** syncs over a single all-accounts `--full`, rather than raising
  the cap.
- The portal is the smallest; thumbnail generation is on-demand and cached on
  disk, so 1 GB is ample.

Check live usage:

```bash
docker stats --no-stream
```

---

## 12. Backups — the Postgres DB is precious

Priority order of what to back up:

1. **Postgres database** (`pgdata`) — the source of record: all metadata,
   folders, sender allow-list, ingest history. **Cannot be regenerated.**
2. **`tokens/`** — Fernet-encrypted OAuth tokens. Losing them just means
   re-running `auth-*`, but back them up to avoid re-consenting all 6 accounts.
3. **`FERNET_KEY` (in `.env`)** — without it the token files are useless.
   Back up `.env` (securely!) alongside the DB.
4. **`media/`** — the original images. Large but largely re-fetchable from
   Drive/Gmail. Back up if you value the EXIF-stamped local copies and don't
   want to re-crawl.
5. `thumbnails/` — **do not bother**; regenerated on demand.

**Nightly `pg_dump` into a backed-up folder.** Create
`$FOLIO/scripts/pg_backup.sh` (the `scripts/` dir is yours to create) pointing
its output at a QNAP shared folder that your QNAP backup job already covers:

```bash
#!/usr/bin/env bash
set -euo pipefail
OUT=/share/CACHEDEV1_DATA/Backups/folio          # adjust to a backed-up share
mkdir -p "$OUT"
cd /share/CACHEDEV1_DATA/Container/folio          # = $FOLIO
STAMP=$(date +%F)
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-folio}" -d "${POSTGRES_DB:-folio}" \
  | gzip > "$OUT/folio-db-$STAMP.sql.gz"
# keep 14 days
find "$OUT" -name 'folio-db-*.sql.gz' -mtime +14 -delete
```

Schedule it with QTS **Control Panel → Task Scheduler → Create → User-defined
script**, daily. (Folio's APScheduler handles *app* jobs only; OS-level backups
use the QTS scheduler.)

Restore (into an empty DB):

```bash
gunzip -c /share/CACHEDEV1_DATA/Backups/folio/folio-db-YYYY-MM-DD.sql.gz \
  | docker compose exec -T db psql -U folio -d folio
```

Finally, add the QNAP backup app (Hybrid Backup Sync / HBS 3) to copy the
backup share + `media/` + `.env` + `tokens/` off the NAS.

---

## 13. Updating / redeploying

```bash
cd $FOLIO
git pull                      # or copy in the new code
docker compose build          # rebuild worker + portal images
docker compose up -d          # recreate changed containers
docker compose logs -f --tail=100
```

Schema migrations apply **automatically** on worker start (entrypoint runs
`alembic upgrade head`); you can also run `make init-db` explicitly. Named
volumes (`pgdata`, `media`, `tokens`, `thumbnails`) survive `up`/`down`/rebuilds.
**Never** run `docker compose down -v` unless you intend to destroy the database
and all stored images — `-v` deletes the named volumes.

To roll back, redeploy the previous code revision; note that down-migrations are
not part of normal ops — restore from a `pg_dump` (section 12) if a migration
needs reverting.

---

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `auth-*` fails / OAuth refused for an account | Account not added as a **test user** on the consent screen, or wrong account chosen in the browser | Add the email under OAuth consent → Test users (section 3); sign in as the exact `--account` email |
| Accounts stop syncing after ~7 days | Consent screen in **Testing** mode expires refresh tokens after 7 days | Publish the app (or use an Internal/Workspace project), then re-run `auth-drive`/`auth-gmail` |
| `CryptoConfigError: FERNET_KEY is not set/invalid` | `FERNET_KEY` missing or not a valid urlsafe-base64 32-byte key | Regenerate with the Fernet one-liner (section 4), put it in `.env`, restart. If you *changed* an existing key, old `*.token` files are undecryptable — re-run `auth-*` |
| `ExiftoolNotFound: exiftool binary 'exiftool' not found` | Running a command in an image without exiftool, or `EXIFTOOL_BINARY` misconfigured | exiftool ships in the **worker** image (`libimage-exiftool-perl`); run EXIF work in the worker and keep `EXIFTOOL_BINARY=exiftool` |
| `exif.stamp_no_update` warnings | File format can't hold EXIF datetimes (some PNG/GIF) | Harmless — the image is still imported and `images.source_date` in the DB stays authoritative; only the in-file tag is skipped |
| Drive/Gmail sync slows or logs 403 `rateLimitExceeded` / `userRateLimitExceeded` | Google API rate limits | Folio backs off (tenacity) and resumes; let the scheduler retry. Avoid running multiple all-account `--full` syncs at once |
| Worker container restarts / OOM-killed during sync | Memory pressure under the 2.5 GB cap on a large full crawl | Sync **per account** incrementally instead of one big all-accounts `--full`; check `docker stats`; do not exceed the section 11 caps |
| Portal won't start / login fails | `ADMIN_PASSWORD` unset (admin never seeded), or `PORTAL_SECRET_KEY` unset | Set both in `.env`, restart portal, run `make init-db` to seed the admin |
| `db` container unhealthy / portal & worker can't connect | `DATABASE_URL` disagrees with `POSTGRES_*`, or db still starting | Make them consistent (section 4); wait for the healthcheck (`docker compose ps`); check `docker compose logs db` |
| Thumbnails blank / slow first load | First request generates + caches the thumbnail under `THUMBNAIL_ROOT` | Normal on first view; subsequent loads are cached. Safe to delete `thumbnails/` to reclaim space — it regenerates |
| Images missing from the library after sync | Sender not enabled (Gmail), or file deduplicated as already-present | Enable the sender in the portal Senders screen (section 9); identical bytes dedupe by `sha256` (one image, multiple sources) — expected |
| Changed `.env` but nothing changed | Containers read `.env` at start | `docker compose up -d` to recreate; for the worker, env applies on its next start |

---

## 15. New commands: assist tasks, vendor-browser sync, DB backup

These are the operations added with the email/vendor-browser acquisition phase.
All are exposed through the worker CLI and the `Makefile`.

### Vendor-browser Gmail ingestion (`sync-gmail`)

Pulls images that arrive as vendor **emails** by driving a headless Chromium
(Playwright). It is RAM-heavy, so on this 8 GB box it is deliberately
constrained: it runs **one job at a time** (`VENDOR_BROWSER_MAX_JOBS=1`, keep it
at 1) and the scheduler only fires it when `BROWSER_ENABLED=true` **and** the
local hour is inside the off-hours window
`[BROWSER_OFFHOURS_START, BROWSER_OFFHOURS_END)` in your `TIMEZONE` (defaults
`1`–`6`). To trigger a run manually (it ignores the window when you invoke it
directly):

```bash
docker compose run --rm worker sync-gmail          # or: make sync-gmail
docker compose run --rm worker sync-gmail --account gmail1@example.com
```

Set `BROWSER_ENABLED=false` in `.env` to disable vendor-browser ingestion
entirely. The worker `mem_limit` stays at 2.5g and compose sets `shm_size: 512mb`
for Chromium; do not raise these on this box.

### Human-assist tasks (`assist-list` / `assist-resolve`)

When an email can't be auto-ingested (no adapter for the vendor, a CAPTCHA, a
failed login), Folio records a **pending assist task** instead of dropping the
image. List them, then resolve one by supplying the original image yourself:

```bash
docker compose run --rm worker assist-list                  # or: make assist-list
# Download the original from the vendor email by hand, then:
docker compose run --rm worker assist-resolve --id 42 --file /data/tmp/original.jpg
```

`assist-resolve` ingests the file through the normal pipeline (dedup by sha256,
EXIF date-stamping) and marks the task `resolved`. The `--file` path must be
readable **inside** the worker container — drop the file on a mounted volume
(e.g. a bind-mounted share, or `docker compose cp` it in) before running.

### Database backup (`backup-db` / the `backup` service)

A timestamped, custom-format `pg_dump` archive written to `BACKUP_DIR`
(`/data/backups`, the `backups` named volume), with archives older than
`BACKUP_RETENTION_DAYS` (default 14) pruned after each successful dump. It never
logs credentials.

```bash
docker compose run --rm backup                     # or: make backup
# equivalently, via the worker CLI:
docker compose run --rm worker backup-db
```

The `backup` service is behind the `backup` compose profile (mem_limit 512m), so
`docker compose up` never starts it — invoke it explicitly, or schedule it with
QTS Task Scheduler. (This is in addition to the host-level `pg_dump` script in
section 12; either approach works — this one keeps the dump on the `backups`
volume.)

### `SESSION_HTTPS_ONLY` and `ACCESS_TOKEN_HASH` notes

- **`SESSION_HTTPS_ONLY`** (default `false`): adds the `Secure` flag to the
  portal session cookie. Leave it `false` for plain-HTTP LAN access
  (`http://NAS-IP:8899`) — with `Secure` set, the browser won't send the cookie
  over HTTP and you can't stay logged in. Set it `true` **only** when the portal
  is reached over HTTPS (reverse proxy / TLS terminator / HTTPS VPN), then
  restart the portal.

- **`ACCESS_TOKEN_HASH`** (optional): an alternative to username+password login.
  It stores the **argon2id hash** of a portal access token — the plaintext token
  is never written to disk. `scripts/bootstrap.sh` mints one for you and prints
  the one-time token; to rotate it later:

  ```bash
  python services/portal/scripts/make_token.py
  ```

  Paste the printed `ACCESS_TOKEN_HASH=` line into `.env` (replacing any old
  value), keep the printed `TOKEN:` secret, and restart the portal. Then paste
  that token into the portal to log in. Leaving `ACCESS_TOKEN_HASH` blank simply
  disables token login; `ADMIN_USERNAME`/`ADMIN_PASSWORD` still works.

---

### Quick command reference

| Action | Command |
| --- | --- |
| Build images | `docker compose build` / `make build` |
| Start stack | `docker compose up -d` / `make up` |
| Stop stack | `docker compose down` / `make down` (no `-v`!) |
| Tail logs | `docker compose logs -f --tail=200` / `make logs` |
| Init DB + seed admin | `docker compose run --rm worker init-db` / `make init-db` |
| Auth a Drive acct | `docker compose run --rm worker auth-drive --account <email>` |
| Auth a Gmail acct | `docker compose run --rm worker auth-gmail --account <email>` |
| Full Drive sync | `docker compose run --rm worker sync-drive --account <email> --full` |
| Discover senders | `docker compose run --rm worker discover-senders` / `make discover` |
| Vendor-browser Gmail sync | `docker compose run --rm worker sync-gmail` / `make sync-gmail` |
| List assist tasks | `docker compose run --rm worker assist-list` / `make assist-list` |
| Resolve an assist task | `docker compose run --rm worker assist-resolve --id <id> --file <path>` |
| DB backup | `docker compose run --rm backup` / `make backup` |
| Reconcile | `docker compose run --rm worker reconcile` / `make reconcile` |
| Bootstrap (.env + secrets) | `scripts/bootstrap.sh` / `make bootstrap` |
| OAuth all accounts | `scripts/oauth.sh` / `make oauth` |
| Verify deployment | `scripts/verify.sh` / `make verify` |
| psql shell | `docker compose exec db psql -U folio -d folio` / `make psql` |

See **[docs/OPERATIONS.md](docs/OPERATIONS.md)** for day-2 operations: incremental
vs full sync, scheduler intervals, dedup & date preservation internals, adding a
vendor, reconciliation, and where logs live.
