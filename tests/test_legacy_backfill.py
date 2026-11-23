import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database.engine import create_database_engine
from app.database.legacy_backfill import run_legacy_backfill
from app.database.models import (
    CatalogTrack,
    LegacyImportMap,
    LegacyImportSource,
    LibraryEntry,
    PlaylistItem,
    TelegramSourceReference,
    UserPreference,
    UserTag,
    UserTrackNote,
    UserTrackRating,
    UserTrackTag,
)
from database import Database


ROOT = Path(__file__).resolve().parents[1]


class LegacyBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.source_path = Path(self.directory.name) / "legacy.sqlite3"
        self.target_path = Path(self.directory.name) / "target.sqlite3"
        self.source_url = f"sqlite:///{self.source_path.as_posix()}"
        self.target_url = f"sqlite:///{self.target_path.as_posix()}"
        self.legacy = Database(self.source_path)
        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(os.environ, {"DATABASE_URL": self.target_url}):
            command.upgrade(config, "head")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _seed(self) -> tuple[int, int, int]:
        self.legacy.create_playlist(42, "Favorites")
        self.legacy.create_playlist(42, "Road trip")
        track_id = self.legacy.add_track(
            42,
            "Favorites",
            100,
            200,
            "First track",
            "Artist",
            "audio",
            "unique-file",
            "Album",
            "00000000-0000-0000-0000-000000000001",
            "telegram-file",
            210,
            "audio/mpeg",
            "track.mp3",
            2024,
        )
        second_track_id = self.legacy.add_track(
            42,
            "Road trip",
            100,
            200,
            "Second library entry",
            "Artist",
            "audio",
            "unique-file",
        )
        self.legacy.set_track_rating(42, track_id, 5)
        self.legacy.set_track_note(42, track_id, "Keep this note")
        self.legacy.add_track_tag(42, track_id, "favorites")
        self.legacy.add_track_tag(42, track_id, "unused")
        self.legacy.remove_track_tag(42, track_id, "unused")
        self.legacy.set_delivery_mode(42, "forward")
        metadata_track_id = self.legacy.create_metadata_only_track(
            42,
            "Provider-only result",
            "Other artist",
            "Provider album",
            "00000000-0000-0000-0000-000000000002",
            "https://music.example/track/1",
            2023,
        )
        return track_id, second_track_id, metadata_track_id

    def _target_engine(self):
        return create_database_engine(self.target_url)

    def test_dry_run_does_not_change_target(self) -> None:
        self._seed()
        engine = self._target_engine()
        try:
            with engine.connect() as connection:
                before = connection.execute(
                    text("SELECT COUNT(*) FROM catalog_tracks")
                ).scalar_one()
            report = run_legacy_backfill(
                self.source_url,
                self.target_url,
                "local-library",
            )
            with engine.connect() as connection:
                after = connection.execute(
                    text("SELECT COUNT(*) FROM catalog_tracks")
                ).scalar_one()
                imports = connection.execute(
                    select(LegacyImportSource.source_key)
                ).all()
            self.assertEqual(report["status"], "dry_run")
            self.assertEqual(report["counts"]["tracks"], 2)
            self.assertEqual(report["counts"]["metadata_tracks"], 1)
            self.assertEqual(before, after)
            self.assertEqual(imports, [])
        finally:
            engine.dispose()

    def test_imports_early_sqlite_schema_without_optional_tables(self) -> None:
        old_source_path = Path(self.directory.name) / "version1.sqlite3"
        with sqlite3.connect(old_source_path) as connection:
            connection.executescript(
                """
                CREATE TABLE playlists (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY,
                    playlist_id INTEGER NOT NULL,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    performer TEXT,
                    media_type TEXT NOT NULL,
                    file_unique_id TEXT,
                    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO playlists (id, user_id, name) VALUES (9, 77, 'Old');
                INSERT INTO tracks (
                    id, playlist_id, source_chat_id, source_message_id,
                    title, performer, media_type, file_unique_id
                ) VALUES (12, 9, 101, 202, 'Version one track',
                          'Version one artist', 'audio', 'old-file');
                PRAGMA user_version = 1;
                """
            )

        report = run_legacy_backfill(
            f"sqlite:///{old_source_path.as_posix()}",
            self.target_url,
            "version-one",
            apply=True,
        )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["sqlite_schema_version"], 1)
        engine = self._target_engine()
        try:
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        select(CatalogTrack.title)
                    ).scalar_one(),
                    "Version one track",
                )
                self.assertEqual(
                    connection.execute(
                        select(TelegramSourceReference.telegram_file_unique_id)
                    ).scalar_one(),
                    "old-file",
                )
        finally:
            engine.dispose()

    def test_apply_preserves_data_allows_reused_message_and_is_idempotent(self) -> None:
        legacy_track_id, second_track_id, metadata_track_id = self._seed()
        report = run_legacy_backfill(
            self.source_url,
            self.target_url,
            "local-library",
            apply=True,
        )
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["counts"]["tracks_imported"], 2)
        self.assertEqual(report["counts"]["metadata_tracks_imported"], 1)

        engine = self._target_engine()
        try:
            with engine.connect() as connection:
                source_track = connection.execute(
                    select(LegacyImportMap.target_id).where(
                        LegacyImportMap.source_key == "local-library",
                        LegacyImportMap.entity_type == "track",
                        LegacyImportMap.legacy_id == legacy_track_id,
                    )
                ).scalar_one()
                second_target_track = connection.execute(
                    select(LegacyImportMap.target_id).where(
                        LegacyImportMap.source_key == "local-library",
                        LegacyImportMap.entity_type == "track",
                        LegacyImportMap.legacy_id == second_track_id,
                    )
                ).scalar_one()
                metadata_target_track = connection.execute(
                    select(LegacyImportMap.target_id).where(
                        LegacyImportMap.source_key == "local-library",
                        LegacyImportMap.entity_type == "metadata_track",
                        LegacyImportMap.legacy_id == metadata_track_id,
                    )
                ).scalar_one()
                self.assertNotEqual(source_track, second_target_track)
                self.assertEqual(
                    connection.execute(
                        select(CatalogTrack.title).where(
                            CatalogTrack.id == source_track
                        )
                    ).scalar_one(),
                    "First track",
                )
                self.assertEqual(
                    connection.execute(
                        select(UserTrackRating.rating).where(
                            UserTrackRating.user_id == 42,
                            UserTrackRating.track_id == source_track,
                        )
                    ).scalar_one(),
                    5,
                )
                self.assertEqual(
                    connection.execute(
                        select(UserTrackNote.note).where(
                            UserTrackNote.user_id == 42,
                            UserTrackNote.track_id == source_track,
                        )
                    ).scalar_one(),
                    "Keep this note",
                )
                self.assertEqual(
                    connection.execute(
                        select(UserTrackTag.track_id).where(
                            UserTrackTag.user_id == 42,
                            UserTrackTag.track_id == source_track,
                        )
                    ).scalar_one(),
                    source_track,
                )
                self.assertEqual(
                    set(
                        connection.execute(
                            select(UserTag.name).where(UserTag.user_id == 42)
                        ).scalars()
                    ),
                    {"favorites", "unused"},
                )
                refs = connection.execute(
                    select(TelegramSourceReference.track_id).where(
                        TelegramSourceReference.user_id == 42,
                        TelegramSourceReference.source_chat_id == 100,
                        TelegramSourceReference.source_message_id == 200,
                    )
                ).scalars().all()
                self.assertEqual(set(refs), {source_track, second_target_track})
                self.assertEqual(
                    connection.execute(
                        select(UserPreference.delivery_mode).where(
                            UserPreference.user_id == 42
                        )
                    ).scalar_one(),
                    "forward",
                )
                self.assertEqual(
                    connection.execute(
                        select(LibraryEntry.track_id).where(
                            LibraryEntry.track_id == metadata_target_track
                        )
                    ).scalar_one(),
                    metadata_target_track,
                )
                self.assertEqual(
                    connection.execute(
                        select(PlaylistItem.track_id).where(
                            PlaylistItem.track_id == source_track
                        )
                    ).scalar_one(),
                    source_track,
                )
                self.assertEqual(
                    connection.execute(
                        select(TelegramSourceReference.mime_type).where(
                            TelegramSourceReference.track_id == source_track
                        )
                    ).scalar_one(),
                    "audio/mpeg",
                )
        finally:
            engine.dispose()

        repeated = run_legacy_backfill(
            self.source_url,
            self.target_url,
            "local-library",
            apply=True,
        )
        self.assertEqual(repeated["status"], "already_imported")

        self.legacy.create_playlist(42, "Changed after import")
        with self.assertRaisesRegex(ValueError, "source changed"):
            run_legacy_backfill(
                self.source_url,
                self.target_url,
                "local-library",
                apply=True,
            )

    def test_apply_rolls_back_everything_when_target_conflicts(self) -> None:
        self._seed()
        engine = self._target_engine()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO users (telegram_user_id) VALUES (42)")
                )
                connection.execute(
                    text(
                        "INSERT INTO user_playlists (user_id, name) "
                        "VALUES (42, 'favorites')"
                    )
                )
        finally:
            engine.dispose()

        with self.assertRaises(IntegrityError):
            run_legacy_backfill(
                self.source_url,
                self.target_url,
                "local-library",
                apply=True,
            )

        engine = self._target_engine()
        try:
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        text("SELECT COUNT(*) FROM catalog_tracks")
                    ).scalar_one(),
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        select(LegacyImportSource.source_key)
                    ).all(),
                    [],
                )
                self.assertEqual(
                    connection.execute(
                        text("SELECT COUNT(*) FROM user_playlists")
                    ).scalar_one(),
                    1,
                )
        finally:
            engine.dispose()

    def test_downgrade_is_blocked_after_import_data_exists(self) -> None:
        self._seed()
        run_legacy_backfill(
            self.source_url,
            self.target_url,
            "local-library",
            apply=True,
        )
        config = Config(str(ROOT / "alembic.ini"))
        with patch.dict(os.environ, {"DATABASE_URL": self.target_url}):
            with self.assertRaisesRegex(RuntimeError, "imported mappings"):
                command.downgrade(config, "3ac461e85b53")

        engine = self._target_engine()
        try:
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        select(LegacyImportSource.source_key)
                    ).scalar_one(),
                    "local-library",
                )
                self.assertEqual(
                    connection.execute(
                        select(LegacyImportMap.source_key)
                    ).first()[0],
                    "local-library",
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
