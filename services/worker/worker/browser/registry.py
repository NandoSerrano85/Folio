"""Adapter registry access + built-in adapter loading.

There is exactly ONE adapter registry in Folio: the ``ADAPTER_REGISTRY`` defined
(and frozen) in :mod:`worker.gmail.sync`, keyed by ``vendors.adapter_key``. This
module re-exports it (and :func:`register_adapter` / :func:`get_adapter`) so the
browser package has a natural import site, and adds :func:`load_builtin_adapters`
which imports the concrete adapter modules so their ``@register_adapter`` side
effects populate the registry.

Keeping a single registry avoids the classic two-registries bug where an adapter
registers in one map while the orchestrator looks it up in another.
"""

from __future__ import annotations

from worker.gmail.sync import ADAPTER_REGISTRY, get_adapter, register_adapter


def load_builtin_adapters() -> dict:
    """Import the built-in adapter modules so they self-register.

    Idempotent: importing an already-imported module is a no-op, and
    ``@register_adapter`` simply re-points the same key at the same class.
    Returns the (now-populated) ``ADAPTER_REGISTRY`` for convenience.

    NOTE: only adapters that should be active by default are imported here. The
    copy-me :mod:`worker.browser.template` adapter is intentionally NOT imported,
    so it never pollutes the registry with a placeholder key.
    """
    # Concrete, always-on adapters. Add new per-vendor modules here once written.
    from worker.browser import generic  # noqa: F401  (import for its side effect)
    from worker.browser import shopify_downloads  # noqa: F401  (side-effect import)

    return ADAPTER_REGISTRY


__all__ = [
    "ADAPTER_REGISTRY",
    "register_adapter",
    "get_adapter",
    "load_builtin_adapters",
]
