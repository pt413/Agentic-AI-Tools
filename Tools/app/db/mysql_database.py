import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()


def get_mysql_engine():
    return create_engine(
        os.getenv("MYSQL_DATABASE_URL"),
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
        connect_args={
            "connect_timeout": 10,
        },
        echo=False,
        future=True,
    )


def fetch_all(engine, query: str, params: dict = None):
    """
    Standard helper for MySQL SELECT queries.
    Returns list of dict rows.
    """
    with engine.connect() as conn:
        result = conn.execute(text(query), params or {})
        return [dict(r._mapping) for r in result.fetchall()]