# -----------------------------------------------------------------------------
# Repo bootstrap
# -----------------------------------------------------------------------------
import os
import sys
from pathlib import Path

def _analytics_engine_project_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "app").is_dir():
            return parent
    raise RuntimeError("Could not find FastApi project root containing the app/ folder.")

PROJECT_ROOT = _analytics_engine_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import json
import time
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text

from app.db.database import get_db
from app.services.analytics_engine.processors.whatsapp_message_sync import WhatsAppMessageSync


SCHEMA = "AnalyticsEngine"
PROCESSOR_NAME = "whatsapp_message_sync"
SOURCE_TABLE_NAME = "staging_whatsapp_messages"
SOURCE_TABLE_REF = f'"{SCHEMA}".{SOURCE_TABLE_NAME}'
CURSOR_TS_EXPR = "COALESCE(message_time, synced_at)"
DEFAULT_REBUILD_WINDOW = 1_000_000


def _parse_notes(notes: Any) -> Dict[str, Any]:
    if notes is None:
        return {}
    if isinstance(notes, dict):
        return notes
    if isinstance(notes, str):
        text_value = notes.strip()
        if not text_value:
            return {}
        try:
            loaded = json.loads(text_value)
            return loaded if isinstance(loaded, dict) else {"notes_message": text_value}
        except Exception:
            return {"notes_message": text_value}
    return {}


def _build_notes_payload(*, notes_message: str, last_source_id_text: Optional[str] = None) -> str:
    payload = {
        "notes_message": notes_message,
        "last_source_id_text": last_source_id_text,
    }
    return json.dumps(payload, ensure_ascii=False)


def get_checkpoint(db) -> Optional[Dict[str, Any]]:
    row = db.execute(
        text(
            f"""
            SELECT
                processor_name,
                source_table,
                cursor_mode,
                last_id,
                last_timestamp,
                last_batch_count,
                last_status,
                last_error,
                notes,
                updated_at
            FROM "{SCHEMA}".processor_checkpoint
            WHERE processor_name = :processor_name
            """
        ),
        {"processor_name": PROCESSOR_NAME},
    ).mappings().fetchone()
    if not row:
        return None

    data = dict(row)
    parsed_notes = _parse_notes(data.get("notes"))
    data["last_source_id_text"] = parsed_notes.get("last_source_id_text")
    data["notes_message"] = parsed_notes.get("notes_message") or data.get("notes")
    return data


def get_latest_time_cursor_start(db, window: int) -> Tuple[Any, Any]:
    row = db.execute(
        text(
            f"""
            WITH latest_window AS (
                SELECT
                    source_id,
                    {CURSOR_TS_EXPR} AS cursor_ts
                FROM {SOURCE_TABLE_REF}
                WHERE {CURSOR_TS_EXPR} IS NOT NULL
                ORDER BY {CURSOR_TS_EXPR} DESC, source_id DESC
                LIMIT :window
            ),
            boundary AS (
                SELECT
                    source_id,
                    cursor_ts
                FROM latest_window
                ORDER BY cursor_ts ASC, source_id ASC
                LIMIT 1
            )
            SELECT source_id, cursor_ts
            FROM boundary
            """
        ),
        {"window": int(window)},
    ).mappings().fetchone()

    if not row:
        return None, None
    return row["source_id"], row["cursor_ts"]


def _checkpoint_safe_last_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value

    value_text = str(value).strip()
    if value_text.isdigit() or (value_text.startswith("-") and value_text[1:].isdigit()):
        try:
            return int(value_text)
        except ValueError:
            return None
    return None


