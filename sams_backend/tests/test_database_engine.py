"""
tests/test_database_engine.py
Sanity checks for the SQLite engine hardening in models/database.py:
WAL journal mode, busy_timeout and per-connection foreign-key enforcement.
Uses a real temp file (WAL is not supported on :memory: databases).
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.database import create_db_engine, get_session_factory, Device


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_db_engine(sqlite_path=str(tmp_path / "sams_test.db"))
    try:
        yield engine
    finally:
        engine.dispose()


def test_sqlite_pragmas_applied(file_engine):
    with file_engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        # synchronous=NORMAL reports as 1
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1


def test_sqlite_enforces_foreign_keys(file_engine):
    """foreign_keys=ON must reject orphan rows (previously silently allowed)."""
    SessionLocal = get_session_factory(file_engine)
    session = SessionLocal()
    try:
        session.add(Device(device_id="dev-x", location_id="no-such-location"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
