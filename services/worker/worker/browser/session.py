"""Playwright/Chromium session manager -- RAM-budgeted, sequential, off-hours.

This is the single place a headless Chromium is launched. The whole design is
shaped by the hard 8 GB RAM ceiling on the NAS: Chromium is the heaviest thing
the worker ever runs, so this module guarantees that

1. **only ONE browser job runs at a time** -- an in-process :class:`threading.Lock`
   plus a cross-process ``flock`` on a lock file (so a scheduled run and a manual
   ``worker sync-gmail`` can never launch two Chromiums at once), honouring
   ``VENDOR_BROWSER_MAX_JOBS=1``; and
2. **the browser only opens off-hours** -- :func:`in_offhours` gates on the
   ``BROWSER_OFFHOURS_START..END`` window in ``settings.timezone`` (the scheduler
   gates too, but enforcing it here protects every caller, including the CLI).

Chromium is launched with an aggressive low-memory flag set (no shared-memory
``/dev/shm`` reliance -- critical with the compose ``shm_size=512mb`` -- a capped
V8 heap, no GPU, fewer helper processes). The :func:`browser_session` context
manager yields a ready ``page`` whose context accepts downloads into
``BROWSER_DOWNLOAD_DIR``, applies the configured navigation timeout, and is always
torn down (page -> context -> browser -> playwright) on exit.

Playwright is imported LAZILY inside :func:`browser_session`, so importing this
module never requires Playwright/Chromium to be installed.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from folio_core.config import get_settings
from folio_core.logging import get_logger

logger = get_logger("worker.browser.session")

# In-process guard. Cross-process exclusion is added via an flock lock file.
_PROC_LOCK = threading.Lock()
_LOCK_FILENAME = ".folio-browser.lock"

# A plausible desktop UA so vendor sites serve their normal (download-capable)
# pages rather than a stripped bot/mobile variant.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Low-memory Chromium launch flags. Tuned for an 8 GB all-NVMe NAS where the
# worker is capped at 2.5g and shares shm_size=512mb.
_LOW_MEM_ARGS = [
    "--no-sandbox",                       # required when running as root in a container
    "--disable-dev-shm-usage",            # write to /tmp not /dev/shm (512mb cap)
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=site-per-process,TranslateUI,IsolateOrigins",  # fewer procs
    "--renderer-process-limit=2",
    "--no-zygote",                        # one fewer helper process
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
    "--js-flags=--max-old-space-size=256",  # cap V8 heap (~256 MB)
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class BrowserUnavailable(RuntimeError):
    """The browser could not be opened right now (off-hours or already busy)."""


class OffHoursError(BrowserUnavailable):
    """The current local time is outside the configured browser window."""


class BrowserBusyError(BrowserUnavailable):
    """Another vendor-browser job already holds the single-job lock."""


# --------------------------------------------------------------------------- #
# Off-hours gating
# --------------------------------------------------------------------------- #
def in_offhours(now: datetime | None = None) -> bool:
    """Return True when the local hour is inside the off-hours window.

    The window is ``[BROWSER_OFFHOURS_START, BROWSER_OFFHOURS_END)`` evaluated in
    ``settings.timezone``. Supports an overnight window (start > end, e.g. 22..6).
    A degenerate ``start == end`` window is treated as "never" (fail safe: no
    browser) rather than "always".
    """
    settings = get_settings()
    start = settings.browser_offhours_start
    end = settings.browser_offhours_end
    if now is None:
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(settings.timezone))
        except Exception:  # noqa: BLE001 - a bad tz name must not crash gating
            now = datetime.now()
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # Overnight window, e.g. 22..6 -> in window if hour >= 22 OR hour < 6.
    return hour >= start or hour < end


# --------------------------------------------------------------------------- #
# Cross-process lock (flock)
# --------------------------------------------------------------------------- #
def _acquire_file_lock(path: Path):
    """Non-blocking exclusive flock on ``path``; returns the open handle or None.

    Returns ``None`` on platforms without ``fcntl`` (the in-process lock still
    applies). Raises :class:`BrowserBusyError` if the lock is already held by
    another process.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-posix dev only
        return None

    handle = open(path, "w")  # noqa: SIM115 - kept open for the lock's lifetime
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BrowserBusyError(
            "Another vendor-browser job holds the cross-process lock."
        ) from exc
    return handle


