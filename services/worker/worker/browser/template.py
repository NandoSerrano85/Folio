"""TEMPLATE vendor adapter -- copy this file to add a real per-vendor flow.

WHEN YOU NEED THIS
------------------
``worker discover-senders`` surfaces the senders that mail you images. For most
of them the generic adapter (:mod:`worker.browser.generic`) already fetches the
original straight from a link. You only need a bespoke adapter when a vendor
hides the original behind a *login + click-to-download* web flow -- e.g. a school
photographer or a print lab whose email just says "your gallery is ready".

HOW TO USE THIS TEMPLATE
------------------------
1. Copy this file to ``worker/browser/<vendor>.py`` (e.g. ``acme_photos.py``).
2. Set :attr:`adapter_key` to a short stable slug and store that SAME slug in the
   vendor's ``vendors.adapter_key`` column (the portal / a SQL insert), and point
   the matching ``senders.vendor_id`` at that vendor.
3. If the vendor needs a login, set ``login_required = True`` and store the
   credentials ENCRYPTED in ``vendor_credentials`` (via the portal). They arrive
   in :meth:`login` already decrypted as the ``credentials`` dict.
4. Fill in the three methods below using your browser's devtools to find the
   right selectors. UNCOMMENT the ``@register_adapter`` decorator.
5. Register the module so it loads: add ``from worker.browser import <vendor>``
   to :func:`worker.browser.registry.load_builtin_adapters`.
6. ``py_compile`` your file, then test off-hours with ``worker sync-gmail``.

CONTRACT / SAFETY NOTES
-----------------------
* The three methods receive a live Playwright ``page`` from the budgeted, single
  off-hours Chromium. Do NOT launch your own browser.
* NEVER log credential values. The ``credentials`` dict holds plaintext secrets.
* Raise the typed adapter errors so the orchestrator records a human-assist task
  instead of crashing the run:
    - :class:`CaptchaEncountered` -> assist reason ``captcha``
    - :class:`LoginFailed`        -> assist reason ``login_failed``
    - :class:`AntiBotBlocked`     -> assist reason ``captcha``
    - :class:`DownloadError`      -> that one asset is skipped
* ``download`` must return the ORIGINAL bytes. The common pipeline computes the
  sha256 on exactly these bytes BEFORE any EXIF stamping, so return the file the
  vendor served -- do not transcode/resize it.

This module is import-clean and is NOT auto-registered, so leaving it in the tree
costs nothing.
"""

from __future__ import annotations

from typing import Any

from folio_core.logging import get_logger
from worker.browser.base import (
    AntiBotBlocked,
    CaptchaEncountered,
    DownloadError,
    DownloadTarget,
    EmailRef,
    LoginFailed,
    VendorAdapter,
)

# from worker.browser.registry import register_adapter  # <- uncomment to enable

logger = get_logger("worker.browser.template")


