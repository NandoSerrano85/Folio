"""Vendor catalog endpoints (list + create)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from folio_core.models import Vendor

from ..deps import get_db, require_user
from ..schemas import VendorCreate, VendorOut

router = APIRouter(
    prefix="/api/vendors",
    tags=["vendors"],
    dependencies=[Depends(require_user)],
)


@router.get("", response_model=list[VendorOut])
@router.get("/", response_model=list[VendorOut], include_in_schema=False)
def list_vendors(db: Session = Depends(get_db)) -> list[VendorOut]:
    rows = db.scalars(select(Vendor).order_by(Vendor.name.asc())).all()
    return [VendorOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("", response_model=VendorOut, status_code=status.HTTP_201_CREATED)
@router.post(
    "/", response_model=VendorOut, status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_vendor(
    payload: VendorCreate, db: Session = Depends(get_db)
) -> VendorOut:
    vendor = Vendor(
        name=payload.name.strip(),
        domain=(payload.domain or None),
        adapter_key=payload.adapter_key.strip(),
        login_required=payload.login_required,
        notes=payload.notes,
    )
    db.add(vendor)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A vendor with this adapter_key already exists",
        )
    db.refresh(vendor)
    return VendorOut.model_validate(vendor, from_attributes=True)
