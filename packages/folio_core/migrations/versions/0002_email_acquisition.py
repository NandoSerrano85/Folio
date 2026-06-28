"""email acquisition: vendor_credentials + assist_tasks

Adds the Phase-2 email/vendor-browser acquisition tables:

  * ``vendor_credentials`` — one Fernet-encrypted credential set per vendor.
  * ``assist_tasks`` — human-assist queue for emails Folio cannot auto-ingest,
    plus the ``assist_status_enum`` PG enum backing its ``status`` column.

Hand-written to stay rigorously consistent with ``folio_core.models``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-28

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --------------------------------------------------------------------------- #
# Enum type (created explicitly; the column references create_type=False)
# --------------------------------------------------------------------------- #
assist_status_enum = postgresql.ENUM(
    "pending",
    "in_progress",
    "resolved",
    "failed",
    "skipped",
    name="assist_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    assist_status_enum.create(bind, checkfirst=True)

    # ----------------------------------------------------------------- #
    # vendor_credentials (one cred set per vendor)
    # ----------------------------------------------------------------- #
    op.create_table(
        "vendor_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.Integer(),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("login_url", sa.Text(), nullable=True),
        sa.Column("username_enc", sa.Text(), nullable=True),
        sa.Column("secret_enc", sa.Text(), nullable=True),
        sa.Column("extra_enc", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "vendor_id", name="uq_vendor_credentials_vendor_id"
        ),
    )

    # ----------------------------------------------------------------- #
    # assist_tasks
    # ----------------------------------------------------------------- #
    op.create_table(
        "assist_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vendor_id",
            sa.Integer(),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email_message_id", sa.Text(), nullable=False),
        sa.Column("email_subject", sa.Text(), nullable=True),
        sa.Column("email_sender", sa.String(length=512), nullable=True),
        sa.Column("vendor_url", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            assist_status_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "resolved_image_id",
            sa.Integer(),
            sa.ForeignKey("images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "account_id",
            "email_message_id",
            "vendor_url",
            name="uq_assist_tasks_account_message_url",
        ),
    )
    op.create_index("ix_assist_tasks_status", "assist_tasks", ["status"])
    op.create_index("ix_assist_tasks_account_id", "assist_tasks", ["account_id"])
    op.create_index("ix_assist_tasks_vendor_id", "assist_tasks", ["vendor_id"])


def downgrade() -> None:
    op.drop_index("ix_assist_tasks_vendor_id", table_name="assist_tasks")
    op.drop_index("ix_assist_tasks_account_id", table_name="assist_tasks")
    op.drop_index("ix_assist_tasks_status", table_name="assist_tasks")
    op.drop_table("assist_tasks")
    op.drop_table("vendor_credentials")

    bind = op.get_bind()
    assist_status_enum.drop(bind, checkfirst=True)
