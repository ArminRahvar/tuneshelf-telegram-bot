"""Add PostgreSQL-backed download job queue.

Revision ID: a7c3e2f1d4b6
Revises: 1f05b6c23d8a
Create Date: 2026-11-24 22:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c3e2f1d4b6"
down_revision: Union[str, Sequence[str], None] = "1f05b6c23d8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "download_jobs",
        sa.Column("id", id_type, autoincrement=True, nullable=False),
        sa.Column("user_id", id_type, nullable=False),
        sa.Column("track_id", id_type, nullable=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "progress",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'converting', 'uploading', "
            "'completed', 'failed', 'cancelled')",
            name="ck_download_jobs_status",
        ),
        sa.CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="ck_download_jobs_progress",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_download_jobs_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["catalog_tracks.id"],
            name="fk_download_jobs_track",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_download_jobs_status_created",
        "download_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_download_jobs_user_status",
        "download_jobs",
        ["user_id", "status"],
    )
    op.create_index("ix_download_jobs_batch", "download_jobs", ["batch_id"])


def downgrade() -> None:
    op.drop_table("download_jobs")
