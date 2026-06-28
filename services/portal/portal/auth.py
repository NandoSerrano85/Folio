"""Password hashing/verification and login helpers (argon2id).

Centralizes the argon2-cffi usage so routers never touch the hasher directly.
``ensure_admin`` mirrors the bootstrap performed by ``portal.main`` startup and
is provided for reuse/tests; it is idempotent and safe to call repeatedly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from folio_core.config import get_settings
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import User

logger = get_logger("portal.auth")

# A single shared hasher (argon2id is the library default).
_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return an argon2id hash for ``plaintext``."""
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Verify ``plaintext`` against ``stored_hash``. Never raises."""
    if not stored_hash:
        return False
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # noqa: BLE001 - any malformed hash -> auth failure
        return False


def hash_token(plaintext: str) -> str:
    """Return an argon2id hash for an access ``token`` (reuses the hasher)."""
    return _hasher.hash(plaintext)


# A throwaway hash used only to equalize timing when no token is configured, so
# an attacker can't distinguish "token auth disabled" from "wrong token" by RTT.
_DUMMY_TOKEN_HASH = _hasher.hash("folio-access-token-not-configured")


def authenticate_token(token: str) -> bool:
    """Verify ``token`` against the argon2id hash in ``settings.access_token_hash``.

    Returns ``True`` only when a hash is configured and the token matches. Never
    raises; any malformed hash or empty input yields ``False``.
    """
    if not token:
        return False
    stored_hash = get_settings().access_token_hash
    if not stored_hash:
        # Run a verify against a throwaway hash so the unconfigured path costs
        # the same as a configured one (constant-time-ish), then fail.
        try:
            _hasher.verify(_DUMMY_TOKEN_HASH, token)
        except Exception:  # noqa: BLE001
            pass
        return False
    return verify_password(stored_hash, token)


def needs_rehash(stored_hash: str) -> bool:
    """Whether ``stored_hash`` should be upgraded to current parameters."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:  # noqa: BLE001
        return False


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Validate credentials and stamp ``last_login_at``.

    Returns the active :class:`User` on success, else ``None``. Transparently
    upgrades the stored hash if argon2 parameters have changed. The caller is
    responsible for committing the session.
    """
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active:
        return None
    if not verify_password(user.argon2_hash, password):
        return None
    if needs_rehash(user.argon2_hash):
        user.argon2_hash = hash_password(password)
    user.last_login_at = datetime.now(timezone.utc)
    return user


def ensure_admin() -> None:
    """Create the admin user from env on first boot if it does not exist.

    Idempotent: a pre-existing username short-circuits. Safe to call from portal
    startup or a management command.
    """
    settings = get_settings()
    if not settings.admin_password:
        logger.warning("admin.bootstrap_skipped reason=ADMIN_PASSWORD not set")
        return
    try:
        with session_scope() as db:
            exists = db.scalar(
                select(User).where(User.username == settings.admin_username)
            )
            if exists is not None:
                return
            db.add(
                User(
                    username=settings.admin_username,
                    argon2_hash=hash_password(settings.admin_password),
                    is_active=True,
                )
            )
            logger.info("admin.created username=%s", settings.admin_username)
    except Exception:  # noqa: BLE001 - never block startup on a seeded user
        logger.exception("admin.bootstrap_failed (continuing)")


__all__ = [
    "hash_password",
    "verify_password",
    "hash_token",
    "authenticate_token",
    "needs_rehash",
    "authenticate",
    "ensure_admin",
]
