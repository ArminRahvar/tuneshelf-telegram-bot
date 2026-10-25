import tempfile
import unittest
from pathlib import Path

from archive import (
    PersonalMusicArchive,
    TelegramMessageReference,
    TrackMetadata,
)
from database import Database
from telegram_delivery import TelegramDeliveryService


class RecordingTelegramAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []

    def call(self, method: str, **parameters: int) -> dict[str, int]:
        self.calls.append((method, parameters))
        return {}


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.repository = Database(Path(self.directory.name) / "archive.sqlite3")
        self.repository.create_collection(42, "Favorites")
        self.collection_id = self.repository.list_collections(42)[0]["id"]
        self.archive = PersonalMusicArchive(self.repository)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_application_service_saves_audio_and_personal_annotations(self) -> None:
        metadata = TrackMetadata(
            "Track",
            performer="Artist",
            release_title="Album",
            duration_seconds=180,
            mime_type="audio/mpeg",
            original_filename="track.mp3",
            musicbrainz_recording_id="recording-id",
        )
        reference = TelegramMessageReference(
            -1001234567890,
            82,
            "audio",
            telegram_file_id="reusable-file-id",
            telegram_file_unique_id="unique-file-id",
        )

        self.assertTrue(self.archive.save_audio(42, self.collection_id, metadata, reference))
        self.assertFalse(self.archive.save_audio(42, self.collection_id, metadata, reference))
        track = self.repository.list_tracks_by_id(42, self.collection_id)[0]
        self.assertEqual(track["telegram_file_id"], "reusable-file-id")
        self.assertEqual(track["duration_seconds"], 180)
        self.assertTrue(self.archive.set_rating(42, track["id"], 4))
        self.assertTrue(self.archive.set_note(42, track["id"], "Great live recording"))
        self.assertTrue(self.archive.add_tag(42, track["id"], "live"))
        playable = self.archive.get_playable_reference(42, track["id"])
        self.assertEqual(playable["source_message_id"], 82)

        self.assertFalse(self.archive.set_rating(99, track["id"], 5))
        self.assertIsNone(self.archive.get_playable_reference(99, track["id"]))

    def test_metadata_only_track_has_no_playable_reference(self) -> None:
        track_id = self.archive.save_metadata_only(
            42,
            TrackMetadata("Studio recording", performer="Artist"),
            "https://musicbrainz.org/recording/example",
        )

        self.assertEqual(self.repository.list_metadata_only_tracks(42)[0]["id"], track_id)
        self.assertEqual(self.repository.list_message_references(42, track_id), None)

    def test_metadata_and_reference_values_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            TrackMetadata(" ", duration_seconds=1)
        with self.assertRaises(ValueError):
            TrackMetadata("Track", duration_seconds=-1)
        with self.assertRaises(ValueError):
            TelegramMessageReference(1, 0, "audio")

    def test_delivery_policy_copies_by_default_and_can_forward(self) -> None:
        api = RecordingTelegramAPI()

        TelegramDeliveryService(api).deliver(2, -1003, 4)
        TelegramDeliveryService(api, mode="forward").deliver(2, -1003, 5)

        self.assertEqual(
            api.calls,
            [
                (
                    "copyMessage",
                    {"chat_id": 2, "from_chat_id": -1003, "message_id": 4},
                ),
                (
                    "forwardMessage",
                    {"chat_id": 2, "from_chat_id": -1003, "message_id": 5},
                ),
            ],
        )
        with self.assertRaises(ValueError):
            TelegramDeliveryService(api, mode="download")


if __name__ == "__main__":
    unittest.main()
