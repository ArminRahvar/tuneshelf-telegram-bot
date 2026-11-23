import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from database import Database
from postgres_database import PostgresDatabase


class RepositoryContractAssertions:
    def assert_repository_contract(self, repository) -> None:
        owner_id = 4201
        other_user_id = 4202
        self.assertTrue(repository.create_collection(owner_id, "Favorites"))
        self.assertFalse(repository.create_collection(owner_id, "favorites"))
        self.assertTrue(repository.create_collection(owner_id, "Road trip"))
        self.assertTrue(repository.create_collection(other_user_id, "Favorites"))

        playlists = repository.list_collections(owner_id)
        favorites_id = next(
            row["id"] for row in playlists if row["name"] == "Favorites"
        )
        road_trip_id = next(
            row["id"] for row in playlists if row["name"] == "Road trip"
        )
        self.assertIsNone(repository.get_playlist(other_user_id, favorites_id))

        self.assertTrue(
            repository.add_track_to_playlist(
                owner_id,
                favorites_id,
                -100123,
                81,
                "Parity track",
                "Parity artist",
                "audio",
                "parity-file",
                "Parity album",
                "00000000-0000-0000-0000-000000000001",
                "sendable-file",
                245,
                "audio/mpeg",
                "parity.mp3",
                2024,
            )
        )
        track = repository.list_tracks_by_id(owner_id, favorites_id)[0]
        track_id = int(track["id"])
        self.assertEqual(
            repository.get_track_id_for_source_reference(
                owner_id,
                favorites_id,
                -100123,
                81,
            ),
            track_id,
        )
        self.assertFalse(
            repository.add_track_to_playlist(
                owner_id,
                favorites_id,
                -100123,
                82,
                "Duplicate copy",
                "Parity artist",
                "audio",
                "parity-file",
            )
        )
        self.assertEqual(len(repository.list_message_references(owner_id, track_id)), 2)

        archive_chat_id = -100987654
        self.assertTrue(
            repository.save_shared_archive_asset(
                archive_chat_id,
                902,
                "parity-file",
                "audio",
                original_filename="parity.mp3",
            )
        )
        self.assertTrue(
            repository.add_message_reference(
                owner_id,
                track_id,
                archive_chat_id,
                902,
                "audio",
                file_unique_id="parity-file",
            )
        )
        self.assertEqual(
            repository.find_shared_archive_reference(
                archive_chat_id,
                "parity-file",
            )["archive_message_id"],
            902,
        )
        self.assertEqual(
            repository.get_available_message_reference(
                owner_id,
                track_id,
                preferred_chat_id=archive_chat_id,
            )["source_message_id"],
            902,
        )
        other_favorites_id = next(
            row["id"]
            for row in repository.list_collections(other_user_id)
            if row["name"] == "Favorites"
        )
        self.assertTrue(
            repository.add_track_to_playlist(
                other_user_id,
                other_favorites_id,
                -100456,
                91,
                "Other user's track",
                "Other artist",
                "audio",
                "other-file",
            )
        )
        other_track_id = int(
            repository.list_tracks_by_id(other_user_id, other_favorites_id)[0]["id"]
        )
        self.assertTrue(
            repository.add_message_reference(
                other_user_id,
                other_track_id,
                archive_chat_id,
                902,
                "audio",
                file_unique_id="parity-file",
            )
        )
        archive_reference = next(
            reference
            for reference in repository.list_message_references(owner_id, track_id)
            if reference["source_chat_id"] == archive_chat_id
        )
        self.assertTrue(
            repository.mark_message_reference_unavailable(
                owner_id,
                int(archive_reference["id"]),
            )
        )
        self.assertIsNone(
            repository.find_shared_archive_reference(
                archive_chat_id,
                "parity-file",
            )
        )
        other_archive_reference = next(
            reference
            for reference in repository.list_message_references(
                other_user_id,
                other_track_id,
            )
            if reference["source_chat_id"] == archive_chat_id
        )
        self.assertTrue(
            repository.mark_message_reference_verified(
                other_user_id,
                int(other_archive_reference["id"]),
            )
        )
        self.assertIsNotNone(
            repository.find_shared_archive_reference(
                archive_chat_id,
                "parity-file",
            )
        )

        self.assertTrue(repository.set_track_rating(owner_id, track_id, 5))
        self.assertTrue(repository.set_track_note(owner_id, track_id, "Parity note"))
        self.assertTrue(repository.add_track_tag(owner_id, track_id, "Road music"))
        with self.assertRaises(ValueError):
            repository.set_track_rating(owner_id, track_id, 6)
        self.assertFalse(repository.set_track_rating(other_user_id, track_id, 4))

        self.assertTrue(repository.add_track_to_collection(owner_id, track_id, road_trip_id))
        copied_track = repository.list_tracks_by_id(owner_id, road_trip_id)[0]
        self.assertEqual(copied_track["rating"], 5)
        self.assertEqual(copied_track["note"], "Parity note")
        self.assertEqual(copied_track["release_year"], 2024)
        self.assertEqual(repository.list_track_tags(owner_id, copied_track["id"]), ["Road music"])
        self.assertEqual(
            len(repository.list_message_references(owner_id, copied_track["id"])),
            3,
        )
        self.assertFalse(
            repository.add_track_to_collection(
                other_user_id,
                track_id,
                road_trip_id,
            )
        )
        copied_track_id = int(copied_track["id"])
        self.assertFalse(
            repository.remove_track_by_id(
                other_user_id,
                road_trip_id,
                copied_track_id,
            )
        )
        self.assertTrue(
            repository.remove_track_by_id(
                owner_id,
                road_trip_id,
                copied_track_id,
            )
        )
        self.assertEqual(repository.list_tracks_by_id(owner_id, road_trip_id), [])

        self.assertFalse(
            repository.rename_collection(other_user_id, road_trip_id, "Hijacked")
        )
        self.assertFalse(
            repository.rename_collection(owner_id, road_trip_id, "Favorites")
        )
        self.assertTrue(
            repository.rename_collection(owner_id, road_trip_id, "Road trip renamed")
        )
        self.assertEqual(
            repository.get_playlist(owner_id, road_trip_id)["name"],
            "Road trip renamed",
        )

        self.assertTrue(repository.create_collection(owner_id, "Temporary"))
        temporary_id = next(
            row["id"]
            for row in repository.list_collections(owner_id)
            if row["name"] == "Temporary"
        )
        self.assertFalse(repository.delete_collection(other_user_id, temporary_id))
        self.assertTrue(repository.delete_collection(owner_id, temporary_id))
        self.assertIsNone(repository.get_playlist(owner_id, temporary_id))

        metadata_id = repository.create_metadata_only_track(
            owner_id,
            "Metadata only",
            "Another artist",
            "Unreleased album",
            "00000000-0000-0000-0000-000000000002",
            "https://music.example/track/1",
            2023,
        )
        self.assertEqual(
            repository.list_metadata_only_tracks(owner_id)[0]["id"],
            metadata_id,
        )
        self.assertEqual(
            repository.list_metadata_only_tracks(owner_id)[0]["title"],
            "Metadata only",
        )

        self.assertEqual(repository.get_delivery_mode(owner_id), "copy")
        self.assertTrue(repository.set_delivery_mode(owner_id, "forward"))
        self.assertEqual(repository.get_delivery_mode(owner_id), "forward")
        with self.assertRaises(ValueError):
            repository.set_delivery_mode(owner_id, "download")


