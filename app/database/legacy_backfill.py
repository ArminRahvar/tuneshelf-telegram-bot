import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from app.database.engine import create_database_engine
from app.database.models import (
    Album,
    ArchiveAsset,
    Artist,
    CatalogTrack,
    CatalogTrackArtist,
    LegacyImportMap,
    LegacyImportSource,
    LibraryEntry,
    PlaylistItem,
    TelegramSourceReference,
    User,
    UserPlaylist,
    UserPreference,
    UserTag,
    UserTrackNote,
    UserTrackRating,
    UserTrackTag,
)


REQUIRED_SOURCE_TABLES = {"playlists", "tracks"}
OPTIONAL_SOURCE_TABLES = {
    "metadata_only_tracks",
    "tags",
    "telegram_message_references",
    "track_tags",
    "user_settings",
    "users",
}
SOURCE_COLUMNS = {
    "playlists": {"id", "user_id", "name", "created_at"},
    "tracks": {
        "id",
        "playlist_id",
        "source_chat_id",
        "source_message_id",
        "title",
        "media_type",
        "added_at",
    },
    "metadata_only_tracks": {"id", "owner_user_id", "title", "created_at"},
    "tags": {"id", "owner_user_id", "name", "created_at"},
    "telegram_message_references": {
        "track_id",
        "owner_user_id",
        "source_chat_id",
        "source_message_id",
        "media_type",
        "created_at",
        "last_verified_at",
    },
    "track_tags": {"track_id", "tag_id"},
    "user_settings": {"user_id", "delivery_mode"},
    "users": {"telegram_user_id", "created_at"},
}
TARGET_TABLES = {
    "albums",
    "artists",
    "catalog_track_artists",
    "catalog_tracks",
    "legacy_import_maps",
    "legacy_import_sources",
    "library_entries",
    "playlist_items",
    "telegram_source_references",
    "user_playlists",
    "user_preferences",
    "user_track_notes",
    "user_track_ratings",
    "user_track_tags",
    "user_tags",
    "users",
}


@dataclass(frozen=True)
class LegacySnapshot:
    dialect: str
    sqlite_schema_version: int | None
    tables: dict[str, list[dict[str, Any]]]


def _read_only_source_engine(url: str) -> Engine:
    parsed_url = make_url(url)
    if parsed_url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("The legacy source must be SQLite or PostgreSQL.")
    if (
        parsed_url.get_backend_name() == "sqlite"
        and parsed_url.database not in (None, "", ":memory:")
        and not Path(parsed_url.database).is_file()
    ):
        raise FileNotFoundError("The SQLite source database does not exist.")
    return create_database_engine(url, poolclass=NullPool)


def _source_table_rows(connection: Connection, table_name: str) -> list[dict[str, Any]]:
    columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
    if "id" in columns:
        order_columns = ["id"]
    elif {"track_id", "tag_id"} <= columns:
        order_columns = ["track_id", "tag_id"]
    elif "telegram_user_id" in columns:
        order_columns = ["telegram_user_id"]
    elif "user_id" in columns:
        order_columns = ["user_id"]
    else:
        order_columns = []
    query = f'SELECT * FROM "{table_name}"'
    if order_columns:
        query += " ORDER BY " + ", ".join(f'"{name}"' for name in order_columns)
    return [dict(row) for row in connection.execute(text(query)).mappings()]


def read_legacy_snapshot(url: str) -> LegacySnapshot:
    engine = _read_only_source_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA query_only = ON")
                    schema_version = int(
                        connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                    )
                else:
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    schema_version = None

                existing_tables = set(inspect(connection).get_table_names())
                missing = REQUIRED_SOURCE_TABLES - existing_tables
                if missing:
                    names = ", ".join(sorted(missing))
                    raise ValueError(
                        f"Legacy database is missing required tables: {names}."
                    )

                table_names = REQUIRED_SOURCE_TABLES | (
                    OPTIONAL_SOURCE_TABLES & existing_tables
                )
                for table_name in sorted(table_names):
                    available_columns = {
                        column["name"]
                        for column in inspect(connection).get_columns(table_name)
                    }
                    missing_columns = SOURCE_COLUMNS[table_name] - available_columns
                    if missing_columns:
                        names = ", ".join(sorted(missing_columns))
                        raise ValueError(
                            f"Legacy table {table_name} is missing required columns: "
                            f"{names}."
                        )
                rows = {
                    name: _source_table_rows(connection, name)
                    for name in sorted(table_names)
                }
                snapshot = LegacySnapshot(
                    dialect=connection.dialect.name,
                    sqlite_schema_version=schema_version,
                    tables=rows,
                )
                transaction.commit()
                return snapshot
            except Exception:
                transaction.rollback()
                raise
    finally:
        engine.dispose()


