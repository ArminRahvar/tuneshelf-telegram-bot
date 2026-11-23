"""add legacy import support

Revision ID: bc917af50d62
Revises: 3ac461e85b53
Create Date: 2026-11-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bc917af50d62"
down_revision: Union[str, Sequence[str], None] = "3ac461e85b53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_source_references",
        sa.Column("mime_type", sa.String(length=255), nullable=True),
    )
    with op.batch_alter_table("telegram_source_references") as batch_op:
        batch_op.drop_constraint("uq_source_reference_message", type_="unique")
        batch_op.create_unique_constraint(
            "uq_source_reference_message",
            [
                "user_id",
                "track_id",
                "source_chat_id",
                "source_message_id",
            ],
        )

    op.create_table(
        "user_preferences",
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("delivery_mode", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "delivery_mode IN ('copy', 'forward')",
            name="ck_user_preference_delivery_mode",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "legacy_import_sources",
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_dialect", sa.String(length=16), nullable=False),
        sa.Column("sqlite_schema_version", sa.Integer(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(source_key)) BETWEEN 1 AND 128",
            name="ck_legacy_import_source_key",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64",
            name="ck_legacy_import_source_fingerprint",
        ),
        sa.CheckConstraint(
            "source_dialect IN ('sqlite', 'postgresql')",
            name="ck_legacy_import_source_dialect",
        ),
        sa.PrimaryKeyConstraint("source_key"),
    )
    op.create_table(
        "legacy_import_maps",
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=24), nullable=False),
        sa.Column(
            "legacy_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('playlist', 'track', 'metadata_track', 'tag')",
            name="ck_legacy_import_map_entity_type",
        ),
        sa.CheckConstraint(
            "length(trim(source_key)) BETWEEN 1 AND 128",
            name="ck_legacy_import_map_source_key",
        ),
        sa.CheckConstraint("legacy_id > 0", name="ck_legacy_import_map_legacy_id"),
        sa.CheckConstraint("target_id > 0", name="ck_legacy_import_map_target_id"),
        sa.ForeignKeyConstraint(
            ["source_key"],
            ["legacy_import_sources.source_key"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_key", "entity_type", "legacy_id"),
    )
    op.create_index(
        "ix_legacy_import_maps_target",
        "legacy_import_maps",
        ["entity_type", "target_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    map_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM legacy_import_maps")
    ).scalar_one()
    preference_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM user_preferences")
    ).scalar_one()
    source_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM legacy_import_sources")
    ).scalar_one()
    if map_count or preference_count or source_count:
        raise RuntimeError(
            "Cannot downgrade while imported mappings or user preferences exist."
        )

    op.drop_index("ix_legacy_import_maps_target", table_name="legacy_import_maps")
    op.drop_table("legacy_import_maps")
    op.drop_table("legacy_import_sources")
    op.drop_table("user_preferences")
    op.drop_column("telegram_source_references", "mime_type")
    with op.batch_alter_table("telegram_source_references") as batch_op:
        batch_op.drop_constraint("uq_source_reference_message", type_="unique")
        batch_op.create_unique_constraint(
            "uq_source_reference_message",
            ["user_id", "source_chat_id", "source_message_id"],
        )
