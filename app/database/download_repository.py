"""Persistence operations for the download job queue."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from app.database.engine import create_database_engine
from app.database.models import DownloadJob, User


ACTIVE_STATUSES = ("pending", "processing", "converting", "uploading")


class DownloadRepository:
    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.engine = engine or create_database_engine(database_url)

    def close(self) -> None:
        self.engine.dispose()

    def _ensure_user(self, connection: Any, user_id: int) -> None:
        if self.engine.dialect.name == "sqlite":
            connection.execute(
                User.__table__.insert()
                .values(telegram_user_id=user_id)
                .prefix_with("OR IGNORE")
            )
        else:
            from sqlalchemy.dialects.postgresql import insert

            connection.execute(
                insert(User.__table__)
                .values(telegram_user_id=user_id)
                .on_conflict_do_nothing()
            )

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def enqueue(
        self,
        user_id: int,
        source_url: str,
        *,
        track_id: int | None = None,
        batch_id: str | None = None,
    ) -> int | None:
        source_url = source_url.strip()
        if not source_url:
            raise ValueError("source_url cannot be empty")

        with self.engine.begin() as connection:
            self._ensure_user(connection, user_id)
            duplicate = connection.execute(
                select(DownloadJob.id)
                .where(
                    DownloadJob.user_id == user_id,
                    DownloadJob.source_url == source_url,
                    DownloadJob.status.in_(ACTIVE_STATUSES),
                )
                .limit(1)
            ).first()
            if duplicate is not None:
                return None
            job = connection.execute(
                DownloadJob.__table__.insert()
                .values(
                    user_id=user_id,
                    track_id=track_id,
                    batch_id=batch_id,
                    source_url=source_url,
                    status="pending",
                    progress=0,
                    attempt_count=0,
                    created_at=self._utcnow(),
                    updated_at=self._utcnow(),
                )
                .returning(DownloadJob.id)
            ).scalar_one()
            return int(job)

    def enqueue_batch(
        self, user_id: int, source_urls: list[str], track_ids: list[int | None] | None = None
    ) -> list[int]:
        batch_id = uuid4().hex
        track_ids = track_ids or [None] * len(source_urls)
        if len(track_ids) != len(source_urls):
            raise ValueError("track_ids and source_urls must have the same length")
        return [
            job_id
            for job_id in (
                self.enqueue(
                    user_id,
                    source_url,
                    track_id=track_id,
                    batch_id=batch_id,
                )
                for source_url, track_id in zip(source_urls, track_ids)
            )
            if job_id is not None
        ]

    def get(self, user_id: int, job_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(DownloadJob).where(
                    DownloadJob.id == job_id,
                    DownloadJob.user_id == user_id,
                )
            ).mappings().first()
            return dict(row) if row is not None else None

    def list_for_user(self, user_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(DownloadJob)
                .where(DownloadJob.user_id == user_id)
                .order_by(DownloadJob.created_at.desc(), DownloadJob.id.desc())
            ).mappings()
            return [dict(row) for row in rows]

    def claim_next(self) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                select(DownloadJob)
                .where(DownloadJob.status == "pending")
                .order_by(DownloadJob.created_at, DownloadJob.id)
                .limit(1)
                .with_for_update()
            ).mappings().first()
            if row is None:
                return None
            job_id = int(row["id"])
            now = self._utcnow()
            connection.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == job_id,
                    DownloadJob.status == "pending",
                )
                .values(
                    status="processing",
                    started_at=now,
                    attempt_count=DownloadJob.attempt_count + 1,
                    updated_at=now,
                    error=None,
                )
            )
            return self._get_mapped(connection, job_id)

    def _get_mapped(self, connection: Any, job_id: int) -> dict[str, Any]:
        row = connection.execute(
            select(DownloadJob).where(DownloadJob.id == job_id)
        ).mappings().one()
        return dict(row)

    def set_status(
        self,
        job_id: int,
        status: str,
        *,
        progress: int | None = None,
        error: str | None = None,
    ) -> bool:
        allowed = {
            "pending",
            "processing",
            "converting",
            "uploading",
            "completed",
            "failed",
            "cancelled",
        }
        if status not in allowed:
            raise ValueError("invalid download job status")
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        values: dict[str, Any] = {
            "status": status,
            "updated_at": self._utcnow(),
            "error": error,
        }
        if progress is not None:
            values["progress"] = progress
        if status in ("completed", "failed", "cancelled"):
            values["completed_at"] = self._utcnow()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(DownloadJob).where(DownloadJob.id == job_id).values(**values)
            )
            return result.rowcount == 1

    def cancel(self, user_id: int, job_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == job_id,
                    DownloadJob.user_id == user_id,
                    DownloadJob.status.in_(ACTIVE_STATUSES),
                )
                .values(
                    status="cancelled",
                    completed_at=self._utcnow(),
                    updated_at=self._utcnow(),
                )
            )
            return result.rowcount == 1

    def retry(self, user_id: int, job_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == job_id,
                    DownloadJob.user_id == user_id,
                    DownloadJob.status.in_(("failed", "cancelled")),
                )
                .values(
                    status="pending",
                    progress=0,
                    error=None,
                    started_at=None,
                    completed_at=None,
                    updated_at=self._utcnow(),
                )
            )
            return result.rowcount == 1