def _value(row: dict[str, Any], key: str, default: Any = None) -> Any:
    return row.get(key, default)


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Legacy timestamp values must be datetime strings or values.")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_timestamp(row: dict[str, Any], key: str, description: str) -> None:
    if _datetime(_value(row, key)) is None:
        raise ValueError(f"{description} has no valid {key} timestamp.")


def _fingerprint(snapshot: LegacySnapshot) -> str:
    payload = {
        "dialect": snapshot.dialect,
        "sqlite_schema_version": snapshot.sqlite_schema_version,
        "tables": snapshot.tables,
    }
    encoded = json.dumps(
        payload,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_snapshot(snapshot: LegacySnapshot) -> dict[str, Any]:
    tables = snapshot.tables
    playlists = tables["playlists"]
    tracks = tables["tracks"]
    playlists_by_id: dict[int, dict[str, Any]] = {}
    tracks_by_id: dict[int, dict[str, Any]] = {}
    user_ids: set[int] = set()
    playlist_names: set[tuple[int, str]] = set()
    tag_names: set[tuple[int, str]] = set()

    for row in tables.get("users", []):
        user_id = int(row["telegram_user_id"])
        user_ids.add(user_id)
        _require_timestamp(row, "created_at", f"Legacy user {user_id}")

    for row in playlists:
        playlist_id = int(row["id"])
        user_id = int(row["user_id"])
        name = str(row["name"])
        if playlist_id <= 0 or not name.strip() or len(name) > 64:
            raise ValueError(f"Legacy playlist {playlist_id} cannot fit the target schema.")
        name_key = (user_id, name.casefold())
        if name_key in playlist_names:
            raise ValueError(
                f"Legacy playlist names collide case-insensitively for owner {user_id}."
            )
        playlist_names.add(name_key)
        playlists_by_id[playlist_id] = row
        user_ids.add(user_id)
        _require_timestamp(row, "created_at", f"Legacy playlist {playlist_id}")

    for row in tracks:
        track_id = int(row["id"])
        playlist_id = int(row["playlist_id"])
        title = str(row["title"])
        if track_id <= 0 or playlist_id not in playlists_by_id:
            raise ValueError(f"Legacy track {track_id} has no valid playlist.")
        if not title.strip():
            raise ValueError(f"Legacy track {track_id} has an empty title.")
        if int(row["source_message_id"]) <= 0:
            raise ValueError(f"Legacy track {track_id} has an invalid source message ID.")
        if len(str(row["media_type"])) > 32:
            raise ValueError(f"Legacy track {track_id} has an unsupported media type.")
        duration = _value(row, "duration_seconds")
        if duration is not None and int(duration) < 0:
            raise ValueError(f"Legacy track {track_id} has a negative duration.")
        year = _value(row, "release_year")
        if year is not None and not 1000 <= int(year) <= 9999:
            raise ValueError(f"Legacy track {track_id} has an invalid release year.")
        rating = _value(row, "rating")
        if rating is not None and not 1 <= int(rating) <= 5:
            raise ValueError(f"Legacy track {track_id} has an invalid rating.")
        note = _value(row, "note")
        if note is not None and len(str(note)) > 1000:
            raise ValueError(f"Legacy track {track_id} has a note longer than 1000 chars.")
        if len(str(_value(row, "mime_type", "") or "")) > 255:
            raise ValueError(f"Legacy track {track_id} has an oversized MIME type.")
        recording_id = _value(row, "musicbrainz_recording_id")
        if recording_id is not None and len(str(recording_id)) > 36:
            raise ValueError(
                f"Legacy track {track_id} has an oversized MusicBrainz ID."
            )
        tracks_by_id[track_id] = row
        user_ids.add(int(playlists_by_id[playlist_id]["user_id"]))
        _require_timestamp(row, "added_at", f"Legacy track {track_id}")

    for row in tables.get("metadata_only_tracks", []):
        track_id = int(row["id"])
        if track_id <= 0 or not str(row["title"]).strip():
            raise ValueError(f"Legacy metadata-only track {track_id} has an invalid title.")
        user_ids.add(int(row["owner_user_id"]))
        year = _value(row, "release_year")
        if year is not None and not 1000 <= int(year) <= 9999:
            raise ValueError(
                f"Legacy metadata-only track {track_id} has an invalid release year."
            )
        recording_id = _value(row, "musicbrainz_recording_id")
        provider_url = _value(row, "provider_url")
        if recording_id is not None and len(str(recording_id)) > 36:
            raise ValueError(
                f"Legacy metadata-only track {track_id} has an oversized MusicBrainz ID."
            )
        if provider_url is not None and len(str(provider_url)) > 255:
            raise ValueError(
                f"Legacy metadata-only track {track_id} has an oversized provider URL."
            )
        _require_timestamp(row, "created_at", f"Metadata-only track {track_id}")

    tag_owners: dict[int, int] = {}
    for row in tables.get("tags", []):
        tag_id = int(row["id"])
        user_id = int(row["owner_user_id"])
        name = str(row["name"])
        if tag_id <= 0 or not name.strip() or len(name) > 32:
            raise ValueError(f"Legacy tag {tag_id} cannot fit the target schema.")
        name_key = (user_id, name.casefold())
        if name_key in tag_names:
            raise ValueError(
                f"Legacy tag names collide case-insensitively for owner {user_id}."
            )
        tag_names.add(name_key)
        tag_owners[tag_id] = user_id
        user_ids.add(user_id)
        _require_timestamp(row, "created_at", f"Legacy tag {tag_id}")

    for row in tables.get("track_tags", []):
        track_id = int(row["track_id"])
        tag_id = int(row["tag_id"])
        if track_id not in tracks_by_id or tag_id not in tag_owners:
            raise ValueError("Legacy track-tag association refers to a missing row.")
        owner_id = int(playlists_by_id[int(tracks_by_id[track_id]["playlist_id"])]["user_id"])
        if tag_owners[tag_id] != owner_id:
            raise ValueError("Legacy track-tag association has a different owner.")

    for row in tables.get("telegram_message_references", []):
        track_id = int(row["track_id"])
        if track_id not in tracks_by_id:
            raise ValueError("Legacy Telegram reference points to a missing track.")
        owner_id = int(playlists_by_id[int(tracks_by_id[track_id]["playlist_id"])]["user_id"])
        if int(row["owner_user_id"]) != owner_id:
            raise ValueError("Legacy Telegram reference has a different owner.")
        if int(row["source_message_id"]) <= 0:
            raise ValueError("Legacy Telegram reference has an invalid message ID.")
        if len(str(row["media_type"])) > 32:
            raise ValueError("Legacy Telegram reference has an unsupported media type.")
        if len(str(_value(row, "mime_type", "") or "")) > 255:
            raise ValueError("Legacy Telegram reference has an oversized MIME type.")
        user_ids.add(owner_id)
        _require_timestamp(row, "created_at", "Legacy Telegram reference")
        _datetime(_value(row, "last_verified_at"))

    for row in tables.get("user_settings", []):
        if row["delivery_mode"] not in {"copy", "forward"}:
            raise ValueError("Legacy user settings contain an unsupported delivery mode.")
        user_ids.add(int(row["user_id"]))

    return {
        "playlists_by_id": playlists_by_id,
        "tracks_by_id": tracks_by_id,
        "tag_owners": tag_owners,
        "user_ids": user_ids,
    }


def _target_source_record(
    connection: Connection,
    source_key: str,
) -> dict[str, Any] | None:
    record = connection.execute(
        select(
            LegacyImportSource.source_key,
            LegacyImportSource.fingerprint,
            LegacyImportSource.source_dialect,
        ).where(LegacyImportSource.source_key == source_key)
    ).mappings().one_or_none()
    return dict(record) if record is not None else None


def _ensure_target_schema(connection: Connection) -> None:
    missing = TARGET_TABLES - set(inspect(connection).get_table_names())
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(
            f"Target schema is incomplete; run Alembic upgrade first. Missing: {names}."
        )


def _mapped_id(
    connection: Connection,
    source_key: str,
    entity_type: str,
    legacy_id: int,
) -> int | None:
    return connection.execute(
        select(LegacyImportMap.target_id).where(
            LegacyImportMap.source_key == source_key,
            LegacyImportMap.entity_type == entity_type,
            LegacyImportMap.legacy_id == legacy_id,
        )
    ).scalar_one_or_none()


def _record_mapping(
    connection: Connection,
    source_key: str,
    entity_type: str,
    legacy_id: int,
    target_id: int,
) -> None:
    connection.execute(
        LegacyImportMap.__table__.insert().values(
            source_key=source_key,
            entity_type=entity_type,
            legacy_id=legacy_id,
            target_id=target_id,
        )
    )


def _insert_id(connection: Connection, table: Any, values: dict[str, Any]) -> int:
    result = connection.execute(table.insert().values(**values))
    return int(result.inserted_primary_key[0])


def _ensure_user(
    connection: Connection,
    user_id: int,
    created_at: Any = None,
) -> None:
    exists = connection.execute(
        select(User.telegram_user_id).where(User.telegram_user_id == user_id)
    ).scalar_one_or_none()
    if exists is None:
        values: dict[str, Any] = {"telegram_user_id": user_id}
        if created_at is not None:
            values["created_at"] = _datetime(created_at)
        connection.execute(User.__table__.insert().values(**values))


def _ensure_preference(connection: Connection, row: dict[str, Any]) -> None:
    user_id = int(row["user_id"])
    delivery_mode = str(row["delivery_mode"])
    current = connection.execute(
        select(UserPreference.delivery_mode).where(UserPreference.user_id == user_id)
    ).scalar_one_or_none()
    if current is None:
        connection.execute(
            UserPreference.__table__.insert().values(
                user_id=user_id,
                delivery_mode=delivery_mode,
            )
        )
    elif current != delivery_mode:
        raise ValueError(
            f"Target user {user_id} already has a conflicting delivery preference."
        )


def _ensure_tag(connection: Connection, user_id: int, name: str, created_at: Any) -> int:
    tag_id = connection.execute(
        select(UserTag.id).where(
            UserTag.user_id == user_id,
            func.lower(UserTag.name) == name.lower(),
        )
    ).scalar_one_or_none()
    if tag_id is not None:
        return int(tag_id)
    values: dict[str, Any] = {"user_id": user_id, "name": name}
    if created_at is not None:
        values["created_at"] = _datetime(created_at)
    return _insert_id(connection, UserTag.__table__, values)


def _ensure_annotation(
    connection: Connection,
    table: Any,
    user_id: int,
    track_id: int,
    column_name: str,
    value: Any,
) -> None:
    if value is None:
        return
    target = table.c[column_name]
    current = connection.execute(
        select(target).where(
            table.c.user_id == user_id,
            table.c.track_id == track_id,
        )
    ).scalar_one_or_none()
    if current is None:
        connection.execute(
            table.insert().values(
                user_id=user_id,
                track_id=track_id,
                **{column_name: value},
            )
        )
    elif current != value:
        raise ValueError(
            f"Target annotation conflicts for owner {user_id}, track {track_id}."
        )


def _source_references(
    tables: dict[str, list[dict[str, Any]]],
    track: dict[str, Any],
) -> list[dict[str, Any]]:
    track_id = int(track["id"])
    references: dict[tuple[int, int], dict[str, Any]] = {}
    base = {
        "source_chat_id": int(track["source_chat_id"]),
        "source_message_id": int(track["source_message_id"]),
        "telegram_file_id": _value(track, "telegram_file_id"),
        "telegram_file_unique_id": _value(track, "file_unique_id"),
        "media_type": track["media_type"],
        "mime_type": _value(track, "mime_type"),
        "original_filename": _value(track, "original_filename"),
        "is_available": True,
        "created_at": _value(track, "added_at"),
        "last_verified_at": None,
    }
    base_key = (base["source_chat_id"], base["source_message_id"])
    references[base_key] = base

    for row in tables.get("telegram_message_references", []):
        if int(row["track_id"]) != track_id:
            continue
        reference = {
            "source_chat_id": int(row["source_chat_id"]),
            "source_message_id": int(row["source_message_id"]),
            "telegram_file_id": _value(row, "telegram_file_id"),
            "telegram_file_unique_id": _value(row, "telegram_file_unique_id")
            or _value(track, "file_unique_id"),
            "media_type": _value(row, "media_type", track["media_type"]),
            "mime_type": _value(row, "mime_type") or _value(track, "mime_type"),
            "original_filename": _value(row, "original_filename")
            or _value(track, "original_filename"),
            "is_available": bool(_value(row, "is_available", True)),
            "created_at": _value(row, "created_at", _value(track, "added_at")),
            "last_verified_at": _value(row, "last_verified_at"),
        }
        key = (reference["source_chat_id"], reference["source_message_id"])
        if key == base_key:
            reference["telegram_file_id"] = (
                reference["telegram_file_id"] or _value(track, "telegram_file_id")
            )
            reference["original_filename"] = (
                reference["original_filename"]
                or _value(track, "original_filename")
            )
        references[key] = reference
    return [references[key] for key in sorted(references)]


def _import_track(
    connection: Connection,
    source_key: str,
    row: dict[str, Any],
    user_id: int,
    references: list[dict[str, Any]],
    tag_ids: list[int],
) -> int:
    legacy_id = int(row["id"])
    mapped_id = _mapped_id(connection, source_key, "track", legacy_id)
    if mapped_id is not None:
        return mapped_id

    album_id = None
    release_title = _value(row, "release_title")
    if release_title and str(release_title).strip():
        album_id = _insert_id(
            connection,
            Album.__table__,
            {
                "title": str(release_title),
                "release_year": _value(row, "release_year"),
            },
        )

    track_id = _insert_id(
        connection,
        CatalogTrack.__table__,
        {
            "title": str(row["title"]),
            "album_id": album_id,
            "duration_seconds": _value(row, "duration_seconds"),
            "release_year": _value(row, "release_year"),
            "musicbrainz_recording_id": _value(row, "musicbrainz_recording_id"),
        },
    )
    performer = _value(row, "performer")
    if performer and str(performer).strip():
        artist_id = _insert_id(
            connection,
            Artist.__table__,
            {"name": str(performer)},
        )
        connection.execute(
            CatalogTrackArtist.__table__.insert().values(
                track_id=track_id,
                artist_id=artist_id,
                position=0,
            )
        )

    connection.execute(
        LibraryEntry.__table__.insert().values(
            user_id=user_id,
            track_id=track_id,
            added_at=_datetime(_value(row, "added_at")),
        )
    )
    rating = _value(row, "rating")
    _ensure_annotation(
        connection,
        UserTrackRating.__table__,
        user_id,
        track_id,
        "rating",
        int(rating) if rating is not None else None,
    )
    _ensure_annotation(
        connection,
        UserTrackNote.__table__,
        user_id,
        track_id,
        "note",
        _value(row, "note"),
    )

    for reference in references:
        connection.execute(
            TelegramSourceReference.__table__.insert().values(
                user_id=user_id,
                track_id=track_id,
                source_chat_id=reference["source_chat_id"],
                source_message_id=reference["source_message_id"],
                telegram_file_id=reference["telegram_file_id"],
                telegram_file_unique_id=reference["telegram_file_unique_id"],
                media_type=reference["media_type"],
                mime_type=reference["mime_type"],
                original_filename=reference["original_filename"],
                is_available=reference["is_available"],
                created_at=_datetime(reference["created_at"]),
                last_verified_at=_datetime(reference["last_verified_at"]),
            )
        )

    for tag_id in tag_ids:
        connection.execute(
            UserTrackTag.__table__.insert().values(
                user_id=user_id,
                track_id=track_id,
                tag_id=tag_id,
            )
        )

    _record_mapping(connection, source_key, "track", legacy_id, track_id)
    return track_id


def _link_unambiguous_archive_assets(connection: Connection) -> None:
    candidates = (
        select(
            TelegramSourceReference.telegram_file_unique_id.label("file_unique_id"),
            func.min(TelegramSourceReference.track_id).label("track_id"),
        )
        .where(TelegramSourceReference.telegram_file_unique_id.is_not(None))
        .group_by(TelegramSourceReference.telegram_file_unique_id)
        .having(func.count(func.distinct(TelegramSourceReference.track_id)) == 1)
        .subquery()
    )
    assets = connection.execute(
        select(ArchiveAsset.id, candidates.c.track_id).join(
            candidates,
            candidates.c.file_unique_id == ArchiveAsset.telegram_file_unique_id,
        ).where(ArchiveAsset.track_id.is_(None))
    )
    for asset_id, track_id in assets:
        connection.execute(
            update(ArchiveAsset)
            .where(ArchiveAsset.id == asset_id, ArchiveAsset.track_id.is_(None))
            .values(track_id=track_id)
        )


def _import_snapshot(
    connection: Connection,
    source_key: str,
    snapshot: LegacySnapshot,
    validated: dict[str, Any],
    fingerprint: str,
) -> dict[str, int]:
    tables = snapshot.tables
    counts: defaultdict[str, int] = defaultdict(int)
    connection.execute(
        LegacyImportSource.__table__.insert().values(
            source_key=source_key,
            fingerprint=fingerprint,
            source_dialect=snapshot.dialect,
            sqlite_schema_version=snapshot.sqlite_schema_version,
        )
    )

    users_by_id = {
        int(row["telegram_user_id"]): row for row in tables.get("users", [])
    }
    for user_id in sorted(validated["user_ids"]):
        user = users_by_id.get(user_id, {})
        _ensure_user(connection, user_id, _value(user, "created_at"))
        counts["users_checked"] += 1

    for row in tables.get("user_settings", []):
        _ensure_preference(connection, row)
        counts["preferences_imported"] += 1

    tag_target_ids: dict[int, int] = {}
    for tag in tables.get("tags", []):
        legacy_tag_id = int(tag["id"])
        target_tag_id = _mapped_id(
            connection,
            source_key,
            "tag",
            legacy_tag_id,
        )
        if target_tag_id is None:
            target_tag_id = _ensure_tag(
                connection,
                int(tag["owner_user_id"]),
                str(tag["name"]),
                _value(tag, "created_at"),
            )
            _record_mapping(
                connection,
                source_key,
                "tag",
                legacy_tag_id,
                target_tag_id,
            )
            counts["tags_imported"] += 1
        tag_target_ids[legacy_tag_id] = target_tag_id

    for playlist in tables["playlists"]:
        legacy_id = int(playlist["id"])
        mapped_id = _mapped_id(connection, source_key, "playlist", legacy_id)
        if mapped_id is None:
            target_id = _insert_id(
                connection,
                UserPlaylist.__table__,
                {
                    "user_id": int(playlist["user_id"]),
                    "name": str(playlist["name"]),
                    "created_at": _datetime(_value(playlist, "created_at")),
                },
            )
            _record_mapping(connection, source_key, "playlist", legacy_id, target_id)
            counts["playlists_imported"] += 1

    tags_by_track: defaultdict[int, list[int]] = defaultdict(list)
    for association in tables.get("track_tags", []):
        track_id = int(association["track_id"])
        tag_id = int(association["tag_id"])
        tags_by_track[track_id].append(tag_target_ids[tag_id])

    track_playlist_positions: defaultdict[int, int] = defaultdict(int)
    for track in tables["tracks"]:
        playlist = validated["playlists_by_id"][int(track["playlist_id"])]
        user_id = int(playlist["user_id"])
        target_playlist_id = _mapped_id(
            connection,
            source_key,
            "playlist",
            int(playlist["id"]),
        )
        if target_playlist_id is None:
            raise RuntimeError("Legacy playlist mapping was not created.")
        target_track_id = _import_track(
            connection,
            source_key,
            track,
            user_id,
            _source_references(tables, track),
            tags_by_track[int(track["id"])],
        )
        counts["tracks_imported"] += 1
        connection.execute(
            PlaylistItem.__table__.insert().values(
                playlist_id=target_playlist_id,
                track_id=target_track_id,
                user_id=user_id,
                position=track_playlist_positions[target_playlist_id],
                added_at=_datetime(_value(track, "added_at")),
            )
        )
        track_playlist_positions[target_playlist_id] += 1

    for row in tables.get("metadata_only_tracks", []):
        legacy_id = int(row["id"])
        target_id = _mapped_id(connection, source_key, "metadata_track", legacy_id)
        if target_id is None:
            album_id = None
            release_title = _value(row, "release_title")
            if release_title and str(release_title).strip():
                album_id = _insert_id(
                    connection,
                    Album.__table__,
                    {
                        "title": str(release_title),
                        "release_year": _value(row, "release_year"),
                    },
                )
            target_id = _insert_id(
                connection,
                CatalogTrack.__table__,
                {
                    "title": str(row["title"]),
                    "album_id": album_id,
                    "release_year": _value(row, "release_year"),
                    "musicbrainz_recording_id": _value(
                        row, "musicbrainz_recording_id"
                    ),
                    "source_provider": "legacy_metadata",
                    "source_identifier": _value(row, "provider_url"),
                },
            )
            performer = _value(row, "performer")
            if performer and str(performer).strip():
                artist_id = _insert_id(
                    connection,
                    Artist.__table__,
                    {"name": str(performer)},
                )
                connection.execute(
                    CatalogTrackArtist.__table__.insert().values(
                        track_id=target_id,
                        artist_id=artist_id,
                        position=0,
                    )
                )
            user_id = int(row["owner_user_id"])
            connection.execute(
                LibraryEntry.__table__.insert().values(
                    user_id=user_id,
                    track_id=target_id,
                    added_at=_datetime(_value(row, "created_at")),
                )
            )
            _record_mapping(
                connection,
                source_key,
                "metadata_track",
                legacy_id,
                target_id,
            )
            counts["metadata_tracks_imported"] += 1

    _link_unambiguous_archive_assets(connection)
    return dict(counts)


def _verify_import(
    connection: Connection,
    source_key: str,
    snapshot: LegacySnapshot,
) -> dict[str, int]:
    tables = snapshot.tables
    expected = {
        "playlist": len(tables["playlists"]),
        "track": len(tables["tracks"]),
        "metadata_track": len(tables.get("metadata_only_tracks", [])),
        "tag": len(tables.get("tags", [])),
    }
    mapping_counts: dict[str, int] = {}
    for entity_type, expected_count in expected.items():
        actual = connection.execute(
            select(func.count()).select_from(LegacyImportMap).where(
                LegacyImportMap.source_key == source_key,
                LegacyImportMap.entity_type == entity_type,
            )
        ).scalar_one()
        if actual != expected_count:
            raise RuntimeError(
                f"Post-import verification failed for {entity_type} mappings."
            )
        mapping_counts[f"{entity_type}_mappings"] = int(actual)

    track_map = {
        int(legacy_id): int(target_id)
        for legacy_id, target_id in connection.execute(
            select(LegacyImportMap.legacy_id, LegacyImportMap.target_id).where(
                LegacyImportMap.source_key == source_key,
                LegacyImportMap.entity_type == "track",
            )
        )
    }
    library_count = connection.execute(
        select(func.count())
        .select_from(LibraryEntry)
        .join(
            LegacyImportMap,
            LibraryEntry.track_id == LegacyImportMap.target_id,
        )
        .where(
            LegacyImportMap.source_key == source_key,
            LegacyImportMap.entity_type.in_(("track", "metadata_track")),
        )
    ).scalar_one()
    expected_library_count = expected["track"] + expected["metadata_track"]
    if library_count != expected_library_count:
        raise RuntimeError("Post-import verification failed for library entries.")

    track_count = connection.execute(
        select(func.count())
        .select_from(PlaylistItem)
        .join(
            LegacyImportMap,
            PlaylistItem.playlist_id == LegacyImportMap.target_id,
        )
        .where(
            LegacyImportMap.source_key == source_key,
            LegacyImportMap.entity_type == "playlist",
        )
    ).scalar_one()
    if track_count != len(tables["tracks"]):
        raise RuntimeError("Post-import verification failed for playlist membership.")

    association_count = connection.execute(
        select(func.count())
        .select_from(UserTrackTag)
        .join(
            LegacyImportMap,
            UserTrackTag.track_id == LegacyImportMap.target_id,
        )
        .where(
            LegacyImportMap.source_key == source_key,
            LegacyImportMap.entity_type == "track",
        )
    ).scalar_one()
    expected_association_count = len(tables.get("track_tags", []))
    if association_count != expected_association_count:
        raise RuntimeError("Post-import verification failed for personal tags.")

    playlist_target_ids = {
        int(legacy_id): int(target_id)
        for legacy_id, target_id in connection.execute(
            select(LegacyImportMap.legacy_id, LegacyImportMap.target_id).where(
                LegacyImportMap.source_key == source_key,
                LegacyImportMap.entity_type == "playlist",
            )
        )
    }
    tracks_by_playlist: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for track in tables["tracks"]:
        tracks_by_playlist[int(track["playlist_id"])].append(track)
    for playlist_id, tracks in tracks_by_playlist.items():
        expected_track_ids = [
            track_map[int(track["id"])]
            for track in sorted(tracks, key=lambda row: int(row["id"]))
        ]
        actual_track_ids = list(
            connection.execute(
                select(PlaylistItem.track_id)
                .where(
                    PlaylistItem.playlist_id
                    == playlist_target_ids[playlist_id]
                )
                .order_by(PlaylistItem.position)
            ).scalars()
        )
        if actual_track_ids != expected_track_ids:
            raise RuntimeError("Post-import verification failed for playlist order.")

    reference_count = connection.execute(
        select(func.count())
        .select_from(TelegramSourceReference)
        .join(
            LegacyImportMap,
            TelegramSourceReference.track_id == LegacyImportMap.target_id,
        )
        .where(
            LegacyImportMap.source_key == source_key,
            LegacyImportMap.entity_type == "track",
        )
    ).scalar_one()
    expected_reference_count = sum(
        len(_source_references(tables, row)) for row in tables["tracks"]
    )
    if reference_count != expected_reference_count:
        raise RuntimeError("Post-import verification failed for Telegram references.")

    if connection.dialect.name == "sqlite":
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise RuntimeError("Post-import verification found foreign-key violations.")

    mapping_counts["library_entries"] = int(library_count)
    mapping_counts["playlist_items"] = int(track_count)
    mapping_counts["source_references"] = int(reference_count)
    return mapping_counts


def run_legacy_backfill(
    source_url: str,
    target_url: str,
    source_key: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    if (
        not source_key.strip()
        or source_key != source_key.strip()
        or len(source_key) > 128
    ):
        raise ValueError("source_key must contain 1 to 128 characters.")
    snapshot = read_legacy_snapshot(source_url)
    validated = _validate_snapshot(snapshot)
    fingerprint = _fingerprint(snapshot)
    engine = create_database_engine(target_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            _ensure_target_schema(connection)
            existing = _target_source_record(connection, source_key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError(
                        "The source changed after its first import. Use a new source key "
                        "only after reviewing target conflicts."
                    )
                return {
                    "status": "already_imported",
                    "source_key": source_key,
                    "source_dialect": snapshot.dialect,
                    "fingerprint": fingerprint,
                    "apply": apply,
                    "counts": {},
                }

            if not apply:
                return {
                    "status": "dry_run",
                    "source_key": source_key,
                    "source_dialect": snapshot.dialect,
                    "sqlite_schema_version": snapshot.sqlite_schema_version,
                    "fingerprint": fingerprint,
                    "apply": False,
                    "counts": {
                        "users": len(validated["user_ids"]),
                        "playlists": len(snapshot.tables["playlists"]),
                        "tracks": len(snapshot.tables["tracks"]),
                        "metadata_tracks": len(
                            snapshot.tables.get("metadata_only_tracks", [])
                        ),
                        "source_references": sum(
                            len(_source_references(snapshot.tables, row))
                            for row in snapshot.tables["tracks"]
                        ),
                    },
                }

            connection.commit()
            transaction = connection.begin()
            try:
                counts = _import_snapshot(
                    connection,
                    source_key,
                    snapshot,
                    validated,
                    fingerprint,
                )
                counts["verified"] = _verify_import(
                    connection,
                    source_key,
                    snapshot,
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
            return {
                "status": "applied",
                "source_key": source_key,
                "source_dialect": snapshot.dialect,
                "sqlite_schema_version": snapshot.sqlite_schema_version,
                "fingerprint": fingerprint,
                "apply": True,
                "counts": counts,
            }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill a legacy TuneShelf database into the normalized schema."
    )
    parser.add_argument(
        "--source-key",
        required=True,
        help="Stable non-secret label used to make this import repeatable.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the backfill. Without this flag, only a read-only dry-run is performed.",
    )
    arguments = parser.parse_args()
    source_url = os.environ.get("LEGACY_DATABASE_URL", "").strip()
    target_url = os.environ.get("DATABASE_URL", "").strip()
    if not source_url or not target_url:
        raise SystemExit("Set LEGACY_DATABASE_URL and DATABASE_URL.")
    print(
        json.dumps(
            run_legacy_backfill(
                source_url,
                target_url,
                arguments.source_key,
                apply=arguments.apply,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
