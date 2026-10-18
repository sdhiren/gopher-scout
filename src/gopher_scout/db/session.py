"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from gopher_scout.config import get_settings
from gopher_scout.db.models import Base


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    if url is None:
        settings.ensure_dirs()
        url = settings.database_url
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")  # the web UI reads while agents write
        cur.close()

    return engine


def init_db(engine: Engine | None = None) -> None:
    Base.metadata.create_all(engine or get_engine())


@lru_cache
def _factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = _factory(engine or get_engine())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
