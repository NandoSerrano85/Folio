"""collection_rules v3: one condition + up to two actions (vendor / folder)

Reworks the auto-filing feature from the old "folder + ANDed JSONB conditions"
shape into the v3 model: each rule has ONE ``field``/``value`` condition (plus
``account_id`` for the ``account`` field) and up to two actions — assign a
``vendor_id`` and/or add to a ``folder_id``. See ``folio_core.rules``.

Since there is no production rules data, the upgrade simply DROPs the old
``collection_rules`` table and CREATEs the new shape; the downgrade recreates
the 0003 shape.

Hand-written to stay rigorously consistent with ``folio_core.models``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-29

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No production rules data — drop the old shape and recreate the v3 shape.
    op.drop_index(
        "ix_collection_rules_folder_id", table_name="collection_rules"
    )
    op.drop_table("collection_rules")

    op.create_table(
        "collection_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "vendor_id",
            sa.Integer(),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "folder_id",
            sa.Integer(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
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
    op.create_index(
        "ix_collection_rules_account_id", "collection_rules", ["account_id"]
    )
    op.create_index(
        "ix_collection_rules_vendor_id", "collection_rules", ["vendor_id"]
    )


def downgrade() -> None:
    # Recreate the 0003 shape (folder + ANDed JSONB conditions).
    op.drop_index(
        "ix_collection_rules_vendor_id", table_name="collection_rules"
    )
    op.drop_index(
        "ix_collection_rules_account_id", table_name="collection_rules"
    )
    op.drop_index(
        "ix_collection_rules_folder_id", table_name="collection_rules"
    )
    op.drop_table("collection_rules")

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
