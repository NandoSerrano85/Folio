"""Shopify "Digital Downloads" vendor adapter.

Shopify's *Digital Downloads* app emails come from the shared sender
``store+<id>@t.shopifyemail.com`` and embed one or more links of the shape::

    https://<shop-host>/a/downloads/-/<hex>/<hex>

where ``<shop-host>`` is the store's OWN domain (so the shop identity lives in the
link host, not the sender). Each link IS the asset: hitting it returns either a
single direct image (e.g. ``OTTER POPS CAN.png``) or a ``.zip`` bundling several
images. The ZIP expansion happens downstream in :mod:`worker.archive`; this
adapter only fetches the ORIGINAL bytes the store served.

Two store configurations exist in the wild:

* **Open token** -- the ``/a/downloads/-/`` link downloads without auth. ``login``
  is a no-op and ``download`` simply fetches the bytes.
* **Login-gated** -- the store requires a Shopify *classic customer-account* login
  (email + password) before the token resolves. The operator stores those
  credentials (encrypted) for the vendor; :meth:`login` drives the classic
  ``/account/login`` form, and :meth:`download` detects an un-authenticated bounce
  (a redirect to the login/password page or an HTML body) and raises
  :class:`LoginFailed` so the orchestrator queues a human-assist task.

Safety: every fetch runs the shared SSRF guard
(:func:`worker.browser.net.assert_public_url`) first, the password is NEVER logged,
and a 50 MB per-asset cap bounds memory on the shared 8 GB box. All DOM/network
work uses the budgeted Chromium ``page`` handed in by the session manager.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from folio_core.logging import get_logger
from worker.browser.base import (
    DownloadError,
    DownloadTarget,
    EmailRef,
    LoginFailed,
    VendorAdapter,
)
from worker.browser.net import fetch_public, safe_host
from worker.browser.registry import register_adapter

logger = get_logger("worker.browser.shopify_downloads")

# The token-download path segment that identifies a Digital Downloads asset link.
_DOWNLOAD_MARKER = "/a/downloads/-/"

# Per-asset memory cap (mirrors the orchestrator's own ceiling).
_MAX_BYTES = 50 * 1024 * 1024

# Playwright timeouts (milliseconds). Kept generous but bounded so a hung store
# never stalls the single off-hours browser.
_NAV_TIMEOUT_MS = 30_000
_ACT_TIMEOUT_MS = 15_000
_REQUEST_TIMEOUT_MS = 60_000

# A logged-in Shopify customer session exposes a logout link.
_LOGOUT_SELECTOR = "a[href*='/account/logout']"
_EMAIL_SELECTOR = "input[name='customer[email]']"
_PASSWORD_SELECTOR = "input[name='customer[password]']"
_SUBMIT_SELECTOR = "button[type='submit'], input[type='submit']"


@register_adapter
class ShopifyDownloadsAdapter(VendorAdapter):
    """Fetch Shopify Digital Downloads originals (optionally behind a login)."""

    adapter_key = "shopify_downloads"
    login_required = True

    # ------------------------------------------------------------------ #
    # 1. login  (optional -- many stores serve the token without auth)
    # ------------------------------------------------------------------ #
    def login(self, page: Any, *, credentials: dict[str, Any] | None = None) -> None:
        """Drive Shopify's classic customer login when credentials are stored.

        No-op when no credentials are present or they lack a username/secret --
        many Digital Downloads stores serve the token link without auth, so we
        let :meth:`download` discover whether a login is actually required.

        ``credentials`` (when present) is the DECRYPTED dict from
        ``vendor_credentials``: ``{login_url, username, secret, extra}``. The
        password is NEVER logged.
        """
        if not credentials:
            return
        username = credentials.get("username")
        secret = credentials.get("secret")
        if not username or not secret:
            # No usable login material -> treat the store as open; download will
            # raise LoginFailed if it turns out auth was required after all.
            return

        login_url = credentials.get("login_url")
        if not login_url:
            # The adapter cannot derive the shop host at login time; without an
            # explicit login URL we skip and let download() detect the bounce.
            logger.info("shopify.login.skip reason=no_login_url")
            return

        try:
            page.goto(
                login_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001
            raise LoginFailed("could not open the shopify login page") from exc

        # The cookie jar persists across messages in a run; if a prior message
        # already authenticated us, skip straight through.
        if self._logged_in(page):
            logger.info("shopify.login.reuse")
            return

        try:
            page.fill(_EMAIL_SELECTOR, username, timeout=_ACT_TIMEOUT_MS)
            page.fill(_PASSWORD_SELECTOR, secret, timeout=_ACT_TIMEOUT_MS)
            try:
                page.locator(_SUBMIT_SELECTOR).first.click(timeout=_ACT_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - no button -> submit via Enter
                page.press(_PASSWORD_SELECTOR, "Enter")
        except Exception as exc:  # noqa: BLE001 - never echo username/secret
            raise LoginFailed("shopify login form interaction failed") from exc

        # A slow network-idle on its own does not mean failure; the verify step
        # below is authoritative.
        try:
            page.wait_for_load_state("networkidle", timeout=_NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass

        if not self._logged_in(page):
            raise LoginFailed("shopify login did not establish a customer session")
        logger.info("shopify.login.ok")

    # ------------------------------------------------------------------ #
    # 2. find_download  (the link IS the asset -- no navigation)
    # ------------------------------------------------------------------ #
    def find_download(self, page: Any, email: EmailRef) -> list[DownloadTarget]:
        """Return one target per Digital Downloads link in the email.

        No page navigation: a ``/a/downloads/-/`` URL is the asset handle itself,
        resolved later by :meth:`download`.
        """
        targets = [
            DownloadTarget(url=url)
            for url in email.vendor_links
            if _DOWNLOAD_MARKER in url
        ]
        logger.info(
            "shopify.find_download links=%d targets=%d",
            len(email.vendor_links),
            len(targets),
        )
        return targets

    # ------------------------------------------------------------------ #
    # 3. download  (fetch original bytes; detect a login bounce)
    # ------------------------------------------------------------------ #
    def download(self, page: Any, target: DownloadTarget) -> bytes:
        """Fetch the ORIGINAL bytes for ``target`` via the authenticated context.

        Raises :class:`LoginFailed` when the store bounced us to a login/password
        page (or served HTML instead of the asset), so the orchestrator records a
        ``login_failed`` assist task prompting the operator to add credentials.
        """
        try:
            # Validates every redirect hop (the token legitimately 302s to a
            # signed CDN URL) so a 3xx can't bounce the fetch to a private host.
            resp = fetch_public(page.request, target.url, timeout_ms=_REQUEST_TIMEOUT_MS)
        except DownloadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DownloadError(f"request failed for {safe_host(target.url)}") from exc

        try:
            ok = bool(resp.ok)
            status = resp.status
        except Exception:  # noqa: BLE001 - older API shapes
            ok, status = True, 0
        try:
            final_url = (resp.url or "").lower()
        except Exception:  # noqa: BLE001
            final_url = ""
        try:
            headers = resp.headers or {}
        except Exception:  # noqa: BLE001
            headers = {}
        content_type = (headers.get("content-type") or "").lower()

        # A login-gated store redirects the token to /account/login (or /password)
        # and serves the HTML login page with a 200; an open store streams the
        # image/zip bytes. Treat any of these signals as "needs a customer login".
        if (
            not ok
            or content_type.startswith("text/html")
            or "/account/login" in final_url
            or "/password" in final_url
        ):
            logger.info(
                "shopify.download.login_bounce host=%s status=%s ctype=%s",
                safe_host(target.url),
                status,
                content_type or "?",
            )
            raise LoginFailed(
                "store requires customer login; add credentials for this vendor"
            )

        body = resp.body()
        if not body:
            raise DownloadError("empty response")
        if len(body) > _MAX_BYTES:
            raise DownloadError(f"asset exceeds {_MAX_BYTES} byte cap")

        # Adopt the server-suggested filename when present; else leave None and
        # let the caller fall back to the URL basename.
        suggested = _filename_from_disposition(headers.get("content-disposition"))
        if suggested:
            target.filename = suggested

        logger.info(
            "shopify.download host=%s bytes=%d filename=%s",
            safe_host(target.url),
            len(body),
            target.filename or "?",
        )
        return body

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _logged_in(self, page: Any) -> bool:
        """True when a logged-in customer marker (a logout link) is present."""
        try:
            return page.locator(_LOGOUT_SELECTOR).count() > 0
        except Exception:  # noqa: BLE001 - a hostile page must not crash login
            return False


def _filename_from_disposition(value: str | None) -> str | None:
    """Parse a download filename from a ``Content-Disposition`` header value.

    Prefers the RFC 5987 ``filename*=charset''pct-encoded`` form, then a quoted
    ``filename="..."``, then a bare ``filename=...``. Any directory components are
    stripped. Returns ``None`` when no filename is present.
    """
    if not value:
        return None

    # RFC 5987 extended form takes precedence (carries the real encoding).
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match:
        token = match.group(1).strip().strip('"')
        if "''" in token:
            token = token.split("''", 1)[1]
        try:
            token = urllib.parse.unquote(token)
        except Exception:  # noqa: BLE001
            pass
        name = _basename_only(token)
        if name:
            return name

    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
    if match:
        name = _basename_only(match.group(1))
        if name:
            return name

    match = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match:
        name = _basename_only(match.group(1).strip().strip('"'))
        if name:
            return name

    return None


def _basename_only(name: str) -> str:
    """Strip any path components and surrounding whitespace from ``name``."""
    return name.replace("\\", "/").rsplit("/", 1)[-1].strip()


__all__ = ["ShopifyDownloadsAdapter"]
