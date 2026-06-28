"""Recursive, incremental Google Drive ingestion.

``run_drive_sync`` walks every image the account can see -- My Drive, shared
drives, and shared-with-me files (``corpora='allDrives'``) -- and feeds each new
one through the common acquisition pipeline.

Modes
-----
* **Incremental (default):** uses the Drive *changes* feed, resuming from the
  ``drive_change_token`` cursor stored in ``sync_state`` (or the last page token
  of an interrupted run). After completion the cursor advances to the API's
  ``newStartPageToken``.
* **Full (``--full``):** captures a fresh start-page-token first, then lists all
  images via ``files.list``; on success the captured token becomes the new
  incremental cursor so changes during the scan are picked up next time.

Robustness
----------
* Idempotent on ``image_sources`` UNIQUE(account_id,'drive',fileId) -- an
  already-imported file is skipped WITHOUT being re-downloaded.
* ``tenacity`` exponential backoff on Drive 429/5xx and transient network errors.
* Page tokens are checkpointed into ``ingest_runs`` after each page for resume.
* Each file is its own transaction; one failure never aborts the run.
* Chunked download via ``MediaIoBaseDownload`` (bounded memory per file).

Date semantics: ``source_date = createdTime`` with origin ``drive_created``.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from folio_core.config import get_settings
from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    CursorTypeEnum,
    ImageSource,
    IngestStatusEnum,
    ProviderEnum,
    SourceDateOriginEnum,
    SourceTypeEnum,
    SyncState,
)
from worker.checkpoint import (
    append_error,
    create_ingest_run,
    finalize_run,
    increment_counts,
    latest_unfinished_run,
    mark_interrupted,
    record_page_token,
)
from worker.drive.auth import build_drive_service
from worker.pipeline import parse_rfc3339, run_pipeline

logger = get_logger("worker.drive.ingest")

_RUN_KIND = "drive_sync"
_DOWNLOAD_CHUNK = 8 * 1024 * 1024  # 8 MiB resumable download chunks
_PAGE_SIZE = 100
_IMAGE_QUERY = "mimeType contains 'image/' and trashed = false"

# Fields requested for each Drive file.
_FILE_FIELDS = (
    "id,name,mimeType,createdTime,modifiedTime,size,parents,webViewLink,"
    "owners(displayName,emailAddress),imageMediaMetadata(width,height),trashed"
)
_LIST_FIELDS = f"nextPageToken,files({_FILE_FIELDS})"
_CHANGES_FIELDS = (
    f"nextPageToken,newStartPageToken,changes(removed,fileId,file({_FILE_FIELDS}))"
)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


# --------------------------------------------------------------------------- #
# Retry plumbing
# --------------------------------------------------------------------------- #
def _is_retryable(exc: BaseException) -> bool:
    """True for Drive rate-limit/5xx HttpErrors and transient transport errors."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc, "status_code", None)
            if status is None:
                resp = getattr(exc, "resp", None)
                status = getattr(resp, "status", None)
            try:
                return int(status) in _RETRY_STATUSES
            except (TypeError, ValueError):
                return False
    except Exception:  # noqa: BLE001 - HttpError import only matters at runtime
        pass
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _retry():
    """Standard backoff decorator for Drive API calls."""
    return retry(
        reraise=True,
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


# --------------------------------------------------------------------------- #
# Drive API wrappers (all retried)
# --------------------------------------------------------------------------- #
@_retry()
def _files_list(service, *, query: str, page_token: str | None) -> dict:
    return (
        service.files()
        .list(
            q=query,
            spaces="drive",
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=_PAGE_SIZE,
            fields=_LIST_FIELDS,
            pageToken=page_token,
        )
        .execute()
    )


@_retry()
def _changes_list(service, *, page_token: str) -> dict:
    return (
        service.changes()
        .list(
            pageToken=page_token,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            pageSize=_PAGE_SIZE,
            fields=_CHANGES_FIELDS,
        )
        .execute()
    )


@_retry()
def _get_start_page_token(service) -> str | None:
    resp = service.changes().getStartPageToken(supportsAllDrives=True).execute()
    return resp.get("startPageToken")


@_retry()
def _get_file_meta(service, file_id: str, fields: str) -> dict | None:
    try:
        return (
            service.files()
            .get(fileId=file_id, fields=fields, supportsAllDrives=True)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        # 404 / permission gaps when walking parents are expected; treat as gap.
        try:
            from googleapiclient.errors import HttpError

            if isinstance(exc, HttpError):
                status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "resp", None), "status", None
                )
                if status and int(status) in (403, 404):
                    return None
        except Exception:  # noqa: BLE001
            pass
        raise


