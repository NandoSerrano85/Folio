"""Vendor helpers shared by the worker and the portal.

The single source of truth for turning a free-form vendor *name* into a stable,
unique ``adapter_key`` slug and for the get-or-create upsert that both the
bulk set-vendor portal endpoint and the Drive-path ``derive-vendors`` worker
command rely on.

Neither helper commits — the caller owns the transaction (the worker via
``session_scope`` and the portal via its request-scoped session). The
``get_or_create_vendor`` upsert flushes so the returned :class:`Vendor` has its
primary key populated for immediate use (FK assignment, response building).
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from folio_core.models import Vendor

__all__ = ["slugify_adapter_key", "get_or_create_vendor"]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MAX_KEY_LEN = 128  # vendors.adapter_key is String(128)


def slugify_adapter_key(name: str) -> str:
    """Return a URL-safe ``adapter_key`` slug derived from ``name``.

    Lowercases, replaces every run of non-alphanumeric characters with a single
    ``-``, and strips leading/trailing dashes. Returns ``"vendor"`` when the
    result would otherwise be empty (e.g. name was blank or all punctuation).
    """
    slug = _NON_ALNUM.sub("-", name.lower()).strip("-")
    return slug or "vendor"


def _bounded_key(slug: str, suffix: str = "") -> str:
    """Fit ``slug`` (+ optional ``suffix``) within adapter_key's column width."""
    base = slug[: _MAX_KEY_LEN - len(suffix)].rstrip("-") or "vendor"
    return f"{base}{suffix}"


def get_or_create_vendor(session: Session, name: str) -> Vendor:
    """Return the vendor named ``name``, creating it if it does not exist.

    Matching is, in order: an existing vendor whose ``adapter_key`` equals the
    slug of ``name``, then any vendor whose ``name`` matches case-insensitively.
    When neither is found a new vendor is created with a *unique* ``adapter_key``
    (the base slug, or ``slug-2``, ``slug-3``, ...), bounded to the column width,
    and ``login_required=False``.

    The session is flushed so the returned vendor has a populated ``id``. The
    caller is responsible for committing.
    """
    name = name.strip()
    slug = _bounded_key(slugify_adapter_key(name))

    # Exact adapter_key match wins (the canonical identity).
    existing = session.execute(
        select(Vendor).where(Vendor.adapter_key == slug)
    ).scalars().first()
    if existing is not None:
        return existing

    # Fall back to a case-insensitive name match so we don't create a duplicate
    # for the same human-facing vendor that slugged differently. Vendor.name is
    # NOT unique, so take the lowest id rather than assuming exactly one row.
    existing = session.execute(
        select(Vendor)
        .where(func.lower(Vendor.name) == name.lower())
        .order_by(Vendor.id)
        .limit(1)
    ).scalars().first()
    if existing is not None:
        return existing

    # Ensure adapter_key uniqueness: suffix -2, -3, ... if the slug is taken.
    unique_key = slug
    suffix = 2
    while session.execute(
        select(Vendor.id).where(Vendor.adapter_key == unique_key)
    ).first() is not None:
        unique_key = _bounded_key(slug, f"-{suffix}")
        suffix += 1

    # Create inside a SAVEPOINT so a concurrent insert of the same key can't
    # poison the caller's transaction — on conflict, return the row that won.
    try:
        with session.begin_nested():
            vendor = Vendor(name=name, adapter_key=unique_key, login_required=False)
            session.add(vendor)
            session.flush()
        return vendor
    except IntegrityError:
        existing = session.execute(
            select(Vendor).where(Vendor.adapter_key == unique_key)
        ).scalars().first()
        if existing is not None:
            return existing
        raise
