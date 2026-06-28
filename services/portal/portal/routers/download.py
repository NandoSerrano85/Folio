"""Bulk download: a single image streams through directly; multiple images are
packaged into a ZIP written to a temp file on the (fast NVMe) disk and streamed
back, then cleaned up. Writing to disk instead of memory keeps the 8 GB box safe
for large selections.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from folio_core.config import get_settings
from folio_core.models import Image

from ..deps import get_db, require_user, safe_media_path
from ..schemas import DownloadRequest

router = APIRouter(prefix="/api", tags=["download"], dependencies=[Depends(require_user)])


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _zip_scratch_dir() -> str | None:
    """Directory for the temp ZIP: a managed, mounted volume (THUMBNAIL_ROOT, on
    NVMe) rather than the container's unbounded overlay ``/tmp``. Falls back to
    the system temp dir if that path can't be created."""
    try:
        d = Path(get_settings().thumbnail_root) / "_ziptmp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except OSError:
        return None


def _archive_name(preferred: str | None, fallback: str) -> str:
    """A safe download/zip-entry name: basename only, never a path.

    ``original_filename`` is user/vendor-supplied and could in theory carry path
    separators (``a/b.jpg``, ``..\\evil``). Collapsing to the basename keeps zip
    entries flat and prevents a crafted name from writing outside the extraction
    root on the *recipient's* machine (zip-slip). Falls back to the on-disk name.
    """
    raw = (preferred or "").strip().replace("\\", "/")
    base = os.path.basename(raw)
    return base or fallback


@router.post("/download")
def download(payload: DownloadRequest, db: Session = Depends(get_db)) -> FileResponse:
    ids = [i for i in payload.image_ids if i]
    if not ids:
        raise HTTPException(
            status_code=422,
            detail="image_ids is required",
        )

    # Preserve request order while de-duplicating.
    seen: set[int] = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]

    images = db.scalars(select(Image).where(Image.id.in_(ordered))).all()
    by_id = {img.id: img for img in images}

    resolved: list[tuple[Image, object]] = []
    for image_id in ordered:
        img = by_id.get(image_id)
        if img is None:
            continue
        try:
            path = safe_media_path(img.stored_path)
        except HTTPException:
            continue
        resolved.append((img, path))

    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="None of the requested images are available",
        )

    # Single image -> direct passthrough.
    if len(resolved) == 1:
        img, path = resolved[0]
        filename = _archive_name(img.original_filename, path.name)  # type: ignore[union-attr]
        return FileResponse(
            path,
            media_type=img.mime or "application/octet-stream",
            filename=filename,
            content_disposition_type="attachment",
        )

    # Multiple images -> ZIP on a temp file, streamed and cleaned up after.
    fd, tmp_path = tempfile.mkstemp(prefix="folio_dl_", suffix=".zip", dir=_zip_scratch_dir())
    os.close(fd)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for img, path in resolved:
                arcname = _archive_name(img.original_filename, path.name)  # type: ignore[union-attr]
                if arcname in used:
                    arcname = f"{img.id}_{arcname}"
                try:
                    zf.write(path, arcname=arcname)
                except OSError:
                    # File vanished/became unreadable between resolution and
                    # archiving (e.g. pruned mid-request). Skip it rather than
                    # failing the whole download.
                    continue
                used.add(arcname)
    except Exception:
        _cleanup(tmp_path)
        raise

    # Every resolved file disappeared during archiving -> nothing to return.
    if not used:
        _cleanup(tmp_path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="None of the requested images are available",
        )

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="folio_images.zip",
        background=BackgroundTask(_cleanup, tmp_path),
    )
