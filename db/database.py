"""SQLAlchemy database engine, session factory, and initialization."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from broker_recon_flow.config import get_db_config
from broker_recon_flow.db.models import Base
from broker_recon_flow.utils.logger import get_logger

logger = get_logger(__name__)

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_db_config()
        db_url = cfg.get("url", "sqlite:///data/reconciliation.db")

        # Resolve relative SQLite paths against the project root (broker_recon_flow/)
        # so the DB is always created inside the project regardless of CWD.
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            if not Path(db_path).is_absolute():
                project_root = Path(__file__).parent.parent  # broker_recon_flow/
                db_path = str(project_root / db_path)
                db_url = f"sqlite:///{db_path}"
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        _engine = create_engine(
            db_url,
            echo=cfg.get("echo", False),
            connect_args={"check_same_thread": False},
        )
        logger.info("Database engine created: %s", db_url)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionLocal


def get_db() -> Session:
    """FastAPI dependency — yields a session and always closes it."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup.

    Also runs lightweight column-addition migrations for SQLite so that new
    nullable columns added to existing models get applied to already-created
    databases without requiring Alembic.
    """
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _run_column_migrations(engine)
    logger.info("Database tables initialized.")


# Columns to add if they are missing from an existing table.
# Format: (table_name, column_name, column_ddl)
_COLUMN_MIGRATIONS = [
    ("template_cache",          "pdf_fingerprint", "TEXT"),
    ("optimized_prompt_cache",  "pdf_fingerprint", "TEXT"),
]


def _run_column_migrations(engine) -> None:
    """Add new nullable columns to existing SQLite tables (idempotent)."""
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, col, ddl in _COLUMN_MIGRATIONS:
            try:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                existing = {r[1] for r in rows}  # column names are index 1
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                    conn.commit()
                    logger.info("Migration: added column %s.%s", table, col)
            except Exception as exc:
                logger.warning("Column migration %s.%s skipped: %s", table, col, exc)
