from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.pool import NullPool

from app.database.engine import create_database_engine


ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
RUNTIME_BACKFILL_SOURCE_KEY = "runtime-library-cutover-v1"


def upgrade_schema(database_url: str) -> None:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    engine = create_database_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def backfill_legacy_library(database_url: str) -> None:
    engine = create_database_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            if connection.execute(
                text(
                    "SELECT source_key FROM legacy_import_sources "
                    "WHERE source_key = :source_key"
                ),
                {"source_key": RUNTIME_BACKFILL_SOURCE_KEY},
            ).first() is not None:
                return
            tables = set(inspect(connection).get_table_names())
            track_count = int(
                connection.execute(text("SELECT COUNT(*) FROM tracks")).scalar_one()
            )
            metadata_count = (
                int(
                    connection.execute(
                        text("SELECT COUNT(*) FROM metadata_only_tracks")
                    ).scalar_one()
                )
                if "metadata_only_tracks" in tables
                else 0
            )
            normalized_library_count = sum(
                int(
                    connection.execute(
                        text(f"SELECT COUNT(*) FROM {table_name}")
                    ).scalar_one()
                )
                for table_name in ("library_entries", "user_playlists")
            )
        if track_count == 0 and metadata_count == 0:
            return

        from app.database.legacy_backfill import run_legacy_backfill

        run_legacy_backfill(
            database_url,
            database_url,
            RUNTIME_BACKFILL_SOURCE_KEY,
            apply=True,
            merge_existing=normalized_library_count > 0,
        )
    finally:
        engine.dispose()
