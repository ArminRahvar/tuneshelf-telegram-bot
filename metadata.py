from dataclasses import dataclass
from typing import Any, Protocol


class MetadataProviderError(Exception):
    pass


@dataclass(frozen=True)
class MetadataSearchResult:
    provider: str
    identifier: str
    title: str
    artist: str | None
    release_title: str | None
    release_year: int | None
    provider_url: str
    score: int | None = None
    musicbrainz_recording_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "identifier": self.identifier,
            "title": self.title,
            "artist": self.artist,
            "release_title": self.release_title,
            "release_year": self.release_year,
            "provider_url": self.provider_url,
            "score": self.score,
            "musicbrainz_recording_id": self.musicbrainz_recording_id,
        }


class MetadataSearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> list[MetadataSearchResult]: ...
