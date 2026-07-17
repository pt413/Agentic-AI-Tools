"""Database session management for deduplication"""
import os
from contextlib import contextmanager
from sqlalchemy import pool, create_engine
from app.dedupe_config import CONFIG
from app.utils.dedupe_logger import log

_engine = None

def create_pooled_engine(config=CONFIG):
    log.info(f"[DB_ENGINE] Creating engine with pool_size={config.POOL_SIZE}")
    
    return create_engine(
        os.getenv("DATABASE_URL", "postgresql://localhost/mydb"),
        poolclass=pool.QueuePool,
        pool_size=config.POOL_SIZE,
        max_overflow=config.MAX_OVERFLOW,
        pool_recycle=config.POOL_RECYCLE,
        pool_pre_ping=config.POOL_PRE_PING,
        pool_timeout=config.POOL_TIMEOUT,
        echo=False,
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "statement_timeout": config.STATEMENT_TIMEOUT * 1000,
            "application_name": "dedupe_v7",
        }
    )

def get_engine():
    global _engine
    if _engine is None:
        _engine = create_pooled_engine(CONFIG)
        log.info("[DB_ENGINE] Engine created and cached")
    return _engine

@contextmanager
def dedupe_db_session():
    """Properly managed database session"""
    from app.db.database import SessionLocal
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"[DB_SESSION] Error: {type(e).__name__}")
        raise
    finally:
        session.close()