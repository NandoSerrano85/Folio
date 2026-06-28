"""Human-in-the-loop resolution of un-automatable vendor emails.

When :func:`worker.gmail.sync.run_gmail_sync` meets a vendor email it cannot
finish unattended -- no adapter, a CAPTCHA / anti-bot wall, a login failure, or
simply no resolvable download -- it records a pending ``assist_tasks`` row
instead of failing the run. This module is the operator's side of that handshake:

* :func:`run_assist_list` -- print the pending (and in-progress) tasks with the
  context a human needs (account, vendor, subject, sender, the link, and why it
  could not be automated).
* :func:`run_assist_resolve` -- given a task id and a path to the original image
  the operator downloaded by hand, ingest it through the SAME common pipeline the
  automated paths use (preserving the original email's acquisition date, vendor,
  and message context), then mark the task ``resolved`` and link
  ``resolved_image_id``.

These are the two callables ``worker assist-list`` / ``worker assist-resolve``
import. Google-client imports are lazy (only :func:`run_assist_resolve` may need
to re-read the email Date header).
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from folio_core.db import session_scope
from folio_core.logging import get_logger
from folio_core.models import (
    Account,
    AssistStatusEnum,
    AssistTask,
    ProviderEnum,
    SourceDateOriginEnum,
    SourceTypeEnum,
    Vendor,
)
from worker.pipeline import run_pipeline

logger = get_logger("worker.assist")


# --------------------------------------------------------------------------- #
# assist-list
# --------------------------------------------------------------------------- #
def run_assist_list() -> None:
    """Print the pending / in-progress human-assist tasks."""
    pending_states = (AssistStatusEnum.pending, AssistStatusEnum.in_progress)
    rows_out: list[dict] = []
    with session_scope() as session:
        tasks = session.scalars(
            select(AssistTask)
            .where(AssistTask.status.in_(pending_states))
            .order_by(AssistTask.created_at)
        ).all()
        # Resolve display context inside the session (account email, vendor name).
        for task in tasks:
            account = session.get(Account, task.account_id)
            vendor = session.get(Vendor, task.vendor_id) if task.vendor_id else None
            rows_out.append(
                {
                    "id": task.id,
                    "status": task.status.value,
                    "reason": task.reason or "-",
                    "account": account.email if account else f"account#{task.account_id}",
                    "vendor": vendor.name if vendor else "-",
                    "subject": task.email_subject or "-",
                    "sender": task.email_sender or "-",
                    "url": task.vendor_url or "-",
                    "created_at": (
                        task.created_at.isoformat() if task.created_at else "-"
                    ),
                }
            )

    if not rows_out:
        print("No pending assist tasks.")
        logger.info("assist.list count=0")
        return

    print(f"{len(rows_out)} pending assist task(s):\n")
    for row in rows_out:
        print(f"  [#{row['id']}] {row['status']}  reason={row['reason']}")
        print(f"      account : {row['account']}")
        print(f"      vendor  : {row['vendor']}")
        print(f"      sender  : {row['sender']}")
        print(f"      subject : {row['subject']}")
        print(f"      link    : {row['url']}")
        print(f"      created : {row['created_at']}")
        print(
            f"      resolve : worker assist-resolve --id {row['id']} "
            f"--file /path/to/original.jpg"
        )
        print()
    logger.info("assist.list count=%d", len(rows_out))


# --------------------------------------------------------------------------- #
# assist-resolve
# --------------------------------------------------------------------------- #
def run_assist_resolve(task_id: int, file_path: str) -> None:
    """Ingest a manually-supplied original for one assist task and resolve it.

    The file is run through :func:`worker.pipeline.run_pipeline` exactly as an
    automated email acquisition would be: ``source_type='email'``,
    ``source_id`` = the email message id, ``source_date`` = the email ``Date``
    header (re-fetched from Gmail for fidelity; falls back to the task's
    ``created_at``), ``source_date_origin='email_date'``, carrying the original
    vendor / subject / sender / message-id provenance. On success the task moves
    to ``resolved`` with ``resolved_image_id`` set.
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        logger.error("assist.resolve.file_missing id=%s path=%s", task_id, file_path)
        raise FileNotFoundError(f"No such file: {file_path}")

    data = path.read_bytes()
    if not data:
        logger.error("assist.resolve.file_empty id=%s path=%s", task_id, file_path)
        raise ValueError(f"File is empty: {file_path}")

    with session_scope() as session:
        task = session.get(AssistTask, task_id)
        if task is None:
            logger.error("assist.resolve.not_found id=%s", task_id)
            raise ValueError(f"No assist task with id {task_id}")
        if task.status == AssistStatusEnum.resolved:
            logger.info("assist.resolve.already_resolved id=%s", task_id)
            print(f"Assist task {task_id} is already resolved.")
            return

        account = session.get(Account, task.account_id)
        if account is None:
            logger.error("assist.resolve.account_missing id=%s", task_id)
            raise ValueError(f"Assist task {task_id} references a missing account.")
        vendor = session.get(Vendor, task.vendor_id) if task.vendor_id else None

        source_date = _email_date_for_task(account, task)
        mime = mimetypes.guess_type(path.name)[0]

        result = run_pipeline(
            session,
            account=account,
            source_type=SourceTypeEnum.email,
            source_id=task.email_message_id,
            data=data,
            original_filename=path.name,
            mime=mime,
            source_date=source_date,
            source_date_origin=SourceDateOriginEnum.email_date,
            vendor=vendor,
            image_source_fields={
                "vendor_url": task.vendor_url,
                "email_subject": task.email_subject,
                "email_sender": task.email_sender,
                "email_message_id": task.email_message_id,
            },
        )

        task.status = AssistStatusEnum.resolved
        task.resolved_image_id = result.image_id
        task.resolved_at = datetime.now(timezone.utc)
        if result.image_id is None:
            # Race-skip inside the pipeline: rare, but make it visible.
            note = "resolved but image_id unresolved (pipeline race-skip)"
            task.notes = f"{task.notes} | {note}" if task.notes else note

        logger.info(
            "assist.resolve.ok id=%s image_id=%s created_image=%s created_source=%s",
            task_id,
            result.image_id,
            result.created_image,
            result.created_source,
        )
        print(
            f"Resolved assist task {task_id} -> image_id={result.image_id} "
            f"(new_image={result.created_image})"
        )


