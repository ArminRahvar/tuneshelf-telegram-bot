"""Telegram-independent domain and application services for personal music archives."""

from dataclasses import dataclass
from typing import Any, Protocol


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
    def get_playlist(self, user_id: int, playlist_id: int) -> Any | None: ...

    def list_playlists(self, user_id: int) -> list[Any]: ...

    def create_playlist(self, user_id: int, name: str) -> bool: ...

    def rename_playlist(self, user_id: int, playlist_id: int, name: str) -> bool: ...

    def delete_playlist_by_id(self, user_id: int, playlist_id: int) -> bool: ...

    def list_tracks_by_id(
        self, user_id: int, playlist_id: int
    ) -> list[Any] | None: ...

    def remove_track_by_id(
        self, user_id: int, playlist_id: int, track_id: int
    ) -> bool: ...

    def add_audio_to_library(
        self,
        user_id: int,
        source_chat_id: int,
        source_message_id: int,
        title: str,
        performer: str | None,
        media_type: str,
        file_unique_id: str | None,
        *,
        playlist_id: int | None = None,
        release_title: str | None = None,
        musicbrainz_recording_id: str | None = None,
        telegram_file_id: str | None = None,
        duration_seconds: int | None = None,
        mime_type: str | None = None,
        original_filename: str | None = None,
        release_year: int | None = None,
    ) -> tuple[int, bool, bool]: ...

    def get_available_message_reference(
        self,
        user_id: int,
        track_id: int,
        preferred_chat_id: int | None = None,
    ) -> Any | None: ...

    def find_shared_archive_reference(
        self,
        archive_chat_id: int,
        file_unique_id: str | None,
        musicbrainz_recording_id: str | None = None,
    ) -> Any | None: ...

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

    def remove_track_tag(self, user_id: int, track_id: int, name: str) -> bool: ...

    def get_delivery_mode(self, user_id: int) -> str: ...

    def set_delivery_mode(self, user_id: int, mode: str) -> bool: ...

    def create_metadata_only_track(
        self,
        user_id: int,
        title: str,
        performer: str | None = None,
        release_title: str | None = None,
        musicbrainz_recording_id: str | None = None,
        provider_url: str | None = None,
        release_year: int | None = None,
        source_provider: str | None = None,
    ) -> int: ...


class PersonalMusicArchive:
    """Application operations enforcing owner-scoped access through the repository."""

    def __init__(self, repository: ArchiveRepository) -> None:
        self.repository = getattr(repository, "library_repository", repository)

    def save_audio(
        self,
        owner_user_id: int,
        metadata: TrackMetadata,
        reference: TelegramMessageReference,
        collection_id: int | None = None,
    ) -> tuple[int, bool, bool]:
        return self.repository.add_audio_to_library(
            user_id=owner_user_id,
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
            playlist_id=collection_id,
        )

    def get_playable_reference(
        self,
        owner_user_id: int,
        track_id: int,
        preferred_chat_id: int | None = None,
    ) -> Any | None:
        return self.repository.get_available_message_reference(
            owner_user_id,
            track_id,
            preferred_chat_id,
        )

    def list_collections(self, owner_user_id: int) -> list[Any]:
        return self.repository.list_playlists(owner_user_id)

    def get_collection(self, owner_user_id: int, collection_id: int) -> Any | None:
        return self.repository.get_playlist(owner_user_id, collection_id)

    def create_collection(self, owner_user_id: int, name: str) -> bool:
        return self.repository.create_playlist(owner_user_id, name)

    def rename_collection(
        self,
        owner_user_id: int,
        collection_id: int,
        name: str,
    ) -> bool:
        return self.repository.rename_playlist(owner_user_id, collection_id, name)

    def delete_collection(self, owner_user_id: int, collection_id: int) -> bool:
        return self.repository.delete_playlist_by_id(owner_user_id, collection_id)

    def list_collection_tracks(
        self,
        owner_user_id: int,
        collection_id: int,
    ) -> list[Any] | None:
        return self.repository.list_tracks_by_id(owner_user_id, collection_id)

    def remove_from_collection(
        self,
        owner_user_id: int,
        collection_id: int,
        track_id: int,
    ) -> bool:
        return self.repository.remove_track_by_id(
            owner_user_id,
            collection_id,
            track_id,
        )

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

    def remove_tag(self, owner_user_id: int, track_id: int, tag: str) -> bool:
        return self.repository.remove_track_tag(owner_user_id, track_id, tag)

    def get_delivery_mode(self, owner_user_id: int) -> str:
        return self.repository.get_delivery_mode(owner_user_id)

    def set_delivery_mode(self, owner_user_id: int, mode: str) -> bool:
        return self.repository.set_delivery_mode(owner_user_id, mode)

    def save_metadata_only(
        self,
        owner_user_id: int,
        metadata: TrackMetadata,
        provider_url: str | None = None,
        source_provider: str | None = None,
    ) -> int:
        return self.repository.create_metadata_only_track(
            owner_user_id,
            metadata.title,
            metadata.performer,
            metadata.release_title,
            metadata.musicbrainz_recording_id,
            provider_url,
            metadata.release_year,
            source_provider,
        )
