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

        # Ensure directory exists for SQLite
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
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
    """Create all tables if they don't exist. Safe to call on every startup."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialized.")
