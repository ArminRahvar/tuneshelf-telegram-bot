import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import TelegramAPI, TelegramAPIError, handle_callback, handle_message
from database import Database
from musicbrainz import MusicBrainzError, RecordingMetadata


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict[str, object] | None]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, object] | None = None
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))

    def call(self, method: str, **parameters: object) -> object:
        self.calls.append((method, parameters))
        return {}


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "test.sqlite3")
        self.database.create_playlist(7, "Road Trip")
        self.api = FakeTelegramAPI()

    def tearDown(self) -> None:
        self.directory.cleanup()

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
        keyboard = self.api.messages[-1][2]["inline_keyboard"]
        add_callback = keyboard[0][0]["callback_data"]
        handle_callback(
            {
                "id": "callback-add",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 200},
                "data": add_callback,
            },
            self.database,
            self.api,
        )
        handle_callback(
            {
                "id": "callback-play",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 200},
                "data": f"play:{self.database.list_playlists(7)[0]['id']}",
            },
            self.database,
            self.api,
        )

        copy_calls = [parameters for method, parameters in self.api.calls if method == "copyMessage"]
        self.assertEqual(
            copy_calls,
            [{"chat_id": 7, "from_chat_id": 7, "message_id": 123}],
        )
        tracks = self.database.list_tracks(7, "Road Trip")
        self.assertEqual(tracks[0]["title"], "Track")
        self.assertEqual(tracks[0]["file_unique_id"], "file-unique")
        self.assertIsNotNone(
            self.database.list_message_references(7, tracks[0]["id"])[0]["last_verified_at"]
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
        callback_data = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        handle_callback(
            {
                "id": "callback-add-enriched",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 216},
                "data": callback_data,
            },
            self.database,
            self.api,
        )

        track = self.database.list_tracks(7, "Road Trip")[0]
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
        pending = self.database.get_pending_track(7, 7, 812)
        self.assertEqual(pending["title"], "Original Title")
        self.assertEqual(pending["performer"], "Original Artist")
        self.assertIsNone(pending["musicbrainz_recording_id"])

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
        callback_data = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        handle_callback(
            {
                "id": "add-document",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 219},
                "data": callback_data,
            },
            self.database,
            self.api,
        )

        track = self.database.list_tracks(7, "Road Trip")[0]
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
            def search_recordings(
                self, title: str, artist: str | None = None
            ) -> list[RecordingMetadata]:
                if (title, artist) != ("Metadata Track", "Artist"):
                    raise AssertionError("Text query was not split into title and artist.")
                return [
                    RecordingMetadata(
                        "recording-id", "Metadata Track", "Artist", "Album", 95
                    )
                ]

            def lookup_url(self, url: str) -> RecordingMetadata:
                raise AssertionError("No URL was sent in this test.")

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Metadata Track - Artist",
            },
            self.database,
            self.api,
            FakeProvider(),
        )

        rows = self.database.list_metadata_only_tracks(7)
        self.assertEqual(rows[0]["title"], "Metadata Track")
        self.assertIn("No playable Telegram copy", self.api.messages[-1][1])

    def test_ambiguous_metadata_search_requires_user_selection(self) -> None:
        class FakeProvider:
            def search_recordings(
                self, title: str, artist: str | None = None
            ) -> list[RecordingMetadata]:
                return [
                    RecordingMetadata("first-id", "First result", "Artist", None, 97),
                    RecordingMetadata("second-id", "Second result", "Artist", "Album", 92),
                ]

            def lookup_url(self, url: str) -> RecordingMetadata:
                raise AssertionError("No URL was sent in this test.")

        handle_message(
            {
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
                "text": "Possible recording",
            },
            self.database,
            self.api,
            FakeProvider(),
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
        self.assertEqual(rows[0]["musicbrainz_recording_id"], "second-id")

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
            callback_data = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
            handle_callback(
                {
                    "id": f"callback-add-{_}",
                    "from": {"id": 7},
                    "message": {
                        "chat": {"id": 7, "type": "private"},
                        "message_id": 212,
                    },
                    "data": callback_data,
                },
                self.database,
                self.api,
            )

        tracks = self.database.list_tracks(7, "Road Trip")
        self.assertEqual(len(tracks), 1)
        self.assertTrue(
            any(
                "already in" in parameters.get("text", "")
                for method, parameters in self.api.calls
                if method == "editMessageText"
            )
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
        choose_keyboard = self.api.messages[-1][2]["inline_keyboard"]
        create_callback = choose_keyboard[0][0]["callback_data"]
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

        add_keyboard = self.api.messages[-1][2]["inline_keyboard"]
        add_callback = add_keyboard[0][0]["callback_data"]
        handle_callback(
            {
                "id": "callback-save-new-audio",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 203},
                "data": add_callback,
            },
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
        create_callback = self.api.messages[-1][2]["inline_keyboard"][1][0]["callback_data"]
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
        self.assertIn("already exists", self.api.messages[-2][1])
        add_callback = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        handle_callback(
            {
                "id": "callback-add-after-duplicate",
                "from": {"id": 7},
                "message": {"chat": {"id": 7, "type": "private"}, "message_id": 208},
                "data": add_callback,
            },
            self.database,
            self.api,
        )
        tracks = self.database.list_tracks(7, "Road Trip")
        self.assertEqual(tracks[0]["source_message_id"], 654)

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

    def test_audio_playlist_chooser_paginates_and_preserves_source_reference(self) -> None:
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
        first_keyboard = self.api.messages[-1][2]["inline_keyboard"]
        next_data = first_keyboard[8][0]["callback_data"]
        self.assertEqual(next_data, "choose_page:1:7:777")

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
        self.assertTrue(add_data.endswith(":7:777"))

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
