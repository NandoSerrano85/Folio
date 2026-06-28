"""Bulk download: a single image streams through directly; multiple images are
packaged into a ZIP written to a temp file on the (fast NVMe) disk and streamed
back, then cleaned up. Writing to disk instead of memory keeps the 8 GB box safe
for large selections.
"""

from __future__ import annotations

import os
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from folio_core.models import Image

from ..deps import get_db, require_user, safe_media_path
from ..schemas import DownloadRequest

router = APIRouter(prefix="/api", tags=["download"], dependencies=[Depends(require_user)])


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


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
        filename = img.original_filename or path.name  # type: ignore[union-attr]
        return FileResponse(
            path,
            media_type=img.mime or "application/octet-stream",
            filename=filename,
            content_disposition_type="attachment",
        )

    # Multiple images -> ZIP on a temp file, streamed and cleaned up after.
    fd, tmp_path = tempfile.mkstemp(prefix="folio_dl_", suffix=".zip")
    os.close(fd)
    used: set[str] = set()
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for img, path in resolved:
                arcname = img.original_filename or path.name  # type: ignore[union-attr]
                if arcname in used:
                    arcname = f"{img.id}_{arcname}"
                used.add(arcname)
                zf.write(path, arcname=arcname)
    except Exception:
        _cleanup(tmp_path)
        raise

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="folio_images.zip",
        background=BackgroundTask(_cleanup, tmp_path),
    )