def _email_date_for_task(account: Account, task: AssistTask) -> datetime:
    """Best authoritative acquisition date for the task's email.

    Re-fetches the ``Date`` header from Gmail (most faithful); falls back to
    Gmail's ``internalDate`` and finally the task's ``created_at``. The source
    date is the library's default sort key, so getting it right matters.
    """
    if account.provider == ProviderEnum.gmail and task.email_message_id:
        try:
            from worker.gmail.auth import build_gmail_service
            from worker.gmail.discover import _execute, _parse_date

            service = build_gmail_service(account.email)
            if service is not None:
                msg = _execute(
                    service.users().messages().get(
                        userId="me",
                        id=task.email_message_id,
                        format="metadata",
                        metadataHeaders=["Date"],
                    )
                )
                headers = {
                    h.get("name", "").lower(): h.get("value", "")
                    for h in (msg.get("payload", {}) or {}).get("headers", [])
                }
                dt = _parse_date(headers.get("date", ""))
                if dt is None:
                    internal = msg.get("internalDate")
                    if internal:
                        try:
                            dt = datetime.fromtimestamp(
                                int(internal) / 1000, tz=timezone.utc
                            )
                        except (TypeError, ValueError, OSError):
                            dt = None
                if dt is not None:
                    return dt
        except Exception:  # noqa: BLE001 - fall back to created_at; never fatal
            logger.warning(
                "assist.resolve.email_date_fetch_failed id=%s -- using created_at",
                task.id,
            )

    if task.created_at is not None:
        created = task.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created
    return datetime.now(timezone.utc)


__all__ = ["run_assist_list", "run_assist_resolve"]
