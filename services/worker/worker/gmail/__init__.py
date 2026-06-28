"""Gmail integration for the Folio worker.

This subpackage owns three responsibilities:

* :mod:`worker.gmail.auth` — the installed-app OAuth consent flow
  (``gmail.readonly`` scope) plus an authorized Gmail API service builder with
  automatic token refresh.
* :mod:`worker.gmail.discover` — mailbox scanning that aggregates the senders
  who deliver images/links, feeding the portal allow-list (and doubling as a
  first pass at vendor discovery).
* :mod:`worker.gmail.sync` — the *framework stub* for future headless-browser
  vendor ingestion (Phase 3); it imports cleanly and runs as a no-op-with-
  logging today so the ``worker sync-gmail`` command works.

Leaf entry points the worker CLI imports (see ``worker/main.py``):

    run_gmail_auth(account_email: str) -> None
    run_discover_senders(account_email: str | None = None) -> ...
    run_gmail_sync(account_email: str | None = None) -> None
"""

from __future__ import annotations

__all__ = [
    "run_gmail_auth",
    "run_discover_senders",
    "run_gmail_sync",
]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export shim
    """Lazily re-export the leaf ``run_*`` callables.

    Imports are deferred so that merely importing :mod:`worker.gmail` never
    drags in the Google client libraries (kept inside the leaf functions).
    """
    if name == "run_gmail_auth":
        from worker.gmail.auth import run_gmail_auth

        return run_gmail_auth
    if name == "run_discover_senders":
        from worker.gmail.discover import run_discover_senders

        return run_discover_senders
    if name == "run_gmail_sync":
        from worker.gmail.sync import run_gmail_sync

        return run_gmail_sync
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
