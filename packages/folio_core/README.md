# folio_core

Shared, pip-installable spine for the Folio NAS image-aggregation app.

Both the **worker** and the **portal** depend on this package. It is the single
source of truth for:

- `config`   — pydantic-settings `Settings` loaded from env / `.env`
- `db`       — SQLAlchemy 2.0 engine, session factory, `Base`
- `models`   — all ORM models (the DB contract)
- `hashing`  — sha256 of original bytes (image identity)
- `exif`     — exiftool stamping of the authoritative source date
- `paths`    — deterministic MEDIA_ROOT-relative storage paths
- `crypto`   — Fernet encryption of OAuth refresh tokens
- `logging`  — structured logging honoring `LOG_LEVEL`

Alembic migrations live under `migrations/` and use `models.Base.metadata`
as their target. The worker runs `alembic upgrade head` on boot.

Do **not** duplicate models in the services — always import from here.
