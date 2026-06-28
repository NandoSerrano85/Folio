"""Connected-account listing with per-account image/source counts."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from folio_core.models import Account, ImageSource

from ..deps import get_db, require_user
from ..schemas import AccountOut

router = APIRouter(
    prefix="/api/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_user)],
)


@router.get("", response_model=list[AccountOut])
@router.get("/", response_model=list[AccountOut], include_in_schema=False)
def list_accounts(db: Session = Depends(get_db)) -> list[AccountOut]:
    accounts = db.scalars(
        select(Account).order_by(Account.provider.asc(), Account.email.asc())
    ).all()

    # distinct images and total sources per account, in one pass each.
    image_counts = dict(
        db.execute(
            select(
                ImageSource.account_id,
                func.count(func.distinct(ImageSource.image_id)),
            ).group_by(ImageSource.account_id)
        ).all()
    )
    source_counts = dict(
        db.execute(
            select(ImageSource.account_id, func.count(ImageSource.id)).group_by(
                ImageSource.account_id
            )
        ).all()
    )

    return [
        AccountOut(
            id=a.id,
            provider=(
                a.provider.value if hasattr(a.provider, "value") else str(a.provider)
            ),
            email=a.email,
            label=a.label,
            status=a.status,
            image_count=int(image_counts.get(a.id, 0)),
            source_count=int(source_counts.get(a.id, 0)),
        )
        for a in accounts
    ]
