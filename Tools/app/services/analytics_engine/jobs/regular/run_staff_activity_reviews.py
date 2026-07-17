#!/usr/bin/env python3
"""
run_staff_activity_reviews.py

Cron-safe staff activity review job for RentMyStay AnalyticsEngine.

Purpose:
  - Select active admin/staff users from AnalyticsEngine.admin_login_token.
  - For each staff user, call StaffActivityReviewService.build_staff_activity().
  - Optionally run the generated LLM prompt through the bpai LLM proxy.
  - Store one latest row per staff member in AnalyticsEngine.staff_activity_review.

Typical commands:
  # Dry-run candidate selection only
  python -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --dry-run --limit 10 --pretty

  # Rate all active admin users, sequentially
  python -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --limit 200 --pretty

  # Run in parallel, only caretakers
  python -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --team Caretaker --workers 3 --limit 200 --pretty

  # Build/stash prompts without calling LLM
  python -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --no-llm --limit 200 --pretty

Windows Task Scheduler example:
  Program:  C:\\BP_AI\\BP_AI\\venv\\Scripts\\python.exe
  Arguments: -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --limit 500 --workers 3 --pretty
  Start in: C:\\BP_AI\\BP_AI\\FastApi

Linux cron example:
  15 8 * * * cd /opt/BP_AI/FastApi && /opt/BP_AI/venv/bin/python -m app.services.analytics_engine.jobs.regular.run_staff_activity_reviews --limit 500 --workers 3 >> /var/log/staff_activity_reviews.log 2>&1
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------

def _project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # noqa: E402
    from app.services.analytics_engine.capabilities.staff_activity_review import StaffActivityReviewService
except Exception as exc:  # pragma: no cover
    StaffActivityReviewService = None  # type: ignore[assignment]
    STAFF_ACTIVITY_IMPORT_ERROR = exc
else:
    STAFF_ACTIVITY_IMPORT_ERROR = None


DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", os.getenv("STAFF_ACTIVITY_REVIEW_MODEL", "gpt-5-mini"))
LLM_UPSTREAM_URL = os.getenv("LLM_UPSTREAM_URL", "https://app.bpai.info/api/bpai/run_llm")
STAFF_REVIEW_CACHE_TABLE = os.getenv("STAFF_ACTIVITY_REVIEW_TABLE", "staff_activity_review")
DEFAULT_WORKERS = max(1, int(os.getenv("STAFF_ACTIVITY_REVIEW_WORKERS", "3")))

SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------

def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT.parent / ".env",
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _db_url(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    value = os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if value:
        return value
    _try_load_env()
    return os.getenv("DATABASE_URL") or os.getenv("PG_URL")


@contextmanager
def open_db_session(database_url: Optional[str] = None) -> Iterable[Session]:
    """Open a DB session for the current process/thread.

    SQLAlchemy sessions are not thread-safe, so every parallel worker must open
    its own session instead of sharing the parent selection session.
    """
    if not database_url:
        try:
            from app.db.database import SessionLocal  # type: ignore  # noqa: WPS433

            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()
            return
        except Exception:
            pass

        # Compatibility fallback for repo versions that expose get_db() but not
        # SessionLocal directly.
        repo_gen = None
        repo_db = None
        try:
            from app.db.database import get_db  # type: ignore  # noqa: WPS433

            repo_gen = get_db()
            repo_db = next(repo_gen)
            try:
                yield repo_db
            finally:
                try:
                    repo_db.close()
                except Exception:
                    pass
                try:
                    next(repo_gen, None)
                except Exception:
                    try:
                        repo_gen.close()
                    except Exception:
                        pass
            return
        except Exception:
            if repo_db is not None:
                try:
                    repo_db.close()
                except Exception:
                    pass
            if repo_gen is not None:
                try:
                    repo_gen.close()
                except Exception:
                    pass

    url = _db_url(database_url)
    if not url:
        raise RuntimeError("No DB available. Run inside repo or set DATABASE_URL / PG_URL, or pass --database-url.")
    engine = create_engine(url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()


def _safe_ident(value: str) -> str:
    if not SAFE_IDENT_RE.fullmatch(str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def schema_ident(schema: str) -> str:
    return f'"{_safe_ident(schema)}"'


def table_ref(schema: str, table_name: str) -> str:
    return f"{schema_ident(schema)}.{_safe_ident(table_name)}"


def q(db: Session, sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in db.execute(text(sql), params or {}).fetchall()]


def q1(db: Session, sql: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    rows = q(db, sql, params)
    return rows[0] if rows else None


def table_exists(db: Session, schema: str, table_name: str) -> bool:
    row = q1(
        db,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = :schema_name
              AND table_name = :table_name
        ) AS present
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return bool(row and row.get("present"))


def table_columns(db: Session, schema: str, table_name: str) -> set[str]:
    if not table_exists(db, schema, table_name):
        return set()
    rows = q(
        db,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema_name
          AND table_name = :table_name
        """,
        {"schema_name": schema, "table_name": table_name},
    )
    return {str(row.get("column_name")) for row in rows if row.get("column_name")}


