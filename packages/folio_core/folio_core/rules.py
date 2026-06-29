"""Collection rules — auto-filing images into folders by ANDed conditions.

A *rule* targets one folder (``folder_id``) and carries a list of
``{field, op, value}`` *conditions* that are ANDed together: an image is
auto-added to the folder when it matches ALL conditions. An image may match
many rules and so be filed into many folders; membership is idempotent via the
``folder_images`` primary key.

Supported conditions (field, op, value):

  * ``vendor`` / ``is`` / ``int``        -> some source has ``vendor_id == value``
  * ``account`` / ``is`` / ``int``       -> some source has ``account_id == value``
  * ``source_type`` / ``is`` / ``str``   -> some source has ``source_type == value``
                                            (``'drive'`` | ``'email'``)
  * ``folder_path`` / ``contains`` / ``str`` -> some source's
                                            ``drive_folder_path`` ILIKE %value%
  * ``filename`` / ``contains`` / ``str``    -> ``images.original_filename``
                                            ILIKE %value%
  * ``date`` / ``within_days`` / ``int`` -> ``images.source_date`` within the
                                            last N days

Every value is bound as a parameter — NEVER string-interpolated into SQL. ILIKE
patterns escape ``%`` and ``_`` (and the escape char) in the user value and then
wrap it with ``%...%``. An EMPTY conditions list matches NOTHING: such a rule is
skipped, never applied (it must not file the entire library).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from folio_core.logging import get_logger
from folio_core.models import (
    CollectionRule,
    FolderImage,
    Image,
    ImageSource,
    SourceTypeEnum,
)

logger = get_logger("folio_core.rules")

__all__ = [
    "SUPPORTED_FIELDS",
    "SUPPORTED_OPS",
    "FIELD_SPECS",
    "validate_conditions",
    "summarize_conditions",
    "apply_collection_rules",
]


# --------------------------------------------------------------------------- #
# Supported field/op contract
# --------------------------------------------------------------------------- #
# Per field: the single allowed op, the expected python value type, and (for
# source_type) the closed set of allowed string values.
FIELD_SPECS: dict[str, dict[str, Any]] = {
    "vendor": {"op": "is", "type": int},
    "account": {"op": "is", "type": int},
    "source_type": {
        "op": "is",
        "type": str,
        "choices": {e.value for e in SourceTypeEnum},
    },
    "folder_path": {"op": "contains", "type": str},
    "filename": {"op": "contains", "type": str},
    "date": {"op": "within_days", "type": int},
}

SUPPORTED_FIELDS: frozenset[str] = frozenset(FIELD_SPECS)
SUPPORTED_OPS: frozenset[str] = frozenset(
    spec["op"] for spec in FIELD_SPECS.values()
)

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


def _is_int(value: Any) -> bool:
    # bool is a subclass of int — reject it for numeric fields.
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_conditions(conditions: Any) -> list[dict[str, Any]]:
    """Validate a list of ``{field, op, value}`` condition dicts.

    Raises :class:`ValueError` on a non-list, a malformed entry, an unknown
    field or op, or a value of the wrong type / outside the allowed set. Returns
    the conditions list unchanged on success. An empty list is valid (it simply
    matches nothing when applied).
    """
    if not isinstance(conditions, list):
        raise ValueError("conditions must be a list")

    for idx, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            raise ValueError(f"condition[{idx}] must be an object")
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")

        if field not in FIELD_SPECS:
            raise ValueError(
                f"condition[{idx}] unknown field {field!r}; "
                f"supported: {sorted(SUPPORTED_FIELDS)}"
            )
        spec = FIELD_SPECS[field]
        if op != spec["op"]:
            raise ValueError(
                f"condition[{idx}] field {field!r} only supports op "
                f"{spec['op']!r}, got {op!r}"
            )

        expected = spec["type"]
        if expected is int:
            if not _is_int(value):
                raise ValueError(
                    f"condition[{idx}] field {field!r} requires an integer value"
                )
            if field == "date" and value < 1:
                raise ValueError(
                    f"condition[{idx}] field 'date' requires a positive "
                    f"number of days"
                )
        elif expected is str:
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"condition[{idx}] field {field!r} requires a non-empty "
                    f"string value"
                )
            choices = spec.get("choices")
            if choices is not None and value not in choices:
                raise ValueError(
                    f"condition[{idx}] field {field!r} value must be one of "
                    f"{sorted(choices)}, got {value!r}"
                )

    return conditions


# --------------------------------------------------------------------------- #
# Human-readable summary (structural — can't resolve vendor/account names)
# --------------------------------------------------------------------------- #
def _summarize_one(cond: dict[str, Any]) -> str:
    field = cond.get("field")
    value = cond.get("value")
    if field in ("vendor", "account"):
        return f"{field} is #{value}"
    if field == "source_type":
        return f"source_type is {value}"
    if field in ("folder_path", "filename"):
        return f"{field} contains '{value}'"
    if field == "date":
        return f"date within last {value} days"
    return f"{field} {cond.get('op')} {value!r}"


def summarize_conditions(conditions: list[dict[str, Any]]) -> str:
    """Return a structural human label for a conditions list.

    Names cannot be resolved here (no session), so vendor/account render by id,
    e.g. ``"vendor is #3 AND folder_path contains 'Lookbook'"``. An empty list
    renders as ``"(no conditions)"``.
    """
    if not conditions:
        return "(no conditions)"
    return " AND ".join(_summarize_one(c) for c in conditions)


# --------------------------------------------------------------------------- #
# SQL match clause
# --------------------------------------------------------------------------- #
def _source_exists(*criteria: ColumnElement[bool]) -> ColumnElement[bool]:
    """EXISTS over the image's sources, correlated on ``image_id``."""
    return exists().where(
        and_(ImageSource.image_id == Image.id, *criteria)
    )