def _release_file_lock(handle) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 - best-effort unlock
        pass
    try:
        handle.close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# The session context manager
# --------------------------------------------------------------------------- #
@contextmanager
def browser_session(
    *,
    ignore_offhours: bool = False,
    download_dir: Path | str | None = None,
) -> Iterator[Any]:
    """Yield a ready Playwright ``page`` inside a fully managed Chromium session.

    Guarantees, in order:
      * off-hours gating (unless ``ignore_offhours``) -> :class:`OffHoursError`;
      * single-job exclusion (in-process + cross-process) -> :class:`BrowserBusyError`;
      * a low-memory headless Chromium with downloads captured into
        ``download_dir`` (default ``BROWSER_DOWNLOAD_DIR``);
      * unconditional teardown of page/context/browser/playwright on exit.

    The yielded object is the raw Playwright ``page`` (typed ``Any`` to match the
    frozen :class:`~worker.gmail.sync.VendorAdapter` method signatures).
    """
    settings = get_settings()

    # VENDOR_BROWSER_MAX_JOBS MUST stay 1 on 8 GB RAM. We do not honour a higher
    # value -- the lock enforces 1 regardless -- but we surface the misconfig.
    if settings.vendor_browser_max_jobs != 1:
        logger.warning(
            "browser.max_jobs_misconfigured value=%d -- enforcing 1 (8 GB RAM cap)",
            settings.vendor_browser_max_jobs,
        )

    if not ignore_offhours and not in_offhours():
        raise OffHoursError(
            "Outside the browser off-hours window "
            f"[{settings.browser_offhours_start},{settings.browser_offhours_end}) "
            f"in {settings.timezone}."
        )

    dl_dir = Path(download_dir) if download_dir is not None else Path(
        settings.browser_download_dir
    )
    dl_dir.mkdir(parents=True, exist_ok=True)

    # --- single-job exclusion ---------------------------------------------- #
    if not _PROC_LOCK.acquire(blocking=False):
        raise BrowserBusyError(
            "Another vendor-browser job is already running in this process."
        )

    lock_handle = None
    try:
        lock_handle = _acquire_file_lock(dl_dir / _LOCK_FILENAME)

        # --- launch Chromium (LAZY playwright import) ---------------------- #
        from playwright.sync_api import sync_playwright

        nav_ms = max(1, settings.browser_nav_timeout_seconds) * 1000
        playwright = sync_playwright().start()
        browser = None
        context = None
        page = None
        try:
            browser = playwright.chromium.launch(
                headless=settings.browser_headless,
                args=_LOW_MEM_ARGS,
                downloads_path=str(dl_dir),
            )
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1024, "height": 768},
                user_agent=_USER_AGENT,
            )
            context.set_default_navigation_timeout(nav_ms)
            context.set_default_timeout(nav_ms)
            page = context.new_page()
            logger.info(
                "browser.session_open headless=%s nav_timeout_s=%d download_dir=%s",
                settings.browser_headless,
                settings.browser_nav_timeout_seconds,
                dl_dir,
            )
            yield page
        finally:
            # Tear down inner -> outer; never let a cleanup error mask the body.
            for closer, name in (
                (lambda: page.close() if page is not None else None, "page"),
                (lambda: context.close() if context is not None else None, "context"),
                (lambda: browser.close() if browser is not None else None, "browser"),
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    logger.warning("browser.cleanup_failed step=%s", name)
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                logger.warning("browser.cleanup_failed step=playwright")
            logger.info("browser.session_close")
    finally:
        _release_file_lock(lock_handle)
        _PROC_LOCK.release()


__all__ = [
    "browser_session",
    "in_offhours",
    "BrowserUnavailable",
    "OffHoursError",
    "BrowserBusyError",
]
