"""Headless-friendly Google OAuth consent for installed (Desktop) clients.

Why this exists: on a NAS the browser runs on the operator's laptop, not in the
container. ``flow.run_local_server`` starts a callback server *inside the
container* and redirects to ``http://localhost:<port>`` — which, from the
laptop, is the laptop, so the redirect can never arrive and the flow hangs. The
old out-of-band (``urn:ietf:wg:oauth:2.0:oob``) flow that would have worked was
removed by Google in 2022.

The robust pattern is a manual exchange with a **loopback redirect** (which
Desktop OAuth clients accept implicitly for any localhost/127.0.0.1 port):
print the consent URL, the operator authorizes in any browser, the browser then
fails to load ``http://localhost:8765/?code=...`` (expected — nothing is
listening), the operator copies that URL (or just the ``code`` value) and pastes
it back, and we exchange it for tokens.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from folio_core.logging import get_logger

logger = get_logger("worker.google_oauth")

# Loopback redirect — Desktop clients accept any http://localhost[:port]; no
# server actually runs here, the operator copies the code out of the URL.
LOOPBACK_REDIRECT = "http://localhost:8765/"


def run_console_consent(flow, *, label: str) -> object:
    """Drive an installed-app OAuth consent via manual code paste; return creds.

    ``flow`` is a configured ``google_auth_oauthlib.flow.InstalledAppFlow``.
    ``label`` is shown in the prompt (e.g. ``"Drive"`` / ``"Gmail"``).
    """
    flow.redirect_uri = LOOPBACK_REDIRECT
    auth_url, _state = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )
    print(
        f"\n=== Folio {label} authorization (READ-ONLY) ===\n\n"
        "1) Open this URL in any browser, sign in as the account you are "
        "connecting, and grant access:\n\n"
        f"{auth_url}\n\n"
        "2) Your browser will then try to load a page like\n"
        "     http://localhost:8765/?code=...&scope=...\n"
        '   and show "this site can’t be reached". THAT IS EXPECTED.\n\n'
        "3) Copy the FULL address from the browser’s address bar (or just "
        "the value of the code= parameter) and paste it below.\n",
        flush=True,
    )
    raw = input("Paste the redirect URL (or the code): ").strip()
    code = _extract_code(raw)
    if not code:
        raise RuntimeError(
            "No authorization code found in the pasted value. Paste the full "
            "http://localhost:8765/?code=... URL, or just the code= value."
        )
    flow.fetch_token(code=code)
    return flow.credentials


def _extract_code(raw: str) -> str | None:
    """Accept either a bare code or a full redirect URL; return the code."""
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        params = parse_qs(urlparse(raw).query)
        values = params.get("code")
        return values[0] if values else None
    return raw


__all__ = ["run_console_consent", "LOOPBACK_REDIRECT"]
