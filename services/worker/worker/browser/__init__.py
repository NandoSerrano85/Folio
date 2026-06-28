"""Headless-browser vendor ingestion subpackage.

This package drives a single, RAM-budgeted headless Chromium (via Playwright) to
fetch the ORIGINAL image bytes for vendors that hide them behind a *web download*
flow rather than an email attachment. It is the per-vendor automation layer that
:mod:`worker.gmail.sync` orchestrates.

Layout
------
* :mod:`worker.browser.session` -- the Chromium session manager: lazy Playwright
  import, low-memory launch flags, a process-level (in-process + cross-process)
  lock so only ONE browser job ever runs at a time, off-hours gating, and a
  context manager that yields a ready ``page``.
* :mod:`worker.browser.base` -- the :class:`VendorAdapter` ABC (re-exported from
  the FROZEN definition in :mod:`worker.gmail.sync`), the adapter exception
  hierarchy, and a credential loader that decrypts ``vendor_credentials`` rows.
* :mod:`worker.browser.registry` -- the adapter registry (the same one defined in
  :mod:`worker.gmail.sync`) plus :func:`load_builtin_adapters`.
* :mod:`worker.browser.generic` -- a concrete, no-login "direct image / obvious
  download" adapter registered under ``adapter_key='generic'``.
* :mod:`worker.browser.template` -- a heavily-commented template to copy when a
  real per-vendor login+download flow needs to be added.

Importing this package never imports Playwright; the heavy import lives inside
:func:`worker.browser.session.browser_session`.
"""

from __future__ import annotations

__all__ = [
    "browser_session",
    "in_offhours",
    "BrowserUnavailable",
    "OffHoursError",
    "BrowserBusyError",
    "VendorAdapter",
    "DownloadTarget",
    "EmailRef",
    "AdapterError",
    "CaptchaEncountered",
    "AntiBotBlocked",
    "LoginFailed",
    "DownloadError",
    "load_vendor_credentials",
    "register_adapter",
    "get_adapter",
    "load_builtin_adapters",
    "ADAPTER_REGISTRY",
]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export shim
    """Lazily re-export submodule symbols without importing Playwright eagerly."""
    if name in {"browser_session", "in_offhours", "BrowserUnavailable",
                "OffHoursError", "BrowserBusyError"}:
        from worker.browser import session

        return getattr(session, name)
    if name in {"VendorAdapter", "DownloadTarget", "EmailRef", "AdapterError",
                "CaptchaEncountered", "AntiBotBlocked", "LoginFailed",
                "DownloadError", "load_vendor_credentials"}:
        from worker.browser import base

        return getattr(base, name)
    if name in {"register_adapter", "get_adapter", "load_builtin_adapters",
                "ADAPTER_REGISTRY"}:
        from worker.browser import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
