#!/usr/bin/env bash
#
# restore.sh — restore a Folio Postgres backup produced by `worker backup-db`.
#
# Backup model (see docs/OPERATIONS.md "Backups & restore"):
#   * `docker compose run --rm backup`  (or the `backup-db` worker verb) runs
#     `pg_dump --format=custom` and writes  folio-<UTC-timestamp>.dump  into the
#     `backups` named volume, mounted at /data/backups inside the worker/backup
#     containers. It then prunes archives older than BACKUP_RETENTION_DAYS
#     (default 14).
#   * Because the dump is CUSTOM format (-Fc), it is restored with `pg_restore`
#     (NOT `psql`). This script streams a chosen dump into `pg_restore` running
#     inside the live `db` container.
#
# This is DESTRUCTIVE: `--clean --if-exists` drops existing objects before
# recreating them. You are prompted to confirm by typing the database name.
#
# Usage:
#   scripts/restore.sh --list                 # list dumps in the backups volume
#   scripts/restore.sh <name>                 # restore a dump from the volume
#                                             #   e.g. folio-20260627T010000Z.dump
#   scripts/restore.sh /path/on/host.dump     # restore a dump file on the host
#   scripts/restore.sh --yes <name|path>      # skip the interactive confirmation
#
set -euo pipefail

# ---------------------------------------------------------------------------- #
# Locate the repo root (this script lives in <repo>/scripts) so `docker compose`
# finds docker-compose.yml regardless of where the script is invoked from.
# ---------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd -P)"
cd "$REPO_ROOT"

# Load DB name/user from .env if present, else fall back to the compose defaults.
PG_USER="folio"
PG_DB="folio"
if [[ -f .env ]]; then
  # Read ONLY the two keys we need — do NOT `source` .env. Sourcing would run
  # shell expansion/command-substitution on every value (e.g. ACCESS_TOKEN_HASH
  # = $argon2id$... would be mangled, and any $(...) would execute).
  env_get() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
  v="$(env_get POSTGRES_USER)"; [[ -n "$v" ]] && PG_USER="$v"
  v="$(env_get POSTGRES_DB)"; [[ -n "$v" ]] && PG_DB="$v"
fi

# `docker compose` (v2) vs legacy `docker-compose`.
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "error: neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

VOLUME_DIR="/data/backups"

list_dumps() {
  # List dumps from the backups volume via a throwaway container that mounts it.
  # `--no-deps --entrypoint sh` avoids running the worker's alembic entrypoint.
  echo "Dumps in the 'backups' volume ($VOLUME_DIR):"
  "${DC[@]}" run --rm --no-deps --entrypoint sh backup -c \
    "ls -1t $VOLUME_DIR/folio-*.dump 2>/dev/null || echo '  (none found)'"
}

# ---------------------------------------------------------------------------- #
# Argument parsing
# ---------------------------------------------------------------------------- #
ASSUME_YES=0
TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      list_dumps
      exit 0
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    -*)
      echo "error: unknown option '$1'" >&2
      exit 2
      ;;
    *)
      if [[ -n "$TARGET" ]]; then
        echo "error: only one dump may be specified (got '$TARGET' and '$1')." >&2
        exit 2
      fi
      TARGET="$1"
      shift
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "error: no dump specified." >&2
  echo "Run 'scripts/restore.sh --list' to see available dumps, then:" >&2
  echo "  scripts/restore.sh <dump-name-or-host-path>" >&2
  exit 2
fi

# ---------------------------------------------------------------------------- #
# Decide the dump source: a readable file on the host, or a name in the volume.
# In both cases we produce a single command that emits the dump bytes to stdout,
# which we then pipe into pg_restore inside the db container.
# ---------------------------------------------------------------------------- #
if [[ -f "$TARGET" ]]; then
  SOURCE_DESC="host file: $TARGET"
  emit_dump() { cat -- "$TARGET"; }
else
  # Treat as a name inside the backups volume. Reject path components so the
  # name can only address a file directly under $VOLUME_DIR.
  NAME="$(basename -- "$TARGET")"
  if [[ "$NAME" != "$TARGET" ]]; then
    echo "error: '$TARGET' is not a host file and contains path separators;" >&2
    echo "       pass just the dump filename (see --list) or a full host path." >&2
    exit 2
  fi
  # Verify it exists in the volume before we start tearing the DB down.
  if ! "${DC[@]}" run --rm --no-deps --entrypoint sh backup -c \
        "test -f $VOLUME_DIR/$NAME" >/dev/null 2>&1; then
    echo "error: '$NAME' not found in the backups volume." >&2
    echo "Run 'scripts/restore.sh --list' to see available dumps." >&2
    exit 1
  fi
  SOURCE_DESC="backups volume: $VOLUME_DIR/$NAME"
  emit_dump() {
    "${DC[@]}" run --rm --no-deps -T --entrypoint sh backup -c \
      "cat $VOLUME_DIR/$NAME"
  }
fi

# ---------------------------------------------------------------------------- #
# Confirm — this overwrites the live database.
# ---------------------------------------------------------------------------- #
cat <<EOF

  About to RESTORE into the running Folio database.

    Source : $SOURCE_DESC
    Target : database '$PG_DB' as user '$PG_USER' (db container)
    Mode   : pg_restore --clean --if-exists  (DROPS then recreates objects)

  This OVERWRITES the current contents of '$PG_DB'. Connected clients (the
  portal/worker) may error during the restore; consider stopping them first:
    ${DC[*]} stop portal worker

EOF

if [[ "$ASSUME_YES" -ne 1 ]]; then
  printf "  Type the database name (%s) to proceed: " "$PG_DB"
  read -r reply
  if [[ "$reply" != "$PG_DB" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

# The db container must be up to receive the restore.
if ! "${DC[@]}" ps --status running db 2>/dev/null | grep -q db; then
  echo "error: the 'db' service is not running. Start it first: ${DC[*]} up -d db" >&2
  exit 1
fi

echo "Restoring… (errors about non-existent objects on the first --clean pass are normal)"

# Stream the dump into pg_restore inside the db container. `-T` keeps stdin open;
# the db service name resolves inside the compose network. We restore over a
# local socket as the superuser role, so no password/URL is placed on argv.
#   --clean --if-exists : drop existing objects (quietly if absent) before load
#   --no-owner          : don't fail if the dumping role differs from this one
#   --exit-on-error off : let pg_restore continue past the harmless DROP misses
set +e
emit_dump | "${DC[@]}" exec -T db \
  pg_restore --clean --if-exists --no-owner -U "$PG_USER" -d "$PG_DB"
status=${PIPESTATUS[1]}
set -e

if [[ "$status" -ne 0 ]]; then
  echo "" >&2
  echo "pg_restore exited with status $status. If the only messages were" >&2
  echo "'... does not exist, skipping' from the --clean pass, the data still" >&2
  echo "loaded; otherwise review the output above." >&2
  exit "$status"
fi

echo "Restore complete. If you stopped them, restart services: ${DC[*]} up -d"
