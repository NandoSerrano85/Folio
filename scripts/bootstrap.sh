#!/usr/bin/env bash
#
# Folio first-run bootstrap. Idempotent: safe to re-run.
#
# What it does:
#   1. Generates the required secrets using Folio's own helpers/one-liners:
#        - FERNET_KEY        (cryptography.fernet.Fernet — encrypts OAuth tokens)
#        - PORTAL_SECRET_KEY (secrets.token_urlsafe — signs the session cookie)
#        - ACCESS_TOKEN_HASH (services/portal/scripts/make_token.py — argon2id)
#      and prints the matching ONE-TIME access token (shown once; paste it into
#      the portal to log in — it is NEVER written to disk).
#   2. Scaffolds .env from .env.example, fills the generated secrets, and
#      prompts for ADMIN_PASSWORD.
#   3. Creates host directories used by the stack (./secrets bind mount, and the
#      optional ./data/* scaffold for bind-mount deployments — DEPLOY-QNAP §2).
#   4. Prints next steps.
#
# It will NOT overwrite an existing .env unless you pass --force.
#
# Usage:
#   scripts/bootstrap.sh            # first run
#   scripts/bootstrap.sh --force    # regenerate .env (rotates all secrets!)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "bootstrap: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[bootstrap] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 0. Sanity
# --------------------------------------------------------------------------- #
[ -f "$ROOT/.env.example" ] || die ".env.example not found in $ROOT (run from the repo)."

if [ -f "$ROOT/.env" ] && [ "$FORCE" -ne 1 ]; then
  say ".env already exists — leaving it untouched (re-run with --force to regenerate)."
  ENV_SKIPPED=1
else
  ENV_SKIPPED=0
fi

# --------------------------------------------------------------------------- #
# 1. Pick a Python that can produce the secrets (cryptography + argon2)
# --------------------------------------------------------------------------- #
# Prefer a local python3 that already has the libs; otherwise fall back to a
# throwaway python:3.12-slim container (needs network for pip the first time).
USE_DOCKER=0
if [ "$ENV_SKIPPED" -eq 0 ]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c "import cryptography, argon2" >/dev/null 2>&1; then
    say "Using local python3 (cryptography + argon2 available)."
  elif command -v docker >/dev/null 2>&1; then
    USE_DOCKER=1
    warn "Local python3 lacks cryptography/argon2; using a throwaway python:3.12-slim container (needs network)."
  else
    die "Need either a python3 with 'cryptography' + 'argon2-cffi', or Docker. Install one and re-run."
  fi
fi

# pyrun: read a Python program on stdin and execute it with cwd = repo root.
# The program is written to a 0600 temp file and the ADMIN_PASSWORD is piped on
# stdin (read by the program). The password is NEVER passed via `-e`/argv, which
# `docker inspect`/`ps` would expose.
pyrun() {
  local prog rc=0
  prog="$(mktemp "$ROOT/.bootstrap-gen.XXXXXX.py")"
  chmod 600 "$prog" 2>/dev/null || true
  cat > "$prog"
  if [ "$USE_DOCKER" -eq 1 ]; then
    printf '%s' "${ADMIN_PASSWORD:-}" | docker run --rm -i -v "$ROOT:/repo" -w /repo python:3.12-slim \
      sh -c "pip install --quiet cryptography argon2-cffi >/dev/null 2>&1 && exec python '/repo/$(basename "$prog")'" || rc=$?
  else
    printf '%s' "${ADMIN_PASSWORD:-}" | python3 "$prog" || rc=$?
  fi
  rm -f "$prog"
  return "$rc"
}

# --------------------------------------------------------------------------- #
# 2. Generate .env (with prompt for ADMIN_PASSWORD)
# --------------------------------------------------------------------------- #
ONE_TIME_TOKEN=""
if [ "$ENV_SKIPPED" -eq 0 ]; then
  # Prompt for the admin password (hidden, confirmed).
  ADMIN_PASSWORD=""
  if [ -t 0 ]; then
    while :; do
      printf '[bootstrap] Set the portal ADMIN_PASSWORD (input hidden): '
      read -r -s p1; echo
      printf '[bootstrap] Confirm ADMIN_PASSWORD: '
      read -r -s p2; echo
      if [ -z "$p1" ]; then warn "Password cannot be empty (it seeds the admin user)."; continue; fi
      if [ "$p1" != "$p2" ]; then warn "Passwords did not match — try again."; continue; fi
      ADMIN_PASSWORD="$p1"; break
    done
  else
    warn "Non-interactive shell: leaving ADMIN_PASSWORD blank in .env — set it before 'make init-db'."
  fi
  export ADMIN_PASSWORD

  say "Generating secrets and writing .env ..."
  # The Python program writes .env and prints ONLY the one-time token on stdout.
  ONE_TIME_TOKEN="$(pyrun <<'PY'
