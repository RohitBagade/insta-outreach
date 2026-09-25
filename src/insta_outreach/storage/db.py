"""Engine/session management. SQLite (WAL) locally, any SQLAlchemy URL in prod."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from insta_outreach.storage.models import Base


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite"):
            path = url.split("///", 1)[1] if "///" in url else ""
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.engine: Engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
            event.listen(self.engine, "connect", _sqlite_pragmas)
        else:
            self.engine = create_engine(url, pool_pre_ping=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Unit of work: commits on success, rolls back on error."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
