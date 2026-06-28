"""The common per-image acquisition pipeline.

Reused by Drive ingestion today and Gmail/vendor ingestion later. Given the
ORIGINAL bytes of a candidate image plus its provenance, the pipeline:

1. computes ``sha256`` over the ORIGINAL bytes (image identity, pre-EXIF stamp);
2. short-circuits if this exact ``(account, source_type, source_id)`` was already
   imported (the idempotency key) -> returns ``skipped``;
3. dedups against ``images.sha256`` -- if the bytes already exist, no file is
   written; only a new ``image_sources`` provenance row is linked;
4. for genuinely new images: builds a deterministic, collision-safe stored path
   (``folio_core.paths``), writes the file under ``MEDIA_ROOT``, stamps the
   authoritative ``source_date`` into EXIF (``folio_core.exif``), records
   dimensions/size, and inserts the ``images`` row;
5. inserts the ``image_sources`` provenance row.

The function is transaction-safe and idempotent: it operates inside a session
supplied by the caller (it never commits), and it cleans up an orphaned file if
the accompanying DB insert loses a race on a unique constraint.

Date semantics: ``source_date`` is THE authoritative acquisition date (Drive
``createdTime`` / email ``Date``) and the default library sort key. Callers pass
the matching ``source_date_origin`` enum.
"""

from __future__ import annotations

import io
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from folio_core.config import get_settings
from folio_core.exif import ExiftoolNotFound, stamp_source_date
from folio_core.hashing import sha256_bytes
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    Image,
    ImageSource,
    SourceDateOriginEnum,
    SourceTypeEnum,
    Vendor,
)
from folio_core.paths import build_stored_path

logger = get_logger("worker.pipeline")

# image_sources columns the caller may populate via ``image_source_fields``.
_ALLOWED_SOURCE_FIELDS = frozenset(
    {
        "vendor_url",
        "email_subject",
        "email_sender",
        "email_message_id",
        "drive_folder_path",
        "drive_created_time",
        "drive_modified_time",
        "drive_owner",
        "raw_meta",
    }
)


@dataclass(slots=True)
class IngestResult:
    """Outcome of a single pipeline invocation."""

    sha256: str
    image_id: int | None
    created_image: bool  # a brand-new image file was stored
    created_source: bool  # a new image_sources provenance row was inserted
    skipped: bool  # this exact source was already imported (no-op)
    stored_path: str | None  # MEDIA_ROOT-relative path of the image


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse an RFC-3339 / ISO-8601 timestamp into an aware UTC-normalized datetime.

    Returns ``None`` for empty/garbage input. Naive results are assumed UTC.
    Falls back to ``dateutil`` (a folio_core dependency) for exotic inputs.
    """
    if not value:
        return None
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            from dateutil import parser as _dateparser

            dt = _dateparser.isoparse(text)
        except Exception:  # noqa: BLE001 - parsing is strictly best-effort
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _derive_ext(filename: str | None, mime: str | None) -> str:
    """Best-effort file extension from the original name, else the MIME type."""
    if filename:
        suffix = Path(filename).suffix.lstrip(".").lower()
        if suffix:
            return suffix
    if mime:
        guessed = mimetypes.guess_extension(mime.split(";", 1)[0].strip())
        if guessed:
            return guessed.lstrip(".").lower()
    return ""


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Return ``(width, height)`` via Pillow, or ``(None, None)`` if undecodable."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 - non-images / unsupported formats are fine
        return None, None


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("pipeline.orphan_cleanup_failed path=%s", path)


def run_pipeline(
    session: Session,
    *,
    account: Account,
    source_type: SourceTypeEnum,
    source_id: str,
    data: bytes,
    original_filename: str | None,
    mime: str | None,
    source_date: datetime,
    source_date_origin: SourceDateOriginEnum,
    vendor: Vendor | None = None,
    image_source_fields: dict | None = None,
    media_root: Path | None = None,
) -> IngestResult:
    """Acquire one image into the library. See module docstring for the contract.

    ``account`` and ``vendor`` may be detached ORM instances (only their scalar
    ``id`` / ``label`` / ``email`` / ``name`` attributes are read); they are not
    added to ``session``. The caller owns the commit.
    """
    settings = get_settings()
    root = Path(media_root) if media_root is not None else Path(settings.media_root)
    digest = sha256_bytes(data)

    extra = {
        k: v for k, v in (image_source_fields or {}).items() if k in _ALLOWED_SOURCE_FIELDS
    }

    # 1. Idempotency: have we already imported this exact upstream source?
    already = session.scalar(
        select(ImageSource).where(
            ImageSource.account_id == account.id,
            ImageSource.source_type == source_type,
            ImageSource.source_id == source_id,
        )
    )
    if already is not None:
        return IngestResult(
            sha256=digest,
            image_id=already.image_id,
            created_image=False,
            created_source=False,
            skipped=True,
            stored_path=None,
        )

    # 2. Dedup by content hash.
    image = session.scalar(select(Image).where(Image.sha256 == digest))
    created_image = False
    written_path: Path | None = None

    if image is None:
        ext = _derive_ext(original_filename, mime)
        vendor_name = vendor.name if vendor is not None else None
        rel_path = build_stored_path(
            account.label or account.email,
            source_date,
            vendor_name,
            original_filename or "image",
            ext,
            media_root=root,
        )
        abs_path = root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        written_path = abs_path

        # Stamp the authoritative acquisition date (best-effort; never fatal).
        try:
            stamped = stamp_source_date(abs_path, source_date)
            if not stamped:
                logger.info(
                    "pipeline.exif_unsupported path=%s sha=%s", rel_path, digest
                )
        except ExiftoolNotFound:
            logger.warning("pipeline.exif_binary_missing path=%s", rel_path)
        except Exception:  # noqa: BLE001 - stamping must not abort ingestion
            logger.exception("pipeline.exif_failed path=%s", rel_path)

        width, height = _dimensions(data)
        image = Image(
            sha256=digest,
            original_filename=original_filename,
            stored_path=rel_path,
            ext=ext or None,
            mime=mime,
            bytes=len(data),
            width=width,
            height=height,
            source_date=source_date,
            source_date_origin=source_date_origin,
        )
        session.add(image)
        try:
            session.flush()
        except Exception:
            _safe_unlink(written_path)
            raise
        created_image = True

    # 3. Link provenance.
    src = ImageSource(
        image_id=image.id,
        account_id=account.id,
        source_type=source_type,
        source_id=source_id,
        vendor_id=vendor.id if vendor is not None else None,
        **extra,
    )
    session.add(src)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race on uq_image_sources_account_type_sourceid (or, when this
        # image is new, on sha256/stored_path). Roll back this unit of work and
        # treat it as an idempotent skip.
        session.rollback()
        if created_image and written_path is not None:
            _safe_unlink(written_path)
        logger.info(
            "pipeline.race_skip account=%s type=%s source_id=%s",
            account.id,
            source_type.value,
            source_id,
        )
        return IngestResult(
            sha256=digest,
            image_id=None,
            created_image=False,
            created_source=False,
            skipped=True,
            stored_path=None,
        )
    except Exception:
        if created_image and written_path is not None:
            _safe_unlink(written_path)
        raise

    return IngestResult(
        sha256=digest,
        image_id=image.id,
        created_image=created_image,
        created_source=True,
        skipped=False,
        stored_path=image.stored_path,
    )


__all__ = ["IngestResult", "run_pipeline", "parse_rfc3339"]