def named_in(values: Sequence[Any], prefix: str) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    holders: list[str] = []
    for idx, value in enumerate(values):
        key = f"{prefix}_{idx}"
        holders.append(f":{key}")
        params[key] = value
    return ", ".join(holders) or "NULL", params


# -----------------------------------------------------------------------------
# JSON / parsing helpers
# -----------------------------------------------------------------------------

def json_dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, default=str, sort_keys=not pretty)


def json_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def compact_dict(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if item in (None, "", [], {}, ()):  # keep 0 and False
            continue
        if isinstance(item, dict):
            child = compact_dict(item)
            if child:
                out[key] = child
        elif isinstance(item, list):
            cleaned = []
            for row in item:
                if isinstance(row, dict):
                    child = compact_dict(row)
                    if child:
                        cleaned.append(child)
                elif row not in (None, "", [], {}, ()):  # keep 0 and False
                    cleaned.append(row)
            if cleaned:
                out[key] = cleaned
        else:
            out[key] = item
    return out


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def score_number(value: Any) -> int | float | None:
    match = re.search(r"\b(10|[0-9](?:\.\d+)?)\s*/\s*10\b|\b(10|[0-9](?:\.\d+)?)\b", str(value or ""))
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        score = float(raw)
        return int(score) if score.is_integer() else score
    except Exception:
        return None


def _clean_cell(value: Any) -> str:
    text_value = re.sub(r"<br\s*/?>", " ", str(value or ""), flags=re.I)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value.strip("` ")


def _header_key(cell: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(cell or "").strip().lower())).strip("_")


def _is_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", str(cell or "").replace(" ", "")) for cell in cells)


def parse_markdown_table(review_text: str, required_terms: Sequence[str]) -> list[dict[str, str]]:
    lines = [line.rstrip() for line in str(review_text or "").splitlines()]
    required = [term.lower() for term in required_terms]
    for idx, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        lowered = line.lower()
        if not all(term in lowered for term in required):
            continue
        headers = [_header_key(cell) for cell in line.strip().strip("|").split("|")]
        rows: list[dict[str, str]] = []
        for raw in lines[idx + 1:]:
            if not raw.strip():
                if rows:
                    break
                continue
            if not raw.lstrip().startswith("|"):
                if rows:
                    break
                continue
            cells = [_clean_cell(cell) for cell in raw.strip().strip("|").split("|")]
            if len(cells) < len(headers):
                continue
            if _is_separator(cells):
                continue
            rows.append({headers[i]: cells[i] for i in range(min(len(headers), len(cells)))})
        return rows
    return []


def _priority_number(value: Any) -> int | None:
    parsed = score_number(value)
    if parsed is None:
        return None
    try:
        return max(1, min(10, int(round(float(parsed)))))
    except Exception:
        return None


