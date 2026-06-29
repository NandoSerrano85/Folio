"""Pydantic v2 request/response models for the portal JSON API.

These mirror the frozen HTTP contract. Response models use plain field types
(no ORM coupling) so routers build them from explicit dicts/values, keeping the
wire shape stable regardless of ORM internals.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    """Login body is EITHER {token} OR {username, password}.

    Exactly one mode must be supplied: a bare access ``token`` XOR a
    ``username``+``password`` pair. All three fields are optional at the type
    level; the ``model_validator`` enforces the XOR and rejects empty/ambiguous
    bodies with a 422.
    """

    token: str | None = Field(default=None, min_length=1, max_length=1024)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "LoginRequest":
        has_token = bool(self.token)
        has_credentials = bool(self.username) and bool(self.password)
        if has_token == has_credentials:
            # Both supplied, or neither -> ambiguous/empty.
            raise ValueError(
                "Provide either a token, or a username and password (not both)."
            )
        return self


class MeResponse(BaseModel):
    username: str


class OkResponse(BaseModel):
    status: str = "ok"


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
class ImageListItem(BaseModel):
    id: int
    filename: str | None = None
    source_date: datetime
    source_date_origin: str
    vendor: str | None = None
    account: str | None = None
    thumb_url: str
    ext: str | None = None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None


class ImageListResponse(BaseModel):
    items: list[ImageListItem]
    total: int
    page: int
    page_size: int
    pages: int


class ImageSourceOut(BaseModel):
    id: int
    source_type: str
    source_id: str
    account_id: int
    account: str | None = None
    vendor_id: int | None = None
    vendor: str | None = None
    vendor_url: str | None = None
    email_subject: str | None = None
    email_sender: str | None = None
    email_message_id: str | None = None
    drive_folder_path: str | None = None
    drive_created_time: datetime | None = None
    drive_modified_time: datetime | None = None
    drive_owner: str | None = None
    created_at: datetime | None = None


class ImageDetail(BaseModel):
    id: int
    sha256: str
    filename: str | None = None
    stored_path: str
    ext: str | None = None
    mime: str | None = None
    bytes: int | None = None
    width: int | None = None
    height: int | None = None
    source_date: datetime
    source_date_origin: str
    ingested_at: datetime | None = None
    thumb_url: str
    file_url: str
    sources: list[ImageSourceOut] = []


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #
class FolderNode(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    sort_order: int = 0
    image_count: int = 0
    children: list["FolderNode"] = []


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    parent_id: int | None = None


class FolderImagesAdd(BaseModel):
    image_ids: list[int] = Field(default_factory=list)


class FolderImagesRemove(BaseModel):
    image_ids: list[int] = Field(default_factory=list)


class FolderImagesRemoveResponse(BaseModel):
    removed: int


class FolderOut(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    sort_order: int = 0


# --------------------------------------------------------------------------- #
# Collection rules (auto-filing)
# --------------------------------------------------------------------------- #
class CollectionRuleCreate(BaseModel):
    """Create a v3 rule: ONE condition (field/value or account) + up to two
    actions (vendor and/or folder). Validated server-side via
    ``folio_core.rules.validate_rule`` before persisting."""

    field: str = Field(min_length=1, max_length=32)
    value: str | None = Field(default=None, max_length=512)
    account_id: int | None = None
    vendor_id: int | None = None
    folder_id: int | None = None
    enabled: bool = True


class CollectionRuleUpdate(BaseModel):
    field: str | None = Field(default=None, min_length=1, max_length=32)
    value: str | None = Field(default=None, max_length=512)
    account_id: int | None = None
    vendor_id: int | None = None
    folder_id: int | None = None
    enabled: bool | None = None


class CollectionRuleOut(BaseModel):
    id: int
    field: str
    value: str | None = None
    account_id: int | None = None
    account_name: str | None = None
    vendor_id: int | None = None
    vendor_name: str | None = None
    folder_id: int | None = None
    folder_name: str | None = None
    enabled: bool = True
    match_count: int | None = None


# --------------------------------------------------------------------------- #
# Senders
# --------------------------------------------------------------------------- #
class SenderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    address: str
    domain: str | None = None
    display_name: str | None = None
    vendor_id: int | None = None
    enabled: bool = False
    discovered_count: int = 0
    last_seen_at: datetime | None = None


class DiscoveredSenderOut(BaseModel):
    address: str
    display_name: str | None = None
    count: int


class SenderCreate(BaseModel):
    account_id: int
    address: str | None = Field(default=None, max_length=512)
    domain: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=512)
    vendor_id: int | None = None
    enabled: bool = True


class SenderUpdate(BaseModel):
    enabled: bool | None = None
    vendor_id: int | None = None


# --------------------------------------------------------------------------- #
# Vendors
# --------------------------------------------------------------------------- #
class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    domain: str | None = None
    adapter_key: str
    login_required: bool = False
    notes: str | None = None
    image_count: int = 0


class VendorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    domain: str | None = Field(default=None, max_length=256)
    adapter_key: str | None = Field(default=None, max_length=128)
    login_required: bool = False
    notes: str | None = None


class VendorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    domain: str | None = Field(default=None, max_length=256)
    notes: str | None = None
    login_required: bool | None = None


class VendorRef(BaseModel):
    """Minimal vendor reference for bulk-operation responses."""

    id: int
    name: str


class SetImagesVendorRequest(BaseModel):
    """Bulk set-vendor body.

    Targets the images in ``image_ids``. Supply EITHER an existing
    ``vendor_id`` OR a ``vendor_name`` (get-or-created server-side via
    ``folio_core.vendors.get_or_create_vendor``). When both are null the
    images' sources have their vendor cleared.
    """

    image_ids: list[int] = Field(default_factory=list)
    vendor_id: int | None = None
    vendor_name: str | None = Field(default=None, max_length=256)


class SetImagesVendorResponse(BaseModel):
    updated_images: int
    updated_sources: int
    vendor: VendorRef | None = None


class VendorCredentialIn(BaseModel):
    """Write-only vendor login credentials.

    ``password`` is the secret stored Fernet-encrypted server-side via
    ``folio_core.credentials.set_vendor_credentials``. This model is NEVER used
    as a response, so the secret never leaves the server. All fields optional so
    a caller can patch just the login_url or just the password.
    """

    login_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)


class VendorCredentialStatus(BaseModel):
    """Read model for a vendor's stored credentials.

    Reports only whether credentials exist plus the non-secret ``login_url`` /
    ``username``. The ``password`` is NEVER returned.
    """

    has_credentials: bool = False
    login_url: str | None = None
    username: str | None = None


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
class AccountOut(BaseModel):
    id: int
    provider: str
    email: str
    label: str | None = None
    status: str
    image_count: int = 0
    source_count: int = 0


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
class DownloadRequest(BaseModel):
    image_ids: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
class CountByName(BaseModel):
    name: str
    count: int


class StatsResponse(BaseModel):
    total_images: int
    by_account: list[CountByName]
    by_vendor: list[CountByName]
    latest_source_date: datetime | None = None
    library_bytes: int


__all__ = [
    "LoginRequest",
    "MeResponse",
    "OkResponse",
    "ImageListItem",
    "ImageListResponse",
    "ImageSourceOut",
    "ImageDetail",
    "FolderNode",
    "FolderCreate",
    "FolderUpdate",
    "FolderImagesAdd",
    "FolderImagesRemove",
    "FolderImagesRemoveResponse",
    "FolderOut",
    "CollectionRuleCreate",
    "CollectionRuleUpdate",
    "CollectionRuleOut",
    "SenderOut",
    "DiscoveredSenderOut",
    "SenderCreate",
    "SenderUpdate",
    "VendorOut",
    "VendorCreate",
    "VendorUpdate",
    "VendorRef",
    "SetImagesVendorRequest",
    "SetImagesVendorResponse",
    "VendorCredentialIn",
    "VendorCredentialStatus",
    "AccountOut",
    "DownloadRequest",
    "CountByName",
    "StatsResponse",
]
