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
        cursor.execute("PRAGMA cache_size=-32000")   # 32 MB page cache (negative = KB)
        cursor.execute("PRAGMA temp_store=MEMORY")   # temp tables in RAM
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
        "ALTER TABLE bot_registry ADD COLUMN bucket_t1 REAL DEFAULT NULL",
        "ALTER TABLE bot_registry ADD COLUMN bucket_t2 REAL DEFAULT NULL",
        "ALTER TABLE bot_registry ADD COLUMN bucket_t3 REAL DEFAULT NULL",
        "ALTER TABLE bot_registry ADD COLUMN bucket_t4 REAL DEFAULT NULL",
        "ALTER TABLE bot_registry ADD COLUMN reset_at TEXT DEFAULT NULL",
        # Performance indexes — CREATE INDEX IF NOT EXISTS is safe to re-run
        "CREATE INDEX IF NOT EXISTS idx_paper_trades_bot_created ON paper_trades(bot_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_paper_trades_resolved ON paper_trades(market_resolved)",
        "CREATE INDEX IF NOT EXISTS idx_target_trades_bot_detected ON target_trades(bot_id, detected_at)",
        "CREATE INDEX IF NOT EXISTS idx_target_trades_status ON target_trades(status, detected_at)",
        "CREATE INDEX IF NOT EXISTS idx_daily_pnl_bot_date ON daily_pnl(bot_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_seen_tx_bot ON seen_transactions(bot_id)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists / index already exists — safe to ignore


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
