"""Telegram-independent domain and application services for personal music archives."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TrackMetadata:
    title: str
    performer: str | None = None
    release_title: str | None = None
    duration_seconds: int | None = None
    mime_type: str | None = None
    original_filename: str | None = None
    musicbrainz_recording_id: str | None = None
    release_year: int | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Track title cannot be empty.")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("Track duration cannot be negative.")
        if self.release_year is not None and not 1000 <= self.release_year <= 9999:
            raise ValueError("Release year must be between 1000 and 9999.")


@dataclass(frozen=True)
class TelegramMessageReference:
    source_chat_id: int
    source_message_id: int
    media_type: str
    telegram_file_id: str | None = None
    telegram_file_unique_id: str | None = None
    original_filename: str | None = None

    def __post_init__(self) -> None:
        if self.source_message_id < 1:
            raise ValueError("Telegram message IDs must be positive.")
        if not self.media_type.strip():
            raise ValueError("Media type cannot be empty.")


class ArchiveRepository(Protocol):
    def get_playlist(self, user_id: int, playlist_id: int) -> object | None: ...

    def add_track_to_playlist(
        self,
        user_id: int,
        playlist_id: int,
        source_chat_id: int,
        source_message_id: int,
        title: str,
        performer: str | None,
        media_type: str,
        file_unique_id: str | None,
        release_title: str | None = None,
        musicbrainz_recording_id: str | None = None,
        telegram_file_id: str | None = None,
        duration_seconds: int | None = None,
        mime_type: str | None = None,
        original_filename: str | None = None,
        release_year: int | None = None,
    ) -> bool: ...

    def get_available_message_reference(
        self, user_id: int, track_id: int
    ) -> object | None: ...

    def add_track_to_collection(
        self, user_id: int, track_id: int, collection_id: int
    ) -> bool: ...

    def set_track_rating(
        self, user_id: int, track_id: int, rating: int | None
    ) -> bool: ...

    def set_track_note(self, user_id: int, track_id: int, note: str | None) -> bool: ...

    def update_track_metadata(
        self, user_id: int, track_id: int, title: str, performer: str | None
    ) -> bool: ...

    def add_track_tag(self, user_id: int, track_id: int, name: str) -> bool: ...

    def create_metadata_only_track(
        self,
        user_id: int,
        title: str,
        performer: str | None = None,
        release_title: str | None = None,
        musicbrainz_recording_id: str | None = None,
        provider_url: str | None = None,
        release_year: int | None = None,
    ) -> int: ...


class PersonalMusicArchive:
    """Application operations enforcing owner-scoped access through the repository."""

    def __init__(self, repository: ArchiveRepository) -> None:
        self.repository = repository

    def save_audio(
        self,
        owner_user_id: int,
        collection_id: int,
        metadata: TrackMetadata,
        reference: TelegramMessageReference,
    ) -> bool:
        if self.repository.get_playlist(owner_user_id, collection_id) is None:
            return False
        return self.repository.add_track_to_playlist(
            user_id=owner_user_id,
            playlist_id=collection_id,
            source_chat_id=reference.source_chat_id,
            source_message_id=reference.source_message_id,
            title=metadata.title.strip(),
            performer=metadata.performer,
            media_type=reference.media_type,
            file_unique_id=reference.telegram_file_unique_id,
            release_title=metadata.release_title,
            musicbrainz_recording_id=metadata.musicbrainz_recording_id,
            telegram_file_id=reference.telegram_file_id,
            duration_seconds=metadata.duration_seconds,
            mime_type=metadata.mime_type,
            original_filename=metadata.original_filename or reference.original_filename,
            release_year=metadata.release_year,
        )

    def get_playable_reference(
        self, owner_user_id: int, track_id: int
    ) -> object | None:
        return self.repository.get_available_message_reference(owner_user_id, track_id)

    def add_to_collection(
        self, owner_user_id: int, track_id: int, collection_id: int
    ) -> bool:
        return self.repository.add_track_to_collection(
            owner_user_id, track_id, collection_id
        )

    def set_rating(
        self, owner_user_id: int, track_id: int, rating: int | None
    ) -> bool:
        return self.repository.set_track_rating(owner_user_id, track_id, rating)

    def set_note(
        self, owner_user_id: int, track_id: int, note: str | None
    ) -> bool:
        return self.repository.set_track_note(owner_user_id, track_id, note)

    def update_metadata(
        self,
        owner_user_id: int,
        track_id: int,
        title: str,
        performer: str | None,
    ) -> bool:
        return self.repository.update_track_metadata(
            owner_user_id, track_id, title, performer
        )

    def add_tag(self, owner_user_id: int, track_id: int, tag: str) -> bool:
        return self.repository.add_track_tag(owner_user_id, track_id, tag)

    def save_metadata_only(
        self,
        owner_user_id: int,
        metadata: TrackMetadata,
        provider_url: str | None = None,
    ) -> int:
        return self.repository.create_metadata_only_track(
            owner_user_id,
            metadata.title,
            metadata.performer,
            metadata.release_title,
            metadata.musicbrainz_recording_id,
            provider_url,
            metadata.release_year,
        )
