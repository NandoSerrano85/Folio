# Folio worker image.
#
# Runs the ingestion CLI + APScheduler loop. On boot the entrypoint applies
# Alembic migrations, then execs the requested command (default: schedule).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Path to folio_core's alembic.ini inside the image (used by entrypoint).
    FOLIO_ALEMBIC_INI=/opt/folio_core/alembic.ini

# System deps:
# - libimage-exiftool-perl: EXIF stamping (folio_core.exif)
# - build-essential + libpq-dev: building psycopg / Pillow wheels if needed
# - libjpeg/zlib: Pillow runtime
# - postgresql-client: pg_dump for `worker backup-db` (DB backups)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        build-essential \
        libpq-dev \
        libjpeg62-turbo \
        zlib1g \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared spine first (better layer caching). Keep the source tree at
# /opt/folio_core so alembic.ini + migrations are available at runtime.
COPY packages/folio_core /opt/folio_core
RUN pip install /opt/folio_core

# Worker-specific deps.
COPY services/worker/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Headless Chromium for the vendor-browser ingestion (Playwright).
#
# RAM NOTE: Chromium is the heaviest thing this image runs. On the 8 GB QNAP it
# is gated to ONE job at a time, off-hours (folio_core.config browser_* / the
# scheduler in main.py), with the worker mem_limit held at 2.5g and
# --disable-dev-shm-usage in code (compose also sets shm_size: 512mb).
#
# `--with-deps` pulls the required OS libraries (fonts, libnss, libatk, ...)
# and installs only the chromium browser build to keep the image lean.
RUN python -m playwright install --with-deps chromium

# Worker source.
COPY services/worker/ /app/
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["schedule"]
