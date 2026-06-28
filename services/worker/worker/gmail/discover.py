"""Gmail sender discovery.

``run_discover_senders`` scans each connected Gmail account's mailbox for
messages that *carry images* — either image attachments or HTML-body links out
to vendor sites — and aggregates the DISTINCT senders behind them with a
per-sender count, display name and last-seen timestamp. Those rows are UPSERTed
into the ``senders`` table so the portal can present an allow-list dropdown.

This pass doubles as a first cut at *vendor discovery*: external link domains
found in qualifying messages are tallied and logged as vendor candidates.

Design notes / bounds:
* The corpus is the (tunable) ``DEFAULT_DISCOVER_QUERY`` — attachment-bearing
  mail within a recent window — paged up to ``MAX_DISCOVER_MESSAGES`` so a huge
  mailbox can't make a single run unbounded.
* ``discovered_count`` is stored as ``max(existing, this_run_count)`` so
  re-scanning the same window is idempotent rather than inflating the tally.
* All Google client imports are lazy; importing this module never requires the
  Google libraries.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    IngestRun,
    IngestStatusEnum,
    ProviderEnum,
    Sender,
)

logger = get_logger("worker.gmail.discover")

# Gmail search corpus for discovery. Attachment-bearing mail is where images
# live; the bodies of those same messages are also mined for vendor links.
# Tunable: broaden the window or operator-target specific labels in Phase 3.
DEFAULT_DISCOVER_QUERY = "has:attachment newer_than:3y"

# Hard ceiling on messages inspected per account per run (bounded runtime).
MAX_DISCOVER_MESSAGES = 2000
_GMAIL_PAGE_SIZE = 100

_IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff",
    "webp", "heic", "heif", "avif",
}
_HREF_RE = re.compile(r"""href\s*=\s*["']?(https?://[^"'>\s)]+)""", re.IGNORECASE)

# Link hosts that are never "vendor sites" (trackers, CDNs, social, unsubscribe).
_LINK_HOST_IGNORE = (
    "google.com", "gstatic.com", "googleusercontent.com", "googleapis.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "linkedin.com", "pinterest.com", "apple.com", "list-manage.com",
    "mailchimp.com", "sendgrid.net", "mcusercontent.com", "doubleclick.net",
)


# --------------------------------------------------------------------------- #
# Retry plumbing for the Gmail API
# --------------------------------------------------------------------------- #
def _is_retryable(exc: BaseException) -> bool:
    """True for transient Gmail API errors (rate limit / 5xx)."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover - lib always present in the image
        return False
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in {429, 500, 502, 503, 504}
    return False


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _execute(request):
    """Execute a googleapiclient request with transient-error backoff."""
    return request.execute()


# --------------------------------------------------------------------------- #
# Aggregation model
# --------------------------------------------------------------------------- #
@dataclass
class _SenderAgg:
    address: str
    domain: str | None = None
    display_name: str | None = None
    count: int = 0
    last_seen: datetime | None = None

    def observe(self, display_name: str | None, when: datetime | None) -> None:
        self.count += 1
        if display_name and not self.display_name:
            self.display_name = display_name
        if when is not None and (self.last_seen is None or when > self.last_seen):
            self.last_seen = when


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_discover_senders(account_email: str | None = None) -> dict:
    """Discover image-bearing senders across Gmail accounts.

    Args:
        account_email: limit to a single account email, or ``None`` for all
            ``provider='gmail'`` accounts.

    Returns:
        A summary dict ``{"accounts": [<per-account summary>...]}`` where each
        per-account summary lists the top senders by count.
    """
    accounts = _select_gmail_accounts(account_email)
    if not accounts:
        logger.warning(
            "discover.no_accounts filter=%s — nothing to scan.", account_email
        )
        return {"accounts": []}

    summaries = []
    for account_id, email in accounts:
        try:
            summaries.append(_discover_for_account(account_id, email))
        except Exception:  # noqa: BLE001 - one account must not kill the rest
            logger.exception("discover.account_failed account=%s", email)
            summaries.append({"account": email, "error": True})

    return {"accounts": summaries}


def _select_gmail_accounts(account_email: str | None) -> list[tuple[int, str]]:
    """Return ``(account_id, email)`` for the targeted Gmail account(s)."""
    from sqlalchemy import select

    stmt = select(Account.id, Account.email).where(
        Account.provider == ProviderEnum.gmail
    )
    if account_email:
        stmt = stmt.where(Account.email == account_email.strip().lower())

    with session_scope() as session:
        return [(row.id, row.email) for row in session.execute(stmt)]


def _discover_for_account(account_id: int, email: str) -> dict:
    """Scan one account, upsert senders, return its summary."""
    from worker.gmail.auth import build_gmail_service

    run_id = _start_ingest_run(account_id)
    logger.info("discover.start account=%s run_id=%s", email, run_id)

    service = build_gmail_service(email)
    if service is None:
        _finish_ingest_run(run_id, IngestStatusEnum.failed, seen=0)
        return {"account": email, "authorized": False, "top_senders": []}

    aggregates: dict[str, _SenderAgg] = {}
    vendor_domains: dict[str, int] = {}
    seen = 0

    for message_id in _iter_message_ids(service, DEFAULT_DISCOVER_QUERY):
        seen += 1
        try:
            msg = _execute(
                service.users().messages().get(
                    userId="me", id=message_id, format="full"
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("discover.message_failed account=%s id=%s", email, message_id)
            continue

        address, display_name, when, has_image, links = _classify_message(msg)
        if not address:
            continue
        if not (has_image or links):
            continue

        agg = aggregates.get(address)
        if agg is None:
            agg = _SenderAgg(address=address, domain=_domain_of(address))
            aggregates[address] = agg
        agg.observe(display_name, when)

        for host in links:
            vendor_domains[host] = vendor_domains.get(host, 0) + 1

    upserted = _upsert_senders(account_id, aggregates)
    _finish_ingest_run(run_id, IngestStatusEnum.completed, seen=seen)

    top = sorted(aggregates.values(), key=lambda a: a.count, reverse=True)
    top_vendor_domains = sorted(
        vendor_domains.items(), key=lambda kv: kv[1], reverse=True
    )[:15]
    logger.info(
        "discover.done account=%s scanned=%d senders=%d vendor_domains=%d",
        email, seen, len(aggregates), len(vendor_domains),
    )
    return {
        "account": email,
        "authorized": True,
        "scanned": seen,
        "distinct_senders": len(aggregates),
        "senders_upserted": upserted,
        "top_senders": [
            {
                "address": a.address,
                "display_name": a.display_name,
                "count": a.count,
                "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            }
            for a in top[:25]
        ],
        "vendor_domain_candidates": [
            {"domain": d, "count": c} for d, c in top_vendor_domains
        ],
    }


# --------------------------------------------------------------------------- #
# Gmail traversal helpers
# --------------------------------------------------------------------------- #
def _iter_message_ids(service, query: str):
    """Yield up to ``MAX_DISCOVER_MESSAGES`` message ids matching ``query``."""
    yielded = 0
    page_token = None
    while True:
        resp = _execute(
            service.users().messages().list(
                userId="me",
                q=query,
                maxResults=_GMAIL_PAGE_SIZE,
                pageToken=page_token,
            )
        )
        for meta in resp.get("messages", []):
            yield meta["id"]
            yielded += 1
            if yielded >= MAX_DISCOVER_MESSAGES:
                return
        page_token = resp.get("nextPageToken")
        if not page_token:
            return


def _classify_message(msg: dict):
    """Extract ``(address, display_name, date, has_image, link_hosts)``.

    ``link_hosts`` is the set of external (non-ignored) hosts linked from the
    HTML body — the vendor-candidate signal.
    """
    payload = msg.get("payload", {}) or {}
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }
    display_name, address = parseaddr(headers.get("from", ""))
    address = (address or "").strip().lower()
    when = _parse_date(headers.get("date", ""))

    has_image = False
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        if mime.startswith("image/") or _has_image_ext(filename):
            # Treat as a carried image when it's an actual attachment (filename)
            # or a non-inline image part.
            if filename or part.get("body", {}).get("attachmentId"):
                has_image = True
        elif mime == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                html_chunks.append(_decode_b64url(data))

    link_hosts = _extract_link_hosts("".join(html_chunks)) if html_chunks else set()
    return address, (display_name or None), when, has_image, link_hosts


def _walk_parts(payload: dict):
    """Depth-first walk over a Gmail message payload's MIME parts."""
    stack = [payload]
    while stack:
        part = stack.pop()
        yield part
        stack.extend(part.get("parts", []) or [])


def _decode_b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return ""


def _extract_link_hosts(html: str) -> set[str]:
    hosts: set[str] = set()
    for url in _HREF_RE.findall(html):
        host = _host_of(url)
        if host and not _is_ignored_host(host):
            hosts.add(host)
    return hosts


def _host_of(url: str) -> str | None:
    from urllib.parse import urlsplit

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


def _is_ignored_host(host: str) -> bool:
    return any(host == ig or host.endswith("." + ig) for ig in _LINK_HOST_IGNORE)


def _has_image_ext(filename: str) -> bool:
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS


def _domain_of(address: str) -> str | None:
    return address.rsplit("@", 1)[-1] if "@" in address else None


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _upsert_senders(account_id: int, aggregates: dict[str, _SenderAgg]) -> int:
    """UPSERT aggregated senders for ``account_id``. Returns rows touched."""
    if not aggregates:
        return 0

    from sqlalchemy import select

    touched = 0
    with session_scope() as session:
        existing = {
            s.address: s
            for s in session.scalars(
                select(Sender).where(Sender.account_id == account_id)
            )
        }
        for address, agg in aggregates.items():
            row = existing.get(address)
            if row is None:
                session.add(
                    Sender(
                        account_id=account_id,
                        address=address,
                        domain=agg.domain,
                        display_name=agg.display_name,
                        enabled=False,
                        discovered_count=agg.count,
                        last_seen_at=agg.last_seen,
                    )
                )
            else:
                # Idempotent on a stable window: keep the larger tally.
                row.discovered_count = max(row.discovered_count or 0, agg.count)
                if agg.display_name and not row.display_name:
                    row.display_name = agg.display_name
                if not row.domain and agg.domain:
                    row.domain = agg.domain
                if agg.last_seen and (
                    row.last_seen_at is None or agg.last_seen > row.last_seen_at
                ):
                    row.last_seen_at = agg.last_seen
            touched += 1
    return touched


def _start_ingest_run(account_id: int) -> int | None:
    """Open a ``gmail_discover`` ingest run; return its id (or None on failure)."""
    try:
        with session_scope() as session:
            run = IngestRun(
                account_id=account_id,
                kind="gmail_discover",
                status=IngestStatusEnum.running,
            )
            session.add(run)
            session.flush()
            return run.id
    except Exception:  # noqa: BLE001 - bookkeeping must not block discovery
        logger.exception("discover.ingest_run_start_failed account_id=%s", account_id)
        return None


def _finish_ingest_run(run_id: int | None, status: IngestStatusEnum, *, seen: int) -> None:
    if run_id is None:
        return
    try:
        with session_scope() as session:
            run = session.get(IngestRun, run_id)
            if run is None:
                return
            run.status = status
            run.items_seen = seen
            run.finished_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        logger.exception("discover.ingest_run_finish_failed run_id=%s", run_id)


# --------------------------------------------------------------------------- #
# Gmail query helper (shared with sync)
# --------------------------------------------------------------------------- #
def build_from_query(senders) -> str:
    """Build a Gmail ``from:(...)`` query for the *enabled* senders.

    Accepts an iterable of :class:`folio_core.models.Sender` rows (or any object
    exposing ``address``/``enabled``) and returns a string like
    ``from:(a@x.com OR b@y.com OR @vendor.com)``. An ``address`` beginning with
    ``@`` is treated as a whole-domain match. Returns ``""`` when nothing is
    enabled (callers should treat an empty query as "skip").
    """
    terms: list[str] = []
    seen: set[str] = set()
    for s in senders:
        if not getattr(s, "enabled", True):
            continue
        address = (getattr(s, "address", "") or "").strip().lower()
        if not address or address in seen:
            continue
        seen.add(address)
        terms.append(address)
    if not terms:
        return ""
    return "from:(" + " OR ".join(terms) + ")"


__all__ = [
    "run_discover_senders",
    "build_from_query",
    "DEFAULT_DISCOVER_QUERY",
    "MAX_DISCOVER_MESSAGES",
]