@_retry()
def _download_file(service, file_id: str) -> bytes:
    """Chunked download of a file's bytes into memory."""
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Folder-path resolution (memoized per process run)
# --------------------------------------------------------------------------- #
def _resolve_folder_path(service, file_meta: dict, cache: dict[str, dict]) -> str | None:
    """Walk ``parents`` up to the root, building a ``A/B/C`` folder path."""
    parents = file_meta.get("parents")
    if not parents:
        return None
    parts: list[str] = []
    current = parents[0]
    seen: set[str] = set()
    depth = 0
    while current and current not in seen and depth < 25:
        seen.add(current)
        depth += 1
        meta = cache.get(current)
        if meta is None:
            meta = _get_file_meta(service, current, "id,name,parents")
            if meta is None:
                break
            cache[current] = meta
        name = meta.get("name")
        if name:
            parts.append(name)
        next_parents = meta.get("parents")
        current = next_parents[0] if next_parents else None
    if not parts:
        return None
    parts.reverse()
    return "/".join(parts)


def _owner_str(file_meta: dict) -> str | None:
    owners = file_meta.get("owners") or []
    if owners:
        owner = owners[0]
        return owner.get("emailAddress") or owner.get("displayName")
    return None


def _raw_meta(file_meta: dict) -> dict:
    keys = (
        "id",
        "name",
        "mimeType",
        "size",
        "parents",
        "webViewLink",
        "createdTime",
        "modifiedTime",
        "owners",
        "imageMediaMetadata",
    )
    return {k: file_meta[k] for k in keys if file_meta.get(k) is not None}


# --------------------------------------------------------------------------- #
# Account + cursor helpers
# --------------------------------------------------------------------------- #
def _resolve_accounts(account_email: str | None) -> list[Account]:
    """Drive accounts to process (detached ORM rows; scalar attrs are loaded)."""
    with session_scope() as session:
        query = select(Account).where(Account.provider == ProviderEnum.drive)
        if account_email:
            query = query.where(Account.email == account_email)
        return list(session.scalars(query.order_by(Account.id)))


def _get_cursor(account_id: int) -> str | None:
    with session_scope() as session:
        state = session.scalar(
            select(SyncState).where(
                SyncState.account_id == account_id,
                SyncState.cursor_type == CursorTypeEnum.drive_change_token,
            )
        )
        return state.cursor_value if state else None


def _set_cursor(account_id: int, value: str | None, *, full: bool) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        state = session.scalar(
            select(SyncState).where(
                SyncState.account_id == account_id,
                SyncState.cursor_type == CursorTypeEnum.drive_change_token,
            )
        )
        if state is None:
            state = SyncState(
                account_id=account_id,
                cursor_type=CursorTypeEnum.drive_change_token,
            )
            session.add(state)
        state.cursor_value = value
        if full:
            state.last_full_sync_at = now
        else:
            state.last_incremental_at = now


def _already_imported(session, account_id: int, file_id: str) -> bool:
    return (
        session.scalar(
            select(ImageSource.id).where(
                ImageSource.account_id == account_id,
                ImageSource.source_type == SourceTypeEnum.drive,
                ImageSource.source_id == file_id,
            )
        )
        is not None
    )


# --------------------------------------------------------------------------- #
# Per-file handling
# --------------------------------------------------------------------------- #
def _handle_file(
    service,
    account: Account,
    run_id: int,
    file_meta: dict,
    folder_cache: dict[str, dict],
) -> None:
    """Process a single Drive image file: skip-if-known, else download + ingest."""
    file_id = file_meta.get("id")
    name = file_meta.get("name")
    mime = file_meta.get("mimeType")
    if not file_id:
        return

    try:
        # Cheap idempotency pre-check: avoid downloading already-imported files.
        with session_scope() as session:
            already = _already_imported(session, account.id, file_id)
            if already:
                increment_counts(_run(session, run_id), seen=1, skipped=1)
        if already:
            return

        created_time = parse_rfc3339(file_meta.get("createdTime"))
        modified_time = parse_rfc3339(file_meta.get("modifiedTime"))
        source_date = created_time or modified_time or datetime.now(timezone.utc)
        source_fields = {
            "drive_folder_path": _resolve_folder_path(service, file_meta, folder_cache),
            "drive_created_time": created_time,
            "drive_modified_time": modified_time,
            "drive_owner": _owner_str(file_meta),
            "vendor_url": file_meta.get("webViewLink"),
            "raw_meta": _raw_meta(file_meta),
        }

        data = _download_file(service, file_id)

        with session_scope() as session:
            result = run_pipeline(
                session,
                account=account,
                source_type=SourceTypeEnum.drive,
                source_id=file_id,
                data=data,
                original_filename=name,
                mime=mime,
                source_date=source_date,
                source_date_origin=SourceDateOriginEnum.drive_created,
                vendor=None,
                image_source_fields=source_fields,
            )
            run = _run(session, run_id)
            increment_counts(
                run,
                seen=1,
                imported=1 if result.created_image else 0,
                skipped=0 if result.created_image else 1,
            )
        logger.info(
            "drive.file id=%s name=%s sha=%s imported=%s",
            file_id,
            name,
            result.sha256[:12],
            result.created_image,
        )
    except Exception as exc:  # noqa: BLE001 - one bad file must not abort the run
        logger.exception("drive.file_failed id=%s name=%s", file_id, name)
        with session_scope() as session:
            run = _run(session, run_id)
            if run is not None:
                increment_counts(run, seen=1, failed=1)
                append_error(run, exc, context={"file_id": file_id, "name": name})


