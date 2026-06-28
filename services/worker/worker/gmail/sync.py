"""Gmail vendor-email ingestion — FRAMEWORK STUB (Phase 3).

The end goal: for vendors that put images behind a *login + download* web flow
(rather than as email attachments), Folio will drive a headless browser to fetch
the originals. This module lays down the contract for that work **without** any
browser/Playwright dependency yet, so it imports cleanly and ``worker sync-gmail``
runs today as a bounded no-op-with-logging.

What is real now:
* :class:`VendorAdapter` — the abstract interface every per-vendor adapter
  implements (``login`` / ``find_download`` / ``download``).
* :data:`ADAPTER_REGISTRY` + :func:`register_adapter` / :func:`get_adapter` —
  a registry keyed by ``vendors.adapter_key``.
* :func:`run_gmail_sync` — iterates enabled-sender emails, extracts vendor
  link(s), resolves the adapter by domain → ``adapter_key``, and where no
  adapter exists logs/queues the email as "needs adapter".

What is deliberately NOT here yet (Phase 3 TODO):
* Any concrete adapter / Playwright / Chromium automation.
* Writing images to ``MEDIA_ROOT`` (sha256 of ORIGINAL bytes BEFORE EXIF stamp,
  then ``folio_core.exif.stamp_source_date``), recording ``image_sources`` with
  ``source_type='email'``, ``source_date`` = the email ``Date`` header and
  ``source_date_origin='email_date'``, and idempotency on
  ``UNIQUE(account_id,'email',messageId)``.

All Google client imports are lazy; importing this module never requires the
Google libraries (or a browser).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    ProviderEnum,
    Sender,
    Vendor,
)

logger = get_logger("worker.gmail.sync")

# Hard ceiling on emails examined per account per run while stubbed.
MAX_SYNC_MESSAGES = 1000


# --------------------------------------------------------------------------- #
# Vendor adapter contract (Phase 3 implements concrete subclasses)
# --------------------------------------------------------------------------- #
@dataclass
class DownloadTarget:
    """A resolved downloadable asset discovered on a vendor page.

    Phase 3 populates this from ``find_download``; ``url`` is the direct asset
    URL (or an opaque handle the adapter understands), ``filename`` the
    suggested original name, ``content_type`` the MIME if known.
    """

    url: str
    filename: str | None = None
    content_type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmailRef:
    """The minimal email context an adapter needs to do its job."""

    account_id: int
    account_email: str
    message_id: str
    sender: str | None
    subject: str | None
    date_header: str | None
    vendor_links: list[str] = field(default_factory=list)


class VendorAdapter(ABC):
    """Abstract base for a per-vendor headless-browser download adapter.

    Concrete adapters are registered under a ``vendors.adapter_key`` and, in
    Phase 3, will be handed a live browser ``page`` plus the originating
    :class:`EmailRef`. The three lifecycle hooks below are intentionally narrow.
    """

    #: The ``vendors.adapter_key`` this adapter handles. Subclasses MUST set it.
    adapter_key: str = ""
    #: Whether the vendor flow requires an authenticated session.
    login_required: bool = False

    @abstractmethod
    def login(self, page: Any, *, credentials: dict[str, Any] | None = None) -> None:
        """Authenticate the browser session if the vendor requires it.

        Phase 3: drive the vendor's login form on ``page``. No-op for vendors
        whose galleries are reachable from the email link without auth.
        """
        raise NotImplementedError

    @abstractmethod
    def find_download(self, page: Any, email: EmailRef) -> list[DownloadTarget]:
        """Locate the downloadable original(s) for ``email`` on ``page``.

        Phase 3: navigate from the email's vendor link to the asset(s) and
        return their direct/handle URLs.
        """
        raise NotImplementedError

    @abstractmethod
    def download(self, page: Any, target: DownloadTarget) -> bytes:
        """Fetch the ORIGINAL bytes for ``target``.

        Phase 3: return raw bytes; the caller computes sha256 on these ORIGINAL
        bytes BEFORE any EXIF stamping (per the project conventions).
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Adapter registry (keyed by vendors.adapter_key)
# --------------------------------------------------------------------------- #
ADAPTER_REGISTRY: dict[str, type[VendorAdapter]] = {}


def register_adapter(cls: type[VendorAdapter]) -> type[VendorAdapter]:
    """Class decorator registering a :class:`VendorAdapter` by ``adapter_key``."""
    key = getattr(cls, "adapter_key", "") or ""
    if not key:
        raise ValueError(
            f"{cls.__name__} must set a non-empty `adapter_key` to be registered."
        )
    if key in ADAPTER_REGISTRY and ADAPTER_REGISTRY[key] is not cls:
        logger.warning("sync.adapter_override key=%s", key)
    ADAPTER_REGISTRY[key] = cls
    return cls


def get_adapter(adapter_key: str | None) -> type[VendorAdapter] | None:
    """Return the registered adapter class for ``adapter_key`` (or ``None``)."""
    if not adapter_key:
        return None
    return ADAPTER_REGISTRY.get(adapter_key)