def parse_action_rows(review_text: str) -> list[dict[str, Any]]:
    rows = parse_markdown_table(review_text, ("priority", "owner", "action"))
    out: list[dict[str, Any]] = []
    for row in rows:
        priority_key = next((key for key in row if "priority" in key), "")
        owner_key = next((key for key in row if "owner" in key or "team" in key), "")
        action_key = next((key for key in row if "action" in key), "")
        evidence_key = next((key for key in row if "evidence" in key), "")
        if "stakeholder" in owner_key and "owner" not in owner_key:
            continue
        out.append(
            compact_dict(
                {
                    "priority_score": _priority_number(row.get(priority_key)),
                    "owner_team": row.get(owner_key),
                    "action": row.get(action_key),
                    "evidence": row.get(evidence_key),
                }
            )
        )
    return out


def parse_stakeholder_scores(review_text: str) -> list[dict[str, Any]]:
    rows = parse_markdown_table(review_text, ("stakeholder", "score", "evidence"))
    out: list[dict[str, Any]] = []
    for row in rows:
        stakeholder_key = next((key for key in row if "stakeholder" in key or "team" in key), "")
        score_key = next((key for key in row if key == "score_10" or key.startswith("score") or "score" in key), "")
        priority_key = next((key for key in row if "priority" in key), "")
        handled_key = next((key for key in row if "handled" in key or "what_they" in key), "")
        gaps_key = next((key for key in row if "gap" in key), "")
        evidence_key = next((key for key in row if "evidence" in key), "")
        out.append(
            compact_dict(
                {
                    "stakeholder_team": row.get(stakeholder_key),
                    "score": score_number(row.get(score_key)),
                    "priority_score": _priority_number(row.get(priority_key)),
                    "handled": row.get(handled_key),
                    "gaps": row.get(gaps_key),
                    "evidence": row.get(evidence_key),
                }
            )
        )
    return out


def _score_after_label(text_value: str, *labels: str) -> int | float | None:
    for label in labels:
        pattern = rf"\b{re.escape(label)}\s*(?:score)?\s*(?:[:\-;]|\s)\s*(10|[0-9](?:\.\d+)?)\s*/\s*10"
        match = re.search(pattern, text_value, flags=re.I)
        if match:
            return score_number(match.group(1))
    return None


def parse_review_text(review_text: str) -> dict[str, Any]:
    text_value = str(review_text or "")
    out: dict[str, Any] = {}
    out["overall_score"] = _score_after_label(text_value, "overall", "score")
    out["overall_priority_score"] = _score_after_label(text_value, "priority", "overall priority")
    out["staff_activity_score"] = _score_after_label(text_value, "staff activity", "activity")
    out["communication_score"] = _score_after_label(text_value, "communication")
    out["ownership_score"] = _score_after_label(text_value, "ownership")

    if out.get("overall_score") is None:
        match = re.search(r"1\.\s*Overall.*?(10|[0-9](?:\.\d+)?)\s*/\s*10", text_value, flags=re.I | re.S)
        if match:
            out["overall_score"] = score_number(match.group(1))

    risk_match = re.search(r"(?:overall\s+)?risk\s*(?:[:\-;]|\s)\s*(Low|Medium|High)", text_value, flags=re.I)
    if risk_match:
        out["overall_risk"] = risk_match.group(1).title()

    reason_match = re.search(r"(?:main reason|one[-\s]*line reason|reason)\s*[:\-]\s*(.+)", text_value, flags=re.I)
    if reason_match:
        out["main_reason"] = reason_match.group(1).strip().strip("*")[:800]

    actions = parse_action_rows(text_value)
    stakeholders = parse_stakeholder_scores(text_value)
    out["action_rows"] = actions
    out["stakeholder_scores"] = stakeholders

    # If the prompt/output does not contain a global priority, derive it from
    # the action table to keep the dashboard sortable.
    if out.get("overall_priority_score") is None and actions:
        priorities = [row.get("priority_score") for row in actions if row.get("priority_score") is not None]
        if priorities:
            out["overall_priority_score"] = max(priorities)

    return compact_dict(out)


# -----------------------------------------------------------------------------
# LLM proxy
# -----------------------------------------------------------------------------