# @register_adapter   # <-- UNCOMMENT this line once the adapter is filled in.
class TemplateVendorAdapter(VendorAdapter):
    """Example login + click-to-download adapter. Copy and customise."""

    # The slug that ties this class to ``vendors.adapter_key``. CHANGE THIS.
    adapter_key = "example_vendor"
    # Flip to False if the gallery is reachable straight from the email link.
    login_required = True

    # Optional: a fallback login URL if the credential row doesn't carry one.
    LOGIN_URL = "https://vendor.example.com/login"

    # ------------------------------------------------------------------ #
    # 1. login
    # ------------------------------------------------------------------ #
    def login(self, page: Any, *, credentials: dict[str, Any] | None = None) -> None:
        """Authenticate the session if needed.

        ``credentials`` (when present) is the DECRYPTED dict from
        ``vendor_credentials``::

            {"login_url": str|None, "username": str|None,
             "secret": str|None, "extra": {...}}
        """
        # If the vendor needs no login, just `return` and set login_required=False.
        if not credentials:
            raise LoginFailed("no credentials stored for this vendor")

        # Cheap re-use: if a prior message already logged us in this run, the
        # session cookie is still in the context -- skip re-login by checking a
        # logged-in marker. Example:
        #   if page.locator("a.logout").count() > 0:
        #       return

        login_url = credentials.get("login_url") or self.LOGIN_URL
        username = credentials.get("username")
        secret = credentials.get("secret")

        try:
            page.goto(login_url, wait_until="domcontentloaded")
            # --- CUSTOMISE these selectors to the vendor's login form ------- #
            page.fill("input[name='email']", username or "")
            page.fill("input[name='password']", secret or "")
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")
        except Exception as exc:  # noqa: BLE001
            # Never include `username`/`secret` in the message.
            raise LoginFailed("login form interaction failed") from exc

        # Detect a CAPTCHA / anti-bot wall and surface it as an assist task.
        # if page.locator("iframe[src*='recaptcha']").count() > 0:
        #     raise CaptchaEncountered("recaptcha on login")
        # if "Access Denied" in (page.title() or ""):
        #     raise AntiBotBlocked("WAF block page")

        # Confirm we actually logged in (else creds are likely wrong).
        # if page.locator("a.logout").count() == 0:
        #     raise LoginFailed("no logged-in marker after submit")

    # ------------------------------------------------------------------ #
    # 2. find_download
    # ------------------------------------------------------------------ #
    def find_download(self, page: Any, email: EmailRef) -> list[DownloadTarget]:
        """Navigate from the email's gallery link to the downloadable original(s).

        Return one :class:`DownloadTarget` per asset. For click-to-download
        vendors the real URL isn't known until the click, so stash the selector
        to click in ``extra`` and let :meth:`download` perform the capture.
        """
        if not email.vendor_links:
            return []

        gallery_url = email.vendor_links[0]
        try:
            page.goto(gallery_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise DownloadError("could not open the gallery page") from exc

        # if page.locator("iframe[src*='captcha']").count() > 0:
        #     raise CaptchaEncountered("captcha on gallery")

        targets: list[DownloadTarget] = []
        # --- CUSTOMISE: enumerate each downloadable item on the page -------- #
        # Example: one "Download original" button per photo tile.
        # buttons = page.locator("button.download-original")
        # for i in range(buttons.count()):
        #     targets.append(
        #         DownloadTarget(
        #             url=gallery_url,                       # nominal provenance URL
        #             extra={"click_selector": "button.download-original", "index": i},
        #         )
        #     )

        logger.info("template.find_download targets=%d", len(targets))
        return targets

    # ------------------------------------------------------------------ #
    # 3. download
    # ------------------------------------------------------------------ #
    def download(self, page: Any, target: DownloadTarget) -> bytes:
        """Fetch the ORIGINAL bytes for ``target``.

        Two common shapes:

        A) Direct URL is already known -> fetch via the page's request context so
           the authenticated cookies apply::

               resp = page.request.get(target.url)
               if not resp.ok:
                   raise DownloadError(f"http {resp.status}")
               return resp.body()

        B) Click-to-download -> capture the browser download event and read the
           bytes off disk (Playwright stored it under BROWSER_DOWNLOAD_DIR)::
        """
        selector = target.extra.get("click_selector")
        if not selector:
            # Shape A: direct URL.
            try:
                resp = page.request.get(target.url)
            except Exception as exc:  # noqa: BLE001
                raise DownloadError("request failed") from exc
            if not resp.ok:
                raise DownloadError(f"http {resp.status}")
            return resp.body()

        # Shape B: click + capture.
        index = target.extra.get("index", 0)
        try:
            with page.expect_download() as download_info:
                page.locator(selector).nth(index).click()
            download = download_info.value
            from pathlib import Path

            path = download.path()  # Playwright wrote it under downloads_path
            data = Path(path).read_bytes()
        except Exception as exc:  # noqa: BLE001
            raise DownloadError("click-to-download capture failed") from exc

        if not data:
            raise DownloadError("captured download was empty")
        return data


# Re-exported so a copy can `from worker.browser.template import TemplateVendorAdapter`.
__all__ = ["TemplateVendorAdapter"]

# Silence "imported but unused" for the example-only error types referenced in
# the commented guidance above; they are part of the documented contract.
_DOCUMENTED_ERRORS = (CaptchaEncountered, AntiBotBlocked)
