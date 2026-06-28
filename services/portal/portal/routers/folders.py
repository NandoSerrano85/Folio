"""Virtual folder CRUD and membership management.

Folders are nestable (self-referential ``parent_id``) and never touch files on
disk. Membership lives in ``folder_images``. Re-parenting guards against cycles.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from folio_core.models import Folder, FolderImage, Image

from ..deps import get_db, require_user
from ..schemas import (
    FolderCreate,
    FolderImagesAdd,
    FolderNode,
    FolderOut,
    FolderUpdate,
    OkResponse,
)

router = APIRouter(
    prefix="/api/folders",
    tags=["folders"],
    dependencies=[Depends(require_user)],
)


def _get_folder_or_404(db: Session, folder_id: int) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return folder


def _would_create_cycle(db: Session, folder_id: int, new_parent_id: int) -> bool:
    """True if setting ``new_parent_id`` as parent of ``folder_id`` loops."""
    if new_parent_id == folder_id:
        return True
    seen: set[int] = set()
    current: int | None = new_parent_id
    while current is not None:
        if current == folder_id:
            return True
        if current in seen:
            break
        seen.add(current)
        parent = db.get(Folder, current)
        current = parent.parent_id if parent is not None else None
    return False


@router.get("", response_model=list[FolderNode])
@router.get("/", response_model=list[FolderNode], include_in_schema=False)
def folder_tree(db: Session = Depends(get_db)) -> list[FolderNode]:
    folders = db.scalars(
        select(Folder).order_by(Folder.sort_order.asc(), Folder.name.asc())
    ).all()

    counts = dict(
        db.execute(
            select(FolderImage.folder_id, func.count(FolderImage.image_id)).group_by(
                FolderImage.folder_id
            )
        ).all()
    )

    nodes: dict[int, FolderNode] = {
        f.id: FolderNode(
            id=f.id,
            name=f.name,
            parent_id=f.parent_id,
            sort_order=f.sort_order,
            image_count=int(counts.get(f.id, 0)),
            children=[],
        )
        for f in folders
    }

    roots: list[FolderNode] = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id is not None and f.parent_id in nodes:
            nodes[f.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
@router.post(
    "/", response_model=FolderOut, status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_folder(
    payload: FolderCreate, db: Session = Depends(get_db)
) -> FolderOut:
    if payload.parent_id is not None:
        _get_folder_or_404(db, payload.parent_id)
    folder = Folder(name=payload.name.strip(), parent_id=payload.parent_id)
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.patch("/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: int, payload: FolderUpdate, db: Session = Depends(get_db)
) -> FolderOut:
    folder = _get_folder_or_404(db, folder_id)

    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        folder.name = fields["name"].strip()
    if "parent_id" in fields:
        new_parent = fields["parent_id"]
        if new_parent is not None:
            _get_folder_or_404(db, new_parent)
            if _would_create_cycle(db, folder_id, new_parent):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Re-parenting would create a cycle",
                )
        folder.parent_id = new_parent

    db.commit()
    db.refresh(folder)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.delete("/{folder_id}", response_model=OkResponse)
def delete_folder(folder_id: int, db: Session = Depends(get_db)) -> OkResponse:
    _get_folder_or_404(db, folder_id)
    # Use a Core DELETE so PostgreSQL's ON DELETE CASCADE removes the whole
    # subtree and membership rows. An ORM delete would instead NULL the
    # children's parent_id (the relationship has no passive_deletes), wrongly
    # promoting the subtree to roots.
    db.execute(delete(Folder).where(Folder.id == folder_id))
    db.commit()
    return OkResponse()


@router.post("/{folder_id}/images", response_model=OkResponse)
def add_images(
    folder_id: int, payload: FolderImagesAdd, db: Session = Depends(get_db)
) -> OkResponse:
    _get_folder_or_404(db, folder_id)
    ids = sorted({i for i in payload.image_ids if i})
    if not ids:
        return OkResponse()

    valid = set(
        db.scalars(select(Image.id).where(Image.id.in_(ids))).all()
    )
    existing = set(
        db.scalars(
            select(FolderImage.image_id).where(
                FolderImage.folder_id == folder_id,
                FolderImage.image_id.in_(ids),
            )
        ).all()
    )
    now = datetime.now(timezone.utc)
    for image_id in ids:
        if image_id in valid and image_id not in existing:
            db.add(
                FolderImage(
                    folder_id=folder_id, image_id=image_id, added_at=now
                )
            )
    db.commit()
    return OkResponse()


@router.delete("/{folder_id}/images/{image_id}", response_model=OkResponse)
def remove_image(
    folder_id: int, image_id: int, db: Session = Depends(get_db)
) -> OkResponse:
    _get_folder_or_404(db, folder_id)
    db.execute(
        delete(FolderImage).where(
            FolderImage.folder_id == folder_id,
            FolderImage.image_id == image_id,
        )
    )
    db.commit()
    return OkResponse()
