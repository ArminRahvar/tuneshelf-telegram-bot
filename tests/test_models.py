import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.database.engine import create_database_engine
from app.database.models import (
    CatalogTrack,
    LibraryEntry,
    PlaylistItem,
    User,
    UserPlaylist,
)
from database import Database


ROOT = Path(__file__).resolve().parents[1]


class ModelMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "tuneshelf.sqlite3"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _upgrade(self) -> None:
        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(
            os.environ,
            {"DATABASE_URL": f"sqlite:///{self.database_path.as_posix()}"},
        ):
            command.upgrade(config, "head")

    def test_fresh_migration_creates_additive_schema_and_downgrade_keeps_users(self) -> None:
        self._upgrade()
        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(
            os.environ,
            {"DATABASE_URL": f"sqlite:///{self.database_path.as_posix()}"},
        ):
            command.check(config)
        engine = create_database_engine(f"sqlite:///{self.database_path.as_posix()}")
        try:
            tables = set(inspect(engine).get_table_names())
            self.assertTrue(
                {
                    "users",
                    "catalog_tracks",
                    "archive_assets",
                    "library_entries",
                    "user_playlists",
                    "playlist_items",
                    "telegram_source_references",
                }.issubset(tables)
            )
            with engine.begin() as connection:
                connection.execute(
                    User.__table__.insert().values(telegram_user_id=42)
                )
        finally:
            engine.dispose()

        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(
            os.environ,
            {"DATABASE_URL": f"sqlite:///{self.database_path.as_posix()}"},
        ):
            command.downgrade(config, "base")

        connection = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("users", tables)
            self.assertNotIn("catalog_tracks", tables)
            self.assertEqual(
                connection.execute(
                    "SELECT telegram_user_id FROM users"
                ).fetchone(),
                (42,),
            )
        finally:
            connection.close()

    def test_existing_sqlite_data_survives_upgrade_and_downgrade(self) -> None:
        legacy_database = Database(self.database_path)
        legacy_database.create_playlist(7, "Favorites")
        legacy_database.add_track(
            7, "Favorites", 7, 17, "Legacy track", "Artist", "audio", "legacy-file"
        )

        self._upgrade()

        tracks = legacy_database.list_tracks(7, "Favorites")
        self.assertEqual([track["title"] for track in tracks], ["Legacy track"])
        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(
            os.environ,
            {"DATABASE_URL": f"sqlite:///{self.database_path.as_posix()}"},
        ):
            command.check(config)
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                8,
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("catalog_tracks", tables)
            self.assertIn("tracks", tables)
        finally:
            connection.close()

        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(
            os.environ,
            {"DATABASE_URL": f"sqlite:///{self.database_path.as_posix()}"},
        ):
            command.downgrade(config, "base")

        self.assertEqual(
            legacy_database.list_tracks(7, "Favorites")[0]["title"],
            "Legacy track",
        )

    def test_playlist_item_cannot_reference_another_users_library_track(self) -> None:
        self._upgrade()
        engine = create_database_engine(f"sqlite:///{self.database_path.as_posix()}")
        try:
            with engine.begin() as connection:
                connection.execute(
                    User.__table__.insert(),
                    [
                        {"telegram_user_id": 1},
                        {"telegram_user_id": 2},
                    ],
                )
                connection.execute(
                    CatalogTrack.__table__.insert(),
                    [
                        {"id": 101, "title": "User one's track"},
                        {"id": 202, "title": "User two's track"},
                    ],
                )
                connection.execute(
                    LibraryEntry.__table__.insert(),
                    [
                        {"user_id": 1, "track_id": 101},
                        {"user_id": 2, "track_id": 202},
                    ],
                )
                playlist_result = connection.execute(
                    UserPlaylist.__table__.insert().values(
                        user_id=1,
                        name="Favorites",
                    )
                )
                playlist_id = playlist_result.inserted_primary_key[0]
                connection.execute(
                    PlaylistItem.__table__.insert().values(
                        playlist_id=playlist_id,
                        track_id=101,
                        user_id=1,
                        position=0,
                    )
                )

            with self.assertRaises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        UserPlaylist.__table__.insert().values(
                            user_id=1,
                            name="  ",
                        )
                    )

            with self.assertRaises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        UserPlaylist.__table__.insert().values(
                            user_id=1,
                            name="favorites",
                        )
                    )

            with self.assertRaises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        PlaylistItem.__table__.insert().values(
                            playlist_id=playlist_id,
                            track_id=202,
                            user_id=1,
                            position=1,
                        )
                    )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
