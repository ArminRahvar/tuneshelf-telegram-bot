import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database, PlaylistNotFoundError


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_playlist_and_track_references_are_scoped_to_user(self) -> None:
        self.assertTrue(self.database.create_playlist(10, "Favorites"))
        self.assertFalse(self.database.create_playlist(10, "favorites"))
        self.assertTrue(self.database.create_playlist(11, "Favorites"))

        self.database.add_track(
            user_id=10,
            playlist_name="Favorites",
            source_chat_id=10,
            source_message_id=42,
            title="Song",
            performer="Artist",
            media_type="audio",
            file_unique_id="unique",
        )

        tracks = self.database.list_tracks(10, "favorites")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["source_message_id"], 42)
        self.assertEqual(self.database.list_tracks(11, "Favorites"), [])
        self.assertEqual(self.database.list_playlists(10)[0]["track_count"], 1)

    def test_unknown_playlist_is_reported_when_adding_track(self) -> None:
        with self.assertRaises(PlaylistNotFoundError):
            self.database.add_track(10, "Missing", 10, 5, "Song", None, "audio", None)

    def test_remove_and_delete_playlist(self) -> None:
        self.database.create_playlist(10, "Queue")
        for message_id in (1, 2):
            self.database.add_track(10, "Queue", 10, message_id, "Song", None, "audio", None)

        self.assertTrue(self.database.remove_track(10, "Queue", 1))
        tracks = self.database.list_tracks(10, "Queue")
        self.assertEqual([track["source_message_id"] for track in tracks], [2])
        self.assertTrue(self.database.delete_playlist(10, "Queue"))
        self.assertIsNone(self.database.list_tracks(10, "Queue"))

    def test_rename_playlist_preserves_tracks_and_enforces_owner_and_unique_name(self) -> None:
        self.database.create_playlist(10, "Old name")
        self.database.create_playlist(10, "Taken")
        self.database.create_playlist(11, "Other user's list")
        self.database.add_track(10, "Old name", 10, 42, "Song", None, "audio", None)

        playlists = self.database.list_playlists(10)
        playlist_id = next(row["id"] for row in playlists if row["name"] == "Old name")

        self.assertFalse(self.database.rename_playlist(10, playlist_id, "tAKEN"))
        self.assertTrue(self.database.rename_playlist(10, playlist_id, "New name"))
        self.assertFalse(self.database.rename_playlist(11, playlist_id, "Hijacked"))
        self.assertEqual(
            self.database.list_tracks(10, "New name")[0]["source_message_id"],
            42,
        )
        self.assertIsNone(self.database.list_tracks(10, "Old name"))

    def test_same_source_message_is_only_added_once_per_playlist(self) -> None:
        self.database.create_playlist(10, "Queue")
        playlist_id = self.database.list_playlists(10)[0]["id"]
        track = {
            "user_id": 10,
            "playlist_id": playlist_id,
            "source_chat_id": 10,
            "source_message_id": 42,
            "title": "Song",
            "performer": None,
            "media_type": "audio",
            "file_unique_id": "file",
        }

        self.assertTrue(self.database.add_track_to_playlist(**track))
        self.assertFalse(self.database.add_track_to_playlist(**track))
        self.assertEqual(len(self.database.list_tracks_by_id(10, playlist_id)), 1)

    def test_same_file_in_a_collection_adds_reference_without_duplicate_track(self) -> None:
        self.database.create_playlist(10, "Queue")
        playlist_id = self.database.list_playlists(10)[0]["id"]
        self.assertTrue(
            self.database.add_track_to_playlist(
                10, playlist_id, 10, 42, "Song", "Artist", "audio", "file-unique"
            )
        )
        self.assertFalse(
            self.database.add_track_to_playlist(
                10, playlist_id, 10, 43, "Song copy", "Artist", "audio", "file-unique"
            )
        )

        tracks = self.database.list_tracks_by_id(10, playlist_id)
        self.assertEqual(len(tracks), 1)
        references = self.database.list_message_references(10, tracks[0]["id"])
        self.assertEqual([row["source_message_id"] for row in references], [42, 43])

    def test_search_tracks_matches_title_artist_or_playlist_with_user_scope(self) -> None:
        self.database.create_playlist(10, "Road Trips")
        self.database.create_playlist(11, "Private Mix")
        self.database.add_track(10, "Road Trips", 10, 41, "Northern Lights", "Nova", "audio", None)
        self.database.add_track(11, "Private Mix", 11, 41, "Northern Lights", "Nova", "audio", None)
        playlist_id = next(
            row["id"] for row in self.database.list_playlists(10) if row["name"] == "Road Trips"
        )
        self.database.add_track_to_playlist(
            10,
            playlist_id,
            10,
            42,
            "Another Song",
            "Nova",
            "audio",
            None,
            "Night Drives",
            "recording-id",
        )
        second_track = self.database.search_tracks(10, "night drives", 10)[0]
        self.database.add_track_tag(10, second_track["id"], "melancholic")

        self.assertEqual(len(self.database.search_tracks(10, "NORTH", 10)), 1)
        self.assertEqual(len(self.database.search_tracks(10, "nova", 10)), 2)
        self.assertEqual(len(self.database.search_tracks(10, "trips", 10)), 2)
        self.assertEqual(
            self.database.search_tracks(10, "night drives", 10)[0]["musicbrainz_recording_id"],
            "recording-id",
        )
        self.assertEqual(len(self.database.search_tracks(10, "private", 10)), 0)
        self.assertEqual(len(self.database.search_tracks(10, "%", 10)), 0)
        self.assertEqual(len(self.database.search_tracks(10, "melancholic", 10)), 1)
        self.assertIsNone(self.database.get_track(10, 2))
        self.assertEqual(self.database.get_track(11, 2)["playlist_name"], "Private Mix")

    def test_existing_version_one_database_is_upgraded_without_losing_tracks(self) -> None:
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE playlists (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            );
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                performer TEXT,
                media_type TEXT NOT NULL,
                file_unique_id TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX tracks_playlist_id_idx ON tracks(playlist_id, id);
            INSERT INTO playlists (id, user_id, name) VALUES (1, 7, 'Legacy');
            INSERT INTO tracks (
                playlist_id, source_chat_id, source_message_id, title, media_type
            ) VALUES (1, 7, 99, 'Kept song', 'audio');
            INSERT INTO tracks (
                playlist_id, source_chat_id, source_message_id, title, media_type
            ) VALUES (1, 7, 99, 'Duplicate song', 'audio');
            PRAGMA user_version = 1;
            """
        )
        connection.commit()
        connection.close()

        upgraded = Database(legacy_path)
        tracks = upgraded.list_tracks(7, "Legacy")
        connection = sqlite3.connect(legacy_path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()

        self.assertEqual(version, 7)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["title"], "Kept song")
        self.assertEqual(tracks[0]["source_message_id"], 99)
        self.assertEqual(upgraded.get_user_state(7), None)

    def test_multiple_message_references_keep_track_and_availability_is_scoped(self) -> None:
        self.database.create_playlist(10, "Collection")
        track_id = self.database.add_track(
            user_id=10,
            playlist_name="Collection",
            source_chat_id=-1001234567890,
            source_message_id=2**31 + 8,
            title="Recorded track",
            performer="Artist",
            media_type="audio",
            file_unique_id="unique-file",
            telegram_file_id="sendable-file-id",
            duration_seconds=245,
            mime_type="audio/mpeg",
            original_filename="song.mp3",
        )

        self.assertTrue(
            self.database.add_message_reference(
                10, track_id, -1001234567890, 70, "document", "new-file-id",
                "new-unique-id", "alternate.mp3",
            )
        )
        self.assertFalse(
            self.database.add_message_reference(
                10, track_id, -1001234567890, 70, "document"
            )
        )
        self.assertEqual(
            len(self.database.list_message_references(10, track_id)),
            2,
        )
        first_reference = self.database.get_available_message_reference(10, track_id)
        self.assertEqual(first_reference["source_message_id"], 2**31 + 8)
        self.assertTrue(
            self.database.mark_message_reference_unavailable(10, first_reference["id"])
        )
        available = self.database.get_available_message_reference(10, track_id)
        self.assertEqual(available["source_message_id"], 70)

        track = self.database.get_track(10, track_id)
        self.assertEqual(track["telegram_file_id"], "sendable-file-id")
        self.assertEqual(track["duration_seconds"], 245)
        self.assertEqual(track["mime_type"], "audio/mpeg")
        self.assertEqual(track["original_filename"], "song.mp3")
        self.assertIsNone(self.database.list_message_references(11, track_id))
        self.assertFalse(self.database.add_message_reference(11, track_id, 11, 4, "audio"))

    def test_rating_notes_and_tags_are_private_and_validated(self) -> None:
        self.database.create_playlist(10, "Library")
        self.database.create_playlist(11, "Library")
        track_id = self.database.add_track(10, "Library", 10, 1, "Song", None, "audio", None)

        self.assertTrue(self.database.set_track_rating(10, track_id, 5))
        self.assertTrue(self.database.set_track_note(10, track_id, "Live version"))
        self.assertTrue(self.database.add_track_tag(10, track_id, "Progressive Metal"))
        self.assertFalse(self.database.add_track_tag(10, track_id, "progressive metal"))
        self.assertEqual(self.database.list_track_tags(10, track_id), ["Progressive Metal"])
        self.assertEqual(self.database.list_track_tags(11, track_id), None)
        self.assertFalse(self.database.set_track_rating(11, track_id, 3))
        self.assertFalse(self.database.set_track_note(11, track_id, "private"))
        with self.assertRaises(ValueError):
            self.database.set_track_rating(10, track_id, 6)

        track = self.database.get_track(10, track_id)
        self.assertEqual(track["rating"], 5)
        self.assertEqual(track["note"], "Live version")

    def test_metadata_only_tracks_are_private_and_have_no_telegram_reference(self) -> None:
        track_id = self.database.create_metadata_only_track(
            10,
            "Metadata only",
            "Artist",
            "Album",
            "recording-mbid",
            "https://musicbrainz.org/recording/recording-mbid",
        )

        rows = self.database.list_metadata_only_tracks(10)
        self.assertEqual(rows[0]["id"], track_id)
        self.assertEqual(rows[0]["title"], "Metadata only")
        self.assertEqual(self.database.list_metadata_only_tracks(11), [])
        self.assertTrue(self.database.delete_metadata_only_track(10, track_id))
        self.assertFalse(self.database.delete_metadata_only_track(11, track_id))

    def test_adding_existing_track_to_collection_copies_private_metadata_and_sources(self) -> None:
        self.database.create_collection(10, "Inbox")
        self.database.create_collection(10, "Favorites")
        self.database.create_collection(11, "Private")
        collections = {
            row["name"]: row["id"] for row in self.database.list_collections(10)
        }
        inbox_id = collections["Inbox"]
        favorites_id = collections["Favorites"]
        private_id = self.database.list_collections(11)[0]["id"]
        track_id = self.database.add_track(
            10, "Inbox", 10, 71, "Song", "Artist", "audio", "unique-file"
        )
        self.database.add_message_reference(10, track_id, 10, 72, "audio")
        self.database.set_track_rating(10, track_id, 4)
        self.database.set_track_note(10, track_id, "Keep")
        self.database.add_track_tag(10, track_id, "favorite")

        self.assertTrue(self.database.add_track_to_collection(10, track_id, favorites_id))
        self.assertFalse(self.database.add_track_to_collection(10, track_id, favorites_id))
        self.assertFalse(self.database.add_track_to_collection(10, track_id, private_id))
        self.assertEqual(len(self.database.list_tracks_by_id(10, inbox_id)), 1)
        copied = self.database.list_tracks_by_id(10, favorites_id)[0]
        self.assertEqual(copied["rating"], 4)
        self.assertEqual(copied["note"], "Keep")
        self.assertEqual(self.database.list_track_tags(10, copied["id"]), ["favorite"])
        self.assertEqual(
            [row["source_message_id"] for row in self.database.list_message_references(10, copied["id"])],
            [71, 72],
        )


if __name__ == "__main__":
    unittest.main()
