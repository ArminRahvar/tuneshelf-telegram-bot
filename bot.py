import json
import logging
import os
import random
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from archive import PersonalMusicArchive, TelegramMessageReference, TrackMetadata
from database import Database
from musicbrainz import MusicBrainzError, MusicBrainzProvider, RecordingMetadata
from telegram_delivery import TelegramDeliveryService


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
            [{"text": "⚙️ Settings"}],
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
    tracks = database.list_library(
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
    stats = database.get_statistics(user_id)
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
    track = database.get_track(user_id, track_id)
    if track is None:
        _show_library(user_id, chat_id, database, api, message_id)
        return
    tags = database.list_track_tags(user_id, track_id) or []
    references = database.list_message_references(user_id, track_id) or []
    available_count = sum(bool(reference["is_available"]) for reference in references)
    lines = [
        f"#{track_id} {_format_track(track)}",
        f"Collection: {track['playlist_name']}",
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
    suggestions = database.discover_tracks(user_id, SEARCH_RESULTS_PER_PAGE)
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
    mode = database.get_delivery_mode(user_id)
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
) -> None:
    playlists = database.list_playlists(user_id)
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
    playlist = database.get_playlist(user_id, playlist_id)
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
    playlist = database.get_playlist(user_id, playlist_id)
    tracks = database.list_tracks_by_id(user_id, playlist_id)
    if playlist is None or tracks is None:
        _show_playlists(user_id, chat_id, database, api, message_id)
        return
    page_count = max(1, (len(tracks) + TRACKS_PER_PAGE - 1) // TRACKS_PER_PAGE)
    page = min(max(page, 0), page_count - 1)
    start = page * TRACKS_PER_PAGE
    selected = tracks[start : start + TRACKS_PER_PAGE]
    keyboard: list[list[dict[str, str]]] = []
    for index, track in enumerate(selected, start + 1):
        label = f"✖ #{track['id']} · {index}. {_format_track(track)}"
        keyboard.append([_button(label[:60], f"remove:{playlist_id}:{track['id']}")])
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
        text += "\nTap a track to remove it."
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
        results = database.search_metadata_only_tracks(
            user_id,
            search_text,
            SEARCH_RESULTS_PER_PAGE + 1,
            offset,
            filters["release_year"],
        )
    else:
        results = database.search_tracks(
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
    playlist_id: int,
    source_chat_id: int,
    source_message_id: int,
    database: Database,
) -> tuple[str, bool] | None:
    playlist = database.get_playlist(user_id, playlist_id)
    pending = database.get_pending_track(user_id, source_chat_id, source_message_id)
    if playlist is None or pending is None:
        return None
    archive = PersonalMusicArchive(database)
    saved = archive.save_audio(
        user_id,
        playlist_id,
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
    return playlist["name"], saved


def _deliver_track(
    user_id: int,
    chat_id: int,
    track: dict[str, Any],
    database: Database,
    api: TelegramAPI,
) -> bool:
    reference = database.get_available_message_reference(user_id, track["id"])
    if reference is None:
        api.send_message(
            chat_id,
            f"No playable Telegram message is available for {_format_track(track)}. "
            "Send another copy of the audio, then use /link to associate it.",
        )
        return False
    delivery = TelegramDeliveryService(api, database.get_delivery_mode(user_id))
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
            database.mark_message_reference_verified(user_id, reference["id"])
            return True
        LOGGER.error("Telegram could not deliver track %s: %s", track["id"], error)
        if error.error_code in (400, 403):
            database.mark_message_reference_unavailable(user_id, reference["id"])
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
    database.mark_message_reference_verified(user_id, reference["id"])
    return True


def handle_message(
    message: dict[str, Any],
    database: Database,
    api: TelegramAPI,
    metadata_provider: MusicBrainzProvider | None = None,
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
    if command == "/link":
        try:
            track_id = int(command_args)
        except ValueError:
            api.send_message(chat_id, "Usage: /link <your-track-id>")
            return
        if database.get_track(user_id, track_id) is None:
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
            changed = database.update_track_metadata(user_id, track_id, title, performer)
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
            updated = database.set_track_rating(user_id, track_id, rating)
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
        track = database.get_track(user_id, track_id)
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
        track = database.get_track(user_id, track_id)
        if track is None:
            api.send_message(chat_id, "That track is not in your personal library.")
            return
        _deliver_track(user_id, chat_id, track, database, api)
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
        updated = database.set_track_note(user_id, track_id, note)
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
                database.add_track_tag(user_id, track_id, pieces[1])
                if command == "/tag"
                else database.remove_track_tag(user_id, track_id, pieces[1])
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
        rows = database.list_metadata_only_tracks(user_id)
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
        deleted = database.delete_metadata_only_track(user_id, track_id)
        api.send_message(
            chat_id,
            "Metadata-only track removed."
            if deleted
            else "That metadata-only track is not in your personal library.",
        )
        return
    if command == "/collections":
        collections = database.list_collections(user_id)
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
        if database.get_track(user_id, track_id) is None:
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
        added = database.add_message_reference(
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
        _show_playlists(
            user_id,
            chat_id,
            database,
            api,
            source=(chat_id, source_message_id),
            notice=metadata_notice,
        )
        return

    if not text or text.startswith("/"):
        return
    if state is not None and state.startswith("create"):
        playlist_name = text.strip()
        if not playlist_name or len(playlist_name) > 64:
            api.send_message(chat_id, "Playlist names must contain 1–64 characters. Try again.")
            return
        if not database.create_playlist(user_id, playlist_name):
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
            if database.get_track(user_id, track_id) is None:
                database.clear_user_state(user_id)
                api.send_message(chat_id, "That track is not in your personal library.")
                return
            try:
                if action == "rate_track":
                    value = None if text.lower() == "clear" else int(text)
                    changed = database.set_track_rating(user_id, track_id, value)
                elif action == "note_track":
                    if len(text) > 500:
                        raise ValueError("Notes must be at most 500 characters.")
                    changed = database.set_track_note(
                        user_id, track_id, None if text.lower() == "clear" else text
                    )
                elif action == "tag_track":
                    changed = database.add_track_tag(user_id, track_id, text)
                else:
                    pieces = text.split("|", maxsplit=1)
                    changed = database.update_track_metadata(
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
    if state is not None and state.startswith("link:"):
        api.send_message(chat_id, "Send an audio or audio document to associate it.")
        return
    if state == "search":
        query = text.strip()
        if not query or len(query) > 100:
            api.send_message(chat_id, "Search text must contain 1–100 characters. Try again.")
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
        playlist = database.get_playlist(user_id, playlist_id)
        if playlist is None:
            database.clear_user_state(user_id)
            api.send_message(chat_id, "That playlist no longer exists.")
            _show_playlists(user_id, chat_id, database, api)
            return
        if not database.rename_playlist(user_id, playlist_id, playlist_name):
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

    if metadata_provider is not None and state is None:
        try:
            if text.startswith(("https://", "http://")):
                matches = [metadata_provider.lookup_url(text)]
            else:
                title_query, separator, artist_query = text.partition(" - ")
                matches = metadata_provider.search_recordings(
                    title_query.strip(),
                    artist_query.strip() if separator and artist_query.strip() else None,
                )
        except MusicBrainzError as error:
            LOGGER.warning("MusicBrainz text search failed for %r: %s", text, error)
            api.send_message(
                chat_id,
                f"Music metadata search failed: {error}. No track was added.",
            )
            return
        if len(matches) > 1:
            candidates = [
                {
                    "recording_id": match.recording_id,
                    "title": match.title,
                    "artist": match.artist,
                    "release_title": match.release_title,
                    "score": match.score,
                    "release_year": match.release_year,
                }
                for match in matches[:5]
            ]
            database.set_user_state(
                user_id, f"metadata_results:{json.dumps(candidates, ensure_ascii=True)}"
            )
            keyboard = [
                [
                    _button(
                        f"{match.title} — {match.artist or 'Unknown artist'}"[:60],
                        f"metadata:{index}",
                    )
                ]
                for index, match in enumerate(matches[:5])
            ]
            keyboard.append([_button("Cancel", "home")])
            api.send_message(
                chat_id,
                "Several confident metadata matches were found. Choose the correct one. "
                "This saves metadata only; it does not create a playable Telegram reference.",
                _markup(keyboard),
            )
            return
        if matches:
            match = matches[0]
            metadata_track_id = PersonalMusicArchive(database).save_metadata_only(
                user_id,
                TrackMetadata(
                    title=match.title,
                    performer=match.artist,
                    release_title=match.release_title,
                    musicbrainz_recording_id=match.recording_id,
                    release_year=match.release_year,
                ),
                f"https://musicbrainz.org/recording/{match.recording_id}",
            )
            api.send_message(
                chat_id,
                f"Saved metadata-only track #{metadata_track_id}: "
                f"{match.title}"
                + (f" — {match.artist}" if match.artist else "")
                + ". No playable Telegram copy is available; send the audio to add a playable "
                "track to a collection.",
            )
            return
        api.send_message(
            chat_id,
            "No confident metadata match was found. You can still send a Telegram audio "
            "message to save a playable reference.",
        )
        return
    if state is None:
        api.send_message(
            chat_id,
            "Send audio to save a playable Telegram reference, or configure "
            "MUSICBRAINZ_CONTACT_EMAIL to search metadata by text or MusicBrainz URL.",
        )
        return

    _show_home(api, chat_id, text="Use the menu buttons below to manage your playlists.")


def handle_callback(callback: dict[str, Any], database: Database, api: TelegramAPI) -> None:
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
    elif action == "discover":
        _show_discovery(user_id, chat_id, database, api, message_id)
    elif action == "stats":
        _show_statistics(user_id, chat_id, database, api, message_id)
    elif action == "settings":
        _show_settings(user_id, chat_id, database, api, message_id)
    elif action == "delivery" and len(parts) == 2 and parts[1] in {"copy", "forward"}:
        database.set_delivery_mode(user_id, parts[1])
        _show_settings(user_id, chat_id, database, api, message_id)
    elif action == "track" and len(parts) == 2:
        _show_track_detail(
            user_id, chat_id, int(parts[1]), database, api, message_id
        )
    elif action in {"rate_track", "note_track", "tag_track", "edit_track", "link_track"} and len(parts) == 2:
        track_id = int(parts[1])
        if database.get_track(user_id, track_id) is None:
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
        metadata_tracks = database.list_metadata_only_tracks(user_id)
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
                recording_id = selected["recording_id"]
                title = selected["title"]
                artist = selected.get("artist")
                release_title = selected.get("release_title")
                if not all(isinstance(value, str) for value in (recording_id, title)):
                    raise ValueError("Invalid result")
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
                        musicbrainz_recording_id=recording_id,
                        release_year=selected.get("release_year"),
                    ),
                    f"https://musicbrainz.org/recording/{recording_id}",
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
            "Send a track title, artist, or playlist name to search your library.",
            [[_button("Cancel", "home")]],
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
        track = database.get_track(user_id, int(parts[1]))
        if track is None:
            _show_home(api, chat_id, message_id, "That track is no longer available.")
        else:
            _deliver_track(user_id, chat_id, track, database, api)
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
    elif action == "open" and len(parts) == 2:
        _clear_interaction_state(database, user_id)
        _show_playlist(user_id, chat_id, int(parts[1]), database, api, message_id)
    elif action == "rename" and len(parts) == 2:
        _clear_interaction_state(database, user_id)
        playlist_id = int(parts[1])
        if database.get_playlist(user_id, playlist_id) is None:
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
            user_id, playlist_id, source_chat_id, source_message_id, database
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
            playlist_name, saved = save_result
            status = (
                f"✅ Added the audio to “{playlist_name}”."
                if saved
                else f"That audio is already in “{playlist_name}”."
            )
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
        playlist = database.get_playlist(user_id, playlist_id)
        tracks = database.list_tracks_by_id(user_id, playlist_id)
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
                _deliver_track(user_id, chat_id, track, database, api)
    elif action == "remove" and len(parts) == 3:
        playlist_id, track_id = map(int, parts[1:])
        if database.remove_track_by_id(user_id, playlist_id, track_id):
            _show_tracks(user_id, chat_id, playlist_id, 0, database, api, message_id)
        else:
            _show_playlist(user_id, chat_id, playlist_id, database, api, message_id)
    elif action == "delete" and len(parts) == 2:
        playlist_id = int(parts[1])
        playlist = database.get_playlist(user_id, playlist_id)
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
        database.delete_playlist_by_id(user_id, int(parts[1]))
        _show_playlists(user_id, chat_id, database, api, message_id)
    else:
        _show_home(api, chat_id, message_id, "That menu action is no longer available.")


def run() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN to your Telegram bot token.")
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from postgres_database import PostgresDatabase

        database = PostgresDatabase(database_url)
        LOGGER.info("Using PostgreSQL archive database")
    else:
        database = Database(os.environ.get("DATABASE_PATH", "playlist_bot.sqlite3"))
        LOGGER.info("Using SQLite archive database")
    api = TelegramAPI(token)
    contact_email = os.environ.get("MUSICBRAINZ_CONTACT_EMAIL", "").strip()
    metadata_provider = None
    if contact_email:
        metadata_provider = MusicBrainzProvider(contact_email)
        LOGGER.info("MusicBrainz metadata enrichment enabled")
    else:
        LOGGER.warning(
            "MusicBrainz enrichment is disabled; set MUSICBRAINZ_CONTACT_EMAIL to enable it"
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
                        handle_callback(update["callback_query"], database, api)
                    elif "message" in update:
                        handle_message(
                            update["message"], database, api, metadata_provider
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
