import logging
from celery import shared_task

from app.services.email_risk_pipeline_v2 import run_pipeline


logger = logging.getLogger(__name__)


@shared_task(
    name="app.tasks.email_risk_pipeline_task.run_email_risk_pipeline",
    bind=True,
)
def run_email_risk_pipeline(self):
    """
    Celery task wrapper for Email Risk Classification pipeline.
    """

    logger.info("Starting Email Risk Pipeline task...")

    try:
        run_pipeline()

        logger.info("Email Risk Pipeline completed successfully.")

    except Exception as e:

        logger.exception("Email Risk Pipeline failed.")

        raise e