"""Application configuration.

All settings are loaded from environment variables (and an optional ``.env``
file) via pydantic-settings. Every key here MUST have a corresponding line in
``.env.example`` at the repo root, and vice-versa.

Usage::

    from folio_core.config import get_settings
    settings = get_settings()
    engine_url = settings.database_url
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings shared by the worker and the portal."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str = Field(
        default="postgresql+psycopg://folio:folio@db:5432/folio",
        description="SQLAlchemy URL. Uses the psycopg (v3) driver.",
    )

    # POSTGRES_* are consumed by the postgres container itself, but we accept
    # them here so a single .env drives everything and validation is centralized.
    postgres_user: str = Field(default="folio")
    postgres_password: str = Field(default="folio")
    postgres_db: str = Field(default="folio")

    # ------------------------------------------------------------------ #
    # Filesystem layout (paths inside the containers; mounted as volumes)
    # ------------------------------------------------------------------ #
    media_root: Path = Field(
        default=Path("/data/media"),
        description="Root of the original-image library. Worker rw, portal ro.",
    )
    thumbnail_root: Path = Field(
        default=Path("/data/thumbnails"),
        description="On-demand thumbnail cache. Portal rw.",
    )
    token_dir: Path = Field(
        default=Path("/data/tokens"),
        description="Directory holding per-account encrypted OAuth refresh tokens.",
    )

    # ------------------------------------------------------------------ #
    # Crypto / secrets
    # ------------------------------------------------------------------ #
    fernet_key: str = Field(
        default="",
        description="Base64 urlsafe 32-byte Fernet key for token encryption.",
    )
    portal_secret_key: str = Field(
        default="",
        description="Secret used to sign the portal session cookie.",
    )
    session_https_only: bool = Field(
        default=False,
        description=(
            "Set the Secure flag on the session cookie. Default False because "
            "the portal is usually reached at http://NAS-IP:8899 on the LAN; "
            "set True when the portal is served over HTTPS (reverse proxy/VPN), "
            "otherwise the cookie won't be sent and login won't persist."
        ),
    )

    # ------------------------------------------------------------------ #
    # Google OAuth (installed-app flow, READ-ONLY scopes only)
    # ------------------------------------------------------------------ #
    google_client_secrets_file: Path = Field(
        default=Path("/data/secrets/google_client_secret.json"),
        description="Path to the OAuth client secrets JSON (installed app).",
    )
    google_oauth_scopes: str = Field(
        default=(
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/drive.readonly"
        ),
        description="Space-separated OAuth scopes. READ-ONLY by policy.",
    )

    # ------------------------------------------------------------------ #
    # Portal admin bootstrap
    # ------------------------------------------------------------------ #
    admin_username: str = Field(
        default="admin",
        description="Username for the seeded admin user (first boot only).",
    )
    admin_password: str = Field(
        default="",
        description="Password for the seeded admin user (first boot only).",
    )
    access_token_hash: str = Field(
        default="",
        description=(
            "argon2id hash of the portal access token. The plaintext token is "
            "NEVER stored; only this hash. Mint with services/portal/scripts/"
            "make_token.py and paste the printed token into the portal."
        ),
    )

    # ------------------------------------------------------------------ #
    # Worker scheduler intervals (minutes)
    # ------------------------------------------------------------------ #
    sync_drive_interval_minutes: int = Field(default=30)
    discover_senders_interval_minutes: int = Field(default=720)
    reconcile_interval_minutes: int = Field(default=1440)
    gmail_sync_interval_minutes: int = Field(
        default=360,
        description=(
            "How often the vendor-browser Gmail ingestion runs. Only fires "
            "inside the off-hours window and when browser_enabled is True."
        ),
    )
    sync_on_startup: bool = Field(
        default=True,
        description=(
            "Run sync-drive + discover-senders ~10s after the scheduler starts "
            "instead of waiting a full interval. First run is a full Drive "
            "backfill (no cursor yet); later restarts do a quick incremental."
        ),
    )

    # ------------------------------------------------------------------ #
    # Vendor browser (Playwright/Chromium) — RAM-constrained, off-hours only
    # ------------------------------------------------------------------ #
    browser_enabled: bool = Field(
        default=True,
        description="Master switch for the headless-browser vendor ingestion.",
    )
    browser_headless: bool = Field(
        default=True,
        description="Run Chromium headless (always True in production).",
    )
    browser_nav_timeout_seconds: int = Field(
        default=45,
        description="Per-navigation timeout for Playwright page operations.",
    )
    browser_download_dir: Path = Field(
        default=Path("/data/tmp/downloads"),
        description="Scratch directory for browser-downloaded originals.",
    )
    browser_offhours_start: int = Field(
        default=1,
        description=(
            "Local-hour (settings.timezone) the browser window opens; the job "
            "runs only when start <= hour < end."
        ),
    )
    browser_offhours_end: int = Field(
        default=6,
        description="Local-hour the browser window closes (exclusive).",
    )
    vendor_browser_max_jobs: int = Field(
        default=1,
        description=(
            "Max concurrent vendor-browser jobs. MUST stay 1 on 8 GB RAM — "
            "Chromium is memory-hungry."
        ),
    )

    # ------------------------------------------------------------------ #
    # Vendor derivation (worker derive-vendors)
    # ------------------------------------------------------------------ #
    vendor_derive_strategy: str = Field(
        default="frequent",
        description=(
            "How to pick a vendor name from a Drive folder path "
            "('A/B/C'): 'frequent' (most common token across the run), "
            "'parent' (the deepest folder), or 'top' (the first folder)."
        ),
    )
    vendor_derive_stoplist: str = Field(
        default="",
        description=(
            "Comma/space-separated generic folder names to ignore when "
            "deriving a vendor (e.g. 'photos, shared, images')."
        ),
    )
    vendor_derive_min_len: int = Field(
        default=3,
        description="Ignore path tokens shorter than this many characters.",
    )
    vendor_derive_include_filename: bool = Field(
        default=False,
        description="Include the file's basename as a candidate vendor token.",
    )

    # ------------------------------------------------------------------ #
    # Database backups (pg_dump custom format)
    # ------------------------------------------------------------------ #
    backup_dir: Path = Field(
        default=Path("/data/backups"),
        description="Destination for timestamped pg_dump archives.",
    )
    backup_retention_days: int = Field(
        default=14,
        description="Dumps older than this many days are pruned after a backup.",
    )

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #
    log_level: str = Field(default="INFO", description="Root log level.")
    exiftool_binary: str = Field(
        default="exiftool",
        description="Path/name of the exiftool executable.",
    )
    thumbnail_default_size: int = Field(default=320)
    timezone: str = Field(default="UTC")

    @property
    def scopes_list(self) -> list[str]:
        """OAuth scopes as a list."""
        return [s for s in self.google_oauth_scopes.split() if s]

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
