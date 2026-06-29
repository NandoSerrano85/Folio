"""Expand a downloaded asset that may be a ZIP of images into image members.

Shopify "Digital Downloads" links sometimes return a single direct image and
sometimes a ``.zip`` bundling several images. :func:`expand_image_archive`
normalises both cases into a flat list of ``(filename, bytes)`` pairs so the
ingest pipeline can treat each image uniformly.

Pure and offline: no network, no database. Shopify Digital Downloads zips are
NOT encrypted, so no password handling is needed here. Hostile/oversized inputs
are bounded by a member count cap and a per-member uncompressed-size cap to keep
a malicious or accidental zip bomb from exhausting memory.
"""

from __future__ import annotations

import io
import zipfile

__all__ = ["expand_image_archive", "is_zip"]

# Image extensions we extract from an archive (lower-cased, no dot).
_IMAGE_EXTS = {
    "png", "jpg", "jpeg", "webp", "gif", "tif", "tiff", "bmp", "heic",
}

# Bounds so a malicious/accidental zip cannot exhaust memory.
_MAX_MEMBERS = 200
_MAX_MEMBER_BYTES = 50 * 1024 * 1024  # 50 MB uncompressed per member
_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB extracted in aggregate per zip

_ZIP_MAGIC = b"PK\x03\x04"
# Empty / spanned archive signatures still start with "PK".
_ZIP_MAGIC_PREFIX = b"PK"


def is_zip(filename: str, data: bytes) -> bool:
    """True when ``data`` looks like a ZIP (PK magic) or ``filename`` ends .zip."""
    if data[:4] == _ZIP_MAGIC:
        return True
    if data[:2] == _ZIP_MAGIC_PREFIX and zipfile.is_zipfile(io.BytesIO(data)):
        return True
    return filename.lower().endswith(".zip")


def _member_basename(name: str) -> str:
    """Return the trailing path component of a zip member name (any separator)."""
    # Zip names use forward slashes; normalise backslashes defensively too.
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _is_image_member(name: str) -> bool:
    base = _member_basename(name)
    if not base or base.startswith("."):  # dotfiles / resource forks
        return False
    if "." not in base:
        return False
    return base.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS


def expand_image_archive(filename: str, data: bytes) -> list[tuple[str, bytes]]:
    """Expand ``data`` into a list of ``(member_basename, member_bytes)`` images.

    If ``data`` is a ZIP (PK magic, or ``filename`` ends with ``.zip``), open it
    and return every member whose name has a recognised image extension, using
    the member's basename as the filename. Directory entries, ``__MACOSX``
    resource forks, and dotfiles are skipped. At most ``_MAX_MEMBERS`` members
    are returned, and any member whose uncompressed size exceeds
    ``_MAX_MEMBER_BYTES`` is skipped.

    If ``data`` is NOT a zip, the input is returned unchanged as a single
    ``[(filename, data)]`` pair.
    """
    if not is_zip(filename, data):
        return [(filename, data)]

    out: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if len(out) >= _MAX_MEMBERS:
                    break
                if info.is_dir():
                    continue
                name = info.filename
                if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
                    continue
                if not _is_image_member(name):
                    continue
                if info.file_size > _MAX_MEMBER_BYTES:
                    continue  # honest oversized member — cheap early skip
                # Bounded streaming read: pull at most one byte past the cap so a
                # member whose header UNDER-reports its size (zip bomb) still can't
                # decompress unbounded into RAM.
                with zf.open(info) as fh:
                    member = fh.read(_MAX_MEMBER_BYTES + 1)
                if len(member) > _MAX_MEMBER_BYTES:
                    continue
                total += len(member)
                if total > _MAX_TOTAL_BYTES:
                    break  # aggregate cap — stop extracting this archive
                out.append((_member_basename(name), member))
    except zipfile.BadZipFile:
        # Mislabelled / truncated archive: fall back to treating it as raw bytes.
        return [(filename, data)]

    return out
