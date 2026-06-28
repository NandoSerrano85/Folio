"""On-demand thumbnail generation with Pillow, cached to ``THUMBNAIL_ROOT``.

Cache key is ``{image_id}_{size}.jpg``. Generation is best-effort: corrupt or
non-image source files fall back to a generated placeholder so the gallery never
shows a broken tile. ``generate_thumbnail`` always returns a readable JPEG path.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage, ImageColor, ImageOps, UnidentifiedImageError

from folio_core.config import get_settings
from folio_core.logging import get_logger

logger = get_logger("portal.thumbnails")

MIN_SIZE = 16
MAX_SIZE = 1024
_PLACEHOLDER_BG = "#e5e7eb"


def _thumb_root() -> Path:
    root = Path(get_settings().thumbnail_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def clamp_size(size: int | None) -> int:
    """Clamp a requested edge size to a sane range, defaulting from settings."""
    if not size or size <= 0:
        size = get_settings().thumbnail_default_size
    return max(MIN_SIZE, min(MAX_SIZE, int(size)))


def cache_path(image_id: int, size: int) -> Path:
    """Deterministic cache path for an image/size thumbnail."""
    return _thumb_root() / f"{image_id}_{size}.jpg"


def _placeholder_path(size: int) -> Path:
    return _thumb_root() / f"placeholder_{size}.jpg"


def _ensure_placeholder(size: int) -> Path:
    path = _placeholder_path(size)
    if path.exists():
        return path
    try:
        img = PILImage.new("RGB", (size, size), ImageColor.getrgb(_PLACEHOLDER_BG))
        img.save(path, format="JPEG", quality=70)
    except Exception:  # noqa: BLE001 - last-ditch; should never fail
        logger.exception("thumbnail.placeholder_failed size=%s", size)
    return path


def generate_thumbnail(
    image_id: int,
    source_path: Path | None,
    size: int | None = None,
) -> Path:
    """Return a cached thumbnail JPEG path, generating it on first request.

    Falls back to a neutral placeholder for missing/corrupt/non-image sources.
    Honors EXIF orientation and never upscales beyond the source dimensions.
    """
    size = clamp_size(size)
    cached = cache_path(image_id, size)
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    if source_path is None or not source_path.is_file():
        return _ensure_placeholder(size)

    try:
        with PILImage.open(source_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((size, size), PILImage.Resampling.LANCZOS)
            img.save(cached, format="JPEG", quality=82, optimize=True)
        return cached
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning(
            "thumbnail.generate_failed image_id=%s path=%s err=%s",
            image_id,
            source_path,
            exc,
        )
        return _ensure_placeholder(size)
    except Exception:  # noqa: BLE001 - any Pillow edge case -> placeholder
        logger.exception("thumbnail.generate_error image_id=%s", image_id)
        return _ensure_placeholder(size)


__all__ = [
    "generate_thumbnail",
    "cache_path",
    "clamp_size",
    "MIN_SIZE",
    "MAX_SIZE",
]
