"""Persistence operations for the normalized personal-library schema."""

from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.engine import Engine

from app.database.engine import create_database_engine
from app.database.models import (
    Album,
    Artist,
    CatalogTrack,
    CatalogTrackArtist,
    LibraryEntry,
    PlaylistItem,
    TelegramSourceReference,
    User,
    UserPlaylist,
    UserPreference,
    UserTag,
    UserTrackNote,
    UserTrackMetadata,
    UserTrackRating,
    UserTrackTag,
)


class LibraryRepository:
    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.engine = engine or create_database_engine(database_url)

    def close(self) -> None:
        self.engine.dispose()

    def _ensure_user(self, connection: Any, user_id: int) -> None:
        if self.engine.dialect.name == "sqlite":
            connection.execute(
                User.__table__.insert()
                .values(telegram_user_id=user_id)
                .prefix_with("OR IGNORE")
            )
        else:
            from sqlalchemy.dialects.postgresql import insert

            connection.execute(
                insert(User.__table__)
                .values(telegram_user_id=user_id)
                .on_conflict_do_nothing()
            )

    @staticmethod
    def _track_select():
        performer = (
            select(Artist.name)
            .join(CatalogTrackArtist, CatalogTrackArtist.artist_id == Artist.id)
            .where(CatalogTrackArtist.track_id == CatalogTrack.id)
            .order_by(CatalogTrackArtist.position)
            .limit(1)
            .scalar_subquery()
        )
        release_title = (
            select(Album.title)
            .where(Album.id == CatalogTrack.album_id)
            .scalar_subquery()
        )
        playlist = (
            select(UserPlaylist.id, UserPlaylist.name)
            .join(PlaylistItem, PlaylistItem.playlist_id == UserPlaylist.id)
            .where(
                PlaylistItem.track_id == CatalogTrack.id,
                PlaylistItem.user_id == LibraryEntry.user_id,
            )
            .order_by(PlaylistItem.position)
            .limit(1)
        )
        playlist_id = playlist.with_only_columns(UserPlaylist.id).scalar_subquery()
        playlist_name = playlist.with_only_columns(UserPlaylist.name).scalar_subquery()
        reference = (
            select(
                TelegramSourceReference.source_chat_id,
                TelegramSourceReference.source_message_id,
                TelegramSourceReference.telegram_file_id,
                TelegramSourceReference.telegram_file_unique_id,
                TelegramSourceReference.media_type,
                TelegramSourceReference.mime_type,
                TelegramSourceReference.original_filename,
            )
            .where(
                TelegramSourceReference.user_id == LibraryEntry.user_id,
                TelegramSourceReference.track_id == CatalogTrack.id,
            )
            .order_by(TelegramSourceReference.id)
            .limit(1)
        )
        is_playable = (
            select(TelegramSourceReference.id)
            .where(
                TelegramSourceReference.user_id == LibraryEntry.user_id,
                TelegramSourceReference.track_id == CatalogTrack.id,
                TelegramSourceReference.is_available.is_(True),
            )
            .exists()
        )
        return (
            select(
                CatalogTrack.id.label("id"),
                func.coalesce(UserTrackMetadata.title, CatalogTrack.title).label(
                    "title"
                ),
                case(
                    (
                        UserTrackMetadata.track_id.is_not(None),
                        UserTrackMetadata.performer,
                    ),
                    else_=performer,
                ).label("performer"),
                release_title.label("release_title"),
                CatalogTrack.musicbrainz_recording_id.label(
                    "musicbrainz_recording_id"
                ),
                CatalogTrack.duration_seconds.label("duration_seconds"),
                CatalogTrack.release_year.label("release_year"),
                UserTrackRating.rating.label("rating"),
                UserTrackNote.note.label("note"),
                CatalogTrack.album_id.label("album_id"),
                reference.with_only_columns(
                    TelegramSourceReference.source_chat_id
                ).scalar_subquery().label("source_chat_id"),
                reference.with_only_columns(
                    TelegramSourceReference.source_message_id
                ).scalar_subquery().label("source_message_id"),
                reference.with_only_columns(
                    TelegramSourceReference.telegram_file_id
                ).scalar_subquery().label("telegram_file_id"),
                reference.with_only_columns(
                    TelegramSourceReference.telegram_file_unique_id
                ).scalar_subquery().label("file_unique_id"),
                reference.with_only_columns(
                    TelegramSourceReference.media_type
                ).scalar_subquery().label("media_type"),
                reference.with_only_columns(
                    TelegramSourceReference.mime_type
                ).scalar_subquery().label("mime_type"),
                reference.with_only_columns(
                    TelegramSourceReference.original_filename
                ).scalar_subquery().label("original_filename"),
                playlist_id.label("playlist_id"),
                playlist_name.label("playlist_name"),
                is_playable.label("is_playable"),
            )
            .select_from(LibraryEntry)
            .join(CatalogTrack, CatalogTrack.id == LibraryEntry.track_id)
            .outerjoin(
                UserTrackRating,
                and_(
                    UserTrackRating.user_id == LibraryEntry.user_id,
                    UserTrackRating.track_id == LibraryEntry.track_id,
                ),
            )
            .outerjoin(
                UserTrackNote,
                and_(
                    UserTrackNote.user_id == LibraryEntry.user_id,
                    UserTrackNote.track_id == LibraryEntry.track_id,
                ),
            )
            .outerjoin(
                UserTrackMetadata,
                and_(
                    UserTrackMetadata.user_id == LibraryEntry.user_id,
                    UserTrackMetadata.track_id == LibraryEntry.track_id,
                ),
            )
        )

    def list_playlists(self, user_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    UserPlaylist.id,
                    UserPlaylist.name,
                    func.count(PlaylistItem.track_id).label("track_count"),
                )
                .outerjoin(
                    PlaylistItem,
                    and_(
                        PlaylistItem.playlist_id == UserPlaylist.id,
                        PlaylistItem.user_id == UserPlaylist.user_id,
                    ),
                )
                .where(UserPlaylist.user_id == user_id)
                .group_by(UserPlaylist.id)
                .order_by(func.lower(UserPlaylist.name), UserPlaylist.id)
            ).mappings()
            return [dict(row) for row in rows]

    def get_playlist(self, user_id: int, playlist_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    UserPlaylist.id,
                    UserPlaylist.name,
                    func.count(PlaylistItem.track_id).label("track_count"),
                )
                .outerjoin(
                    PlaylistItem,
                    and_(
                        PlaylistItem.playlist_id == UserPlaylist.id,
                        PlaylistItem.user_id == UserPlaylist.user_id,
                    ),
                )
                .where(
                    UserPlaylist.user_id == user_id,
                    UserPlaylist.id == playlist_id,
                )
                .group_by(UserPlaylist.id)
            ).mappings().first()
            return dict(row) if row is not None else None

    def create_playlist(self, user_id: int, name: str) -> bool:
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("Playlist names must contain 1–64 characters.")
        with self.engine.begin() as connection:
            self._ensure_user(connection, user_id)
            existing = connection.execute(
                select(UserPlaylist.id).where(
                    UserPlaylist.user_id == user_id,
                    func.lower(UserPlaylist.name) == normalized.lower(),
                )
            ).first()
            if existing is not None:
                return False
            connection.execute(
                UserPlaylist.__table__.insert().values(
                    user_id=user_id, name=normalized
                )
            )
            return True

    def rename_playlist(self, user_id: int, playlist_id: int, name: str) -> bool:
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("Playlist names must contain 1–64 characters.")
        with self.engine.begin() as connection:
            conflict = connection.execute(
                select(UserPlaylist.id).where(
                    UserPlaylist.user_id == user_id,
                    func.lower(UserPlaylist.name) == normalized.lower(),
                    UserPlaylist.id != playlist_id,
                )
            ).first()
            if conflict is not None:
                return False
            result = connection.execute(
                update(UserPlaylist)
                .where(UserPlaylist.user_id == user_id, UserPlaylist.id == playlist_id)
                .values(name=normalized)
            )
            return result.rowcount == 1

    def delete_playlist_by_id(self, user_id: int, playlist_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                UserPlaylist.__table__.delete().where(
                    UserPlaylist.user_id == user_id, UserPlaylist.id == playlist_id
                )
            )
            return result.rowcount == 1

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
    ) -> tuple[int, bool, bool]:
        with self.engine.begin() as connection:
            if playlist_id is not None and connection.execute(
                select(UserPlaylist.id).where(
                    UserPlaylist.id == playlist_id, UserPlaylist.user_id == user_id
                )
            ).first() is None:
                raise ValueError("That playlist is not available in your library.")

            self._ensure_user(connection, user_id)

            track_id = None
            if file_unique_id:
                track_id = connection.execute(
                    select(TelegramSourceReference.track_id)
                    .where(
                        TelegramSourceReference.telegram_file_unique_id
                        == file_unique_id
                    )
                    .order_by(TelegramSourceReference.id)
                    .limit(1)
                ).scalar_one_or_none()
            if track_id is None and musicbrainz_recording_id:
                track_id = connection.execute(
                    select(CatalogTrack.id)
                    .where(
                        CatalogTrack.musicbrainz_recording_id
                        == musicbrainz_recording_id
                    )
                    .order_by(CatalogTrack.id)
                    .limit(1)
                ).scalar_one_or_none()

            if track_id is None:
                album_id = None
                if release_title:
                    album_id = connection.execute(
                        select(Album.id)
                        .where(func.lower(Album.title) == release_title.lower())
                        .order_by(Album.id)
                        .limit(1)
                    ).scalar_one_or_none()
                    if album_id is None:
                        album_id = connection.execute(
                            Album.__table__.insert()
                            .values(title=release_title, release_year=release_year)
                            .returning(Album.id)
                        ).scalar_one()
                track_id = connection.execute(
                    CatalogTrack.__table__.insert()
                    .values(
                        title=title,
                        album_id=album_id,
                        duration_seconds=duration_seconds,
                        release_year=release_year,
                        musicbrainz_recording_id=musicbrainz_recording_id,
                    )
                    .returning(CatalogTrack.id)
                ).scalar_one()
                if performer and performer.strip():
                    artist_id = connection.execute(
                        select(Artist.id)
                        .where(func.lower(Artist.name) == performer.strip().lower())
                        .order_by(Artist.id)
                        .limit(1)
                    ).scalar_one_or_none()
                    if artist_id is None:
                        artist_id = connection.execute(
                            Artist.__table__.insert()
                            .values(name=performer.strip())
                            .returning(Artist.id)
                        ).scalar_one()
                    connection.execute(
                        CatalogTrackArtist.__table__.insert().values(
                            track_id=track_id, artist_id=artist_id, position=0
                        )
                    )

            was_member = connection.execute(
                select(LibraryEntry.user_id).where(
                    LibraryEntry.user_id == user_id, LibraryEntry.track_id == track_id
                )
            ).first() is not None
            if not was_member:
                connection.execute(
                    LibraryEntry.__table__.insert().values(
                        user_id=user_id, track_id=track_id
                    )
                )

            existing_reference = connection.execute(
                select(TelegramSourceReference.id).where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.track_id == track_id,
                    TelegramSourceReference.source_chat_id == source_chat_id,
                    TelegramSourceReference.source_message_id == source_message_id,
                )
            ).first()
            if existing_reference is None:
                connection.execute(
                    TelegramSourceReference.__table__.insert().values(
                        user_id=user_id,
                        track_id=track_id,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        telegram_file_id=telegram_file_id,
                        telegram_file_unique_id=file_unique_id,
                        media_type=media_type,
                        mime_type=mime_type,
                        original_filename=original_filename,
                    )
                )

            added_to_playlist = False
            if playlist_id is not None:
                existing_item = connection.execute(
                    select(PlaylistItem.track_id).where(
                        PlaylistItem.playlist_id == playlist_id,
                        PlaylistItem.track_id == track_id,
                    )
                ).first()
                if existing_item is None:
                    position = connection.execute(
                        select(func.coalesce(func.max(PlaylistItem.position) + 1, 0))
                        .where(PlaylistItem.playlist_id == playlist_id)
                    ).scalar_one()
                    connection.execute(
                        PlaylistItem.__table__.insert().values(
                            playlist_id=playlist_id,
                            track_id=track_id,
                            user_id=user_id,
                            position=position,
                        )
                    )
                    added_to_playlist = True
            return int(track_id), not was_member, added_to_playlist

    def list_library(
        self, user_id: int, limit: int = 8, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._track_select()
                .where(LibraryEntry.user_id == user_id)
                .order_by(LibraryEntry.added_at.desc(), LibraryEntry.track_id.desc())
                .limit(limit)
                .offset(offset)
            ).mappings()
            return [dict(row) for row in rows]

    def get_track(self, user_id: int, track_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                self._track_select().where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).mappings().first()
            return dict(row) if row is not None else None

    def list_tracks_by_id(
        self, user_id: int, playlist_id: int
    ) -> list[dict[str, Any]] | None:
        if self.get_playlist(user_id, playlist_id) is None:
            return None
        with self.engine.connect() as connection:
            rows = connection.execute(
                self._track_select()
                .join(
                    PlaylistItem,
                    and_(
                        PlaylistItem.track_id == LibraryEntry.track_id,
                        PlaylistItem.user_id == LibraryEntry.user_id,
                    ),
                )
                .where(
                    LibraryEntry.user_id == user_id,
                    PlaylistItem.playlist_id == playlist_id,
                )
                .order_by(PlaylistItem.position)
            ).mappings()
            return [dict(row) for row in rows]

    def add_track_to_collection(
        self, user_id: int, track_id: int, playlist_id: int
    ) -> bool:
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(LibraryEntry.track_id)
                .join(
                    UserPlaylist,
                    and_(
                        UserPlaylist.id == playlist_id,
                        UserPlaylist.user_id == LibraryEntry.user_id,
                    ),
                )
                .where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first()
            if exists is None:
                return False
            current = connection.execute(
                select(PlaylistItem.track_id).where(
                    PlaylistItem.playlist_id == playlist_id,
                    PlaylistItem.track_id == track_id,
                )
            ).first()
            if current is not None:
                return False
            position = connection.execute(
                select(func.coalesce(func.max(PlaylistItem.position) + 1, 0)).where(
                    PlaylistItem.playlist_id == playlist_id
                )
            ).scalar_one()
            connection.execute(
                PlaylistItem.__table__.insert().values(
                    playlist_id=playlist_id,
                    track_id=track_id,
                    user_id=user_id,
                    position=position,
                )
            )
            return True

    def remove_track_by_id(self, user_id: int, playlist_id: int, track_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                PlaylistItem.__table__.delete().where(
                    PlaylistItem.user_id == user_id,
                    PlaylistItem.playlist_id == playlist_id,
                    PlaylistItem.track_id == track_id,
                )
            )
            return result.rowcount == 1

    def add_message_reference(
        self,
        user_id: int,
        track_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: str,
        telegram_file_id: str | None = None,
        file_unique_id: str | None = None,
        original_filename: str | None = None,
        mime_type: str | None = None,
    ) -> bool:
        with self.engine.begin() as connection:
            if connection.execute(
                select(LibraryEntry.track_id).where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first() is None:
                return False
            existing = connection.execute(
                select(TelegramSourceReference.id).where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.track_id == track_id,
                    TelegramSourceReference.source_chat_id == source_chat_id,
                    TelegramSourceReference.source_message_id == source_message_id,
                )
            ).first()
            if existing is not None:
                return False
            connection.execute(
                TelegramSourceReference.__table__.insert().values(
                    user_id=user_id,
                    track_id=track_id,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    telegram_file_id=telegram_file_id,
                    telegram_file_unique_id=file_unique_id,
                    media_type=media_type,
                    mime_type=mime_type,
                    original_filename=original_filename,
                )
            )
            return True

    def get_track_id_for_source_reference(
        self, user_id: int, source_chat_id: int, source_message_id: int
    ) -> int | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(TelegramSourceReference.track_id).where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.source_chat_id == source_chat_id,
                    TelegramSourceReference.source_message_id == source_message_id,
                ).order_by(TelegramSourceReference.id).limit(1)
            ).scalar_one_or_none()

    def get_available_message_reference(
        self,
        user_id: int,
        track_id: int,
        preferred_chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            query = select(
                TelegramSourceReference.id,
                TelegramSourceReference.source_chat_id,
                TelegramSourceReference.source_message_id,
                TelegramSourceReference.telegram_file_id,
                TelegramSourceReference.telegram_file_unique_id,
                TelegramSourceReference.media_type,
            ).where(
                TelegramSourceReference.user_id == user_id,
                TelegramSourceReference.track_id == track_id,
                TelegramSourceReference.is_available.is_(True),
            )
            if preferred_chat_id is not None:
                query = query.order_by(
                    (TelegramSourceReference.source_chat_id != preferred_chat_id),
                    TelegramSourceReference.id,
                )
            else:
                query = query.order_by(TelegramSourceReference.id)
            row = connection.execute(query.limit(1)).mappings().first()
            return dict(row) if row is not None else None

    def list_message_references(
        self, user_id: int, track_id: int
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    TelegramSourceReference.id,
                    TelegramSourceReference.source_chat_id,
                    TelegramSourceReference.source_message_id,
                    TelegramSourceReference.media_type,
                    TelegramSourceReference.is_available,
                    TelegramSourceReference.last_verified_at,
                )
                .where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.track_id == track_id,
                )
                .order_by(TelegramSourceReference.id)
            ).mappings()
            return [dict(row) for row in rows]

    def _set_reference_available(
        self, user_id: int, reference_id: int, available: bool
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(TelegramSourceReference)
                .where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.id == reference_id,
                )
                .values(
                    is_available=available,
                    last_verified_at=func.current_timestamp() if available else None,
                )
            )
            return result.rowcount == 1

    def mark_message_reference_available(self, user_id: int, reference_id: int) -> bool:
        return self._set_reference_available(user_id, reference_id, True)

    def mark_message_reference_unavailable(
        self, user_id: int, reference_id: int
    ) -> bool:
        return self._set_reference_available(user_id, reference_id, False)

    def search_tracks(
        self,
        user_id: int,
        query: str,
        limit: int,
        offset: int = 0,
        rating: int | None = None,
        release_year: int | None = None,
        playable: bool | None = None,
    ) -> list[dict[str, Any]]:
        text = f"%{query.strip().lower()}%"
        artist_match = (
            select(CatalogTrackArtist.track_id)
            .join(Artist, Artist.id == CatalogTrackArtist.artist_id)
            .where(
                CatalogTrackArtist.track_id == CatalogTrack.id,
                func.lower(Artist.name).like(text),
            )
            .exists()
        )
        tag_match = (
            select(UserTrackTag.track_id)
            .join(UserTag, UserTag.id == UserTrackTag.tag_id)
            .where(
                UserTrackTag.user_id == LibraryEntry.user_id,
                UserTrackTag.track_id == LibraryEntry.track_id,
                func.lower(UserTag.name).like(text),
            )
            .exists()
        )
        album_match = (
            select(Album.id)
            .where(
                Album.id == CatalogTrack.album_id,
                func.lower(Album.title).like(text),
            )
            .exists()
        )
        playlist_match = (
            select(UserPlaylist.id)
            .join(PlaylistItem, PlaylistItem.playlist_id == UserPlaylist.id)
            .where(
                PlaylistItem.user_id == user_id,
                PlaylistItem.track_id == CatalogTrack.id,
                func.lower(UserPlaylist.name).like(text),
            )
            .exists()
        )
        conditions = [
            LibraryEntry.user_id == user_id,
            or_(
                func.lower(CatalogTrack.title).like(text),
                artist_match,
                tag_match,
                album_match,
                playlist_match,
                func.lower(func.coalesce(UserTrackNote.note, "")).like(text),
            ),
        ]
        if rating is not None:
            conditions.append(UserTrackRating.rating == rating)
        if release_year is not None:
            conditions.append(CatalogTrack.release_year == release_year)
        if playable is not None:
            has_available_reference = (
                select(TelegramSourceReference.id)
                .where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.track_id == CatalogTrack.id,
                    TelegramSourceReference.is_available.is_(True),
                )
                .exists()
            )
            conditions.append(
                has_available_reference
                if playable
                else ~has_available_reference
            )
        query_statement = (
            self._track_select()
            .where(*conditions)
            .order_by(CatalogTrack.title, CatalogTrack.id)
            .limit(limit)
            .offset(offset)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query_statement).mappings()
            return [dict(row) for row in rows]

    def search_metadata_only_tracks(
        self,
        user_id: int,
        query: str,
        limit: int,
        offset: int = 0,
        release_year: int | None = None,
    ) -> list[dict[str, Any]]:
        text = f"%{query.strip().lower()}%"
        has_reference = (
            select(TelegramSourceReference.id)
            .where(
                TelegramSourceReference.user_id == user_id,
                TelegramSourceReference.track_id == CatalogTrack.id,
            )
            .exists()
        )
        conditions = [
            LibraryEntry.user_id == user_id,
            ~has_reference,
            or_(
                func.lower(CatalogTrack.title).like(text),
                func.lower(Artist.name).like(text),
            ),
        ]
        if release_year is not None:
            conditions.append(CatalogTrack.release_year == release_year)
        statement = (
            self._track_select()
            .outerjoin(
                CatalogTrackArtist,
                CatalogTrackArtist.track_id == CatalogTrack.id,
            )
            .outerjoin(Artist, Artist.id == CatalogTrackArtist.artist_id)
            .where(*conditions)
            .order_by(CatalogTrack.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement).mappings()
            ]

    def list_metadata_only_tracks(self, user_id: int) -> list[dict[str, Any]]:
        return self.search_metadata_only_tracks(user_id, "", 1000)

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
    ) -> int:
        normalized_provider = source_provider or (
            "musicbrainz" if provider_url else None
        )
        if normalized_provider is not None and (
            not normalized_provider.strip() or len(normalized_provider) > 64
        ):
            raise ValueError("Metadata provider must contain 1 to 64 characters.")
        with self.engine.begin() as connection:
            self._ensure_user(connection, user_id)
            if musicbrainz_recording_id:
                existing_track_id = connection.execute(
                    select(CatalogTrack.id)
                    .where(
                        CatalogTrack.musicbrainz_recording_id
                        == musicbrainz_recording_id
                    )
                    .order_by(CatalogTrack.id)
                    .limit(1)
                ).scalar_one_or_none()
                if existing_track_id is not None:
                    member = connection.execute(
                        select(LibraryEntry.track_id).where(
                            LibraryEntry.user_id == user_id,
                            LibraryEntry.track_id == existing_track_id,
                        )
                    ).first()
                    if member is None:
                        connection.execute(
                            LibraryEntry.__table__.insert().values(
                                user_id=user_id, track_id=existing_track_id
                            )
                        )
                    return int(existing_track_id)
            if normalized_provider and provider_url:
                existing_track_id = connection.execute(
                    select(CatalogTrack.id)
                    .where(
                        CatalogTrack.source_provider == normalized_provider,
                        CatalogTrack.source_identifier == provider_url,
                    )
                    .order_by(CatalogTrack.id)
                    .limit(1)
                ).scalar_one_or_none()
                if existing_track_id is not None:
                    member = connection.execute(
                        select(LibraryEntry.track_id).where(
                            LibraryEntry.user_id == user_id,
                            LibraryEntry.track_id == existing_track_id,
                        )
                    ).first()
                    if member is None:
                        connection.execute(
                            LibraryEntry.__table__.insert().values(
                                user_id=user_id, track_id=existing_track_id
                            )
                        )
                    return int(existing_track_id)
            album_id = None
            if release_title:
                album_id = connection.execute(
                    select(Album.id)
                    .where(func.lower(Album.title) == release_title.lower())
                    .order_by(Album.id)
                    .limit(1)
                ).scalar_one_or_none()
                if album_id is None:
                    album_id = connection.execute(
                        Album.__table__.insert()
                        .values(title=release_title, release_year=release_year)
                        .returning(Album.id)
                    ).scalar_one()
            track_id = connection.execute(
                CatalogTrack.__table__.insert()
                .values(
                    title=title.strip(),
                    album_id=album_id,
                    release_year=release_year,
                    musicbrainz_recording_id=musicbrainz_recording_id,
                    source_provider=normalized_provider,
                    source_identifier=provider_url,
                )
                .returning(CatalogTrack.id)
            ).scalar_one()
            if performer and performer.strip():
                artist_id = connection.execute(
                    select(Artist.id)
                    .where(func.lower(Artist.name) == performer.strip().lower())
                    .order_by(Artist.id)
                    .limit(1)
                ).scalar_one_or_none()
                if artist_id is None:
                    artist_id = connection.execute(
                        Artist.__table__.insert()
                        .values(name=performer.strip())
                        .returning(Artist.id)
                    ).scalar_one()
                connection.execute(
                    CatalogTrackArtist.__table__.insert().values(
                        track_id=track_id, artist_id=artist_id, position=0
                    )
                )
            connection.execute(
                LibraryEntry.__table__.insert().values(
                    user_id=user_id, track_id=track_id
                )
            )
            return int(track_id)

    def delete_metadata_only_track(self, user_id: int, track_id: int) -> bool:
        with self.engine.begin() as connection:
            has_reference = connection.execute(
                select(TelegramSourceReference.id).where(
                    TelegramSourceReference.user_id == user_id,
                    TelegramSourceReference.track_id == track_id,
                )
            ).first()
            if has_reference is not None:
                return False
            result = connection.execute(
                LibraryEntry.__table__.delete().where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            )
            return result.rowcount == 1

    def get_statistics(self, user_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            total = connection.execute(
                select(func.count()).select_from(LibraryEntry).where(
                    LibraryEntry.user_id == user_id
                )
            ).scalar_one()
            metadata = connection.execute(
                select(func.count())
                .select_from(LibraryEntry)
                .where(
                    LibraryEntry.user_id == user_id,
                    ~select(TelegramSourceReference.id)
                    .where(
                        TelegramSourceReference.user_id == user_id,
                        TelegramSourceReference.track_id == LibraryEntry.track_id,
                    )
                    .exists(),
                )
            ).scalar_one()
            playlist_count = connection.execute(
                select(func.count()).select_from(UserPlaylist).where(
                    UserPlaylist.user_id == user_id
                )
            ).scalar_one()
            ratings = connection.execute(
                select(UserTrackRating.rating, func.count().label("track_count"))
                .where(UserTrackRating.user_id == user_id)
                .group_by(UserTrackRating.rating)
                .order_by(UserTrackRating.rating)
            ).mappings()
            artists = connection.execute(
                select(
                    Artist.name,
                    func.count(func.distinct(CatalogTrack.id)).label("track_count"),
                )
                .join(
                    CatalogTrackArtist,
                    CatalogTrackArtist.artist_id == Artist.id,
                )
                .join(
                    CatalogTrack,
                    CatalogTrack.id == CatalogTrackArtist.track_id,
                )
                .join(LibraryEntry, LibraryEntry.track_id == CatalogTrack.id)
                .where(LibraryEntry.user_id == user_id)
                .group_by(Artist.id)
                .order_by(
                    func.count(func.distinct(CatalogTrack.id)).desc(),
                    func.lower(Artist.name),
                )
                .limit(5)
            ).mappings()
            tags = connection.execute(
                select(
                    UserTag.name,
                    func.count(func.distinct(UserTrackTag.track_id)).label(
                        "track_count"
                    ),
                )
                .join(UserTrackTag, UserTrackTag.tag_id == UserTag.id)
                .where(UserTrackTag.user_id == user_id)
                .group_by(UserTag.id)
                .order_by(
                    func.count(func.distinct(UserTrackTag.track_id)).desc(),
                    func.lower(UserTag.name),
                )
                .limit(5)
            ).mappings()
            return {
                "total_tracks": total,
                "metadata_only_tracks": metadata,
                "collection_count": playlist_count,
                "artists": [dict(row) for row in artists],
                "ratings": [dict(row) for row in ratings],
                "tags": [dict(row) for row in tags],
            }

    def discover_tracks(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        artist_overlap = (
            select(CatalogTrackArtist.track_id)
            .where(
                CatalogTrackArtist.track_id == CatalogTrack.id,
                CatalogTrackArtist.artist_id.in_(
                    select(CatalogTrackArtist.artist_id)
                    .join(
                        LibraryEntry,
                        LibraryEntry.track_id == CatalogTrackArtist.track_id,
                    )
                    .where(LibraryEntry.user_id == user_id)
                ),
            )
            .exists()
        )
        tag_overlap = (
            select(UserTrackTag.track_id)
            .where(
                UserTrackTag.user_id == user_id,
                UserTrackTag.track_id == CatalogTrack.id,
            )
            .exists()
        )
        with self.engine.connect() as connection:
            track_count = connection.execute(
                select(func.count()).select_from(LibraryEntry).where(
                    LibraryEntry.user_id == user_id
                )
            ).scalar_one()
            if track_count < 2:
                return []
            rows = connection.execute(
                self._track_select()
                .where(
                    LibraryEntry.user_id == user_id,
                    or_(artist_overlap, tag_overlap),
                )
                .order_by(CatalogTrack.id.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def list_track_tags(self, user_id: int, track_id: int) -> list[str]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(UserTag.name)
                    .join(UserTrackTag, UserTrackTag.tag_id == UserTag.id)
                    .where(
                        UserTrackTag.user_id == user_id,
                        UserTrackTag.track_id == track_id,
                    )
                    .order_by(func.lower(UserTag.name))
                ).scalars()
            )

    def add_track_tag(self, user_id: int, track_id: int, name: str) -> bool:
        normalized = name.strip()
        if not normalized or len(normalized) > 32:
            raise ValueError("Tags must contain 1–32 characters.")
        with self.engine.begin() as connection:
            if connection.execute(
                select(LibraryEntry.track_id).where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first() is None:
                return False
            tag_id = connection.execute(
                select(UserTag.id).where(
                    UserTag.user_id == user_id,
                    func.lower(UserTag.name) == normalized.lower(),
                )
            ).scalar_one_or_none()
            if tag_id is None:
                tag_id = connection.execute(
                    UserTag.__table__.insert()
                    .values(user_id=user_id, name=normalized)
                    .returning(UserTag.id)
                ).scalar_one()
            exists = connection.execute(
                select(UserTrackTag.tag_id).where(
                    UserTrackTag.user_id == user_id,
                    UserTrackTag.track_id == track_id,
                    UserTrackTag.tag_id == tag_id,
                )
            ).first()
            if exists is not None:
                return False
            connection.execute(
                UserTrackTag.__table__.insert().values(
                    user_id=user_id, track_id=track_id, tag_id=tag_id
                )
            )
            return True

    def remove_track_tag(self, user_id: int, track_id: int, name: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                UserTrackTag.__table__.delete().where(
                    UserTrackTag.user_id == user_id,
                    UserTrackTag.track_id == track_id,
                    UserTrackTag.tag_id
                    == select(UserTag.id)
                    .where(
                        UserTag.user_id == user_id,
                        func.lower(UserTag.name) == name.strip().lower(),
                    )
                    .scalar_subquery(),
                )
            )
            return result.rowcount == 1

    def set_track_rating(
        self, user_id: int, track_id: int, rating: int | None
    ) -> bool:
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError("Rating must be between 1 and 5.")
        with self.engine.begin() as connection:
            member = connection.execute(
                select(LibraryEntry.track_id).where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first()
            if member is None:
                return False
            if rating is None:
                connection.execute(
                    UserTrackRating.__table__.delete().where(
                        UserTrackRating.user_id == user_id,
                        UserTrackRating.track_id == track_id,
                    )
                )
                return True
            exists = connection.execute(
                select(UserTrackRating.track_id).where(
                    UserTrackRating.user_id == user_id,
                    UserTrackRating.track_id == track_id,
                )
            ).first()
            if exists is None:
                connection.execute(
                    UserTrackRating.__table__.insert().values(
                        user_id=user_id, track_id=track_id, rating=rating
                    )
                )
            else:
                connection.execute(
                    update(UserTrackRating)
                    .where(
                        UserTrackRating.user_id == user_id,
                        UserTrackRating.track_id == track_id,
                    )
                    .values(rating=rating, updated_at=func.current_timestamp())
                )
            return True

    def set_track_note(self, user_id: int, track_id: int, note: str | None) -> bool:
        if note is not None and len(note) > 1000:
            raise ValueError("Notes are limited to 1000 characters.")
        with self.engine.begin() as connection:
            if connection.execute(
                select(LibraryEntry.track_id).where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first() is None:
                return False
            current = connection.execute(
                select(UserTrackNote.track_id).where(
                    UserTrackNote.user_id == user_id,
                    UserTrackNote.track_id == track_id,
                )
            ).first()
            if note is None:
                if current is not None:
                    connection.execute(
                        UserTrackNote.__table__.delete().where(
                            UserTrackNote.user_id == user_id,
                            UserTrackNote.track_id == track_id,
                        )
                    )
                return True
            if current is None:
                connection.execute(
                    UserTrackNote.__table__.insert().values(
                        user_id=user_id, track_id=track_id, note=note
                    )
                )
            else:
                connection.execute(
                    update(UserTrackNote)
                    .where(
                        UserTrackNote.user_id == user_id,
                        UserTrackNote.track_id == track_id,
                    )
                    .values(note=note, updated_at=func.current_timestamp())
                )
            return True

    def update_track_metadata(
        self, user_id: int, track_id: int, title: str, performer: str | None
    ) -> bool:
        normalized = title.strip()
        if not normalized:
            raise ValueError("Track title cannot be empty.")
        with self.engine.begin() as connection:
            if connection.execute(
                select(LibraryEntry.track_id).where(
                    LibraryEntry.user_id == user_id,
                    LibraryEntry.track_id == track_id,
                )
            ).first() is None:
                return False
            existing = connection.execute(
                select(UserTrackMetadata.track_id).where(
                    UserTrackMetadata.user_id == user_id,
                    UserTrackMetadata.track_id == track_id,
                )
            ).first()
            values = {
                "title": normalized,
                "performer": performer.strip() if performer and performer.strip() else None,
                "updated_at": func.current_timestamp(),
            }
            if existing is None:
                connection.execute(
                    UserTrackMetadata.__table__.insert().values(
                        user_id=user_id,
                        track_id=track_id,
                        **values,
                    )
                )
            else:
                connection.execute(
                    update(UserTrackMetadata)
                    .where(
                        UserTrackMetadata.user_id == user_id,
                        UserTrackMetadata.track_id == track_id,
                    )
                    .values(**values)
                )
            return True

    def get_delivery_mode(self, user_id: int) -> str:
        with self.engine.connect() as connection:
            mode = connection.execute(
                select(UserPreference.delivery_mode).where(
                    UserPreference.user_id == user_id
                )
            ).scalar_one_or_none()
            return mode or "copy"

    def set_delivery_mode(self, user_id: int, mode: str) -> bool:
        if mode not in {"copy", "forward"}:
            raise ValueError("Delivery mode must be 'copy' or 'forward'.")
        with self.engine.begin() as connection:
            self._ensure_user(connection, user_id)
            existing = connection.execute(
                select(UserPreference.user_id).where(
                    UserPreference.user_id == user_id
                )
            ).first()
            if existing is None:
                connection.execute(
                    UserPreference.__table__.insert().values(
                        user_id=user_id, delivery_mode=mode
                    )
                )
            else:
                connection.execute(
                    update(UserPreference)
                    .where(UserPreference.user_id == user_id)
                    .values(delivery_mode=mode)
                )
            return True
