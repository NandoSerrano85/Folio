"""Image listing, detail, original-file streaming, and thumbnails.

The list endpoint does all pagination/sorting/filtering in SQL. Filters that
touch source provenance (account/vendor/sender, and the ``q`` free-text search
over email subject/sender) use correlated ``EXISTS`` subqueries so a single
image with many sources is never duplicated in the result set or the count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from folio_core.models import Account, Folder, FolderImage, Image, ImageSource, Vendor

from ..deps import get_db, require_user, safe_media_path
from ..schemas import (
    ImageDetail,
    ImageListItem,
    ImageListResponse,
    ImageSourceOut,
)
from ..thumbnails import clamp_size, generate_thumbnail

router = APIRouter(
    prefix="/api/images",
    tags=["images"],
    dependencies=[Depends(require_user)],
)

PAGE_SIZES = (25, 50, 100, 200)
SORTS = ("newest", "oldest", "name", "vendor", "account")


def _parse_bound(value: str | None, *, end: bool) -> datetime | None:
    """Parse a date/datetime filter bound into a tz-aware UTC datetime.

    Date-only ``end`` bounds are pushed to the start of the following day so the
    range is inclusive of the whole day (callers compare with ``<``).
    """
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw[:10])
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"Invalid date: {value!r}",
            ) from exc
    date_only = len(raw) <= 10
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end and date_only:
        parsed = parsed + timedelta(days=1)
    return parsed


def _apply_filters(
    stmt: Select,
    *,
    q: str | None,
    sender: str | None,
    vendor: int | None,
    account: int | None,
    folder: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> Select:
    if account is not None:
        stmt = stmt.where(
            exists().where(
                and_(
                    ImageSource.image_id == Image.id,
                    ImageSource.account_id == account,
                )
            )
        )
    if vendor is not None:
        stmt = stmt.where(
            exists().where(
                and_(
                    ImageSource.image_id == Image.id,
                    ImageSource.vendor_id == vendor,
                )
            )
        )
    if sender:
        pat = f"%{sender.strip()}%"
        stmt = stmt.where(
            exists().where(
                and_(
                    ImageSource.image_id == Image.id,
                    ImageSource.email_sender.ilike(pat),
                )
            )
        )
    if folder is not None:
        stmt = stmt.where(
            exists().where(
                and_(
                    FolderImage.image_id == Image.id,
                    FolderImage.folder_id == folder,
                )
            )
        )
    if q:
        pat = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Image.original_filename.ilike(pat),
                exists().where(
                    and_(
                        ImageSource.image_id == Image.id,
                        or_(
                            ImageSource.email_subject.ilike(pat),
                            ImageSource.email_sender.ilike(pat),
                        ),
                    )
                ),
            )
        )
    if date_from is not None:
        stmt = stmt.where(Image.source_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Image.source_date < date_to)
    return stmt


def _account_sort_key():
    return (
        select(Account.email)
        .where(
            exists().where(
                and_(
                    ImageSource.image_id == Image.id,
                    ImageSource.account_id == Account.id,
                )
            )
        )
        .order_by(Account.email)
        .limit(1)
        .scalar_subquery()
    )


def _vendor_sort_key():
    return (
        select(Vendor.name)
        .where(
            exists().where(
                and_(
                    ImageSource.image_id == Image.id,
                    ImageSource.vendor_id == Vendor.id,
                )
            )
        )
        .order_by(Vendor.name)
        .limit(1)
        .scalar_subquery()
    )


def _apply_sort(stmt: Select, sort: str) -> Select:
    if sort == "oldest":
        return stmt.order_by(Image.source_date.asc(), Image.id.asc())
    if sort == "name":
        return stmt.order_by(
            Image.original_filename.asc().nulls_last(), Image.id.asc()
        )
    if sort == "vendor":
        return stmt.order_by(
            _vendor_sort_key().asc().nulls_last(),
            Image.source_date.desc(),
            Image.id.desc(),
        )
    if sort == "account":
        return stmt.order_by(
            _account_sort_key().asc().nulls_last(),
            Image.source_date.desc(),
            Image.id.desc(),
        )
    # default: newest
    return stmt.order_by(Image.source_date.desc(), Image.id.desc())


def _representative(image: Image) -> tuple[str | None, str | None]:
    """Pick a display vendor name and account email from an image's sources."""
    vendor_name: str | None = None
    account_email: str | None = None
    for src in image.sources:
        if account_email is None and src.account is not None:
            account_email = src.account.email
        if vendor_name is None and src.vendor is not None:
            vendor_name = src.vendor.name
        if vendor_name and account_email:
            break
    return vendor_name, account_email


