import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from app.database.engine import create_database_engine


REQUIRED_TABLES = {"playlists", "tracks"}
OPTIONAL_TABLES = {
    "metadata_only_tracks",
    "pending_tracks",
    "tags",
    "telegram_message_references",
    "track_tags",
    "user_settings",
    "user_states",
    "users",
}


def _read_only_engine(url: str) -> Engine:
    parsed_url = make_url(url)
    if (
        parsed_url.get_backend_name() == "sqlite"
        and parsed_url.database not in (None, "", ":memory:")
        and not Path(parsed_url.database).is_file()
    ):
        raise FileNotFoundError("The SQLite source database does not exist.")
    return create_database_engine(url, poolclass=NullPool)


def _count(connection: Any, query: str) -> int:
    return int(connection.execute(text(query)).scalar_one())


def inspect_legacy_database(url: str) -> dict[str, Any]:
    engine = _read_only_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA query_only = ON")
                elif connection.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")

                inspector = inspect(connection)
                existing_tables = set(inspector.get_table_names())
                missing = REQUIRED_TABLES - existing_tables
                if missing:
                    names = ", ".join(sorted(missing))
                    raise ValueError(f"Legacy database is missing required tables: {names}.")

                tables = REQUIRED_TABLES | (OPTIONAL_TABLES & existing_tables)
                counts = {
                    name: _count(connection, f'SELECT COUNT(*) FROM "{name}"')
                    for name in sorted(tables)
                }
                integrity = {
                    "tracks_without_playlist": _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM tracks t
                        LEFT JOIN playlists p ON p.id = t.playlist_id
                        WHERE p.id IS NULL
                        """,
                    ),
                    "playlists_without_user": None,
                    "tag_owner_mismatches": None,
                    "reference_owner_mismatches": None,
                    "duplicate_file_unique_groups": _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM (
                            SELECT p.user_id, t.file_unique_id
                            FROM tracks t
                            JOIN playlists p ON p.id = t.playlist_id
                            WHERE t.file_unique_id IS NOT NULL
                            GROUP BY p.user_id, t.file_unique_id
                            HAVING COUNT(*) > 1
                        ) duplicate_groups
                        """,
                    ),
                    "source_message_reused_across_tracks": _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM (
                            SELECT p.user_id, t.source_chat_id, t.source_message_id
                            FROM tracks t
                            JOIN playlists p ON p.id = t.playlist_id
                            GROUP BY p.user_id, t.source_chat_id, t.source_message_id
                            HAVING COUNT(*) > 1
                        ) duplicate_groups
                        """,
                    ),
                }
                if "users" in existing_tables:
                    integrity["playlists_without_user"] = _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM playlists p
                        LEFT JOIN users u ON u.telegram_user_id = p.user_id
                        WHERE u.telegram_user_id IS NULL
                        """,
                    )
                if {"tags", "track_tags"} <= existing_tables:
                    integrity["tag_owner_mismatches"] = _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM track_tags link
                        JOIN tags tag ON tag.id = link.tag_id
                        JOIN tracks t ON t.id = link.track_id
                        JOIN playlists p ON p.id = t.playlist_id
                        WHERE tag.owner_user_id != p.user_id
                        """,
                    )
                if "telegram_message_references" in existing_tables:
                    integrity["reference_owner_mismatches"] = _count(
                        connection,
                        """
                        SELECT COUNT(*) FROM telegram_message_references ref
                        JOIN tracks t ON t.id = ref.track_id
                        JOIN playlists p ON p.id = t.playlist_id
                        WHERE ref.owner_user_id != p.user_id
                        """,
                    )

                report: dict[str, Any] = {
                    "dialect": connection.dialect.name,
                    "sqlite_schema_version": None,
                    "tables": counts,
                    "integrity": integrity,
                }
                if connection.dialect.name == "sqlite":
                    report["sqlite_schema_version"] = int(
                        connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                    )
                transaction.commit()
                return report
            except Exception:
                transaction.rollback()
                raise
    finally:
        engine.dispose()


def main() -> None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("Set DATABASE_URL to the legacy source database.")
    print(json.dumps(inspect_legacy_database(url), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
