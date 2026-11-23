import unittest
from typing import Any

from metadata import MetadataProviderError
from youtube_music import YouTubeMusicProvider
from ytmusicapi.exceptions import YTMusicServerError


class FakeYTMusicClient:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, str, int]] = []
        self.error: Exception | None = None

    def search(
        self,
        query: str,
        filter: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, filter, limit))
        if self.error is not None:
            raise self.error
        return self.items


class YouTubeMusicProviderTests(unittest.TestCase):
    def test_search_normalizes_song_results_and_discards_invalid_duplicates(self) -> None:
        client = FakeYTMusicClient(
            [
                {"videoId": "invalid id", "title": "Invalid"},
                {
                    "videoId": "video-id",
                    "title": " Track ",
                    "artists": [{"name": "Artist"}, {"name": "Guest"}],
                    "album": {"name": "Album"},
                    "year": "2024",
                },
                {
                    "videoId": "video-id",
                    "title": "Duplicate",
                },
                {"videoId": "second-id", "title": "Second"},
            ]
        )

        results = YouTubeMusicProvider(client).search(" Track ", limit=2)

        self.assertEqual(client.calls, [("Track", "songs", 2)])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Track")
        self.assertEqual(results[0].artist, "Artist & Guest")
        self.assertEqual(results[0].release_title, "Album")
        self.assertEqual(results[0].release_year, 2024)
        self.assertEqual(results[0].provider, "youtube_music")
        self.assertEqual(results[0].identifier, "video-id")
        self.assertEqual(
            results[0].provider_url,
            "https://music.youtube.com/watch?v=video-id",
        )

    def test_invalid_queries_do_not_call_provider(self) -> None:
        client = FakeYTMusicClient()
        provider = YouTubeMusicProvider(client)

        self.assertEqual(provider.search(" "), [])
        self.assertEqual(provider.search("https://example.com/song"), [])
        self.assertEqual(provider.search("HTTPS://example.com/song"), [])
        self.assertEqual(provider.search("track", limit=26), [])
        self.assertEqual(client.calls, [])

    def test_provider_errors_are_reported_as_metadata_provider_errors(self) -> None:
        client = FakeYTMusicClient()
        client.error = YTMusicServerError("service unavailable")

        with self.assertRaisesRegex(MetadataProviderError, "service unavailable"):
            YouTubeMusicProvider(client).search("Track")
