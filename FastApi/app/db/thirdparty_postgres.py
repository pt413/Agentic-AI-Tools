import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# Singleton Engine (Created Once Per Process)
# --------------------------------------------------

_THIRD_PG_ENGINE = None


def get_thirdparty_pg_engine():
    """
    Returns a singleton SQLAlchemy engine
    for the third-party (Neon) Postgres database.

    Engine is created only once per process.
    """

    global _THIRD_PG_ENGINE

    if _THIRD_PG_ENGINE is None:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL not found in environment")

        _THIRD_PG_ENGINE = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=5,
            max_overflow=10,
            connect_args={
                "connect_timeout": 10,
                # "sslmode": "require",
            },
            echo=False,
            future=True,
        )

    return _THIRD_PG_ENGINE


# --------------------------------------------------
# Query Helper
# --------------------------------------------------

def fetch_all(engine, query: str, params: dict | None = None):
    """
    Execute SELECT query and return list of dict rows.
    """

    with engine.connect() as conn:
        result = conn.execute(text(query), params or {})
        return [dict(r._mapping) for r in result.fetchall()]