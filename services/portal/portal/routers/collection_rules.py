"""Collection-rule CRUD and an on-demand apply endpoint (v3).

A v3 collection rule is ONE condition + up to two actions: assign a vendor to
matching images' sources and/or add them to a folder. The condition is one of
``sender``/``domain``/``filename``/``subject`` (a substring ``value``) or
``account`` (an ``account_id`` equality). Rules are validated server-side with
:func:`folio_core.rules.validate_rule` before they touch the DB, since the
pydantic schema only type-checks ``field`` as a free string. Matching/filing is
delegated to :mod:`folio_core.rules` (bound params, ILIKE wildcards escaped).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from folio_core.models import Account, CollectionRule, Folder, Vendor
from folio_core.rules import (
    TEXT_FIELDS,
    apply_collection_rules,
    count_matches,
    match_count,
    validate_rule,
)

from ..deps import get_db, require_user
from ..schemas import (
    CollectionRuleCreate,
    CollectionRuleOut,
    CollectionRuleUpdate,
    OkResponse,
)

router = APIRouter(
    prefix="/api/collection-rules",
    tags=["collection-rules"],
    dependencies=[Depends(require_user)],
)


def _get_rule_or_404(db: Session, rule_id: int) -> CollectionRule:
    rule = db.get(CollectionRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Collection rule not found"
        )
    return rule


def _422(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _validate_or_422(
    field: str | None,
    value: str | None,
    account_id: int | None,
    vendor_id: int | None,
    folder_id: int | None,
) -> None:
    try:
        validate_rule(field, value, account_id, vendor_id, folder_id)
    except ValueError as exc:
        raise _422(str(exc)) from exc


def _ensure_fks(
    db: Session,
    account_id: int | None,
    vendor_id: int | None,
    folder_id: int | None,
) -> None:
    """Reject references to rows that do not exist (422)."""
    if account_id is not None and db.get(Account, account_id) is None:
        raise _422(f"account_id {account_id} does not exist")
    if vendor_id is not None and db.get(Vendor, vendor_id) is None:
        raise _422(f"vendor_id {vendor_id} does not exist")
    if folder_id is not None and db.get(Folder, folder_id) is None:
        raise _422(f"folder_id {folder_id} does not exist")


def _normalize_value(field: str, value: str | None) -> str | None:
    """For text fields keep a stripped value; for ``account`` force null."""
    if field in TEXT_FIELDS:
        return value.strip() if isinstance(value, str) else value
    return None


def _names_for(
    db: Session, rule: CollectionRule
) -> tuple[str | None, str | None, str | None]:
    account_name = vendor_name = folder_name = None
    if rule.account_id is not None:
        account = db.get(Account, rule.account_id)
        account_name = account.email if account is not None else None
    if rule.vendor_id is not None:
        vendor = db.get(Vendor, rule.vendor_id)
        vendor_name = vendor.name if vendor is not None else None
    if rule.folder_id is not None:
        folder = db.get(Folder, rule.folder_id)
        folder_name = folder.name if folder is not None else None
    return account_name, vendor_name, folder_name


def _to_out(
    db: Session,
    rule: CollectionRule,
    account_name: str | None,
    vendor_name: str | None,
    folder_name: str | None,
) -> CollectionRuleOut:
    return CollectionRuleOut.model_validate(
        {
            "id": rule.id,
            "field": rule.field,
            "value": rule.value,
            "account_id": rule.account_id,
            "account_name": account_name,
            "vendor_id": rule.vendor_id,
            "vendor_name": vendor_name,
            "folder_id": rule.folder_id,
            "folder_name": folder_name,
            "enabled": rule.enabled,
            "match_count": match_count(db, rule),
        }
    )


@router.get("", response_model=list[CollectionRuleOut])
@router.get("/", response_model=list[CollectionRuleOut], include_in_schema=False)
def list_rules(db: Session = Depends(get_db)) -> list[CollectionRuleOut]:
    rows = db.execute(
        select(CollectionRule, Account.email, Vendor.name, Folder.name)
        .outerjoin(Account, Account.id == CollectionRule.account_id)
        .outerjoin(Vendor, Vendor.id == CollectionRule.vendor_id)
        .outerjoin(Folder, Folder.id == CollectionRule.folder_id)
        .order_by(CollectionRule.id.asc())
    ).all()
    return [
        _to_out(db, rule, account_name, vendor_name, folder_name)
        for rule, account_name, vendor_name, folder_name in rows
    ]


@router.get("/match-count")
def preview_match_count(
    field: str,
    value: str | None = None,
    account_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Live builder preview: how many images a DRAFT condition matches, before
    it is saved. An unusable/empty condition matches nothing (returns 0). This
    is a literal path, so it never collides with the ``/{rule_id}`` routes.
    """
    return {"match_count": count_matches(db, field=field, value=value, account_id=account_id)}


@router.post("", response_model=CollectionRuleOut, status_code=status.HTTP_201_CREATED)
@router.post(
    "/",
    response_model=CollectionRuleOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_rule(
    payload: CollectionRuleCreate, db: Session = Depends(get_db)
) -> CollectionRuleOut:
    _validate_or_422(
        payload.field,
        payload.value,
        payload.account_id,
        payload.vendor_id,
        payload.folder_id,
    )
    _ensure_fks(db, payload.account_id, payload.vendor_id, payload.folder_id)

    rule = CollectionRule(
        field=payload.field,
        value=_normalize_value(payload.field, payload.value),
        account_id=payload.account_id if payload.field == "account" else None,
        vendor_id=payload.vendor_id,
        folder_id=payload.folder_id,
        enabled=payload.enabled,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    account_name, vendor_name, folder_name = _names_for(db, rule)
    return _to_out(db, rule, account_name, vendor_name, folder_name)


@router.patch("/{rule_id}", response_model=CollectionRuleOut)
def update_rule(
    rule_id: int, payload: CollectionRuleUpdate, db: Session = Depends(get_db)
) -> CollectionRuleOut:
    rule = _get_rule_or_404(db, rule_id)
    fields = payload.model_dump(exclude_unset=True)

    # Merge the patch with the current row, then validate the resulting rule.
    field = fields.get("field", rule.field)
    value = fields.get("value", rule.value)
    account_id = fields.get("account_id", rule.account_id)
    vendor_id = fields.get("vendor_id", rule.vendor_id)
    folder_id = fields.get("folder_id", rule.folder_id)
    enabled = fields.get("enabled", rule.enabled)

    _validate_or_422(field, value, account_id, vendor_id, folder_id)
    _ensure_fks(db, account_id, vendor_id, folder_id)

    rule.field = field
    rule.value = _normalize_value(field, value)
    rule.account_id = account_id if field == "account" else None
    rule.vendor_id = vendor_id
    rule.folder_id = folder_id
    if enabled is not None:
        rule.enabled = enabled

    db.commit()
    db.refresh(rule)

    account_name, vendor_name, folder_name = _names_for(db, rule)
    return _to_out(db, rule, account_name, vendor_name, folder_name)


@router.delete("/{rule_id}", response_model=OkResponse)
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> OkResponse:
    rule = _get_rule_or_404(db, rule_id)
    db.delete(rule)
    db.commit()
    return OkResponse()


@router.post("/apply")
def apply_rules(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Apply every enabled rule to the existing library, right now."""
    applied = apply_collection_rules(db)
    db.commit()
    total_filed = sum(counts["filed"] for counts in applied.values())
    total_vendored = sum(counts["vendored"] for counts in applied.values())
    return {
        "applied": applied,
        "total_filed": int(total_filed),
        "total_vendored": int(total_vendored),
    }
