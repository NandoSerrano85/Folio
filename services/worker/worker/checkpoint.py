"""Ingest-run bookkeeping: start / checkpoint / finish + resume support.

Every ingestion (Drive sync, reconcile, future Gmail sync) records progress in
the ``ingest_runs`` table so that a crashed or restarted worker can:

* report what happened (counts + a capped error log), and
* resume mid-stream from the last persisted page token.

These helpers operate on a caller-supplied :class:`~sqlalchemy.orm.Session`; the
caller owns the transaction boundary (typically ``folio_core.db.session_scope``)
so that counter updates commit atomically with the rows they describe.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from folio_core.logging import get_logger
from folio_core.models import IngestRun, IngestStatusEnum

logger = get_logger("worker.checkpoint")

# Keep the per-run JSON error log bounded so a pathological run cannot bloat the
# row indefinitely.
_ERROR_CAP = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_ingest_run(
    session: Session,
    account_id: int,
    kind: str,
    *,
    source_count: int | None = None,
) -> int:
    """Insert a fresh ``running`` ingest run and return its primary key."""
    run = IngestRun(
        account_id=account_id,
        kind=kind,
        status=IngestStatusEnum.running,
        source_count=source_count,
    )
    session.add(run)
    session.flush()
    logger.info("ingest_run.start id=%s account=%s kind=%s", run.id, account_id, kind)
    return run.id


def latest_unfinished_run(
    session: Session, account_id: int, kind: str
) -> IngestRun | None:
    """Return the most recent still-``running`` run for an account+kind, if any."""
    return session.scalar(
        select(IngestRun)
        .where(
            IngestRun.account_id == account_id,
            IngestRun.kind == kind,
            IngestRun.status == IngestStatusEnum.running,
        )
        .order_by(IngestRun.started_at.desc())
    )


def mark_interrupted(
    session: Session,
    account_id: int,
    kind: str,
    *,
    exclude_id: int | None = None,
) -> None:
    """Flag any lingering ``running`` runs for this account+kind as interrupted.

    Called at the start of a new run so a previously crashed run is not left in
    the ``running`` state forever.
    """
    runs = session.scalars(
        select(IngestRun).where(
            IngestRun.account_id == account_id,
            IngestRun.kind == kind,
            IngestRun.status == IngestStatusEnum.running,
        )
    ).all()
    now = _utcnow()
    for run in runs:
        if exclude_id is not None and run.id == exclude_id:
            continue
        run.status = IngestStatusEnum.interrupted
        if run.finished_at is None:
            run.finished_at = now
        logger.info("ingest_run.interrupted id=%s", run.id)


def record_page_token(session: Session, run_id: int, token: str | None) -> None:
    """Persist the page token a resumed run should continue from."""
    run = session.get(IngestRun, run_id)
    if run is not None:
        run.last_page_token = token


def increment_counts(
    run: IngestRun,
    *,
    seen: int = 0,
    imported: int = 0,
    skipped: int = 0,
    failed: int = 0,
) -> None:
    """Bump the per-run progress counters in place."""
    run.items_seen = (run.items_seen or 0) + seen
    run.items_imported = (run.items_imported or 0) + imported
    run.items_skipped = (run.items_skipped or 0) + skipped
    run.items_failed = (run.items_failed or 0) + failed


def append_error(
    run: IngestRun,
    message: object,
    *,
    context: dict | None = None,
    cap: int = _ERROR_CAP,
) -> None:
    """Append a structured error entry to ``run.errors`` (a JSONB list)."""
    entry: dict = {
        "ts": _utcnow().isoformat(),
        "message": str(message)[:1000],
    }
    if context:
        entry["context"] = context
    # Reassign (rather than mutate) so SQLAlchemy detects the JSONB change.
    existing = list(run.errors or [])
    existing.append(entry)
    run.errors = existing[-cap:]


def finalize_run(
    session: Session,
    run_id: int,
    status: IngestStatusEnum,
    *,
    source_count: int | None = None,
) -> None:
    """Mark a run finished with the given terminal status."""
    run = session.get(IngestRun, run_id)
    if run is None:
        return
    run.status = status
    if source_count is not None:
        run.source_count = source_count
    run.finished_at = _utcnow()
    logger.info(
        "ingest_run.finish id=%s status=%s seen=%s imported=%s skipped=%s failed=%s",
        run.id,
        status.value,
        run.items_seen,
        run.items_imported,
        run.items_skipped,
        run.items_failed,
    )


__all__ = [
    "create_ingest_run",
    "latest_unfinished_run",
    "mark_interrupted",
    "record_page_token",
    "increment_counts",
    "append_error",
    "finalize_run",
]
