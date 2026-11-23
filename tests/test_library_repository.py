import tempfile
import unittest
from pathlib import Path

from archive import PersonalMusicArchive, TelegramMessageReference, TrackMetadata
from database import Database


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "library.sqlite3")
        self.repository = self.database.library_repository

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def test_audio_is_saved_to_library_without_playlist_membership(self) -> None:
        saved = PersonalMusicArchive(self.database).save_audio(
            42,
            TrackMetadata(
                "Track",
                performer="Artist",
                release_title="Album",
                duration_seconds=180,
                musicbrainz_recording_id="recording-42",
            ),
            TelegramMessageReference(
                -100123,
                81,
                "audio",
                telegram_file_id="sendable-42",
                telegram_file_unique_id="unique-42",
            ),
        )

        track_id, added_to_library, added_to_playlist = saved
        self.assertTrue(added_to_library)
        self.assertFalse(added_to_playlist)
        track = self.repository.get_track(42, track_id)
        self.assertEqual(track["title"], "Track")
        self.assertEqual(track["performer"], "Artist")
        self.assertIsNone(track["playlist_id"])
        self.assertTrue(track["is_playable"])
        self.assertEqual(
            self.repository.get_available_message_reference(42, track_id)[
                "source_message_id"
            ],
            81,
        )
        self.assertIsNone(self.repository.get_track(99, track_id))

    def test_playlist_membership_is_independent_and_removal_keeps_library_track(self) -> None:
        self.assertTrue(self.repository.create_playlist(42, "Favorites"))
        playlist_id = self.repository.list_playlists(42)[0]["id"]
        track_id, added, _ = self.repository.add_audio_to_library(
            42,
            100,
            9,
            "Track",
            "Artist",
            "audio",
            "file-9",
        )
        self.assertTrue(added)
        self.assertTrue(
            self.repository.add_track_to_collection(42, track_id, playlist_id)
        )
        self.assertFalse(
            self.repository.add_track_to_collection(42, track_id, playlist_id)
        )
        self.assertEqual(
            self.repository.list_tracks_by_id(42, playlist_id)[0]["id"],
            track_id,
        )
        self.assertTrue(self.repository.remove_track_by_id(42, playlist_id, track_id))
        self.assertEqual(self.repository.list_tracks_by_id(42, playlist_id), [])
        self.assertIsNotNone(self.repository.get_track(42, track_id))
        self.assertEqual(self.repository.list_library(42)[0]["id"], track_id)

    def test_exact_file_reuse_prevents_duplicate_library_membership(self) -> None:
        first = self.repository.add_audio_to_library(
            42, 100, 9, "Original title", "Artist", "audio", "stable-file"
        )
        second = self.repository.add_audio_to_library(
            42, 101, 10, "Different title", "Other artist", "audio", "stable-file"
        )

        self.assertEqual(first[0], second[0])
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        self.assertEqual(len(self.repository.list_library(42)), 1)
        self.assertEqual(
            len(self.repository.list_message_references(42, first[0])),
            2,
        )

    def test_metadata_edits_are_private_when_catalog_track_is_shared(self) -> None:
        first = self.repository.add_audio_to_library(
            42,
            100,
            9,
            "Canonical title",
            "Canonical artist",
            "audio",
            "first-file",
            musicbrainz_recording_id="shared-recording",
        )
        second = self.repository.add_audio_to_library(
            43,
            101,
            10,
            "Different upload title",
            "Different artist",
            "audio",
            "second-file",
            musicbrainz_recording_id="shared-recording",
        )
        self.assertEqual(first[0], second[0])

        self.assertTrue(
            self.repository.update_track_metadata(
                42, first[0], "Private title", "Private artist"
            )
        )

        self.assertEqual(self.repository.get_track(42, first[0])["title"], "Private title")
        self.assertEqual(
            self.repository.get_track(42, first[0])["performer"], "Private artist"
        )
        self.assertEqual(self.repository.get_track(43, second[0])["title"], "Canonical title")
        self.assertEqual(
            self.repository.get_track(43, second[0])["performer"], "Canonical artist"
        )


if __name__ == "__main__":
    unittest.main()
