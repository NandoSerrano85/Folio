#!/usr/bin/env bash
# Worker entrypoint: migrate the database, then dispatch the CLI.
#
# Alembic is the single source of DB structure. We run it against folio_core's
# config (FOLIO_ALEMBIC_INI points at the installed alembic.ini whose env.py
# pulls the URL + metadata from folio_core).
set -euo pipefail

ALEMBIC_INI="${FOLIO_ALEMBIC_INI:-/opt/folio_core/alembic.ini}"

echo "[entrypoint] applying database migrations (alembic upgrade head)..."
alembic -c "${ALEMBIC_INI}" upgrade head
echo "[entrypoint] migrations applied."

echo "[entrypoint] launching: python -m worker.main $*"
exec python -m worker.main "$@"