def extract_llm_text(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, default=str)

    for key in ("result", "response", "content", "text", "output"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("data", "message", "choices"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = extract_llm_text(value)
            if nested and nested != "{}":
                return nested
        if isinstance(value, list) and value:
            chunks = [extract_llm_text(item) for item in value]
            chunks = [chunk for chunk in chunks if chunk and chunk != "{}"]
            if chunks:
                return "\n".join(chunks).strip()

    return json.dumps(data, ensure_ascii=False, default=str)


def run_llm_prompt(prompt: str, *, model: str, timeout_seconds: int, staff_key: str) -> str:
    import httpx

    safe_timeout = max(30.0, float(timeout_seconds or 120))
    timeout = httpx.Timeout(connect=10.0, read=safe_timeout, write=30.0, pool=5.0)
    request_payload = {
        "system_prompt": prompt,
        "payload": {
            "model": model,
            "source": "staff_activity_review",
            "staff_key": staff_key,
        },
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(LLM_UPSTREAM_URL, json=request_payload)
            response.raise_for_status()
            return extract_llm_text(response.json())
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"LLM upstream timed out: {exc.__class__.__name__}") from exc
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(f"LLM upstream returned {status}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM proxy error: {exc.__class__.__name__}: {exc}") from exc


# -----------------------------------------------------------------------------
# Cache table
# -----------------------------------------------------------------------------

CACHE_COLUMNS: dict[str, str] = {
    "staff_key": "TEXT PRIMARY KEY",
    "user_source_id": "BIGINT",
    "username": "TEXT",
    "email": "TEXT",
    "team": "TEXT",
    "active": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'ok'",
    "error": "TEXT",
    "model": "TEXT",
    "context_hash": "TEXT",
    "overall_score": "NUMERIC",
    "overall_priority_score": "NUMERIC",
    "staff_activity_score": "NUMERIC",
    "communication_score": "NUMERIC",
    "ownership_score": "NUMERIC",
    "overall_risk": "TEXT",
    "main_reason": "TEXT",
    "action_rows": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "stakeholder_scores": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "summary": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "activity_payload": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "llm_context": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "llm_prompt": "TEXT",
    "review_text": "TEXT",
    "review_window_days": "INT",
    "review_generated_at": "TIMESTAMP",
    "stale_at": "TIMESTAMP",
    "created_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
    "updated_at": "TIMESTAMP NOT NULL DEFAULT NOW()",
}
JSONB_COLUMNS = {"action_rows", "stakeholder_scores", "summary", "activity_payload", "llm_context"}
NO_UPDATE_COLUMNS = {"staff_key", "created_at"}


def ensure_staff_review_cache_table(db: Session, schema: str) -> None:
    table = table_ref(schema, STAFF_REVIEW_CACHE_TABLE)
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_ident(schema)}"))
    db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                staff_key TEXT PRIMARY KEY,
                user_source_id BIGINT,
                username TEXT,
                email TEXT,
                team TEXT,
                active TEXT,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT,
                model TEXT,
                context_hash TEXT,
                overall_score NUMERIC,
                overall_priority_score NUMERIC,
                staff_activity_score NUMERIC,
                communication_score NUMERIC,
                ownership_score NUMERIC,
                overall_risk TEXT,
                main_reason TEXT,
                action_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
                stakeholder_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
                summary JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                activity_payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                llm_context JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                llm_prompt TEXT,
                review_text TEXT,
                review_window_days INT,
                review_generated_at TIMESTAMP,
                stale_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    for column_name, column_type in CACHE_COLUMNS.items():
        if column_name == "staff_key":
            continue
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_safe_ident(column_name)} {column_type}"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_staff_activity_review_team ON {table} (team)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_staff_activity_review_priority ON {table} (overall_priority_score DESC NULLS LAST)"))
    db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_staff_activity_review_updated_at ON {table} (updated_at DESC NULLS LAST)"))
    db.commit()


