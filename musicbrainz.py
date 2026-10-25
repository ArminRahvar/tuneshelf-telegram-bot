import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


API_URL = "https://musicbrainz.org/ws/2/recording/"
MIN_REQUEST_INTERVAL = 1.1
MIN_MATCH_SCORE = 80


class MusicBrainzError(Exception):
    pass


@dataclass(frozen=True)
class RecordingMetadata:
    recording_id: str
    title: str
    artist: str | None
    release_title: str | None
    score: int
    release_year: int | None = None


class MusicBrainzProvider:
    def __init__(self, contact_email: str, timeout: float = 8.0) -> None:
        if not contact_email.strip() or "@" not in contact_email:
            raise ValueError("A valid contact email is required for the MusicBrainz User-Agent.")
        self.user_agent = f"TelegramPlaylistBot/1.0 (mailto:{contact_email.strip()})"
        self.timeout = timeout
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, int], tuple[RecordingMetadata, ...]] = {}

    @staticmethod
    def _escape_query_value(value: str) -> str:
        special = set(r'+-!(){}[]^"~*?:\/&|')
        return "".join(f"\\{char}" if char in special else char for char in value)

    @staticmethod
    def _artist_name(credits: object) -> str | None:
        if not isinstance(credits, list):
            return None
        rendered: list[str] = []
        for credit in credits:
            if not isinstance(credit, dict):
                continue
            artist = credit.get("artist")
            name = credit.get("name")
            if not isinstance(name, str) and isinstance(artist, dict):
                name = artist.get("name")
            if isinstance(name, str) and name:
                rendered.append(name)
                joinphrase = credit.get("joinphrase")
                if isinstance(joinphrase, str):
                    rendered.append(joinphrase)
        return "".join(rendered).strip() or None

    def _wait_for_rate_limit(self) -> None:
        with self._lock:
            delay = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.monotonic()

    def _cache_result(
        self, key: tuple[str, str, int], value: tuple[RecordingMetadata, ...]
    ) -> None:
        if key not in self._cache and len(self._cache) >= 512:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = value

    def search_recordings(
        self,
        title: str,
        artist: str | None = None,
        limit: int = 5,
    ) -> list[RecordingMetadata]:
        title = title.strip()
        artist = artist.strip() if artist else ""
        if not title or limit < 1 or limit > 25:
            return []
        cache_key = (title.casefold(), artist.casefold(), limit)
        if cache_key in self._cache:
            return list(self._cache[cache_key][:limit])

        query = f'recording:"{self._escape_query_value(title)}"'
        if artist:
            query += f' AND artist:"{self._escape_query_value(artist)}"'
        url = f"{API_URL}?{urlencode({'query': query, 'fmt': 'json', 'limit': limit})}"
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        self._wait_for_rate_limit()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise MusicBrainzError(f"MusicBrainz request failed: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MusicBrainzError("MusicBrainz returned invalid JSON") from error

        if not isinstance(payload, dict) or not isinstance(payload.get("recordings"), list):
            raise MusicBrainzError("MusicBrainz returned an invalid recording search response")

        candidates = [
            recording
            for recording in payload["recordings"]
            if isinstance(recording, dict)
            and isinstance(recording.get("id"), str)
            and isinstance(recording.get("title"), str)
        ]
        def score_for(recording: dict[str, object]) -> int:
            try:
                return int(recording.get("score", 0) or 0)
            except (TypeError, ValueError):
                return 0

        ranked = sorted(candidates, key=score_for, reverse=True)
        results: list[RecordingMetadata] = []
        for candidate in ranked:
            score = score_for(candidate)
            if score < MIN_MATCH_SCORE:
                continue
            releases = candidate.get("releases")
            release_title = None
            release_year = None
            if isinstance(releases, list) and releases and isinstance(releases[0], dict):
                candidate_title = releases[0].get("title")
                if isinstance(candidate_title, str):
                    release_title = candidate_title
                release_year = self._year(releases[0].get("date"))
            if release_year is None:
                release_year = self._year(candidate.get("first-release-date"))
            results.append(
                RecordingMetadata(
                    recording_id=candidate["id"],
                    title=candidate["title"],
                    artist=self._artist_name(candidate.get("artist-credit")),
                    release_title=release_title,
                    score=score,
                    release_year=release_year,
                )
            )
        self._cache_result(cache_key, tuple(results))
        return results

    def lookup_recording(
        self, title: str, artist: str | None = None
    ) -> RecordingMetadata | None:
        results = self.search_recordings(title, artist, limit=5)
        return results[0] if results else None

    def lookup_url(self, url: str) -> RecordingMetadata:
        parsed = urlparse(url)
        recording_match = re.fullmatch(
            r"/recording/([0-9a-fA-F-]{36})/?", parsed.path
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"musicbrainz.org", "www.musicbrainz.org"}
            or recording_match is None
            or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                recording_match.group(1),
            )
        ):
            raise MusicBrainzError("Only MusicBrainz recording URLs are supported.")
        recording_id = recording_match.group(1).lower()
        url = f"{API_URL}{recording_id}?{urlencode({'inc': 'artists+releases', 'fmt': 'json'})}"
        request = Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        self._wait_for_rate_limit()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise MusicBrainzError(f"MusicBrainz request failed: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MusicBrainzError("MusicBrainz returned invalid JSON") from error
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("id"), str)
            or not isinstance(payload.get("title"), str)
        ):
            raise MusicBrainzError("MusicBrainz returned an invalid recording response")
        releases = payload.get("releases")
        release_title = None
        release_year = None
        if isinstance(releases, list) and releases and isinstance(releases[0], dict):
            candidate_title = releases[0].get("title")
            if isinstance(candidate_title, str):
                release_title = candidate_title
            release_year = self._year(releases[0].get("date"))
        return RecordingMetadata(
            recording_id=payload["id"],
            title=payload["title"],
            artist=self._artist_name(payload.get("artist-credit")),
            release_title=release_title,
            score=100,
            release_year=release_year or self._year(payload.get("first-release-date")),
        )

    @staticmethod
    def _year(value: object) -> int | None:
        if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
            return None
        year = int(value[:4])
        return year if 1000 <= year <= 9999 else None
