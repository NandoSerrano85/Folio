"""Low-level network safety helpers shared by every vendor adapter.

The single home for Folio's SSRF guard, :func:`assert_public_url`. Email links
are only as trusted as the sender allow-list, so before any adapter fetches a
vendor URL it must run this guard to refuse non-http(s) schemes and hosts that
resolve to a private / loopback / link-local / otherwise non-public address.

This module is import-clean (stdlib only) and has NO dependency on Playwright,
Google libraries, or the database, so it is safe to import from any adapter or
from pure unit tests.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

from worker.browser.base import DownloadError

__all__ = ["assert_public_url", "safe_host", "fetch_public"]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def safe_host(url: str) -> str:
    """Return ``url``'s host (sans any ``user:pass@`` userinfo) for log lines."""
    try:
        netloc = urlsplit(url).netloc or "?"
    except ValueError:
        return "?"
    # Strip credentials so an inline-userinfo URL can never leak into logs.
    return netloc.rsplit("@", 1)[-1] or "?"


def assert_public_url(url: str) -> None:
    """SSRF guard: reject non-http(s) URLs and hosts that resolve to a private,
    loopback, link-local, or otherwise non-public address. Email links are only
    as trusted as the sender allow-list, so refuse internal targets outright.

    Raises :class:`worker.browser.base.DownloadError` on any violation.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise DownloadError(f"refusing non-http(s) scheme for {safe_host(url)}")
    host = parts.hostname
    if not host:
        raise DownloadError("refusing url with no host")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DownloadError(f"cannot resolve {safe_host(url)}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise DownloadError(f"refusing non-public address for {safe_host(url)}")


def fetch_public(request_context: Any, url: str, *, timeout_ms: int | None = None,
                 max_hops: int = 10) -> Any:
    """GET ``url`` through a Playwright APIRequestContext, re-running the SSRF
    guard on EVERY redirect hop.

    ``page.request.get`` follows redirects automatically, so validating only the
    initial URL leaves a hole: a 3xx could bounce the fetch to a private/loopback
    host (e.g. cloud metadata). Here we disable auto-redirects (``max_redirects=0``)
    and follow them by hand, calling :func:`assert_public_url` on each ``Location``
    before fetching it. Cookies set along the way persist in the shared context
    jar, so an authenticated download (Shopify token -> signed CDN URL) still works.

    Returns the terminal ``APIResponse``. Raises :class:`DownloadError` on a
    blocked host or after ``max_hops`` redirects.
    """
    current = url
    for _ in range(max_hops + 1):
        assert_public_url(current)
        kwargs: dict[str, Any] = {"max_redirects": 0}
        if timeout_ms is not None:
            kwargs["timeout"] = timeout_ms
        resp = request_context.get(current, **kwargs)
        status = getattr(resp, "status", 0) or 0
        if status in _REDIRECT_STATUSES:
            location = (resp.headers or {}).get("location")
            if not location:
                return resp
            current = urljoin(current, location)
            continue
        return resp
    raise DownloadError(f"too many redirects for {safe_host(url)}")
