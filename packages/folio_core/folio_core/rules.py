"""Collection rules — auto-tagging images by ONE condition + up to two actions.

A *rule* matches images by a single condition and, for every matching image,
applies up to two actions: assign a vendor to the image's sources and/or add the
image to a folder. A rule must set at least ONE action.

Shape: ``{field, value?, account_id?, vendor_id?, folder_id?, enabled}``.

The ``field`` selects the condition (all matches are case-insensitive
substring matches via ILIKE, except ``account`` which is an id equality):

  * ``sender``   -> EXISTS(image_sources s: ``s.email_sender`` ILIKE %value%)
  * ``domain``   -> strip a leading ``'@'`` from value, then
                    EXISTS(s.email_sender ILIKE %@<domain>%)
  * ``subject``  -> EXISTS(s.email_subject ILIKE %value%)
  * ``filename`` -> ``images.original_filename`` ILIKE %value%
  * ``account``  -> EXISTS(s.account_id == account_id)  (uses the ``account_id``
                    column, NOT ``value``)

Every value is bound as a parameter — NEVER string-interpolated into SQL. ILIKE
patterns escape ``%`` and ``_`` (and the escape char) in the user value and then
wrap it with ``%...%``. A rule with no usable condition, or with no action, is
skipped — never applied.

Actions, per matching image:

  * ``vendor_id`` -> ``UPDATE image_sources SET vendor_id=:vid`` for every source
    of every matching image (overwrites the existing vendor, mirroring the
    design's ``applyVendorTo``). ``'vendored'`` = rows updated.
  * ``folder_id`` -> ``INSERT INTO folder_images(folder_id, image_id, added_at)
    SELECT :fid, i.id, now() ... ON CONFLICT DO NOTHING RETURNING image_id``.
    ``'filed'`` = number of rows actually inserted.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from folio_core.logging import get_logger
from folio_core.models import (
    CollectionRule,
    FolderImage,
    Image,
    ImageSource,
)

logger = get_logger("folio_core.rules")

__all__ = [
    "SUPPORTED_FIELDS",
    "TEXT_FIELDS",
    "validate_rule",
    "match_count",
    "apply_collection_rules",
]

# The closed set of condition fields. ``account`` uses the ``account_id``
# column; every other field uses the free-text ``value``.
TEXT_FIELDS: frozenset[str] = frozenset({"sender", "domain", "filename", "subject"})
SUPPORTED_FIELDS: frozenset[str] = TEXT_FIELDS | {"account"}

# Backslash is the ILIKE escape character below.
_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    """Escape ILIKE wildcards so the user value is matched literally.

    Escapes the escape char first, then ``%`` and ``_``. The caller wraps the
    result with ``%...%`` and passes ``escape=_LIKE_ESCAPE`` to ``.ilike(...)``.
    """
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _is_int(value: object) -> bool:
    # bool is a subclass of int — reject it for numeric fields.
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_rule(
    field: object,
    value: object,
    account_id: object,
    vendor_id: object,
    folder_id: object,
) -> None:
    """Validate a v3 rule's condition + actions, raising :class:`ValueError`.

    Rules:

      * ``field`` must be one of :data:`SUPPORTED_FIELDS`.
      * For a text field (sender/domain/filename/subject) ``value`` is required
        and non-empty.
      * For ``account``, ``account_id`` is required (an int) and ``value`` must
        be null.
      * At least one action (``vendor_id`` or ``folder_id``) must be set.
    """
    if field not in SUPPORTED_FIELDS:
        raise ValueError(
            f"unknown field {field!r}; supported: {sorted(SUPPORTED_FIELDS)}"
        )

    if field == "account":
        if not _is_int(account_id):
            raise ValueError("field 'account' requires an integer account_id")
        if value is not None and value != "":
            raise ValueError("field 'account' must not carry a value")
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"field {field!r} requires a non-empty string value"
            )

    has_vendor = vendor_id is not None
    has_folder = folder_id is not None
    if not has_vendor and not has_folder:
        raise ValueError("a rule must set at least one action (vendor or folder)")


# --------------------------------------------------------------------------- #
# SQL match clause
# --------------------------------------------------------------------------- #
def _source_exists(*criteria: ColumnElement[bool]) -> ColumnElement[bool]:
    """EXISTS over the image's sources, correlated on ``image_id``."""
    return exists().where(and_(ImageSource.image_id == Image.id, *criteria))


