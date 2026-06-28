"""Library statistics for the dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from folio_core.models import Account, Image, ImageSource, Vendor

from ..deps import get_db, require_user
from ..schemas import CountByName, StatsResponse

router = APIRouter(prefix="/api", tags=["stats"], dependencies=[Depends(require_user)])


@router.get("/stats", response_model=StatsResponse)
def stats(db: Session = Depends(get_db)) -> StatsResponse:
    total_images = int(db.scalar(select(func.count(Image.id))) or 0)
    latest = db.scalar(select(func.max(Image.source_date)))
    library_bytes = int(db.scalar(select(func.coalesce(func.sum(Image.bytes), 0))) or 0)

    by_account_rows = db.execute(
        select(
            Account.email,
            func.count(func.distinct(ImageSource.image_id)),
        )
        .join(ImageSource, ImageSource.account_id == Account.id)
        .group_by(Account.email)
        .order_by(func.count(func.distinct(ImageSource.image_id)).desc())
    ).all()

    by_vendor_rows = db.execute(
        select(
            Vendor.name,
            func.count(func.distinct(ImageSource.image_id)),
        )
        .join(ImageSource, ImageSource.vendor_id == Vendor.id)
        .group_by(Vendor.name)
        .order_by(func.count(func.distinct(ImageSource.image_id)).desc())
    ).all()

    return StatsResponse(
        total_images=total_images,
        by_account=[CountByName(name=name, count=int(c)) for name, c in by_account_rows],
        by_vendor=[CountByName(name=name, count=int(c)) for name, c in by_vendor_rows],
        latest_source_date=latest,
        library_bytes=library_bytes,
    )
