"""Google Drive OAuth (installed-app flow) + authorized-service construction.

READ-ONLY scopes only. The per-account refresh token is serialized to JSON and
stored ENCRYPTED on disk via ``folio_core.crypto`` (Fernet), keyed by a
provider-qualified reference so a Drive token never collides with a Gmail token
for the same Google identity.

Headless / NAS note: ``run_drive_auth`` prints the Google consent URL and reads
back the authorization code the operator copies from the browser's address bar
(a loopback redirect — no reachable callback server needed). See
``worker.google_oauth``.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from folio_core.config import Settings, get_settings
from folio_core.crypto import load_token, save_token
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import Account, ProviderEnum

logger = get_logger("worker.drive.auth")

_DEFAULT_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _token_ref(account_email: str) -> str:
    """Provider-qualified token reference (distinct from a Gmail token)."""
    return f"drive:{account_email}"


def _drive_scopes(settings: Settings) -> list[str]:
    """Drive read-only scope(s) drawn from configured OAuth scopes."""
    scopes = [s for s in settings.scopes_list if "drive" in s]
    return scopes or [_DEFAULT_DRIVE_SCOPE]


def _run_consent(flow) -> object:
    """Drive the OAuth consent step and return fetched credentials.

    Uses the headless copy-the-URL / paste-the-code flow (works when the browser
    is on a different machine than the container — the NAS case). See
    ``worker.google_oauth``.
    """
    from worker.google_oauth import run_console_consent

    return run_console_consent(flow, label="Drive")


def _fetch_identity(creds) -> tuple[str | None, str | None]:
    """Return ``(emailAddress, displayName)`` of the authorized Drive user."""
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = service.about().get(fields="user(emailAddress,displayName)").execute()
        user = about.get("user", {})
        return user.get("emailAddress"), user.get("displayName")
    except Exception:  # noqa: BLE001 - identity probe is advisory only
        logger.warning("drive.auth.identity_probe_failed", exc_info=True)
        return None, None


def _upsert_account(email: str, token_ref: str, label: str | None) -> None:
    """Create or refresh the Drive account row for ``email``."""
    with session_scope() as session:
        account = session.scalar(
            select(Account).where(
                Account.provider == ProviderEnum.drive,
                Account.email == email,
            )
        )
        if account is None:
            account = Account(
                provider=ProviderEnum.drive,
                email=email,
                label=label or email,
                status="active",
                token_ref=token_ref,
            )
            session.add(account)
            logger.info("drive.auth.account_created email=%s", email)
        else:
            account.token_ref = token_ref
            account.status = "active"
            if label and not account.label:
                account.label = label
            logger.info("drive.auth.account_updated email=%s", email)


def run_drive_auth(account_email: str) -> None:
    """Run the Drive OAuth consent flow and persist the encrypted refresh token."""
    settings = get_settings()
    secrets_file = Path(settings.google_client_secrets_file)
    if not secrets_file.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {secrets_file}. Place the "
            "installed-app client secrets JSON there (see GOOGLE_CLIENT_SECRETS_FILE)."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = _drive_scopes(settings)
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes=scopes)
    creds = _run_consent(flow)

    ref = _token_ref(account_email)
    save_token(ref, creds.to_json())

    verified_email, display_name = _fetch_identity(creds)
    if verified_email and verified_email.lower() != account_email.lower():
        logger.warning(
            "drive.auth.email_mismatch requested=%s authorized=%s "
            "(storing under requested email)",
            account_email,
            verified_email,
        )

    _upsert_account(account_email, ref, display_name)
    logger.info("drive.auth.complete email=%s scopes=%s", account_email, scopes)


def build_drive_service(account_email: str, token_ref: str | None = None):
    """Build an authorized Drive v3 service for an account from its stored token.

    Refreshes (and re-persists) the credentials when the access token is expired.
    Raises ``RuntimeError`` if no usable token is stored.
    """
    settings = get_settings()
    ref = token_ref or _token_ref(account_email)
    token_json = load_token(ref)
    if not token_json:
        raise RuntimeError(
            f"No stored Drive token for {account_email!r} (ref={ref!r}). "
            f"Run `worker auth-drive --account {account_email}` first."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    info = json.loads(token_json)
    scopes = info.get("scopes") or _drive_scopes(settings)
    creds = Credentials.from_authorized_user_info(info, scopes=scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist the refreshed material (handles token rotation).
            save_token(ref, creds.to_json())
            logger.info("drive.auth.token_refreshed email=%s", account_email)
        elif not creds.refresh_token:
            raise RuntimeError(
                f"Stored Drive token for {account_email!r} has no refresh token; "
                f"re-run `worker auth-drive --account {account_email}`."
            )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


__all__ = ["run_drive_auth", "build_drive_service"]
