#!/usr/bin/env python3
"""Mint (or rotate) a Folio portal access token.

Prints a fresh secret token and the matching ``ACCESS_TOKEN_HASH=`` line for
``.env``. The plaintext token is shown ONCE: copy it into the portal login.
Only the argon2id hash is ever stored on the server.

Usage::

    python services/portal/scripts/make_token.py

Then:
  1. Paste the ACCESS_TOKEN_HASH line into your .env (replacing any old value).
  2. Keep the printed TOKEN secret and paste it into the portal to log in.
  3. Restart the portal so the new hash is loaded.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher


def main() -> None:
    token = "folio_sk_live_" + secrets.token_urlsafe(24)
    token_hash = PasswordHasher().hash(token)
    print("TOKEN:", token)
    print("ACCESS_TOKEN_HASH=" + token_hash)


if __name__ == "__main__":
    main()