@router.get("", response_model=ImageListResponse)
@router.get("/", response_model=ImageListResponse, include_in_schema=False)
def list_images(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50),
    sort: str = Query("newest"),
    q: str | None = Query(None),
    sender: str | None = Query(None),
    vendor: int | None = Query(None),
    account: int | None = Query(None),
    folder: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
) -> ImageListResponse:
    if page_size not in PAGE_SIZES:
        page_size = 50
    if sort not in SORTS:
        sort = "newest"

    df = _parse_bound(date_from, end=False)
    dt = _parse_bound(date_to, end=True)

    filter_kwargs = dict(
        q=q,
        sender=sender,
        vendor=vendor,
        account=account,
        folder=folder,
        date_from=df,
        date_to=dt,
    )

    count_stmt = _apply_filters(
        select(func.count(Image.id)).select_from(Image), **filter_kwargs
    )
    total = int(db.scalar(count_stmt) or 0)

    pages = (total + page_size - 1) // page_size if total else 0
    offset = (page - 1) * page_size

    rows_stmt = _apply_filters(select(Image), **filter_kwargs)
    rows_stmt = _apply_sort(rows_stmt, sort)
    rows_stmt = rows_stmt.options(
        selectinload(Image.sources).selectinload(ImageSource.vendor),
        selectinload(Image.sources).selectinload(ImageSource.account),
    ).offset(offset).limit(page_size)

    images = db.scalars(rows_stmt).all()

    base = str(request.base_url).rstrip("/")
    items: list[ImageListItem] = []
    for img in images:
        vendor_name, account_email = _representative(img)
        items.append(
            ImageListItem(
                id=img.id,
                filename=img.original_filename,
                source_date=img.source_date,
                source_date_origin=(
                    img.source_date_origin.value
                    if hasattr(img.source_date_origin, "value")
                    else str(img.source_date_origin)
                ),
                vendor=vendor_name,
                account=account_email,
                thumb_url=f"{base}/api/images/{img.id}/thumb",
                ext=img.ext,
                bytes=img.bytes,
                width=img.width,
                height=img.height,
            )
        )

    return ImageListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


def _get_image_or_404(db: Session, image_id: int) -> Image:
    img = db.get(Image, image_id)
    if img is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return img


@router.get("/{image_id}", response_model=ImageDetail)
def image_detail(
    image_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> ImageDetail:
    stmt = (
        select(Image)
        .where(Image.id == image_id)
        .options(
            selectinload(Image.sources).selectinload(ImageSource.vendor),
            selectinload(Image.sources).selectinload(ImageSource.account),
        )
    )
    img = db.scalars(stmt).first()
    if img is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    base = str(request.base_url).rstrip("/")
    sources = [
        ImageSourceOut(
            id=s.id,
            source_type=(
                s.source_type.value
                if hasattr(s.source_type, "value")
                else str(s.source_type)
            ),
            source_id=s.source_id,
            account_id=s.account_id,
            account=s.account.email if s.account is not None else None,
            vendor_id=s.vendor_id,
            vendor=s.vendor.name if s.vendor is not None else None,
            vendor_url=s.vendor_url,
            email_subject=s.email_subject,
            email_sender=s.email_sender,
            email_message_id=s.email_message_id,
            drive_folder_path=s.drive_folder_path,
            drive_created_time=s.drive_created_time,
            drive_modified_time=s.drive_modified_time,
            drive_owner=s.drive_owner,
            created_at=s.created_at,
        )
        for s in img.sources
    ]

    return ImageDetail(
        id=img.id,
        sha256=img.sha256,
        filename=img.original_filename,
        stored_path=img.stored_path,
        ext=img.ext,
        mime=img.mime,
        bytes=img.bytes,
        width=img.width,
        height=img.height,
        source_date=img.source_date,
        source_date_origin=(
            img.source_date_origin.value
            if hasattr(img.source_date_origin, "value")
            else str(img.source_date_origin)
        ),
        ingested_at=img.ingested_at,
        thumb_url=f"{base}/api/images/{img.id}/thumb",
        file_url=f"{base}/api/images/{img.id}/file",
        sources=sources,
    )


@router.get("/{image_id}/file")
def image_file(image_id: int, db: Session = Depends(get_db)) -> FileResponse:
    img = _get_image_or_404(db, image_id)
    path = safe_media_path(img.stored_path)
    filename = img.original_filename or path.name
    # Starlette's FileResponse honors the Range header (returns 206) and sets
    # Accept-Ranges; this keeps large-image / video-ish streaming correct.
    return FileResponse(
        path,
        media_type=img.mime or "application/octet-stream",
        filename=filename,
        content_disposition_type="inline",
    )


@router.get("/{image_id}/thumb")
def image_thumb(
    image_id: int,
    db: Session = Depends(get_db),
    size: int = Query(default=0),
) -> FileResponse:
    img = _get_image_or_404(db, image_id)
    # Resolve the source path defensively; a missing/escaping path yields a
    # placeholder rather than a hard error so the gallery keeps rendering.
    source_path = None
    try:
        source_path = safe_media_path(img.stored_path)
    except HTTPException:
        source_path = None
    thumb = generate_thumbnail(img.id, source_path, clamp_size(size or None))
    return FileResponse(thumb, media_type="image/jpeg")
