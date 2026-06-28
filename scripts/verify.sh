#!/usr/bin/env bash
#
# Folio post-deploy health checks. Run AFTER `docker compose up -d` (and ideally
# after `init-db`). Each check prints PASS/FAIL; the script exits non-zero if any
# check fails.
#
# Checks:
#   1. db reachable        — pg_isready inside the db container
#   2. migrations at head  — `alembic current` reports the head revision
#   3. portal /health      — HTTP 200 with {"status":"ok"}
#   4. worker imports       — `python -c "import worker.main"` in the worker image
#
# Usage:
#   scripts/verify.sh
#   PORTAL_URL=http://192.168.1.50:8080 scripts/verify.sh
#   COMPOSE="docker-compose" scripts/verify.sh    # legacy compose binary
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="${COMPOSE:-docker compose}"
PORTAL_URL="${PORTAL_URL:-http://localhost:8080}"

# Pull POSTGRES_USER / POSTGRES_DB out of .env (defaults match .env.example).
env_get() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
PGUSER="$(env_get POSTGRES_USER)"; PGUSER="${PGUSER:-folio}"
PGDB="$(env_get POSTGRES_DB)"; PGDB="${PGDB:-folio}"

green() { printf '\033[1;32m%s\033[0m' "$*"; }
red() { printf '\033[1;31m%s\033[0m' "$*"; }
say() { printf '\033[1;34m[verify]\033[0m %s\n' "$*"; }

RESULTS=()   # "name|PASS|detail" rows for the summary table
FAILED=0

record() {  # record <name> <PASS|FAIL> <detail>
  RESULTS+=("$1|$2|$3")
  if [ "$2" = "PASS" ]; then
    printf '  [%s] %-22s %s\n' "$(green PASS)" "$1" "$3"
  else
    printf '  [%s] %-22s %s\n' "$(red FAIL)" "$1" "$3"
    FAILED=1
  fi
}

say "Folio health checks (compose='$COMPOSE', portal=$PORTAL_URL)"
echo

# --------------------------------------------------------------------------- #
# 1. Database reachable
# --------------------------------------------------------------------------- #
if $COMPOSE exec -T db pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1; then
  record "db-reachable" PASS "pg_isready OK ($PGUSER@$PGDB)"
else
  record "db-reachable" FAIL "pg_isready failed — is the db container up & healthy?"
fi

# --------------------------------------------------------------------------- #
# 2. Migrations at head (alembic current ends with '(head)')
# --------------------------------------------------------------------------- #
# Override the entrypoint so we read state WITHOUT triggering an upgrade.
ALEMBIC_OUT="$(
  $COMPOSE run --rm --entrypoint sh worker -c \
    'alembic -c "${FOLIO_ALEMBIC_INI:-/opt/folio_core/alembic.ini}" current 2>&1' 2>/dev/null || true
)"
CURRENT_REV="$(printf '%s\n' "$ALEMBIC_OUT" | grep -E '\(head\)' | tail -n1 | awk '{print $1}')"
if [ -n "$CURRENT_REV" ]; then
  record "migrations-head" PASS "at head: $CURRENT_REV"
else
  LAST="$(printf '%s\n' "$ALEMBIC_OUT" | grep -vE '^\s*$' | tail -n1)"
  record "migrations-head" FAIL "not at head (run 'make init-db'). last: ${LAST:-<none>}"
fi

# --------------------------------------------------------------------------- #
# 3. Portal /health -> 200 {"status":"ok"}
# --------------------------------------------------------------------------- #
if command -v curl >/dev/null 2>&1; then
  # curl prints the status code via -w; on a connection failure that is '000'.
  # '|| true' keeps `set -e` happy without appending a second code.
  CODE="$(curl -s -o /tmp/folio_health.$$ -w '%{http_code}' "$PORTAL_URL/health" 2>/dev/null || true)"
  CODE="${CODE:-000}"
  BODY="$(cat /tmp/folio_health.$$ 2>/dev/null || true)"; rm -f /tmp/folio_health.$$
  if [ "$CODE" = "200" ] && printf '%s' "$BODY" | grep -q '"status"'; then
    record "portal-health" PASS "HTTP 200 $BODY"
  else
    record "portal-health" FAIL "HTTP $CODE (expected 200) from $PORTAL_URL/health"
  fi
else
  record "portal-health" FAIL "curl not installed; cannot probe $PORTAL_URL/health"
fi

# --------------------------------------------------------------------------- #
# 4. Worker image imports cleanly
# --------------------------------------------------------------------------- #
if $COMPOSE run --rm --entrypoint python worker -c "import worker.main" >/dev/null 2>&1; then
  record "worker-imports" PASS "import worker.main OK"
else
  record "worker-imports" FAIL "import worker.main raised (check 'docker compose build worker')"
fi

# --------------------------------------------------------------------------- #
# Summary table
# --------------------------------------------------------------------------- #
echo
say "Summary"
printf '  %-22s %-6s %s\n' "CHECK" "RESULT" "DETAIL"
printf '  %-22s %-6s %s\n' "----------------------" "------" "----------------------------------"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r name status detail <<<"$row"
  printf '  %-22s %-6s %s\n' "$name" "$status" "$detail"
done
echo

if [ "$FAILED" -ne 0 ]; then
  say "$(red 'One or more checks FAILED.')"
  exit 1
fi
say "$(green 'All checks passed.')"
