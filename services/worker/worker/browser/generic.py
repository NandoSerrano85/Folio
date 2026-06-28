"""Generic "direct image / obvious download" adapter (no login).

Registered under ``adapter_key='generic'`` and used as the fallback whenever an
allow-listed sender's email links out to an image but no per-vendor adapter is
mapped. Because the sender allow-list is the trust boundary (only operator-
approved senders ever reach here), a conservative generic fetch is safe and lets
"unmapped-but-simple" vendors work end to end.

It is deliberately conservative so it never stores marketing junk as a photo:

* ``find_download`` only yields a target when the link is *already* a direct
  image URL, or when the linked page exposes an obvious download (an ``<a
  download>``, a link/button whose text says "download"/"original"/"full res",
  or a single clearly-largest ``<img>``).
* ``download`` re-validates the fetched bytes against image magic numbers and
  raises :class:`DownloadError` for anything that is not a real image (e.g. an
  HTML error/login page served with a 200).

All network/DOM work goes through the Playwright ``page`` handed in by the
session manager, so it reuses the one budgeted Chromium and its cookie jar. No
Playwright import is needed here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from folio_core.logging import get_logger
from worker.browser.base import DownloadError, DownloadTarget, EmailRef, VendorAdapter
from worker.browser.registry import register_adapter

logger = get_logger("worker.browser.generic")

_IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff",
    "webp", "heic", "heif", "avif",
}

# Max distinct download candidates returned per email (bounds runtime + memory).
_MAX_TARGETS = 5
# Minimum on-page <img> dimension (px) to be considered a "real" photo, not an
# icon/tracking pixel/logo.
_MIN_IMG_DIM = 200

# JavaScript run in the page to surface obvious download candidates. Returns an
# ordered list of absolute URLs (download links first, largest image last).
_CANDIDATE_JS = """
() => {
  const out = [];
  const push = (u) => { if (u && /^https?:\\/\\//i.test(u)) out.push(u); };
  // 1. explicit download links
  document.querySelectorAll('a[download][href]').forEach(a => push(a.href));
  // 2. links/buttons whose text or href screams "download / original / full res"
  const re = /download|full[\\s-]?res|full[\\s-]?size|original|high[\\s-]?res|hi[\\s-]?res/i;
  document.querySelectorAll('a[href]').forEach(a => {
    if (re.test(a.textContent || '') || re.test(a.getAttribute('href') || '')) {
      push(a.href);
    }
  });
  // 3. the single clearly-largest image on the page
  let best = null, bestArea = 0;
  document.querySelectorAll('img').forEach(img => {
    const w = img.naturalWidth || img.width || 0;
    const h = img.naturalHeight || img.height || 0;
    const area = w * h;
    if (area > bestArea && w >= __MIN_DIM__ && h >= __MIN_DIM__) {
      bestArea = area;
      best = img.currentSrc || img.src;
    }
  });
  push(best);
  return out;
}
""".replace("__MIN_DIM__", str(_MIN_IMG_DIM))


@register_adapter
class GenericDownloadAdapter(VendorAdapter):
    """No-login adapter that fetches a direct/obvious original image."""

    adapter_key = "generic"
    login_required = False

    # No authentication for the generic flow.
    def login(self, page: Any, *, credentials: dict[str, Any] | None = None) -> None:
        return None

    def find_download(self, page: Any, email: EmailRef) -> list[DownloadTarget]:
        """Resolve the email's vendor links into concrete download targets."""
        targets: list[DownloadTarget] = []
        seen: set[str] = set()

        for url in email.vendor_links:
            if len(targets) >= _MAX_TARGETS:
                break

            # Direct image link -> take it as-is (no navigation needed).
            if _is_direct_image(url):
                if url not in seen:
                    seen.add(url)
                    targets.append(DownloadTarget(url=url, filename=_basename(url)))
                continue

            # Otherwise render the page and look for an obvious download/image.
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - a dead link must not abort others
                logger.info("generic.nav_failed host=%s", _safe_host(url))
                continue

            for candidate in _candidate_urls(page):
                if len(targets) >= _MAX_TARGETS:
                    break
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    targets.append(
                        DownloadTarget(url=candidate, filename=_basename(candidate))
                    )

        logger.info(
            "generic.find_download links=%d targets=%d",
            len(email.vendor_links),
            len(targets),
        )
        return targets

    def download(self, page: Any, target: DownloadTarget) -> bytes:
        """Fetch ``target`` and return the ORIGINAL bytes (validated as image)."""
        data = _fetch_bytes(page, target.url)
        if not data:
            raise DownloadError("empty response")
        if not _looks_like_image(data):
            raise DownloadError("response was not a recognised image")
        logger.info(
            "generic.download host=%s bytes=%d", _safe_host(target.url), len(data)
        )
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _candidate_urls(page: Any) -> list[str]:
    try:
        result = page.evaluate(_CANDIDATE_JS)
    except Exception:  # noqa: BLE001 - evaluate can fail on hostile pages
        return []
    if not isinstance(result, list):
        return []
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in result:
        if isinstance(item, str) and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _assert_public_url(url: str) -> None:
    """SSRF guard: reject non-http(s) URLs and hosts that resolve to a private,
    loopback, link-local, or otherwise non-public address. Email links are only
    as trusted as the sender allow-list, so refuse internal targets outright."""
    import ipaddress
    import socket

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise DownloadError(f"refusing non-http(s) scheme for {_safe_host(url)}")
    host = parts.hostname
    if not host:
        raise DownloadError("refusing url with no host")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DownloadError(f"cannot resolve {_safe_host(url)}") from exc
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
            raise DownloadError(f"refusing non-public address for {_safe_host(url)}")


def _fetch_bytes(page: Any, url: str) -> bytes:
    """Fetch ``url`` via the page's request context (shares cookies/session).

    Falls back to ``urllib`` when no live page is available (e.g. unit testing).
    """
    _assert_public_url(url)
    if page is not None:
        try:
            response = page.request.get(url)
        except Exception as exc:  # noqa: BLE001
            raise DownloadError(f"request failed for {_safe_host(url)}") from exc
        try:
            ok = response.ok
            status = response.status
        except Exception:  # noqa: BLE001 - older API shapes
            ok, status = True, 0
        if not ok:
            raise DownloadError(f"http {status} for {_safe_host(url)}")
        return response.body()

    # No browser -> plain HTTP GET (kept simple; the browser path is the norm).
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            return resp.read()
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"urllib fetch failed for {_safe_host(url)}") from exc


def _is_direct_image(url: str) -> bool:
    path = urlsplit(url).path
    if "." not in path:
        return False
    return path.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS


def _basename(url: str) -> str:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name or "image"


def _safe_host(url: str) -> str:
    try:
        return urlsplit(url).netloc or "?"
    except ValueError:
        return "?"


def _looks_like_image(data: bytes) -> bool:
    """True if ``data`` begins with a known image signature."""
    if len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":           # JPEG
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":       # PNG
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):     # GIF
        return True
    if data[:2] == b"BM":                       # BMP
        return True
    if data[:4] in (b"II*\x00", b"MM\x00*"):   # TIFF (LE / BE)
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP
        return True
    if data[4:8] == b"ftyp":                    # HEIC/HEIF/AVIF container
        brand = data[8:16]
        if any(b in brand for b in (b"heic", b"heif", b"avif", b"mif1", b"hevc")):
            return True
    return False


__all__ = ["GenericDownloadAdapter"]
