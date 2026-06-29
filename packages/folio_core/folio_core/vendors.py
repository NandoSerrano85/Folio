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

__all__ = [
    "slugify_adapter_key",
    "get_or_create_vendor",
    "ensure_adapter_vendor",
]

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


def _unique_adapter_key(
    session: Session, desired: str, *, exclude_id: int | None = None
) -> str:
    """Return ``desired`` (bounded to column width), suffixed ``-2``/``-3``/... if
    another vendor (id != ``exclude_id``) already owns it."""
    base = _bounded_key(desired)
    key = base
    suffix = 2
    while True:
        stmt = select(Vendor.id).where(Vendor.adapter_key == key)
        if exclude_id is not None:
            stmt = stmt.where(Vendor.id != exclude_id)
        if session.execute(stmt).first() is None:
            return key
        key = _bounded_key(base, f"-{suffix}")
        suffix += 1


def ensure_adapter_vendor(
    session: Session,
    *,
    name: str,
    domain: str | None,
    adapter_key: str,
    login_required: bool = False,
) -> Vendor:
    """Get-or-create the vendor named ``name`` and bind it to an adapter.

    The vendor is identified by its (case-insensitive) display ``name`` — the
    stable identity for vendors auto-created from an email From display name
    (the sender being a shared provider domain such as Shopify's). On a hit the
    existing row's ``domain``, ``adapter_key`` and ``login_required`` are
    updated; on a miss a new row is created with those values.

    ``adapter_key`` is the registry key the adapter is registered under (e.g.
    ``"shopify_downloads"``). Because ``vendors.adapter_key`` is UNIQUE, if a
    DIFFERENT vendor already owns the requested key it is suffixed ``-2`` / ``-3``
    / ... to satisfy the constraint; the requested key is kept as-is when it is
    free or already owned by this same vendor.

    The session is flushed so the returned vendor has a populated ``id``. The
    caller owns the transaction (no commit here).
    """
    name = name.strip()

    vendor = session.execute(
        select(Vendor)
        .where(func.lower(Vendor.name) == name.lower())
        .order_by(Vendor.id)
        .limit(1)
    ).scalars().first()

    if vendor is not None:
        vendor.domain = domain
        vendor.login_required = login_required
        if vendor.adapter_key != adapter_key:
            vendor.adapter_key = _unique_adapter_key(
                session, adapter_key, exclude_id=vendor.id
            )
        session.flush()
        return vendor

    unique_key = _unique_adapter_key(session, adapter_key)
    try:
        with session.begin_nested():
            vendor = Vendor(
                name=name,
                domain=domain,
                adapter_key=unique_key,
                login_required=login_required,
            )
            session.add(vendor)
            session.flush()
        return vendor
    except IntegrityError:
        existing = session.execute(
            select(Vendor).where(Vendor.adapter_key == unique_key)
        ).scalars().first()
        if existing is not None:
            existing.domain = domain
            existing.login_required = login_required
            session.flush()
            return existing
        raise
