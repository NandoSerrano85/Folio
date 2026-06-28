"""Gmail vendor-email ingestion -- real browser-driven acquisition (Phase 3).

For vendors that deliver images behind a *web* flow (a link to a gallery / a
direct image / a login + download) rather than as an email attachment, this
module drives the single, RAM-budgeted, off-hours headless Chromium to fetch the
ORIGINAL bytes and hands them to the common acquisition pipeline.

Flow per Gmail account
----------------------
1. Build the ``from:(...)`` query from the account's ENABLED senders (the
   operator allow-list -- the trust boundary for what we'll auto-fetch).
2. Page through matching messages (resumable via an ``ingest_runs`` checkpoint,
   ``kind='gmail_sync'``; idempotent because already-handled messages are
   skipped without re-downloading).
3. For each new message, extract candidate vendor link(s) from the HTML body and
   resolve a vendor + adapter (by ``senders.vendor_id`` first, then by link host
   -> ``vendors.domain`` -> ``adapter_key`` -> :func:`get_adapter`; falling back
   to the ``'generic'`` adapter for allow-listed-but-unmapped links).
4. Drive the adapter on the shared browser page to download the original(s) and
   run each through :func:`worker.pipeline.run_pipeline` with
   ``source_type='email'``, ``source_date`` = the email ``Date`` header,
   ``source_date_origin='email_date'``, idempotency ``source_id=f"{messageId}:{n}"``
   and the vendor URL / subject / sender / message-id recorded.
5. When no adapter can finish (no usable download, a CAPTCHA / anti-bot wall, or
   a login failure) ENQUEUE a pending ``assist_tasks`` row (idempotent on
   ``UNIQUE(account_id,email_message_id,vendor_url)``) instead of failing -- a
   human resolves it later via ``worker assist-resolve``.

RAM discipline: the browser is opened LAZILY (only when a message actually needs
it) and shared across the whole run; the whole sync only runs when
``BROWSER_ENABLED`` and inside the off-hours window. All Google-client,
Playwright and adapter imports are LAZY so importing this module never requires
those libraries.

The :class:`VendorAdapter` ABC, :class:`DownloadTarget` / :class:`EmailRef`
dataclasses, and the ``ADAPTER_REGISTRY`` / :func:`register_adapter` /
:func:`get_adapter` registry below are the FROZEN Phase-2 contract and MUST NOT
change.
"""

from __future__ import annotations

import re
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select

from folio_core.config import get_settings
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    AssistStatusEnum,
    AssistTask,
    ImageSource,
    IngestRun,
    IngestStatusEnum,
    ProviderEnum,
    Sender,
    SourceDateOriginEnum,
    SourceTypeEnum,
    Vendor,
)
from worker.checkpoint import (
    create_ingest_run,
    finalize_run,
    increment_counts,
    latest_unfinished_run,
    mark_interrupted,
    record_page_token,
)
from worker.pipeline import run_pipeline

logger = get_logger("worker.gmail.sync")

# Hard ceiling on emails examined per account per run.
MAX_SYNC_MESSAGES = 1000

# Run bookkeeping + bounds.
_RUN_KIND = "gmail_sync"
_PAGE_SIZE = 100
_MAX_VENDOR_LINKS = 25            # candidate links considered per message
_MAX_TARGETS_PER_MESSAGE = 6      # assets per message (bounded for the 8 GB box)
_MAX_IMAGE_BYTES = 50 * 1024 * 1024   # per-image cap; held in memory before ingest

