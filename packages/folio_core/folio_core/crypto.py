"""Fernet-based encryption for per-account OAuth refresh tokens.

Tokens are stored ENCRYPTED on disk under ``TOKEN_DIR``, one JSON-ish blob per
account, keyed by a caller-supplied reference (typically the account email).
The symmetric key comes from ``FERNET_KEY`` (a urlsafe base64 32-byte key).

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import re
from pathlib import Path

from cryptography.fernet import Fernet

from folio_core.config import get_settings

_TOKEN_SUFFIX = ".token"
_REF_SAFE = re.compile(r"[^A-Za-z0-9._@+-]+")


class CryptoConfigError(RuntimeError):
    """Raised when FERNET_KEY is missing or invalid."""


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise CryptoConfigError(
            "FERNET_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and put it in .env."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CryptoConfigError(f"FERNET_KEY is invalid: {exc}") from exc


def encrypt_token(plaintext: str) -> bytes:
    """Encrypt a token string, returning ciphertext bytes."""
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_token(ciphertext: bytes) -> str:
    """Decrypt ciphertext bytes back into the original token string."""
    return _fernet().decrypt(ciphertext).decode("utf-8")


def _ref_to_filename(ref: str) -> str:
    safe = _REF_SAFE.sub("_", ref.strip()).strip("_") or "account"
    return f"{safe}{_TOKEN_SUFFIX}"


def token_path(ref: str, *, token_dir: Path | None = None) -> Path:
    """Return the absolute on-disk path for an account's token reference."""
    base = Path(token_dir) if token_dir else get_settings().token_dir
    return base / _ref_to_filename(ref)


def save_token(ref: str, plaintext: str, *, token_dir: Path | None = None) -> Path:
    """Encrypt and persist a token for ``ref``. Returns the file path written."""
    path = token_path(ref, token_dir=token_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt_token(plaintext))
    try:
        path.chmod(0o600)
    except OSError:
        # Some volume backends (e.g. certain QNAP shares) reject chmod; the
        # directory perms still protect the file.
        pass
    return path


def load_token(ref: str, *, token_dir: Path | None = None) -> str | None:
    """Load and decrypt a token for ``ref``. Returns ``None`` if absent."""
    path = token_path(ref, token_dir=token_dir)
    if not path.exists():
        return None
    return decrypt_token(path.read_bytes())


__all__ = [
    "encrypt_token",
    "decrypt_token",
    "save_token",
    "load_token",
    "token_path",
    "CryptoConfigError",
]
