"""Compatibility facade routing library workflows to normalized persistence."""

from typing import Any


class RuntimeDatabase:
    def __init__(self, legacy_adapter: Any) -> None:
        self.legacy_adapter = legacy_adapter
        self.library_repository = legacy_adapter.library_repository

    def create_playlist(self, user_id: int, name: str) -> bool:
        return self.library_repository.create_playlist(user_id, name)

    create_collection = create_playlist

    def list_playlists(self, user_id: int) -> list[dict[str, Any]]:
        return self.library_repository.list_playlists(user_id)

    list_collections = list_playlists

    def get_playlist(self, user_id: int, playlist_id: int) -> dict[str, Any] | None:
        return self.library_repository.get_playlist(user_id, playlist_id)

    def delete_playlist_by_id(self, user_id: int, playlist_id: int) -> bool:
        return self.library_repository.delete_playlist_by_id(user_id, playlist_id)

    def delete_playlist(self, user_id: int, playlist_name: str) -> bool:
        playlist = next(
            (
                row
                for row in self.list_playlists(user_id)
                if row["name"].lower() == playlist_name.lower()
            ),
            None,
        )
        return (
            self.delete_playlist_by_id(user_id, int(playlist["id"]))
            if playlist is not None
            else False
        )

    def rename_playlist(self, user_id: int, playlist_id: int, name: str) -> bool:
        return self.library_repository.rename_playlist(user_id, playlist_id, name)

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
    ) -> bool:
        _, _added_to_library, added_to_playlist = (
            self.library_repository.add_audio_to_library(
                user_id,
                source_chat_id,
                source_message_id,
                title,
                performer,
                media_type,
                file_unique_id,
                playlist_id=playlist_id,
                release_title=release_title,
                musicbrainz_recording_id=musicbrainz_recording_id,
                telegram_file_id=telegram_file_id,
                duration_seconds=duration_seconds,
                mime_type=mime_type,
                original_filename=original_filename,
                release_year=release_year,
            )
        )
        return added_to_playlist

    def add_track(
        self,
        user_id: int,
        playlist_name: str,
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
    ) -> int:
        playlist = next(
            (
                row
                for row in self.list_playlists(user_id)
                if row["name"].lower() == playlist_name.lower()
            ),
            None,
        )
        if playlist is None:
            raise ValueError(f"Playlist {playlist_name!r} does not exist.")
        track_id, _added_to_library, _added_to_playlist = (
            self.library_repository.add_audio_to_library(
                user_id,
                source_chat_id,
                source_message_id,
                title,
                performer,
                media_type,
                file_unique_id,
                playlist_id=int(playlist["id"]),
                release_title=release_title,
                musicbrainz_recording_id=musicbrainz_recording_id,
                telegram_file_id=telegram_file_id,
                duration_seconds=duration_seconds,
                mime_type=mime_type,
                original_filename=original_filename,
                release_year=release_year,
            )
        )
        return track_id

    def list_tracks(self, user_id: int, playlist_name: str) -> list[dict[str, Any]] | None:
        playlist = next(
            (
                row
                for row in self.list_playlists(user_id)
                if row["name"].lower() == playlist_name.lower()
            ),
            None,
        )
        if playlist is None:
            return None
        return self.library_repository.list_tracks_by_id(
            user_id, int(playlist["id"])
        )

    def list_tracks_by_id(
        self, user_id: int, playlist_id: int
    ) -> list[dict[str, Any]] | None:
        return self.library_repository.list_tracks_by_id(user_id, playlist_id)

    def get_track(self, user_id: int, track_id: int) -> dict[str, Any] | None:
        return self.library_repository.get_track(user_id, track_id)

    def list_library(
        self, user_id: int, limit: int = 8, offset: int = 0
    ) -> list[dict[str, Any]]:
        return self.library_repository.list_library(user_id, limit, offset)

    def search_tracks(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.library_repository.search_tracks(*args, **kwargs)

    def search_metadata_only_tracks(
        self, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self.library_repository.search_metadata_only_tracks(*args, **kwargs)

    def list_metadata_only_tracks(self, user_id: int) -> list[dict[str, Any]]:
        return self.library_repository.list_metadata_only_tracks(user_id)

    def delete_metadata_only_track(self, user_id: int, track_id: int) -> bool:
        return self.library_repository.delete_metadata_only_track(user_id, track_id)

    def get_statistics(self, user_id: int) -> dict[str, Any]:
        return self.library_repository.get_statistics(user_id)

    def discover_tracks(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        return self.library_repository.discover_tracks(user_id, limit)

    def get_track_id_for_source_reference(
        self,
        user_id: int,
        playlist_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> int | None:
        return self.library_repository.get_track_id_for_source_reference(
            user_id, source_chat_id, source_message_id
        )

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.library_repository, name):
            return getattr(self.library_repository, name)
        return getattr(self.legacy_adapter, name)