# --------------------------------------------------------------------------- #
# Entry point (stubbed: resolve adapters, queue gaps, do not browse)
# --------------------------------------------------------------------------- #
def run_gmail_sync(account_email: str | None = None) -> None:
    """Resolve vendor adapters for enabled-sender emails (no browsing yet).

    For each Gmail account it builds the enabled-sender Gmail query, walks the
    matching messages (bounded), extracts vendor link host(s), resolves the
    vendor → ``adapter_key`` → adapter, and:

      * if an adapter exists → logs that it *would* ingest (Phase 3 wires the
        browser + image write here);
      * if none exists → records the email as "needs adapter".

    This is a no-op with logging today; it never raises on a per-account error.
    """
    accounts = _select_gmail_accounts(account_email)
    if not accounts:
        logger.warning(
            "sync.no_accounts filter=%s — nothing to do (stub).", account_email
        )
        return

    logger.info(
        "sync.start mode=stub accounts=%d registered_adapters=%d",
        len(accounts), len(ADAPTER_REGISTRY),
    )

    for account_id, email in accounts:
        try:
            _sync_account_stub(account_id, email)
        except Exception:  # noqa: BLE001 - one account must not kill the rest
            logger.exception("sync.account_failed account=%s", email)

    logger.info("sync.done mode=stub (Phase 3: implement browser ingestion)")


def _select_gmail_accounts(account_email: str | None) -> list[tuple[int, str]]:
    from sqlalchemy import select

    stmt = select(Account.id, Account.email).where(
        Account.provider == ProviderEnum.gmail
    )
    if account_email:
        stmt = stmt.where(Account.email == account_email.strip().lower())
    with session_scope() as session:
        return [(row.id, row.email) for row in session.execute(stmt)]


def _enabled_senders(account_id: int) -> list[Sender]:
    from sqlalchemy import select

    with session_scope() as session:
        rows = session.scalars(
            select(Sender).where(
                Sender.account_id == account_id, Sender.enabled.is_(True)
            )
        ).all()
        # Detach lightweight copies so callers can read attrs after the session
        # closes (we only need address/enabled downstream).
        session.expunge_all()
        return list(rows)


def _vendor_domain_map() -> dict[str, str | None]:
    """Map known vendor domain -> adapter_key (lowercased domain keys)."""
    from sqlalchemy import select

    out: dict[str, str | None] = {}
    with session_scope() as session:
        for name, domain, adapter_key in session.execute(
            select(Vendor.name, Vendor.domain, Vendor.adapter_key)
        ):
            if domain:
                out[domain.strip().lower()] = adapter_key
    return out


def _sync_account_stub(account_id: int, email: str) -> None:
    # Lazy imports of the discover helpers + auth service builder keep this
    # module import-clean and reuse the Gmail plumbing.
    from worker.gmail.auth import build_gmail_service
    from worker.gmail.discover import (
        _classify_message,
        _execute,
        _iter_message_ids,
        build_from_query,
    )

    senders = _enabled_senders(account_id)
    if not senders:
        logger.info("sync.skip account=%s reason=no_enabled_senders", email)
        return

    query = build_from_query(senders)
    if not query:
        logger.info("sync.skip account=%s reason=empty_query", email)
        return

    service = build_gmail_service(email)
    if service is None:
        logger.info("sync.skip account=%s reason=not_authorized", email)
        return

    vendor_by_domain = _vendor_domain_map()

    seen = 0
    with_adapter = 0
    needs_adapter = 0
    no_links = 0
    needs_adapter_domains: dict[str, int] = {}

    for message_id in _iter_message_ids(service, query):
        seen += 1
        if seen > MAX_SYNC_MESSAGES:
            break
        try:
            msg = _execute(
                service.users().messages().get(
                    userId="me", id=message_id, format="full"
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("sync.message_failed account=%s id=%s", email, message_id)
            continue

        _addr, _name, _when, _has_image, link_hosts = _classify_message(msg)
        if not link_hosts:
            no_links += 1
            continue

        resolved = False
        for host in link_hosts:
            adapter_key = _match_vendor(host, vendor_by_domain)
            if adapter_key and get_adapter(adapter_key) is not None:
                with_adapter += 1
                resolved = True
                logger.debug(
                    "sync.adapter_match account=%s msg=%s host=%s adapter=%s "
                    "(Phase 3: would download originals)",
                    email, message_id, host, adapter_key,
                )
                break
        if not resolved:
            needs_adapter += 1
            for host in link_hosts:
                if not _match_vendor(host, vendor_by_domain):
                    needs_adapter_domains[host] = (
                        needs_adapter_domains.get(host, 0) + 1
                    )

    top_needs = sorted(
        needs_adapter_domains.items(), key=lambda kv: kv[1], reverse=True
    )[:15]
    logger.info(
        "sync.account_summary account=%s scanned=%d with_adapter=%d "
        "needs_adapter=%d no_links=%d top_needs_adapter=%s",
        email, seen, with_adapter, needs_adapter, no_links, top_needs,
    )


def _match_vendor(host: str, vendor_by_domain: dict[str, str | None]) -> str | None:
    """Resolve a link host to an ``adapter_key`` via vendor domain matching."""
    host = host.lower()
    if host in vendor_by_domain:
        return vendor_by_domain[host]
    # Suffix match: link host is a subdomain of a known vendor domain.
    for domain, adapter_key in vendor_by_domain.items():
        if host == domain or host.endswith("." + domain):
            return adapter_key
    return None


__all__ = [
    "run_gmail_sync",
    "VendorAdapter",
    "DownloadTarget",
    "EmailRef",
    "ADAPTER_REGISTRY",
    "register_adapter",
    "get_adapter",
    "MAX_SYNC_MESSAGES",
]