import os, re, secrets, sys
from pathlib import Path

from cryptography.fernet import Fernet
from argon2 import PasswordHasher

example = Path(".env.example")
target = Path(".env")
text = example.read_text()

fernet_key = Fernet.generate_key().decode()
portal_secret = secrets.token_urlsafe(48)
# Mirrors services/portal/scripts/make_token.py exactly.
token = "folio_sk_live_" + secrets.token_urlsafe(24)
token_hash = PasswordHasher().hash(token)
admin_password = sys.stdin.read()  # piped by pyrun; never on argv/env

def set_key(t: str, key: str, value: str) -> str:
    pat = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    # Use a function replacement so '$', '\\', etc. in the value are literal.
    if pat.search(t):
        return pat.sub(lambda _m: f"{key}={value}", t)
    return t.rstrip("\n") + f"\n{key}={value}\n"

text = set_key(text, "FERNET_KEY", fernet_key)
text = set_key(text, "PORTAL_SECRET_KEY", portal_secret)
text = set_key(text, "ACCESS_TOKEN_HASH", token_hash)
if admin_password:
    text = set_key(text, "ADMIN_PASSWORD", admin_password)

target.write_text(text)
try:
    os.chmod(target, 0o600)
except OSError:
    pass

# Diagnostics to stderr; the token (and ONLY the token) to stdout.
sys.stderr.write("[bootstrap] wrote .env (FERNET_KEY, PORTAL_SECRET_KEY, ACCESS_TOKEN_HASH set)\n")
sys.stdout.write(token)
PY
)"
  [ -n "$ONE_TIME_TOKEN" ] || die "Secret generation failed (no token returned)."
  chmod 600 "$ROOT/.env" 2>/dev/null || true
  say ".env created and locked to 0600."
fi

# --------------------------------------------------------------------------- #
# 3. Host directories
# --------------------------------------------------------------------------- #
# Required: ./secrets holds the Google OAuth client JSON (compose bind mount
# ./secrets:/data/secrets:ro). The image library / tokens / thumbnails / db use
# named Docker volumes by default (nothing to create). The ./data/* dirs below
# are only needed if you switch compose to bind mounts (DEPLOY-QNAP §2, Opt. B).
say "Creating host directories ..."
mkdir -p "$ROOT/secrets"
chmod 700 "$ROOT/secrets" 2>/dev/null || true
mkdir -p "$ROOT/data/media" "$ROOT/data/thumbnails" "$ROOT/data/tokens" "$ROOT/data/backups"
say "Ready: ./secrets (place google_client_secret.json here) and ./data/* (optional, for bind mounts)."

# --------------------------------------------------------------------------- #
# 4. Next steps
# --------------------------------------------------------------------------- #
echo
say "Bootstrap complete."
echo
if [ -n "$ONE_TIME_TOKEN" ]; then
  printf '\033[1;32m================ ONE-TIME PORTAL ACCESS TOKEN (shown once) ================\033[0m\n'
  printf '    %s\n' "$ONE_TIME_TOKEN"
  printf '\033[1;32m===========================================================================\033[0m\n'
  echo "  Copy it now. Only its argon2 hash is stored (ACCESS_TOKEN_HASH in .env)."
  echo "  Paste it into the portal login, or just use ADMIN_USERNAME/ADMIN_PASSWORD."
  echo
fi
cat <<EOF
Next steps:
  1. Put your Google OAuth client JSON at:
       $ROOT/secrets/google_client_secret.json   (chmod 600)
     (See DEPLOY-QNAP.md §3 for creating it — read-only Gmail + Drive scopes.)
  2. Review .env (POSTGRES_PASSWORD, TIMEZONE, intervals) — chmod 600 already set.
  3. Build + start the stack:
       docker compose up -d --build      # or: make build && make up
  4. Initialize the database + seed the admin user:
       docker compose run --rm worker init-db    # or: make init-db
  5. OAuth your accounts (3 Drive + 3 Gmail):
       scripts/oauth.sh                  # or: make oauth
  6. Verify the deployment:
       scripts/verify.sh                 # or: make verify
  7. First Drive crawl + sender discovery:
       docker compose run --rm worker sync-drive --full
       docker compose run --rm worker discover-senders
  8. Open the portal:  http://NAS-IP:8899   (PORTAL_PORT; QTS uses 8080)

See docs/QUICKSTART.md for the full copy-paste walkthrough.
EOF