def _ilike_source(column: ColumnElement, raw: str) -> ColumnElement[bool]:
    """EXISTS(source where ``column`` ILIKE %raw%) with escaped, bound value."""
    pattern = f"%{_escape_like(raw)}%"
    return _source_exists(column.ilike(pattern, escape=_LIKE_ESCAPE))


def _match_clause(
    field: str, value: str | None, account_id: int | None
) -> ColumnElement[bool] | None:
    """Build the bound-param boolean match clause for one rule condition.

    Returns ``None`` when the condition is unusable (unknown field, missing
    text value, or missing account id) — the caller treats ``None`` as "matches
    nothing" and skips the rule rather than over-matching.
    """
    if field == "account":
        if account_id is None:
            return None
        return _source_exists(ImageSource.account_id == account_id)

    if field not in TEXT_FIELDS or not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()

    if field == "sender":
        return _ilike_source(ImageSource.email_sender, text)
    if field == "domain":
        domain = text[1:] if text.startswith("@") else text
        return _ilike_source(ImageSource.email_sender, f"@{domain}")
    if field == "subject":
        return _ilike_source(ImageSource.email_subject, text)
    if field == "filename":
        pattern = f"%{_escape_like(text)}%"
        return Image.original_filename.ilike(pattern, escape=_LIKE_ESCAPE)
    return None  # pragma: no cover


# --------------------------------------------------------------------------- #
# Count
# --------------------------------------------------------------------------- #
def match_count(session: Session, rule: CollectionRule) -> int:
    """Count the images matching ``rule``'s condition.

    Returns 0 for a rule whose condition is unusable (it matches nothing).
    """
    clause = _match_clause(rule.field, rule.value, rule.account_id)
    if clause is None:
        return 0
    stmt = select(func.count()).select_from(Image).where(clause)
    return int(session.execute(stmt).scalar_one())


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_collection_rules(
    session: Session, *, rule_id: int | None = None
) -> dict[int, dict]:
    """Apply enabled collection rules, auto-tagging matching images.

    For each enabled rule (or only ``rule_id`` when given) build the match
    whereclause (bound params, ``%``/``_`` escaped) and run its actions:

      * ``vendor_id`` -> ``UPDATE image_sources SET vendor_id=:vid WHERE image_id
        IN (SELECT i.id FROM images i WHERE <match>)`` — overwrites the vendor on
        every source of every matching image. ``'vendored'`` = rows updated.
      * ``folder_id`` -> ``INSERT INTO folder_images SELECT ... ON CONFLICT DO
        NOTHING RETURNING image_id`` — ``'filed'`` = rows actually inserted.

    A rule with no usable condition, or with no action, is skipped. Returns
    ``{rule_id: {'vendored': X, 'filed': Y}}`` for every rule actually run.

    Does not commit — the caller owns the transaction.
    """
    stmt = select(CollectionRule).where(CollectionRule.enabled.is_(True))
    if rule_id is not None:
        stmt = stmt.where(CollectionRule.id == rule_id)

    results: dict[int, dict] = {}
    for rule in session.execute(stmt).scalars().all():
        if rule.vendor_id is None and rule.folder_id is None:
            # No action — nothing to apply.
            continue
        clause = _match_clause(rule.field, rule.value, rule.account_id)
        if clause is None:
            # Unusable condition — never tag the whole library.
            continue

        counts = {"vendored": 0, "filed": 0}

        if rule.vendor_id is not None:
            matching_image_ids = select(Image.id).where(clause)
            upd = (
                update(ImageSource)
                .where(ImageSource.image_id.in_(matching_image_ids))
                .values(vendor_id=rule.vendor_id)
                .returning(ImageSource.image_id)
            )
            # Count DISTINCT images tagged, not source rows: an image with
            # several sources would otherwise inflate the user-facing count.
            rows = session.execute(upd).fetchall()
            counts["vendored"] = len({r[0] for r in rows})

        if rule.folder_id is not None:
            select_stmt = select(
                literal(rule.folder_id),
                Image.id,
                func.now(),
            ).where(clause)
            insert_stmt = (
                pg_insert(FolderImage)
                .from_select(["folder_id", "image_id", "added_at"], select_stmt)
                .on_conflict_do_nothing(index_elements=["folder_id", "image_id"])
                .returning(FolderImage.image_id)
            )
            # RETURNING yields exactly the rows actually inserted (ON CONFLICT
            # skips are not returned), giving a reliable count.
            counts["filed"] = len(session.execute(insert_stmt).fetchall())

        results[rule.id] = counts

    return results
