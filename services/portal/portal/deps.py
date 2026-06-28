"""Shared FastAPI dependencies: DB session, authenticated-user guard, and a
path-traversal-safe resolver for files served out of ``MEDIA_ROOT``.

``require_user`` reads the signed session cookie populated by the auth router
and rejects unauthenticated callers with ``401``. Every ``/api/*`` router except
the login endpoints mounts it as a router-level dependency.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from folio_core.config import get_settings
from folio_core.db import get_session

SESSION_USER_KEY = "user"


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a SQLAlchemy session.

    Thin wrapper over :func:`folio_core.db.get_session` so routers have a single
    import surface. Writes must ``commit()`` explicitly.
    """
    yield from get_session()


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Return the logged-in user payload from the session, or ``None``."""
    user = request.session.get(SESSION_USER_KEY)
    if isinstance(user, dict) and user.get("username"):
        return user
    return None


def require_user(request: Request) -> dict[str, Any]:
    """Auth guard: return the session user or raise ``401``."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return user


def safe_media_path(stored_path: str) -> Path:
    """Resolve a MEDIA_ROOT-relative stored path to an absolute file path.

    Rejects absolute inputs and any ``..`` escape that would resolve outside
    ``MEDIA_ROOT``. Raises ``404`` if the path escapes or the file is missing.
    """
    root = Path(get_settings().media_root).resolve()
    rel = (stored_path or "").lstrip("/\\")
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return candidate


# Convenience alias matching FastAPI dependency idioms.
DbSession = Depends(get_db)
CurrentUser = Depends(require_user)


__all__ = [
    "get_db",
    "get_current_user",
    "require_user",
    "safe_media_path",
    "SESSION_USER_KEY",
    "DbSession",
    "CurrentUser",
]
