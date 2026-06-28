"""Gmail OAuth (installed-app flow) + authorized service builder.

``run_gmail_auth`` drives the one-time consent flow for an account, persists the
resulting credentials ENCRYPTED on disk (Fernet, via ``folio_core.crypto``), and
upserts the matching ``accounts`` row (``provider='gmail'``).

``build_gmail_service`` reconstructs an authorized ``gmail`` API client from the
stored token, transparently refreshing (and re-persisting) it when expired. It
is the single entry point used by :mod:`worker.gmail.discover` and
:mod:`worker.gmail.sync`.

Scope policy: READ-ONLY. Only ``gmail.readonly`` is ever requested.

All Google client imports are performed lazily inside the functions so that
importing this module never requires the Google libraries to be present.
"""

from __future__ import annotations

import json

from folio_core.config import get_settings
from folio_core.crypto import load_token, save_token
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import Account, ProviderEnum

logger = get_logger("worker.gmail.auth")

# The only Gmail scope Folio ever requests. READ-ONLY by policy.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def gmail_scopes() -> list[str]:
    """Return the Gmail scope(s) to request.

    Derived from the configured ``GOOGLE_OAUTH_SCOPES`` (filtered to Gmail) so
    the operator stays in control, but always falls back to the read-only scope.
    """
    configured = [s for s in get_settings().scopes_list if "gmail" in s]
    return configured or [GMAIL_SCOPE]


def token_ref(account_email: str) -> str:
    """Token-store reference for a Gmail account.

    Provider-prefixed so a single Google identity used for *both* Gmail and
    Drive keeps two distinct encrypted tokens on disk (the accounts table also
    holds two rows, one per provider).
    """
    return f"gmail-{account_email.strip().lower()}"


# --------------------------------------------------------------------------- #
# Consent flow
# --------------------------------------------------------------------------- #
def run_gmail_auth(account_email: str) -> None:
    """Run the installed-app OAuth consent flow for ``account_email``.

    On success the encrypted credentials are written under ``TOKEN_DIR`` and an
    ``accounts`` row (provider='gmail') is created/updated with ``status='active'``
    and ``token_ref`` pointing at the stored token.
    """
    account_email = account_email.strip().lower()
    settings = get_settings()
    secrets_file = settings.google_client_secrets_file

    if not secrets_file.exists():
        raise FileNotFoundError(
            "Google OAuth client secrets not found at "
            f"{secrets_file}. Provide an installed-app client_secret.json "
            "(mounted read-only into the worker) before running auth-gmail."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = gmail_scopes()
    logger.info("gmail.auth.start account=%s scopes=%s", account_email, scopes)

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes=scopes)
    # ``run_local_server`` is the supported installed-app flow (run_console was
    # removed upstream). On a headless NAS the operator copies the printed URL
    # into a browser; the loopback redirect completes the handshake.
    creds = flow.run_local_server(
        port=0,
        open_browser=False,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=(
            "Open this URL to authorize Folio (Gmail, read-only) for "
            f"{account_email}:\n{{url}}"
        ),
    )

    if not creds.refresh_token:
        logger.warning(
            "gmail.auth.no_refresh_token account=%s — re-run with a fresh "
            "consent (the account may have an existing grant).",
            account_email,
        )

    ref = token_ref(account_email)
    save_token(ref, creds.to_json())
    _upsert_gmail_account(account_email, ref)
    logger.info("gmail.auth.ok account=%s token_ref=%s", account_email, ref)


def _upsert_gmail_account(account_email: str, ref: str) -> None:
    """Create or update the ``accounts`` row for this Gmail account."""
    from sqlalchemy import select

    with session_scope() as session:
        account = session.scalar(
            select(Account).where(
                Account.provider == ProviderEnum.gmail,
                Account.email == account_email,
            )
        )
        if account is None:
            account = Account(
                provider=ProviderEnum.gmail,
                email=account_email,
                status="active",
                token_ref=ref,
            )
            session.add(account)
            logger.info("gmail.auth.account_created email=%s", account_email)
        else:
            account.status = "active"
            account.token_ref = ref
            logger.info("gmail.auth.account_updated email=%s", account_email)


# --------------------------------------------------------------------------- #
# Authorized service builder (used by discover/sync)
# --------------------------------------------------------------------------- #
def load_credentials(account_email: str):
    """Load + refresh stored credentials for ``account_email``.

    Returns a ``google.oauth2.credentials.Credentials`` or ``None`` when no
    token has been stored yet (account not authorized). A refreshed token is
    re-persisted so the on-disk copy stays current.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    ref = token_ref(account_email)
    raw = load_token(ref)
    if not raw:
        logger.warning(
            "gmail.auth.no_token account=%s — run `worker auth-gmail "
            "--account %s` first.",
            account_email,
            account_email,
        )
        return None

    creds = Credentials.from_authorized_user_info(json.loads(raw), scopes=gmail_scopes())
    if creds.expired and creds.refresh_token:
        logger.info("gmail.auth.refresh account=%s", account_email)
        creds.refresh(Request())
        save_token(ref, creds.to_json())
    return creds


def build_gmail_service(account_email: str):
    """Build an authorized Gmail API ``service`` for ``account_email``.

    Returns ``None`` when the account has not been authorized yet so callers can
    skip it gracefully instead of crashing a multi-account run.
    """
    creds = load_credentials(account_email)
    if creds is None:
        return None

    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


__all__ = [
    "run_gmail_auth",
    "build_gmail_service",
    "load_credentials",
    "gmail_scopes",
    "token_ref",
    "GMAIL_SCOPE",
]
