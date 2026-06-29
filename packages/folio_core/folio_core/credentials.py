"""Vendor-credential read/write shared by the portal and the worker.

A vendor's browser-download login lives in one ``vendor_credentials`` row, with
every secret stored as Fernet ciphertext (see :mod:`folio_core.crypto`). The
portal needs to WRITE these (operator enters a login), and the worker needs to
READ them (the adapter logs in). Both layers import from here so the writer does
not force a worker<-portal (or portal<-worker) dependency.

Plaintext secrets are NEVER logged: only the shape (which fields are present) is.
Neither helper commits — the caller owns the transaction. ``set_vendor_credentials``
flushes so a freshly created row has its primary key populated.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from folio_core.crypto import decrypt_value, encrypt_value
from folio_core.logging import get_logger
from folio_core.models import VendorCredential

logger = get_logger("folio_core.credentials")

__all__ = ["set_vendor_credentials", "get_vendor_credentials"]

# Sentinel so callers can distinguish "leave this field unchanged" (the default)
# from "explicitly clear it" (pass ``None``).
_UNSET: Any = object()


def set_vendor_credentials(
    session: Session,
    vendor_id: int,
    *,
    login_url: str | None = _UNSET,
    username: str | None = _UNSET,
    secret: str | None = _UNSET,
    extra: dict[str, Any] | None = _UNSET,
) -> None:
    """Create or update the credential row for ``vendor_id``.

    Each keyword defaults to a private sentinel meaning "leave unchanged"; pass
    an explicit value (including ``None``) to set or clear that field. ``secret``
    and ``username`` are Fernet-ENCRYPTED before storage; ``extra`` is
    JSON-encoded then encrypted. The plaintext ``secret`` / ``username`` /
    ``extra`` values are NEVER logged.

    The session is flushed so a newly created row has its ``id``. The caller is
    responsible for committing.
    """
    cred = session.scalar(
        select(VendorCredential).where(VendorCredential.vendor_id == vendor_id)
    )
    if cred is None:
        cred = VendorCredential(vendor_id=vendor_id)
        session.add(cred)

    if login_url is not _UNSET:
        cred.login_url = login_url
    if username is not _UNSET:
        cred.username_enc = encrypt_value(username) if username else None
    if secret is not _UNSET:
        cred.secret_enc = encrypt_value(secret) if secret else None
    if extra is not _UNSET:
        cred.extra_enc = encrypt_value(json.dumps(extra)) if extra else None

    session.flush()
    logger.info(
        "creds.saved vendor_id=%s has_login_url=%s has_username=%s "
        "has_secret=%s has_extra=%s",
        vendor_id,
        cred.login_url is not None,
        cred.username_enc is not None,
        cred.secret_enc is not None,
        cred.extra_enc is not None,
    )


def get_vendor_credentials(
    session: Session, vendor_id: int | None
) -> dict[str, Any] | None:
    """Load + decrypt the credential set for ``vendor_id`` using ``session``.

    Returns ``None`` when the vendor has no stored credentials. Otherwise returns
    a dict with keys ``login_url`` / ``username`` / ``secret`` / ``extra`` (the
    last three decrypted). Plaintext values are returned but NEVER logged.
    """
    if vendor_id is None:
        return None

    cred = session.scalar(
        select(VendorCredential).where(VendorCredential.vendor_id == vendor_id)
    )
    if cred is None:
        return None

    out: dict[str, Any] = {
        "login_url": cred.login_url,
        "username": None,
        "secret": None,
        "extra": {},
    }
    if cred.username_enc:
        out["username"] = decrypt_value(cred.username_enc)
    if cred.secret_enc:
        out["secret"] = decrypt_value(cred.secret_enc)
    if cred.extra_enc:
        raw = decrypt_value(cred.extra_enc)
        try:
            parsed = json.loads(raw)
            out["extra"] = parsed if isinstance(parsed, dict) else {"value": parsed}
        except (ValueError, TypeError):
            out["extra"] = {"raw": raw}
    return out
