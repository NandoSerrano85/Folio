"""Collection-rule CRUD and an on-demand apply endpoint.

A collection rule maps a target folder to a list of ANDed conditions; matching
images are auto-filed into the folder (idempotent via ``folder_images`` PK).
Field/op/value tuples are validated server-side with
:func:`folio_core.rules.validate_conditions` before they ever reach the DB, since
the pydantic schema only type-checks ``field``/``op`` as free strings.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from folio_core.models import CollectionRule, Folder, Image
from folio_core.rules import _match_clause, apply_collection_rules, validate_conditions

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


def _ensure_folder(db: Session, folder_id: int) -> None:
    if db.get(Folder, folder_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"folder_id {folder_id} does not exist",
        )


def _validate_or_422(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return validate_conditions(conditions)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _conditions_to_dicts(conditions: Any) -> list[dict[str, Any]]:
    """Dump a list of ``CollectionRuleCondition`` to plain dicts for validation."""
    return [c.model_dump() for c in conditions]


def _match_count(db: Session, conditions: list[dict[str, Any]]) -> int:
    """Count images matching a (pre-validated) conditions list.

    An empty / unsatisfiable clause matches nothing (never the whole library).
    """
    clause = _match_clause(conditions)
    if clause is None:
        return 0
    return int(
        db.scalar(select(func.count()).select_from(Image).where(clause)) or 0
    )


def _to_out(
    db: Session, rule: CollectionRule, folder_name: str | None
) -> CollectionRuleOut:
    conditions = rule.conditions or []
    return CollectionRuleOut.model_validate(
        {
            "id": rule.id,
            "name": rule.name,
            "folder_id": rule.folder_id,
            "folder_name": folder_name,
            "enabled": rule.enabled,
            "conditions": conditions,
            # Omitted: a per-rule COUNT with leading-wildcard ILIKE is a full
            # scan, which made the rules list an N+1 of seq scans on a large
            # library. "Apply now" reports the real impact (total filed).
            "match_count": None,
        }
    )


@router.get("", response_model=list[CollectionRuleOut])
@router.get("/", response_model=list[CollectionRuleOut], include_in_schema=False)
def list_rules(db: Session = Depends(get_db)) -> list[CollectionRuleOut]:
    rows = db.execute(
        select(CollectionRule, Folder.name)
        .outerjoin(Folder, Folder.id == CollectionRule.folder_id)
        .order_by(CollectionRule.id.asc())
    ).all()
    return [_to_out(db, rule, folder_name) for rule, folder_name in rows]


@router.post(
    "", response_model=CollectionRuleOut, status_code=status.HTTP_201_CREATED
)
@router.post(
    "/",
    response_model=CollectionRuleOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_rule(
    payload: CollectionRuleCreate, db: Session = Depends(get_db)
) -> CollectionRuleOut:
    _ensure_folder(db, payload.folder_id)
    conditions = _validate_or_422(_conditions_to_dicts(payload.conditions))
    name = payload.name.strip() if payload.name else None
    rule = CollectionRule(
        name=name or None,
        folder_id=payload.folder_id,
        enabled=payload.enabled,
        conditions=conditions,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    folder = db.get(Folder, rule.folder_id)
    return _to_out(db, rule, folder.name if folder is not None else None)


@router.patch("/{rule_id}", response_model=CollectionRuleOut)
def update_rule(
    rule_id: int, payload: CollectionRuleUpdate, db: Session = Depends(get_db)
) -> CollectionRuleOut:
    rule = _get_rule_or_404(db, rule_id)
    fields = payload.model_dump(exclude_unset=True)

    if "name" in fields:
        name = fields["name"]
        rule.name = (name.strip() or None) if name else None
    if "folder_id" in fields and fields["folder_id"] is not None:
        _ensure_folder(db, fields["folder_id"])
        rule.folder_id = fields["folder_id"]
    if "enabled" in fields and fields["enabled"] is not None:
        rule.enabled = fields["enabled"]
    if "conditions" in fields and fields["conditions"] is not None:
        rule.conditions = _validate_or_422(
            _conditions_to_dicts(payload.conditions)
        )

    db.commit()
    db.refresh(rule)
    folder = db.get(Folder, rule.folder_id)
    return _to_out(db, rule, folder.name if folder is not None else None)


@router.delete("/{rule_id}", response_model=OkResponse)
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> OkResponse:
    rule = _get_rule_or_404(db, rule_id)
    db.delete(rule)
    db.commit()
    return OkResponse()


@router.post("/apply")
def apply_rules(db: Session = Depends(get_db)) -> dict[str, Any]:
    """File existing images into folders for every enabled rule, right now."""
    applied = apply_collection_rules(db)
    db.commit()
    return {
        "applied": applied,
        "total_added": int(sum(applied.values())),
    }
