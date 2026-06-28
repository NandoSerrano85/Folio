"""Vendor-adapter base: the FROZEN ABC, adapter exceptions, credential loading.

The authoritative :class:`VendorAdapter` ABC (and the :class:`DownloadTarget` /
:class:`EmailRef` dataclasses) live in :mod:`worker.gmail.sync`, where the
Phase-2 contract was frozen. This module RE-EXPORTS them so adapter code can
``from worker.browser.base import VendorAdapter`` without reaching across the
package, and adds the two things adapters need beyond the bare interface:

* a small exception hierarchy the orchestrator (:mod:`worker.gmail.sync`)
  classifies into ``assist_tasks`` reasons (``captcha`` / ``login_failed`` /
  ...), and
* :func:`load_vendor_credentials`, which reads a vendor's ``vendor_credentials``
  row and DECRYPTS the Fernet ciphertext into a plain dict for the adapter's
  ``login``. Decrypted values are NEVER logged.

Importing this module pulls in :mod:`worker.gmail.sync` (which is import-clean --
no Playwright, no Google libs at module load) but never Playwright itself.
"""

from __future__ import annotations

from typing import Any

# Re-export the FROZEN interface so adapters have a single, stable import site.
from worker.gmail.sync import DownloadTarget, EmailRef, VendorAdapter

from folio_core.logging import get_logger

logger = get_logger("worker.browser.base")


# --------------------------------------------------------------------------- #
# Adapter exception hierarchy
# --------------------------------------------------------------------------- #
class AdapterError(RuntimeError):
    """Base class for recoverable vendor-adapter failures.

    The orchestrator turns these into a pending ``assist_tasks`` row (so a human
    can finish the download) rather than aborting the whole sync run.
    """


class LoginFailed(AdapterError):
    """The vendor login step did not succeed (bad creds, changed form, ...)."""


class CaptchaEncountered(AdapterError):
    """A CAPTCHA / human-verification challenge blocked automation."""


class AntiBotBlocked(AdapterError):
    """An anti-bot wall (WAF, JS challenge, rate-limit page) blocked the flow."""


class DownloadError(AdapterError):
    """The asset could not be fetched, or the bytes were not a valid image."""


# --------------------------------------------------------------------------- #
# Vendor credential loading (Fernet-decrypted; values never logged)
# --------------------------------------------------------------------------- #
def load_vendor_credentials(vendor_id: int | None) -> dict[str, Any] | None:
    """Load + decrypt the credential set for ``vendor_id``.

    Returns ``None`` when the vendor has no stored credentials. Otherwise returns
    a dict::

        {
            "login_url": <str | None>,
            "username":  <str | None>,   # decrypted
            "secret":    <str | None>,   # decrypted
            "extra":     <dict>,          # decrypted JSON (cookies/2FA/notes)
        }

    The plaintext ``username`` / ``secret`` / ``extra`` values are returned to the
    caller but are NEVER logged here. Decryption errors (e.g. a rotated
    ``FERNET_KEY``) propagate so the caller can record a failure.
    """
    if vendor_id is None:
        return None

    import json

    from sqlalchemy import select

    from folio_core.crypto import decrypt_value
    from folio_core.db import session_scope
    from folio_core.models import VendorCredential

    with session_scope() as session:
        cred = session.scalar(
            select(VendorCredential).where(VendorCredential.vendor_id == vendor_id)
        )
        if cred is None:
            logger.info("creds.absent vendor_id=%s", vendor_id)
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
                # Free-form note rather than JSON; keep it usable without logging.
                out["extra"] = {"raw": raw}
        # Log only the SHAPE of what we loaded, never the secret material.
        logger.info(
            "creds.loaded vendor_id=%s has_username=%s has_secret=%s extra_keys=%d",
            vendor_id,
            out["username"] is not None,
            out["secret"] is not None,
            len(out["extra"]),
        )
        return out


__all__ = [
    "VendorAdapter",
    "DownloadTarget",
    "EmailRef",
    "AdapterError",
    "LoginFailed",
    "CaptchaEncountered",
    "AntiBotBlocked",
    "DownloadError",
    "load_vendor_credentials",
]
