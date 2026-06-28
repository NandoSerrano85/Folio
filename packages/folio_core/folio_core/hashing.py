"""Content hashing. The sha256 of the ORIGINAL bytes is an image's identity.

The hash is always computed BEFORE any EXIF stamping so that re-stamping or
metadata rewrites never change identity and dedup stays stable.
"""

from __future__ import annotations

import hashlib
from os import PathLike
from pathlib import Path

_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 digest of an in-memory byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | PathLike[str]) -> str:
    """Return the hex sha256 digest of a file, streamed to bound memory."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["sha256_bytes", "sha256_file"]
