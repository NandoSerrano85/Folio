"""Re-apply EXIF source-date stamps from the database to files on disk.

Ingestion is idempotent, so a re-sync will NOT re-stamp files it already
imported. After fixing the stamping path (e.g. switching exiftool to in-place
writes), use ``worker restamp`` to (re)write ``DateTimeOriginal``/``CreateDate``
onto the existing files from the authoritative ``images.source_date``. Safe to
run repeatedly; the database date is unaffected either way.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from folio_core.config import get_settings
from folio_core.db import session_scope
from folio_core.exif import ExiftoolNotFound, stamp_source_date
from folio_core.logging import get_logger
from folio_core.models import Account, Image, ImageSource

logger = get_logger("worker.restamp")


def run_restamp(account_email: str | None = None) -> None:
    """Re-stamp EXIF dates onto stored files from the DB ``source_date``."""
    settings = get_settings()
    root = Path(settings.media_root)

    with session_scope() as session:
        stmt = select(Image)
        if account_email:
            stmt = stmt.where(
                Image.id.in_(
                    select(ImageSource.image_id)
                    .join(Account, Account.id == ImageSource.account_id)
                    .where(Account.email == account_email.strip().lower())
                )
            )
        images = session.scalars(stmt).all()

    total = len(images)
    stamped = unsupported = missing = failed = 0
    logger.info("restamp.start total=%d account=%s", total, account_email or "all")

    for img in images:
        path = root / img.stored_path
        if not path.is_file():
            logger.warning("restamp.missing_file path=%s", img.stored_path)
            missing += 1
            continue
        try:
            if stamp_source_date(path, img.source_date):
                stamped += 1
            else:
                unsupported += 1
        except ExiftoolNotFound:
            logger.error("restamp.exiftool_missing — aborting")
            raise
        except Exception:  # noqa: BLE001 - one bad file must not abort the run
            logger.exception("restamp.failed path=%s", img.stored_path)
            failed += 1

    logger.info(
        "restamp.done total=%d stamped=%d unsupported=%d missing=%d failed=%d",
        total,
        stamped,
        unsupported,
        missing,
        failed,
    )


__all__ = ["run_restamp"]
