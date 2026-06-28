"""initial schema

Creates every enum, table, index, and constraint defined in
``folio_core.models``. Hand-written to stay rigorously consistent with the ORM.

Revision ID: 0001
Revises:
Create Date: 2026-06-27

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --------------------------------------------------------------------------- #
# Enum type definitions (created explicitly; columns reference create_type=False)
# --------------------------------------------------------------------------- #
provider_enum = postgresql.ENUM(
    "gmail", "drive", name="provider_enum", create_type=False
)
source_date_origin_enum = postgresql.ENUM(
    "email_date", "drive_created", name="source_date_origin_enum", create_type=False
)
source_type_enum = postgresql.ENUM(
    "drive", "email", name="source_type_enum", create_type=False
)
cursor_type_enum = postgresql.ENUM(
    "gmail_history_id",
    "drive_change_token",
    name="cursor_type_enum",
    create_type=False,
)
ingest_status_enum = postgresql.ENUM(
    "running",
    "completed",
    "failed",
    "interrupted",
    name="ingest_status_enum",
    create_type=False,
)

_ALL_ENUMS = (
    provider_enum,
    source_date_origin_enum,
    source_type_enum,
    cursor_type_enum,
    ingest_status_enum,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    # ----------------------------------------------------------------- #
    # accounts
    # ----------------------------------------------------------------- #
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", provider_enum, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="active"
        ),
        sa.Column("token_ref", sa.String(length=512), nullable=True),
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
        sa.UniqueConstraint(
            "provider", "email", name="uq_accounts_provider_email"
        ),
    )

    # ----------------------------------------------------------------- #
    # vendors
    # ----------------------------------------------------------------- #
    op.create_table(
        "vendors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("domain", sa.String(length=256), nullable=True),
        sa.Column("adapter_key", sa.String(length=128), nullable=False),
        sa.Column(
            "login_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("adapter_key", name="uq_vendors_adapter_key"),
    )

    # ----------------------------------------------------------------- #
    # images
    # ----------------------------------------------------------------- #
    op.create_table(
        "images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("stored_path", sa.String(length=1024), nullable=False),
        sa.Column("ext", sa.String(length=16), nullable=True),
        sa.Column("mime", sa.String(length=128), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("source_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_date_origin", source_date_origin_enum, nullable=False
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("thumb_path", sa.String(length=1024), nullable=True),
        sa.Column("phash", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("sha256", name="uq_images_sha256"),
        sa.UniqueConstraint("stored_path", name="uq_images_stored_path"),
    )
    op.create_index("ix_images_source_date", "images", ["source_date"])
    op.create_index(
        "ix_images_source_date_desc",
        "images",
        [sa.text("source_date DESC")],
    )
    op.create_index("ix_images_ingested_at", "images", ["ingested_at"])

    # ----------------------------------------------------------------- #
    # image_sources
    # ----------------------------------------------------------------- #
    op.create_table(
        "image_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "image_id",
            sa.Integer(),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", source_type_enum, nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column(
            "vendor_id",
            sa.Integer(),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vendor_url", sa.Text(), nullable=True),
        sa.Column("email_subject", sa.Text(), nullable=True),
        sa.Column("email_sender", sa.String(length=512), nullable=True),
        sa.Column("email_message_id", sa.String(length=512), nullable=True),
        sa.Column("drive_folder_path", sa.Text(), nullable=True),
        sa.Column("drive_created_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drive_modified_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drive_owner", sa.String(length=512), nullable=True),
        sa.Column("raw_meta", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id",
            "source_type",
            "source_id",
            name="uq_image_sources_account_type_sourceid",
        ),
    )
    op.create_index(
        "ix_image_sources_image_id", "image_sources", ["image_id"]
    )
    op.create_index(
        "ix_image_sources_account_id", "image_sources", ["account_id"]
    )
    op.create_index(
        "ix_image_sources_vendor_id", "image_sources", ["vendor_id"]
    )

    # ----------------------------------------------------------------- #
    # senders
    # ----------------------------------------------------------------- #
    op.create_table(
        "senders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("address", sa.String(length=512), nullable=False),
        sa.Column("domain", sa.String(length=256), nullable=True),
        sa.Column("display_name", sa.String(length=512), nullable=True),
        sa.Column(
            "vendor_id",
            sa.Integer(),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "discovered_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "account_id", "address", name="uq_senders_account_address"
        ),
    )
    op.create_index("ix_senders_account_id", "senders", ["account_id"])
    op.create_index("ix_senders_vendor_id", "senders", ["vendor_id"])

    # ----------------------------------------------------------------- #
    # folders
    # ----------------------------------------------------------------- #
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])

    # ----------------------------------------------------------------- #
    # folder_images (composite PK)
    # ----------------------------------------------------------------- #
    op.create_table(
        "folder_images",
        sa.Column(
            "folder_id",
            sa.Integer(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "image_id",
            sa.Integer(),
            sa.ForeignKey("images.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ----------------------------------------------------------------- #
    # sync_state
    # ----------------------------------------------------------------- #
    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cursor_type", cursor_type_enum, nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("last_full_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_incremental_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id", "cursor_type", name="uq_sync_state_account_cursor"
        ),
    )

    # ----------------------------------------------------------------- #
    # ingest_runs
    # ----------------------------------------------------------------- #
    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            ingest_status_enum,
            nullable=False,
            server_default="running",
        ),
        sa.Column("last_page_token", sa.Text(), nullable=True),
        sa.Column("source_count", sa.Integer(), nullable=True),
        sa.Column(
            "items_seen", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "items_imported",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "items_skipped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "items_failed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("errors", postgresql.JSONB(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ingest_runs_account_id", "ingest_runs", ["account_id"])
    op.create_index("ix_ingest_runs_status", "ingest_runs", ["status"])

    # ----------------------------------------------------------------- #
    # users
    # ----------------------------------------------------------------- #
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("argon2_hash", sa.String(length=512), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_index("ix_ingest_runs_status", table_name="ingest_runs")
    op.drop_index("ix_ingest_runs_account_id", table_name="ingest_runs")
    op.drop_table("ingest_runs")
    op.drop_table("sync_state")
    op.drop_table("folder_images")
    op.drop_index("ix_folders_parent_id", table_name="folders")
    op.drop_table("folders")
    op.drop_index("ix_senders_vendor_id", table_name="senders")
    op.drop_index("ix_senders_account_id", table_name="senders")
    op.drop_table("senders")
    op.drop_index("ix_image_sources_vendor_id", table_name="image_sources")
    op.drop_index("ix_image_sources_account_id", table_name="image_sources")
    op.drop_index("ix_image_sources_image_id", table_name="image_sources")
    op.drop_table("image_sources")
    op.drop_index("ix_images_ingested_at", table_name="images")
    op.drop_index("ix_images_source_date_desc", table_name="images")
    op.drop_index("ix_images_source_date", table_name="images")
    op.drop_table("images")
    op.drop_table("vendors")
    op.drop_table("accounts")

    bind = op.get_bind()
    for enum in reversed(_ALL_ENUMS):
        enum.drop(bind, checkfirst=True)
