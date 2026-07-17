# ========================================================================
# FILE: app/cli_dedupe.py (FIXED CLI INTERFACE)
# ========================================================================
"""CLI interface for deduplication"""
import argparse
import json
import time
from app.db.dedupe_session import dedupe_db_session
from app.utils.dedupe_models import create_execution_context
from app.persistence.dedupe_discovery import DuplicateKeyDiscovery, EdgeListBuilder, ConnectedComponentFinder
from app.services.dedupe_group_merger import GroupMerger
from app.utils.dedupe_logger import log
from app.model.user_data_test import UserDataa

class DeduplicationOrchestrator:
    @staticmethod
    def run(workers=4, dry_run=False):
        """Main orchestration: Phases 1-3 discovery, Phase 5-11 merging"""
        start_time = time.time()
        log.info("="*70)
        log.info("[MAIN] DEDUPLICATION v7.0 START")
        log.info("[MAIN] Config: workers={}, dry_run={}".format(workers, dry_run))
        log.info("="*70)

        ctx = create_execution_context()
        groups = []
        merge_results = []

        try:
            # ========== PHASES 1-3: DISCOVERY ==========
            with dedupe_db_session() as session:
                log.info("[MAIN] PHASE 1: DISCOVERY")
                dup_keys = DuplicateKeyDiscovery.discover(session, ctx)
                
                log.info("[MAIN] PHASE 2: EDGE BUILDING")
                edge_count = EdgeListBuilder.build(session, dup_keys, ctx)
                
                if edge_count > 0:
                    log.info("[MAIN] PHASE 3: COMPONENT DETECTION")
                    groups = ConnectedComponentFinder.find(session, ctx)
                    groups.sort(key=len, reverse=True)
                    log.info("[MAIN] Phase 3 complete: Found {} groups".format(len(groups)))
                else:
                    groups = []
                    log.info("[MAIN] Skipping PHASE 3: No edges found")

            # ========== HANDLE NO-DUPLICATES CASE ==========
            if not groups:
                elapsed = time.time() - start_time
                log.info("[MAIN] No duplicates found")
                return {
                    "status": "success",
                    "groups_found": 0,
                    "groups_merged": 0,
                    "users_merged": 0,
                    "activities_updated": 0,
                    "batch_id": ctx.batch_id,
                    "elapsed_seconds": elapsed
                }

            # ========== DRY RUN MODE ==========
            if dry_run:
                log.info("[MAIN] DRY RUN MODE: Preview only")
                with dedupe_db_session() as session:
                    for idx, g in enumerate(groups[:10], 1):
                        users = session.query(UserDataa).filter(
                            UserDataa.u_id.in_(g)
                        ).order_by(UserDataa.u_id).all()
                        user_str = ", ".join(str(u.u_id) for u in users)
                        log.info("[MAIN] GROUP {}: [{}] (size={})".format(idx, user_str, len(g)))
                
                elapsed = time.time() - start_time
                return {
                    "status": "dry_run",
                    "groups_found": len(groups),
                    "batch_id": ctx.batch_id,
                    "elapsed_seconds": elapsed
                }

            # ========== PHASE 5-11: MERGING ==========
            log.info("[MAIN] PHASE 5-11: MERGING {} groups".format(len(groups)))
            ctx.phase = "merging"
            
            users_merged_total = 0
            activities_updated_total = 0
            
            for idx, group in enumerate(groups):
                try:
                    with dedupe_db_session() as session:
                        result = GroupMerger.merge_group(session, group, ctx)
                        merge_results.append(result)
                        
                        if result.get("success"):
                            users_merged_total += result.get("merged_count", 0)
                            activities_updated_total += result.get("activities_reassigned", 0)
                        
                        if (idx + 1) % 50 == 0:
                            elapsed_so_far = time.time() - start_time
                            rate = (idx + 1) / elapsed_so_far if elapsed_so_far > 0 else 0
                            eta = (len(groups) - idx - 1) / rate if rate > 0 else 0
                            log.info("[MAIN] Progress: {}/{} groups ({:.1f}%) | Rate: {:.1f} groups/sec | ETA: {:.1f}s".format(
                                idx + 1, len(groups), 
                                (idx + 1) / len(groups) * 100,
                                rate,
                                eta
                            ))
                except Exception as e:
                    log.exception("[MAIN] Failed to merge group {}: {}".format(group, type(e).__name__))
                    merge_results.append({
                        "error": str(e),
                        "group": group,
                        "success": False
                    })

            # ========== AGGREGATE RESULTS ==========
            groups_merged = sum(1 for r in merge_results if r.get("success"))
            
            elapsed = time.time() - start_time

            log.info("="*70)
            log.info("[MAIN] DEDUPLICATION COMPLETE")
            log.info("[MAIN] Groups found: {}".format(len(groups)))
            log.info("[MAIN] Groups merged: {}".format(groups_merged))
            log.info("[MAIN] Users merged: {}".format(users_merged_total))
            log.info("[MAIN] Activities updated: {}".format(activities_updated_total))
            log.info("[MAIN] Total time: {:.2f}s ({:.1f} min)".format(elapsed, elapsed/60))
            log.info("="*70)

            return {
                "status": "success",
                "groups_found": len(groups),
                "groups_merged": groups_merged,
                "users_merged": users_merged_total,
                "activities_updated": activities_updated_total,
                "batch_id": ctx.batch_id,
                "elapsed_seconds": elapsed,
                "merge_results_summary": {
                    "total_results": len(merge_results),
                    "successful": groups_merged,
                    "failed": len(merge_results) - groups_merged
                }
            }

        except Exception as e:
            elapsed = time.time() - start_time
            log.exception("[MAIN] FATAL ERROR: {}".format(type(e).__name__))
            return {
                "status": "failed",
                "error": str(e),
                "batch_id": ctx.batch_id,
                "elapsed_seconds": elapsed
            }

def main():
    parser = argparse.ArgumentParser(
        description="Production User Deduplication Engine v7.0"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview duplicates without merging"
    )
    args = parser.parse_args()

    result = DeduplicationOrchestrator.run(workers=args.workers, dry_run=args.dry_run)

    print("\n" + "="*70)
    print("DEDUPLICATION SUMMARY")
    print("="*70)
    print(json.dumps(result, indent=2, default=str))
    print("="*70)
    
    if result.get("status") == "success":
        print("\nSUCCESS: {} users merged in {:.2f}s".format(
            result['users_merged'],
            result['elapsed_seconds']
        ))
    else:
        print("\nFAILED: {}".format(result.get('error', 'Unknown error')))

if __name__ == "__main__":
    main()