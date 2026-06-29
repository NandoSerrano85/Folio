"""Folio worker CLI (typer).

This module is the stable dispatch spine. The actual ingestion logic lives in
Phase-2 leaf modules and is imported LAZILY inside each command so that:

  * ``worker init-db`` works in this foundation build (no leaf modules yet), and
  * a broken/half-written leaf module can never stop the whole CLI from loading.

Phase-2 modules MUST expose these exact callables (see the manifest):

    worker.drive.auth        -> run_drive_auth(account: str) -> None
    worker.gmail.auth        -> run_gmail_auth(account: str) -> None
    worker.drive.ingest      -> run_drive_sync(account: str | None, full: bool) -> None
    worker.gmail.discover    -> run_discover_senders(account: str | None) -> None
    worker.gmail.sync        -> run_gmail_sync(account: str | None) -> None
    worker.reconcile         -> run_reconcile(account: str | None) -> None
    worker.assist            -> run_assist_list() -> None
    worker.assist            -> run_assist_resolve(task_id: int, file_path: str) -> None
    worker.backup            -> run_backup() -> None
"""

from __future__ import annotations

import typer

from folio_core.config import get_settings
from folio_core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger("worker")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Folio worker: ingestion CLI + scheduler.",
)


# --------------------------------------------------------------------------- #
# init-db
# --------------------------------------------------------------------------- #
@app.command("init-db")
def init_db() -> None:
    """Apply migrations (alembic upgrade head) and seed the admin user.

    Migrations are also applied by the container entrypoint; running them here
    keeps the command correct when invoked directly.
    """
    _run_migrations()
    _seed_admin_user()
    logger.info("init-db complete.")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
@app.command("auth-drive")
def auth_drive(
    account: str = typer.Option(..., "--account", help="Account email."),
) -> None:
    """Run the Drive OAuth consent flow and store the encrypted refresh token."""
    from worker.drive.auth import run_drive_auth

    run_drive_auth(account)


@app.command("auth-gmail")
def auth_gmail(
    account: str = typer.Option(..., "--account", help="Account email."),
) -> None:
    """Run the Gmail OAuth consent flow and store the encrypted refresh token."""
    from worker.gmail.auth import run_gmail_auth

    run_gmail_auth(account)


# --------------------------------------------------------------------------- #
# sync / discover / reconcile
# --------------------------------------------------------------------------- #
@app.command("sync-drive")
def sync_drive(
    account: str | None = typer.Option(
        None, "--account", help="Limit to one account email (default: all)."
    ),
    full: bool = typer.Option(
        False, "--full", help="Force a full re-scan instead of incremental."
    ),
) -> None:
    """Recursive Google Drive ingestion (incremental by default)."""
    from worker.drive.ingest import run_drive_sync

    run_drive_sync(account, full)


@app.command("discover-senders")
def discover_senders(
    account: str | None = typer.Option(
        None, "--account", help="Limit to one account email (default: all)."
    ),
) -> None:
    """Scan Gmail to discover candidate senders for the allow-list."""
    from worker.gmail.discover import run_discover_senders

    run_discover_senders(account)


@app.command("sync-gmail")
def sync_gmail(
    account: str | None = typer.Option(
        None, "--account", help="Limit to one account email (default: all)."
    ),
    now: bool = typer.Option(
        False,
        "--now",
        help="Run immediately, ignoring the off-hours window (manual/test runs).",
    ),
) -> None:
    """Vendor-browser email ingestion. By default only runs in the off-hours
    window; pass --now to force an on-demand run."""
    from worker.gmail.sync import run_gmail_sync

    run_gmail_sync(account, ignore_offhours=now)


@app.command("reconcile")
def reconcile(
    account: str | None = typer.Option(
        None, "--account", help="Limit to one account email (default: all)."
    ),
) -> None:
    """Compare upstream source counts against what was imported."""
    from worker.reconcile import run_reconcile

    run_reconcile(account)


@app.command("restamp")
def restamp(
    account: str | None = typer.Option(
        None, "--account", help="Limit to one account email (default: all)."
    ),
) -> None:
    """Re-write EXIF source dates onto files already on disk (from the DB)."""
    from worker.restamp import run_restamp

    run_restamp(account)


# --------------------------------------------------------------------------- #
# assist (human-in-the-loop resolution of un-automatable vendor emails)
# --------------------------------------------------------------------------- #
@app.command("assist-list")
def assist_list() -> None:
    """List pending human-assist tasks (emails Folio could not auto-ingest)."""
    from worker.assist import run_assist_list

    run_assist_list()


@app.command("assist-resolve")
def assist_resolve(
    task_id: int = typer.Option(..., "--id", help="assist_tasks.id to resolve."),
    file_path: str = typer.Option(
        ..., "--file", help="Path to the original image to ingest for this task."
    ),
) -> None:
    """Resolve an assist task by ingesting a manually-supplied original image."""
    from worker.assist import run_assist_resolve

    run_assist_resolve(task_id, file_path)


