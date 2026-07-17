import logging
from celery import shared_task

from app.scripts.build_bge_email_embeddings import run  

logger = logging.getLogger(__name__)


@shared_task(
    name="app.tasks.bge_embedding_task.run_bge_embedding",
    bind=True,
)
def run_bge_embedding(self):
    """
    Celery task wrapper for BGE embedding builder.
    """
    logger.info("Starting BGE embedding task...")
    try:
        run()
        logger.info("BGE embedding task completed successfully.")
    except Exception as e:
        logger.exception("BGE embedding task failed.")
        raise e