def _run(session, run_id: int):
    from folio_core.models import IngestRun

    return session.get(IngestRun, run_id)


# --------------------------------------------------------------------------- #
# Scan drivers
# --------------------------------------------------------------------------- #
def _full_sync(service, account: Account, run_id: int) -> None:
    folder_cache: dict[str, dict] = {}
    page_token: str | None = None
    while True:
        resp = _files_list(service, query=_IMAGE_QUERY, page_token=page_token)
        for file_meta in resp.get("files", []):
            _handle_file(service, account, run_id, file_meta, folder_cache)
        page_token = resp.get("nextPageToken")
        with session_scope() as session:
            record_page_token(session, run_id, page_token)
        if not page_token:
            break


def _incremental_sync(
    service, account: Account, run_id: int, start_token: str
) -> str | None:
    """Process the Drive changes feed from ``start_token``; return new cursor."""
    folder_cache: dict[str, dict] = {}
    page_token: str | None = start_token
    new_cursor: str | None = None
    while page_token:
        resp = _changes_list(service, page_token=page_token)
        for change in resp.get("changes", []):
            if change.get("removed"):
                continue
            file_meta = change.get("file")
            if not file_meta or file_meta.get("trashed"):
                continue
            mime = file_meta.get("mimeType") or ""
            if not mime.startswith("image/"):
                continue
            _handle_file(service, account, run_id, file_meta, folder_cache)
        if "newStartPageToken" in resp:
            new_cursor = resp["newStartPageToken"]
        next_token = resp.get("nextPageToken")
        # Checkpoint the token a resume should continue from.
        with session_scope() as session:
            record_page_token(session, run_id, next_token or new_cursor)
        page_token = next_token
    return new_cursor


# --------------------------------------------------------------------------- #
# Per-account orchestration
# --------------------------------------------------------------------------- #
def _sync_account(account: Account, *, full: bool) -> None:
    service = build_drive_service(account.email, account.token_ref)

    stored_cursor = _get_cursor(account.id)
    use_incremental = (not full) and bool(stored_cursor)

    # Capture a fresh start token BEFORE a full scan so changes during the scan
    # are not missed on the next incremental run.
    full_scan_cursor: str | None = None
    if not use_incremental:
        full_scan_cursor = _get_start_page_token(service)

    # Open a run, inheriting a resume token from any interrupted run first.
    with session_scope() as session:
        previous = latest_unfinished_run(session, account.id, _RUN_KIND)
        resume_token = previous.last_page_token if previous else None
        mark_interrupted(session, account.id, _RUN_KIND)
        run_id = create_ingest_run(session, account.id, _RUN_KIND)

    logger.info(
        "drive.sync.start account=%s mode=%s resume=%s",
        account.email,
        "incremental" if use_incremental else "full",
        bool(resume_token and use_incremental),
    )

    try:
        if use_incremental:
            start_token = (resume_token or stored_cursor) or full_scan_cursor
            if not start_token:
                # No cursor at all -> behave like a full scan.
                start_token = _get_start_page_token(service)
                _full_sync(service, account, run_id)
                _set_cursor(account.id, start_token, full=True)
            else:
                new_cursor = _incremental_sync(service, account, run_id, start_token)
                if new_cursor:
                    _set_cursor(account.id, new_cursor, full=False)
        else:
            _full_sync(service, account, run_id)
            if full_scan_cursor:
                _set_cursor(account.id, full_scan_cursor, full=True)
    except Exception:
        logger.exception("drive.sync.account_error account=%s", account.email)
        with session_scope() as session:
            finalize_run(session, run_id, IngestStatusEnum.failed)
        raise

    with session_scope() as session:
        finalize_run(session, run_id, IngestStatusEnum.completed)
    logger.info("drive.sync.done account=%s", account.email)


def run_drive_sync(account_email: str | None = None, full: bool = False) -> None:
    """Ingest Google Drive images for one account (or all Drive accounts)."""
    settings = get_settings()
    _ = settings  # ensure config (and thus MEDIA_ROOT) is loaded/validated early

    accounts = _resolve_accounts(account_email)
    if not accounts:
        logger.warning(
            "drive.sync.no_accounts filter=%s; run `worker auth-drive` first.",
            account_email,
        )
        return

    for account in accounts:
        try:
            _sync_account(account, full=full)
        except Exception:  # noqa: BLE001 - continue with the next account
            logger.exception("drive.sync.account_failed email=%s", account.email)


__all__ = ["run_drive_sync"]