class SQLiteRepositoryParityTests(
    unittest.TestCase,
    RepositoryContractAssertions,
):
    def test_sqlite_repository_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Database(Path(directory) / "parity.sqlite3")
            self.assert_repository_contract(repository)


class PostgreSQLRepositoryParityTests(
    unittest.TestCase,
    RepositoryContractAssertions,
):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ.get("TEST_POSTGRES_URL", "").strip()
        if not cls.database_url:
            raise unittest.SkipTest("Set TEST_POSTGRES_URL to run PostgreSQL parity tests.")

        try:
            import psycopg
            from psycopg import sql
        except ImportError as error:
            raise unittest.SkipTest("PostgreSQL parity tests require psycopg.") from error

        cls.schema = f"parity_{uuid.uuid4().hex}"
        with patch.dict(os.environ, {"PGOPTIONS": ""}):
            with psycopg.connect(cls.database_url) as connection:
                connection.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
                )

    @classmethod
    def tearDownClass(cls) -> None:
        if not getattr(cls, "database_url", "") or not getattr(cls, "schema", ""):
            return
        import psycopg
        from psycopg import sql

        with patch.dict(os.environ, {"PGOPTIONS": ""}):
            with psycopg.connect(cls.database_url) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(cls.schema)
                    )
                )

    def test_postgresql_repository_contract(self) -> None:
        with patch.dict(
            os.environ,
            {"PGOPTIONS": f"-c search_path={self.schema}"},
        ):
            repository = PostgresDatabase(self.database_url)
            self.assert_repository_contract(repository)


if __name__ == "__main__":
    unittest.main()
