# ========================================================================
# FILE: app/services/workers.py (CELERY TASKS - FIXED)
# ========================================================================
"""Celery tasks for deduplication"""
import os
import time
from celery import shared_task, current_task
from app.db.dedupe_session import dedupe_db_session
from app.utils.dedupe_models import create_execution_context
from app.persistence.dedupe_discovery import DuplicateKeyDiscovery, EdgeListBuilder, ConnectedComponentFinder
from app.services.dedupe_group_merger import GroupMerger
from app.utils.dedupe_logger import log

@shared_task(bind=True, name='dedupe.discover_duplicates')
def discover_duplicates_task(self):
    """Celery task: Phase 1-3 discovery"""
    log.info("[CELERY] Task started: discover_duplicates, task_id={}".format(self.request.id))
    ctx = create_execution_context(worker_id="celery-{}".format(self.request.id[:8]))
    
    try:
        with dedupe_db_session() as session:
            log.info("[CELERY] Phase 1: Discovering duplicate keys")
            dup_keys = DuplicateKeyDiscovery.discover(session, ctx)
            
            log.info("[CELERY] Phase 2: Building edge list")
            edge_count = EdgeListBuilder.build(session, dup_keys, ctx)
            
            if edge_count > 0:
                log.info("[CELERY] Phase 3: Finding connected components")
                groups = ConnectedComponentFinder.find(session, ctx)
                groups.sort(key=len, reverse=True)
            else:
                groups = []
            
            log.info("[CELERY] Discovery complete: {} groups found".format(len(groups)))
            
            return {
                "status": "success",
                "groups_found": len(groups),
                "duplicate_keys": len(dup_keys),
                "edges": edge_count,
                "task_id": self.request.id,
                "batch_id": ctx.batch_id
            }
    except Exception as e:
        log.error("[CELERY] Discovery failed: {}".format(type(e).__name__))
        self.update_state(state='FAILURE', meta={'error': str(e)})
        raise

@shared_task(bind=True, name='dedupe.merge_group')
def merge_group_task(self, group, batch_id=None):
    """Celery task: Merge single group"""
    log.info("[CELERY] Merging group {}, task_id={}".format(group, self.request.id))
    ctx = create_execution_context(worker_id="celery-{}".format(self.request.id[:8]))
    
    try:
        with dedupe_db_session() as session:
            result = GroupMerger.merge_group(session, group, ctx)
            log.info("[CELERY] Group merge complete: success={}".format(result.get("success")))
            return result
    except Exception as e:
        log.error("[CELERY] Group merge failed: {}".format(type(e).__name__))
        self.update_state(state='FAILURE', meta={'error': str(e), 'group': group})
        raise

@shared_task(bind=True, name='dedupe.run_full_dedupe')
def run_full_deduplication_task(self, dry_run=False):
    """Celery task: Full deduplication pipeline"""
    log.info("[CELERY] Full dedupe started, task_id={}, dry_run={}".format(self.request.id, dry_run))
    ctx = create_execution_context(worker_id="celery-{}".format(self.request.id[:8]))
    start_time = time.time()
    
    try:
        # ========== PHASE 1-3: DISCOVERY ==========
        self.update_state(state='PROGRESS', meta={'phase': 'discovery', 'progress': 10})
        log.info("[CELERY] Phase 1-3: Starting discovery")
        
        with dedupe_db_session() as session:
            dup_keys = DuplicateKeyDiscovery.discover(session, ctx)
            log.info("[CELERY] Phase 1: Found {} duplicate keys".format(len(dup_keys)))
            
            self.update_state(state='PROGRESS', meta={'phase': 'discovery', 'progress': 20})
            
            edge_count = EdgeListBuilder.build(session, dup_keys, ctx)
            log.info("[CELERY] Phase 2: Built {} edges".format(edge_count))
            
            if edge_count > 0:
                self.update_state(state='PROGRESS', meta={'phase': 'discovery', 'progress': 30})
                groups = ConnectedComponentFinder.find(session, ctx)
                groups.sort(key=len, reverse=True)
                log.info("[CELERY] Phase 3: Found {} groups".format(len(groups)))
            else:
                groups = []
                log.info("[CELERY] No edges found, skipping component detection")
        
        # ========== HANDLE NO-DUPLICATES CASE ==========
        if not groups:
            elapsed = time.time() - start_time
            log.info("[CELERY] No duplicates found")
            return {
                "status": "success",
                "groups_found": 0,
                "groups_merged": 0,
                "users_merged": 0,
                "activities_updated": 0,
                "task_id": self.request.id,
                "batch_id": ctx.batch_id,
                "elapsed_seconds": elapsed
            }
        
        # ========== DRY RUN MODE ==========
        if dry_run:
            self.update_state(state='PROGRESS', meta={'phase': 'dry_run', 'progress': 50})
            elapsed = time.time() - start_time
            log.info("[CELERY] Dry run mode: {} groups found".format(len(groups)))
            return {
                "status": "dry_run",
                "groups_found": len(groups),
                "task_id": self.request.id,
                "batch_id": ctx.batch_id,
                "elapsed_seconds": elapsed
            }
        
        # ========== PHASE 5-11: MERGING ==========
        self.update_state(state='PROGRESS', meta={'phase': 'merging', 'progress': 40})
        log.info("[CELERY] Phase 5-11: Starting merge of {} groups".format(len(groups)))
        
        merge_results = []
        users_merged = 0
        activities_updated = 0
        
        for idx, group in enumerate(groups):
            try:
                with dedupe_db_session() as session:
                    result = GroupMerger.merge_group(session, group, ctx)
                    merge_results.append(result)
                    
                    if result.get("success"):
                        users_merged += result.get("merged_count", 0)
                        activities_updated += result.get("activities_reassigned", 0)
                    
                    # Update progress every 50 groups
                    if (idx + 1) % 50 == 0:
                        progress_pct = 40 + int((idx + 1) / len(groups) * 50)
                        self.update_state(state='PROGRESS', meta={
                            'phase': 'merging',
                            'progress': progress_pct,
                            'groups_processed': idx + 1,
                            'users_merged_so_far': users_merged,
                            'activities_updated_so_far': activities_updated
                        })
                        log.info("[CELERY] Merge progress: {}/{} groups".format(idx + 1, len(groups)))
            
            except Exception as e:
                log.warning("[CELERY] Failed to merge group {}: {}".format(group, type(e).__name__))
                merge_results.append({
                    "error": str(e),
                    "group": group,
                    "success": False
                })
        
        groups_merged = sum(1 for r in merge_results if r.get("success"))
        elapsed = time.time() - start_time
        
        self.update_state(state='PROGRESS', meta={'phase': 'finalizing', 'progress': 95})
        
        log.info("[CELERY] Dedupe complete: {}/{} groups, {} users merged".format(
            groups_merged, len(groups), users_merged
        ))
        
        return {
            "status": "success",
            "groups_found": len(groups),
            "groups_merged": groups_merged,
            "users_merged": users_merged,
            "activities_updated": activities_updated,
            "task_id": self.request.id,
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
        log.error("[CELERY] Full dedupe failed: {}".format(type(e).__name__))
        self.update_state(state='FAILURE', meta={'error': str(e), 'elapsed_seconds': elapsed})
        raise