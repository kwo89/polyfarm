"""SQLAlchemy engine and session factory with SQLite WAL mode."""

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session

from core.config import settings
from core.models import Base


def _get_engine():
    db_path = settings.db_dir()
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    # Enable WAL mode and foreign keys on every new connection
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


engine = _get_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    """Create all tables and apply any pending column migrations."""
    Base.metadata.create_all(engine)
    _migrate()


def _migrate():
    """
    Safe ALTER TABLE migrations for new columns added to existing tables.
    SQLite doesn't support IF NOT EXISTS on ALTER TABLE so we catch the error.
    Add a new entry here whenever a column is added to an existing model.
    """
    migrations = [
        "ALTER TABLE bot_registry ADD COLUMN our_capital REAL DEFAULT 100.0",
        "ALTER TABLE bot_registry ADD COLUMN initial_capital REAL DEFAULT NULL",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore


@contextmanager
def get_session() -> Session:
    """Context manager for a DB session with automatic rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
