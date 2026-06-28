# Folio — Quick Start

A tight, copy-pasteable path from a fresh checkout to a running stack with your
first images imported. Every command below is real: it matches
`docker-compose.yml`, `.env.example`, the worker CLI, and the helper scripts in
`scripts/`.

For the full QNAP / Container Station runbook (Google Cloud setup, NAS storage,
networking, memory caps, backups, troubleshooting) see
[DEPLOY-QNAP.md](../DEPLOY-QNAP.md). For day-2 operations see
[docs/OPERATIONS.md](OPERATIONS.md).

> Prerequisites: Docker + `docker compose` (v2), a Google OAuth **Desktop-app**
> client JSON (DEPLOY-QNAP §3), and the 3 Gmail + 3 Drive account emails you
> intend to connect. Run every command from the repo root (the directory with
> `docker-compose.yml`) — the compose build context is the repo root.

---

## 0. Bootstrap (secrets + .env + dirs)

Generates `FERNET_KEY`, `PORTAL_SECRET_KEY`, and `ACCESS_TOKEN_HASH`, writes a
locked-down `.env` from `.env.example`, prompts for `ADMIN_PASSWORD`, prints a
**one-time portal access token**, and creates `./secrets` (+ optional `./data`).

```bash
scripts/bootstrap.sh          # or: make bootstrap
```

It will NOT overwrite an existing `.env` (pass `--force` to regenerate — note
that rotates all secrets). **Copy the one-time token it prints** — only its hash
is stored.

Then drop your Google OAuth client JSON where compose expects it:

```bash
# DEPLOY-QNAP §3 explains how to create this in Google Cloud (read-only scopes).
cp /path/to/downloaded_client.json secrets/google_client_secret.json
chmod 600 secrets/google_client_secret.json
```

Sanity-check `.env` before first boot: confirm `POSTGRES_PASSWORD` (and keep
`DATABASE_URL` in sync), and set `TIMEZONE` (drives the off-hours browser window).

---

## 1. Build + start the stack

```bash
docker compose up -d --build         # or: make build && make up
docker compose ps                    # all three Up; db should be healthy
```

Services: **db** (Postgres 16), **worker** (ingestion CLI + scheduler), **portal**
(FastAPI + UI on host port **8080**). The worker entrypoint runs
`alembic upgrade head` on every boot.

---

## 2. Initialize the database + seed the admin user

```bash
docker compose run --rm worker init-db     # or: make init-db
```

Expect `migrations.upgrade ...` then `seed_admin.created username=<admin>` (or
`seed_admin.exists`). If you see `seed_admin.skipped reason=ADMIN_PASSWORD not
set`, fix `ADMIN_PASSWORD` in `.env` and re-run.

---

## 3. OAuth your accounts (3 Drive + 3 Gmail)

Drive and Gmail are authorized **separately** (one token per provider, even for
the same email). The helper loops over all of them:

```bash
scripts/oauth.sh                                   # prompts for the emails
# or non-interactive:
DRIVE_ACCOUNTS="d1@x.com d2@x.com d3@x.com" \
GMAIL_ACCOUNTS="g1@x.com g2@x.com g3@x.com" scripts/oauth.sh   # or: make oauth
```

**Headless flow:** each account prints a Google **consent URL**. Open it in a
browser, sign in **as the exact `--account` email**, grant read-only access, and
paste the returned **code** back at the prompt. On success Folio writes a
Fernet-encrypted token to `/data/tokens`.

Prefer to do them one at a time?

```bash
docker compose run --rm worker auth-drive --account d1@x.com   # make auth-drive ACCOUNT=d1@x.com
docker compose run --rm worker auth-gmail --account g1@x.com   # make auth-gmail ACCOUNT=g1@x.com
```

Verify tokens landed:

```bash
docker compose run --rm worker python -c "import os; print(sorted(os.listdir('/data/tokens')))"
```

---

## 4. First Drive crawl

The first run per account should be a **full** crawl; the scheduler then runs
incrementals automatically (every `SYNC_DRIVE_INTERVAL_MINUTES`, default 30).

```bash
docker compose run --rm worker sync-drive --full          # all accounts
# or per account:
docker compose run --rm worker sync-drive --account d1@x.com --full
# incremental shortcut:  make sync-drive ACCOUNT=d1@x.com
```

Watch progress:

```bash
docker compose logs -f worker      # or: make logs-worker
```

---

## 5. Gmail sender discovery

Gmail ingestion is allow-list based. Discover candidate senders, then enable the
ones you want in the portal **Senders** screen:

```bash
docker compose run --rm worker discover-senders           # all accounts
# or: make discover  (ACCOUNT=g1@x.com to scope it)
```

---

## 6. Open the portal

```
http://NAS-IP:8080            # locally: http://localhost:8080
```

Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` (or paste the one-time access
token from step 0). Health check (no auth): `GET /health` → `{"status":"ok"}`.

> Keep the portal LAN-only or behind a VPN — do not port-forward 8080 to the
> internet. If you serve it over HTTPS, set `SESSION_HTTPS_ONLY=true` in `.env`
> (see DEPLOY-QNAP §10).

---

## 7. Verify the deployment

```bash
scripts/verify.sh             # or: make verify
```

Checks db reachability, that migrations are at head, portal `/health` is 200,
and that the worker image imports cleanly. Non-zero exit on any failure.

---

## 8. Later: vendor-browser Gmail ingestion + assist + backups

The scheduler runs `sync-drive`, `discover-senders`, and `reconcile` on their
intervals automatically. The RAM-heavy **vendor-browser** Gmail ingestion
(`sync-gmail`) runs only when `BROWSER_ENABLED=true` AND inside the off-hours
window `[BROWSER_OFFHOURS_START, BROWSER_OFFHOURS_END)` (local `TIMEZONE`), one
job at a time. To run it on demand:

```bash
docker compose run --rm worker sync-gmail        # or: make sync-gmail
```

Emails Folio can't auto-ingest become **assist tasks**. List and resolve them:

```bash
docker compose run --rm worker assist-list                     # or: make assist-list
docker compose run --rm worker assist-resolve --id 42 --file /path/to/original.jpg
```

One-shot database backup (custom-format `pg_dump`, timestamped, with retention
pruning to `BACKUP_RETENTION_DAYS`):

```bash
docker compose run --rm backup                   # or: make backup
```

---

### Command cheat-sheet

| Action | Command |
| --- | --- |
| Bootstrap secrets + .env | `scripts/bootstrap.sh` / `make bootstrap` |
| Build + start | `docker compose up -d --build` / `make build && make up` |
| Init DB + seed admin | `docker compose run --rm worker init-db` / `make init-db` |
| OAuth all accounts | `scripts/oauth.sh` / `make oauth` |
| Full Drive sync | `docker compose run --rm worker sync-drive --full` |
| Discover senders | `docker compose run --rm worker discover-senders` / `make discover` |
| Vendor-browser Gmail sync | `docker compose run --rm worker sync-gmail` / `make sync-gmail` |
| List assist tasks | `docker compose run --rm worker assist-list` / `make assist-list` |
| DB backup | `docker compose run --rm backup` / `make backup` |
| Verify | `scripts/verify.sh` / `make verify` |
| Worker / portal logs | `make logs-worker` / `make logs-portal` |
| psql shell | `docker compose exec db psql -U folio -d folio` / `make psql` |
