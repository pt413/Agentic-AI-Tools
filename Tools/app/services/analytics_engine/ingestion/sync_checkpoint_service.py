from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.analytics_engine.core.config import STAGING_SYNC_CHECKPOINT


class StagingSyncCheckpointService:
    def __init__(self, db: Session):
        self.db = db

    def get_checkpoint(self, sync_name: str) -> dict:
        row = self.db.execute(
            text(
                f"""
                SELECT
                    sync_name,
                    last_id,
                    last_timestamp,
                    updated_at,
                    last_batch_count,
                    last_status,
                    notes
                FROM {STAGING_SYNC_CHECKPOINT}
                WHERE sync_name = :sync_name
                """
            ),
            {"sync_name": sync_name},
        ).mappings().fetchone()

        if not row:
            return {
                "sync_name": sync_name,
                "last_id": 0,
                "last_timestamp": None,
                "updated_at": None,
                "last_batch_count": 0,
                "last_status": None,
                "notes": None,
            }

        return dict(row)

    def update_success(
        self,
        sync_name: str,
        *,
        last_id=None,
        last_timestamp=None,
        batch_count: int = 0,
        notes: str | None = None,
    ) -> None:
        self.db.execute(
            text(
                f"""
                INSERT INTO {STAGING_SYNC_CHECKPOINT}
                (
                    sync_name,
                    last_id,
                    last_timestamp,
                    updated_at,
                    last_batch_count,
                    last_status,
                    notes
                )
                VALUES
                (
                    :sync_name,
                    :last_id,
                    :last_timestamp,
                    NOW(),
                    :batch_count,
                    'SUCCESS',
                    :notes
                )
                ON CONFLICT (sync_name)
                DO UPDATE SET
                    last_id = EXCLUDED.last_id,
                    last_timestamp = EXCLUDED.last_timestamp,
                    updated_at = NOW(),
                    last_batch_count = EXCLUDED.last_batch_count,
                    last_status = EXCLUDED.last_status,
                    notes = EXCLUDED.notes
                """
            ),
            {
                "sync_name": sync_name,
                "last_id": last_id,
                "last_timestamp": last_timestamp,
                "batch_count": int(batch_count),
                "notes": notes,
            },
        )
        self.db.commit()

    def update_failure(self, sync_name: str, error_msg: str) -> None:
        self.db.execute(
            text(
                f"""
                INSERT INTO {STAGING_SYNC_CHECKPOINT}
                (
                    sync_name,
                    updated_at,
                    last_status,
                    notes
                )
                VALUES
                (
                    :sync_name,
                    NOW(),
                    'FAILED',
                    :notes
                )
                ON CONFLICT (sync_name)
                DO UPDATE SET
                    updated_at = NOW(),
                    last_status = 'FAILED',
                    notes = EXCLUDED.notes
                """
            ),
            {
                "sync_name": sync_name,
                "notes": error_msg[:4000] if error_msg else None,
            },
        )
        self.db.commit()