"""Database backup: a timestamped ``pg_dump`` archive + retention pruning.

``run_backup`` shells out to ``pg_dump`` (custom format, ``-Fc``) against
``DATABASE_URL`` and writes ``folio-<UTC-timestamp>.dump`` into ``backup_dir``,
then deletes archives older than ``backup_retention_days``.

Requirements / notes:
* The worker image ships ``postgresql-client`` (added in the worker Dockerfile)
  so ``pg_dump`` is on PATH.
* The SQLAlchemy URL uses the ``postgresql+psycopg`` driver; pg_dump wants a
  plain ``postgresql://`` libpq URL, so we strip the ``+psycopg`` suffix.
* The DB password is passed to pg_dump via the ``PGPASSWORD`` env var (never on
  argv, where ``ps``/proc would expose it) and is NEVER logged. Only the
  host/db/port and the output filename are logged.
* This runs at runtime (not in a workflow), so ``datetime.now`` for the
  filename timestamp is fine.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from folio_core.config import get_settings
from folio_core.logging import get_logger

logger = get_logger("worker.backup")

_DUMP_PREFIX = "folio-"
_DUMP_SUFFIX = ".dump"


def _libpq_url(sqlalchemy_url: str) -> str:
    """Convert a SQLAlchemy URL to a libpq URL pg_dump understands.

    ``postgresql+psycopg://...`` -> ``postgresql://...``. Any other scheme is
    returned unchanged (pg_dump will report a clear error if it is unusable).
    """
    scheme, sep, rest = sqlalchemy_url.partition("://")
    if not sep:
        return sqlalchemy_url
    base = scheme.split("+", 1)[0]
    return f"{base}://{rest}"


def _safe_target(url: str) -> str:
    """A credential-free 'host:port/db' description for logging."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = parts.port or 5432
        db = parts.path.lstrip("/") or "?"
        return f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001 - logging helper must never raise
        return "?"


def _prune(backup_dir: Path, retention_days: int) -> int:
    """Delete dumps older than ``retention_days``. Returns the count removed."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for path in backup_dir.glob(f"{_DUMP_PREFIX}*{_DUMP_SUFFIX}"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("backup.prune_failed file=%s", path.name)
    return removed


def run_backup() -> None:
    """Create a custom-format pg_dump archive and prune stale ones."""
    settings = get_settings()
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    url = _libpq_url(settings.database_url)
    target = _safe_target(url)
    parts = urlsplit(url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_dir / f"{_DUMP_PREFIX}{stamp}{_DUMP_SUFFIX}"

    logger.info("backup.start target=%s out=%s", target, out_path.name)
    # Connection params as flags; the password goes in PGPASSWORD (subprocess
    # env), NEVER on argv where `ps`/proc would expose it.
    cmd = [
        "pg_dump",
        "--format=custom",
        "--file",
        str(out_path),
        "-h",
        parts.hostname or "localhost",
        "-p",
        str(parts.port or 5432),
        "-U",
        unquote(parts.username) if parts.username else "postgres",
        "-d",
        unquote(parts.path.lstrip("/")) or "postgres",
    ]
    env = os.environ.copy()
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    try:
        # capture stderr so we can surface pg_dump errors (no secrets on argv).
        result = subprocess.run(  # noqa: S603 - args are controlled, no shell
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        logger.error(
            "backup.failed reason=pg_dump_not_found "
            "(install postgresql-client in the worker image)"
        )
        raise

    if result.returncode != 0:
        # pg_dump never prints the password in stderr, but be conservative and
        # log only a trimmed first line.
        stderr_head = (result.stderr or "").strip().splitlines()
        detail = stderr_head[0] if stderr_head else f"exit={result.returncode}"
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.error("backup.failed target=%s detail=%s", target, detail)
        raise RuntimeError(f"pg_dump failed: {detail}")

    try:
        size = out_path.stat().st_size
    except OSError:
        size = -1

    pruned = _prune(backup_dir, settings.backup_retention_days)
    logger.info(
        "backup.done out=%s bytes=%d pruned=%d retention_days=%d",
        out_path.name,
        size,
        pruned,
        settings.backup_retention_days,
    )


__all__ = ["run_backup"]