def seed_rebuild_checkpoint(db, *, start_source_id: Any, start_timestamp: Any, window: int) -> Optional[int]:
    checkpoint_last_id = _checkpoint_safe_last_id(start_source_id)
    notes_payload = _build_notes_payload(
        notes_message=f"Seeded WhatsApp rebuild for latest {int(window)} rows",
        last_source_id_text=(str(start_source_id) if checkpoint_last_id is None and start_source_id not in (None, "") else None),
    )

    db.execute(
        text(
            f"""
            INSERT INTO "{SCHEMA}".processor_checkpoint
            (
                processor_name,
                source_table,
                cursor_mode,
                last_id,
                last_timestamp,
                last_batch_count,
                last_status,
                last_error,
                notes,
                updated_at
            )
            VALUES
            (
                :processor_name,
                :source_table,
                'time',
                :last_id,
                :last_timestamp,
                0,
                'IDLE',
                NULL,
                :notes,
                NOW()
            )
            ON CONFLICT (processor_name)
            DO UPDATE SET
                source_table = EXCLUDED.source_table,
                cursor_mode = EXCLUDED.cursor_mode,
                last_id = EXCLUDED.last_id,
                last_timestamp = EXCLUDED.last_timestamp,
                last_batch_count = 0,
                last_status = 'IDLE',
                last_error = NULL,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """
        ),
        {
            "processor_name": PROCESSOR_NAME,
            "source_table": SOURCE_TABLE_REF,
            "last_id": checkpoint_last_id,
            "last_timestamp": start_timestamp,
            "notes": notes_payload,
        },
    )
    db.commit()
    return checkpoint_last_id


def reset_checkpoint_to_start(db) -> None:
    notes_payload = _build_notes_payload(
        notes_message="Seeded WhatsApp rebuild from start",
        last_source_id_text=None,
    )
    db.execute(
        text(
            f"""
            INSERT INTO "{SCHEMA}".processor_checkpoint
            (
                processor_name,
                source_table,
                cursor_mode,
                last_id,
                last_timestamp,
                last_batch_count,
                last_status,
                last_error,
                notes,
                updated_at
            )
            VALUES
            (
                :processor_name,
                :source_table,
                'time',
                NULL,
                NULL,
                0,
                'IDLE',
                NULL,
                :notes,
                NOW()
            )
            ON CONFLICT (processor_name)
            DO UPDATE SET
                source_table = EXCLUDED.source_table,
                cursor_mode = EXCLUDED.cursor_mode,
                last_id = NULL,
                last_timestamp = NULL,
                last_batch_count = 0,
                last_status = 'IDLE',
                last_error = NULL,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """
        ),
        {
            "processor_name": PROCESSOR_NAME,
            "source_table": SOURCE_TABLE_REF,
            "notes": notes_payload,
        },
    )
    db.commit()


