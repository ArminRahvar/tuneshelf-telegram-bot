import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database.legacy_inventory import inspect_legacy_database
from database import Database


class LegacyInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "legacy.sqlite3"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_inventory_reports_counts_and_conflicts_without_writing(self) -> None:
        database = Database(self.database_path)
        database.create_playlist(7, "First")
        database.create_playlist(7, "Second")
        database.add_track(7, "First", 7, 42, "Song", "Artist", "audio", "same-file")
        database.add_track(7, "Second", 7, 42, "Song copy", "Artist", "audio", "same-file")

        report = inspect_legacy_database(
            f"sqlite:///{self.database_path.as_posix()}"
        )

        self.assertEqual(report["dialect"], "sqlite")
        self.assertEqual(report["sqlite_schema_version"], 8)
        self.assertEqual(report["tables"]["playlists"], 2)
        self.assertEqual(report["tables"]["tracks"], 2)
        self.assertEqual(report["integrity"]["tracks_without_playlist"], 0)
        self.assertEqual(report["integrity"]["playlists_without_user"], 0)
        self.assertEqual(report["integrity"]["duplicate_file_unique_groups"], 1)
        self.assertEqual(report["integrity"]["source_message_reused_across_tracks"], 1)
        self.assertEqual(report["integrity"]["reference_owner_mismatches"], 0)
        self.assertEqual(len(database.list_tracks(7, "First")), 1)

    def test_inventory_rejects_a_database_without_legacy_tables(self) -> None:
        sqlite3.connect(self.database_path).close()

        with self.assertRaisesRegex(ValueError, "missing required tables"):
            inspect_legacy_database(
                f"sqlite:///{self.database_path.as_posix()}"
            )

    def test_inventory_does_not_create_a_missing_sqlite_database(self) -> None:
        missing_path = Path(self.directory.name) / "missing.sqlite3"

        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            inspect_legacy_database(f"sqlite:///{missing_path.as_posix()}")

        self.assertFalse(missing_path.exists())


if __name__ == "__main__":
    unittest.main()
