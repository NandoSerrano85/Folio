"""Reconciliation: compare upstream source counts against what was imported.

For each Drive account, count the images Drive currently reports versus the
``image_sources`` rows we hold for that account, and record the result as a
``reconcile`` ingest run. Discrepancies are logged so a follow-up sync (or a
human) can investigate; reconcile itself imports nothing.

Gmail/email reconciliation is intentionally out of scope here (Drive is the only
fully implemented source in this build).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    ImageSource,
    IngestRun,
    IngestStatusEnum,
    ProviderEnum,
    SourceTypeEnum,
)
from worker.drive.auth import build_drive_service

logger = get_logger("worker.reconcile")

_RUN_KIND = "reconcile"
_IMAGE_QUERY = "mimeType contains 'image/' and trashed = false"
_PAGE_SIZE = 1000
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "resp", None), "status", None)
            try:
                return int(status) in _RETRY_STATUSES
            except (TypeError, ValueError):
                return False
    except Exception:  # noqa: BLE001
        pass
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


@retry(
    reraise=True,
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _list_page(service, page_token: str | None) -> dict:
    return (
        service.files()
        .list(
            q=_IMAGE_QUERY,
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=_PAGE_SIZE,
            fields="nextPageToken,files(id)",
            pageToken=page_token,
        )
        .execute()
    )


def _count_upstream(service) -> int:
    total = 0
    page_token: str | None = None
    while True:
        resp = _list_page(service, page_token)
        total += len(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return total


def _resolve_accounts(account_email: str | None) -> list[Account]:
    with session_scope() as session:
        query = select(Account).where(Account.provider == ProviderEnum.drive)
        if account_email:
            query = query.where(Account.email == account_email)
        return list(session.scalars(query.order_by(Account.id)))


def _reconcile_account(account: Account) -> None:
    service = build_drive_service(account.email, account.token_ref)
    upstream = _count_upstream(service)

    with session_scope() as session:
        local = (
            session.scalar(
                select(func.count())
                .select_from(ImageSource)
                .where(
                    ImageSource.account_id == account.id,
                    ImageSource.source_type == SourceTypeEnum.drive,
                )
            )
            or 0
        )
        missing = max(upstream - local, 0)
        run = IngestRun(
            account_id=account.id,
            kind=_RUN_KIND,
            status=IngestStatusEnum.completed,
            source_count=upstream,
            items_seen=upstream,
            items_imported=local,
            items_skipped=missing,
            finished_at=datetime.now(timezone.utc),
        )
        if upstream != local:
            run.errors = [
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "message": (
                        f"count mismatch: upstream={upstream} imported={local} "
                        f"missing={missing}"
                    ),
                }
            ]
        session.add(run)

    if upstream != local:
        logger.warning(
            "reconcile.discrepancy account=%s upstream=%s imported=%s missing=%s",
            account.email,
            upstream,
            local,
            missing,
        )
    else:
        logger.info(
            "reconcile.ok account=%s count=%s", account.email, upstream
        )


def run_reconcile(account_email: str | None = None) -> None:
    """Reconcile imported counts against Drive for one or all Drive accounts."""
    accounts = _resolve_accounts(account_email)
    if not accounts:
        logger.warning("reconcile.no_accounts filter=%s", account_email)
        return

    for account in accounts:
        try:
            _reconcile_account(account)
        except Exception:  # noqa: BLE001 - continue with the next account
            logger.exception("reconcile.account_failed email=%s", account.email)


__all__ = ["run_reconcile"]
