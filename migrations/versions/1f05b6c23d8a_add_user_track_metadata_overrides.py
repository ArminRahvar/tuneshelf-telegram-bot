"""Keep metadata edits scoped to each user's library entry.

Revision ID: 1f05b6c23d8a
Revises: 65e43ad74b4d
Create Date: 2026-11-24 02:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1f05b6c23d8a"
down_revision: Union[str, Sequence[str], None] = "65e43ad74b4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "user_track_metadata",
        sa.Column("user_id", id_type, nullable=False),
        sa.Column("track_id", id_type, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("performer", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_user_track_metadata_title_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            name="fk_user_track_metadata_library_entry",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "track_id"),
    )


def downgrade() -> None:
    op.drop_table("user_track_metadata")
