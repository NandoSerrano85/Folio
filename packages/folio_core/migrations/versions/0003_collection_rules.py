"""collection_rules: auto-filing rules (folder + ANDed conditions)

Adds the ``collection_rules`` table backing the auto-filing feature: each
enabled rule targets a folder and carries a JSONB list of ``{field, op, value}``
conditions that are ANDed; images matching ALL conditions are auto-added to the
folder (see ``folio_core.rules.apply_collection_rules``).

Hand-written to stay rigorously consistent with ``folio_core.models``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-29

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collection_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column(
            "folder_id",
            sa.Integer(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_collection_rules_folder_id", "collection_rules", ["folder_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_collection_rules_folder_id", table_name="collection_rules"
    )
    op.drop_table("collection_rules")
