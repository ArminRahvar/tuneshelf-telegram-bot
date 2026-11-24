from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    column,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Artist(Base):
    __tablename__ = "artists"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_artists_name_nonempty"),
        Index("ix_artists_name_lower", func.lower(column("name"))),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    musicbrainz_artist_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_albums_title_nonempty"),
        CheckConstraint(
            "release_year IS NULL OR release_year BETWEEN 1000 AND 9999",
            name="ck_albums_release_year",
        ),
        Index("ix_albums_musicbrainz_release_id", "musicbrainz_release_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    release_year: Mapped[int | None] = mapped_column(Integer)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Genre(Base):
    __tablename__ = "genres"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_genres_name_nonempty"),
        Index("ix_genres_name_lower", func.lower(column("name"))),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_genre_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("genres.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class CatalogTrack(Base):
    __tablename__ = "catalog_tracks"
    __table_args__ = (
        CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_catalog_tracks_title_nonempty",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_catalog_tracks_duration",
        ),
        CheckConstraint(
            "release_year IS NULL OR release_year BETWEEN 1000 AND 9999",
            name="ck_catalog_tracks_release_year",
        ),
        Index("ix_catalog_tracks_isrc", "isrc"),
        Index("ix_catalog_tracks_musicbrainz_recording_id", "musicbrainz_recording_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    album_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("albums.id", ondelete="SET NULL")
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    release_date: Mapped[date | None] = mapped_column(Date)
    release_year: Mapped[int | None] = mapped_column(Integer)
    isrc: Mapped[str | None] = mapped_column(String(12))
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(36))
    cover_art_url: Mapped[str | None] = mapped_column(String(2048))
    source_provider: Mapped[str | None] = mapped_column(String(64))
    source_identifier: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class CatalogTrackArtist(Base):
    __tablename__ = "catalog_track_artists"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_track_artist_position"),
        UniqueConstraint("track_id", "position", name="uq_track_artist_position"),
    )

    track_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("catalog_tracks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    artist_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("artists.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CatalogTrackGenre(Base):
    __tablename__ = "catalog_track_genres"

    track_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("catalog_tracks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    genre_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("genres.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class ArchiveAsset(Base):
    __tablename__ = "archive_assets"
    __table_args__ = (
        UniqueConstraint(
            "archive_chat_id",
            "archive_message_id",
            name="uq_archive_asset_message",
        ),
        UniqueConstraint(
            "archive_chat_id",
            "telegram_file_unique_id",
            name="uq_archive_asset_file",
        ),
        CheckConstraint(
            "archive_message_id > 0",
            name="ck_archive_asset_message_id",
        ),
        Index("ix_archive_assets_track_available", "track_id", "is_available"),
        Index("ix_archive_assets_file_unique_id", "telegram_file_unique_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    track_id: Mapped[int | None] = mapped_column(
        ID_TYPE,
        ForeignKey("catalog_tracks.id", ondelete="CASCADE"),
        nullable=True,
    )
    archive_chat_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    archive_message_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class LibraryEntry(Base):
    __tablename__ = "library_entries"

    user_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    track_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("catalog_tracks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class TelegramSourceReference(Base):
    __tablename__ = "telegram_source_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_source_reference_library_entry",
        ),
        UniqueConstraint(
            "user_id",
            "track_id",
            "source_chat_id",
            "source_message_id",
            name="uq_source_reference_message",
        ),
        CheckConstraint(
            "source_message_id > 0",
            name="ck_source_reference_message_id",
        ),
        Index(
            "ix_source_references_file_unique_id",
            "telegram_file_unique_id",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    track_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    source_chat_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    source_message_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    original_filename: Mapped[str | None] = mapped_column(Text)
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class UserPlaylist(Base):
    __tablename__ = "user_playlists"
    __table_args__ = (
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 64",
            name="ck_user_playlist_name_length",
        ),
        UniqueConstraint("id", "user_id", name="uq_user_playlist_id_owner"),
        Index(
            "uq_user_playlist_name_lower",
            "user_id",
            func.lower(column("name")),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["playlist_id", "user_id"],
            ["user_playlists.id", "user_playlists.user_id"],
            ondelete="CASCADE",
            name="fk_playlist_item_playlist_owner",
        ),
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_playlist_item_library_entry",
        ),
        UniqueConstraint(
            "playlist_id",
            "position",
            name="uq_playlist_item_position",
        ),
        CheckConstraint("position >= 0", name="ck_playlist_item_position"),
    )

    playlist_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    track_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    user_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class UserTrackNote(Base):
    __tablename__ = "user_track_notes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_user_track_note_library_entry",
        ),
        CheckConstraint("length(note) <= 1000", name="ck_user_track_note_length"),
    )

    user_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    track_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class UserTrackMetadata(Base):
    __tablename__ = "user_track_metadata"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_user_track_metadata_library_entry",
        ),
        CheckConstraint(
            "length(trim(title)) > 0",
            name="ck_user_track_metadata_title_nonempty",
        ),
    )

    user_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    track_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    performer: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class UserTrackRating(Base):
    __tablename__ = "user_track_ratings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_user_track_rating_library_entry",
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_user_track_rating_range"),
    )

    user_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    track_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class UserTag(Base):
    __tablename__ = "user_tags"
    __table_args__ = (
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 32",
            name="ck_user_tag_name_length",
        ),
        UniqueConstraint("id", "user_id", name="uq_user_tag_id_owner"),
        Index(
            "uq_user_tag_name_lower",
            "user_id",
            func.lower(column("name")),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class UserTrackTag(Base):
    __tablename__ = "user_track_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "track_id"],
            ["library_entries.user_id", "library_entries.track_id"],
            ondelete="CASCADE",
            name="fk_user_track_tag_library_entry",
        ),
        ForeignKeyConstraint(
            ["tag_id", "user_id"],
            ["user_tags.id", "user_tags.user_id"],
            ondelete="CASCADE",
            name="fk_user_track_tag_tag_owner",
        ),
    )

    user_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    track_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    tag_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "delivery_mode IN ('copy', 'forward')",
            name="ck_user_preference_delivery_mode",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    delivery_mode: Mapped[str] = mapped_column(String(16), nullable=False)


