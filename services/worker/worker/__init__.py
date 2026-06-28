"""Folio worker service.

Hosts the ingestion CLI and the in-container APScheduler loop. Leaf ingestion
modules (Drive/Gmail) are filled in by Phase-2 agents under
``worker.drive`` / ``worker.gmail`` and ``worker.reconcile``.
"""
