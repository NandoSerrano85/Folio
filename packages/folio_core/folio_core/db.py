"""Database engine, session factory, and the declarative ``Base``.

All models inherit from :class:`Base`. Services obtain sessions via
:func:`session_scope` (a transactional context manager) or :func:`get_session`
(a FastAPI-friendly generator dependency).
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from folio_core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all Folio ORM models."""


_settings = get_settings()

# pool_pre_ping guards against stale connections after the db container
# restarts; the worker is long-lived so this matters.
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    Commits on success, rolls back on exception, always closes.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session (no implicit commit).

    Callers are responsible for committing writes; the session is always
    closed when the request finishes.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