def store_staff_review(db: Session, schema: str, row: dict[str, Any]) -> None:
    ensure_staff_review_cache_table(db, schema)
    table = table_ref(schema, STAFF_REVIEW_CACHE_TABLE)
    payload = {column: row.get(column) for column in CACHE_COLUMNS}
    payload["updated_at"] = row.get("updated_at") or None
    columns = list(payload.keys())
    params: dict[str, Any] = {}
    insert_values: list[str] = []
    for column in columns:
        value = payload[column]
        if column in JSONB_COLUMNS:
            params[column] = json_param(value if value not in (None, "") else ([] if column.endswith("rows") or column.endswith("scores") else {}))
            insert_values.append(f"CAST(:{column} AS jsonb)")
        elif column in {"updated_at", "review_generated_at"} and value is None:
            insert_values.append("NOW()")
        else:
            params[column] = value
            insert_values.append(f":{column}")

    update_parts = []
    for column in columns:
        if column in NO_UPDATE_COLUMNS:
            continue
        update_parts.append(f"{_safe_ident(column)} = EXCLUDED.{_safe_ident(column)}")
    if "updated_at" not in columns:
        update_parts.append("updated_at = NOW()")

    db.execute(
        text(
            f"""
            INSERT INTO {table} ({', '.join(_safe_ident(column) for column in columns)})
            VALUES ({', '.join(insert_values)})
            ON CONFLICT (staff_key) DO UPDATE SET
                {', '.join(update_parts)}
            """
        ),
        params,
    )
    db.commit()


# -----------------------------------------------------------------------------
# Staff selection
# -----------------------------------------------------------------------------