def _condition_clause(cond: dict[str, Any]) -> ColumnElement[bool]:
    """Build a single bound-param SQLAlchemy boolean clause against ``Image``."""
    field = cond["field"]
    value = cond["value"]

    if field == "vendor":
        return _source_exists(ImageSource.vendor_id == value)
    if field == "account":
        return _source_exists(ImageSource.account_id == value)
    if field == "source_type":
        return _source_exists(ImageSource.source_type == value)
    if field == "folder_path":
        pattern = f"%{_escape_like(value)}%"
        return _source_exists(
            ImageSource.drive_folder_path.ilike(pattern, escape=_LIKE_ESCAPE)
        )
    if field == "filename":
        pattern = f"%{_escape_like(value)}%"
        return Image.original_filename.ilike(pattern, escape=_LIKE_ESCAPE)
    if field == "date":
        # timedelta is bound as a parameter (rendered as an interval) — the
        # day count is never string-formatted into the SQL text.
        return Image.source_date >= func.now() - timedelta(days=value)
    raise ValueError(f"unsupported field {field!r}")  # pragma: no cover


def _match_clause(conditions: list[dict[str, Any]]) -> ColumnElement[bool] | None:
    """AND every condition into one whereclause, or None if empty.

    A None return signals "matches nothing" to the caller (an empty rule must
    not file the whole library).
    """
    if not conditions:
        return None
    try:
        clauses = [_condition_clause(c) for c in conditions]
    except (KeyError, ValueError, TypeError):
        # Conditions are validated on every write, so a malformed entry here can
        # only come from a hand-edited/legacy DB row. Make the WHOLE rule match
        # nothing rather than 500-ing apply/list or silently over-matching.
        logger.warning("rules.invalid_conditions skipped conditions=%s", conditions)
        return None
    return and_(*clauses)


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_collection_rules(
    session: Session, *, rule_id: int | None = None
) -> dict[int, int]:
    """Apply enabled collection rules, auto-filing matching images.

    For each enabled rule (or only ``rule_id`` when given) run ONE
    ``INSERT INTO folder_images(folder_id, image_id, added_at)
    SELECT :folder_id, i.id, now() FROM images i WHERE <conds>
    ON CONFLICT (folder_id, image_id) DO NOTHING``. Rules with an empty
    conditions list are skipped (they match nothing). Returns a mapping of
    ``{rule_id: rows_added}`` for every rule that was actually run (skipped /
    empty-condition rules are omitted).

    Does not commit — the caller owns the transaction.
    """
    stmt = select(CollectionRule).where(CollectionRule.enabled.is_(True))
    if rule_id is not None:
        stmt = stmt.where(CollectionRule.id == rule_id)

    added: dict[int, int] = {}
    for rule in session.execute(stmt).scalars().all():
        conditions = rule.conditions or []
        clause = _match_clause(conditions)
        if clause is None:
            # Empty conditions match nothing — never file the whole library.
            continue

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
        # RETURNING yields exactly the rows actually inserted (ON CONFLICT skips
        # are not returned), giving a reliable count — rowcount is -1/unreliable
        # for INSERT...SELECT...ON CONFLICT under psycopg3.
        added[rule.id] = len(session.execute(insert_stmt).fetchall())

    return added
