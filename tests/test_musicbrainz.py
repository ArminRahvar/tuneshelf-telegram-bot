import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from musicbrainz import MusicBrainzError, MusicBrainzProvider


class MusicBrainzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = MusicBrainzProvider("musicbot@example.org")

    @patch("musicbrainz.urlopen")
    def test_lookup_returns_recording_artist_release_and_identifier(
        self, urlopen: MagicMock
    ) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "recordings": [
                    {
                        "id": "recording-mbid",
                        "title": "Canonical Track",
                        "score": "98",
                        "artist-credit": [
                            {
                                "joinphrase": " & ",
                                "artist": {"name": "Example Artist"},
                            },
                            {"artist": {"name": "Guest Artist"}},
                        ],
                        "releases": [{"title": "Example Album"}],
                    }
                ]
            }
        ).encode()
        urlopen.return_value = response

        result = self.provider.lookup_recording('Track: "Name"', "Example Artist")

        self.assertIsNotNone(result)
        self.assertEqual(result.recording_id, "recording-mbid")
        self.assertEqual(result.title, "Canonical Track")
        self.assertEqual(result.artist, "Example Artist & Guest Artist")
        self.assertEqual(result.release_title, "Example Album")
        request = urlopen.call_args.args[0]
        self.assertIn("TelegramPlaylistBot/1.0 (mailto:musicbot@example.org)", request.get_header("User-agent"))
        query = parse_qs(urlparse(request.full_url).query)["query"][0]
        self.assertIn(r'Track\: \"Name\"', query)
        self.assertIn('artist:"Example Artist"', query)

    @patch("musicbrainz.urlopen")
    def test_lookup_caches_positive_and_negative_results(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"recordings":[]}'
        urlopen.return_value = response

        self.assertIsNone(self.provider.lookup_recording("Unknown"))
        self.assertIsNone(self.provider.lookup_recording("Unknown"))
        self.assertEqual(urlopen.call_count, 1)

    @patch("musicbrainz.urlopen")
    def test_low_confidence_result_is_not_used(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"recordings":[{"id":"weak","title":"Different","score":"42"}]}'
        )
        urlopen.return_value = response

        self.assertIsNone(self.provider.lookup_recording("Song"))

    @patch("musicbrainz.urlopen")
    def test_search_returns_multiple_confident_matches_in_score_order(
        self, urlopen: MagicMock
    ) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "recordings": [
                    {"id": "lower", "title": "Track B", "score": "87"},
                    {"id": "higher", "title": "Track A", "score": "98"},
                    {"id": "weak", "title": "Weak result", "score": "20"},
                ]
            }
        ).encode()
        urlopen.return_value = response

        matches = self.provider.search_recordings("Track")

        self.assertEqual([match.recording_id for match in matches], ["higher", "lower"])

    @patch("musicbrainz.urlopen")
    def test_musicbrainz_recording_url_lookup(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "id": "12345678-1234-1234-1234-123456789abc",
                "title": "Linked Recording",
                "artist-credit": [{"artist": {"name": "Linked Artist"}}],
                "releases": [{"title": "Linked Album"}],
            }
        ).encode()
        urlopen.return_value = response

        result = self.provider.lookup_url(
            "https://musicbrainz.org/recording/12345678-1234-1234-1234-123456789abc"
        )

        self.assertEqual(result.title, "Linked Recording")
        self.assertEqual(result.artist, "Linked Artist")
        self.assertEqual(result.release_title, "Linked Album")

    def test_musicbrainz_url_lookup_rejects_other_hosts(self) -> None:
        with self.assertRaisesRegex(MusicBrainzError, "Only MusicBrainz"):
            self.provider.lookup_url("https://attacker.example/recording/12345678-1234-1234-1234-123456789abc")

    @patch("musicbrainz.urlopen")
    def test_invalid_response_is_reported(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        urlopen.return_value = response

        with self.assertRaisesRegex(MusicBrainzError, "invalid JSON"):
            self.provider.lookup_recording("Song")

    def test_contact_email_is_required_for_provider_user_agent(self) -> None:
        with self.assertRaises(ValueError):
            MusicBrainzProvider("")


if __name__ == "__main__":
    unittest.main()
