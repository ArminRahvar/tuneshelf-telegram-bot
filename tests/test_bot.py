import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from bot import TelegramAPI, TelegramAPIError, handle_callback, handle_message
from app.database.runtime import RuntimeDatabase
from app.database.models import CatalogTrack
from database import Database
from metadata import MetadataSearchResult
from musicbrainz import MusicBrainzError, RecordingMetadata


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict[str, object] | None]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, object] = {}
        self.errors: dict[str, TelegramAPIError] = {}

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, object] | None = None
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))

    def call(self, method: str, **parameters: object) -> object:
        self.calls.append((method, parameters))
        if method in self.errors:
            raise self.errors[method]
        return self.responses.get(method, {})


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = RuntimeDatabase(
            Database(Path(self.directory.name) / "test.sqlite3")
        )
        self.database.create_playlist(7, "Road Trip")
        self.api = FakeTelegramAPI()

    def tearDown(self) -> None:
        self.database.library_repository.close()
        self.directory.cleanup()

    def test_search_select_downloads_and_queues_youtube_result(self) -> None:
        class FakeSearchProvider:
            def search(self, query: str, limit: int = 5) -> list[MetadataSearchResult]:
                self.query = query
                self.limit = limit
                return [
                    MetadataSearchResult(
                        provider="youtube_music",
                        identifier="video-id",
                        title="Track",
                        artist="Artist",
                        release_title=None,
                        release_year=2024,
                        provider_url="https://music.youtube.com/watch?v=video-id",
                    )
                ]

        provider = FakeSearchProvider()
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "🔎 Search",
            },
            self.database,
            self.api,
            metadata_search_provider=provider,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Track Artist",
            },
            self.database,
            self.api,
            metadata_search_provider=provider,
        )

        self.assertEqual(provider.query, "Track Artist")
        self.assertEqual(provider.limit, 5)
        self.assertTrue(
            self.database.get_user_state(7).startswith("download_search_results:")
        )

        handle_callback(
            {
                "id": "callback-download-search",
                "from": {"id": 7},
                "message": {
                    "chat": {"id": 7, "type": "private"},
                    "message_id": 221,
                },
                "data": "download_search:0",
            },
            self.database,
            self.api,
        )

        jobs = self.database.download_repository.list_for_user(7)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["source_url"],
            "https://music.youtube.com/watch?v=video-id",
        )
        self.assertIsNotNone(jobs[0]["track_id"])
        self.assertIsNone(self.database.get_user_state(7))

    def test_download_button_prompts_for_url_and_queues_job(self) -> None:
        handle_callback(
            {
                "id": "callback-download-add",
                "from": {"id": 7},
                "message": {
                    "chat": {"id": 7, "type": "private"},
                    "message_id": 220,
                },
                "data": "download_add",
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.database.get_user_state(7), "download_url")

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "https://example.com/audio",
            },
            self.database,
            self.api,
        )

        jobs = self.database.download_repository.list_for_user(7)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source_url"], "https://example.com/audio")
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertIsNone(self.database.get_user_state(7))

    def test_inline_menu_add_and_play_use_original_message_reference(self) -> None:
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 123,
                "audio": {
                    "title": "Track",
                    "performer": "Artist",
                    "file_unique_id": "file-unique",
                },
            },
            self.database,
            self.api,
        )
        track = self.database.list_library(7, 10)[0]
        handle_callback(
            {
                "id": "callback-play",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 200},
                "data": f"search_play:{track['id']}",
            },
            self.database,
            self.api,
        )

        copy_calls = [parameters for method, parameters in self.api.calls if method == "copyMessage"]
        self.assertEqual(
            copy_calls,
            [{"chat_id": 7, "from_chat_id": 7, "message_id": 123}],
        )
        self.assertEqual(track["title"], "Track")
        self.assertEqual(track["file_unique_id"], "file-unique")
        self.assertIsNotNone(
            self.database.list_message_references(7, track["id"])[0]["last_verified_at"]
        )

    def test_shared_archive_copies_once_and_is_used_for_playback(self) -> None:
        archive_chat_id = -100123456789
        self.api.responses["copyMessage"] = {"message_id": 901}
        self.database.create_playlist(8, "Other")

        for user_id, message_id in ((7, 123), (8, 456)):
            handle_message(
                {
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id},
                    "message_id": message_id,
                    "audio": {
                        "title": "Shared track",
                        "performer": "Shared artist",
                        "file_unique_id": "shared-file",
                    },
                },
                self.database,
                self.api,
                archive_chat_id=archive_chat_id,
            )

        archive_copies = [
            parameters
            for method, parameters in self.api.calls
            if method == "copyMessage" and parameters.get("chat_id") == archive_chat_id
        ]
        self.assertEqual(
            archive_copies,
            [{"chat_id": archive_chat_id, "from_chat_id": 7, "message_id": 123}],
        )
        for user_id in (7, 8):
            track = self.database.list_library(user_id, 10)[0]
            archive_reference = self.database.get_available_message_reference(
                user_id,
                track["id"],
                preferred_chat_id=archive_chat_id,
            )
            self.assertEqual(archive_reference["source_chat_id"], archive_chat_id)
            self.assertEqual(archive_reference["source_message_id"], 901)

        with self.database._connection() as connection:
            asset = connection.execute(
                """
                SELECT track_id FROM archive_assets
                WHERE archive_chat_id = ? AND telegram_file_unique_id = ?
                """,
                (archive_chat_id, "shared-file"),
            ).fetchone()
            catalog_track = connection.execute(
                """
                SELECT t.title, a.name AS performer
                FROM catalog_tracks AS t
                JOIN catalog_track_artists AS link ON link.track_id = t.id
                JOIN artists AS a ON a.id = link.artist_id
                WHERE t.id = ?
                """,
                (asset["track_id"],),
            ).fetchone()
            self.assertEqual(dict(catalog_track), {
                "title": "Shared track",
                "performer": "Shared artist",
            })

        track = self.database.list_library(7, 10)[0]
        handle_callback(
            {
                "id": "callback-play-archive",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 201},
                "data": f"search_play:{track['id']}",
            },
            self.database,
            self.api,
            archive_chat_id=archive_chat_id,
        )
        self.assertIn(
            ("copyMessage", {"chat_id": 7, "from_chat_id": archive_chat_id, "message_id": 901}),
            self.api.calls,
        )
        for user_id in (7, 8):
            playlist_id = self.database.list_playlists(user_id)[0]["id"]
            self.database.delete_playlist_by_id(user_id, playlist_id)
        self.assertIsNotNone(
            self.database.find_shared_archive_reference(
                archive_chat_id,
                "shared-file",
            )
        )

    def test_shared_archive_matches_musicbrainz_id_not_title(self) -> None:
        class FakeProvider:
            def lookup_recording(
                self, title: str, artist: str | None
            ) -> RecordingMetadata:
                return RecordingMetadata(
                    recording_id="shared-recording-id",
                    title=f"Canonical {title}",
                    artist=artist,
                    release_title=None,
                    score=98,
                )

        archive_chat_id = -100123456789
        self.api.responses["copyMessage"] = {"message_id": 902}
        self.database.create_playlist(8, "Other")

        for user_id, message_id, source_title in (
            (7, 125, "First upload"),
            (8, 457, "Different title"),
        ):
            handle_message(
                {
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id},
                    "message_id": message_id,
                    "audio": {
                        "title": source_title,
                        "performer": "Same artist",
                        "file_unique_id": f"file-{user_id}",
                    },
                },
                self.database,
                self.api,
                FakeProvider(),
                archive_chat_id,
            )

        archive_copies = [
            parameters
            for method, parameters in self.api.calls
            if method == "copyMessage" and parameters.get("chat_id") == archive_chat_id
        ]
        self.assertEqual(len(archive_copies), 1)
        for user_id in (7, 8):
            track = self.database.list_library(user_id, 10)[0]
            reference = self.database.get_available_message_reference(
                user_id,
                track["id"],
                preferred_chat_id=archive_chat_id,
            )
            self.assertEqual(reference["source_message_id"], 902)

    def test_archive_copy_failure_keeps_saved_source_and_shows_notice(self) -> None:
        archive_chat_id = -100123456789
        self.api.errors["copyMessage"] = TelegramAPIError(
            "bot is not an administrator",
            error_code=403,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 123,
                "audio": {
                    "title": "Track",
                    "file_unique_id": "track-file",
                },
            },
            self.database,
            self.api,
            archive_chat_id=archive_chat_id,
        )
        self.assertIn(
            "could not be copied to the shared archive",
            self.api.messages[-1][1],
        )
        track = self.database.list_library(7, 10)[0]
        self.assertEqual(len(self.database.list_message_references(7, track["id"])), 1)
        self.assertTrue(
            self.database.get_available_message_reference(7, track["id"])
        )

    def test_musicbrainz_metadata_is_saved_without_changing_source_message(self) -> None:
        class FakeProvider:
            def lookup_recording(self, title: str, artist: str | None) -> RecordingMetadata:
                return RecordingMetadata(
                    recording_id="recording-id",
                    title="Canonical Title",
                    artist="Canonical Artist",
                    release_title="Canonical Album",
                    score=98,
                )

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 811,
                "audio": {"title": "telegram title", "performer": "telegram artist"},
            },
            self.database,
            self.api,
            FakeProvider(),
        )
        self.assertIn("Canonical Album", self.api.messages[-1][1])

        track = self.database.list_library(7, 10)[0]
        self.assertEqual(track["title"], "Canonical Title")
        self.assertEqual(track["performer"], "Canonical Artist")
        self.assertEqual(track["release_title"], "Canonical Album")
        self.assertEqual(track["musicbrainz_recording_id"], "recording-id")
        self.assertEqual(track["source_chat_id"], 7)
        self.assertEqual(track["source_message_id"], 811)

    def test_musicbrainz_failure_keeps_telegram_metadata_and_surfaces_notice(self) -> None:
        class FailingProvider:
            def lookup_recording(self, title: str, artist: str | None) -> None:
                raise MusicBrainzError("service unavailable")

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 812,
                "audio": {"title": "Original Title", "performer": "Original Artist"},
            },
            self.database,
            self.api,
            FailingProvider(),
        )
        prompt = self.api.messages[-1][1]
        self.assertIn("lookup failed", prompt)
        track = self.database.list_library(7, 10)[0]
        self.assertEqual(track["title"], "Original Title")
        self.assertEqual(track["performer"], "Original Artist")
        self.assertIsNone(track["musicbrainz_recording_id"])

    def test_shuffle_play_sends_all_tracks_in_randomized_order(self) -> None:
        playlist_id = self.database.list_playlists(7)[0]["id"]
        for message_id in (10, 20, 30):
            self.database.add_track(
                user_id=7,
                playlist_name="Road Trip",
                source_chat_id=7,
                source_message_id=message_id,
                title=f"Track {message_id}",
                performer=None,
                media_type="audio",
                file_unique_id=None,
            )

        with patch("bot.random.shuffle", side_effect=lambda tracks: tracks.reverse()), patch(
            "bot.time.sleep"
        ):
            handle_callback(
                {
                    "id": "callback-shuffle",
                    "from": {"id": 7},
                    "message": {
                        "chat": {"id": 7, "type": "private"},
                        "message_id": 211,
                    },
                    "data": f"shuffle:{playlist_id}",
                },
                self.database,
                self.api,
            )

        copied_messages = [
            parameters["message_id"]
            for method, parameters in self.api.calls
            if method == "copyMessage"
        ]
        self.assertEqual(copied_messages, [30, 20, 10])

    def test_search_and_play_track_uses_original_message_reference(self) -> None:
        playlist_id = self.database.list_playlists(7)[0]["id"]
        self.database.add_track(
            7, "Road Trip", 7, 909, "Northern Lights", "Nova", "audio", "file"
        )
        handle_callback(
            {
                "id": "callback-search",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 213},
                "data": "search",
            },
            self.database,
            self.api,
        )
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "nova"},
            self.database,
            self.api,
        )
        result_keyboard = self.api.messages[-1][2]["inline_keyboard"]
        play_data = result_keyboard[0][0]["callback_data"]
        self.assertIn("Northern Lights", result_keyboard[0][0]["text"])
        handle_callback(
            {
                "id": "callback-search-play",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 214},
                "data": play_data,
            },
            self.database,
            self.api,
        )
        copy_calls = [params for method, params in self.api.calls if method == "copyMessage"]
        self.assertEqual(copy_calls[-1]["from_chat_id"], 7)
        self.assertEqual(copy_calls[-1]["message_id"], 909)
        self.assertEqual(self.database.get_track(7, playlist_id)["source_message_id"], 909)

    def test_collection_track_list_can_play_or_remove_individual_tracks(self) -> None:
        playlist_id = self.database.list_playlists(7)[0]["id"]
        track_id = self.database.add_track(
            7, "Road Trip", 7, 910, "Northern Lights", "Nova", "audio", "file"
        )
        self.database.add_track(
            7, "Road Trip", 7, 911, "Blue Hour", "Nova", "audio", "another-file"
        )

        handle_callback(
            {
                "id": "collection-tracks",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 215},
                "data": f"tracks:{playlist_id}:0",
            },
            self.database,
            self.api,
        )
        keyboard = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], f"search_play:{track_id}")
        self.assertEqual(keyboard[0][1]["callback_data"], f"remove:{playlist_id}:{track_id}")

        handle_callback(
            {
                "id": "collection-track-play",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 215},
                "data": keyboard[0][0]["callback_data"],
            },
            self.database,
            self.api,
        )
        copy_calls = [params for method, params in self.api.calls if method == "copyMessage"]
        self.assertEqual(copy_calls[-1]["message_id"], 910)
        self.assertIsNotNone(self.database.get_track(7, track_id))

        handle_callback(
            {
                "id": "collection-track-remove",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 215},
                "data": keyboard[0][1]["callback_data"],
            },
            self.database,
            self.api,
        )
        self.assertIsNotNone(self.database.get_track(7, track_id))
        self.assertNotIn(
            track_id,
            [
                track["id"]
                for track in self.database.list_tracks_by_id(7, playlist_id)
            ],
        )

    def test_link_command_adds_an_alternate_source_reference(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 909, "Northern Lights", "Nova", "audio", "file"
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": f"/link {track_id}",
            },
            self.database,
            self.api,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 910,
                "audio": {
                    "title": "Alternate title",
                    "file_id": "current-file-id",
                    "file_unique_id": "alternate-unique-id",
                },
            },
            self.database,
            self.api,
        )

        references = self.database.list_message_references(7, track_id)
        self.assertEqual(len(references), 2)
        self.assertEqual(references[1]["source_message_id"], 910)
        self.assertIn("Added this Telegram message", self.api.messages[-1][1])

    def test_audio_document_metadata_is_saved_without_downloading_media(self) -> None:
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 911,
                "document": {
                    "file_id": "telegram-file-id",
                    "file_unique_id": "telegram-file-unique-id",
                    "mime_type": "audio/flac",
                    "file_name": "recording.flac",
                },
            },
            self.database,
            self.api,
        )
        track = self.database.list_library(7, 10)[0]
        self.assertEqual(track["title"], "recording.flac")
        self.assertEqual(track["media_type"], "document")
        self.assertEqual(track["telegram_file_id"], "telegram-file-id")
        self.assertEqual(track["mime_type"], "audio/flac")
        self.assertEqual(track["original_filename"], "recording.flac")
        self.assertNotIn("audio_binary", track.keys())

    def test_rate_note_tag_and_track_commands_are_owner_scoped(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 912, "Northern Lights", "Nova", "audio", "file"
        )
        for text in (
            f"/rate {track_id} 5",
            f"/note {track_id} favorite live version",
            f"/tag {track_id} live",
        ):
            handle_message(
                {
                    "chat": {"id": 7, "type": "private"},
                    "from": {"id": 7},
                    "text": text,
                },
                self.database,
                self.api,
            )

        track = self.database.get_track(7, track_id)
        self.assertEqual(track["rating"], 5)
        self.assertEqual(track["note"], "favorite live version")
        self.assertEqual(self.database.list_track_tags(7, track_id), ["live"])
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": f"/track {track_id}",
            },
            self.database,
            self.api,
        )
        self.assertIn("Playable message references: 1/1", self.api.messages[-1][1])

    def test_edit_command_updates_only_owned_track_metadata(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 914, "Old title", "Old artist", "audio", None
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": f"/edit {track_id} Correct title | Correct artist",
            },
            self.database,
            self.api,
        )
        track = self.database.get_track(7, track_id)
        self.assertEqual(track["title"], "Correct title")
        self.assertEqual(track["performer"], "Correct artist")
        self.assertFalse(
            self.database.update_track_metadata(8, track_id, "Unauthorized", None)
        )

    def test_addtrack_command_adds_track_to_another_owned_collection(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 915, "Collection track", "Artist", "audio", "file"
        )
        self.database.create_collection(7, "Favorites")
        collection_id = next(
            row["id"]
            for row in self.database.list_collections(7)
            if row["name"] == "Favorites"
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": f"/addtrack {track_id} {collection_id}",
            },
            self.database,
            self.api,
        )

        self.assertEqual(
            len(self.database.list_tracks_by_id(7, collection_id)),
            1,
        )
        self.assertIn("Track added", self.api.messages[-1][1])

    def test_failed_copy_marks_reference_unavailable_but_preserves_track(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 913, "Persisted metadata", "Artist", "audio", None
        )

        class UnavailableSourceAPI(FakeTelegramAPI):
            def call(self, method: str, **parameters: object) -> object:
                self.calls.append((method, parameters))
                if method == "copyMessage":
                    raise TelegramAPIError("message not found", error_code=400)
                return {}

        api = UnavailableSourceAPI()
        handle_callback(
            {
                "id": "copy-unavailable",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 216},
                "data": f"search_play:{track_id}",
            },
            self.database,
            api,
        )

        reference = self.database.list_message_references(7, track_id)[0]
        self.assertEqual(reference["is_available"], 0)
        self.assertEqual(self.database.get_track(7, track_id)["title"], "Persisted metadata")
        self.assertIn("marked unavailable", api.messages[-1][1])

    def test_rate_limited_copy_retries_without_disabling_the_reference(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 916, "Retry track", "Artist", "audio", None
        )

        class RateLimitedOnceAPI(FakeTelegramAPI):
            attempts = 0

            def call(self, method: str, **parameters: object) -> object:
                self.calls.append((method, parameters))
                if method == "copyMessage":
                    self.attempts += 1
                    if self.attempts == 1:
                        raise TelegramAPIError(
                            "Too Many Requests", error_code=429, retry_after=0
                        )
                return {}

        api = RateLimitedOnceAPI()
        with patch("bot.time.sleep"):
            handle_callback(
                {
                    "id": "copy-rate-limit",
                    "from": {"id": 7},
                    "message": {"chat": {"id": 7, "type": "private"}, "message_id": 220},
                    "data": f"search_play:{track_id}",
                },
                self.database,
                api,
            )

        reference = self.database.list_message_references(7, track_id)[0]
        self.assertEqual(api.attempts, 2)
        self.assertEqual(reference["is_available"], 1)
        self.assertIsNotNone(reference["last_verified_at"])

    def test_metadata_query_is_saved_without_a_playable_telegram_reference(self) -> None:
        class FakeProvider:
            def search(self, query: str, limit: int = 5) -> list[MetadataSearchResult]:
                if query != "Metadata Track - Artist" or limit != 5:
                    raise AssertionError("The complete query was not passed to the provider.")
                return [
                    MetadataSearchResult(
                        provider="youtube_music",
                        identifier="video-id",
                        title="Metadata Track",
                        artist="Artist",
                        release_title="Album",
                        release_year=2024,
                        provider_url="https://music.youtube.com/watch?v=video-id",
                    )
                ]

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Metadata Track - Artist",
            },
            self.database,
            self.api,
            metadata_search_provider=FakeProvider(),
        )

        self.assertEqual(self.database.list_metadata_only_tracks(7), [])
        handle_callback(
            {
                "id": "choose-youtube-metadata",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 217},
                "data": "metadata:0",
            },
            self.database,
            self.api,
        )
        rows = self.database.list_metadata_only_tracks(7)
        self.assertEqual(rows[0]["title"], "Metadata Track")
        self.assertIn(
            "No playable Telegram copy",
            self.api.calls[-1][1]["text"],
        )
        with self.database.library_repository.engine.connect() as connection:
            source_provider, source_identifier = connection.execute(
                select(
                    CatalogTrack.source_provider,
                    CatalogTrack.source_identifier,
                ).where(CatalogTrack.id == rows[0]["id"])
            ).one()
        self.assertEqual(source_provider, "youtube_music")
        self.assertEqual(
            source_identifier,
            "https://music.youtube.com/watch?v=video-id",
        )

    def test_ambiguous_metadata_search_requires_user_selection(self) -> None:
        class FakeProvider:
            def search(self, query: str, limit: int = 5) -> list[MetadataSearchResult]:
                return [
                    MetadataSearchResult(
                        "youtube_music",
                        "first-id",
                        "First result",
                        "Artist",
                        None,
                        None,
                        "https://music.youtube.com/watch?v=first-id",
                    ),
                    MetadataSearchResult(
                        "youtube_music",
                        "second-id",
                        "Second result",
                        "Artist",
                        "Album",
                        None,
                        "https://music.youtube.com/watch?v=second-id",
                    ),
                ]

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Possible recording",
            },
            self.database,
            self.api,
            metadata_search_provider=FakeProvider(),
        )
        keyboard = self.api.messages[-1][2]["inline_keyboard"]
        self.assertEqual(keyboard[1][0]["callback_data"], "metadata:1")

        handle_callback(
            {
                "id": "choose-metadata",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 218},
                "data": "metadata:1",
            },
            self.database,
            self.api,
        )
        rows = self.database.list_metadata_only_tracks(7)
        self.assertEqual(rows[0]["title"], "Second result")
        self.assertIsNone(rows[0]["musicbrainz_recording_id"])

    def test_collection_delivery_is_limited_to_twenty_tracks(self) -> None:
        playlist_id = self.database.list_playlists(7)[0]["id"]
        for message_id in range(100, 125):
            self.database.add_track(
                7, "Road Trip", 7, message_id, f"Track {message_id}", None, "audio", None
            )

        with patch("bot.time.sleep"):
            handle_callback(
                {
                    "id": "callback-play-limited",
                    "from": {"id": 7},
                    "message": {
                        "chat": {"id": 7, "type": "private"},
                        "message_id": 217,
                    },
                    "data": f"play:{playlist_id}",
                },
                self.database,
                self.api,
            )

        copies = [call for call in self.api.calls if call[0] == "copyMessage"]
        self.assertEqual(len(copies), 20)
        status = next(
            parameters["text"]
            for method, parameters in self.api.calls
            if method == "editMessageText"
        )
        self.assertIn("limited to 20 per request", status)

    def test_search_results_are_paginated(self) -> None:
        playlist_id = self.database.list_playlists(7)[0]["id"]
        for index in range(9):
            self.database.add_track(
                7,
                "Road Trip",
                7,
                1000 + index,
                f"Result {index}",
                "Common Artist",
                "audio",
                None,
            )
        handle_callback(
            {
                "id": "callback-search",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 215},
                "data": "search",
            },
            self.database,
            self.api,
        )
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "common"},
            self.database,
            self.api,
        )
        keyboard = self.api.messages[-1][2]["inline_keyboard"]
        page_callback = keyboard[8][0]["callback_data"]
        self.assertEqual(page_callback, "search_page:1")

        handle_callback(
            {
                "id": "callback-search-page",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 215},
                "data": page_callback,
            },
            self.database,
            self.api,
        )
        self.assertEqual(len(self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]), 4)
        self.assertEqual(self.database.search_tracks(7, "common", 8, 8)[0]["playlist_id"], playlist_id)

    def test_readding_same_audio_reports_duplicate_without_duplicating_track(self) -> None:
        message = {
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 7},
            "message_id": 555,
            "audio": {"title": "Same audio", "file_unique_id": "same-file"},
        }
        for _ in range(2):
            handle_message(message, self.database, self.api)
        tracks = self.database.list_library(7, 10)
        self.assertEqual(len(tracks), 1)
        self.assertTrue(
            any("already in" in text for _, text, _ in self.api.messages)
        )

    def test_group_chat_audio_is_ignored(self) -> None:
        handle_message(
            {
                "chat": {"id": -2, "type": "group"},
                "from": {"id": 7},
                "message_id": 123,
                "audio": {"title": "Track"},
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.api.messages, [])
        self.assertEqual(self.api.calls, [])

    def test_cancel_audio_selection_clears_pending_reference(self) -> None:
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 333,
                "audio": {"title": "Not saved"},
            },
            self.database,
            self.api,
        )
        cancel_data = self.api.messages[-1][2]["inline_keyboard"][-1][0]["callback_data"]
        handle_callback(
            {
                "id": "callback-cancel",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 204},
                "data": cancel_data,
            },
            self.database,
            self.api,
        )
        self.assertIsNone(self.database.get_pending_track(7, 7, 333))

    def test_create_playlist_uses_button_then_text_input(self) -> None:
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/start"},
            self.database,
            self.api,
        )
        keyboard = self.api.messages[-1][2]["inline_keyboard"]
        handle_callback(
            {
                "id": "callback-new",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 201},
                "data": next(
                    button["callback_data"]
                    for row in keyboard
                    for button in row
                    if button["callback_data"] == "new"
                ),
            },
            self.database,
            self.api,
        )
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "Jazz"},
            self.database,
            self.api,
        )
        self.assertEqual(self.database.list_playlists(7)[0]["name"], "Jazz")
        self.assertIsNone(self.database.get_user_state(7))

    def test_start_exposes_both_reply_and_inline_main_menu_buttons(self) -> None:
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/start"},
            self.database,
            self.api,
        )

        self.assertEqual(self.api.messages[0][2]["keyboard"][0][0]["text"], "➕ Add Music")
        inline_buttons = [
            button["text"]
            for row in self.api.messages[-1][2]["inline_keyboard"]
            for button in row
        ]
        for label in (
            "➕ Add Music",
            "📚 My Library",
            "🔎 Search",
            "🗂 Collections",
            "✨ Discover",
            "📊 Statistics",
            "⚙️ Settings",
        ):
            self.assertIn(label, inline_buttons)

    def test_statistics_discovery_and_delivery_settings_are_user_scoped(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 918, "Night Drive", "Nova", "audio", "drive",
            release_year=2024,
        )
        self.database.set_track_rating(7, track_id, 5)
        self.database.add_track_tag(7, track_id, "favorite")

        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "📊 Statistics"},
            self.database,
            self.api,
        )
        stats_text = self.api.messages[-1][1]
        self.assertIn("Saved tracks: 1", stats_text)
        self.assertIn("Nova (1)", stats_text)
        self.assertIn("5★: 1", stats_text)
        self.assertIn("favorite (1)", stats_text)

        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "✨ Discover"},
            self.database,
            self.api,
        )
        self.assertIn("Not enough shared artist or tag data", self.api.messages[-1][1])

        handle_callback(
            {
                "id": "settings-open",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 220},
                "data": "delivery:forward",
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.database.get_delivery_mode(7), "forward")
        self.assertEqual(self.database.get_delivery_mode(8), "copy")

    def test_search_filters_by_rating_year_and_playability(self) -> None:
        playable_id = self.database.add_track(
            7, "Road Trip", 7, 919, "Blue Hour", "Nova", "audio", "blue",
            release_year=2024,
        )
        self.database.add_track(
            7, "Road Trip", 7, 920, "Blue Hour (live)", "Nova", "audio", "blue-live",
            release_year=2023,
        )
        self.database.set_track_rating(7, playable_id, 5)
        self.database.set_track_rating(7, playable_id + 1, 3)

        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "🔎 Search"},
            self.database,
            self.api,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Blue rating:5 year:2024 playable:true",
            },
            self.database,
            self.api,
        )

        search_keyboard = self.api.messages[-1][2]["inline_keyboard"]
        result_buttons = [
            button["callback_data"]
            for row in search_keyboard
            for button in row
            if button["callback_data"].startswith("search_play:")
        ]
        self.assertEqual(result_buttons, [f"search_play:{playable_id}"])

    def test_track_detail_buttons_edit_personal_data_without_slash_commands(self) -> None:
        track_id = self.database.add_track(
            7, "Road Trip", 7, 921, "Evening", "Nova", "audio", "evening",
            release_year=2022,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": f"/track {track_id}",
            },
            self.database,
            self.api,
        )
        details = self.api.messages[-1]
        self.assertIn("Release year: 2022", details[1])
        self.assertIn("rate_track", str(details[2]))

        for action, value in (
            ("rate_track", "4"),
            ("note_track", "favorite arrangement"),
            ("tag_track", "melancholic"),
            ("edit_track", "Evening Reprise | Nova"),
        ):
            handle_callback(
                {
                    "id": f"callback-{action}",
                    "from": {"id": 7},
                    "message": {"chat": {"id": 7, "type": "private"}, "message_id": 221},
                    "data": f"{action}:{track_id}",
                },
                self.database,
                self.api,
            )
            handle_message(
                {
                    "chat": {"id": 7, "type": "private"},
                    "from": {"id": 7},
                    "text": value,
                },
                self.database,
                self.api,
            )

        track = self.database.get_track(7, track_id)
        self.assertEqual(track["title"], "Evening Reprise")
        self.assertEqual(track["rating"], 4)
        self.assertEqual(track["note"], "favorite arrangement")
        self.assertEqual(self.database.list_track_tags(7, track_id), ["melancholic"])

    def test_rename_playlist_from_inline_button(self) -> None:
        playlist_id = next(
            row["id"] for row in self.database.list_playlists(7) if row["name"] == "Road Trip"
        )
        handle_callback(
            {
                "id": "callback-rename",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 209},
                "data": f"rename:{playlist_id}",
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.database.get_user_state(7), f"rename:{playlist_id}")

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Favorites renamed",
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.database.get_playlist(7, playlist_id)["name"], "Favorites renamed")
        self.assertIsNone(self.database.get_user_state(7))
        self.assertTrue(any("Renamed playlist" in text for _, text, _ in self.api.messages))

    def test_duplicate_rename_keeps_name_and_returns_playlist_menu(self) -> None:
        self.database.create_playlist(7, "Already used")
        playlist_id = next(
            row["id"] for row in self.database.list_playlists(7) if row["name"] == "Road Trip"
        )
        handle_callback(
            {
                "id": "callback-rename-duplicate",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 210},
                "data": f"rename:{playlist_id}",
            },
            self.database,
            self.api,
        )
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Already used",
            },
            self.database,
            self.api,
        )
        self.assertEqual(self.database.get_playlist(7, playlist_id)["name"], "Road Trip")
        self.assertIsNone(self.database.get_user_state(7))
        self.assertTrue(any("already in use" in text for _, text, _ in self.api.messages))

    def test_create_playlist_from_audio_then_add_via_inline_buttons(self) -> None:
        self.database.delete_playlist(7, "Road Trip")
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 321,
                "audio": {"file_name": "new-song.mp3", "file_unique_id": "pending-file"},
            },
            self.database,
            self.api,
        )
        track_id = self.database.list_library(7, 10)[0]["id"]
        handle_callback(
            {
                "id": "callback-open-collections",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 202},
                "data": f"library_add:{track_id}",
            },
            self.database,
            self.api,
        )
        create_callback = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        handle_callback(
            {
                "id": "callback-create-from-audio",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 202},
                "data": create_callback,
            },
            self.database,
            self.api,
        )
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "New playlist"},
            self.database,
            self.api,
        )

        tracks = self.database.list_tracks(7, "New playlist")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["source_message_id"], 321)
        self.assertEqual(tracks[0]["file_unique_id"], "pending-file")

    def test_duplicate_playlist_name_returns_to_audio_chooser(self) -> None:
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 654,
                "audio": {"title": "Pending song"},
            },
            self.database,
            self.api,
        )
        track_id = self.database.list_library(7, 10)[0]["id"]
        handle_callback(
            {
                "id": "callback-open-collections",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 207},
                "data": f"library_add:{track_id}",
            },
            self.database,
            self.api,
        )
        create_callback = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"][1][0][
            "callback_data"
        ]
        handle_callback(
            {
                "id": "callback-create-duplicate",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 207},
                "data": create_callback,
            },
            self.database,
            self.api,
        )
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "Road Trip"},
            self.database,
            self.api,
        )
        self.assertIn("already exists", self.api.messages[-1][1])
        handle_message(
            {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "Road Trip 2"},
            self.database,
            self.api,
        )
        tracks = self.database.list_tracks(7, "Road Trip")
        self.assertEqual(tracks, [])
        self.assertEqual(
            len(self.database.list_tracks(7, "Road Trip 2")),
            1,
        )

    def test_playlist_menu_paginates_large_lists(self) -> None:
        for index in range(9):
            self.database.create_playlist(7, f"Playlist {index:02}")
        handle_callback(
            {
                "id": "callback-playlists",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 205},
                "data": "playlists",
            },
            self.database,
            self.api,
        )
        first_keyboard = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        next_data = first_keyboard[8][0]["callback_data"]
        self.assertEqual(next_data, "playlist_page:1")

        handle_callback(
            {
                "id": "callback-playlist-next",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 205},
                "data": next_data,
            },
            self.database,
            self.api,
        )
        second_keyboard = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(second_keyboard[0][0]["text"], "🎵 Playlist 08 (0)")
        self.assertEqual(second_keyboard[2][0]["callback_data"], "playlist_page:0")

    def test_optional_collection_picker_paginates_for_saved_library_track(self) -> None:
        for index in range(9):
            self.database.create_playlist(7, f"Playlist {index:02}")
        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "message_id": 777,
                "audio": {"title": "Paged track"},
            },
            self.database,
            self.api,
        )
        track_id = self.database.list_library(7, 10)[0]["id"]
        handle_callback(
            {
                "id": "callback-open-collections",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 206},
                "data": f"library_add:{track_id}",
            },
            self.database,
            self.api,
        )
        first_keyboard = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        next_data = first_keyboard[8][0]["callback_data"]
        self.assertEqual(next_data, f"track_choose_page:1:{track_id}")

        handle_callback(
            {
                "id": "callback-choose-next",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 206},
                "data": next_data,
            },
            self.database,
            self.api,
        )
        second_keyboard = self.api.calls[-1][1]["reply_markup"]["inline_keyboard"]
        add_data = second_keyboard[0][0]["callback_data"]
        self.assertTrue(add_data.endswith(f":{track_id}"))
        self.assertEqual(
            self.database.library_repository.get_track_id_for_source_reference(
                7, 7, 777
            ),
            track_id,
        )

    @patch("bot.urlopen")
    def test_telegram_api_reports_invalid_json(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        urlopen.return_value = response

        with self.assertRaisesRegex(TelegramAPIError, "invalid JSON"):
            TelegramAPI("token").call("getUpdates")

    @patch("bot.urlopen")
    def test_telegram_api_reports_missing_result(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        urlopen.return_value = response

        with self.assertRaisesRegex(TelegramAPIError, "did not include a result"):
            TelegramAPI("token").call("getUpdates")


if __name__ == "__main__":
    unittest.main()