def get_post_run_counts(db) -> Dict[str, Any]:
    return {
        "event_fact": db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM "{SCHEMA}".event_fact
                WHERE source_table = :source_table
                """
            ),
            {"source_table": SOURCE_TABLE_NAME},
        ).scalar(),
        "event_participant": db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM "{SCHEMA}".event_participant
                WHERE event_id IN (
                    SELECT event_id
                    FROM "{SCHEMA}".event_fact
                    WHERE source_table = :source_table
                )
                """
            ),
            {"source_table": SOURCE_TABLE_NAME},
        ).scalar(),
        "event_context": db.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM "{SCHEMA}".event_context
                WHERE event_id IN (
                    SELECT event_id
                    FROM "{SCHEMA}".event_fact
                    WHERE source_table = :source_table
                )
                """
            ),
            {"source_table": SOURCE_TABLE_NAME},
        ).scalar(),
    }


def print_checkpoint(label: str, checkpoint: Optional[Dict[str, Any]]) -> None:
    print(f"\n{label}")
    print("-" * 80)
    if not checkpoint:
        print("(no checkpoint found)")
        return

    for key in (
        "processor_name",
        "source_table",
        "cursor_mode",
        "last_id",
        "last_source_id_text",
        "last_timestamp",
        "last_batch_count",
        "last_status",
        "last_error",
        "notes_message",
        "updated_at",
    ):
        print(f"{key}: {checkpoint.get(key)}")


def run(
    *,
    limit: Optional[int] = None,
    start_source_id: Any = None,
    batch_size: Optional[int] = None,
    skip_counts: bool = False,
    rebuild: bool = False,
    rebuild_from_start: bool = False,
    window: Optional[int] = None,
    show_checkpoint: bool = False,
) -> None:
    if rebuild and rebuild_from_start:
        raise SystemExit("Use only one of --rebuild or --rebuild-from-start")

    db_gen = get_db()
    db = next(db_gen)

    try:
        rebuild_window = int(window or limit or DEFAULT_REBUILD_WINDOW)

        mode = "incremental"
        if rebuild:
            mode = "rebuild_latest_window"
        elif rebuild_from_start:
            mode = "rebuild_from_start"

        print("=" * 80)
        print("AnalyticsEngine: processing whatsapp messages")
        print("=" * 80)
        print(
            f"mode={mode}, "
            f"limit={limit}, start_source_id={start_source_id}, "
            f"batch_size={batch_size}, skip_counts={skip_counts}, "
            f"window={rebuild_window if rebuild else None}"
        )

        checkpoint_before = get_checkpoint(db)
        if show_checkpoint:
            print_checkpoint("Checkpoint before run", checkpoint_before)

        effective_start_source_id = start_source_id
        effective_limit = limit

        if rebuild_from_start:
            reset_checkpoint_to_start(db)
            print("\nRebuild from start seed")
            print("-" * 80)
            print("checkpoint_last_id: NULL")
            print("checkpoint_last_timestamp: NULL")
            print("checkpoint_last_source_id_text: NULL")
            effective_start_source_id = None

        elif rebuild:
            seeded_start_id, seeded_start_ts = get_latest_time_cursor_start(db, rebuild_window)
            if seeded_start_ts is None:
                print("\nNo WhatsApp rows found in staging with a valid cursor timestamp.")
                return

            print("\nRebuild seed")
            print("-" * 80)
            print(f"start_source_id: {seeded_start_id}")
            print(f"start_timestamp: {seeded_start_ts}")

            checkpoint_seed_id = seed_rebuild_checkpoint(
                db,
                start_source_id=seeded_start_id,
                start_timestamp=seeded_start_ts,
                window=rebuild_window,
            )
            if checkpoint_seed_id is None and seeded_start_id not in (None, ""):
                print(
                    "checkpoint_last_id: NULL (WhatsApp source_id is non-numeric, so the exact "
                    "boundary source_id is persisted as text and also used in-memory for this run)"
                )
            effective_start_source_id = seeded_start_id
            if effective_limit is None:
                effective_limit = rebuild_window

        start_time = time.perf_counter()

        result = WhatsAppMessageSync(db).run(
            limit=effective_limit,
            start_source_id=effective_start_source_id,
            batch_size=batch_size,
        )

        elapsed = time.perf_counter() - start_time
        counts = None if skip_counts else get_post_run_counts(db)
        checkpoint_after = get_checkpoint(db)

        print("\nResult")
        print("-" * 80)
        for k, v in result.items():
            print(f"{k}: {v}")

        print("\nTiming")
        print("-" * 80)
        print(f"Total execution time: {elapsed:.2f} seconds")

        if counts is not None:
            print("\nPost-run whatsapp counts")
            print("-" * 80)
            for k, v in counts.items():
                print(f"{k}: {v}")

        if show_checkpoint:
            print_checkpoint("Checkpoint after run", checkpoint_after)

        print("\nDone.")

    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            next(db_gen, None)
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run AnalyticsEngine WhatsApp sync only. Supports incremental, latest-window rebuild, and rebuild-from-start modes."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-source-id", type=str, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--skip-counts", action="store_true")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Seed checkpoint from the oldest row inside the latest window and rebuild WhatsApp only.",
    )
    parser.add_argument(
        "--rebuild-from-start",
        action="store_true",
        help="Reset the WhatsApp checkpoint to the beginning and backfill from the earliest rows forward.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Window size used only with --rebuild. Defaults to --limit, else 1000000.",
    )
    parser.add_argument(
        "--show-checkpoint",
        action="store_true",
        help="Print WhatsApp checkpoint before and after the run.",
    )
    args = parser.parse_args()

    run(
        limit=args.limit,
        start_source_id=args.start_source_id,
        batch_size=args.batch_size,
        skip_counts=args.skip_counts,
        rebuild=args.rebuild,
        rebuild_from_start=args.rebuild_from_start,
        window=args.window,
        show_checkpoint=args.show_checkpoint,
    )
