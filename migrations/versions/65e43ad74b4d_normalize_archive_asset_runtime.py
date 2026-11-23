"""Use normalized archive assets at runtime.

Revision ID: 65e43ad74b4d
Revises: 3ac461e85b53
Create Date: 2026-11-24 01:20:00.000000
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "65e43ad74b4d"
down_revision: Union[str, Sequence[str], None] = "bc917af50d62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp(value: object, dialect_name: str) -> datetime | str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if dialect_name == "sqlite":
        return parsed.isoformat(sep=" ")
    return parsed


def upgrade() -> None:
    with op.batch_alter_table("archive_assets") as batch_op:
        batch_op.alter_column("track_id", existing_type=sa.BigInteger(), nullable=True)
        batch_op.add_column(sa.Column("original_filename", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_archive_asset_file",
            ["archive_chat_id", "telegram_file_unique_id"],
        )

    if context.is_offline_mode():
        return

    connection = op.get_bind()
    if "telegram_archive_assets" in sa.inspect(connection).get_table_names():
        legacy_assets = connection.execute(
            sa.text(
                """
                SELECT archive_chat_id, archive_message_id, telegram_file_id,
                       telegram_file_unique_id, media_type, original_filename,
                       is_available, last_verified_at, created_at
                FROM telegram_archive_assets
                ORDER BY id
                """
            )
        ).mappings()
        for asset in legacy_assets:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO archive_assets (
                        track_id, archive_chat_id, archive_message_id,
                        telegram_file_id, telegram_file_unique_id, media_type,
                        is_available, last_verified_at, created_at, original_filename
                    ) VALUES (
                        NULL, :archive_chat_id, :archive_message_id,
                        :telegram_file_id, :telegram_file_unique_id, :media_type,
                        :is_available, :last_verified_at, :created_at, :original_filename
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "archive_chat_id": asset["archive_chat_id"],
                    "archive_message_id": asset["archive_message_id"],
                    "telegram_file_id": asset["telegram_file_id"],
                    "telegram_file_unique_id": asset["telegram_file_unique_id"],
                    "media_type": asset["media_type"],
                    "is_available": bool(asset["is_available"]),
                    "last_verified_at": _timestamp(
                        asset["last_verified_at"],
                        connection.dialect.name,
                    ),
                    "created_at": _timestamp(
                        asset["created_at"],
                        connection.dialect.name,
                    ),
                    "original_filename": asset["original_filename"],
                },
            )

    connection.execute(
        sa.text(
            """
            UPDATE archive_assets
            SET track_id = (
                SELECT MIN(reference.track_id)
                FROM telegram_source_references AS reference
                WHERE reference.telegram_file_unique_id =
                      archive_assets.telegram_file_unique_id
                HAVING COUNT(DISTINCT reference.track_id) = 1
            )
            WHERE track_id IS NULL
              AND telegram_file_unique_id IS NOT NULL
              AND (
                SELECT COUNT(DISTINCT reference.track_id)
                FROM telegram_source_references AS reference
                WHERE reference.telegram_file_unique_id =
                      archive_assets.telegram_file_unique_id
              ) = 1
            """
        )
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        connection = op.get_bind()
        has_unlinked_assets = connection.execute(
            sa.text("SELECT 1 FROM archive_assets WHERE track_id IS NULL LIMIT 1")
        ).first()
        if has_unlinked_assets:
            raise RuntimeError(
                "Cannot downgrade while archive assets are not linked to catalog tracks."
            )

    with op.batch_alter_table("archive_assets") as batch_op:
        batch_op.drop_constraint("uq_archive_asset_file", type_="unique")
        batch_op.drop_column("original_filename")
        batch_op.alter_column("track_id", existing_type=sa.BigInteger(), nullable=False)