def _parse_csv(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _staff_key(row: dict[str, Any]) -> str:
    if row.get("user_source_id") not in (None, ""):
        return str(row.get("user_source_id"))
    if row.get("username"):
        return f"username:{str(row.get('username')).strip().lower()}"
    if row.get("email"):
        return f"email:{str(row.get('email')).strip().lower()}"
    raise ValueError(f"Cannot build staff key from row: {row}")


def select_admin_users(
    db: Session,
    *,
    schema: str,
    team: Optional[str] = None,
    active_only: bool = True,
    include_deleted: bool = False,
    include_suspended: bool = False,
    usernames: Sequence[str] | None = None,
    user_source_ids: Sequence[int] | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Select admin/staff users.

    Preferred source is AnalyticsEngine.admin_login_token because it contains
    user_source_id + username + email + team + active in one compact table.
    Fallback is StaffActivityReviewService.list_staff() for older databases.
    """
    if table_exists(db, schema, "admin_login_token"):
        cols = table_columns(db, schema, "admin_login_token")
        table = table_ref(schema, "admin_login_token")
        select_parts = []
        for column in ("user_source_id", "username", "email", "team", "active", "deleted_on", "suspended_on", "synced_at", "last_used_at"):
            select_parts.append(f"{column}" if column in cols else f"NULL AS {column}")

        where_parts = ["1=1"]
        params: dict[str, Any] = {"limit_n": max(1, int(limit or 500)), "offset_n": max(0, int(offset or 0))}

        if active_only and "active" in cols:
            where_parts.append("LOWER(TRIM(COALESCE(active::text, ''))) IN ('active', '1', 'true', 't', 'yes')")
        if not include_deleted and "deleted_on" in cols:
            where_parts.append("(deleted_on IS NULL OR TRIM(COALESCE(deleted_on::text, '')) = '')")
        if not include_suspended and "suspended_on" in cols:
            where_parts.append("(suspended_on IS NULL OR TRIM(COALESCE(suspended_on::text, '')) = '')")
        if team and "team" in cols:
            where_parts.append("LOWER(TRIM(COALESCE(team::text, ''))) = :team")
            params["team"] = str(team).strip().lower()
        if usernames:
            in_sql, in_params = named_in([str(value).strip().lower() for value in usernames], "username")
            params.update(in_params)
            where_parts.append(f"LOWER(TRIM(COALESCE(username::text, ''))) IN ({in_sql})")
        if user_source_ids:
            in_sql, in_params = named_in([int(value) for value in user_source_ids], "user_source_id")
            params.update(in_params)
            where_parts.append(f"user_source_id IN ({in_sql})")

        rows = q(
            db,
            f"""
            SELECT {', '.join(select_parts)}
            FROM {table}
            WHERE {' AND '.join(where_parts)}
              AND COALESCE(username::text, email::text, user_source_id::text, '') <> ''
            ORDER BY
                CASE WHEN team IS NULL OR TRIM(COALESCE(team::text, '')) = '' THEN 1 ELSE 0 END,
                team ASC NULLS LAST,
                user_source_id ASC NULLS LAST,
                username ASC NULLS LAST
            LIMIT :limit_n OFFSET :offset_n
            """,
            params,
        )
        for row in rows:
            row["staff_key"] = _staff_key(row)
        return rows

    # Fallback path.  This is intentionally best-effort because service list
    # shapes can vary by repo version.
    if StaffActivityReviewService is None:
        raise RuntimeError(f"StaffActivityReviewService import failed: {STAFF_ACTIVITY_IMPORT_ERROR}")
    service = StaffActivityReviewService(db=db, schema=schema)
    staff_rows = service.list_staff(team=team, active=active_only, limit=limit)
    rows = []
    for item in staff_rows:
        if not isinstance(item, dict):
            continue
        row = {
            "user_source_id": item.get("user_source_id") or item.get("source_id") or item.get("staff_id"),
            "username": item.get("username") or item.get("actor") or item.get("name"),
            "email": item.get("email"),
            "team": item.get("team") or item.get("role") or item.get("actor_role"),
            "active": item.get("active"),
        }
        if usernames and str(row.get("username") or "").strip().lower() not in {u.lower() for u in usernames}:
            continue
        if user_source_ids and row.get("user_source_id") not in user_source_ids:
            continue
        row["staff_key"] = _staff_key(row)
        rows.append(row)
    return rows[offset: offset + limit]


# -----------------------------------------------------------------------------
# Review execution
# -----------------------------------------------------------------------------

def _safe_build_staff_activity(service: Any, **kwargs: Any) -> dict[str, Any]:
    fn = service.build_staff_activity
    try:
        sig = inspect.signature(fn)
        accepted = {key: value for key, value in kwargs.items() if key in sig.parameters}
        result = fn(**accepted)
    except TypeError:
        # Older repo versions may have a slightly smaller signature.
        minimal = {key: kwargs[key] for key in ("username", "email", "phone", "role", "days") if key in kwargs}
        result = fn(**minimal)
    return result if isinstance(result, dict) else {"result": result}


def _extract_prompt(activity: dict[str, Any]) -> str:
    for key in ("llm_prompt", "prompt"):
        value = activity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    copy_blocks = activity.get("copy_blocks")
    if isinstance(copy_blocks, dict):
        for key in ("prompt", "llm_prompt", "copy_prompt"):
            value = copy_blocks.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(copy_blocks, list):
        for item in copy_blocks:
            if isinstance(item, dict) and "prompt" in str(item.get("label") or "").lower():
                value = item.get("text") or item.get("value")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _extract_context(activity: dict[str, Any]) -> dict[str, Any]:
    for key in ("llm_context", "context", "payload"):
        value = activity.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _extract_summary(activity: dict[str, Any]) -> dict[str, Any]:
    for key in ("summary", "counts", "metrics"):
        value = activity.get(key)
        if isinstance(value, dict):
            return {key: value} if key != "summary" else value
    return {}


def review_one_staff(
    *,
    database_url: Optional[str],
    schema: str,
    staff: dict[str, Any],
    days: int,
    limit: int,
    print_limit: int,
    max_text: int,
    model: str,
    timeout_seconds: int,
    run_llm: bool,
    display_mode: str,
    debug: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    staff_key = str(staff.get("staff_key") or _staff_key(staff))
    username = staff.get("username")
    email = staff.get("email")
    team = staff.get("team")
    try:
        if StaffActivityReviewService is None:
            raise RuntimeError(f"StaffActivityReviewService import failed: {STAFF_ACTIVITY_IMPORT_ERROR}")
        with open_db_session(database_url) as db:
            service = StaffActivityReviewService(db=db, schema=schema)
            activity = _safe_build_staff_activity(
                service,
                username=username,
                email=email,
                phone=staff.get("phone"),
                role="auto",
                days=days,
                limit=limit,
                print_limit=print_limit,
                max_text=max_text,
                llm=True,
                display_mode=display_mode,
            )
            prompt = _extract_prompt(activity)
            llm_context = _extract_context(activity)
            context_hash = stable_json_hash(llm_context or activity)
            review_text = ""
            parsed: dict[str, Any] = {}
            status = "prompt_built"
            error = None

            if run_llm:
                if not prompt:
                    status = "error"
                    error = "No llm_prompt returned by StaffActivityReviewService.build_staff_activity()."
                else:
                    review_text = run_llm_prompt(prompt, model=model, timeout_seconds=timeout_seconds, staff_key=staff_key)
                    parsed = parse_review_text(review_text)
                    status = "ok"

            cache_row = compact_dict(
                {
                    "staff_key": staff_key,
                    "user_source_id": staff.get("user_source_id"),
                    "username": username,
                    "email": email,
                    "team": team,
                    "active": staff.get("active"),
                    "status": status,
                    "error": error,
                    "model": model if run_llm else None,
                    "context_hash": context_hash,
                    "overall_score": parsed.get("overall_score"),
                    "overall_priority_score": parsed.get("overall_priority_score"),
                    "staff_activity_score": parsed.get("staff_activity_score"),
                    "communication_score": parsed.get("communication_score"),
                    "ownership_score": parsed.get("ownership_score"),
                    "overall_risk": parsed.get("overall_risk"),
                    "main_reason": parsed.get("main_reason"),
                    "action_rows": parsed.get("action_rows") or [],
                    "stakeholder_scores": parsed.get("stakeholder_scores") or [],
                    "summary": _extract_summary(activity),
                    "activity_payload": activity if debug else compact_dict({
                        "view": activity.get("view"),
                        "title": activity.get("title"),
                        "staff": activity.get("staff") or activity.get("identity"),
                        "window": activity.get("window"),
                        "counts": activity.get("counts"),
                        "summary": activity.get("summary"),
                    }),
                    "llm_context": llm_context if debug else {},
                    "llm_prompt": prompt if debug or not run_llm else None,
                    "review_text": review_text,
                    "review_window_days": days,
                    "review_generated_at": None,
                    "stale_at": None,
                }
            )
            store_staff_review(db, schema, cache_row)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return compact_dict(
            {
                "staff_key": staff_key,
                "user_source_id": staff.get("user_source_id"),
                "username": username,
                "email": email,
                "team": team,
                "status": status,
                "overall_score": parsed.get("overall_score"),
                "overall_priority_score": parsed.get("overall_priority_score"),
                "overall_risk": parsed.get("overall_risk"),
                "main_reason": parsed.get("main_reason"),
                "prompt_built": bool(prompt),
                "review_stored": True,
                "elapsed_ms": elapsed_ms,
                "error": error,
            }
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        error_text = f"{exc.__class__.__name__}: {exc}"
        try:
            with open_db_session(database_url) as db:
                store_staff_review(
                    db,
                    schema,
                    compact_dict(
                        {
                            "staff_key": staff_key,
                            "user_source_id": staff.get("user_source_id"),
                            "username": username,
                            "email": email,
                            "team": team,
                            "active": staff.get("active"),
                            "status": "error",
                            "error": error_text,
                            "model": model if run_llm else None,
                            "action_rows": [],
                            "stakeholder_scores": [],
                            "summary": {},
                            "activity_payload": {},
                            "llm_context": {},
                            "review_window_days": days,
                        }
                    ),
                )
        except Exception:
            pass
        return compact_dict(
            {
                "staff_key": staff_key,
                "user_source_id": staff.get("user_source_id"),
                "username": username,
                "email": email,
                "team": team,
                "status": "error",
                "error": error_text,
                "elapsed_ms": elapsed_ms,
            }
        )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run staff activity reviews for admin users.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--team", default=None, help="Optional team filter, e.g. Caretaker, Sales, Finance, Ops Team")
    parser.add_argument("--user-source-ids", default=None, help="Comma-separated admin_login_token.user_source_id values")
    parser.add_argument("--usernames", default=None, help="Comma-separated usernames")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--activity-limit", type=int, default=10000)
    parser.add_argument("--print-limit", type=int, default=80)
    parser.add_argument("--max-text", type=int, default=180)
    parser.add_argument("--display-mode", choices=["evidence", "llm", "raw"], default="llm")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--no-llm", action="store_true", help="Only build/store activity prompt/context; do not call LLM.")
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--include-suspended", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Store llm_context/llm_prompt/activity_payload for debugging. Can be large.")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    started = time.perf_counter()
    user_source_ids = [int(value) for value in _parse_csv(args.user_source_ids)] if args.user_source_ids else []
    usernames = _parse_csv(args.usernames)

    with open_db_session(args.database_url) as db:
        ensure_staff_review_cache_table(db, args.schema)
        staff_rows = select_admin_users(
            db,
            schema=args.schema,
            team=args.team,
            active_only=not args.include_inactive,
            include_deleted=args.include_deleted,
            include_suspended=args.include_suspended,
            usernames=usernames,
            user_source_ids=user_source_ids,
            limit=args.limit,
            offset=args.offset,
        )

    base_payload: dict[str, Any] = {
        "event": "staff_activity_review_run_start",
        "schema": args.schema,
        "team": args.team,
        "days": args.days,
        "limit": args.limit,
        "offset": args.offset,
        "selected_count": len(staff_rows),
        "workers": max(1, int(args.workers or 1)),
        "run_llm": not args.no_llm,
        "dry_run": bool(args.dry_run),
        "cache_table": f'{args.schema}.{STAFF_REVIEW_CACHE_TABLE}',
    }

    if args.dry_run:
        out = dict(base_payload)
        out["staff"] = [
            compact_dict(
                {
                    "staff_key": row.get("staff_key"),
                    "user_source_id": row.get("user_source_id"),
                    "username": row.get("username"),
                    "email": row.get("email"),
                    "team": row.get("team"),
                    "active": row.get("active"),
                }
            )
            for row in staff_rows
        ]
        print(json_dumps(out, pretty=args.pretty))
        return 0

    results: list[dict[str, Any]] = []
    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row in staff_rows:
            results.append(
                review_one_staff(
                    database_url=args.database_url,
                    schema=args.schema,
                    staff=row,
                    days=args.days,
                    limit=args.activity_limit,
                    print_limit=args.print_limit,
                    max_text=args.max_text,
                    model=args.model,
                    timeout_seconds=args.timeout_seconds,
                    run_llm=not args.no_llm,
                    display_mode=args.display_mode,
                    debug=args.debug,
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    review_one_staff,
                    database_url=args.database_url,
                    schema=args.schema,
                    staff=row,
                    days=args.days,
                    limit=args.activity_limit,
                    print_limit=args.print_limit,
                    max_text=args.max_text,
                    model=args.model,
                    timeout_seconds=args.timeout_seconds,
                    run_llm=not args.no_llm,
                    display_mode=args.display_mode,
                    debug=args.debug,
                ): row
                for row in staff_rows
            }
            for future in as_completed(future_map):
                results.append(future.result())

    ok_count = sum(1 for row in results if row.get("status") == "ok")
    prompt_only_count = sum(1 for row in results if row.get("status") == "prompt_built")
    error_count = sum(1 for row in results if row.get("status") == "error")
    elapsed = round(time.perf_counter() - started, 2)
    out = dict(base_payload)
    out.update(
        {
            "event": "staff_activity_review_run_complete",
            "ok_count": ok_count,
            "prompt_only_count": prompt_only_count,
            "error_count": error_count,
            "elapsed_seconds": elapsed,
            "results": results,
        }
    )
    print(json_dumps(out, pretty=args.pretty))
    return 1 if error_count and ok_count == 0 and prompt_only_count == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
