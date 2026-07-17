from celery import shared_task
from app.services.unify_data.sync_service import run_sync_once

@shared_task(name="app.tasks.unified_sync_service.run_unified_sync")
def run_unified_sync():
    summary = run_sync_once(batch_size=20)
    return summary