# Local copy of the href extractor (full URLs, not just hosts).
_HREF_RE = re.compile(r"""href\s*=\s*["']?(https?://[^"'>\s)]+)""", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Vendor adapter contract  (FROZEN -- do not change)
# --------------------------------------------------------------------------- #
@dataclass
class DownloadTarget:
    """A resolved downloadable asset discovered on a vendor page.

    ``url`` is the direct asset URL (or an opaque handle the adapter
    understands), ``filename`` the suggested original name, ``content_type`` the
    MIME if known.
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

    Concrete adapters are registered under a ``vendors.adapter_key`` and are
    handed a live browser ``page`` plus the originating :class:`EmailRef`. The
    three lifecycle hooks below are intentionally narrow.
    """

    #: The ``vendors.adapter_key`` this adapter handles. Subclasses MUST set it.
    adapter_key: str = ""
    #: Whether the vendor flow requires an authenticated session.
    login_required: bool = False

    @abstractmethod
    def login(self, page: Any, *, credentials: dict[str, Any] | None = None) -> None:
        """Authenticate the browser session if the vendor requires it.

        Drive the vendor's login form on ``page``. No-op for vendors whose
        galleries are reachable from the email link without auth.
        """
        raise NotImplementedError

    @abstractmethod
    def find_download(self, page: Any, email: EmailRef) -> list[DownloadTarget]:
        """Locate the downloadable original(s) for ``email`` on ``page``.

        Navigate from the email's vendor link to the asset(s) and return their
        direct/handle URLs.
        """
        raise NotImplementedError

    @abstractmethod
    def download(self, page: Any, target: DownloadTarget) -> bytes:
        """Fetch the ORIGINAL bytes for ``target``.

        Return raw bytes; the caller computes sha256 on these ORIGINAL bytes
        BEFORE any EXIF stamping (per the project conventions).
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Adapter registry (keyed by vendors.adapter_key)  (FROZEN)
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
# Internal value objects
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _VendorInfo:
    id: int
    name: str
    domain: str | None
    adapter_key: str
    login_required: bool


@dataclass(slots=True)
class _ParsedEmail:
    address: str
    display_name: str | None
    subject: str | None
    date_header: str | None
    source_date: datetime
    vendor_urls: list[str]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_gmail_sync(
    account_email: str | None = None, *, ignore_offhours: bool = False
) -> None:
    """Acquire vendor-email images for one Gmail account (or all of them).

    Gated to ``BROWSER_ENABLED`` and the off-hours window: outside those it logs
    and returns without launching anything (pass ``ignore_offhours=True`` — the
    ``--now`` CLI flag — to force an on-demand run). The browser is opened lazily
    and shared across accounts; one account's failure never aborts the rest.
    """
    settings = get_settings()
    if not settings.browser_enabled:
        logger.info("gmail.sync.skip reason=browser_disabled")
        return

    from worker.browser.session import BrowserUnavailable, in_offhours

    if not ignore_offhours and not in_offhours():
        logger.info(
            "gmail.sync.skip reason=outside_offhours window=%d-%d tz=%s",
            settings.browser_offhours_start,
            settings.browser_offhours_end,
            settings.timezone,
        )
        return

    if settings.vendor_browser_max_jobs != 1:
        logger.warning(
            "gmail.sync.max_jobs_override value=%d (enforced to 1)",
            settings.vendor_browser_max_jobs,
        )

    # Populate the registry with the built-in (generic) adapter(s).
    from worker.browser.registry import load_builtin_adapters

    load_builtin_adapters()

    accounts = _resolve_gmail_accounts(account_email)
    if not accounts:
        logger.warning(
            "gmail.sync.no_accounts filter=%s; run `worker auth-gmail` first.",
            account_email,
        )
        return

    logger.info(
        "gmail.sync.begin accounts=%d registered_adapters=%d",
        len(accounts),
        len(ADAPTER_REGISTRY),
    )

    holder = _BrowserHolder(ignore_offhours=ignore_offhours)
    try:
        for account in accounts:
            try:
                _sync_account(holder, account)
            except BrowserUnavailable as exc:
                logger.warning(
                    "gmail.sync.browser_unavailable detail=%s -- aborting run", exc
                )
                break
            except Exception:  # noqa: BLE001 - one account must not kill the rest
                logger.exception("gmail.sync.account_failed account=%s", account.email)
    finally:
        holder.close()

    logger.info("gmail.sync.complete")


# --------------------------------------------------------------------------- #
# Lazy, shared browser holder
# --------------------------------------------------------------------------- #
class _BrowserHolder:
    """Opens the single Chromium session on first use; closes it once at the end.

    Opening is deferred so accounts with no fetchable work never launch Chromium.
    The off-hours / single-job gating lives in
    :func:`worker.browser.session.browser_session`.
    """

    def __init__(self, *, ignore_offhours: bool = False) -> None:
        self._cm = None
        self._page = None
        self._ignore_offhours = ignore_offhours

    @property
    def page(self) -> Any:
        if self._page is None:
            from worker.browser.session import browser_session

            self._cm = browser_session(ignore_offhours=self._ignore_offhours)
            self._page = self._cm.__enter__()
            logger.info("gmail.sync.browser_opened")
        return self._page

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("gmail.sync.browser_close_failed")
            finally:
                self._cm = None
                self._page = None
                logger.info("gmail.sync.browser_closed")


# --------------------------------------------------------------------------- #
# Per-account orchestration
# --------------------------------------------------------------------------- #
def _sync_account(holder: _BrowserHolder, account: Account) -> None:
    from worker.gmail.auth import build_gmail_service
    from worker.gmail.discover import build_from_query

    senders = _enabled_senders(account.id)
    if not senders:
        logger.info("gmail.sync.skip account=%s reason=no_enabled_senders", account.email)
        return

    query = build_from_query(senders)
    if not query:
        logger.info("gmail.sync.skip account=%s reason=empty_query", account.email)
        return

    service = build_gmail_service(account.email)
    if service is None:
        logger.info("gmail.sync.skip account=%s reason=not_authorized", account.email)
        return

    vendors_by_id, vendors_by_domain = _load_vendors()
    sender_addr_vendor, sender_domain_vendor = _sender_vendor_maps(senders)
    maps = (vendors_by_id, vendors_by_domain, sender_addr_vendor, sender_domain_vendor)

    # Open / resume a run.
    with session_scope() as session:
        previous = latest_unfinished_run(session, account.id, _RUN_KIND)
        resume_token = previous.last_page_token if previous else None
        mark_interrupted(session, account.id, _RUN_KIND)
        run_id = create_ingest_run(session, account.id, _RUN_KIND)

    logger.info(
        "gmail.sync.start account=%s resume=%s", account.email, bool(resume_token)
    )

    from worker.browser.session import BrowserUnavailable

    try:
        seen = _walk_messages(holder, service, account, run_id, query, resume_token, maps)
        with session_scope() as session:
            finalize_run(session, run_id, IngestStatusEnum.completed)
        logger.info("gmail.sync.done account=%s scanned=%d", account.email, seen)
    except BrowserUnavailable:
        # The browser window closed (or another job grabbed the lock) mid-run.
        # Mark this run interrupted (not failed) and bubble up so the whole sync
        # aborts cleanly rather than re-trying per account.
        with session_scope() as session:
            finalize_run(session, run_id, IngestStatusEnum.interrupted)
        raise
    except Exception:
        logger.exception("gmail.sync.account_error account=%s", account.email)
        with session_scope() as session:
            finalize_run(session, run_id, IngestStatusEnum.failed)
        raise


def _walk_messages(
    holder: _BrowserHolder,
    service: Any,
    account: Account,
    run_id: int,
    query: str,
    resume_token: str | None,
    maps: tuple,
) -> int:
    """Page through messages, checkpointing the page token; returns #scanned."""
    from worker.gmail.discover import _execute

    seen = 0
    page_token = resume_token
    first = True
    while True:
        try:
            resp = _execute(
                service.users().messages().list(
                    userId="me", q=query, maxResults=_PAGE_SIZE, pageToken=page_token
                )
            )
        except Exception:  # noqa: BLE001
            # A stale resume token is the most likely first-page failure; the
            # already-handled guard makes a from-scratch re-list safe + cheap.
            if first and page_token:
                logger.warning("gmail.sync.resume_token_stale -- restarting from start")
                page_token = None
                first = False
                continue
            raise
        first = False

        for meta in resp.get("messages", []):
            _handle_message(holder, service, account, run_id, meta["id"], maps)
            seen += 1
            if seen >= MAX_SYNC_MESSAGES:
                break

        page_token = resp.get("nextPageToken")
        with session_scope() as session:
            record_page_token(session, run_id, page_token)
        if not page_token or seen >= MAX_SYNC_MESSAGES:
            break
    return seen


# --------------------------------------------------------------------------- #
# Per-message handling
# --------------------------------------------------------------------------- #
def _handle_message(
    holder: _BrowserHolder,
    service: Any,
    account: Account,
    run_id: int,
    message_id: str,
    maps: tuple,
) -> None:
    """Process one message: idempotency guard, resolve adapter, fetch + ingest.

    Browser/adapter failures become a pending ``assist_tasks`` row (never abort
    the run). A :class:`BrowserUnavailable` from the lazy browser open is allowed
    to propagate so the whole run aborts cleanly.
    """
    from worker.gmail.discover import _execute

    # 1. Idempotency: skip messages already imported or already queued for assist.
    with session_scope() as session:
        if _message_already_handled(session, account.id, message_id):
            run = session.get(IngestRun, run_id)
            if run is not None:
                increment_counts(run, seen=1, skipped=1)
            return

    # 2. Fetch + parse the message.
    try:
        msg = _execute(
            service.users().messages().get(
                userId="me", id=message_id, format="full"
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "gmail.sync.message_fetch_failed account=%s id=%s", account.email, message_id
        )
        _bump(run_id, seen=1, failed=1)
        return

    parsed = _parse_email(msg)
    if not parsed.vendor_urls:
        _bump(run_id, seen=1, skipped=1)
        return

    # 3. Resolve a vendor + adapter.
    adapter_cls, vinfo, primary_url, _generic = _resolve(parsed, *maps)
    if adapter_cls is None:
        _enqueue_assist(
            account.id,
            vinfo.id if vinfo else None,
            message_id,
            parsed.subject,
            parsed.address,
            primary_url,
            reason="no_adapter",
        )
        _bump(run_id, seen=1, skipped=1)
        logger.info(
            "gmail.sync.no_adapter account=%s id=%s host=%s",
            account.email,
            message_id,
            _safe_host(primary_url),
        )
        return

    # 4. Drive the adapter on the shared browser page. (Browser opens here.)
    email_ref = EmailRef(
        account_id=account.id,
        account_email=account.email,
        message_id=message_id,
        sender=parsed.address,
        subject=parsed.subject,
        date_header=parsed.date_header,
        vendor_links=parsed.vendor_urls,
    )
    status, items, reason = _drive_adapter(holder, adapter_cls, vinfo, email_ref)
    if status != "ok":
        _enqueue_assist(
            account.id,
            vinfo.id if vinfo else None,
            message_id,
            parsed.subject,
            parsed.address,
            primary_url,
            reason=reason,
        )
        _bump(run_id, seen=1, skipped=1)
        logger.info(
            "gmail.sync.assist account=%s id=%s reason=%s", account.email, message_id, reason
        )
        return

    # 5. Ingest each downloaded original through the common pipeline.
    imported = skipped = failed = 0
    with session_scope() as session:
        vendor = session.get(Vendor, vinfo.id) if vinfo is not None else None
        for n, (target, data) in enumerate(items):
            filename = target.filename or _basename(target.url)
            mime = target.content_type or mimetypes.guess_type(filename)[0]
            try:
                result = run_pipeline(
                    session,
                    account=account,
                    source_type=SourceTypeEnum.email,
                    source_id=f"{message_id}:{n}",
                    data=data,
                    original_filename=filename,
                    mime=mime,
                    source_date=parsed.source_date,
                    source_date_origin=SourceDateOriginEnum.email_date,
                    vendor=vendor,
                    image_source_fields={
                        "vendor_url": target.url or primary_url,
                        "email_subject": parsed.subject,
                        "email_sender": parsed.address,
                        "email_message_id": message_id,
                    },
                )
                if result.created_image:
                    imported += 1
                else:
                    skipped += 1
            except Exception:  # noqa: BLE001 - one asset must not abort the message
                logger.exception(
                    "gmail.sync.ingest_failed account=%s id=%s n=%d",
                    account.email,
                    message_id,
                    n,
                )
                failed += 1
        run = session.get(IngestRun, run_id)
        if run is not None:
            increment_counts(run, seen=1, imported=imported, skipped=skipped, failed=failed)

    logger.info(
        "gmail.sync.ingested account=%s id=%s imported=%d skipped=%d failed=%d",
        account.email,
        message_id,
        imported,
        skipped,
        failed,
    )


def _drive_adapter(
    holder: _BrowserHolder,
    adapter_cls: type[VendorAdapter],
    vinfo: _VendorInfo | None,
    email_ref: EmailRef,
) -> tuple[str, list[tuple[DownloadTarget, bytes]], str | None]:
    """Run login -> find_download -> download. Returns ``(status, items, reason)``.

    ``status`` is ``"ok"`` with ``items`` populated, or a non-ok value mapped to
    an assist reason: ``captcha`` / ``login_failed`` / ``no_download``. Adapter
    exceptions are caught here; :class:`BrowserUnavailable` (from opening the
    browser) deliberately propagates to abort the run.
    """
    from worker.browser.base import (
        AdapterError,
        AntiBotBlocked,
        CaptchaEncountered,
        DownloadError,
        LoginFailed,
        load_vendor_credentials,
    )

    adapter = adapter_cls()
    page = holder.page  # may raise BrowserUnavailable -> propagate

    # Login (if required). Credential load failures degrade to login_failed.
    credentials = None
    if adapter.login_required:
        if vinfo is not None:
            try:
                credentials = load_vendor_credentials(vinfo.id)
            except Exception:  # noqa: BLE001 - never log secret material
                logger.warning(
                    "gmail.sync.cred_load_failed vendor_id=%s", vinfo.id
                )
                return ("assist", [], "login_failed")
        try:
            adapter.login(page, credentials=credentials)
        except (CaptchaEncountered, AntiBotBlocked):
            return ("assist", [], "captcha")
        except (LoginFailed, AdapterError):
            return ("assist", [], "login_failed")
        except Exception:  # noqa: BLE001
            logger.exception("gmail.sync.login_error key=%s", adapter.adapter_key)
            return ("assist", [], "login_failed")

    # Locate the downloadable original(s).
    try:
        targets = adapter.find_download(page, email_ref)
    except (CaptchaEncountered, AntiBotBlocked):
        return ("assist", [], "captcha")
    except (LoginFailed, AdapterError):
        return ("assist", [], "login_failed")
    except Exception:  # noqa: BLE001
        logger.exception("gmail.sync.find_error key=%s", adapter.adapter_key)
        return ("assist", [], "no_download")

    if not targets:
        return ("assist", [], "no_download")

    # Download each target (skipping individual failures).
    items: list[tuple[DownloadTarget, bytes]] = []
    for target in targets[:_MAX_TARGETS_PER_MESSAGE]:
        try:
            data = adapter.download(page, target)
        except (CaptchaEncountered, AntiBotBlocked):
            if items:
                break  # keep what we have; the rest needs a human
            return ("assist", [], "captcha")
        except (DownloadError, AdapterError):
            logger.info("gmail.sync.download_skip host=%s", _safe_host(target.url))
            continue
        except Exception:  # noqa: BLE001
            logger.exception("gmail.sync.download_error host=%s", _safe_host(target.url))
            continue
        if not data:
            continue
        if len(data) > _MAX_IMAGE_BYTES:
            logger.warning(
                "gmail.sync.too_large host=%s bytes=%d", _safe_host(target.url), len(data)
            )
            continue
        items.append((target, data))

    if not items:
        return ("assist", [], "no_download")
    return ("ok", items, None)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _resolve(
    parsed: _ParsedEmail,
    vendors_by_id: dict[int, _VendorInfo],
    vendors_by_domain: dict[str, _VendorInfo],
    sender_addr_vendor: dict[str, int],
    sender_domain_vendor: dict[str, int],
) -> tuple[type[VendorAdapter] | None, _VendorInfo | None, str | None, bool]:
    """Resolve ``(adapter_cls, vendor_info, primary_url, generic_used)``."""
    # 1. By sender -> senders.vendor_id.
    vid = sender_addr_vendor.get(parsed.address)
    if vid is None and parsed.address and "@" in parsed.address:
        vid = sender_domain_vendor.get(parsed.address.split("@", 1)[1])
    vinfo = vendors_by_id.get(vid) if vid is not None else None
    if vinfo is not None:
        adapter = get_adapter(vinfo.adapter_key)
        if adapter is not None:
            url = _pick_url_for_vendor(vinfo, parsed.vendor_urls)
            return adapter, vinfo, url, False

    # 2. By link host -> vendors.domain.
    for url in parsed.vendor_urls:
        host = _host_of(url)
        if not host:
            continue
        hv = _match_host(host, vendors_by_domain)
        if hv is not None:
            adapter = get_adapter(hv.adapter_key)
            if adapter is not None:
                return adapter, hv, url, False

    # 3. Generic fallback for allow-listed-but-unmapped links.
    generic = get_adapter("generic")
    if generic is not None and parsed.vendor_urls:
        provenance = vinfo
        if provenance is None:
            for url in parsed.vendor_urls:
                host = _host_of(url)
                hv = _match_host(host, vendors_by_domain) if host else None
                if hv is not None:
                    provenance = hv
                    break
        return generic, provenance, parsed.vendor_urls[0], True

    # 4. Nothing applicable.
    primary = parsed.vendor_urls[0] if parsed.vendor_urls else None
    return None, vinfo, primary, False


def _pick_url_for_vendor(vinfo: _VendorInfo, urls: list[str]) -> str | None:
    """Prefer a URL whose host matches the vendor's domain; else the first."""
    if not urls:
        return None
    if vinfo.domain:
        dom = vinfo.domain.strip().lower()
        for url in urls:
            host = _host_of(url)
            if host and (host == dom or host.endswith("." + dom)):
                return url
    return urls[0]


def _match_host(host: str, by_domain: dict[str, _VendorInfo]) -> _VendorInfo | None:
    host = host.lower()
    if host in by_domain:
        return by_domain[host]
    for dom, info in by_domain.items():
        if host == dom or host.endswith("." + dom):
            return info
    return None


# --------------------------------------------------------------------------- #
# Loading maps
# --------------------------------------------------------------------------- #
def _resolve_gmail_accounts(account_email: str | None) -> list[Account]:
    """Detached Gmail ``Account`` rows (scalar attrs stay loaded after close)."""
    with session_scope() as session:
        stmt = select(Account).where(Account.provider == ProviderEnum.gmail)
        if account_email:
            stmt = stmt.where(Account.email == account_email.strip().lower())
        return list(session.scalars(stmt.order_by(Account.id)))


def _enabled_senders(account_id: int) -> list[Sender]:
    with session_scope() as session:
        rows = session.scalars(
            select(Sender).where(
                Sender.account_id == account_id, Sender.enabled.is_(True)
            )
        ).all()
        session.expunge_all()
        return list(rows)


def _load_vendors() -> tuple[dict[int, _VendorInfo], dict[str, _VendorInfo]]:
    by_id: dict[int, _VendorInfo] = {}
    by_domain: dict[str, _VendorInfo] = {}
    with session_scope() as session:
        for vendor in session.scalars(select(Vendor)):
            info = _VendorInfo(
                id=vendor.id,
                name=vendor.name,
                domain=(vendor.domain or None),
                adapter_key=(vendor.adapter_key or ""),
                login_required=bool(vendor.login_required),
            )
            by_id[info.id] = info
            if info.domain:
                by_domain[info.domain.strip().lower()] = info
    return by_id, by_domain


def _sender_vendor_maps(senders: list[Sender]) -> tuple[dict[str, int], dict[str, int]]:
    by_addr: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    for sender in senders:
        if sender.vendor_id is None:
            continue
        addr = (sender.address or "").strip().lower()
        if addr:
            by_addr[addr] = sender.vendor_id
        domain = sender.domain or (addr.split("@", 1)[1] if "@" in addr else None)
        if domain:
            by_domain[domain.strip().lower()] = sender.vendor_id
    return by_addr, by_domain


# --------------------------------------------------------------------------- #
# Message parsing
# --------------------------------------------------------------------------- #
def _parse_email(msg: dict) -> _ParsedEmail:
    from email.utils import parseaddr

    from worker.gmail.discover import _decode_b64url, _parse_date, _walk_parts

    payload = msg.get("payload", {}) or {}
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }
    display_name, address = parseaddr(headers.get("from", ""))
    address = (address or "").strip().lower()
    subject = headers.get("subject") or None
    date_header = headers.get("date") or None

    # Authoritative acquisition date: the Date header, else Gmail's internalDate,
    # else now() (source_date is NOT NULL and must always be set).
    source_date = (
        _parse_date(date_header or "")
        or _from_internal_date(msg.get("internalDate"))
        or datetime.now(timezone.utc)
    )

    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        if (part.get("mimeType") or "").lower() == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                html_chunks.append(_decode_b64url(data))

    vendor_urls = _extract_urls("".join(html_chunks)) if html_chunks else []
    return _ParsedEmail(
        address=address,
        display_name=display_name or None,
        subject=subject,
        date_header=date_header,
        source_date=source_date,
        vendor_urls=vendor_urls,
    )


def _extract_urls(html: str) -> list[str]:
    from worker.gmail.discover import _is_ignored_host

    seen: set[str] = set()
    out: list[str] = []
    for match in _HREF_RE.finditer(html):
        url = match.group(1)
        host = _host_of(url)
        if not host or _is_ignored_host(host):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= _MAX_VENDOR_LINKS:
            break
    return out


def _from_internal_date(internal_ms: str | None) -> datetime | None:
    if not internal_ms:
        return None
    try:
        return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Idempotency + assist enqueue + counters
# --------------------------------------------------------------------------- #
def _message_already_handled(session, account_id: int, message_id: str) -> bool:
    """True if this message already produced an image source or an assist task."""
    src = session.scalar(
        select(ImageSource.id).where(
            ImageSource.account_id == account_id,
            ImageSource.source_type == SourceTypeEnum.email,
            or_(
                ImageSource.source_id == message_id,
                ImageSource.source_id.like(f"{message_id}:%"),
            ),
        )
    )
    if src is not None:
        return True
    task = session.scalar(
        select(AssistTask.id).where(
            AssistTask.account_id == account_id,
            AssistTask.email_message_id == message_id,
        )
    )
    return task is not None


def _enqueue_assist(
    account_id: int,
    vendor_id: int | None,
    message_id: str,
    subject: str | None,
    sender: str | None,
    vendor_url: str | None,
    *,
    reason: str,
) -> None:
    """Insert a pending assist task, idempotent on (account, message, url)."""
    from sqlalchemy.exc import IntegrityError

    url = vendor_url or ""  # vendor_url is NOT NULL in the model.
    try:
        with session_scope() as session:
            existing = session.scalar(
                select(AssistTask.id).where(
                    AssistTask.account_id == account_id,
                    AssistTask.email_message_id == message_id,
                    AssistTask.vendor_url == url,
                )
            )
            if existing is not None:
                return
            session.add(
                AssistTask(
                    account_id=account_id,
                    vendor_id=vendor_id,
                    email_message_id=message_id,
                    email_subject=subject,
                    email_sender=sender,
                    vendor_url=url,
                    reason=reason,
                    status=AssistStatusEnum.pending,
                )
            )
    except IntegrityError:
        # Lost the race on uq_assist_tasks_account_message_url -> already queued.
        logger.info("gmail.sync.assist_exists id=%s", message_id)


def _bump(run_id: int, **counts: int) -> None:
    with session_scope() as session:
        run = session.get(IngestRun, run_id)
        if run is not None:
            increment_counts(run, **counts)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def _host_of(url: str) -> str | None:
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return None
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _basename(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name or "image"


def _safe_host(url: str | None) -> str:
    if not url:
        return "?"
    return _host_of(url) or "?"


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
