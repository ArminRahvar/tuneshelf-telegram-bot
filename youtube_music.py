import json
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

from requests import RequestException
from ytmusicapi.exceptions import YTMusicError

from metadata import MetadataProviderError, MetadataSearchResult


class YTMusicClient(Protocol):
    def search(
        self,
        query: str,
        filter: Literal["songs"],
        limit: int,
    ) -> list[dict[str, Any]]: ...


class YouTubeMusicProvider:
    PROVIDER = "youtube_music"

    def __init__(self, client: YTMusicClient | None = None) -> None:
        if client is None:
            from ytmusicapi import YTMusic

            client = YTMusic()
        self.client = client

    def search(self, query: str, limit: int = 5) -> list[MetadataSearchResult]:
        query = query.strip()
        if not query or limit < 1 or limit > 25 or _is_url(query):
            return []
        try:
            items = self.client.search(query, filter="songs", limit=limit)
        except (YTMusicError, RequestException, json.JSONDecodeError) as error:
            raise MetadataProviderError(
                f"YouTube Music search failed: {error}"
            ) from error

        results: list[MetadataSearchResult] = []
        seen_ids: set[str] = set()
        for item in items:
            video_id = item.get("videoId")
            title = item.get("title")
            if (
                not isinstance(video_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id)
                or not isinstance(title, str)
                or not title.strip()
                or video_id in seen_ids
            ):
                continue
            seen_ids.add(video_id)
            results.append(
                MetadataSearchResult(
                    provider=self.PROVIDER,
                    identifier=video_id,
                    title=title.strip(),
                    artist=_artists(item.get("artists")),
                    release_title=_album(item.get("album")),
                    release_year=_year(item.get("year")),
                    provider_url=(
                        "https://music.youtube.com/watch?"
                        f"{urlencode({'v': video_id})}"
                    ),
                )
            )
            if len(results) >= limit:
                break
        return results


def _is_url(value: str) -> bool:
    return value.lower().startswith(("https://", "http://"))


def _artists(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    names = [
        artist["name"].strip()
        for artist in value
        if isinstance(artist, dict)
        and isinstance(artist.get("name"), str)
        and artist["name"].strip()
    ]
    return " & ".join(names) or None


def _album(value: object) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _year(value: object) -> int | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}", value.strip()) is None:
        return None
    year = int(value)
    return year if 1000 <= year <= 9999 else None
