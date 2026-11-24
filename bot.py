import json
import logging
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from archive import PersonalMusicArchive, TelegramMessageReference, TrackMetadata
from app.database.runtime import RuntimeDatabase
from database import Database
from metadata import MetadataProviderError, MetadataSearchProvider
from musicbrainz import MusicBrainzError, MusicBrainzProvider
from telegram_delivery import TelegramDeliveryService
from youtube_music import YouTubeMusicProvider
from app.integrations.downloader import FFmpegProcessor, YtDlpDownloader
from app.services.download_service import DownloadService


LOGGER = logging.getLogger("playlist_bot")
API_BASE = "https://api.telegram.org/bot{token}/{method}"
TRACKS_PER_PAGE = 8
PLAYLISTS_PER_PAGE = 8
SEARCH_RESULTS_PER_PAGE = 8
MAX_COLLECTION_DELIVERY = 20
COLLECTION_DELIVERY_INTERVAL = 1.1


class TelegramAPIError(Exception):
    def __init__(
        self,
        message: str,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, **parameters: Any) -> Any:
        url = API_BASE.format(token=self.token, method=method)
        payload = urlencode(
            {
                key: json.dumps(value) if isinstance(value, (list, dict)) else value
                for key, value in parameters.items()
                if value is not None
            }
        ).encode()
        request = Request(url, data=payload)
        try:
            with urlopen(request, timeout=40) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise TelegramAPIError(str(error)) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TelegramAPIError("Telegram returned an invalid JSON response") from error
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram returned an invalid API response")
        if result.get("ok") is not True:
            parameters = result.get("parameters", {})
            retry_after = (
                parameters.get("retry_after")
                if isinstance(parameters, dict)
                else None
            )
            raise TelegramAPIError(
                result.get("description", "Telegram API request failed"),
                error_code=result.get("error_code"),
                retry_after=retry_after if isinstance(retry_after, int) else None,
            )
        if "result" not in result:
            raise TelegramAPIError("Telegram API response did not include a result")
        return result["result"]

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)

    def send_audio(
        self,
        chat_id: int,
        file_path: Path,
        title: str,
        performer: str | None = None,
    ) -> dict[str, Any]:
        url = API_BASE.format(token=self.token, method="sendAudio")
        boundary = f"tuneshelf-{uuid.uuid4().hex}"
        fields: list[tuple[str, str]] = [
            ("chat_id", str(chat_id)),
            ("title", title),
        ]
        if performer:
            fields.append(("performer", performer))

        body = bytearray()
        for name, value in fields:
            body.extend(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            )
        body.extend(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="audio"; '
                f'filename="{file_path.name}"\r\n'
                "Content-Type: audio/mpeg\r\n\r\n"
            ).encode()
        )
        body.extend(file_path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())

        request = Request(
            url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise TelegramAPIError(str(error)) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TelegramAPIError("Telegram returned an invalid JSON response") from error
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramAPIError(
                result.get("description", "Telegram API request failed")
                if isinstance(result, dict)
                else "Telegram API request failed"
            )
        if not isinstance(result.get("result"), dict):
            raise TelegramAPIError("Telegram returned an invalid sendAudio response")
        return result["result"]


def _button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def _markup(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _reply_menu_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "➕ Add Music"}, {"text": "📚 My Library"}],
            [{"text": "🔎 Search"}, {"text": "🗂 Collections"}],
            [{"text": "✨ Discover"}, {"text": "📊 Statistics"}],
            [{"text": "⬇️ Downloads"}, {"text": "⚙️ Settings"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _edit_or_send(
    api: TelegramAPI,
    chat_id: int,
    text: str,
    keyboard: list[list[dict[str, str]]],
    message_id: int | None = None,
) -> None:
    reply_markup = _markup(keyboard)
    if message_id is None:
        api.send_message(chat_id, text, reply_markup=reply_markup)
    else:
        api.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )


def _home_keyboard() -> list[list[dict[str, str]]]:
    return [
        [_button("➕ Add Music", "add_music"), _button("📚 My Library", "library")],
        [_button("🔎 Search", "search"), _button("🗂 Collections", "playlists")],
        [_button("✨ Discover", "discover"), _button("📊 Statistics", "stats")],
        [_button("⬇️ Downloads", "downloads")],
        [_button("⚙️ Settings", "settings")],
        [_button("➕ Create collection", "new")],
    ]


def _show_home(
    api: TelegramAPI, chat_id: int, message_id: int | None = None, text: str | None = None
) -> None:
    api.send_message(chat_id, "Use the menu buttons to navigate.", _reply_menu_markup())
    _edit_or_send(
        api,
        chat_id,
        text
        or "🎧 Your Telegram music library\n\nChoose an option. "
        "Use /track, /edit, /rate, /note, /tag, /untag, and /link with a track ID "
        "shown in your collection. Use /collections and /addtrack to organize tracks.",
        _home_keyboard(),
        message_id,
    )


def _show_library(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
    page: int = 0,
) -> None:
    page = max(0, page)
    tracks = database.library_repository.list_library(
        user_id, SEARCH_RESULTS_PER_PAGE + 1, page * SEARCH_RESULTS_PER_PAGE
    )
    has_next = len(tracks) > SEARCH_RESULTS_PER_PAGE
    tracks = tracks[:SEARCH_RESULTS_PER_PAGE]
    keyboard: list[list[dict[str, str]]] = []
    for track in tracks:
        state = "▶" if track["is_playable"] else "ℹ"
        label = f"{state} #{track['id']} {_format_track(track)}"
        row = [_button(label[:60], f"track:{track['id']}")]
        if track["is_playable"]:
            row.append(_button("▶ Play", f"search_play:{track['id']}"))
        keyboard.append(row)
    navigation: list[dict[str, str]] = []
    if page > 0:
        navigation.append(_button("⬅️ Previous", f"library_page:{page - 1}"))
    if has_next:
        navigation.append(_button("Next ➡️", f"library_page:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([_button("⬅️ Main menu", "home")])
    text = (
        f"📚 Your library — page {page + 1}."
        if tracks
        else "Your library is empty. Use Add Music, then send audio to the bot."
    )
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _show_statistics(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    stats = database.library_repository.get_statistics(user_id)
    lines = [
        "📊 Your archive statistics",
        f"Saved tracks: {stats['total_tracks']}",
        f"Metadata-only tracks: {stats['metadata_only_tracks']}",
        f"Collections: {stats['collection_count']}",
    ]
    artists = stats["artists"]
    lines.append(
        "Most saved artists: "
        + (
            ", ".join(f"{row['name']} ({row['track_count']})" for row in artists)
            if artists
            else "not enough artist metadata"
        )
    )
    ratings = stats["ratings"]
    lines.append(
        "Rating distribution: "
        + (
            ", ".join(f"{row['rating']}★: {row['track_count']}" for row in ratings)
            if ratings
            else "no ratings yet"
        )
    )
    tags = stats["tags"]
    lines.append(
        "Most used tags: "
        + (
            ", ".join(f"{row['name']} ({row['track_count']})" for row in tags)
            if tags
            else "no tags yet"
        )
    )
    _edit_or_send(
        api,
        chat_id,
        "\n".join(lines),
        [[_button("⬅️ Main menu", "home")]],
        message_id,
    )


def _show_track_detail(
    user_id: int,
    chat_id: int,
    track_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    track = database.library_repository.get_track(user_id, track_id)
    if track is None:
        _show_library(user_id, chat_id, database, api, message_id)
        return
    tags = database.library_repository.list_track_tags(user_id, track_id) or []
    references = database.library_repository.list_message_references(user_id, track_id) or []
    available_count = sum(bool(reference["is_available"]) for reference in references)
    lines = [
        f"#{track_id} {_format_track(track)}",
        f"Collection: {track['playlist_name'] or 'not in a playlist'}",
        f"Release year: {track['release_year'] or 'unknown'}",
        f"Duration: {track['duration_seconds']} sec"
        if track["duration_seconds"] is not None
        else "Duration: unknown",
        f"Rating: {track['rating']}/5" if track["rating"] is not None else "Rating: not set",
        f"Note: {track['note']}" if track["note"] else "Note: not set",
        "Tags: " + (", ".join(tags) if tags else "none"),
        f"Playable message references: {available_count}/{len(references)}",
    ]
    keyboard: list[list[dict[str, str]]] = []
    if available_count:
        keyboard.append([_button("▶ Retrieve track", f"search_play:{track_id}")])
    keyboard.extend(
        [
            [
                _button("⭐ Set rating", f"rate_track:{track_id}"),
                _button("📝 Edit note", f"note_track:{track_id}"),
            ],
            [
                _button("🏷 Add tag", f"tag_track:{track_id}"),
                _button("✏️ Edit metadata", f"edit_track:{track_id}"),
            ],
            [_button("🔗 Add another Telegram source", f"link_track:{track_id}")],
            [_button("⬅️ My Library", "library")],
        ]
    )
    _edit_or_send(api, chat_id, "\n".join(lines), keyboard, message_id)


def _show_discovery(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    suggestions = database.library_repository.discover_tracks(
        user_id, SEARCH_RESULTS_PER_PAGE
    )
    keyboard = [
        [_button(f"▶ #{track['id']} {_format_track(track)}"[:60], f"search_play:{track['id']}")]
        for track in suggestions
    ]
    keyboard.append([_button("⬅️ Main menu", "home")])
    text = (
        "✨ Related tracks from your own library, ranked by shared artists and manually "
        "assigned tags:"
        if suggestions
        else "Not enough shared artist or tag data for meaningful recommendations yet. "
        "Add more tracks and assign personal tags."
    )
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _show_settings(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    mode = PersonalMusicArchive(database).get_delivery_mode(user_id)
    description = (
        "Copy source message (recommended; hides source attribution)."
        if mode == "copy"
        else "Forward source message (preserves source attribution)."
    )
    _edit_or_send(
        api,
        chat_id,
        f"⚙️ Delivery preference: {description}\n\nTelegram must still allow the bot to "
        "access the original source message.",
        [
            [
                _button("Copy without attribution", "delivery:copy"),
                _button("Forward with attribution", "delivery:forward"),
            ],
            [_button("⬅️ Main menu", "home")],
        ],
        message_id,
    )


def _show_downloads(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    jobs = database.download_repository.list_for_user(user_id)
    if not jobs:
        text = "⬇️ Downloads\n\nNo download jobs yet."
    else:
        lines = ["⬇️ Downloads"]
        for job in jobs[:10]:
            source = str(job["source_url"])
            if len(source) > 48:
                source = source[:45] + "..."
            lines.append(
                f"#{job['id']} · {job['status']} · {job['progress']}% · {source}"
            )
        text = "\n".join(lines)

    keyboard: list[list[dict[str, str]]] = []
    for job in jobs[:10]:
        job_id = int(job["id"])
        if job["status"] in {"failed", "cancelled"}:
            keyboard.append([
                _button("🔄 Retry", f"download_retry:{job_id}")
            ])
        elif job["status"] in {"pending", "processing", "converting", "uploading"}:
            keyboard.append([
                _button("❌ Cancel", f"download_cancel:{job_id}")
            ])
    keyboard.append([_button("➕ Add download URL", "download_add")])
    keyboard.append([_button("⬅️ Main menu", "home")])
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _clear_interaction_state(database: Database, user_id: int) -> None:
    state = database.get_user_state(user_id)
    if state is not None and state.count(":") == 2:
        _, source_chat_id, source_message_id = state.split(":")
        database.delete_pending_track(user_id, int(source_chat_id), int(source_message_id))
    database.clear_user_state(user_id)


def _audio_details(
    message: dict[str, Any],
) -> tuple[str, str | None, str, str | None, str | None, int | None, str | None, str | None] | None:
    media_type = "audio"
    audio = message.get("audio")
    if audio is None and message.get("voice") is not None:
        audio = message["voice"]
        media_type = "voice"
    if audio is None:
        document = message.get("document")
        if document is not None and str(document.get("mime_type", "")).startswith("audio/"):
            audio = document
            media_type = "document"
    if audio is None:
        return None

    original_filename = audio.get("file_name")
    title = audio.get("title") or original_filename
    if not title:
        title = "Voice message" if media_type == "voice" else "Untitled audio"
    return (
        title,
        audio.get("performer"),
        media_type,
        audio.get("file_unique_id"),
        audio.get("file_id"),
        audio.get("duration"),
        audio.get("mime_type"),
        original_filename,
    )


def _format_track(track: dict[str, Any]) -> str:
    title = track["title"]
    performer = track["performer"]
    formatted = f"{title} — {performer}" if performer else title
    release_title = track["release_title"]
    return f"{formatted} [{release_title}]" if release_title else formatted


def _show_playlists(
    user_id: int,
    chat_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None = None,
    source: tuple[int, int] | None = None,
    page: int = 0,
    notice: str | None = None,
    track_id: int | None = None,
) -> None:
    playlists = PersonalMusicArchive(database).list_collections(user_id)
    page_count = max(1, (len(playlists) + PLAYLISTS_PER_PAGE - 1) // PLAYLISTS_PER_PAGE)
    page = min(max(page, 0), page_count - 1)
    start = page * PLAYLISTS_PER_PAGE
    selected = playlists[start : start + PLAYLISTS_PER_PAGE]
    keyboard: list[list[dict[str, str]]] = []
    if source:
        source_chat_id, source_message_id = source
        for playlist in selected:
            label = f"{playlist['name']} ({playlist['track_count']})"
            keyboard.append(
                [
                    _button(
                        label[:64],
                        f"add:{playlist['id']}:{source_chat_id}:{source_message_id}",
                    )
                ]
            )
        if page_count > 1:
            navigation: list[dict[str, str]] = []
            if page > 0:
                navigation.append(
                    _button(
                        "⬅️ Previous",
                        f"choose_page:{page - 1}:{source_chat_id}:{source_message_id}",
                    )
                )
            if page + 1 < page_count:
                navigation.append(
                    _button(
                        "Next ➡️",
                        f"choose_page:{page + 1}:{source_chat_id}:{source_message_id}",
                    )
                )
            keyboard.append(navigation)
        keyboard.append(
            [_button("➕ Create playlist", f"new:{source_chat_id}:{source_message_id}")]
        )
        keyboard.append(
            [_button("Cancel", f"cancel_add:{source_chat_id}:{source_message_id}")]
        )
        text = "Choose where to save this Telegram audio:"
    elif track_id is not None:
        for playlist in selected:
            label = f"{playlist['name']} ({playlist['track_count']})"
            keyboard.append(
                [
                    _button(
                        label[:64],
                        f"add_track:{playlist['id']}:{track_id}",
                    )
                ]
            )
        if page_count > 1:
            navigation = []
            if page > 0:
                navigation.append(
                    _button("⬅️ Previous", f"track_choose_page:{page - 1}:{track_id}")
                )
            if page + 1 < page_count:
                navigation.append(
                    _button("Next ➡️", f"track_choose_page:{page + 1}:{track_id}")
                )
            keyboard.append(navigation)
        keyboard.append([_button("➕ Create collection", f"new_track:{track_id}")])
        keyboard.append([_button("Cancel", f"track:{track_id}")])
        text = f"Add #{track_id} to a collection (it stays in your library):"
    else:
        for playlist in selected:
            label = f"🎵 {playlist['name']} ({playlist['track_count']})"
            keyboard.append([_button(label[:64], f"open:{playlist['id']}")])
        if page_count > 1:
            navigation = []
            if page > 0:
                navigation.append(_button("⬅️ Previous", f"playlist_page:{page - 1}"))
            if page + 1 < page_count:
                navigation.append(_button("Next ➡️", f"playlist_page:{page + 1}"))
            keyboard.append(navigation)
        keyboard.append([_button("➕ Create playlist", "new")])
        keyboard.append([_button("⬅️ Main menu", "home")])
        text = (
            f"Your playlists (page {page + 1}/{page_count}):"
            if playlists
            else "You don't have any playlists yet."
        )
    if notice:
        text = f"{notice}\n\n{text}"
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _show_playlist(
    user_id: int,
    chat_id: int,
    playlist_id: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    playlist = PersonalMusicArchive(database).get_collection(user_id, playlist_id)
    if playlist is None:
        _show_playlists(user_id, chat_id, database, api, message_id)
        return
    keyboard: list[list[dict[str, str]]] = [
        [_button("▶️ Play all", f"play:{playlist_id}")],
        [_button("🔀 Shuffle play", f"shuffle:{playlist_id}")],
        [_button("📃 View tracks / remove", f"tracks:{playlist_id}:0")],
        [_button("✏️ Rename playlist", f"rename:{playlist_id}")],
        [_button("🗑 Delete playlist", f"delete:{playlist_id}")],
        [_button("⬅️ Playlists", "playlists")],
    ]
    _edit_or_send(
        api,
        chat_id,
        f"🎵 {playlist['name']}\n{playlist['track_count']} track(s)",
        keyboard,
        message_id,
    )


def _show_tracks(
    user_id: int,
    chat_id: int,
    playlist_id: int,
    page: int,
    database: Database,
    api: TelegramAPI,
    message_id: int,
) -> None:
    archive = PersonalMusicArchive(database)
    playlist = archive.get_collection(user_id, playlist_id)
    tracks = archive.list_collection_tracks(user_id, playlist_id)
    if playlist is None or tracks is None:
        _show_playlists(user_id, chat_id, database, api, message_id)
        return
    page_count = max(1, (len(tracks) + TRACKS_PER_PAGE - 1) // TRACKS_PER_PAGE)
    page = min(max(page, 0), page_count - 1)
    start = page * TRACKS_PER_PAGE
    selected = tracks[start : start + TRACKS_PER_PAGE]
    keyboard: list[list[dict[str, str]]] = []
    for index, track in enumerate(selected, start + 1):
        label = f"▶ #{track['id']} · {index}. {_format_track(track)}"
        keyboard.append(
            [
                _button(label[:60], f"search_play:{track['id']}"),
                _button("✖ Remove", f"remove:{playlist_id}:{track['id']}"),
            ]
        )
    if page_count > 1:
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(_button("⬅️ Previous", f"tracks:{playlist_id}:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(_button("Next ➡️", f"tracks:{playlist_id}:{page + 1}"))
        keyboard.append(navigation)
    keyboard.append([_button("▶️ Play all", f"play:{playlist_id}")])
    keyboard.append([_button("🔀 Shuffle play", f"shuffle:{playlist_id}")])
    keyboard.append([_button("⬅️ Playlist", f"open:{playlist_id}")])
    text = f"📃 {playlist['name']} — {len(tracks)} track(s)"
    if tracks:
        text += "\nChoose a track to play it, or tap Remove to remove it."
    else:
        text += "\nThis playlist is empty."
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _show_search_results(
    user_id: int,
    chat_id: int,
    query: str,
    page: int,
    database: Database,
    api: TelegramAPI,
    message_id: int | None,
) -> None:
    filters: dict[str, int | bool | None] = {
        "rating": None,
        "release_year": None,
        "playable": None,
        "metadata_only": False,
    }
    terms: list[str] = []
    for term in query.split():
        match = re.fullmatch(r"(rating|year|playable|metadata):(.+)", term, re.IGNORECASE)
        if match is None:
            terms.append(term)
            continue
        key, value = match.group(1).lower(), match.group(2).lower()
        try:
            if key == "rating" and value in {"1", "2", "3", "4", "5"}:
                filters["rating"] = int(value)
            elif key == "year" and value.isdigit() and 1000 <= int(value) <= 9999:
                filters["release_year"] = int(value)
            elif key == "playable" and value in {"true", "false"}:
                filters["playable"] = value == "true"
            elif key == "metadata" and value in {"true", "false"}:
                filters["metadata_only"] = value == "true"
            else:
                terms.append(term)
        except ValueError:
            terms.append(term)
    search_text = " ".join(terms)
    offset = page * SEARCH_RESULTS_PER_PAGE
    if filters["metadata_only"]:
        results = database.library_repository.search_metadata_only_tracks(
            user_id,
            search_text,
            SEARCH_RESULTS_PER_PAGE + 1,
            offset,
            filters["release_year"],
        )
    else:
        results = database.library_repository.search_tracks(
            user_id,
            search_text,
            SEARCH_RESULTS_PER_PAGE + 1,
            offset,
            rating=filters["rating"],
            release_year=filters["release_year"],
            playable=filters["playable"],
        )
    has_next = len(results) > SEARCH_RESULTS_PER_PAGE
    results = results[:SEARCH_RESULTS_PER_PAGE]
    keyboard: list[list[dict[str, str]]] = []
    for track in results:
        if filters["metadata_only"]:
            label = f"ℹ #{track['id']} · {_format_track(track)} · metadata only"
            callback = f"metadata_info:{track['id']}"
        else:
            label = f"▶ #{track['id']} · {_format_track(track)} · {track['playlist_name']}"
            callback = f"search_play:{track['id']}"
        keyboard.append([_button(label[:60], callback)])
    if page > 0 or has_next:
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(_button("⬅️ Previous", f"search_page:{page - 1}"))
        if has_next:
            navigation.append(_button("Next ➡️", f"search_page:{page + 1}"))
        keyboard.append(navigation)
    keyboard.append([_button("🔎 Search again", "search")])
    keyboard.append([_button("⬅️ Main menu", "home")])
    if not results and page == 0:
        text = f'No tracks found for “{query}”. Try another search.'
    else:
        text = f'Search results for “{query}” (page {page + 1}):'
    _edit_or_send(api, chat_id, text, keyboard, message_id)


def _save_track(
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
    database: Database,
    api: TelegramAPI,
    archive_chat_id: int | None = None,
) -> tuple[int, bool, str | None] | None:
    archive = PersonalMusicArchive(database)
    pending = database.get_pending_track(user_id, source_chat_id, source_message_id)
    if pending is None:
        return None
    track_id, saved, _ = archive.save_audio(
        user_id,
        TrackMetadata(
            title=pending["title"],
            performer=pending["performer"],
            release_title=pending["release_title"],
            duration_seconds=pending["duration_seconds"],
            mime_type=pending["mime_type"],
            original_filename=pending["original_filename"],
            musicbrainz_recording_id=pending["musicbrainz_recording_id"],
            release_year=pending["release_year"],
        ),
        TelegramMessageReference(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            media_type=pending["media_type"],
            telegram_file_id=pending["telegram_file_id"],
            telegram_file_unique_id=pending["file_unique_id"],
            original_filename=pending["original_filename"],
        ),
    )
    database.delete_pending_track(user_id, source_chat_id, source_message_id)
    archive_notice = None
    if archive_chat_id is not None:
        archived = database.find_shared_archive_reference(
            archive_chat_id,
            pending["file_unique_id"],
            pending["musicbrainz_recording_id"],
        )
        if archived is None:
            try:
                result = api.call(
                    "copyMessage",
                    chat_id=archive_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                archive_message_id = (
                    result.get("message_id") if isinstance(result, dict) else None
                )
                if (
                    not isinstance(archive_message_id, int)
                    or isinstance(archive_message_id, bool)
                    or archive_message_id < 1
                ):
                    raise TelegramAPIError(
                        "Telegram did not return a valid archived message ID."
                    )
            except TelegramAPIError as error:
                LOGGER.warning(
                    "Could not copy track %s to the shared archive: %s",
                    track_id,
                    error,
                )
                archive_notice = (
                    "The track was added, but could not be copied to the shared "
                    "archive. Its original message remains available."
                )
            else:
                asset_saved = database.save_shared_archive_asset(
                    archive_chat_id,
                    archive_message_id,
                    pending["file_unique_id"],
                    pending["media_type"],
                    telegram_file_id=pending["telegram_file_id"],
                    original_filename=pending["original_filename"],
                    title=pending["title"],
                    performer=pending["performer"],
                    release_title=pending["release_title"],
                    duration_seconds=pending["duration_seconds"],
                    musicbrainz_recording_id=pending["musicbrainz_recording_id"],
                    release_year=pending["release_year"],
                )
                if not asset_saved:
                    LOGGER.warning(
                        "Archived track %s was copied but not registered",
                        track_id,
                    )
                    archive_notice = (
                        "The archive copy is available, but its reference could "
                        "not be recorded."
                    )
                elif not pending["file_unique_id"] and not pending[
                    "musicbrainz_recording_id"
                ]:
                    LOGGER.warning(
                        "Archived track %s has no stable duplicate identifier",
                        track_id,
                    )
                    archive_notice = (
                        "The archive copy is available, but no stable identifier "
                        "was found for future duplicate detection."
                    )
                database.library_repository.add_message_reference(
                    user_id,
                    track_id,
                    archive_chat_id,
                    archive_message_id,
                    pending["media_type"],
                    file_unique_id=pending["file_unique_id"],
                    original_filename=pending["original_filename"],
                )
        else:
            database.library_repository.add_message_reference(
                user_id,
                track_id,
                int(archived["archive_chat_id"]),
                int(archived["archive_message_id"]),
                archived["media_type"],
                file_unique_id=archived["telegram_file_unique_id"],
                original_filename=archived["original_filename"],
            )
    return track_id, saved, archive_notice


def _deliver_track(
    user_id: int,
    chat_id: int,
    track: dict[str, Any],
    database: Database,
    api: TelegramAPI,
    archive_chat_id: int | None = None,
) -> bool:
    reference = PersonalMusicArchive(database).get_playable_reference(
        user_id,
        track["id"],
        preferred_chat_id=archive_chat_id,
    )
    if reference is None:
        api.send_message(
            chat_id,
            f"No playable Telegram message is available for {_format_track(track)}. "
            "Send another copy of the audio, then use /link to associate it.",
        )
        return False
    delivery = TelegramDeliveryService(
        api,
        PersonalMusicArchive(database).get_delivery_mode(user_id),
    )
    try:
        delivery.deliver(
            chat_id, reference["source_chat_id"], reference["source_message_id"]
        )
    except TelegramAPIError as error:
        if error.retry_after is not None:
            time.sleep(error.retry_after)
            try:
                delivery.deliver(
                    chat_id, reference["source_chat_id"], reference["source_message_id"]
                )
            except TelegramAPIError as retry_error:
                LOGGER.error(
                    "Telegram delivery retry failed for track %s: %s",
                    track["id"],
                    retry_error,
                )
                api.send_message(
                    chat_id,
                    "Telegram is rate limiting delivery. Please retry this track later.",
                )
                return False
            database.library_repository.mark_message_reference_available(
                user_id, reference["id"]
            )
            return True
        LOGGER.error("Telegram could not deliver track %s: %s", track["id"], error)
        if error.error_code in (400, 403):
            database.library_repository.mark_message_reference_unavailable(
                user_id, reference["id"]
            )
            api.send_message(
                chat_id,
                f"Telegram could not retrieve {_format_track(track)} from that source message. "
                "The reference is marked unavailable; send another copy and associate it with "
                "this track using /link.",
            )
        else:
            api.send_message(
                chat_id,
                f"Could not retrieve {_format_track(track)} right now. Please try again later.",
            )
        return False
    database.library_repository.mark_message_reference_available(
        user_id, reference["id"]
    )
    return True


def handle_message(
    message: dict[str, Any],
    database: Database,
    api: TelegramAPI,
    metadata_provider: MusicBrainzProvider | None = None,
    archive_chat_id: int | None = None,
    metadata_search_provider: MetadataSearchProvider | None = None,
) -> None:
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    user_id = message.get("from", {}).get("id")
    if chat_id is None or user_id is None:
        return

    text = message.get("text", "").strip()
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower() if text.startswith("/") else ""
    if command == "/start":
        _clear_interaction_state(database, user_id)
        _show_home(api, chat_id)
        return
    if command == "/help":
        _show_home(
            api,
            chat_id,
            text=(
                "Use the buttons below to add music, browse your library, search, manage "
                "collections, discover related tracks, view statistics, and set delivery "
                "preferences. Audio remains in Telegram; only references and metadata are "
                "stored. Track detail actions are available from the library."
            ),
        )
        return
    command_args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
    if text == "➕ Add Music":
        api.send_message(
            chat_id,
            "Send a Telegram audio message, voice message, or audio document to add it. "
            "The file stays in Telegram; this bot only saves a message reference.",
        )
        return
    if text == "📚 My Library":
        _show_library(user_id, chat_id, database, api, None)
        return
    if text == "🔎 Search":
        database.clear_user_state(user_id)
        database.set_user_state(user_id, "search")
        _edit_or_send(
            api,
            chat_id,
            "Send a title, artist, album, tag, collection, rating (rating:5), "
            "year (year:2024), or playable:true/false filter.",
            [[_button("Cancel", "home")]],
        )
        return
    if text == "🗂 Collections":
        _show_playlists(user_id, chat_id, database, api)
        return
    if text == "✨ Discover":
        _show_discovery(user_id, chat_id, database, api, None)
        return
    if text == "📊 Statistics":
        _show_statistics(user_id, chat_id, database, api, None)
        return
    if text == "⚙️ Settings":
        _show_settings(user_id, chat_id, database, api, None)
        return
    if text == "⬇️ Downloads":
        _show_downloads(user_id, chat_id, database, api, None)
        return
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            job_id = database.download_repository.enqueue(user_id, text)
            database.clear_user_state(user_id)
            api.send_message(
                chat_id,
                f"Download queued as job #{job_id}."
                if job_id is not None
                else "This URL already has an active download job.",
            )
            return
    if command == "/download":
        source_url = command_args.strip()
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            api.send_message(chat_id, "Usage: /download <http-or-https-url>")
            return
        job_id = database.download_repository.enqueue(user_id, source_url)
        api.send_message(
            chat_id,
            f"Download queued as job #{job_id}."
            if job_id is not None
            else "This URL already has an active download job.",
        )
        return
    if command == "/downloads":
        _show_downloads(user_id, chat_id, database, api, None)
        return
    if command in {"/cancel_download", "/retry_download"}:
        try:
            job_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, f"Usage: {command} <job-id>")
            return
        changed = (
            database.download_repository.cancel(user_id, job_id)
            if command == "/cancel_download"
            else database.download_repository.retry(user_id, job_id)
        )
        api.send_message(
            chat_id,
            "Job cancelled." if command == "/cancel_download" and changed
            else "Job queued for retry." if changed
            else "No matching job could be updated.",
        )
        return
    if command == "/link":
        try:
            track_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, "Usage: /link <your-track-id>")
            return
        if database.library_repository.get_track(user_id, track_id) is None:
            api.send_message(chat_id, "That track is not in your personal library.")
            return
        database.set_user_state(user_id, f"link:{track_id}")
        api.send_message(
            chat_id,
            "Now send or forward another Telegram message containing this audio. "
            "The new message reference will be associated with your track.",
        )
        return
    if command == "/edit":
        pieces = command_args.split(maxsplit=1)
        if len(pieces) != 2:
            api.send_message(chat_id, "Usage: /edit <your-track-id> <title> | <artist>")
            return
        try:
            track_id = int(pieces[0])
        except ValueError:
            api.send_message(chat_id, "Track ID must be a number.")
            return
        title_and_artist = pieces[1].split("|", maxsplit=1)
        title = title_and_artist[0].strip()
        performer = title_and_artist[1].strip() if len(title_and_artist) == 2 else None
        try:
            changed = PersonalMusicArchive(database).update_metadata(
                user_id,
                track_id,
                title,
                performer,
            )
        except ValueError as error:
            api.send_message(chat_id, str(error))
            return
        api.send_message(
            chat_id,
            "Track metadata updated."
            if changed
            else "That track is not in your personal library.",
        )
        return
    if command == "/rate":
        pieces = command_args.split()
        if len(pieces) != 2:
            api.send_message(chat_id, "Usage: /rate <your-track-id> <1-5|clear>")
            return
        try:
            track_id = int(pieces[0])
            rating = None if pieces[1].lower() == "clear" else int(pieces[1])
            updated = PersonalMusicArchive(database).set_rating(
                user_id,
                track_id,
                rating,
            )
        except ValueError as error:
            api.send_message(chat_id, f"Invalid rating: {error}")
            return
        api.send_message(
            chat_id,
            "Rating updated." if updated else "That track is not in your personal library.",
        )
        return
    if command == "/track":
        try:
            track_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, "Usage: /track <your-track-id>")
            return
        track = database.library_repository.get_track(user_id, track_id)
        if track is None:
            api.send_message(chat_id, "That track is not in your personal library.")
            return
        _show_track_detail(user_id, chat_id, track_id, database, api, None)
        return
    if command == "/play":
        try:
            track_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, "Usage: /play <your-track-id>")
            return
        track = database.library_repository.get_track(user_id, track_id)
        if track is None:
            api.send_message(chat_id, "That track is not in your personal library.")
            return
        _deliver_track(user_id, chat_id, track, database, api, archive_chat_id)
        return
    if command == "/note":
        pieces = command_args.split(maxsplit=1)
        if len(pieces) != 2:
            api.send_message(chat_id, "Usage: /note <your-track-id> <note|clear>")
            return
        try:
            track_id = int(pieces[0])
        except ValueError:
            api.send_message(chat_id, "Track ID must be a number.")
            return
        note = None if pieces[1].strip().lower() == "clear" else pieces[1].strip()
        if len(note or "") > 1000:
            api.send_message(chat_id, "Notes are limited to 1000 characters.")
            return
        updated = PersonalMusicArchive(database).set_note(user_id, track_id, note)
        api.send_message(
            chat_id,
            "Note updated." if updated else "That track is not in your personal library.",
        )
        return
    if command in ("/tag", "/untag"):
        pieces = command_args.split(maxsplit=1)
        if len(pieces) != 2:
            api.send_message(chat_id, f"Usage: {command} <your-track-id> <tag>")
            return
        try:
            track_id = int(pieces[0])
            changed = (
                PersonalMusicArchive(database).add_tag(user_id, track_id, pieces[1])
                if command == "/tag"
                else PersonalMusicArchive(database).remove_tag(
                    user_id,
                    track_id,
                    pieces[1],
                )
            )
        except ValueError as error:
            api.send_message(chat_id, str(error))
            return
        api.send_message(
            chat_id,
            "Tag saved." if changed else "Track is not in your library or tag was unchanged.",
        )
        return
    if command == "/metadata":
        rows = database.library_repository.list_metadata_only_tracks(user_id)
        if not rows:
            api.send_message(chat_id, "No metadata-only tracks saved yet.")
        else:
            entries = [
                f"{row['id']}. {_format_track(row)} — metadata only (not playable)"
                for row in rows[:20]
            ]
            api.send_message(chat_id, "Metadata-only tracks:\n" + "\n".join(entries))
        return
    if command == "/library":
        _show_library(user_id, chat_id, database, api, None)
        return
    if command == "/search":
        database.set_user_state(user_id, "search")
        api.send_message(
            chat_id,
            "Send title, artist, album, tag, collection name, or filters like "
            "rating:5 year:2024 playable:true metadata:true.",
            _markup([[_button("Cancel", "home")]]),
        )
        return
    if command == "/stats":
        _show_statistics(user_id, chat_id, database, api, None)
        return
    if command == "/settings":
        _show_settings(user_id, chat_id, database, api, None)
        return
    if command == "/discover":
        _show_discovery(user_id, chat_id, database, api, None)
        return
    if command == "/deletemetadata":
        try:
            track_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, "Usage: /deletemetadata <your-metadata-track-id>")
            return
        deleted = database.library_repository.delete_metadata_only_track(
            user_id, track_id
        )
        api.send_message(
            chat_id,
            "Metadata-only track removed."
            if deleted
            else "That metadata-only track is not in your personal library.",
        )
        return
    if command == "/collections":
        collections = PersonalMusicArchive(database).list_collections(user_id)
        if not collections:
            api.send_message(chat_id, "You do not have any collections yet.")
        else:
            entries = [
                f"{collection['id']}. {collection['name']} "
                f"({collection['track_count']} tracks)"
                for collection in collections[:50]
            ]
            api.send_message(chat_id, "Your collections:\n" + "\n".join(entries))
        return
    if command == "/addtrack":
        pieces = command_args.split()
        if len(pieces) != 2:
            api.send_message(chat_id, "Usage: /addtrack <your-track-id> <collection-id>")
            return
        try:
            track_id, collection_id = map(int, pieces)
            added = PersonalMusicArchive(database).add_to_collection(
                user_id, track_id, collection_id
            )
        except ValueError:
            api.send_message(chat_id, "Track and collection IDs must be numbers.")
            return
        api.send_message(
            chat_id,
            "Track added to the collection."
            if added
            else "The track or collection is unavailable, or the track is already in it.",
        )
        return

    details = _audio_details(message)
    state = database.get_user_state(user_id)
    if details is not None and state is not None and state.startswith("link:"):
        try:
            track_id = int(state.split(":", maxsplit=1)[1])
        except ValueError:
            database.clear_user_state(user_id)
            api.send_message(chat_id, "That track association expired. Use /link again.")
            return
        if database.library_repository.get_track(user_id, track_id) is None:
            database.clear_user_state(user_id)
            api.send_message(chat_id, "That track is not in your library.")
            return
        (
            _title,
            _performer,
            media_type,
            file_unique_id,
            telegram_file_id,
            _duration,
            _mime_type,
            original_filename,
        ) = details
        added = database.library_repository.add_message_reference(
            user_id,
            track_id,
            chat_id,
            message["message_id"],
            media_type,
            telegram_file_id,
            file_unique_id,
            original_filename,
        )
        database.clear_user_state(user_id)
        api.send_message(
            chat_id,
            "Added this Telegram message as another source reference."
            if added
            else "That Telegram message is already associated with this track.",
        )
        return

    if details is not None:
        _clear_interaction_state(database, user_id)
        source_message_id = message["message_id"]
        (
            title,
            performer,
            media_type,
            file_unique_id,
            telegram_file_id,
            duration_seconds,
            mime_type,
            original_filename,
        ) = details
        release_title = None
        recording_id = None
        release_year = None
        metadata_notice = None
        if metadata_provider is not None and title not in ("Untitled audio", "Voice message"):
            try:
                match = metadata_provider.lookup_recording(title, performer)
            except MusicBrainzError as error:
                LOGGER.warning("MusicBrainz lookup failed for %r: %s", title, error)
                metadata_notice = (
                    "MusicBrainz lookup failed; the audio's original metadata will be kept."
                )
            else:
                if match is not None:
                    title, performer = match.title, match.artist or performer
                    release_title = match.release_title
                    recording_id = match.recording_id
                    release_year = match.release_year
                    metadata_notice = f"MusicBrainz match: {title}"
                    if performer:
                        metadata_notice += f" — {performer}"
                    if release_title:
                        metadata_notice += f" ({release_title})"
                else:
                    metadata_notice = (
                        "No confident MusicBrainz match; the audio's original metadata will be kept."
                    )
        database.save_pending_track(
            user_id,
            chat_id,
            source_message_id,
            title,
            performer,
            media_type,
            file_unique_id,
            release_title,
            recording_id,
            telegram_file_id,
            duration_seconds,
            mime_type,
            original_filename,
            release_year,
        )
        save_result = _save_track(
            user_id,
            chat_id,
            source_message_id,
            database,
            api,
            archive_chat_id,
        )
        if save_result is None:
            api.send_message(chat_id, "The audio could not be saved. Please send it again.")
            return
        track_id, saved, archive_notice = save_result
        status = (
            f"✅ Saved to your library as #{track_id}."
            if saved
            else f"This audio is already in your library as #{track_id}."
        )
        for notice in (metadata_notice, archive_notice):
            if notice:
                status += f"\n{notice}"
        if archive_chat_id is not None and archive_notice is None:
            status += "\nAvailable from the shared archive."
        _edit_or_send(
            api,
            chat_id,
            status,
            [
                [_button("➕ Add to a collection", f"library_add:{track_id}")],
                [_button("📚 My Library", "library")],
                [_button("⬅️ Main menu", "home")],
            ],
        )
        return

    if not text or text.startswith("/"):
        return
    if state is not None and state.startswith("create_track:"):
        try:
            track_id = int(state.split(":", maxsplit=1)[1])
        except ValueError:
            database.clear_user_state(user_id)
            api.send_message(chat_id, "That collection action expired.")
            return
        playlist_name = text.strip()
        if not playlist_name or len(playlist_name) > 64:
            api.send_message(chat_id, "Collection names must contain 1–64 characters. Try again.")
            return
        archive = PersonalMusicArchive(database)
        if not archive.create_collection(user_id, playlist_name):
            api.send_message(chat_id, f"“{playlist_name}” already exists.")
            return
        playlist = next(
            row
            for row in archive.list_collections(user_id)
            if row["name"] == playlist_name
        )
        added = archive.add_to_collection(user_id, track_id, int(playlist["id"]))
        database.clear_user_state(user_id)
        api.send_message(
            chat_id,
            f"Added #{track_id} to “{playlist_name}”."
            if added
            else f"Created “{playlist_name}”, but the track could not be added.",
        )
        return
    if state is not None and state.startswith("create"):
        playlist_name = text.strip()
        if not playlist_name or len(playlist_name) > 64:
            api.send_message(chat_id, "Playlist names must contain 1–64 characters. Try again.")
            return
        if not PersonalMusicArchive(database).create_collection(user_id, playlist_name):
            database.clear_user_state(user_id)
            if state.count(":") == 2:
                _, source_chat_id, source_message_id = state.split(":")
                api.send_message(
                    chat_id,
                    f"“{playlist_name}” already exists. Choose a playlist for this audio:",
                )
                _show_playlists(
                    user_id,
                    chat_id,
                    database,
                    api,
                    source=(int(source_chat_id), int(source_message_id)),
                )
                return
            api.send_message(
                chat_id,
                f"“{playlist_name}” already exists.",
                reply_markup=_markup(_home_keyboard()),
            )
            return
        database.clear_user_state(user_id)
        if state.count(":") == 2:
            _, source_chat_id, source_message_id = state.split(":")
            _show_playlists(
                user_id,
                chat_id,
                database,
                api,
                source=(int(source_chat_id), int(source_message_id)),
            )
        else:
            _show_playlists(user_id, chat_id, database, api)
        return
    if state is not None and ":" in state:
        action, _, raw_track_id = state.partition(":")
        if action in {"rate_track", "note_track", "tag_track", "edit_track"}:
            try:
                track_id = int(raw_track_id)
            except ValueError:
                database.clear_user_state(user_id)
                api.send_message(chat_id, "That track action expired. Please open the track again.")
                return
            if database.library_repository.get_track(user_id, track_id) is None:
                database.clear_user_state(user_id)
                api.send_message(chat_id, "That track is not in your personal library.")
                return
            try:
                if action == "rate_track":
                    value = None if text.lower() == "clear" else int(text)
                    changed = PersonalMusicArchive(database).set_rating(
                        user_id,
                        track_id,
                        value,
                    )
                elif action == "note_track":
                    if len(text) > 500:
                        raise ValueError("Notes must be at most 500 characters.")
                    changed = PersonalMusicArchive(database).set_note(
                        user_id,
                        track_id,
                        None if text.lower() == "clear" else text,
                    )
                elif action == "tag_track":
                    changed = PersonalMusicArchive(database).add_tag(
                        user_id,
                        track_id,
                        text,
                    )
                else:
                    pieces = text.split("|", maxsplit=1)
                    changed = PersonalMusicArchive(database).update_metadata(
                        user_id,
                        track_id,
                        pieces[0].strip(),
                        pieces[1].strip() if len(pieces) == 2 else None,
                    )
            except ValueError as error:
                api.send_message(chat_id, str(error))
                return
            database.clear_user_state(user_id)
            if changed:
                _show_track_detail(user_id, chat_id, track_id, database, api, None)
            else:
                api.send_message(chat_id, "That track could not be updated.")
            return
    if state == "download_url":
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            api.send_message(chat_id, "Send a valid HTTP(S) URL.")
            return
        job_id = database.download_repository.enqueue(user_id, text)
        database.clear_user_state(user_id)
        api.send_message(
            chat_id,
            f"Download queued as job #{job_id}."
            if job_id is not None
            else "This URL already has an active download job.",
            _markup([[_button("⬇️ Downloads", "downloads")]]),
        )
        return
    if state is not None and state.startswith("link:"):
        api.send_message(chat_id, "Send an audio or audio document to associate it.")
        return
    if state == "search":
        query = text.strip()
        if not query or len(query) > 100:
            api.send_message(chat_id, "Search text must contain 1–100 characters. Try again.")
            return
        if metadata_search_provider is not None:
            try:
                matches = metadata_search_provider.search(query, limit=5)
            except MetadataProviderError as error:
                LOGGER.warning("YouTube Music search failed for %r: %s", query, error)
                api.send_message(chat_id, f"Music search failed: {error}")
                return
            if matches:
                candidates = [match.as_dict() for match in matches[:5]]
                database.set_user_state(
                    user_id,
                    f"download_search_results:{json.dumps(candidates, ensure_ascii=True)}",
                )
                keyboard = [
                    [
                        _button(
                            f"{match.title} — {match.artist or 'Unknown artist'}"[:60],
                            f"download_search:{index}",
                        )
                    ]
                    for index, match in enumerate(matches[:5])
                ]
                keyboard.append([_button("Search library instead", "search_library")])
                keyboard.append([_button("Cancel", "home")])
                api.send_message(
                    chat_id,
                    "نتیجه‌ها را انتخاب کنید تا دانلود و ارسال فایل آغاز شود:",
                    _markup(keyboard),
                )
                return
        database.set_user_state(user_id, f"search_results:{query}")
        _show_search_results(user_id, chat_id, query, 0, database, api, None)
        return
    if state is not None and state.startswith("rename:"):
        playlist_name = text.strip()
        try:
            playlist_id = int(state.split(":", maxsplit=1)[1])
        except ValueError:
            database.clear_user_state(user_id)
            _show_home(api, chat_id, text="That rename action expired. Choose a menu option.")
            return
        if not playlist_name or len(playlist_name) > 64:
            api.send_message(chat_id, "Playlist names must contain 1–64 characters. Try again.")
            return
        archive = PersonalMusicArchive(database)
        playlist = archive.get_collection(user_id, playlist_id)
        if playlist is None:
            database.clear_user_state(user_id)
            api.send_message(chat_id, "That playlist no longer exists.")
            _show_playlists(user_id, chat_id, database, api)
            return
        if not archive.rename_collection(user_id, playlist_id, playlist_name):
            database.clear_user_state(user_id)
            api.send_message(
                chat_id,
                f"“{playlist_name}” is already in use. The playlist name was not changed.",
            )
            _show_playlist(user_id, chat_id, playlist_id, database, api, None)
            return
        database.clear_user_state(user_id)
        api.send_message(chat_id, f"Renamed playlist to “{playlist_name}”.")
        _show_playlist(user_id, chat_id, playlist_id, database, api, None)
        return

    if state is not None and state.startswith("metadata_results:"):
        api.send_message(chat_id, "Choose a metadata match from the results above.")
        return

    if metadata_search_provider is not None and state is None:
        try:
            matches = metadata_search_provider.search(text, limit=5)
        except MetadataProviderError as error:
            LOGGER.warning("YouTube Music search failed for %r: %s", text, error)
            api.send_message(
                chat_id,
                f"Music metadata search failed: {error}. No track was added.",
            )
            return
        if matches:
            candidates = [match.as_dict() for match in matches[:5]]
            database.set_user_state(
                user_id, f"metadata_results:{json.dumps(candidates, ensure_ascii=True)}"
            )
            keyboard = [
                [
                    _button(
                        f"{match.title} — {match.artist or 'Unknown artist'} "
                        f"[{match.provider}]"[:60],
                        f"metadata:{index}",
                    )
                ]
                for index, match in enumerate(matches[:5])
            ]
            keyboard.append([_button("Cancel", "home")])
            api.send_message(
                chat_id,
                "Choose a metadata match from YouTube Music. "
                "This saves metadata only; it does not create a playable Telegram reference.",
                _markup(keyboard),
            )
            return
        api.send_message(
            chat_id,
            "No YouTube Music metadata match was found. You can still send a Telegram audio "
            "message to save a playable reference.",
        )
        return
    if state is None:
        prompt = "Send audio to save a playable Telegram reference."
        if metadata_search_provider is not None:
            prompt += " Search for a track to save YouTube Music metadata only."
        api.send_message(chat_id, prompt)
        return

    _show_home(api, chat_id, text="Use the menu buttons below to manage your playlists.")


def handle_callback(
    callback: dict[str, Any],
    database: Database,
    api: TelegramAPI,
    archive_chat_id: int | None = None,
) -> None:
    callback_id = callback.get("id")
    user_id = callback.get("from", {}).get("id")
    message = callback.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    data = callback.get("data", "")
    if callback_id is None:
        return
    api.call("answerCallbackQuery", callback_query_id=callback_id)
    if user_id is None or chat_id is None or message_id is None or chat.get("type") != "private":
        return

    parts = data.split(":")
    action = parts[0]
    if action == "home":
        _clear_interaction_state(database, user_id)
        _show_home(api, chat_id, message_id)
    elif action == "add_music":
        _edit_or_send(
            api,
            chat_id,
            "Send a Telegram audio message, voice message, or audio document to add it. "
            "The bot stores a reference and metadata, never the audio file.",
            [[_button("⬅️ Main menu", "home")]],
            message_id,
        )
    elif action == "library":
        _show_library(user_id, chat_id, database, api, message_id)
    elif action == "library_page" and len(parts) == 2:
        _show_library(user_id, chat_id, database, api, message_id, int(parts[1]))
    elif action == "library_add" and len(parts) == 2:
        track_id = int(parts[1])
        if database.library_repository.get_track(user_id, track_id) is None:
            _show_library(user_id, chat_id, database, api, message_id)
        else:
            _show_playlists(
                user_id,
                chat_id,
                database,
                api,
                message_id,
                track_id=track_id,
            )
    elif action == "track_choose_page" and len(parts) == 3:
        _show_playlists(
            user_id,
            chat_id,
            database,
            api,
            message_id,
            page=int(parts[1]),
            track_id=int(parts[2]),
        )
    elif action == "add_track" and len(parts) == 3:
        playlist_id, track_id = map(int, parts[1:])
        if PersonalMusicArchive(database).add_to_collection(
            user_id, track_id, playlist_id
        ):
            _show_playlist(user_id, chat_id, playlist_id, database, api, message_id)
        else:
            _show_playlists(
                user_id,
                chat_id,
                database,
                api,
                message_id,
                track_id=track_id,
                notice="The track could not be added to that collection.",
            )
    elif action == "discover":
        _show_discovery(user_id, chat_id, database, api, message_id)
    elif action == "stats":
        _show_statistics(user_id, chat_id, database, api, message_id)
    elif action == "settings":
        _show_settings(user_id, chat_id, database, api, message_id)
    elif action == "downloads":
        _show_downloads(user_id, chat_id, database, api, message_id)
    elif action == "download_add":
        database.set_user_state(user_id, "download_url")
        _edit_or_send(
            api,
            chat_id,
            "Send an HTTP(S) URL to queue for download.",
            [[_button("⬅️ Downloads", "downloads")]],
            message_id,
        )
    elif action == "download_cancel" and len(parts) == 2:
        database.download_repository.cancel(user_id, int(parts[1]))
        _show_downloads(user_id, chat_id, database, api, message_id)
    elif action == "download_retry" and len(parts) == 2:
        database.download_repository.retry(user_id, int(parts[1]))
        _show_downloads(user_id, chat_id, database, api, message_id)
    elif action == "delivery" and len(parts) == 2 and parts[1] in {"copy", "forward"}:
        PersonalMusicArchive(database).set_delivery_mode(user_id, parts[1])
        _show_settings(user_id, chat_id, database, api, message_id)
    elif action == "track" and len(parts) == 2:
        _show_track_detail(
            user_id, chat_id, int(parts[1]), database, api, message_id
        )
    elif action in {"rate_track", "note_track", "tag_track", "edit_track", "link_track"} and len(parts) == 2:
        track_id = int(parts[1])
        if database.library_repository.get_track(user_id, track_id) is None:
            _show_library(user_id, chat_id, database, api, message_id)
            return
        if action == "link_track":
            database.set_user_state(user_id, f"link:{track_id}")
            prompt = "Send another Telegram audio message to link it to this track."
        else:
            database.set_user_state(user_id, f"{action}:{track_id}")
            prompt = {
                "rate_track": "Send a rating from 1 to 5, or send clear to remove it.",
                "note_track": "Send a personal note (up to 500 characters), or clear to remove it.",
                "tag_track": "Send a personal tag to add to this track (1–32 characters).",
                "edit_track": "Send corrected metadata as title | artist. You can omit the artist.",
            }[action]
        _edit_or_send(
            api,
            chat_id,
            prompt,
            [[_button("Cancel", f"track:{track_id}")]],
            message_id,
        )
    elif action == "metadata_info" and len(parts) == 2:
        track_id = int(parts[1])
        metadata_tracks = database.library_repository.list_metadata_only_tracks(
            user_id
        )
        track = next((row for row in metadata_tracks if row["id"] == track_id), None)
        if track is None:
            _show_home(api, chat_id, message_id, "That metadata-only track is unavailable.")
        else:
            _edit_or_send(
                api,
                chat_id,
                f"ℹ #{track['id']} {_format_track(track)}\n"
                f"Release year: {track['release_year'] or 'unknown'}\n"
                "Metadata only: no playable Telegram message is saved.",
                [[_button("⬅️ Main menu", "home")]],
                message_id,
            )
    elif action == "metadata" and len(parts) == 2:
        state = database.get_user_state(user_id)
        if state is None or not state.startswith("metadata_results:"):
            _show_home(api, chat_id, message_id, "Those metadata results have expired.")
        else:
            try:
                candidates = json.loads(state.split(":", maxsplit=1)[1])
                selected = candidates[int(parts[1])]
                if not isinstance(selected, dict):
                    raise ValueError("Invalid selection")
                provider = selected.get("provider")
                identifier = selected.get("identifier")
                if provider is None and isinstance(selected.get("recording_id"), str):
                    provider = "musicbrainz"
                    identifier = selected["recording_id"]
                title = selected["title"]
                artist = selected.get("artist")
                release_title = selected.get("release_title")
                provider_url = selected.get("provider_url")
                if provider_url is None and provider == "musicbrainz":
                    provider_url = (
                        f"https://musicbrainz.org/recording/{identifier}"
                    )
                if (
                    provider not in {"musicbrainz", "youtube_music"}
                    or not isinstance(identifier, str)
                    or not isinstance(title, str)
                    or not isinstance(provider_url, str)
                ):
                    raise ValueError("Invalid result")
                release_year = selected.get("release_year")
                if release_year is not None and (
                    not isinstance(release_year, int)
                    or isinstance(release_year, bool)
                ):
                    raise ValueError("Invalid release year")
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                database.clear_user_state(user_id)
                _show_home(api, chat_id, message_id, "That metadata choice is no longer available.")
            else:
                PersonalMusicArchive(database).save_metadata_only(
                    user_id,
                    TrackMetadata(
                        title=title,
                        performer=artist if isinstance(artist, str) else None,
                        release_title=release_title if isinstance(release_title, str) else None,
                        musicbrainz_recording_id=(
                            selected.get("musicbrainz_recording_id")
                            if isinstance(
                                selected.get("musicbrainz_recording_id"), str
                            )
                            else identifier
                            if provider == "musicbrainz"
                            else None
                        ),
                        release_year=release_year,
                    ),
                    provider_url,
                    source_provider=provider,
                )
                database.clear_user_state(user_id)
                _edit_or_send(
                    api,
                    chat_id,
                    f"Saved {title}"
                    + (f" — {artist}" if isinstance(artist, str) else "")
                    + " as metadata only. No playable Telegram copy is available; send the "
                    "audio separately to add a playable track.",
                    [[_button("Main menu", "home")]],
                    message_id,
                )
    elif action == "playlists":
        _clear_interaction_state(database, user_id)
        _show_playlists(user_id, chat_id, database, api, message_id)
    elif action == "search":
        _clear_interaction_state(database, user_id)
        database.set_user_state(user_id, "search")
        _edit_or_send(
            api,
            chat_id,
            "نام آهنگ یا آرتیست را بنویسید تا نتایج جستجو نمایش داده شود.",
            [[_button("Cancel", "home")]],
            message_id,
        )
    elif action == "search_library":
        state = database.get_user_state(user_id)
        query = ""
        if state and state.startswith("download_search_results:"):
            previous = state.split(":", maxsplit=1)[1]
            if previous:
                # The external search has no original query in its state. Ask for a
                # library query rather than reusing provider JSON.
                query = ""
        database.set_user_state(user_id, "search")
        _edit_or_send(
            api,
            chat_id,
            "برای جستجو در کتابخانه خود، نام آهنگ، آرتیست یا پلی‌لیست را بنویسید.",
            [[_button("Cancel", "home")]],
            message_id,
        )
    elif action == "download_search" and len(parts) == 2:
        state = database.get_user_state(user_id)
        if state is None or not state.startswith("download_search_results:"):
            _show_home(api, chat_id, message_id, "نتیجه‌های جستجو منقضی شده‌اند.")
        else:
            try:
                candidates = json.loads(state.split(":", maxsplit=1)[1])
                selected = candidates[int(parts[1])]
                if not isinstance(selected, dict):
                    raise ValueError("Invalid result")
                title = selected["title"]
                artist = selected.get("artist")
                provider_url = selected["provider_url"]
                if not isinstance(title, str) or not isinstance(provider_url, str):
                    raise ValueError("Invalid result")
                release_title = selected.get("release_title")
                release_year = selected.get("release_year")
                if release_year is not None and (
                    not isinstance(release_year, int)
                    or isinstance(release_year, bool)
                ):
                    raise ValueError("Invalid release year")
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                database.clear_user_state(user_id)
                _show_home(api, chat_id, message_id, "گزینه انتخابی معتبر نیست.")
            else:
                track_id = PersonalMusicArchive(database).save_metadata_only(
                    user_id,
                    TrackMetadata(
                        title=title,
                        performer=artist if isinstance(artist, str) else None,
                        release_title=(
                            release_title if isinstance(release_title, str) else None
                        ),
                        release_year=release_year,
                    ),
                    provider_url,
                    source_provider="youtube_music",
                )
                job_id = database.download_repository.enqueue(
                    user_id,
                    provider_url,
                    track_id=track_id,
                )
                database.clear_user_state(user_id)
                text = (
                    f"دانلود «{title}» در صف قرار گرفت"
                    + (f" (کار #{job_id})" if job_id is not None else "")
                    + "."
                )
                if job_id is None:
                    text = "این آهنگ قبلاً در صف دانلود یا در حال پردازش است."
                _edit_or_send(
                    api,
                    chat_id,
                    text,
                    [[_button("⬇️ Downloads", "downloads")], [_button("⬅️ Main menu", "home")]],
                    message_id,
                )
    elif action == "search_page" and len(parts) == 2:
        state = database.get_user_state(user_id)
        if state is None or not state.startswith("search_results:"):
            _show_home(api, chat_id, message_id, "That search has expired. Start a new search.")
        else:
            query = state.split(":", maxsplit=1)[1]
            _show_search_results(
                user_id, chat_id, query, int(parts[1]), database, api, message_id
            )
    elif action == "search_play" and len(parts) == 2:
        track = database.library_repository.get_track(user_id, int(parts[1]))
        if track is None:
            _show_home(api, chat_id, message_id, "That track is no longer available.")
        else:
            _deliver_track(user_id, chat_id, track, database, api, archive_chat_id)
    elif action == "playlist_page" and len(parts) == 2:
        _show_playlists(
            user_id, chat_id, database, api, message_id, page=int(parts[1])
        )
    elif action == "choose_page" and len(parts) == 4:
        _show_playlists(
            user_id,
            chat_id,
            database,
            api,
            message_id,
            source=(int(parts[2]), int(parts[3])),
            page=int(parts[1]),
        )
    elif action == "cancel_add" and len(parts) == 3:
        database.delete_pending_track(user_id, int(parts[1]), int(parts[2]))
        _show_home(api, chat_id, message_id, "Audio was not added.")
    elif action == "new":
        state = "create"
        if len(parts) == 3:
            state = f"create:{parts[1]}:{parts[2]}"
        database.set_user_state(user_id, state)
        _edit_or_send(
            api,
            chat_id,
            "Send the new playlist name (1–64 characters).",
            [[_button("Cancel", "home")]],
            message_id,
        )
    elif action == "new_track" and len(parts) == 2:
        track_id = int(parts[1])
        if database.library_repository.get_track(user_id, track_id) is None:
            _show_library(user_id, chat_id, database, api, message_id)
            return
        database.set_user_state(user_id, f"create_track:{track_id}")
        _edit_or_send(
            api,
            chat_id,
            "Send the new collection name (1–64 characters).",
            [[_button("Cancel", f"track:{track_id}")]],
            message_id,
        )
    elif action == "open" and len(parts) == 2:
        _clear_interaction_state(database, user_id)
        _show_playlist(user_id, chat_id, int(parts[1]), database, api, message_id)
    elif action == "rename" and len(parts) == 2:
        _clear_interaction_state(database, user_id)
        playlist_id = int(parts[1])
        if PersonalMusicArchive(database).get_collection(user_id, playlist_id) is None:
            _show_playlists(user_id, chat_id, database, api, message_id)
        else:
            database.set_user_state(user_id, f"rename:{playlist_id}")
            _edit_or_send(
                api,
                chat_id,
                "Send the new playlist name (1–64 characters).",
                [[_button("Cancel", f"open:{playlist_id}")]],
                message_id,
            )
    elif action == "tracks" and len(parts) == 3:
        _show_tracks(
            user_id, chat_id, int(parts[1]), int(parts[2]), database, api, message_id
        )
    elif action == "add" and len(parts) == 4:
        playlist_id, source_chat_id, source_message_id = map(int, parts[1:])
        save_result = _save_track(
            user_id,
            source_chat_id,
            source_message_id,
            database,
            api,
            archive_chat_id,
        )
        if save_result is None:
            _edit_or_send(
                api,
                chat_id,
                "This audio selection has expired. Send the audio again to add it.",
                [[_button("Main menu", "home")]],
                message_id,
            )
        else:
            track_id, saved, archive_notice = save_result
            added_to_playlist = PersonalMusicArchive(database).add_to_collection(
                user_id, track_id, playlist_id
            )
            playlist = PersonalMusicArchive(database).get_collection(
                user_id, playlist_id
            )
            status = (
                f"✅ Saved to your library and added to "
                f"“{playlist['name'] if playlist else 'the collection'}”."
                if saved and added_to_playlist
                else (
                    "The audio is in your library, but was already in that collection."
                    if not added_to_playlist
                    else f"Added the existing library track #{track_id} to the collection."
                )
            )
            if archive_chat_id is not None and archive_notice is None:
                status += "\nAvailable from the shared archive."
            elif archive_notice is not None:
                status += f"\n⚠️ {archive_notice}"
            _edit_or_send(
                api,
                chat_id,
                status,
                [
                    [_button("🎵 Open playlist", f"open:{playlist_id}")],
                    [_button("⬅️ Main menu", "home")],
                ],
                message_id,
            )
    elif action in ("play", "shuffle") and len(parts) == 2:
        playlist_id = int(parts[1])
        archive = PersonalMusicArchive(database)
        playlist = archive.get_collection(user_id, playlist_id)
        tracks = archive.list_collection_tracks(user_id, playlist_id)
        if playlist is None or tracks is None:
            _show_playlists(user_id, chat_id, database, api, message_id)
        elif not tracks:
            _edit_or_send(
                api,
                chat_id,
                f"“{playlist['name']}” is empty. Send audio to add tracks.",
                [[_button("⬅️ Playlist", f"open:{playlist_id}")]],
                message_id,
            )
        else:
            if action == "shuffle":
                random.shuffle(tracks)
            selected_tracks = tracks[:MAX_COLLECTION_DELIVERY]
            mode = "Shuffling" if action == "shuffle" else "Sending"
            _edit_or_send(
                api,
                chat_id,
                f"▶️ {mode} {len(selected_tracks)} track(s) from “{playlist['name']}”…"
                + (
                    f" (limited to {MAX_COLLECTION_DELIVERY} per request)."
                    if len(tracks) > MAX_COLLECTION_DELIVERY
                    else ""
                ),
                [[_button("⬅️ Playlist", f"open:{playlist_id}")]],
                message_id,
            )
            for index, track in enumerate(selected_tracks):
                if index > 0:
                    time.sleep(COLLECTION_DELIVERY_INTERVAL)
                _deliver_track(user_id, chat_id, track, database, api, archive_chat_id)
    elif action == "remove" and len(parts) == 3:
        playlist_id, track_id = map(int, parts[1:])
        if PersonalMusicArchive(database).remove_from_collection(
            user_id,
            playlist_id,
            track_id,
        ):
            _show_tracks(user_id, chat_id, playlist_id, 0, database, api, message_id)
        else:
            _show_playlist(user_id, chat_id, playlist_id, database, api, message_id)
    elif action == "delete" and len(parts) == 2:
        playlist_id = int(parts[1])
        playlist = PersonalMusicArchive(database).get_collection(
            user_id,
            playlist_id,
        )
        if playlist is None:
            _show_playlists(user_id, chat_id, database, api, message_id)
        else:
            _edit_or_send(
                api,
                chat_id,
                f"Delete “{playlist['name']}” and all its track references?",
                [
                    [_button("Yes, delete", f"confirm_delete:{playlist_id}")],
                    [_button("Cancel", f"open:{playlist_id}")],
                ],
                message_id,
            )
    elif action == "confirm_delete" and len(parts) == 2:
        PersonalMusicArchive(database).delete_collection(user_id, int(parts[1]))
        _show_playlists(user_id, chat_id, database, api, message_id)
    else:
        _show_home(api, chat_id, message_id, "That menu action is no longer available.")


def _start_download_worker(database: Database, api: TelegramAPI) -> threading.Thread:
    service = DownloadService(
        database.download_repository,
        YtDlpDownloader(),
        FFmpegProcessor(),
    )

    def consume_audio(converted: Path, job: dict[str, Any]) -> None:
        track_id = job.get("track_id")
        track = (
            database.library_repository.get_track(
                int(job["user_id"]),
                int(track_id),
            )
            if track_id is not None
            else None
        )
        title = str(track["title"]) if track else converted.stem
        performer = (
            str(track["performer"])
            if track and track.get("performer")
            else None
        )
        api.send_audio(
            int(job["user_id"]),
            converted,
            title,
            performer,
        )

    def worker_loop() -> None:
        LOGGER.info("Download worker started")
        while True:
            try:
                service.process_next(consume_audio)
            except Exception:
                LOGGER.exception("Unexpected download worker failure")
            time.sleep(5)

    worker = threading.Thread(
        target=worker_loop,
        name="tuneshelf-download-worker",
        daemon=True,
    )
    worker.start()
    return worker


def run() -> None:
    load_dotenv()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN to your Telegram bot token.")
    archive_chat_id_value = os.environ.get("ARCHIVE_CHAT_ID", "").strip()
    try:
        archive_chat_id = int(archive_chat_id_value) if archive_chat_id_value else None
    except ValueError as error:
        raise RuntimeError("ARCHIVE_CHAT_ID must be an integer Telegram chat ID.") from error
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from postgres_database import PostgresDatabase

        database = RuntimeDatabase(PostgresDatabase(database_url))
        LOGGER.info("Using PostgreSQL archive database")
    else:
        database = RuntimeDatabase(
            Database(os.environ.get("DATABASE_PATH", "playlist_bot.sqlite3"))
        )
        LOGGER.info("Using SQLite archive database")
    api = TelegramAPI(token)
    _start_download_worker(database, api)
    if archive_chat_id is None:
        LOGGER.info("Shared Telegram archive disabled")
    else:
        LOGGER.info("Shared Telegram archive enabled: chat_id=%s", archive_chat_id)
    contact_email = os.environ.get("MUSICBRAINZ_CONTACT_EMAIL", "").strip()
    metadata_provider = None
    if contact_email:
        metadata_provider = MusicBrainzProvider(contact_email)
        LOGGER.info("MusicBrainz audio metadata enrichment enabled")
    else:
        LOGGER.info("MusicBrainz audio metadata enrichment disabled")
    ytmusic_enabled_value = os.environ.get("YTMUSIC_ENABLED", "true").strip().lower()
    if ytmusic_enabled_value in {"1", "true", "yes", "on"}:
        metadata_search_provider = YouTubeMusicProvider()
        LOGGER.info("YouTube Music metadata search enabled")
    elif ytmusic_enabled_value in {"0", "false", "no", "off"}:
        metadata_search_provider = None
        LOGGER.info("YouTube Music metadata search disabled")
    else:
        raise RuntimeError(
            "YTMUSIC_ENABLED must be a boolean value (true/false, yes/no, or 1/0)."
        )
    offset: int | None = None
    LOGGER.info("Playlist bot started")

    while True:
        try:
            updates = api.call(
                "getUpdates",
                timeout=30,
                offset=offset,
                allowed_updates=["message", "callback_query"],
            )
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        handle_callback(
                            update["callback_query"],
                            database,
                            api,
                            archive_chat_id,
                        )
                    elif "message" in update:
                        handle_message(
                            update["message"],
                            database,
                            api,
                            metadata_provider,
                            archive_chat_id,
                            metadata_search_provider,
                        )
                except Exception:
                    LOGGER.exception("Failed to handle Telegram update %s", update["update_id"])
                    chat_id = update.get("message", {}).get("chat", {}).get("id")
                    if chat_id is None:
                        chat_id = (
                            update.get("callback_query", {})
                            .get("message", {})
                            .get("chat", {})
                            .get("id")
                        )
                    if chat_id is not None:
                        try:
                            api.send_message(chat_id, "Sorry, that request failed. Please try again.")
                        except TelegramAPIError:
                            LOGGER.exception("Could not send update failure notification")
        except TelegramAPIError:
            LOGGER.exception("Telegram polling failed; retrying shortly")
            time.sleep(3)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()
