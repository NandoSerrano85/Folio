"""SQLAlchemy 2.0 ORM models — the authoritative database contract.

The image ``source_date`` is the true acquisition date (the email ``Date``
header or the Drive ``createdTime``) and is the default sort key for the whole
library. ``sha256`` is computed on the ORIGINAL bytes, before any EXIF stamping,
so image identity is stable regardless of later metadata rewrites.

Enum types are defined with explicit PostgreSQL names so the Alembic migration
and the ORM stay in lockstep.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from folio_core.db import Base


# --------------------------------------------------------------------------- #
# Enums (names are referenced verbatim by the initial migration)
# --------------------------------------------------------------------------- #
class ProviderEnum(str, enum.Enum):
    gmail = "gmail"
    drive = "drive"


class SourceDateOriginEnum(str, enum.Enum):
    email_date = "email_date"
    drive_created = "drive_created"


class SourceTypeEnum(str, enum.Enum):
    drive = "drive"
    email = "email"


class CursorTypeEnum(str, enum.Enum):
    gmail_history_id = "gmail_history_id"
    drive_change_token = "drive_change_token"


class IngestStatusEnum(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"


class AssistStatusEnum(str, enum.Enum):
    """Lifecycle of a human-assist task for an un-automatable vendor email."""

    pending = "pending"
    in_progress = "in_progress"
    resolved = "resolved"
    failed = "failed"
    skipped = "skipped"


# Reusable column type objects. ``create_type=False`` because the enum types are
# created explicitly in migration 0001; native_enum keeps them as real PG enums.
provider_enum = SAEnum(
    ProviderEnum, name="provider_enum", native_enum=True, create_type=False
)
source_date_origin_enum = SAEnum(
    SourceDateOriginEnum,
    name="source_date_origin_enum",
    native_enum=True,
    create_type=False,
)
source_type_enum = SAEnum(
    SourceTypeEnum, name="source_type_enum", native_enum=True, create_type=False
)
cursor_type_enum = SAEnum(
    CursorTypeEnum, name="cursor_type_enum", native_enum=True, create_type=False
)
ingest_status_enum = SAEnum(
    IngestStatusEnum, name="ingest_status_enum", native_enum=True, create_type=False
)
assist_status_enum = SAEnum(
    AssistStatusEnum, name="assist_status_enum", native_enum=True, create_type=False
)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Account(Base):
    """A connected Google account (Gmail or Drive)."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("provider", "email", name="uq_accounts_provider_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[ProviderEnum] = mapped_column(provider_enum, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    token_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    sources: Mapped[list["ImageSource"]] = relationship(back_populates="account")
    senders: Mapped[list["Sender"]] = relationship(back_populates="account")
    sync_states: Mapped[list["SyncState"]] = relationship(back_populates="account")
    ingest_runs: Mapped[list["IngestRun"]] = relationship(back_populates="account")


class Vendor(Base):
    """A known image vendor/source (e.g. a print lab, a stock provider)."""

    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("adapter_key", name="uq_vendors_adapter_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(256), nullable=True)
    adapter_key: Mapped[str] = mapped_column(String(128), nullable=False)
    login_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sources: Mapped[list["ImageSource"]] = relationship(back_populates="vendor")
    senders: Mapped[list["Sender"]] = relationship(back_populates="vendor")
    credential: Mapped["VendorCredential | None"] = relationship(
        back_populates="vendor",
        uselist=False,
        cascade="all, delete-orphan",
    )
    assist_tasks: Mapped[list["AssistTask"]] = relationship(
        back_populates="vendor"
    )


class Image(Base):
    """A stored original image. Identity == sha256 of the original bytes."""

    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_source_date", "source_date"),
        Index("ix_images_source_date_desc", "source_date", postgresql_using="btree"),
        Index("ix_images_ingested_at", "ingested_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    ext: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # THE authoritative acquisition date and default library sort key.
    source_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_date_origin: Mapped[SourceDateOriginEnum] = mapped_column(
        source_date_origin_enum, nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    thumb_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    phash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sources: Mapped[list["ImageSource"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )
    folder_links: Mapped[list["FolderImage"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )


class ImageSource(Base):
    """Provenance of an image from a specific account/source. Idempotency key."""

    __tablename__ = "image_sources"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "source_type",
            "source_id",
            name="uq_image_sources_account_type_sourceid",
        ),
        Index("ix_image_sources_image_id", "image_id"),
        Index("ix_image_sources_account_id", "account_id"),
        Index("ix_image_sources_vendor_id", "vendor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[SourceTypeEnum] = mapped_column(
        source_type_enum, nullable=False
    )
    # Drive fileId or Gmail messageId — the idempotency key for re-ingest.
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True
    )
    vendor_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_sender: Mapped[str | None] = mapped_column(String(512), nullable=True)
    email_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    drive_folder_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_created_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drive_modified_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drive_owner: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    image: Mapped["Image"] = relationship(back_populates="sources")
    account: Mapped["Account"] = relationship(back_populates="sources")
    vendor: Mapped["Vendor | None"] = relationship(back_populates="sources")


class Sender(Base):
    """A Gmail sender on the per-account allow-list."""

    __tablename__ = "senders"
    __table_args__ = (
        UniqueConstraint("account_id", "address", name="uq_senders_account_address"),
        Index("ix_senders_account_id", "account_id"),
        Index("ix_senders_vendor_id", "vendor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped["Account"] = relationship(back_populates="senders")
    vendor: Mapped["Vendor | None"] = relationship(back_populates="senders")


class Folder(Base):
    """A virtual, nestable folder. Never moves files on disk."""

    __tablename__ = "folders"
    __table_args__ = (
        Index("ix_folders_parent_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    parent: Mapped["Folder | None"] = relationship(
        back_populates="children", remote_side="Folder.id"
    )
    children: Mapped[list["Folder"]] = relationship(back_populates="parent")
    image_links: Mapped[list["FolderImage"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan"
    )


class FolderImage(Base):
    """Membership of an image in a virtual folder."""

    __tablename__ = "folder_images"

    folder_id: Mapped[int] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True
    )
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    folder: Mapped["Folder"] = relationship(back_populates="image_links")
    image: Mapped["Image"] = relationship(back_populates="folder_links")


class SyncState(Base):
    """Per-account incremental sync cursor (Gmail history id / Drive change token)."""

    __tablename__ = "sync_state"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "cursor_type", name="uq_sync_state_account_cursor"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    cursor_type: Mapped[CursorTypeEnum] = mapped_column(
        cursor_type_enum, nullable=False
    )
    cursor_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_full_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_incremental_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped["Account"] = relationship(back_populates="sync_states")


class IngestRun(Base):
    """A single ingestion run. Holds resumability + reconciliation state."""

    __tablename__ = "ingest_runs"
    __table_args__ = (
        Index("ix_ingest_runs_account_id", "account_id"),
        Index("ix_ingest_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[IngestStatusEnum] = mapped_column(
        ingest_status_enum, nullable=False, default=IngestStatusEnum.running
    )
    last_page_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped["Account"] = relationship(back_populates="ingest_runs")


class VendorCredential(Base):
    """Fernet-encrypted login credentials for a vendor's browser download flow.

    Exactly one credential set per vendor. All ``*_enc`` columns hold Fernet
    ciphertext strings produced by ``folio_core.crypto.encrypt_value`` — never
    plaintext. ``extra_enc`` is encrypted JSON for cookies / 2FA / free-form
    notes the adapter may need.
    """

    __tablename__ = "vendor_credentials"
    __table_args__ = (
        UniqueConstraint("vendor_id", name="uq_vendor_credentials_vendor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    login_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    username_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    vendor: Mapped["Vendor"] = relationship(back_populates="credential")


class AssistTask(Base):
    """A human-assist task for a vendor email Folio could not auto-ingest.

    Created when the vendor browser flow cannot complete unattended (no adapter,
    captcha, login failure, ...). A human resolves it by uploading the original
    image, which the worker ingests through the common pipeline and links back
    via ``resolved_image_id``. Idempotent per (account, email message, url).
    """

    __tablename__ = "assist_tasks"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "email_message_id",
            "vendor_url",
            name="uq_assist_tasks_account_message_url",
        ),
        Index("ix_assist_tasks_status", "status"),
        Index("ix_assist_tasks_account_id", "account_id"),
        Index("ix_assist_tasks_vendor_id", "vendor_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True
    )
    email_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    email_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_sender: Mapped[str | None] = mapped_column(String(512), nullable=True)
    vendor_url: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AssistStatusEnum] = mapped_column(
        assist_status_enum,
        nullable=False,
        default=AssistStatusEnum.pending,
    )
    resolved_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped["Account"] = relationship()
    vendor: Mapped["Vendor | None"] = relationship(back_populates="assist_tasks")
    resolved_image: Mapped["Image | None"] = relationship()


class User(Base):
    """Portal login user. Seeded with one admin from env on first boot."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    argon2_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = [
    "ProviderEnum",
    "SourceDateOriginEnum",
    "SourceTypeEnum",
    "CursorTypeEnum",
    "IngestStatusEnum",
    "AssistStatusEnum",
    "Account",
    "Vendor",
    "Image",
    "ImageSource",
    "Sender",
    "Folder",
    "FolderImage",
    "SyncState",
    "IngestRun",
    "VendorCredential",
    "AssistTask",
    "User",
]
