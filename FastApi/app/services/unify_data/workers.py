"""
Worker runner for the new modular sync service. Does NOT import or use sync_all_to_unified.py.
"""
import time
import argparse
from app.utils.logger import get_logger
from .sync_service import run_sync_once

log = get_logger(__name__)

def main_once(batch_size: int):
    log.info("running sync once", extra={"batch_size": batch_size})
    summary = run_sync_once(batch_size=batch_size)
    log.info("sync completed", extra={"summary": summary})

def main_loop(batch_size: int, interval_seconds: int):
    log.info("starting sync loop", extra={"batch_size": batch_size, "interval_seconds": interval_seconds})
    try:
        while True:
            summary = run_sync_once(batch_size=batch_size)
            log.info("sync_iteration_complete", extra={"summary": summary})
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log.info("sync_loop_stopped_by_user")

def parse_args():
    p = argparse.ArgumentParser(description="Unify data sync worker (modular)")
    p.add_argument("--once", action="store_true", help="Run sync once and exit")
    p.add_argument("--batch", type=int, default=10, help="Batch size per fetch")
    p.add_argument("--interval", type=int, default=60, help="Interval between runs in seconds (only for loop mode)")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if args.once:
        main_once(batch_size=args.batch)
    else:
        main_loop(batch_size=args.batch, interval_seconds=args.interval)
