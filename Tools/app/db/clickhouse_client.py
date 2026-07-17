import os
from typing import Any

try:
    from clickhouse_connect import get_client
except Exception:  # pragma: no cover - optional dependency
    get_client = None  # type: ignore


def get_ch_client(host: str | None = None, database: str | None = None, username: str | None = None, password: str | None = None) -> Any:

    if get_client is None:
        raise RuntimeError("clickhouse_connect is not installed. pip install clickhouse-connect")

    host = host or os.getenv("CLICKHOUSE_HOST", "clickhouse")
    database = database or os.getenv("CLICKHOUSE_DB", "connector")
    username = username or os.getenv("CLICKHOUSE_USER", "default")
    password = password or os.getenv("CLICKHOUSE_PASSWORD", "")

    return get_client(host=host, username=username, password=password, database=database)
