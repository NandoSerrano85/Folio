"""Gmail allow-list sender management.

``/discovered`` aggregates per-sender counts harvested by the worker's discovery
job (stored on ``senders.discovered_count``) for the allow-list dropdown.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from folio_core.models import Account, Sender, Vendor

from ..deps import get_db, require_user
from ..schemas import (
    DiscoveredSenderOut,
    OkResponse,
    SenderCreate,
    SenderOut,
    SenderUpdate,
)

router = APIRouter(
    prefix="/api/senders",
    tags=["senders"],
    dependencies=[Depends(require_user)],
)


def _domain_of(address: str) -> str | None:
    if "@" in address:
        dom = address.rsplit("@", 1)[1].strip().lower()
        return dom or None
    return None


@router.get("", response_model=list[SenderOut])
@router.get("/", response_model=list[SenderOut], include_in_schema=False)
def list_senders(
    db: Session = Depends(get_db),
    account: int | None = Query(None),
) -> list[SenderOut]:
    stmt = select(Sender)
    if account is not None:
        stmt = stmt.where(Sender.account_id == account)
    stmt = stmt.order_by(Sender.address.asc())
    rows = db.scalars(stmt).all()
    return [SenderOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/discovered", response_model=list[DiscoveredSenderOut])
def discovered_senders(
    db: Session = Depends(get_db),
    account: int | None = Query(None),
) -> list[DiscoveredSenderOut]:
    stmt = select(Sender).where(Sender.discovered_count > 0)
    if account is not None:
        stmt = stmt.where(Sender.account_id == account)
    stmt = stmt.order_by(Sender.discovered_count.desc(), Sender.address.asc())
    rows = db.scalars(stmt).all()
    return [
        DiscoveredSenderOut(
            address=r.address,
            display_name=r.display_name,
            count=r.discovered_count,
        )
        for r in rows
    ]


@router.post("", response_model=SenderOut, status_code=status.HTTP_201_CREATED)
@router.post(
    "/", response_model=SenderOut, status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_sender(
    payload: SenderCreate, db: Session = Depends(get_db)
) -> SenderOut:
    if db.get(Account, payload.account_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )
    if payload.vendor_id is not None and db.get(Vendor, payload.vendor_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
        )

    address = (payload.address or "").strip().lower()
    domain = (payload.domain or "").strip().lower() or None
    if not address and not domain:
        raise HTTPException(
            status_code=422,
            detail="Either address or domain is required",
        )
    # A domain-only allow-list entry is stored as a synthetic address so the
    # UNIQUE(account_id,address) key holds; the worker matches on domain too.
    if not address:
        address = f"@{domain}"
    if domain is None:
        domain = _domain_of(address)

    sender = Sender(
        account_id=payload.account_id,
        address=address,
        domain=domain,
        display_name=payload.display_name,
        vendor_id=payload.vendor_id,
        enabled=payload.enabled,
    )
    db.add(sender)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sender already exists for this account",
        )
    db.refresh(sender)
    return SenderOut.model_validate(sender, from_attributes=True)


@router.patch("/{sender_id}", response_model=SenderOut)
def update_sender(
    sender_id: int, payload: SenderUpdate, db: Session = Depends(get_db)
) -> SenderOut:
    sender = db.get(Sender, sender_id)
    if sender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    fields = payload.model_dump(exclude_unset=True)
    if "enabled" in fields and fields["enabled"] is not None:
        sender.enabled = fields["enabled"]
    if "vendor_id" in fields:
        vid = fields["vendor_id"]
        if vid is not None and db.get(Vendor, vid) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
            )
        sender.vendor_id = vid

    db.commit()
    db.refresh(sender)
    return SenderOut.model_validate(sender, from_attributes=True)


@router.delete("/{sender_id}", response_model=OkResponse)
def delete_sender(sender_id: int, db: Session = Depends(get_db)) -> OkResponse:
    sender = db.get(Sender, sender_id)
    if sender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(sender)
    db.commit()
    return OkResponse()
