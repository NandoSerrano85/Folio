"""Google Drive ingestion subpackage.

Contains the installed-app OAuth flow (:mod:`worker.drive.auth`) and the
recursive, incremental Drive ingester (:mod:`worker.drive.ingest`). Both build
on the shared per-image acquisition pipeline in :mod:`worker.pipeline` and the
run bookkeeping helpers in :mod:`worker.checkpoint`.
"""
