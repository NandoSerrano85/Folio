#!/usr/bin/env bash
#
# Folio OAuth helper. Runs the per-account consent flow for your Drive and Gmail
# accounts (the worker `auth-drive` / `auth-gmail` commands), one at a time,
# handling the headless copy-the-URL / paste-the-code flow.
#
# Drive and Gmail are authorized SEPARATELY, even when the email is the same
# Google identity (Folio keeps a distinct token + account row per provider).
#
# Accounts can come from (in priority order):
#   1. --drive EMAIL / --gmail EMAIL flags (repeatable)
#   2. DRIVE_ACCOUNTS / GMAIL_ACCOUNTS env vars (space- or comma-separated)
#   3. an interactive prompt
#
# Usage:
#   scripts/oauth.sh                                   # prompt for all accounts
#   scripts/oauth.sh --drive d1@x.com --gmail g1@x.com # explicit (repeat flags)
#   DRIVE_ACCOUNTS="d1@x d2@x d3@x" GMAIL_ACCOUNTS="g1@x g2@x g3@x" scripts/oauth.sh
#   scripts/oauth.sh --only drive                      # Drive accounts only
#   scripts/oauth.sh --only gmail                      # Gmail accounts only
#
# Headless box (the NAS has no browser): each run prints a Google consent URL.
# Open it in a browser on your laptop/phone, sign in AS THE EXACT --account
# email, grant read-only access, then paste the returned code back at the prompt.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="${COMPOSE:-docker compose}"

say() { printf '\033[1;34m[oauth]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oauth]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[oauth] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

DRIVE_LIST=()
GMAIL_LIST=()
ONLY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --drive) shift; [ $# -gt 0 ] || die "--drive needs an email"; DRIVE_LIST+=("$1") ;;
    --gmail) shift; [ $# -gt 0 ] || die "--gmail needs an email"; GMAIL_LIST+=("$1") ;;
    --only) shift; ONLY="${1:-}"; [[ "$ONLY" == "drive" || "$ONLY" == "gmail" ]] || die "--only must be 'drive' or 'gmail'" ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# Split a comma/space-separated string into words, one per line on stdout.
split_words() {  # split_words "csv or space list"
  local raw="${1:-}"
  raw="${raw//,/ }"
  # shellcheck disable=SC2086
  for p in $raw; do [ -n "$p" ] && printf '%s\n' "$p"; done
}

# Fall back to env vars (comma OR space separated) when no flags were given.
if [ ${#DRIVE_LIST[@]} -eq 0 ] && [ -n "${DRIVE_ACCOUNTS:-}" ]; then
  while IFS= read -r e; do DRIVE_LIST+=("$e"); done < <(split_words "$DRIVE_ACCOUNTS")
fi
if [ ${#GMAIL_LIST[@]} -eq 0 ] && [ -n "${GMAIL_ACCOUNTS:-}" ]; then
  while IFS= read -r e; do GMAIL_LIST+=("$e"); done < <(split_words "$GMAIL_ACCOUNTS")
fi

# Interactive prompt as the final fallback.
if [ "$ONLY" != "gmail" ] && [ ${#DRIVE_LIST[@]} -eq 0 ] && [ -t 0 ]; then
  printf '[oauth] Enter Drive account emails (space-separated, blank to skip): '
  read -r line
  while IFS= read -r e; do DRIVE_LIST+=("$e"); done < <(split_words "$line")
fi
if [ "$ONLY" != "drive" ] && [ ${#GMAIL_LIST[@]} -eq 0 ] && [ -t 0 ]; then
  printf '[oauth] Enter Gmail account emails (space-separated, blank to skip): '
  read -r line
  while IFS= read -r e; do GMAIL_LIST+=("$e"); done < <(split_words "$line")
fi

if [ "$ONLY" = "drive" ]; then GMAIL_LIST=(); fi
if [ "$ONLY" = "gmail" ]; then DRIVE_LIST=(); fi

if [ ${#DRIVE_LIST[@]} -eq 0 ] && [ ${#GMAIL_LIST[@]} -eq 0 ]; then
  die "No accounts given. Use --drive/--gmail, DRIVE_ACCOUNTS/GMAIL_ACCOUNTS, or run interactively."
fi

OK=()
FAIL=()

run_auth() {  # run_auth <command> <email>
  local cmd="$1" email="$2"
  echo
  say "=== $cmd  $email ==="
  say "A Google consent URL will print below. Open it in a browser, sign in as"
  say "'$email', grant read-only access, then paste the code back here."
  echo
  if $COMPOSE run --rm worker "$cmd" --account "$email"; then
    OK+=("$cmd $email")
    say "OK: $cmd $email"
  else
    FAIL+=("$cmd $email")
    warn "FAILED: $cmd $email (you can re-run scripts/oauth.sh just for this one)."
  fi
}

for email in "${DRIVE_LIST[@]:-}"; do [ -n "$email" ] && run_auth auth-drive "$email"; done
for email in "${GMAIL_LIST[@]:-}"; do [ -n "$email" ] && run_auth auth-gmail "$email"; done

echo
say "Done. Succeeded: ${#OK[@]}, failed: ${#FAIL[@]}."
for s in "${OK[@]:-}"; do [ -n "$s" ] && printf '  \033[1;32mok  \033[0m %s\n' "$s"; done
for s in "${FAIL[@]:-}"; do [ -n "$s" ] && printf '  \033[1;31mfail\033[0m %s\n' "$s"; done

say "Verify tokens landed:"
say "  $COMPOSE run --rm worker python -c \"import os; print(sorted(os.listdir('/data/tokens')))\""

[ ${#FAIL[@]} -eq 0 ]