class DownloadJob(Base):
    __tablename__ = "download_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'converting', 'uploading', "
            "'completed', 'failed', 'cancelled')",
            name="ck_download_jobs_status",
        ),
        CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="ck_download_jobs_progress",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_download_jobs_attempt_count",
        ),
        Index("ix_download_jobs_status_created", "status", "created_at"),
        Index("ix_download_jobs_user_status", "user_id", "status"),
        Index("ix_download_jobs_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    track_id: Mapped[int | None] = mapped_column(
        ID_TYPE,
        ForeignKey("catalog_tracks.id", ondelete="SET NULL"),
    )
    batch_id: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class LegacyImportMap(Base):
    __tablename__ = "legacy_import_maps"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('playlist', 'track', 'metadata_track', 'tag')",
            name="ck_legacy_import_map_entity_type",
        ),
        CheckConstraint(
            "length(trim(source_key)) BETWEEN 1 AND 128",
            name="ck_legacy_import_map_source_key",
        ),
        CheckConstraint("legacy_id > 0", name="ck_legacy_import_map_legacy_id"),
        CheckConstraint("target_id > 0", name="ck_legacy_import_map_target_id"),
        Index("ix_legacy_import_maps_target", "entity_type", "target_id"),
    )

    source_key: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("legacy_import_sources.source_key", ondelete="RESTRICT"),
        primary_key=True,
    )
    entity_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    legacy_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    target_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class LegacyImportSource(Base):
    __tablename__ = "legacy_import_sources"
    __table_args__ = (
        CheckConstraint(
            "length(trim(source_key)) BETWEEN 1 AND 128",
            name="ck_legacy_import_source_key",
        ),
        CheckConstraint(
            "length(fingerprint) = 64",
            name="ck_legacy_import_source_fingerprint",
        ),
        CheckConstraint(
            "source_dialect IN ('sqlite', 'postgresql')",
            name="ck_legacy_import_source_dialect",
        ),
    )

    source_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_dialect: Mapped[str] = mapped_column(String(16), nullable=False)
    sqlite_schema_version: Mapped[int | None] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
