import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, text

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _candidate_env_paths() -> list[Path]:
    current = Path(__file__).resolve()
    candidates = [
        current.parents[4] / ".env",  # FastApi/.env
        current.parents[5] / ".env",
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
    ]
    deduped: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            deduped.append(path)
            seen.add(key)
    return deduped


def _try_load_env() -> list[str]:
    """
    Load .env from the repo/common working directories.

    Returns the list of .env files that were found and attempted. This is useful
    for debug output because env vars may be absent before this function runs.
    """
    loaded_paths: list[str] = []
    if load_dotenv is None:
        return loaded_paths

    for env_path in _candidate_env_paths():
        if env_path.exists():
            load_dotenv(env_path, override=False)
            loaded_paths.append(str(env_path))
    return loaded_paths


def _mask_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if not parsed.scheme:
            return "***"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        database = parsed.path or ""
        return f"{parsed.scheme}://***:***@{host}{port}{database}"
    except Exception:
        return "***"


def resolve_url(*env_names: str) -> dict:
    """
    Resolve a DB URL and return debug-friendly metadata.

    Important: this loads .env before the second env check. Use this in scripts
    when you want accurate "env present" diagnostics.
    """
    checked_before = {
        name: bool(os.getenv(name) and str(os.getenv(name)).strip())
        for name in env_names
    }

    for env_name in env_names:
        value = os.getenv(env_name)
        if value and str(value).strip():
            return {
                "ok": True,
                "env_name": env_name,
                "url": str(value).strip(),
                "masked_url": _mask_url(str(value).strip()),
                "loaded_env_files": [],
                "checked_before_load": checked_before,
                "checked_after_load": checked_before,
            }

    loaded_paths = _try_load_env()

    checked_after = {
        name: bool(os.getenv(name) and str(os.getenv(name)).strip())
        for name in env_names
    }

    for env_name in env_names:
        value = os.getenv(env_name)
        if value and str(value).strip():
            return {
                "ok": True,
                "env_name": env_name,
                "url": str(value).strip(),
                "masked_url": _mask_url(str(value).strip()),
                "loaded_env_files": loaded_paths,
                "checked_before_load": checked_before,
                "checked_after_load": checked_after,
            }

    return {
        "ok": False,
        "env_name": None,
        "url": None,
        "masked_url": None,
        "loaded_env_files": loaded_paths,
        "checked_before_load": checked_before,
        "checked_after_load": checked_after,
        "error": f"Missing required environment variable. Tried: {', '.join(env_names)}",
    }


def _require_url(*env_names: str) -> str:
    resolved = resolve_url(*env_names)
    if resolved.get("ok") and resolved.get("url"):
        return str(resolved["url"])
    raise RuntimeError(str(resolved.get("error") or f"Missing required environment variable. Tried: {', '.join(env_names)}"))


@lru_cache(maxsize=1)
def get_thirdparty_mysql_engine():
    url = _require_url("MYSQL_DATABASE_URL", "MYSQL_URL")
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_thirdparty_pg_engine():
    url = _require_url("THIRDPARTY_POSTGRES_URL", "THIRD_PARTY_DATABASE_URL")
    return create_engine(url, pool_pre_ping=True)


def get_source_engine(source_kind: str):
    if source_kind == "mysql":
        return get_thirdparty_mysql_engine()
    if source_kind == "thirdparty_pg":
        return get_thirdparty_pg_engine()
    raise ValueError(f"Unsupported source_kind={source_kind}")


def get_source_url_debug(source_kind: str) -> dict:
    if source_kind == "mysql":
        data = resolve_url("MYSQL_DATABASE_URL", "MYSQL_URL")
    elif source_kind == "thirdparty_pg":
        data = resolve_url("THIRDPARTY_POSTGRES_URL", "THIRD_PARTY_DATABASE_URL")
    else:
        raise ValueError(f"Unsupported source_kind={source_kind}")
    clean = dict(data)
    clean.pop("url", None)
    clean["source_kind"] = source_kind
    return clean


def test_engine_connection(engine) -> dict:
    """
    Return source DB identity or an error. Does not raise unless something very
    unexpected happens outside SQLAlchemy.
    """
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                        current_database() AS database_name,
                        current_schema() AS current_schema,
                        inet_server_addr()::text AS server_addr,
                        inet_server_port() AS server_port,
                        current_user AS current_user
                    """
                )
            ).mappings().fetchone()
            return {"ok": True, "db_info": dict(row) if row else {}}
    except Exception as exc:
        return {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }


def fetch_all(engine, sql: str, params: dict | None = None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {}).mappings().fetchall()
