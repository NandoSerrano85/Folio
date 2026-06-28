"""Deterministic, collision-safe storage-path construction.

Layout (relative to ``MEDIA_ROOT``)::

    <account>/<YYYY>/<YYYY-MM-DD>_<vendor-or-drive>_<sanitized-origname>.<ext>

The ``source_date`` drives the year folder and the date prefix so the on-disk
tree mirrors the authoritative acquisition date. Filenames are sanitized to a
safe ASCII subset and de-duplicated with a numeric suffix when a target path
already exists.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_DASH = re.compile(r"-{2,}")
_MAX_STEM = 80


def sanitize_component(value: str, *, fallback: str = "item") -> str:
    """Sanitize an arbitrary string into a filesystem-safe path component."""
    value = (value or "").strip()
    # Replace path separators and unsafe chars with a dash.
    value = value.replace("/", "-").replace("\\", "-")
    value = _SAFE_CHARS.sub("-", value)
    value = _MULTI_DASH.sub("-", value).strip("-._")
    return value or fallback


def _normalize_ext(ext: str) -> str:
    ext = (ext or "").strip().lstrip(".").lower()
    return _SAFE_CHARS.sub("", ext)


def build_stored_path(
    account_label: str,
    source_date: datetime,
    vendor: str | None,
    original_name: str,
    ext: str,
    *,
    media_root: Path | None = None,
) -> str:
    """Build a MEDIA_ROOT-relative stored path for an image.

    Parameters
    ----------
    account_label:
        Human label of the account (becomes the top-level folder).
    source_date:
        The authoritative acquisition datetime (drives year + date prefix).
    vendor:
        Vendor slug/name, or ``None`` for direct Drive files (-> ``drive``).
    original_name:
        Original filename (its stem is sanitized and truncated).
    ext:
        File extension (with or without a leading dot).
    media_root:
        If provided, the returned path is checked for on-disk collisions and a
        numeric suffix is appended until a free name is found. If ``None``, no
        filesystem check is performed and the base name is returned.

    Returns
    -------
    str
        A POSIX-style relative path.
    """
    account = sanitize_component(account_label, fallback="account")
    vendor_slug = sanitize_component(vendor, fallback="drive") if vendor else "drive"
    year = f"{source_date.year:04d}"
    date_prefix = source_date.strftime("%Y-%m-%d")

    stem = Path(original_name or "image").stem
    stem = sanitize_component(stem, fallback="image")[:_MAX_STEM] or "image"
    clean_ext = _normalize_ext(ext)

    base_name = f"{date_prefix}_{vendor_slug}_{stem}"
    rel_dir = Path(account) / year

    def _compose(suffix: str = "") -> str:
        fname = f"{base_name}{suffix}"
        if clean_ext:
            fname = f"{fname}.{clean_ext}"
        return (rel_dir / fname).as_posix()

    candidate = _compose()
    if media_root is None:
        return candidate

    root = Path(media_root)
    if not (root / candidate).exists():
        return candidate

    counter = 1
    while True:
        candidate = _compose(f"-{counter}")
        if not (root / candidate).exists():
            return candidate
        counter += 1


__all__ = ["build_stored_path", "sanitize_component"]
