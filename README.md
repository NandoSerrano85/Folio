# Folio

Folio is a self-hosted image-aggregation app for your NAS. It pulls images out
of the places they accumulate — Google Drive folders and vendor emails in Gmail
— deduplicates them, and presents one clean, date-sorted library you actually
own. It is built to run on a QNAP TBS-464 (Intel N5105, 8 GB RAM, all-NVMe).

## Why the date matters

When you download a photo from an email or Drive, the file's timestamps reflect
*today* — the day you saved it — not the day the picture was taken or sent. Open
that in Apple Photos and your memories scatter to the bottom of the timeline.

Folio treats the **source date** as authoritative:

- For email images, it's the message's `Date` header.
- For Drive files, it's the Drive `createdTime`.

That date is stored as `images.source_date` (the default sort key for the whole
library) **and** stamped into the file's EXIF `DateTimeOriginal` / `CreateDate`
via `exiftool`, so the correct date travels with the file everywhere it goes.
Image identity (`sha256`) is computed on the **original bytes before stamping**,
so re-stamping never breaks dedup.

## Architecture

```
                       +--------------------+
                       |      Postgres      |
                       |        (db)        |
                       |  source of record  |
                       +----------+---------+
                                  ^
                  SQLAlchemy 2.0  |  (folio_core.models)
                +-----------------+------------------+
                |                                    |
      +---------+----------+              +----------+---------+
      |       worker       |              |       portal       |
      |  ingestion + APS   |              |  FastAPI JSON API  |
      |  scheduler         |              |  + vanilla-JS UI   |
      |                    |              |                    |
      | Drive/Gmail OAuth  |              | login (argon2id)   |
      | (read-only)        |              | images/folders/... |
      | exiftool stamping  |              | thumbnails (Pillow)|
      +----+----------+----+              +----+----------+----+
           | rw       | rw                     | ro       | rw
           v          v                        v          v
      +---------+ +---------+             +---------+ +-----------+
      |  media  | | tokens  |  (shared)   |  media  | |thumbnails |
      | volume  | | (Fernet)| <---------> | volume  | |  volume   |
      +---------+ +---------+             +---------+ +-----------+
```

All three services share the **`folio_core`** package (config, db, models,
hashing, exif, paths, crypto, logging). It is pip-installed into both the worker
and portal images, so there is exactly one definition of the data model.

### The three services

- **db** — Postgres 16. The single source of record. Schema is owned by Alembic
  migrations in `folio_core`.
- **worker** — Long-running container. Applies migrations on boot, runs the
  ingestion CLI (Drive sync, Gmail sender discovery, reconciliation), and hosts
  an in-container APScheduler loop (not system cron). OAuth refresh tokens are
  stored **encrypted** on disk (Fernet).
- **portal** — FastAPI + Jinja2 shell serving a vanilla-JS frontend against a
  JSON API. Session-cookie login with argon2id password hashing. Serves original
  bytes (Range-aware) and on-demand Pillow thumbnails.

## Tech stack

Python 3.12 · SQLAlchemy 2.0 (typed `Mapped[]`) · psycopg 3 · Postgres 16 ·
Alembic · pydantic v2 + pydantic-settings · FastAPI + uvicorn · Jinja2 ·
argon2-cffi · Pillow · APScheduler · tenacity · exiftool · Google API client
libraries (read-only Drive + Gmail). No Node build step.

## Dev quickstart

```bash
# 1. Configure
cp .env.example .env
# Generate the two required secrets and paste them into .env:
python -c "from cryptography.fernet import Fernet; print('FERNET_KEY=' + Fernet.generate_key().decode())"
python -c "import secrets; print('PORTAL_SECRET_KEY=' + secrets.token_urlsafe(48))"
# Set ADMIN_PASSWORD too.

# 2. Drop your Google OAuth client secrets here:
#    ./secrets/google_client_secret.json   (installed-app credentials)

# 3. Build and migrate
make build
make init-db            # alembic upgrade head + seed admin user

# 4. Connect accounts (interactive OAuth consent)
make auth-drive ACCOUNT=you@example.com
make auth-gmail ACCOUNT=you@example.com

# 5. Run the stack
make up                 # worker scheduler + portal on http://localhost:8080
make logs
```

Useful targets: `make sync-drive`, `make discover`, `make reconcile`,
`make psql`, `make down`. Run `make help` for the full list.

## Deployment

For QNAP TBS-464 deployment (Container Station, volume mapping, memory caps,
reverse proxy), see **[DEPLOY-QNAP.md](DEPLOY-QNAP.md)** (maintained separately).

## Repository layout

```
packages/folio_core   # shared spine (installed into both services)
services/worker       # ingestion CLI + scheduler
services/portal       # FastAPI JSON API + UI shell
docker-compose.yml    # the three-service stack
```
