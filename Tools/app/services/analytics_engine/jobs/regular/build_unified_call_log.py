#!/usr/bin/env python3
"""
build_unified_call_log.py

Builds AnalyticsEngine.staging_call_log_unified from two independent call sources:
  1) RMS MySQL call_tracking_log
  2) recording/transcript Postgres public.call_recordings_transcript

Design goals:
  - Raw source tables are kept separately.
  - The final analytics table is one deterministic unified call table.
  - Either source can arrive first: RMS-only and recording-only rows are kept,
    then enriched/merged when the matching counterpart appears later.
  - Late transcript/audio updates are detected through a recent-id resync window
    and raw-hash comparison, so analytics can re-process changed unified rows by
    updated_at.

Recommended run before analytics processors:
  python -m app.services.analytics_engine.jobs.regular.build_unified_call_log --limit 50000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

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

try:
    from app.db.database import get_db  # type: ignore
except Exception:  # pragma: no cover
    get_db = None  # type: ignore

from app.services.analytics_engine.ingestion.source_db import (  # noqa: E402
    fetch_all,
    get_thirdparty_mysql_engine,
    get_thirdparty_pg_engine,
)
from app.services.analytics_engine.ingestion.sync_checkpoint_service import (  # noqa: E402
    StagingSyncCheckpointService,
)


DEFAULT_SCHEMA = os.getenv("SCHEMA_NAME", "AnalyticsEngine")
try:  # reset_raw_tables needs the fully-qualified checkpoint table name.
    from app.services.analytics_engine.core.config import STAGING_SYNC_CHECKPOINT  # type: ignore  # noqa: E402
except Exception:  # pragma: no cover - keeps the script runnable during partial imports
    STAGING_SYNC_CHECKPOINT = f'"{DEFAULT_SCHEMA}".staging_sync_checkpoint'

NON_DIGIT_RE = re.compile(r"\D+")
MATCH_WINDOW_SECONDS = 300
DURATION_TOLERANCE_SECONDS = 120
TIMEZONE_OFFSET_MATCH_SECONDS = int(os.getenv("CALL_LOG_TIMEZONE_OFFSET_MATCH_SECONDS", "19800"))
TIMEZONE_OFFSET_TOLERANCE_SECONDS = int(os.getenv("CALL_LOG_TIMEZONE_OFFSET_TOLERANCE_SECONDS", "5"))
TIMEZONE_OFFSET_DURATION_TOLERANCE_SECONDS = int(os.getenv("CALL_LOG_TIMEZONE_OFFSET_DURATION_TOLERANCE_SECONDS", "2"))
CALL_RECORDING_ASSUME_NAIVE_UTC = str(os.getenv("CALL_RECORDING_ASSUME_NAIVE_UTC", "1")).strip().lower() not in {"0", "false", "no", "n"}
DEFAULT_RESYNC_WINDOW = 5000
IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_PROGRESS_EVERY = 10000


# -----------------------------------------------------------------------------
# Timing / progress logging
# -----------------------------------------------------------------------------

def _utc_now_text() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _rate(count: Any, elapsed_sec: float) -> Optional[float]:
    try:
        numeric_count = float(count or 0)
    except Exception:
        return None
    if numeric_count <= 0 or elapsed_sec <= 0:
        return None
    return round(numeric_count / elapsed_sec, 2)


class TimingRecorder:
    """Lightweight timing collector.

    Logs go to stderr so normal stdout JSON output remains parseable.
    The same records are also returned in the final payload under `timing`.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.records: list[dict[str, Any]] = []
        self.started_at = time.perf_counter()

    def log(self, stage: str, elapsed_sec: float, **details: Any) -> dict[str, Any]:
        rows = details.get("rows") or details.get("processed") or details.get("fetched")
        record = compact_dict(
            {
                "at_utc": _utc_now_text(),
                "stage": stage,
                "elapsed_sec": round(float(elapsed_sec), 3),
                "rows_per_sec": _rate(rows, float(elapsed_sec)),
                **details,
            }
        )
        self.records.append(record)

        if self.enabled:
            parts = [f"stage={stage}", f"elapsed={record['elapsed_sec']}s"]
            for key in (
                "rows", "processed", "fetched", "total", "source", "last_id",
                "resync_from_id", "actions", "note",
            ):
                if key in record:
                    parts.append(f"{key}={record[key]}")
            if "rows_per_sec" in record:
                parts.append(f"rate={record['rows_per_sec']}/s")
            print("[timing] " + " ".join(parts), file=sys.stderr, flush=True)

        return record

    @contextmanager
    def stage(self, stage: str, **details: Any):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.log(stage, time.perf_counter() - started, **details)

    def total_elapsed(self) -> float:
        return time.perf_counter() - self.started_at



# -----------------------------------------------------------------------------
# DB/session helpers
# -----------------------------------------------------------------------------

def _try_load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


@contextmanager
def get_target_session(database_url: Optional[str] = None) -> Iterable[Session]:
    """Open target analytics DB safely.

    Do not catch exceptions thrown inside the `with` body. Catching those here
    can produce: RuntimeError: generator didn't stop after throw().
    """
    if database_url is None and get_db is not None:
        db_gen = None
        db = None
        try:
            db_gen = get_db()
            db = next(db_gen)
            db.execute(text("SELECT 1"))
        except Exception:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
            if db_gen is not None:
                try:
                    db_gen.close()
                except Exception:
                    pass
            db = None
            db_gen = None

        if db is not None:
            try:
                yield db
            finally:
                try:
                    db.close()
                except Exception:
                    pass
                if db_gen is not None:
                    try:
                        next(db_gen, None)
                    except Exception:
                        try:
                            db_gen.close()
                        except Exception:
                            pass
            return

    _try_load_env()
    db_url = database_url or os.getenv("DATABASE_URL") or os.getenv("PG_URL")
    if not db_url:
        raise RuntimeError("No target Postgres DB available. Run inside repo or set DATABASE_URL/PG_URL.")

    engine = create_engine(db_url, pool_pre_ping=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            engine.dispose()


# -----------------------------------------------------------------------------
# Normalization helpers
# -----------------------------------------------------------------------------

def _safe_ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(value or "")):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return str(value)


def _schema_ref(schema: str) -> str:
    return f'"{_safe_ident(schema)}"'


def _table_ref(schema: str, table_name: str) -> str:
    return f'{_schema_ref(schema)}.{_safe_ident(table_name)}'


def _digits(value: Any) -> str:
    return NON_DIGIT_RE.sub("", str(value or ""))


def norm_phone(value: Any) -> Optional[str]:
    digits = _digits(value)
    if not digits:
        return None
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits[-12:] if len(digits) >= 12 else digits


def phone10(value: Any) -> Optional[str]:
    normalized = norm_phone(value)
    return normalized[-10:] if normalized and len(normalized) >= 10 else None


def participant_pair_key(phone_a: Any, phone_b: Any) -> Optional[str]:
    a = phone10(phone_a)
    b = phone10(phone_b)
    if not a or not b:
        return None
    return "|".join(sorted([a, b]))


def to_naive_ist(value: Any, *, assume_naive_utc: bool = CALL_RECORDING_ASSUME_NAIVE_UTC) -> Any:
    """Normalize recording call timestamps to naive IST to match RMS callDate.

    The recording source has historically emitted `call_datetime` as a naive
    UTC-like timestamp. RMS `callDate` is local IST. Treating a naive recording
    timestamp as already-local creates duplicate unified rows exactly 5h30m
    apart. The default therefore assumes naive recording datetimes are UTC and
    converts them to IST. Set CALL_RECORDING_ASSUME_NAIVE_UTC=0 only if the
    upstream recording source is changed to emit local IST naive timestamps.
    """
    if value in (None, ""):
        return None
    parsed = value
    if not isinstance(parsed, datetime):
        text_value = str(value).strip()
        if not text_value:
            return None
        try:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except Exception:
            return value
    if parsed.tzinfo is not None:
        return parsed.astimezone(IST).replace(tzinfo=None)
    if assume_naive_utc:
        return parsed.replace(tzinfo=timezone.utc).astimezone(IST).replace(tzinfo=None)
    return parsed.replace(tzinfo=None) if isinstance(parsed, datetime) else parsed


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def safe_int(value: Any) -> Optional[int]:
    if value in (None, "", "NULL", "null", "NA", "N/A"):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.lstrip("-").isdigit():
        try:
            return int(text_value)
        except Exception:
            return None
    return None


def duration_to_seconds(value: Any) -> Optional[int]:
    if value in (None, "", "NULL", "null", "NA", "N/A"):
        return None
    if isinstance(value, int):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.lstrip("-").isdigit():
        return int(text_value)
    parts = text_value.split(":")
    if len(parts) in (2, 3) and all(part.strip().isdigit() for part in parts):
        nums = [int(part.strip()) for part in parts]
        if len(nums) == 2:
            minutes, seconds = nums
            return minutes * 60 + seconds
        hours, minutes, seconds = nums
        return hours * 3600 + minutes * 60 + seconds
    digits = _digits(text_value)
    return int(digits) if digits else None


def normalize_direction(value: Any) -> Optional[str]:
    text_value = str(value or "").strip().lower()
    if not text_value:
        return None
    if text_value in {"incoming", "inbound", "received", "receive", "in", "missed"}:
        return "incoming"
    if text_value in {"outgoing", "outbound", "dialed", "dial", "out", "sent"}:
        return "outgoing"
    return text_value[:30]


def derive_call_result(duration_value: Any, raw_status: Any = None) -> str:
    duration = duration_to_seconds(duration_value) or 0
    status = str(raw_status or "").strip().lower()
    if duration > 0:
        return "connected"
    if status in {"missed", "not connected", "not_connected", "busy", "no answer", "no_answer", "rejected", "failed"}:
        return "missed"
    if status in {"connected", "completed", "success", "answered"}:
        return "connected"
    return status[:30] if status else "missed"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def raw_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def datetime_second_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    parsed = value
    if not isinstance(parsed, datetime):
        text_value = str(value).strip()
        if not text_value:
            return None
        try:
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
        except Exception:
            return text_value[:19]
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.replace(tzinfo=None)
    parsed = parsed.replace(microsecond=0)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def build_dedupe_key(
    *,
    sales_phone: Any,
    counterparty_phone: Any,
    call_time: Any,
    talk_time_sec: Any,
    call_direction: Any = None,
) -> Optional[str]:
    """Stable same-source duplicate key.

    Requires phone pair + exact second timestamp + duration. This collapses
    duplicate RMS rows and duplicate recording rows even when recording call_id
    is missing/different, while avoiding broad over-merging.
    """
    sales10 = phone10(sales_phone)
    counter10 = phone10(counterparty_phone)
    time_text = datetime_second_text(call_time)
    duration = duration_to_seconds(talk_time_sec)
    if not sales10 or not counter10 or not time_text or duration is None:
        return None
    direction = normalize_direction(call_direction) or "-"
    return "|".join([sales10, counter10, time_text, str(duration), direction])


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v not in (None, "", [], {})}


# -----------------------------------------------------------------------------
# Unified builder
# -----------------------------------------------------------------------------

