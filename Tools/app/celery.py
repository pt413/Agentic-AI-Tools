import os
from celery import Celery
from celery.schedules import crontab


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")

broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
backend_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/1"


celery_app = Celery(
    "embedding_worker",
    broker=broker_url,
    backend=backend_url,
    include=[
        "app.tasks.unified_sync_service",
        # "app.tasks.bge_embedding_task",
        # "app.tasks.email_risk_pipeline_task",
    ],
)


celery_app.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=False,
    task_track_started=True,
    task_time_limit=60 * 60 * 6,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)


celery_app.conf.beat_schedule = {

    
    "run-unified-sync-every-30s": {
        "task": "app.tasks.unified_sync_service.run_unified_sync",
        "schedule": 30.0,
    },

    
    # "run-bge-embedding-daily-11am": {
    #     "task": "app.tasks.bge_embedding_task.run_bge_embedding",
    #     "schedule": crontab(minute="*/40"),
    # },

    
    # "run-email-risk-pipeline": {
    #     "task": "app.tasks.email_risk_pipeline_task.run_email_risk_pipeline",
    #     "schedule": 600.0,
    # },
}


@celery_app.task(name="app.celery.debug_task")
def debug_task():
    return "Celery is working"