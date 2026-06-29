"""Derive ``image_sources.vendor_id`` from Drive folder paths.

Drive imports land with ``vendor_id IS NULL`` but they DO carry a
``drive_folder_path`` (an ``"A/B/C"`` string). This command mines those paths
(and, optionally, the file basename) for a representative vendor name and
backfills the vendor FK using :func:`folio_core.vendors.get_or_create_vendor`.

It is DRY-RUN by default: it logs what it *would* do (each derived vendor name,
how many images map to it, a couple of example paths, and totals) so the
operator can judge the rule against real data before committing. Pass
``apply=True`` to actually write and commit.

Strategies (token = an eligible folder segment, original casing preserved):
  * ``frequent`` (default): count every eligible token's global frequency across
    the whole target set, then for each source pick its own token with the
    highest global count (ties broken toward the DEEPEST / last path segment).
  * ``parent``: the last (deepest) eligible folder segment.
  * ``top``: the first (shallowest) eligible folder segment.

Eligibility: a token is dropped when it is numeric-only, shorter than
``min_len``, in the (case-insensitive) stoplist, or equal to the owning
account's label or email local-part.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from sqlalchemy import select

from folio_core.config import get_settings
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import Account, Image, ImageSource, SourceTypeEnum

logger = get_logger("worker.derive_vendors")

_VALID_STRATEGIES = frozenset({"frequent", "parent", "top"})
# Filename tokenization: split on space, underscore, dot, dash.
_FILENAME_SPLIT = re.compile(r"[ _.\-]+")
# A token that is purely digits (optionally with separators) carries no vendor
# signal — dates, sequence numbers, order ids, etc.
_NUMERIC_ONLY = re.compile(r"^\d+$")
_MAX_NAME_LEN = 200  # cap derived vendor NAMES (fits Vendor.name); the adapter_key
# slug is separately bounded to 128 inside folio_core.vendors.get_or_create_vendor.


def _parse_stoplist(raw: str) -> set[str]:
    """Return a lowercase set of stop-tokens from a comma/space-separated string."""
    return {tok.lower() for tok in re.split(r"[,\s]+", raw or "") if tok}


def _account_labels(account: Account) -> set[str]:
    """Lowercase tokens identifying the account itself (never a vendor)."""
    labels: set[str] = set()
    if account.label:
        labels.add(account.label.strip().lower())
    if account.email:
        local = account.email.split("@", 1)[0].strip().lower()
        if local:
            labels.add(local)
    return labels


def _is_eligible(
    token: str, *, min_len: int, stoplist: set[str], account_labels: set[str]
) -> bool:
    stripped = token.strip()
    if not stripped:
        return False
    if len(stripped) < min_len:
        return False
    if _NUMERIC_ONLY.match(stripped):
        return False
    low = stripped.lower()
    if low in stoplist or low in account_labels:
        return False
    return True


def _filename_tokens(filename: str | None) -> list[str]:
    """Tokenize a basename, dropping the extension and empty fragments."""
    if not filename:
        return []
    base = filename.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return [tok for tok in _FILENAME_SPLIT.split(base) if tok]


def _candidate_tokens(
    path: str | None,
    filename: str | None,
    *,
    include_filename: bool,
    min_len: int,
    stoplist: set[str],
    account_labels: set[str],
) -> list[str]:
    """Ordered list of eligible tokens for a source (path first, then filename).

    Order is shallow -> deep so ``top`` == first and ``parent`` == last folder
    segment; filename tokens (when included) are appended after the path so they
    never outrank a real folder for ``parent``/``top``.
    """
    raw: list[str] = []
    if path:
        raw.extend(seg for seg in path.split("/") if seg.strip())
    if include_filename:
        raw.extend(_filename_tokens(filename))
    return [
        seg
        for seg in raw
        if _is_eligible(
            seg, min_len=min_len, stoplist=stoplist, account_labels=account_labels
        )
    ]


def _folder_tokens(
    path: str | None,
    *,
    min_len: int,
    stoplist: set[str],
    account_labels: set[str],
) -> list[str]:
    """Eligible folder segments only (shallow -> deep), for parent/top."""
    if not path:
        return []
    return [
        seg
        for seg in path.split("/")
        if seg.strip()
        and _is_eligible(
            seg, min_len=min_len, stoplist=stoplist, account_labels=account_labels
        )
    ]


def _pick_name(
    *,
    strategy: str,
    path: str | None,
    filename: str | None,
    include_filename: bool,
    min_len: int,
    stoplist: set[str],
    account_labels: set[str],
    global_counts: Counter[str],
) -> str | None:
    """Return the derived vendor name for one source, or None if undecidable."""
    if strategy == "top":
        folders = _folder_tokens(
            path, min_len=min_len, stoplist=stoplist, account_labels=account_labels
        )
        return folders[0] if folders else None
    if strategy == "parent":
        folders = _folder_tokens(
            path, min_len=min_len, stoplist=stoplist, account_labels=account_labels
        )
        return folders[-1] if folders else None

    # strategy == "frequent": pick the source's own token with the highest
    # GLOBAL frequency; tie -> the deepest/last candidate (so we iterate in
    # order and use >= to let later, deeper tokens win ties).
    candidates = _candidate_tokens(
        path,
        filename,
        include_filename=include_filename,
        min_len=min_len,
        stoplist=stoplist,
        account_labels=account_labels,
    )
    if not candidates:
        return None
    best: str | None = None
    best_count = -1
    for tok in candidates:
        count = global_counts.get(tok.lower(), 0)
        if count >= best_count:
            best_count = count
            best = tok
    return best


def _load_targets(
    session, account_email: str | None, only_unmapped: bool
) -> list[tuple[int, int, str | None, str | None, int]]:
    """Return (source_id, image_id, drive_folder_path, original_filename, account_id)."""
    query = (
        select(
            ImageSource.id,
            ImageSource.image_id,
            ImageSource.drive_folder_path,
            Image.original_filename,
            ImageSource.account_id,
        )
        .join(Image, Image.id == ImageSource.image_id)
        .where(ImageSource.source_type == SourceTypeEnum.drive)
    )
    if only_unmapped:
        query = query.where(ImageSource.vendor_id.is_(None))
    if account_email:
        query = query.join(Account, Account.id == ImageSource.account_id).where(
            Account.email == account_email
        )
    return [tuple(row) for row in session.execute(query.order_by(ImageSource.id))]


def run_derive_vendors(
    account_email: str | None = None,
    *,
    apply: bool = False,
    strategy: str | None = None,
    include_filename: bool | None = None,
    only_unmapped: bool = True,
) -> None:
    """Derive and (optionally) backfill vendor_id from Drive folder paths.

    DRY-RUN by default (``apply=False``): logs a summary and writes nothing.
    """
    settings = get_settings()
    if strategy is None:
        strategy = settings.vendor_derive_strategy
    if include_filename is None:
        include_filename = settings.vendor_derive_include_filename
    min_len = settings.vendor_derive_min_len
    stoplist = _parse_stoplist(settings.vendor_derive_stoplist)

    strategy = (strategy or "frequent").strip().lower()
    if strategy not in _VALID_STRATEGIES:
        logger.error(
            "derive_vendors.bad_strategy strategy=%s valid=%s",
            strategy,
            sorted(_VALID_STRATEGIES),
        )
        return

    logger.info(
        "derive_vendors.start mode=%s strategy=%s include_filename=%s "
        "only_unmapped=%s min_len=%s stoplist=%d account=%s",
        "APPLY" if apply else "dry-run",
        strategy,
        include_filename,
        only_unmapped,
        min_len,
        len(stoplist),
        account_email or "<all>",
    )

    with session_scope() as session:
        rows = _load_targets(session, account_email, only_unmapped)
        if not rows:
            logger.warning(
                "derive_vendors.no_targets account=%s only_unmapped=%s",
                account_email or "<all>",
                only_unmapped,
            )
            return

        # Cache per-account label sets so account-name tokens are excluded.
        account_labels_cache: dict[int, set[str]] = {}

        def _labels_for(account_id: int) -> set[str]:
            if account_id not in account_labels_cache:
                acc = session.get(Account, account_id)
                account_labels_cache[account_id] = (
                    _account_labels(acc) if acc is not None else set()
                )
            return account_labels_cache[account_id]

        # First pass (frequent only): global token frequency across all targets.
        global_counts: Counter[str] = Counter()
        if strategy == "frequent":
            for _sid, _img, path, filename, account_id in rows:
                for tok in _candidate_tokens(
                    path,
                    filename,
                    include_filename=include_filename,
                    min_len=min_len,
                    stoplist=stoplist,
                    account_labels=_labels_for(account_id),
                ):
                    global_counts[tok.lower()] += 1

        # Second pass: derive a name per source.
        # name -> set of image_ids (dedupe images that have multiple sources)
        name_images: dict[str, set[int]] = defaultdict(set)
        name_examples: dict[str, list[str]] = defaultdict(list)
        derived: list[tuple[int, str]] = []  # (source_id, name)
        no_derivation_images: set[int] = set()
        all_images: set[int] = set()

        for source_id, image_id, path, filename, account_id in rows:
            all_images.add(image_id)
            name = _pick_name(
                strategy=strategy,
                path=path,
                filename=filename,
                include_filename=include_filename,
                min_len=min_len,
                stoplist=stoplist,
                account_labels=_labels_for(account_id),
                global_counts=global_counts,
            )
            if name is None:
                no_derivation_images.add(image_id)
                continue
            derived.append((source_id, name))
            name_images[name].add(image_id)
            if len(name_examples[name]) < 2 and path:
                name_examples[name].append(path)

        images_with = {img for imgs in name_images.values() for img in imgs}
        images_without = all_images - images_with

        # ---- Summary (always logged) -------------------------------------- #
        logger.info(
            "derive_vendors.summary sources=%d images=%d distinct_vendors=%d "
            "images_with_derivation=%d images_without=%d",
            len(rows),
            len(all_images),
            len(name_images),
            len(images_with),
            len(images_without),
        )
        for name in sorted(
            name_images, key=lambda n: (-len(name_images[n]), n.lower())
        ):
            examples = ", ".join(name_examples.get(name, [])) or "<no path>"
            logger.info(
                "derive_vendors.vendor name=%r images=%d examples=[%s]",
                name,
                len(name_images[name]),
                examples,
            )

        if not apply:
            logger.info(
                "derive_vendors.dry_run_complete (no changes written). "
                "Re-run with --apply to backfill %d sources across %d vendors.",
                len(derived),
                len(name_images),
            )
            return

        # ---- Apply -------------------------------------------------------- #
        from folio_core.vendors import get_or_create_vendor
        from folio_core.models import Vendor

        # Capture pre-existing vendor ids so we can report how many are NEW.
        existing_vendor_ids = set(session.scalars(select(Vendor.id)))

        # Resolve/create each distinct vendor once (name bounded for adapter_key).
        vendor_id_by_name: dict[str, int] = {}
        for name in name_images:
            vendor = get_or_create_vendor(session, name[:_MAX_NAME_LEN])
            vendor_id_by_name[name] = vendor.id

        created_count = len(
            set(vendor_id_by_name.values()) - existing_vendor_ids
        )

        updated_sources = 0
        for source_id, name in derived:
            vid = vendor_id_by_name.get(name)
            if vid is None:
                continue
            session.execute(
                ImageSource.__table__.update()
                .where(ImageSource.id == source_id)
                .values(vendor_id=vid)
            )
            updated_sources += 1

        logger.info(
            "derive_vendors.applied vendors_created=%d vendors_touched=%d "
            "sources_updated=%d images_mapped=%d images_unmapped=%d",
            created_count,
            len(vendor_id_by_name),
            updated_sources,
            len(images_with),
            len(images_without),
        )
        # session_scope commits on exit.


__all__ = ["run_derive_vendors"]