class UnifiedCallLogBuilder:
    def __init__(
        self,
        db: Session,
        *,
        schema: str = DEFAULT_SCHEMA,
        match_window_seconds: int = MATCH_WINDOW_SECONDS,
        timing_enabled: bool = True,
    ) -> None:
        self.db = db
        self.schema = schema
        self.match_window_seconds = int(match_window_seconds)
        self.checkpoint = StagingSyncCheckpointService(db)
        self.timing = TimingRecorder(enabled=timing_enabled)

    @property
    def rms_table(self) -> str:
        return _table_ref(self.schema, "staging_rms_call_log_tracking")

    @property
    def recording_raw_table(self) -> str:
        return _table_ref(self.schema, "staging_call_recordings_transcript_raw")

    @property
    def unified_table(self) -> str:
        return _table_ref(self.schema, "staging_call_log_unified")

    def ensure_tables(self, *, backfill_existing_keys: bool = True) -> None:
        schema_ref = _schema_ref(self.schema)
        self.db.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema_ref}'))

        self.db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {self.rms_table} (
            source_id BIGINT PRIMARY KEY,
            username TEXT,
            ph_num TEXT,
            call_time TIMESTAMP,
            source_timestamp TEXT,
            call_duration_raw TEXT,
            talk_time_sec INTEGER,
            sales_phone_number TEXT,
            call_type TEXT,
            raw_status TEXT,
            raw_message TEXT,
            lead_status INTEGER,
            lead_id BIGINT,
            added_on TIMESTAMP,
            executive_id TEXT,
            counterparty_phone TEXT,
            sales_phone TEXT,
            call_direction TEXT,
            call_result TEXT,
            raw_payload JSONB,
            raw_hash TEXT,
            dedupe_key TEXT,
            pair_key TEXT,
            synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        """))

        self.db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {self.recording_raw_table} (
            source_id BIGINT PRIMARY KEY,
            emp_phone_number TEXT,
            source_call_id TEXT,
            emp_name TEXT,
            customer_phone_number TEXT,
            call_time TIMESTAMP,
            talk_time_sec INTEGER,
            call_type TEXT,
            department TEXT,
            audio_url TEXT,
            transcript_text TEXT,
            filename TEXT,
            uploaded_at TIMESTAMP,
            source_status INTEGER,
            transcript_text_eleven_labs TEXT,
            raw_eleven_labs_transcript JSONB,
            sync_status INTEGER,
            distinct_cus_ph TEXT,
            raw_transcripts TEXT,
            translated_text TEXT,
            counterparty_phone TEXT,
            sales_phone TEXT,
            call_direction TEXT,
            call_result TEXT,
            raw_payload JSONB,
            raw_hash TEXT,
            dedupe_key TEXT,
            pair_key TEXT,
            synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        """))

        self.db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {self.unified_table} (
            source_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            rms_source_id BIGINT,
            recording_source_id BIGINT,
            source_call_id TEXT,
            dedupe_key TEXT,
            pair_key TEXT,
            executive_id TEXT,
            executive_name TEXT,
            call_time TIMESTAMP,
            talk_time_sec INTEGER,
            call_direction TEXT,
            call_result TEXT,
            counterparty_phone TEXT,
            sales_phone TEXT,
            lead_id BIGINT,
            department TEXT,
            audio_url TEXT,
            transcript_text TEXT,
            transcript_text_eleven_labs TEXT,
            translated_text TEXT,
            raw_transcripts TEXT,
            raw_eleven_labs_transcript JSONB,
            intent TEXT,
            emotion TEXT,
            tone TEXT,
            action_layer TEXT,
            context TEXT,
            outcome TEXT,
            language TEXT,
            priority TEXT,
            filename TEXT,
            uploaded_at TIMESTAMP,
            source_status INTEGER,
            sync_status INTEGER,
            match_status TEXT NOT NULL DEFAULT 'unknown',
            match_confidence TEXT,
            match_reason TEXT,
            rms_raw_payload JSONB,
            recording_raw_payload JSONB,
            rms_raw_hash TEXT,
            recording_raw_hash TEXT,
            duplicate_rms_source_ids BIGINT[] NOT NULL DEFAULT '{{}}',
            duplicate_recording_source_ids BIGINT[] NOT NULL DEFAULT '{{}}',
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            matched_at TIMESTAMP,
            synced_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        """))

        # Existing deployments may already have the tables; add new dedupe/audit
        # columns without requiring a manual migration.
        self.db.execute(text(f"ALTER TABLE {self.rms_table} ADD COLUMN IF NOT EXISTS dedupe_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.rms_table} ADD COLUMN IF NOT EXISTS pair_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.recording_raw_table} ADD COLUMN IF NOT EXISTS dedupe_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.recording_raw_table} ADD COLUMN IF NOT EXISTS pair_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.unified_table} ADD COLUMN IF NOT EXISTS dedupe_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.unified_table} ADD COLUMN IF NOT EXISTS pair_key TEXT"))
        self.db.execute(text(f"ALTER TABLE {self.unified_table} ADD COLUMN IF NOT EXISTS duplicate_rms_source_ids BIGINT[] NOT NULL DEFAULT '{{}}'"))
        self.db.execute(text(f"ALTER TABLE {self.unified_table} ADD COLUMN IF NOT EXISTS duplicate_recording_source_ids BIGINT[] NOT NULL DEFAULT '{{}}'"))
        self.db.execute(text(f"ALTER TABLE {self.unified_table} ADD COLUMN IF NOT EXISTS duplicate_count INTEGER NOT NULL DEFAULT 0"))
        if backfill_existing_keys:
            backfill_started = time.perf_counter()
            self._backfill_match_keys()
            self.timing.log("ensure_tables.backfill_match_keys", time.perf_counter() - backfill_started)

        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_rms_call_log_tracking_call_time ON {self.rms_table} (call_time)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_rms_call_log_tracking_phones ON {self.rms_table} (sales_phone, counterparty_phone)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_rms_call_log_tracking_dedupe_key ON {self.rms_table} (dedupe_key)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_rms_call_log_tracking_pair_time ON {self.rms_table} (pair_key, call_time) WHERE pair_key IS NOT NULL'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_recordings_transcript_raw_call_time ON {self.recording_raw_table} (call_time)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_recordings_transcript_raw_call_id ON {self.recording_raw_table} (source_call_id)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_recordings_transcript_raw_dedupe_key ON {self.recording_raw_table} (dedupe_key)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_recordings_transcript_raw_pair_time ON {self.recording_raw_table} (pair_key, call_time) WHERE pair_key IS NOT NULL'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_updated_at ON {self.unified_table} (COALESCE(updated_at, synced_at), source_id)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_call_time ON {self.unified_table} (call_time)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_phones ON {self.unified_table} (sales_phone, counterparty_phone)'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_dedupe_key ON {self.unified_table} (dedupe_key) WHERE dedupe_key IS NOT NULL'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_pair_time ON {self.unified_table} (pair_key, call_time) WHERE pair_key IS NOT NULL'))
        customer_phone10_expr = self._phone10_sql("counterparty_phone")
        self.db.execute(text(f'''
            CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_customer10_signature_time
            ON {self.unified_table} (({customer_phone10_expr}), call_direction, call_result, talk_time_sec, call_time)
            WHERE counterparty_phone IS NOT NULL AND call_time IS NOT NULL
        '''))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_pair_recording_open ON {self.unified_table} (pair_key, call_time) WHERE pair_key IS NOT NULL AND rms_source_id IS NULL AND recording_source_id IS NOT NULL'))
        self.db.execute(text(f'CREATE INDEX IF NOT EXISTS idx_staging_call_log_unified_pair_rms_open ON {self.unified_table} (pair_key, call_time) WHERE pair_key IS NOT NULL AND recording_source_id IS NULL AND rms_source_id IS NOT NULL'))
        self.db.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_staging_call_log_unified_rms_source_id ON {self.unified_table} (rms_source_id) WHERE rms_source_id IS NOT NULL'))
        self.db.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_staging_call_log_unified_recording_source_id ON {self.unified_table} (recording_source_id) WHERE recording_source_id IS NOT NULL'))
        self.db.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_staging_call_log_unified_source_call_id ON {self.unified_table} (source_call_id) WHERE source_call_id IS NOT NULL'))
        self.db.commit()

    def _phone10_sql(self, column_name: str) -> str:
        return f"RIGHT(REGEXP_REPLACE(COALESCE({column_name}, ''), '\\D', '', 'g'), 10)"

    def _pair_key_sql_expr(self) -> str:
        sales10 = self._phone10_sql("sales_phone")
        counter10 = self._phone10_sql("counterparty_phone")
        return f"""
        CASE
            WHEN {sales10} <> ''
             AND {counter10} <> ''
            THEN LEAST({sales10}, {counter10}) || '|' || GREATEST({sales10}, {counter10})
            ELSE NULL
        END
        """

    def _dedupe_key_sql_expr(self) -> str:
        sales10 = self._phone10_sql("sales_phone")
        counter10 = self._phone10_sql("counterparty_phone")
        return f"""
        CASE
            WHEN {sales10} <> ''
             AND {counter10} <> ''
             AND call_time IS NOT NULL
             AND talk_time_sec IS NOT NULL
            THEN
                {sales10}
                || '|' || {counter10}
                || '|' || TO_CHAR(DATE_TRUNC('second', call_time), 'YYYY-MM-DD HH24:MI:SS')
                || '|' || talk_time_sec::text
                || '|' || COALESCE(NULLIF(call_direction, ''), '-')
            ELSE NULL
        END
        """

    def _backfill_match_keys(self) -> None:
        pair_expr = self._pair_key_sql_expr()
        dedupe_expr = self._dedupe_key_sql_expr()
        for table_ref in (self.rms_table, self.recording_raw_table, self.unified_table):
            self.db.execute(text(f"""
            UPDATE {table_ref}
            SET pair_key = {pair_expr}
            WHERE pair_key IS NULL
              AND sales_phone IS NOT NULL
              AND counterparty_phone IS NOT NULL
            """))
            self.db.execute(text(f"""
            UPDATE {table_ref}
            SET dedupe_key = {dedupe_expr}
            WHERE dedupe_key IS NULL
              AND sales_phone IS NOT NULL
              AND counterparty_phone IS NOT NULL
              AND call_time IS NOT NULL
              AND talk_time_sec IS NOT NULL
            """))

    def _refresh_duplicate_counts(self) -> None:
        self.db.execute(text(f"""
        UPDATE {self.unified_table}
        SET duplicate_count =
            CARDINALITY(COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[]))
            + CARDINALITY(COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[]))
        WHERE duplicate_count IS DISTINCT FROM (
            CARDINALITY(COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[]))
            + CARDINALITY(COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[]))
        )
        """))

    def reset_unified_table(self) -> None:
        self.db.execute(text(f"TRUNCATE TABLE {self.unified_table} RESTART IDENTITY"))
        self.db.commit()

    def reset_raw_tables(self) -> None:
        self.db.execute(text(f"TRUNCATE TABLE {self.rms_table}, {self.recording_raw_table}, {self.unified_table} RESTART IDENTITY"))
        self.db.execute(
            text(
                f"""
                DELETE FROM {STAGING_SYNC_CHECKPOINT}
                WHERE sync_name IN ('rms_call_log_tracking_raw', 'call_recordings_transcript_raw')
                """
            )
        )
        self.db.commit()

    # ------------------------------------------------------------------
    # Raw syncs
    # ------------------------------------------------------------------
    def sync_rms_call_logs(self, *, limit: int, resync_window: int = DEFAULT_RESYNC_WINDOW) -> dict[str, Any]:
        sync_name = "rms_call_log_tracking_raw"
        checkpoint = self.checkpoint.get_checkpoint(sync_name)
        last_id = int(checkpoint.get("last_id") or 0)
        start_id = max(0, last_id - int(resync_window or 0))
        total_started = time.perf_counter()

        fetch_started = time.perf_counter()
        rows = fetch_all(
            get_thirdparty_mysql_engine(),
            """
            SELECT
                id,
                username,
                phNum,
                callDate,
                `timestamp` AS source_timestamp,
                callDuration,
                salesPhoneNumber,
                callType,
                status,
                message,
                lead_status,
                lead_id,
                added_on
            FROM call_tracking_log
            WHERE id > :start_id
            ORDER BY id
            LIMIT :limit
            """,
            {"start_id": start_id, "limit": int(limit)},
        )
        self.timing.log(
            "sync_rms.fetch_source",
            time.perf_counter() - fetch_started,
            source=sync_name,
            fetched=len(rows),
            last_id=last_id,
            resync_from_id=start_id,
        )

        if not rows:
            elapsed = time.perf_counter() - total_started
            self.timing.log("sync_rms.total", elapsed, source=sync_name, fetched=0)
            return {
                "source": sync_name,
                "fetched": 0,
                "last_id": last_id,
                "resync_from_id": start_id,
                "elapsed_sec": round(elapsed, 3),
            }

        normalize_started = time.perf_counter()
        payload: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            source_id = safe_int(raw.get("id"))
            row_payload = {
                "source_id": source_id,
                "username": clean_text(raw.get("username")),
                "ph_num": clean_text(raw.get("phNum")),
                "call_time": raw.get("callDate"),
                "source_timestamp": clean_text(raw.get("source_timestamp")),
                "call_duration_raw": clean_text(raw.get("callDuration")),
                "talk_time_sec": duration_to_seconds(raw.get("callDuration")),
                "sales_phone_number": clean_text(raw.get("salesPhoneNumber")),
                "call_type": clean_text(raw.get("callType")),
                "raw_status": clean_text(raw.get("status")),
                "raw_message": clean_text(raw.get("message")),
                "lead_status": safe_int(raw.get("lead_status")),
                "lead_id": safe_int(raw.get("lead_id")),
                "added_on": raw.get("added_on"),
                "executive_id": clean_text(raw.get("username")),
                "counterparty_phone": norm_phone(raw.get("phNum")),
                "sales_phone": norm_phone(raw.get("salesPhoneNumber")),
                "call_direction": normalize_direction(raw.get("callType")),
                "call_result": derive_call_result(raw.get("callDuration"), raw.get("status")),
                "raw_payload": json_dumps(raw),
            }
            row_payload["raw_hash"] = raw_hash(raw)
            row_payload["dedupe_key"] = build_dedupe_key(
                sales_phone=row_payload.get("sales_phone"),
                counterparty_phone=row_payload.get("counterparty_phone"),
                call_time=row_payload.get("call_time"),
                talk_time_sec=row_payload.get("talk_time_sec"),
                call_direction=row_payload.get("call_direction"),
            )
            row_payload["pair_key"] = participant_pair_key(
                row_payload.get("sales_phone"),
                row_payload.get("counterparty_phone"),
            )
            if source_id is not None:
                payload.append(row_payload)

        self.timing.log(
            "sync_rms.normalize_payload",
            time.perf_counter() - normalize_started,
            source=sync_name,
            rows=len(payload),
        )

        sql = text(f"""
        INSERT INTO {self.rms_table} (
            source_id, username, ph_num, call_time, source_timestamp,
            call_duration_raw, talk_time_sec, sales_phone_number, call_type,
            raw_status, raw_message, lead_status, lead_id, added_on,
            executive_id, counterparty_phone, sales_phone, call_direction,
            call_result, raw_payload, raw_hash, dedupe_key, pair_key, synced_at, updated_at
        ) VALUES (
            :source_id, :username, :ph_num, :call_time, :source_timestamp,
            :call_duration_raw, :talk_time_sec, :sales_phone_number, :call_type,
            :raw_status, :raw_message, :lead_status, :lead_id, :added_on,
            :executive_id, :counterparty_phone, :sales_phone, :call_direction,
            :call_result, CAST(:raw_payload AS JSONB), :raw_hash, :dedupe_key, :pair_key, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$), (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        ON CONFLICT (source_id) DO UPDATE SET
            username = EXCLUDED.username,
            ph_num = EXCLUDED.ph_num,
            call_time = EXCLUDED.call_time,
            source_timestamp = EXCLUDED.source_timestamp,
            call_duration_raw = EXCLUDED.call_duration_raw,
            talk_time_sec = EXCLUDED.talk_time_sec,
            sales_phone_number = EXCLUDED.sales_phone_number,
            call_type = EXCLUDED.call_type,
            raw_status = EXCLUDED.raw_status,
            raw_message = EXCLUDED.raw_message,
            lead_status = EXCLUDED.lead_status,
            lead_id = EXCLUDED.lead_id,
            added_on = EXCLUDED.added_on,
            executive_id = EXCLUDED.executive_id,
            counterparty_phone = EXCLUDED.counterparty_phone,
            sales_phone = EXCLUDED.sales_phone,
            call_direction = EXCLUDED.call_direction,
            call_result = EXCLUDED.call_result,
            raw_payload = EXCLUDED.raw_payload,
            raw_hash = EXCLUDED.raw_hash,
            dedupe_key = EXCLUDED.dedupe_key,
            pair_key = EXCLUDED.pair_key,
            synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at = CASE
                WHEN {self.rms_table}.raw_hash IS DISTINCT FROM EXCLUDED.raw_hash THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                ELSE {self.rms_table}.updated_at
            END
        """)
        upsert_started = time.perf_counter()
        self.db.execute(sql, payload)
        self.db.commit()
        self.timing.log(
            "sync_rms.upsert_target",
            time.perf_counter() - upsert_started,
            source=sync_name,
            rows=len(payload),
        )

        new_last_id = max(int(item["source_id"]) for item in payload)
        checkpoint_started = time.perf_counter()
        self.checkpoint.update_success(
            sync_name,
            last_id=max(last_id, new_last_id),
            batch_count=len(payload),
            notes=f"resync_from_id={start_id}",
        )
        self.timing.log(
            "sync_rms.checkpoint",
            time.perf_counter() - checkpoint_started,
            source=sync_name,
            last_id=max(last_id, new_last_id),
        )
        elapsed = time.perf_counter() - total_started
        self.timing.log("sync_rms.total", elapsed, source=sync_name, fetched=len(payload), last_id=max(last_id, new_last_id))
        return {
            "source": sync_name,
            "fetched": len(payload),
            "last_id": max(last_id, new_last_id),
            "resync_from_id": start_id,
            "elapsed_sec": round(elapsed, 3),
        }

    def sync_recordings_raw(self, *, limit: int, resync_window: int = DEFAULT_RESYNC_WINDOW) -> dict[str, Any]:
        sync_name = "call_recordings_transcript_raw"
        checkpoint = self.checkpoint.get_checkpoint(sync_name)
        last_id = int(checkpoint.get("last_id") or 0)
        start_id = max(0, last_id - int(resync_window or 0))
        total_started = time.perf_counter()

        fetch_started = time.perf_counter()
        rows = fetch_all(
            get_thirdparty_pg_engine(),
            """
            SELECT
                id,
                emp_phone_number,
                call_id,
                emp_name,
                customer_phone_number,
                call_datetime,
                call_duration,
                call_type,
                department,
                audio_url,
                transcript_text,
                filename,
                uploaded_at,
                status,
                transcript_text_eleven_labs,
                raw_eleven_labs_transcript,
                sync_status,
                distinct_cus_ph,
                raw_transcripts,
                translated_text
            FROM public.call_recordings_transcript
            WHERE id > :start_id
            ORDER BY id
            LIMIT :limit
            """,
            {"start_id": start_id, "limit": int(limit)},
        )
        self.timing.log(
            "sync_recordings.fetch_source",
            time.perf_counter() - fetch_started,
            source=sync_name,
            fetched=len(rows),
            last_id=last_id,
            resync_from_id=start_id,
        )

        if not rows:
            elapsed = time.perf_counter() - total_started
            self.timing.log("sync_recordings.total", elapsed, source=sync_name, fetched=0)
            return {
                "source": sync_name,
                "fetched": 0,
                "last_id": last_id,
                "resync_from_id": start_id,
                "elapsed_sec": round(elapsed, 3),
            }

        normalize_started = time.perf_counter()
        payload: list[dict[str, Any]] = []
        for row in rows:
            raw = dict(row)
            source_id = safe_int(raw.get("id"))
            duration = duration_to_seconds(raw.get("call_duration"))
            row_payload = {
                "source_id": source_id,
                "emp_phone_number": clean_text(raw.get("emp_phone_number")),
                "source_call_id": clean_text(raw.get("call_id")),
                "emp_name": clean_text(raw.get("emp_name")),
                "customer_phone_number": clean_text(raw.get("customer_phone_number")),
                "call_time": to_naive_ist(raw.get("call_datetime")),
                "talk_time_sec": duration,
                "call_type": clean_text(raw.get("call_type")),
                "department": clean_text(raw.get("department")),
                "audio_url": clean_text(raw.get("audio_url")),
                "transcript_text": clean_text(raw.get("transcript_text")),
                "filename": clean_text(raw.get("filename")),
                "uploaded_at": to_naive_ist(raw.get("uploaded_at")),
                "source_status": safe_int(raw.get("status")),
                "transcript_text_eleven_labs": clean_text(raw.get("transcript_text_eleven_labs")),
                "raw_eleven_labs_transcript": json_dumps(raw.get("raw_eleven_labs_transcript") or {}),
                "sync_status": safe_int(raw.get("sync_status")),
                "distinct_cus_ph": clean_text(raw.get("distinct_cus_ph")),
                "raw_transcripts": clean_text(raw.get("raw_transcripts")),
                "translated_text": clean_text(raw.get("translated_text")),
                "counterparty_phone": norm_phone(raw.get("customer_phone_number") or raw.get("distinct_cus_ph")),
                "sales_phone": norm_phone(raw.get("emp_phone_number")),
                "call_direction": normalize_direction(raw.get("call_type")),
                "call_result": derive_call_result(duration, raw.get("status")),
                "raw_payload": json_dumps(raw),
            }
            row_payload["raw_hash"] = raw_hash(raw)
            row_payload["dedupe_key"] = build_dedupe_key(
                sales_phone=row_payload.get("sales_phone"),
                counterparty_phone=row_payload.get("counterparty_phone"),
                call_time=row_payload.get("call_time"),
                talk_time_sec=row_payload.get("talk_time_sec"),
                call_direction=row_payload.get("call_direction"),
            )
            row_payload["pair_key"] = participant_pair_key(
                row_payload.get("sales_phone"),
                row_payload.get("counterparty_phone"),
            )
            if source_id is not None:
                payload.append(row_payload)

        self.timing.log(
            "sync_recordings.normalize_payload",
            time.perf_counter() - normalize_started,
            source=sync_name,
            rows=len(payload),
        )

        sql = text(f"""
        INSERT INTO {self.recording_raw_table} (
            source_id, emp_phone_number, source_call_id, emp_name,
            customer_phone_number, call_time, talk_time_sec, call_type,
            department, audio_url, transcript_text, filename, uploaded_at,
            source_status, transcript_text_eleven_labs, raw_eleven_labs_transcript,
            sync_status, distinct_cus_ph, raw_transcripts, translated_text,
            counterparty_phone, sales_phone, call_direction, call_result,
            raw_payload, raw_hash, dedupe_key, pair_key, synced_at, updated_at
        ) VALUES (
            :source_id, :emp_phone_number, :source_call_id, :emp_name,
            :customer_phone_number, :call_time, :talk_time_sec, :call_type,
            :department, :audio_url, :transcript_text, :filename, :uploaded_at,
            :source_status, :transcript_text_eleven_labs, CAST(:raw_eleven_labs_transcript AS JSONB),
            :sync_status, :distinct_cus_ph, :raw_transcripts, :translated_text,
            :counterparty_phone, :sales_phone, :call_direction, :call_result,
            CAST(:raw_payload AS JSONB), :raw_hash, :dedupe_key, :pair_key, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$), (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        ON CONFLICT (source_id) DO UPDATE SET
            emp_phone_number = EXCLUDED.emp_phone_number,
            source_call_id = EXCLUDED.source_call_id,
            emp_name = EXCLUDED.emp_name,
            customer_phone_number = EXCLUDED.customer_phone_number,
            call_time = EXCLUDED.call_time,
            talk_time_sec = EXCLUDED.talk_time_sec,
            call_type = EXCLUDED.call_type,
            department = EXCLUDED.department,
            audio_url = EXCLUDED.audio_url,
            transcript_text = EXCLUDED.transcript_text,
            filename = EXCLUDED.filename,
            uploaded_at = EXCLUDED.uploaded_at,
            source_status = EXCLUDED.source_status,
            transcript_text_eleven_labs = EXCLUDED.transcript_text_eleven_labs,
            raw_eleven_labs_transcript = EXCLUDED.raw_eleven_labs_transcript,
            sync_status = EXCLUDED.sync_status,
            distinct_cus_ph = EXCLUDED.distinct_cus_ph,
            raw_transcripts = EXCLUDED.raw_transcripts,
            translated_text = EXCLUDED.translated_text,
            counterparty_phone = EXCLUDED.counterparty_phone,
            sales_phone = EXCLUDED.sales_phone,
            call_direction = EXCLUDED.call_direction,
            call_result = EXCLUDED.call_result,
            raw_payload = EXCLUDED.raw_payload,
            raw_hash = EXCLUDED.raw_hash,
            dedupe_key = EXCLUDED.dedupe_key,
            pair_key = EXCLUDED.pair_key,
            synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at = CASE
                WHEN {self.recording_raw_table}.raw_hash IS DISTINCT FROM EXCLUDED.raw_hash THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                ELSE {self.recording_raw_table}.updated_at
            END
        """)
        upsert_started = time.perf_counter()
        self.db.execute(sql, payload)
        self.db.commit()
        self.timing.log(
            "sync_recordings.upsert_target",
            time.perf_counter() - upsert_started,
            source=sync_name,
            rows=len(payload),
        )

        new_last_id = max(int(item["source_id"]) for item in payload)
        checkpoint_started = time.perf_counter()
        self.checkpoint.update_success(
            sync_name,
            last_id=max(last_id, new_last_id),
            batch_count=len(payload),
            notes=f"resync_from_id={start_id}",
        )
        self.timing.log(
            "sync_recordings.checkpoint",
            time.perf_counter() - checkpoint_started,
            source=sync_name,
            last_id=max(last_id, new_last_id),
        )
        elapsed = time.perf_counter() - total_started
        self.timing.log("sync_recordings.total", elapsed, source=sync_name, fetched=len(payload), last_id=max(last_id, new_last_id))
        return {
            "source": sync_name,
            "fetched": len(payload),
            "last_id": max(last_id, new_last_id),
            "resync_from_id": start_id,
            "elapsed_sec": round(elapsed, 3),
        }

    # ------------------------------------------------------------------
    # Unified upsert/match
    # ------------------------------------------------------------------
    def _find_unified_for_rms(self, row: dict[str, Any]) -> tuple[Optional[int], Optional[str], Optional[str]]:
        existing = self.db.execute(
            text(f"SELECT source_id FROM {self.unified_table} WHERE rms_source_id = :source_id LIMIT 1"),
            {"source_id": row["source_id"]},
        ).mappings().fetchone()
        if existing:
            return int(existing["source_id"]), "existing_rms", "rms_source_id"

        dedupe_key = row.get("dedupe_key") or build_dedupe_key(
            sales_phone=row.get("sales_phone"),
            counterparty_phone=row.get("counterparty_phone"),
            call_time=row.get("call_time"),
            talk_time_sec=row.get("talk_time_sec"),
            call_direction=row.get("call_direction"),
        )
        if dedupe_key:
            candidate = self.db.execute(
                text(f"""
                SELECT source_id, rms_source_id, recording_source_id
                FROM {self.unified_table}
                WHERE dedupe_key = :dedupe_key
                ORDER BY
                    CASE WHEN rms_source_id = :source_id THEN 0 ELSE 1 END,
                    CASE WHEN recording_source_id IS NOT NULL THEN 0 ELSE 1 END,
                    source_id ASC
                LIMIT 1
                """),
                {"dedupe_key": dedupe_key, "source_id": row["source_id"]},
            ).mappings().fetchone()
            if candidate:
                if candidate.get("recording_source_id") is not None and candidate.get("rms_source_id") is None:
                    return int(candidate["source_id"]), "matched", "dedupe_key"
                return int(candidate["source_id"]), "duplicate_rms", "dedupe_key"

        pair_key = row.get("pair_key") or participant_pair_key(row.get("sales_phone"), row.get("counterparty_phone"))
        call_time = row.get("call_time")
        if not pair_key or call_time is None:
            return None, None, None

        candidate = self.db.execute(
            text(f"""
            SELECT source_id
            FROM {self.unified_table}
            WHERE rms_source_id IS NULL
              AND recording_source_id IS NOT NULL
              AND pair_key = :pair_key
              AND call_time >= CAST(:call_time AS timestamp) - (:window_seconds * INTERVAL '1 second')
              AND call_time <= CAST(:call_time AS timestamp) + (:window_seconds * INTERVAL '1 second')
              AND (talk_time_sec IS NULL OR :talk_time_sec IS NULL OR ABS(COALESCE(talk_time_sec, 0) - COALESCE(:talk_time_sec, 0)) <= :duration_tolerance)
            ORDER BY ABS(EXTRACT(EPOCH FROM (call_time - CAST(:call_time AS timestamp)))) ASC,
                     ABS(COALESCE(talk_time_sec, 0) - COALESCE(:talk_time_sec, 0)) ASC,
                     CASE WHEN transcript_text IS NOT NULL OR translated_text IS NOT NULL OR audio_url IS NOT NULL THEN 0 ELSE 1 END,
                     source_id ASC
            LIMIT 1
            """),
            {
                "pair_key": pair_key,
                "call_time": call_time,
                "talk_time_sec": row.get("talk_time_sec"),
                "window_seconds": self.match_window_seconds,
                "duration_tolerance": DURATION_TOLERANCE_SECONDS,
            },
        ).mappings().fetchone()
        if candidate:
            return int(candidate["source_id"]), "matched", "strong_signature_unordered_pair"
        return None, None, None

    def _find_unified_for_recording(self, row: dict[str, Any]) -> tuple[Optional[int], Optional[str], Optional[str]]:
        existing = self.db.execute(
            text(f"""
            SELECT source_id
            FROM {self.unified_table}
            WHERE recording_source_id = :source_id
               OR (:source_call_id IS NOT NULL AND source_call_id = :source_call_id)
            ORDER BY CASE WHEN recording_source_id = :source_id THEN 0 ELSE 1 END, source_id
            LIMIT 1
            """),
            {"source_id": row["source_id"], "source_call_id": row.get("source_call_id")},
        ).mappings().fetchone()
        if existing:
            return int(existing["source_id"]), "existing_recording", "recording_source_id_or_call_id"

        dedupe_key = row.get("dedupe_key") or build_dedupe_key(
            sales_phone=row.get("sales_phone"),
            counterparty_phone=row.get("counterparty_phone"),
            call_time=row.get("call_time"),
            talk_time_sec=row.get("talk_time_sec"),
            call_direction=row.get("call_direction"),
        )
        if dedupe_key:
            candidate = self.db.execute(
                text(f"""
                SELECT source_id, rms_source_id, recording_source_id
                FROM {self.unified_table}
                WHERE dedupe_key = :dedupe_key
                ORDER BY
                    CASE WHEN recording_source_id = :source_id THEN 0 ELSE 1 END,
                    CASE WHEN rms_source_id IS NOT NULL THEN 0 ELSE 1 END,
                    source_id ASC
                LIMIT 1
                """),
                {"dedupe_key": dedupe_key, "source_id": row["source_id"]},
            ).mappings().fetchone()
            if candidate:
                if candidate.get("rms_source_id") is not None and candidate.get("recording_source_id") is None:
                    return int(candidate["source_id"]), "matched", "dedupe_key"
                return int(candidate["source_id"]), "duplicate_recording", "dedupe_key"

        pair_key = row.get("pair_key") or participant_pair_key(row.get("sales_phone"), row.get("counterparty_phone"))
        call_time = row.get("call_time")
        if not pair_key or call_time is None:
            return None, None, None

        candidate = self.db.execute(
            text(f"""
            SELECT source_id
            FROM {self.unified_table}
            WHERE recording_source_id IS NULL
              AND rms_source_id IS NOT NULL
              AND pair_key = :pair_key
              AND call_time >= CAST(:call_time AS timestamp) - (:window_seconds * INTERVAL '1 second')
              AND call_time <= CAST(:call_time AS timestamp) + (:window_seconds * INTERVAL '1 second')
              AND (talk_time_sec IS NULL OR :talk_time_sec IS NULL OR ABS(COALESCE(talk_time_sec, 0) - COALESCE(:talk_time_sec, 0)) <= :duration_tolerance)
            ORDER BY ABS(EXTRACT(EPOCH FROM (call_time - CAST(:call_time AS timestamp)))) ASC,
                     ABS(COALESCE(talk_time_sec, 0) - COALESCE(:talk_time_sec, 0)) ASC,
                     source_id ASC
            LIMIT 1
            """),
            {
                "pair_key": pair_key,
                "call_time": call_time,
                "talk_time_sec": row.get("talk_time_sec"),
                "window_seconds": self.match_window_seconds,
                "duration_tolerance": DURATION_TOLERANCE_SECONDS,
            },
        ).mappings().fetchone()
        if candidate:
            return int(candidate["source_id"]), "matched", "strong_signature_unordered_pair"
        return None, None, None

    def _load_rms_rows(self, ids: Optional[list[int]] = None) -> list[dict[str, Any]]:
        if ids is not None and not ids:
            return []
        if ids is not None:
            rows = self.db.execute(
                text(f"SELECT * FROM {self.rms_table} WHERE source_id = ANY(:ids) ORDER BY source_id"),
                {"ids": ids},
            ).mappings().fetchall()
        else:
            rows = self.db.execute(text(f"SELECT * FROM {self.rms_table} ORDER BY source_id")).mappings().fetchall()
        return [dict(row) for row in rows]

    def _load_recording_rows(self, ids: Optional[list[int]] = None) -> list[dict[str, Any]]:
        if ids is not None and not ids:
            return []
        if ids is not None:
            rows = self.db.execute(
                text(f"SELECT * FROM {self.recording_raw_table} WHERE source_id = ANY(:ids) ORDER BY source_id"),
                {"ids": ids},
            ).mappings().fetchall()
        else:
            rows = self.db.execute(text(f"SELECT * FROM {self.recording_raw_table} ORDER BY source_id")).mappings().fetchall()
        return [dict(row) for row in rows]

    def upsert_rms_unified(self, row: dict[str, Any]) -> str:
        unified_id, match_status_hint, match_confidence = self._find_unified_for_rms(row)
        raw_payload = json_dumps(row.get("raw_payload") or {})
        params = {
            "unified_id": unified_id,
            "rms_source_id": row.get("source_id"),
            "executive_id": row.get("executive_id"),
            "call_time": row.get("call_time"),
            "talk_time_sec": row.get("talk_time_sec"),
            "call_direction": row.get("call_direction"),
            "call_result": row.get("call_result"),
            "counterparty_phone": row.get("counterparty_phone"),
            "sales_phone": row.get("sales_phone"),
            "lead_id": row.get("lead_id"),
            "dedupe_key": row.get("dedupe_key") or build_dedupe_key(
                sales_phone=row.get("sales_phone"),
                counterparty_phone=row.get("counterparty_phone"),
                call_time=row.get("call_time"),
                talk_time_sec=row.get("talk_time_sec"),
                call_direction=row.get("call_direction"),
            ),
            "pair_key": row.get("pair_key") or participant_pair_key(row.get("sales_phone"), row.get("counterparty_phone")),
            "source_status": safe_int(row.get("lead_status")),
            "raw_payload": raw_payload,
            "raw_hash": row.get("raw_hash"),
            "match_confidence": match_confidence or "source_only",
            "match_reason": match_status_hint or "rms_only",
        }

        if unified_id:
            self.db.execute(text(f"""
            UPDATE {self.unified_table}
            SET
                rms_source_id = COALESCE(rms_source_id, :rms_source_id),
                executive_id = COALESCE(:executive_id, executive_id),
                call_time = COALESCE(:call_time, call_time),
                talk_time_sec = COALESCE(:talk_time_sec, talk_time_sec),
                call_direction = COALESCE(:call_direction, call_direction),
                call_result = COALESCE(:call_result, call_result),
                counterparty_phone = COALESCE(:counterparty_phone, counterparty_phone),
                sales_phone = COALESCE(:sales_phone, sales_phone),
                lead_id = COALESCE(:lead_id, lead_id),
                source_status = COALESCE(:source_status, source_status),
                dedupe_key = COALESCE(dedupe_key, :dedupe_key),
                pair_key = COALESCE(pair_key, :pair_key),
                duplicate_rms_source_ids = CASE
                    WHEN :rms_source_id IS NOT NULL
                     AND rms_source_id IS NOT NULL
                     AND rms_source_id <> :rms_source_id
                     AND NOT COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[]) @> ARRAY[:rms_source_id]::BIGINT[]
                    THEN ARRAY_APPEND(COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[]), :rms_source_id)
                    ELSE COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[])
                END,
                duplicate_count = COALESCE(duplicate_count, 0) + CASE
                    WHEN :rms_source_id IS NOT NULL
                     AND rms_source_id IS NOT NULL
                     AND rms_source_id <> :rms_source_id
                     AND NOT COALESCE(duplicate_rms_source_ids, '{{}}'::BIGINT[]) @> ARRAY[:rms_source_id]::BIGINT[]
                    THEN 1 ELSE 0 END,
                rms_raw_payload = CAST(:raw_payload AS JSONB),
                rms_raw_hash = :raw_hash,
                match_status = CASE WHEN recording_source_id IS NOT NULL THEN 'matched' ELSE 'rms_only' END,
                match_confidence = CASE WHEN recording_source_id IS NOT NULL THEN :match_confidence ELSE COALESCE(match_confidence, :match_confidence, 'source_only') END,
                match_reason = CASE WHEN recording_source_id IS NOT NULL THEN :match_reason ELSE COALESCE(match_reason, :match_reason, 'rms_only') END,
                matched_at = CASE WHEN recording_source_id IS NOT NULL AND rms_source_id IS NULL THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$) ELSE matched_at END,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                updated_at = CASE
                    WHEN rms_raw_hash IS DISTINCT FROM :raw_hash OR rms_source_id IS NULL THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                    ELSE updated_at
                END
            WHERE source_id = :unified_id
            """), params)
            return "updated_matched" if match_status_hint == "matched" else "updated_existing"

        self.db.execute(text(f"""
        INSERT INTO {self.unified_table} (
            rms_source_id, executive_id, call_time, talk_time_sec, call_direction,
            call_result, counterparty_phone, sales_phone, lead_id, source_status,
            dedupe_key, pair_key, match_status, match_confidence, match_reason, rms_raw_payload,
            rms_raw_hash, synced_at, updated_at
        ) VALUES (
            :rms_source_id, :executive_id, :call_time, :talk_time_sec, :call_direction,
            :call_result, :counterparty_phone, :sales_phone, :lead_id, :source_status,
            :dedupe_key, :pair_key, 'rms_only', 'source_only', 'rms_only', CAST(:raw_payload AS JSONB),
            :raw_hash, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$), (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        """), params)
        return "inserted_rms_only"

    def upsert_recording_unified(self, row: dict[str, Any]) -> str:
        unified_id, match_status_hint, match_confidence = self._find_unified_for_recording(row)
        raw_payload = json_dumps(row.get("raw_payload") or {})
        params = {
            "unified_id": unified_id,
            "recording_source_id": row.get("source_id"),
            "source_call_id": row.get("source_call_id"),
            "executive_name": row.get("emp_name"),
            "call_time": row.get("call_time"),
            "talk_time_sec": row.get("talk_time_sec"),
            "call_direction": row.get("call_direction"),
            "call_result": row.get("call_result"),
            "counterparty_phone": row.get("counterparty_phone"),
            "sales_phone": row.get("sales_phone"),
            "dedupe_key": row.get("dedupe_key") or build_dedupe_key(
                sales_phone=row.get("sales_phone"),
                counterparty_phone=row.get("counterparty_phone"),
                call_time=row.get("call_time"),
                talk_time_sec=row.get("talk_time_sec"),
                call_direction=row.get("call_direction"),
            ),
            "pair_key": row.get("pair_key") or participant_pair_key(row.get("sales_phone"), row.get("counterparty_phone")),
            "department": row.get("department"),
            "audio_url": row.get("audio_url"),
            "transcript_text": row.get("transcript_text"),
            "transcript_text_eleven_labs": row.get("transcript_text_eleven_labs"),
            "translated_text": row.get("translated_text"),
            "raw_transcripts": row.get("raw_transcripts"),
            "raw_eleven_labs_transcript": json_dumps(row.get("raw_eleven_labs_transcript") or {}),
            "filename": row.get("filename"),
            "uploaded_at": row.get("uploaded_at"),
            "source_status": row.get("source_status"),
            "sync_status": row.get("sync_status"),
            "raw_payload": raw_payload,
            "raw_hash": row.get("raw_hash"),
            "match_confidence": match_confidence or "source_only",
            "match_reason": match_status_hint or "recording_only",
        }

        if unified_id:
            self.db.execute(text(f"""
            UPDATE {self.unified_table}
            SET
                recording_source_id = COALESCE(recording_source_id, :recording_source_id),
                source_call_id = COALESCE(:source_call_id, source_call_id),
                executive_name = COALESCE(:executive_name, executive_name),
                call_time = CASE
                    WHEN rms_source_id IS NULL THEN COALESCE(:call_time, call_time)
                    ELSE call_time
                END,
                talk_time_sec = COALESCE(talk_time_sec, :talk_time_sec),
                call_direction = COALESCE(call_direction, :call_direction),
                call_result = COALESCE(call_result, :call_result),
                counterparty_phone = COALESCE(counterparty_phone, :counterparty_phone),
                sales_phone = COALESCE(sales_phone, :sales_phone),
                department = COALESCE(:department, department),
                audio_url = COALESCE(:audio_url, audio_url),
                transcript_text = COALESCE(:transcript_text, transcript_text),
                transcript_text_eleven_labs = COALESCE(:transcript_text_eleven_labs, transcript_text_eleven_labs),
                translated_text = COALESCE(:translated_text, translated_text),
                raw_transcripts = COALESCE(:raw_transcripts, raw_transcripts),
                raw_eleven_labs_transcript = CAST(:raw_eleven_labs_transcript AS JSONB),
                filename = COALESCE(:filename, filename),
                uploaded_at = COALESCE(:uploaded_at, uploaded_at),
                source_status = COALESCE(:source_status, source_status),
                sync_status = COALESCE(:sync_status, sync_status),
                dedupe_key = COALESCE(dedupe_key, :dedupe_key),
                pair_key = COALESCE(pair_key, :pair_key),
                duplicate_recording_source_ids = CASE
                    WHEN :recording_source_id IS NOT NULL
                     AND recording_source_id IS NOT NULL
                     AND recording_source_id <> :recording_source_id
                     AND NOT COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[]) @> ARRAY[:recording_source_id]::BIGINT[]
                    THEN ARRAY_APPEND(COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[]), :recording_source_id)
                    ELSE COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[])
                END,
                duplicate_count = COALESCE(duplicate_count, 0) + CASE
                    WHEN :recording_source_id IS NOT NULL
                     AND recording_source_id IS NOT NULL
                     AND recording_source_id <> :recording_source_id
                     AND NOT COALESCE(duplicate_recording_source_ids, '{{}}'::BIGINT[]) @> ARRAY[:recording_source_id]::BIGINT[]
                    THEN 1 ELSE 0 END,
                recording_raw_payload = CAST(:raw_payload AS JSONB),
                recording_raw_hash = :raw_hash,
                match_status = CASE WHEN rms_source_id IS NOT NULL THEN 'matched' ELSE 'recording_only' END,
                match_confidence = CASE WHEN rms_source_id IS NOT NULL THEN :match_confidence ELSE COALESCE(match_confidence, :match_confidence, 'source_only') END,
                match_reason = CASE WHEN rms_source_id IS NOT NULL THEN :match_reason ELSE COALESCE(match_reason, :match_reason, 'recording_only') END,
                matched_at = CASE WHEN rms_source_id IS NOT NULL AND recording_source_id IS NULL THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$) ELSE matched_at END,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                updated_at = CASE
                    WHEN recording_raw_hash IS DISTINCT FROM :raw_hash
                      OR recording_source_id IS NULL
                      OR (rms_source_id IS NULL AND call_time IS DISTINCT FROM :call_time)
                    THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                    ELSE updated_at
                END
            WHERE source_id = :unified_id
            """), params)
            return "updated_matched" if match_status_hint == "matched" else "updated_existing"

        self.db.execute(text(f"""
        INSERT INTO {self.unified_table} (
            recording_source_id, source_call_id, executive_name, call_time, talk_time_sec,
            call_direction, call_result, counterparty_phone, sales_phone, department,
            audio_url, transcript_text, transcript_text_eleven_labs, translated_text,
            raw_transcripts, raw_eleven_labs_transcript, filename, uploaded_at,
            source_status, sync_status, dedupe_key, pair_key, match_status, match_confidence, match_reason,
            recording_raw_payload, recording_raw_hash, synced_at, updated_at
        ) VALUES (
            :recording_source_id, :source_call_id, :executive_name, :call_time, :talk_time_sec,
            :call_direction, :call_result, :counterparty_phone, :sales_phone, :department,
            :audio_url, :transcript_text, :transcript_text_eleven_labs, :translated_text,
            :raw_transcripts, CAST(:raw_eleven_labs_transcript AS JSONB), :filename, :uploaded_at,
            :source_status, :sync_status, :dedupe_key, :pair_key, 'recording_only', 'source_only', 'recording_only',
            CAST(:raw_payload AS JSONB), :raw_hash, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$), (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        )
        """), params)
        return "inserted_recording_only"

    def _source_scope_sql(
        self,
        *,
        alias: str,
        ids: Optional[list[int]] = None,
        min_source_id: Optional[int] = None,
        rebuild_all: bool = False,
        param_prefix: str,
    ) -> tuple[str, dict[str, Any]]:
        """Build a source-row scope without loading large batches into Python."""
        if rebuild_all:
            return "", {}
        if ids is not None:
            if not ids:
                return " AND FALSE", {}
            return f" AND {alias}.source_id = ANY(:{param_prefix}_ids)", {f"{param_prefix}_ids": [int(x) for x in ids]}
        if min_source_id is not None:
            return f" AND {alias}.source_id > :{param_prefix}_min_source_id", {f"{param_prefix}_min_source_id": int(min_source_id)}
        return " AND FALSE", {}

    def bulk_upsert_rms_unified(
        self,
        *,
        ids: Optional[list[int]] = None,
        min_source_id: Optional[int] = None,
        rebuild_all: bool = False,
    ) -> dict[str, int]:
        """Bulk append/update RMS rows into unified without row-by-row matching.

        This is the default high-throughput path. Matching/enrichment is handled
        by match_unified_rows() as a separate set-based phase.
        """
        where_sql, params = self._source_scope_sql(
            alias="r",
            ids=ids,
            min_source_id=min_source_id,
            rebuild_all=rebuild_all,
            param_prefix="rms",
        )
        if "FALSE" in where_sql:
            return {"rms_rows_considered": 0, "inserted_rms_only": 0, "updated_existing_rms": 0}

        row = self.db.execute(text(f"""
        WITH src AS MATERIALIZED (
            SELECT r.*
            FROM {self.rms_table} r
            WHERE 1 = 1
            {where_sql}
        ), selected AS (
            SELECT COUNT(*)::int AS count FROM src
        ), updated AS (
            UPDATE {self.unified_table} u
            SET
                executive_id = COALESCE(src.executive_id, u.executive_id),
                call_time = COALESCE(src.call_time, u.call_time),
                talk_time_sec = COALESCE(src.talk_time_sec, u.talk_time_sec),
                call_direction = COALESCE(src.call_direction, u.call_direction),
                call_result = COALESCE(src.call_result, u.call_result),
                counterparty_phone = COALESCE(src.counterparty_phone, u.counterparty_phone),
                sales_phone = COALESCE(src.sales_phone, u.sales_phone),
                lead_id = COALESCE(src.lead_id, u.lead_id),
                source_status = COALESCE(src.lead_status, u.source_status),
                dedupe_key = COALESCE(u.dedupe_key, src.dedupe_key),
                pair_key = COALESCE(u.pair_key, src.pair_key),
                rms_raw_payload = src.raw_payload,
                rms_raw_hash = src.raw_hash,
                match_status = CASE WHEN u.recording_source_id IS NOT NULL THEN 'matched' ELSE 'rms_only' END,
                match_confidence = CASE WHEN u.recording_source_id IS NOT NULL THEN COALESCE(u.match_confidence, 'source_only') ELSE 'source_only' END,
                match_reason = CASE WHEN u.recording_source_id IS NOT NULL THEN COALESCE(u.match_reason, 'existing_rms') ELSE 'rms_only' END,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                updated_at = CASE
                    WHEN u.rms_raw_hash IS DISTINCT FROM src.raw_hash OR u.rms_source_id IS NULL THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                    ELSE u.updated_at
                END
            FROM src
            WHERE u.rms_source_id = src.source_id
            RETURNING u.source_id
        ), inserted AS (
            INSERT INTO {self.unified_table} (
                rms_source_id,
                executive_id,
                call_time,
                talk_time_sec,
                call_direction,
                call_result,
                counterparty_phone,
                sales_phone,
                lead_id,
                source_status,
                dedupe_key,
                pair_key,
                match_status,
                match_confidence,
                match_reason,
                rms_raw_payload,
                rms_raw_hash,
                synced_at,
                updated_at
            )
            SELECT
                src.source_id,
                src.executive_id,
                src.call_time,
                src.talk_time_sec,
                src.call_direction,
                src.call_result,
                src.counterparty_phone,
                src.sales_phone,
                src.lead_id,
                src.lead_status,
                src.dedupe_key,
                src.pair_key,
                'rms_only',
                'source_only',
                'rms_only',
                src.raw_payload,
                src.raw_hash,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            FROM src
            WHERE NOT EXISTS (
                SELECT 1
                FROM {self.unified_table} u
                WHERE u.rms_source_id = src.source_id
            )
            ON CONFLICT DO NOTHING
            RETURNING source_id
        )
        SELECT
            (SELECT count FROM selected) AS rms_rows_considered,
            (SELECT COUNT(*)::int FROM inserted) AS inserted_rms_only,
            (SELECT COUNT(*)::int FROM updated) AS updated_existing_rms
        """), params).mappings().fetchone()
        return dict(row or {})

    def bulk_upsert_recording_unified(
        self,
        *,
        ids: Optional[list[int]] = None,
        min_source_id: Optional[int] = None,
        rebuild_all: bool = False,
    ) -> dict[str, int]:
        """Bulk append/update recording rows into unified without row-by-row matching."""
        where_sql, params = self._source_scope_sql(
            alias="r",
            ids=ids,
            min_source_id=min_source_id,
            rebuild_all=rebuild_all,
            param_prefix="recording",
        )
        if "FALSE" in where_sql:
            return {"recording_rows_considered": 0, "inserted_recording_only": 0, "updated_existing_recording": 0}

        row = self.db.execute(text(f"""
        WITH src AS MATERIALIZED (
            SELECT r.*
            FROM {self.recording_raw_table} r
            WHERE 1 = 1
            {where_sql}
        ), selected AS (
            SELECT COUNT(*)::int AS count FROM src
        ), updated AS (
            UPDATE {self.unified_table} u
            SET
                recording_source_id = COALESCE(u.recording_source_id, src.source_id),
                source_call_id = COALESCE(src.source_call_id, u.source_call_id),
                executive_name = COALESCE(src.emp_name, u.executive_name),
                call_time = CASE
                    WHEN u.rms_source_id IS NULL THEN COALESCE(src.call_time, u.call_time)
                    ELSE u.call_time
                END,
                talk_time_sec = COALESCE(u.talk_time_sec, src.talk_time_sec),
                call_direction = COALESCE(u.call_direction, src.call_direction),
                call_result = COALESCE(u.call_result, src.call_result),
                counterparty_phone = COALESCE(u.counterparty_phone, src.counterparty_phone),
                sales_phone = COALESCE(u.sales_phone, src.sales_phone),
                department = COALESCE(src.department, u.department),
                audio_url = COALESCE(src.audio_url, u.audio_url),
                transcript_text = COALESCE(src.transcript_text, u.transcript_text),
                transcript_text_eleven_labs = COALESCE(src.transcript_text_eleven_labs, u.transcript_text_eleven_labs),
                translated_text = COALESCE(src.translated_text, u.translated_text),
                raw_transcripts = COALESCE(src.raw_transcripts, u.raw_transcripts),
                raw_eleven_labs_transcript = COALESCE(src.raw_eleven_labs_transcript, u.raw_eleven_labs_transcript),
                filename = COALESCE(src.filename, u.filename),
                uploaded_at = COALESCE(src.uploaded_at, u.uploaded_at),
                source_status = COALESCE(src.source_status, u.source_status),
                sync_status = COALESCE(src.sync_status, u.sync_status),
                dedupe_key = COALESCE(u.dedupe_key, src.dedupe_key),
                pair_key = COALESCE(u.pair_key, src.pair_key),
                duplicate_recording_source_ids = CASE
                    WHEN u.recording_source_id IS NOT NULL
                     AND u.recording_source_id <> src.source_id
                     AND NOT COALESCE(u.duplicate_recording_source_ids, '{{}}'::BIGINT[]) @> ARRAY[src.source_id]::BIGINT[]
                    THEN ARRAY_APPEND(COALESCE(u.duplicate_recording_source_ids, '{{}}'::BIGINT[]), src.source_id)
                    ELSE COALESCE(u.duplicate_recording_source_ids, '{{}}'::BIGINT[])
                END,
                recording_raw_payload = src.raw_payload,
                recording_raw_hash = src.raw_hash,
                match_status = CASE WHEN u.rms_source_id IS NOT NULL THEN 'matched' ELSE 'recording_only' END,
                match_confidence = CASE WHEN u.rms_source_id IS NOT NULL THEN COALESCE(u.match_confidence, 'source_only') ELSE 'source_only' END,
                match_reason = CASE WHEN u.rms_source_id IS NOT NULL THEN COALESCE(u.match_reason, 'existing_recording') ELSE 'recording_only' END,
                synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                updated_at = CASE
                    WHEN u.recording_raw_hash IS DISTINCT FROM src.raw_hash
                      OR u.recording_source_id IS NULL
                      OR (u.rms_source_id IS NULL AND u.call_time IS DISTINCT FROM src.call_time)
                    THEN (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
                    ELSE u.updated_at
                END
            FROM src
            WHERE u.recording_source_id = src.source_id
               OR (src.source_call_id IS NOT NULL AND u.source_call_id = src.source_call_id)
            RETURNING u.source_id
        ), inserted AS (
            INSERT INTO {self.unified_table} (
                recording_source_id,
                source_call_id,
                executive_name,
                call_time,
                talk_time_sec,
                call_direction,
                call_result,
                counterparty_phone,
                sales_phone,
                department,
                audio_url,
                transcript_text,
                transcript_text_eleven_labs,
                translated_text,
                raw_transcripts,
                raw_eleven_labs_transcript,
                filename,
                uploaded_at,
                source_status,
                sync_status,
                dedupe_key,
                pair_key,
                match_status,
                match_confidence,
                match_reason,
                recording_raw_payload,
                recording_raw_hash,
                synced_at,
                updated_at
            )
            SELECT
                src.source_id,
                src.source_call_id,
                src.emp_name,
                src.call_time,
                src.talk_time_sec,
                src.call_direction,
                src.call_result,
                src.counterparty_phone,
                src.sales_phone,
                src.department,
                src.audio_url,
                src.transcript_text,
                src.transcript_text_eleven_labs,
                src.translated_text,
                src.raw_transcripts,
                src.raw_eleven_labs_transcript,
                src.filename,
                src.uploaded_at,
                src.source_status,
                src.sync_status,
                src.dedupe_key,
                src.pair_key,
                'recording_only',
                'source_only',
                'recording_only',
                src.raw_payload,
                src.raw_hash,
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
                (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
            FROM src
            WHERE NOT EXISTS (
                SELECT 1
                FROM {self.unified_table} u
                WHERE u.recording_source_id = src.source_id
                   OR (src.source_call_id IS NOT NULL AND u.source_call_id = src.source_call_id)
            )
            ON CONFLICT DO NOTHING
            RETURNING source_id
        )
        SELECT
            (SELECT count FROM selected) AS recording_rows_considered,
            (SELECT COUNT(*)::int FROM inserted) AS inserted_recording_only,
            (SELECT COUNT(*)::int FROM updated) AS updated_existing_recording
        """), params).mappings().fetchone()
        return dict(row or {})

    def _match_scope_sql(
        self,
        *,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        match_all: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        if match_all:
            return "", {}
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if rms_min_source_id is not None:
            clauses.append("rms.rms_source_id > :match_rms_min_source_id")
            params["match_rms_min_source_id"] = int(rms_min_source_id)
        if recording_min_source_id is not None:
            clauses.append("rec.recording_source_id > :match_recording_min_source_id")
            params["match_recording_min_source_id"] = int(recording_min_source_id)
        if not clauses:
            return " AND FALSE", {}
        return " AND (" + " OR ".join(clauses) + ")", params

    def _merge_match_table(self, *, temp_table: str, match_confidence: str, match_reason: str) -> int:
        row = self.db.execute(text(f"SELECT COUNT(*)::int AS count FROM {temp_table}")).mappings().fetchone()
        match_count = int(row["count"] if row else 0)
        if match_count <= 0:
            return 0

        # Delete the recording-only row first so the partial unique index on
        # recording_source_id is free before the RMS canonical row is enriched.
        self.db.execute(text(f"""
        DELETE FROM {self.unified_table} rec
        USING {temp_table} m
        WHERE rec.source_id = m.recording_unified_id
        """))

        self.db.execute(text(f"""
        UPDATE {self.unified_table} rms
        SET
            recording_source_id = m.recording_source_id,
            source_call_id = COALESCE(m.source_call_id, rms.source_call_id),
            executive_name = COALESCE(m.executive_name, rms.executive_name),
            department = COALESCE(m.department, rms.department),
            audio_url = COALESCE(m.audio_url, rms.audio_url),
            transcript_text = COALESCE(m.transcript_text, rms.transcript_text),
            transcript_text_eleven_labs = COALESCE(m.transcript_text_eleven_labs, rms.transcript_text_eleven_labs),
            translated_text = COALESCE(m.translated_text, rms.translated_text),
            raw_transcripts = COALESCE(m.raw_transcripts, rms.raw_transcripts),
            raw_eleven_labs_transcript = COALESCE(m.raw_eleven_labs_transcript, rms.raw_eleven_labs_transcript),
            filename = COALESCE(m.filename, rms.filename),
            uploaded_at = COALESCE(m.uploaded_at, rms.uploaded_at),
            sync_status = COALESCE(m.sync_status, rms.sync_status),
            source_status = COALESCE(rms.source_status, m.source_status),
            recording_raw_payload = m.recording_raw_payload,
            recording_raw_hash = m.recording_raw_hash,
            dedupe_key = COALESCE(rms.dedupe_key, m.dedupe_key),
            pair_key = COALESCE(rms.pair_key, m.pair_key),
            match_status = 'matched',
            match_confidence = :match_confidence,
            match_reason = :match_reason,
            matched_at = COALESCE(rms.matched_at, (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)),
            synced_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$),
            updated_at = (CURRENT_TIMESTAMP AT TIME ZONE $$Asia/Kolkata$$)
        FROM {temp_table} m
        WHERE rms.source_id = m.rms_unified_id
        """), {"match_confidence": match_confidence, "match_reason": match_reason})
        return match_count

    def _create_exact_match_table(
        self,
        *,
        temp_table: str,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        match_all: bool = False,
    ) -> None:
        scope_sql, params = self._match_scope_sql(
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        self.db.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
        self.db.execute(text(f"""
        CREATE TEMP TABLE {temp_table} ON COMMIT DROP AS
        WITH candidates AS (
            SELECT
                rms.source_id AS rms_unified_id,
                rec.source_id AS recording_unified_id,
                rec.recording_source_id,
                rec.source_call_id,
                rec.executive_name,
                rec.department,
                rec.audio_url,
                rec.transcript_text,
                rec.transcript_text_eleven_labs,
                rec.translated_text,
                rec.raw_transcripts,
                rec.raw_eleven_labs_transcript,
                rec.filename,
                rec.uploaded_at,
                rec.source_status,
                rec.sync_status,
                rec.recording_raw_payload,
                rec.recording_raw_hash,
                rec.dedupe_key,
                rec.pair_key,
                ROW_NUMBER() OVER (PARTITION BY rms.source_id ORDER BY rec.source_id) AS rms_rank,
                ROW_NUMBER() OVER (PARTITION BY rec.source_id ORDER BY rms.source_id) AS rec_rank
            FROM {self.unified_table} rms
            JOIN {self.unified_table} rec
              ON rec.dedupe_key = rms.dedupe_key
            WHERE rms.rms_source_id IS NOT NULL
              AND rms.recording_source_id IS NULL
              AND rec.recording_source_id IS NOT NULL
              AND rec.rms_source_id IS NULL
              AND rms.dedupe_key IS NOT NULL
              {scope_sql}
        )
        SELECT *
        FROM candidates
        WHERE rms_rank = 1
          AND rec_rank = 1
        """), params)

    def _create_window_match_table(
        self,
        *,
        temp_table: str,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        match_all: bool = False,
    ) -> None:
        scope_sql, params = self._match_scope_sql(
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        params = {
            **params,
            "window_seconds": self.match_window_seconds,
            "duration_tolerance": DURATION_TOLERANCE_SECONDS,
        }
        self.db.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
        self.db.execute(text(f"""
        CREATE TEMP TABLE {temp_table} ON COMMIT DROP AS
        WITH candidates AS (
            SELECT
                rms.source_id AS rms_unified_id,
                rec.source_id AS recording_unified_id,
                rec.recording_source_id,
                rec.source_call_id,
                rec.executive_name,
                rec.department,
                rec.audio_url,
                rec.transcript_text,
                rec.transcript_text_eleven_labs,
                rec.translated_text,
                rec.raw_transcripts,
                rec.raw_eleven_labs_transcript,
                rec.filename,
                rec.uploaded_at,
                rec.source_status,
                rec.sync_status,
                rec.recording_raw_payload,
                rec.recording_raw_hash,
                rec.dedupe_key,
                rec.pair_key,
                ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time))) AS time_gap_sec,
                ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) AS duration_gap_sec,
                ROW_NUMBER() OVER (
                    PARTITION BY rms.source_id
                    ORDER BY
                        ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time))) ASC,
                        ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) ASC,
                        rec.source_id ASC
                ) AS rms_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY rec.source_id
                    ORDER BY
                        ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time))) ASC,
                        ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) ASC,
                        rms.source_id ASC
                ) AS rec_rank
            FROM {self.unified_table} rms
            JOIN {self.unified_table} rec
              ON rec.pair_key = rms.pair_key
             AND rec.call_time >= rms.call_time - (:window_seconds * INTERVAL '1 second')
             AND rec.call_time <= rms.call_time + (:window_seconds * INTERVAL '1 second')
             AND (rms.talk_time_sec IS NULL OR rec.talk_time_sec IS NULL OR ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) <= :duration_tolerance)
            WHERE rms.rms_source_id IS NOT NULL
              AND rms.recording_source_id IS NULL
              AND rec.recording_source_id IS NOT NULL
              AND rec.rms_source_id IS NULL
              AND rms.pair_key IS NOT NULL
              AND rms.call_time IS NOT NULL
              AND rec.call_time IS NOT NULL
              {scope_sql}
        )
        SELECT *
        FROM candidates
        WHERE rms_rank = 1
          AND rec_rank = 1
        """), params)

    def _create_timezone_offset_match_table(
        self,
        *,
        temp_table: str,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        match_all: bool = False,
    ) -> None:
        """Match legacy rows where recording time was stored as naive UTC.

        Existing bad unified data can contain one RMS row at local IST time and
        one recording-only row exactly 5h30m earlier. This pass links those rows
        without requiring sales_phone to match, because office/pooled numbers can
        differ between RMS metadata and the recorder metadata. It still requires
        the same customer phone, direction, result, duration, and offset window.
        """
        scope_sql, params = self._match_scope_sql(
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        params = {
            **params,
            "timezone_offset_seconds": TIMEZONE_OFFSET_MATCH_SECONDS,
            "timezone_offset_tolerance_seconds": TIMEZONE_OFFSET_TOLERANCE_SECONDS,
            "duration_tolerance": TIMEZONE_OFFSET_DURATION_TOLERANCE_SECONDS,
        }
        rms_customer10 = self._phone10_sql("rms.counterparty_phone")
        rec_customer10 = self._phone10_sql("rec.counterparty_phone")
        rms_sales10 = self._phone10_sql("rms.sales_phone")
        rec_sales10 = self._phone10_sql("rec.sales_phone")

        self.db.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
        self.db.execute(text(f"""
        CREATE TEMP TABLE {temp_table} ON COMMIT DROP AS
        WITH candidates AS (
            SELECT
                rms.source_id AS rms_unified_id,
                rec.source_id AS recording_unified_id,
                rec.recording_source_id,
                rec.source_call_id,
                rec.executive_name,
                rec.department,
                rec.audio_url,
                rec.transcript_text,
                rec.transcript_text_eleven_labs,
                rec.translated_text,
                rec.raw_transcripts,
                rec.raw_eleven_labs_transcript,
                rec.filename,
                rec.uploaded_at,
                rec.source_status,
                rec.sync_status,
                rec.recording_raw_payload,
                rec.recording_raw_hash,
                rec.dedupe_key,
                rec.pair_key,
                ABS(EXTRACT(EPOCH FROM (rms.call_time - (rec.call_time + (:timezone_offset_seconds * INTERVAL '1 second'))))) AS offset_gap_sec,
                ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) AS duration_gap_sec,
                CASE WHEN {rms_sales10} <> '' AND {rms_sales10} = {rec_sales10} THEN 0 ELSE 1 END AS sales_phone_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY rms.source_id
                    ORDER BY
                        ABS(EXTRACT(EPOCH FROM (rms.call_time - (rec.call_time + (:timezone_offset_seconds * INTERVAL '1 second'))))) ASC,
                        ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) ASC,
                        CASE WHEN {rms_sales10} <> '' AND {rms_sales10} = {rec_sales10} THEN 0 ELSE 1 END ASC,
                        CASE WHEN rec.transcript_text IS NOT NULL OR rec.translated_text IS NOT NULL OR rec.audio_url IS NOT NULL THEN 0 ELSE 1 END ASC,
                        rec.source_id ASC
                ) AS rms_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY rec.source_id
                    ORDER BY
                        ABS(EXTRACT(EPOCH FROM (rms.call_time - (rec.call_time + (:timezone_offset_seconds * INTERVAL '1 second'))))) ASC,
                        ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) ASC,
                        CASE WHEN {rms_sales10} <> '' AND {rms_sales10} = {rec_sales10} THEN 0 ELSE 1 END ASC,
                        rms.source_id ASC
                ) AS rec_rank
            FROM {self.unified_table} rms
            JOIN {self.unified_table} rec
              ON rec.call_time >= rms.call_time - ((:timezone_offset_seconds + :timezone_offset_tolerance_seconds) * INTERVAL '1 second')
             AND rec.call_time <= rms.call_time - ((:timezone_offset_seconds - :timezone_offset_tolerance_seconds) * INTERVAL '1 second')
             AND {rms_customer10} <> ''
             AND {rms_customer10} = {rec_customer10}
             AND COALESCE(rms.call_direction, '') = COALESCE(rec.call_direction, '')
             AND COALESCE(rms.call_result, '') = COALESCE(rec.call_result, '')
             AND (
                    rms.talk_time_sec IS NULL
                 OR rec.talk_time_sec IS NULL
                 OR ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) <= :duration_tolerance
             )
            WHERE rms.rms_source_id IS NOT NULL
              AND rms.recording_source_id IS NULL
              AND rec.recording_source_id IS NOT NULL
              AND rec.rms_source_id IS NULL
              AND rms.call_time IS NOT NULL
              AND rec.call_time IS NOT NULL
              {scope_sql}
        )
        SELECT *
        FROM candidates
        WHERE rms_rank = 1
          AND rec_rank = 1
        """), params)

    def match_unified_rows(
        self,
        *,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        match_all: bool = False,
    ) -> dict[str, int]:
        """Run matching as a separate set-based phase.

        This replaces the old per-row matching hot path. It first matches exact
        dedupe_key rows, then falls back to pair_key + time/duration matching.
        """
        exact_table = "tmp_call_log_exact_matches"
        window_table = "tmp_call_log_window_matches"
        timezone_offset_table = "tmp_call_log_timezone_offset_matches"

        exact_started = time.perf_counter()
        self._create_exact_match_table(
            temp_table=exact_table,
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        exact_matches = self._merge_match_table(
            temp_table=exact_table,
            match_confidence="dedupe_key",
            match_reason="dedupe_key",
        )
        self.db.commit()
        self.timing.log("match_unified.exact_dedupe", time.perf_counter() - exact_started, rows=exact_matches)

        window_started = time.perf_counter()
        self._create_window_match_table(
            temp_table=window_table,
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        window_matches = self._merge_match_table(
            temp_table=window_table,
            match_confidence="strong_signature_unordered_pair",
            match_reason="pair_key_time_duration_window",
        )
        self.db.commit()
        self.timing.log("match_unified.window_pair_time", time.perf_counter() - window_started, rows=window_matches)

        timezone_started = time.perf_counter()
        self._create_timezone_offset_match_table(
            temp_table=timezone_offset_table,
            rms_min_source_id=rms_min_source_id,
            recording_min_source_id=recording_min_source_id,
            match_all=match_all,
        )
        timezone_offset_matches = self._merge_match_table(
            temp_table=timezone_offset_table,
            match_confidence="timezone_offset_5h30",
            match_reason="recording_naive_utc_to_ist_offset",
        )
        self.db.commit()
        self.timing.log(
            "match_unified.timezone_offset_5h30",
            time.perf_counter() - timezone_started,
            rows=timezone_offset_matches,
        )

        return {
            "matched_exact_dedupe": int(exact_matches),
            "matched_pair_time_window": int(window_matches),
            "matched_timezone_offset_5h30": int(timezone_offset_matches),
            "matched_total": int(exact_matches) + int(window_matches) + int(timezone_offset_matches),
        }

    def build_unified_legacy_row_match(
        self,
        *,
        rms_ids: Optional[list[int]] = None,
        recording_ids: Optional[list[int]] = None,
        rebuild_all: bool = False,
        refresh_duplicate_counts: bool = True,
        progress_every: int = DEFAULT_PROGRESS_EVERY,
    ) -> dict[str, Any]:
        """Compatibility path: old row-by-row matching. Slower for large runs."""
        total_started = time.perf_counter()

        load_started = time.perf_counter()
        rms_rows = self._load_rms_rows(None if rebuild_all else rms_ids or [])
        self.timing.log("build_unified.load_rms_rows", time.perf_counter() - load_started, rows=len(rms_rows))

        load_started = time.perf_counter()
        recording_rows = self._load_recording_rows(None if rebuild_all else recording_ids or [])
        self.timing.log("build_unified.load_recording_rows", time.perf_counter() - load_started, rows=len(recording_rows))

        counters: dict[str, int] = {"mode": "legacy_row_match"}

        process_started = time.perf_counter()
        last_progress = process_started
        for idx, row in enumerate(rms_rows, start=1):
            action = self.upsert_rms_unified(row)
            counters[action] = counters.get(action, 0) + 1
            if progress_every and idx % int(progress_every) == 0:
                now = time.perf_counter()
                self.timing.log(
                    "build_unified.rms_progress",
                    now - last_progress,
                    processed=idx,
                    total=len(rms_rows),
                    actions={k: v for k, v in counters.items() if k.startswith("updated") or k.startswith("inserted")},
                )
                last_progress = now
        self.db.commit()
        self.timing.log(
            "build_unified.process_rms_rows",
            time.perf_counter() - process_started,
            rows=len(rms_rows),
            actions={k: v for k, v in counters.items() if "rms" in k or k.startswith("updated")},
        )

        process_started = time.perf_counter()
        last_progress = process_started
        for idx, row in enumerate(recording_rows, start=1):
            action = self.upsert_recording_unified(row)
            counters[action] = counters.get(action, 0) + 1
            if progress_every and idx % int(progress_every) == 0:
                now = time.perf_counter()
                self.timing.log(
                    "build_unified.recording_progress",
                    now - last_progress,
                    processed=idx,
                    total=len(recording_rows),
                    actions={k: v for k, v in counters.items() if k.startswith("updated") or k.startswith("inserted")},
                )
                last_progress = now
        self.db.commit()
        self.timing.log(
            "build_unified.process_recording_rows",
            time.perf_counter() - process_started,
            rows=len(recording_rows),
            actions={k: v for k, v in counters.items() if "recording" in k or k.startswith("updated")},
        )

        if refresh_duplicate_counts:
            refresh_started = time.perf_counter()
            self._refresh_duplicate_counts()
            self.db.commit()
            self.timing.log("build_unified.refresh_duplicate_counts", time.perf_counter() - refresh_started)
        else:
            self.timing.log("build_unified.refresh_duplicate_counts", 0.0, note="skipped")

        counters["rms_rows_considered"] = len(rms_rows)
        counters["recording_rows_considered"] = len(recording_rows)
        counters["elapsed_sec"] = round(time.perf_counter() - total_started, 3)
        self.timing.log(
            "build_unified.total",
            time.perf_counter() - total_started,
            rows=len(rms_rows) + len(recording_rows),
        )
        return counters

    def build_unified(
        self,
        *,
        rms_ids: Optional[list[int]] = None,
        recording_ids: Optional[list[int]] = None,
        rms_min_source_id: Optional[int] = None,
        recording_min_source_id: Optional[int] = None,
        rebuild_all: bool = False,
        process_rms: bool = True,
        process_recordings: bool = True,
        match_after_insert: bool = True,
        legacy_row_match: bool = False,
        refresh_duplicate_counts: bool = True,
        progress_every: int = DEFAULT_PROGRESS_EVERY,
    ) -> dict[str, Any]:
        if legacy_row_match:
            return self.build_unified_legacy_row_match(
                rms_ids=rms_ids,
                recording_ids=recording_ids,
                rebuild_all=rebuild_all,
                refresh_duplicate_counts=refresh_duplicate_counts,
                progress_every=progress_every,
            )

        total_started = time.perf_counter()
        counters: dict[str, Any] = {"mode": "bulk_insert_then_match"}

        if process_rms:
            started = time.perf_counter()
            rms_result = self.bulk_upsert_rms_unified(
                ids=None if rebuild_all else rms_ids,
                min_source_id=None if rebuild_all else rms_min_source_id,
                rebuild_all=rebuild_all,
            )
            self.db.commit()
            counters.update(rms_result)
            self.timing.log(
                "build_unified.bulk_upsert_rms",
                time.perf_counter() - started,
                rows=int(rms_result.get("rms_rows_considered") or 0),
                actions={k: v for k, v in rms_result.items() if k != "rms_rows_considered"},
            )
        else:
            counters.update({"rms_rows_considered": 0, "inserted_rms_only": 0, "updated_existing_rms": 0})
            self.timing.log("build_unified.bulk_upsert_rms", 0.0, rows=0, note="skipped")

        if process_recordings:
            started = time.perf_counter()
            recording_result = self.bulk_upsert_recording_unified(
                ids=None if rebuild_all else recording_ids,
                min_source_id=None if rebuild_all else recording_min_source_id,
                rebuild_all=rebuild_all,
            )
            self.db.commit()
            counters.update(recording_result)
            self.timing.log(
                "build_unified.bulk_upsert_recordings",
                time.perf_counter() - started,
                rows=int(recording_result.get("recording_rows_considered") or 0),
                actions={k: v for k, v in recording_result.items() if k != "recording_rows_considered"},
            )
        else:
            counters.update({"recording_rows_considered": 0, "inserted_recording_only": 0, "updated_existing_recording": 0})
            self.timing.log("build_unified.bulk_upsert_recordings", 0.0, rows=0, note="skipped")

        if match_after_insert:
            started = time.perf_counter()
            match_result = self.match_unified_rows(
                rms_min_source_id=None if rebuild_all else rms_min_source_id,
                recording_min_source_id=None if rebuild_all else recording_min_source_id,
                match_all=rebuild_all,
            )
            counters.update(match_result)
            self.timing.log("build_unified.match_phase", time.perf_counter() - started, rows=match_result.get("matched_total", 0))
        else:
            counters.update({"matched_exact_dedupe": 0, "matched_pair_time_window": 0, "matched_timezone_offset_5h30": 0, "matched_total": 0})
            self.timing.log("build_unified.match_phase", 0.0, note="skipped")

        if refresh_duplicate_counts:
            refresh_started = time.perf_counter()
            self._refresh_duplicate_counts()
            self.db.commit()
            self.timing.log("build_unified.refresh_duplicate_counts", time.perf_counter() - refresh_started)
        else:
            self.timing.log("build_unified.refresh_duplicate_counts", 0.0, note="skipped")

        counters["elapsed_sec"] = round(time.perf_counter() - total_started, 3)
        self.timing.log(
            "build_unified.total",
            time.perf_counter() - total_started,
            rows=int(counters.get("rms_rows_considered") or 0) + int(counters.get("recording_rows_considered") or 0),
        )
        return counters

    def match_diagnostics(self) -> dict[str, Any]:
        """Diagnostics using indexed pair_key plus the legacy 5h30 offset signature."""
        rms_customer10 = self._phone10_sql("rms.counterparty_phone")
        rec_customer10 = self._phone10_sql("rec.counterparty_phone")
        row = self.db.execute(text(f"""
        WITH rec AS (
            SELECT source_id, call_time, talk_time_sec, pair_key, counterparty_phone, call_direction, call_result
            FROM {self.recording_raw_table}
            WHERE call_time IS NOT NULL
            ORDER BY source_id DESC
            LIMIT 5000
        ), rms AS (
            SELECT source_id, call_time, talk_time_sec, pair_key, counterparty_phone, call_direction, call_result
            FROM {self.rms_table}
            WHERE call_time IS NOT NULL
            ORDER BY source_id DESC
            LIMIT 200000
        )
        SELECT
            (SELECT COUNT(*) FROM rec) AS recent_recordings_checked,
            (SELECT COUNT(*) FROM rms) AS recent_rms_checked,
            COUNT(*) FILTER (
                WHERE rms.pair_key = rec.pair_key
                  AND ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time))) <= 300
            ) AS pair_time_5m,
            COUNT(*) FILTER (
                WHERE rms.pair_key = rec.pair_key
                  AND ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time))) <= 300
                  AND ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) <= 120
            ) AS pair_time_duration,
            COUNT(*) FILTER (
                WHERE {rms_customer10} <> ''
                  AND {rms_customer10} = {rec_customer10}
                  AND COALESCE(rms.call_direction, '') = COALESCE(rec.call_direction, '')
                  AND COALESCE(rms.call_result, '') = COALESCE(rec.call_result, '')
                  AND ABS(COALESCE(rms.talk_time_sec, 0) - COALESCE(rec.talk_time_sec, 0)) <= :offset_duration_tolerance
                  AND ABS(EXTRACT(EPOCH FROM (rms.call_time - (rec.call_time + (:offset_seconds * INTERVAL '1 second'))))) <= :offset_tolerance
            ) AS timezone_offset_5h30_signature,
            MIN(ABS(EXTRACT(EPOCH FROM (rms.call_time - rec.call_time)))) AS best_time_gap_seconds,
            MIN(ABS(EXTRACT(EPOCH FROM (rms.call_time - (rec.call_time + (:offset_seconds * INTERVAL '1 second')))))) AS best_timezone_offset_gap_seconds
        FROM rec
        JOIN rms
          ON (rms.pair_key IS NOT NULL AND rms.pair_key = rec.pair_key)
          OR (
                {rms_customer10} <> ''
            AND {rms_customer10} = {rec_customer10}
            AND rec.call_time >= rms.call_time - ((:offset_seconds + :offset_tolerance) * INTERVAL '1 second')
            AND rec.call_time <= rms.call_time - ((:offset_seconds - :offset_tolerance) * INTERVAL '1 second')
          )
        """), {
            "offset_seconds": TIMEZONE_OFFSET_MATCH_SECONDS,
            "offset_tolerance": TIMEZONE_OFFSET_TOLERANCE_SECONDS,
            "offset_duration_tolerance": TIMEZONE_OFFSET_DURATION_TOLERANCE_SECONDS,
        }).mappings().fetchone()
        return dict(row) if row else {}

    def latest_raw_ids(self, table_name: str, min_source_id: int) -> list[int]:
        rows = self.db.execute(
            text(f"SELECT source_id FROM {_table_ref(self.schema, table_name)} WHERE source_id > :min_id ORDER BY source_id"),
            {"min_id": int(min_source_id)},
        ).mappings().fetchall()
        return [int(row["source_id"]) for row in rows]

    def run(
        self,
        *,
        limit: int,
        resync_window: int = DEFAULT_RESYNC_WINDOW,
        skip_rms: bool = False,
        skip_recordings: bool = False,
        rebuild_all: bool = False,
        create_only: bool = False,
        reset_unified: bool = False,
        reset_raw: bool = False,
        diagnostics: bool = False,
        skip_counts: bool = False,
        refresh_duplicate_counts: bool = True,
        progress_every: int = DEFAULT_PROGRESS_EVERY,
        backfill_existing_keys: bool = True,
        match_after_insert: bool = True,
        match_only: bool = False,
        legacy_row_match: bool = False,
    ) -> dict[str, Any]:
        run_started = time.perf_counter()

        ensure_started = time.perf_counter()
        self.ensure_tables(backfill_existing_keys=backfill_existing_keys)
        self.timing.log("run.ensure_tables", time.perf_counter() - ensure_started)

        if create_only:
            self.timing.log("run.total", time.perf_counter() - run_started)
            return {"created_tables": True, "schema": self.schema, "timing": self.timing.records}

        if reset_raw:
            reset_started = time.perf_counter()
            self.reset_raw_tables()
            self.timing.log("run.reset_raw_tables", time.perf_counter() - reset_started)
            rebuild_all = True
        elif reset_unified:
            reset_started = time.perf_counter()
            self.reset_unified_table()
            self.timing.log("run.reset_unified_table", time.perf_counter() - reset_started)
            rebuild_all = True

        raw_results: list[dict[str, Any]] = []

        if match_only:
            match_started = time.perf_counter()
            match_result = self.match_unified_rows(match_all=True)
            unified_result = {
                "mode": "match_only",
                **match_result,
                "elapsed_sec": round(time.perf_counter() - match_started, 3),
            }
            if refresh_duplicate_counts:
                refresh_started = time.perf_counter()
                self._refresh_duplicate_counts()
                self.db.commit()
                self.timing.log("build_unified.refresh_duplicate_counts", time.perf_counter() - refresh_started)
            else:
                self.timing.log("build_unified.refresh_duplicate_counts", 0.0, note="skipped")
        else:
            rms_result = None
            recording_result = None

            if not skip_rms:
                rms_result = self.sync_rms_call_logs(limit=limit, resync_window=resync_window)
                raw_results.append(rms_result)
            if not skip_recordings:
                recording_result = self.sync_recordings_raw(limit=limit, resync_window=resync_window)
                raw_results.append(recording_result)

            if legacy_row_match:
                # Compatibility path only. It intentionally loads source ids/rows
                # into Python and is slower for high-volume runs.
                if rebuild_all:
                    unified_result = self.build_unified(
                        rebuild_all=True,
                        legacy_row_match=True,
                        refresh_duplicate_counts=refresh_duplicate_counts,
                        progress_every=progress_every,
                    )
                else:
                    rms_ids: list[int] = []
                    recording_ids: list[int] = []
                    if rms_result is not None:
                        latest_started = time.perf_counter()
                        rms_ids = self.latest_raw_ids("staging_rms_call_log_tracking", int(rms_result.get("resync_from_id") or 0))
                        self.timing.log("run.latest_rms_ids", time.perf_counter() - latest_started, rows=len(rms_ids))
                    if recording_result is not None:
                        latest_started = time.perf_counter()
                        recording_ids = self.latest_raw_ids("staging_call_recordings_transcript_raw", int(recording_result.get("resync_from_id") or 0))
                        self.timing.log("run.latest_recording_ids", time.perf_counter() - latest_started, rows=len(recording_ids))
                    unified_result = self.build_unified(
                        rms_ids=rms_ids,
                        recording_ids=recording_ids,
                        legacy_row_match=True,
                        refresh_duplicate_counts=refresh_duplicate_counts,
                        progress_every=progress_every,
                    )
            else:
                rms_min_source_id = int(rms_result.get("resync_from_id") or 0) if rms_result is not None else None
                recording_min_source_id = int(recording_result.get("resync_from_id") or 0) if recording_result is not None else None
                unified_result = self.build_unified(
                    rms_min_source_id=rms_min_source_id,
                    recording_min_source_id=recording_min_source_id,
                    rebuild_all=rebuild_all,
                    process_rms=not skip_rms,
                    process_recordings=not skip_recordings,
                    match_after_insert=match_after_insert,
                    legacy_row_match=False,
                    refresh_duplicate_counts=refresh_duplicate_counts,
                    progress_every=progress_every,
                )

        counts = None
        if skip_counts:
            self.timing.log("run.unified_counts", 0.0, note="skipped")
        else:
            counts_started = time.perf_counter()
            counts = self.db.execute(text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE match_status = 'matched') AS matched,
                    COUNT(*) FILTER (WHERE match_status = 'rms_only') AS rms_only,
                    COUNT(*) FILTER (WHERE match_status = 'recording_only') AS recording_only,
                    COUNT(*) FILTER (WHERE transcript_text IS NOT NULL OR translated_text IS NOT NULL OR transcript_text_eleven_labs IS NOT NULL OR raw_transcripts IS NOT NULL) AS with_transcript,
                    COUNT(*) FILTER (WHERE audio_url IS NOT NULL) AS with_audio,
                    COALESCE(SUM(duplicate_count), 0) AS duplicates_collapsed
                FROM {self.unified_table}
            """)).mappings().fetchone()
            self.timing.log("run.unified_counts", time.perf_counter() - counts_started)

        diag_payload = None
        if diagnostics:
            diag_started = time.perf_counter()
            diag_payload = self.match_diagnostics()
            self.timing.log("run.match_diagnostics", time.perf_counter() - diag_started)

        self.timing.log("run.total", time.perf_counter() - run_started)

        return compact_dict(
            {
                "schema": self.schema,
                "reset_raw": bool(reset_raw),
                "reset_unified": bool(reset_unified),
                "match_only": bool(match_only),
                "raw_sync": raw_results,
                "unified_build": unified_result,
                "unified_counts": dict(counts) if counts else {},
                "match_diagnostics": diag_payload,
                "timing": self.timing.records,
            }
        )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build unified call logs from RMS call_tracking_log and recording transcripts.")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--limit", type=int, default=50000)
    parser.add_argument("--resync-window", type=int, default=DEFAULT_RESYNC_WINDOW, help="Re-read this many recent source ids to catch late transcript/status updates.")
    parser.add_argument("--match-window-seconds", type=int, default=MATCH_WINDOW_SECONDS)
    parser.add_argument("--skip-rms", action="store_true")
    parser.add_argument("--skip-recordings", action="store_true")
    parser.add_argument("--rebuild-all", action="store_true", help="Rebuild/enrich unified rows from all raw rows already staged.")
    parser.add_argument("--reset-unified", action="store_true", help="Truncate unified call table and rebuild it from staged raw rows. Implies --rebuild-all.")
    parser.add_argument("--reset-raw", action="store_true", help="Truncate raw + unified call tables, clear raw checkpoints, and resync from source id 0. Use after timestamp/normalization changes.")
    parser.add_argument("--diagnostics", action="store_true", help="Include match diagnostics in output.")
    parser.add_argument("--skip-counts", action="store_true", help="Skip full-table unified COUNT(*) summary. Useful for high-volume cron runs.")
    parser.add_argument("--skip-duplicate-refresh", action="store_true", help="Skip full-table duplicate_count refresh. Use after the one-time cleanup/backfill is done.")
    parser.add_argument("--skip-backfill-existing-keys", action="store_true", help="Skip old-row pair_key/dedupe_key backfill during startup. New rows already get these keys during sync.")
    parser.add_argument("--skip-match", action="store_true", help="Only bulk-insert/update source rows into unified; do not run the separate match phase in this run.")
    parser.add_argument("--match-only", action="store_true", help="Do not sync sources; only run the set-based matcher over currently unmatched unified rows.")
    parser.add_argument("--legacy-row-match", action="store_true", help="Use the old row-by-row matcher. Slower; kept only for debugging/compatibility.")
    parser.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY, help="Print unified-build progress every N rows. Use 0 to disable progress logs. Only used by --legacy-row-match.")
    parser.add_argument("--no-timing", action="store_true", help="Disable stderr timing logs. Timing records are still returned in JSON.")
    parser.add_argument("--create-only", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with get_target_session(args.database_url) as db:
        result = UnifiedCallLogBuilder(
            db,
            schema=args.schema,
            match_window_seconds=args.match_window_seconds,
            timing_enabled=not args.no_timing,
        ).run(
            limit=args.limit,
            resync_window=args.resync_window,
            skip_rms=args.skip_rms,
            skip_recordings=args.skip_recordings,
            rebuild_all=args.rebuild_all,
            create_only=args.create_only,
            reset_unified=args.reset_unified,
            reset_raw=args.reset_raw,
            diagnostics=args.diagnostics,
            skip_counts=args.skip_counts,
            refresh_duplicate_counts=not args.skip_duplicate_refresh,
            progress_every=args.progress_every,
            backfill_existing_keys=not args.skip_backfill_existing_keys,
            match_after_insert=not args.skip_match,
            match_only=args.match_only,
            legacy_row_match=args.legacy_row_match,
        )
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