# --------------------------------------------------------------------------- #
# backup (pg_dump custom-format archive + retention pruning)
# --------------------------------------------------------------------------- #
@app.command("backup-db")
def backup_db() -> None:
    """Write a timestamped pg_dump archive and prune old ones."""
    from worker.backup import run_backup

    run_backup()


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #
@app.command("schedule")
def schedule() -> None:
    """Run the APScheduler loop (sync-drive + discover + reconcile + sync-gmail).

    The sync-gmail (vendor-browser) job is gated to off-hours and only runs when
    ``browser_enabled`` is set; all other jobs run on their fixed intervals.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)

    def _safe(fn_name: str, fn) -> None:
        """Wrap a job so a single failure never kills the scheduler."""
        try:
            logger.info("scheduler.run job=%s", fn_name)
            fn()
        except Exception:  # noqa: BLE001 - jobs must not crash the loop
            logger.exception("scheduler.job_failed job=%s", fn_name)

    def _job_sync_drive() -> None:
        from worker.drive.ingest import run_drive_sync

        _safe("sync-drive", lambda: run_drive_sync(None, False))

    def _job_discover() -> None:
        from worker.gmail.discover import run_discover_senders

        _safe("discover-senders", lambda: run_discover_senders(None))

    def _job_reconcile() -> None:
        from worker.reconcile import run_reconcile

        _safe("reconcile", lambda: run_reconcile(None))

    def _job_sync_gmail() -> None:
        # The vendor-browser ingestion is RAM-heavy (Chromium); only run it when
        # explicitly enabled AND inside the off-hours window. The fixed interval
        # tick may land outside the window — skip silently when it does. Use the
        # canonical, overnight-aware gate (in_offhours) so a wrap-around window
        # (e.g. 22..6) is honored — a naive `start <= hour < end` check never is.
        from worker.browser.session import in_offhours

        if not settings.browser_enabled:
            logger.info("scheduler.skip job=sync-gmail reason=browser_disabled")
            return
        if not in_offhours():
            logger.info(
                "scheduler.skip job=sync-gmail reason=outside_offhours window=%d-%d",
                settings.browser_offhours_start,
                settings.browser_offhours_end,
            )
            return
        from worker.gmail.sync import run_gmail_sync

        _safe("sync-gmail", lambda: run_gmail_sync(None))

    scheduler.add_job(
        _job_sync_drive,
        "interval",
        minutes=settings.sync_drive_interval_minutes,
        id="sync-drive",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _job_discover,
        "interval",
        minutes=settings.discover_senders_interval_minutes,
        id="discover-senders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _job_reconcile,
        "interval",
        minutes=settings.reconcile_interval_minutes,
        id="reconcile",
        max_instances=1,
        coalesce=True,
    )
    # Vendor-browser Gmail ingestion. Registered unconditionally so config can be
    # flipped at runtime, but the job body self-gates on browser_enabled +
    # off-hours and max_instances=1 enforces ONE browser job at a time.
    scheduler.add_job(
        _job_sync_gmail,
        "interval",
        minutes=settings.gmail_sync_interval_minutes,
        id="sync-gmail",
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "scheduler.start sync_drive=%smin discover=%smin reconcile=%smin "
        "sync_gmail=%smin browser_enabled=%s offhours=%d-%d",
        settings.sync_drive_interval_minutes,
        settings.discover_senders_interval_minutes,
        settings.reconcile_interval_minutes,
        settings.gmail_sync_interval_minutes,
        settings.browser_enabled,
        settings.browser_offhours_start,
        settings.browser_offhours_end,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler.stop")


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _run_migrations() -> None:
    """Apply Alembic migrations to head using folio_core's config."""
    import os

    from alembic import command
    from alembic.config import Config

    ini = os.environ.get("FOLIO_ALEMBIC_INI", "/opt/folio_core/alembic.ini")
    logger.info("migrations.upgrade ini=%s", ini)
    cfg = Config(ini)
    command.upgrade(cfg, "head")


def _seed_admin_user() -> None:
    """Create the admin user from ADMIN_USERNAME/ADMIN_PASSWORD if absent."""
    from sqlalchemy import select

    from folio_core.db import session_scope
    from folio_core.models import User

    settings = get_settings()
    if not settings.admin_password:
        logger.warning(
            "seed_admin.skipped reason=ADMIN_PASSWORD not set; "
            "set it to seed the portal admin user."
        )
        return

    try:
        from argon2 import PasswordHasher
    except ImportError:
        logger.warning(
            "seed_admin.skipped reason=argon2-cffi not installed in this image."
        )
        return

    with session_scope() as session:
        existing = session.scalar(
            select(User).where(User.username == settings.admin_username)
        )
        if existing is not None:
            logger.info("seed_admin.exists username=%s", settings.admin_username)
            return
        user = User(
            username=settings.admin_username,
            argon2_hash=PasswordHasher().hash(settings.admin_password),
            is_active=True,
        )
        session.add(user)
        logger.info("seed_admin.created username=%s", settings.admin_username)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
