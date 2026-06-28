# Folio portal image.
#
# Serves the FastAPI JSON API + the static vanilla-JS frontend. The database is
# migrated by the worker; the portal only ensures the admin user exists on boot.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Pillow runtime + build deps for psycopg/Pillow wheels if needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the shared spine first for layer caching.
COPY packages/folio_core /opt/folio_core
RUN pip install /opt/folio_core

# Portal-specific deps.
COPY services/portal/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Portal source.
COPY services/portal/ /app/

EXPOSE 8080
CMD ["uvicorn", "portal.main:app", "--host", "0.0.0.0", "--port", "8080"]
