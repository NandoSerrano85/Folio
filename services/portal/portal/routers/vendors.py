"""Vendor catalog endpoints (list, create, update, delete)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from folio_core.models import ImageSource, Vendor
from folio_core.vendors import slugify_adapter_key

from ..deps import get_db, require_user
from ..schemas import OkResponse, VendorCreate, VendorOut, VendorUpdate

router = APIRouter(
    prefix="/api/vendors",
    tags=["vendors"],
    dependencies=[Depends(require_user)],
)


def _image_count_subquery():
    """Subquery: distinct image count per vendor from image_sources."""
    return (
        select(
            ImageSource.vendor_id.label("vendor_id"),
            func.count(func.distinct(ImageSource.image_id)).label("image_count"),
        )
        .group_by(ImageSource.vendor_id)
        .subquery()
    )


def _vendor_image_count(db: Session, vendor_id: int) -> int:
    return (
        db.scalar(
            select(func.count(func.distinct(ImageSource.image_id))).where(
                ImageSource.vendor_id == vendor_id
            )
        )
        or 0
    )


@router.get("", response_model=list[VendorOut])
@router.get("/", response_model=list[VendorOut], include_in_schema=False)
def list_vendors(db: Session = Depends(get_db)) -> list[VendorOut]:
    counts = _image_count_subquery()
    image_count = func.coalesce(counts.c.image_count, 0)
    rows = db.execute(
        select(Vendor, image_count)
        .outerjoin(counts, counts.c.vendor_id == Vendor.id)
        .order_by(Vendor.name.asc())
    ).all()
    return [
        VendorOut.model_validate(
            {
                "id": vendor.id,
                "name": vendor.name,
                "domain": vendor.domain,
                "adapter_key": vendor.adapter_key,
                "login_required": vendor.login_required,
                "notes": vendor.notes,
                "image_count": count,
            }
        )
        for vendor, count in rows
    ]


@router.post("", response_model=VendorOut, status_code=status.HTTP_201_CREATED)
@router.post(
    "/", response_model=VendorOut, status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_vendor(
    payload: VendorCreate, db: Session = Depends(get_db)
) -> VendorOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be blank",
        )
    adapter_key = (payload.adapter_key or "").strip() or slugify_adapter_key(name)
    adapter_key = adapter_key[:128].rstrip("-") or "vendor"
    vendor = Vendor(
        name=name,
        domain=(payload.domain or None),
        adapter_key=adapter_key,
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
    return VendorOut.model_validate(
        {
            "id": vendor.id,
            "name": vendor.name,
            "domain": vendor.domain,
            "adapter_key": vendor.adapter_key,
            "login_required": vendor.login_required,
            "notes": vendor.notes,
            "image_count": 0,
        }
    )


@router.patch("/{vendor_id}", response_model=VendorOut)
def update_vendor(
    vendor_id: int, payload: VendorUpdate, db: Session = Depends(get_db)
) -> VendorOut:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
        )
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        new_name = fields["name"].strip()
        if not new_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="name must not be blank",
            )
        vendor.name = new_name
    if "domain" in fields:
        vendor.domain = fields["domain"] or None
    if "notes" in fields:
        vendor.notes = fields["notes"]
    if "login_required" in fields and fields["login_required"] is not None:
        vendor.login_required = fields["login_required"]
    db.commit()
    db.refresh(vendor)
    return VendorOut.model_validate(
        {
            "id": vendor.id,
            "name": vendor.name,
            "domain": vendor.domain,
            "adapter_key": vendor.adapter_key,
            "login_required": vendor.login_required,
            "notes": vendor.notes,
            "image_count": _vendor_image_count(db, vendor.id),
        }
    )


@router.delete("/{vendor_id}", response_model=OkResponse)
def delete_vendor(
    vendor_id: int, db: Session = Depends(get_db)
) -> OkResponse:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
        )
    db.delete(vendor)
    db.commit()
    return OkResponse()
