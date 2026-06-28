"""EXIF source-date stamping via the ``exiftool`` subprocess.

We write the authoritative acquisition date into ``DateTimeOriginal`` and
``CreateDate`` so that downstream photo apps (Apple Photos, etc.) sort by the
real date rather than the ingest date.

Design rules:
- The original sha256 is computed BEFORE calling :func:`stamp_source_date`.
- Unsupported formats (e.g. some PNG/GIF variants without EXIF) must NOT raise;
  :func:`stamp_source_date` returns ``False`` instead.
- A missing exiftool binary raises :class:`ExiftoolNotFound` with a clear message.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from os import PathLike
from pathlib import Path

from folio_core.config import get_settings
from folio_core.logging import get_logger

logger = get_logger(__name__)

# exiftool's canonical datetime format.
_EXIF_FMT = "%Y:%m:%d %H:%M:%S"


class ExiftoolNotFound(RuntimeError):
    """Raised when the exiftool executable cannot be located."""


def _exiftool_bin() -> str:
    binary = get_settings().exiftool_binary
    resolved = shutil.which(binary)
    if resolved is None:
        raise ExiftoolNotFound(
            f"exiftool binary {binary!r} not found on PATH. "
            "Install libimage-exiftool-perl (it ships in the worker image)."
        )
    return resolved


def stamp_source_date(path: str | PathLike[str], dt: datetime) -> bool:
    """Stamp ``DateTimeOriginal`` and ``CreateDate`` to ``dt`` in-place.

    Returns ``True`` on success, ``False`` when the format cannot hold EXIF
    datetimes (exiftool reports the file but writes nothing). Raises
    :class:`ExiftoolNotFound` only when the binary is missing.
    """
    binary = _exiftool_bin()
    stamp = dt.strftime(_EXIF_FMT)
    fpath = str(Path(path))
    cmd = [
        binary,
        "-overwrite_original",
        f"-DateTimeOriginal={stamp}",
        f"-CreateDate={stamp}",
        fpath,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0 and "1 image files updated" in (proc.stdout or ""):
        return True
    logger.warning(
        "exif.stamp_no_update path=%s rc=%s stdout=%s stderr=%s",
        fpath,
        proc.returncode,
        (proc.stdout or "").strip(),
        (proc.stderr or "").strip(),
    )
    return False


def read_source_date(path: str | PathLike[str]) -> datetime | None:
    """Read ``DateTimeOriginal`` (falling back to ``CreateDate``) as a datetime.

    Returns ``None`` if no usable date tag is present or the format is
    unsupported. Never raises on unsupported format.
    """
    binary = _exiftool_bin()
    fpath = str(Path(path))
    cmd = [
        binary,
        "-s3",
        "-DateTimeOriginal",
        "-CreateDate",
        fpath,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return datetime.strptime(line, _EXIF_FMT)
        except ValueError:
            continue
    return None


__all__ = ["stamp_source_date", "read_source_date", "ExiftoolNotFound"]
