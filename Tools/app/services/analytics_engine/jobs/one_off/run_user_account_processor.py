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
import time
from sqlalchemy import text

from app.db.database import get_db
from app.services.analytics_engine.processors.user_account_sync import UserAccountSync


def run(limit=None, start_source_id=None, batch_size=None, skip_counts=False):
    db_gen = get_db()
    db = next(db_gen)

    try:
        print("=" * 80)
        print("AnalyticsEngine: processing user accounts")
        print("=" * 80)
        print(
            f"limit={limit}, start_source_id={start_source_id}, "
            f"batch_size={batch_size}, skip_counts={skip_counts}"
        )

        start_time = time.perf_counter()

        result = UserAccountSync(db).run(
            limit=limit,
            start_source_id=start_source_id,
            batch_size=batch_size,
        )

        elapsed = time.perf_counter() - start_time

        counts = None
        if not skip_counts:
            counts = {
                "identity_person": db.execute(text('SELECT COUNT(*) FROM "AnalyticsEngine".identity_person')).scalar(),
                "identity_person_key": db.execute(text('SELECT COUNT(*) FROM "AnalyticsEngine".identity_person_key')).scalar(),
                "identity_person_merge": db.execute(text('SELECT COUNT(*) FROM "AnalyticsEngine".identity_person_merge')).scalar(),
                "event_participant": db.execute(text('SELECT COUNT(*) FROM "AnalyticsEngine".event_participant')).scalar(),
            }

        print("\nResult")
        print("-" * 80)
        for k, v in result.items():
            print(f"{k}: {v}")

        print("\nTiming")
        print("-" * 80)
        print(f"Total execution time: {elapsed:.2f} seconds")

        if counts is not None:
            print("\nPost-run counts")
            print("-" * 80)
            for k, v in counts.items():
                print(f"{k}: {v}")

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-source-id", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--skip-counts", action="store_true")
    args = parser.parse_args()

    run(
        limit=args.limit,
        start_source_id=args.start_source_id,
        batch_size=args.batch_size,
        skip_counts=args.skip_counts,
    )
