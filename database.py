import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PlaylistNotFoundError(Exception):
    pass


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.migrate()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                connection.executescript(
                    """
                    CREATE TABLE playlists (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL COLLATE NOCASE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, name)
                    );

                    CREATE TABLE tracks (
                        id INTEGER PRIMARY KEY,
                        playlist_id INTEGER NOT NULL REFERENCES playlists(id)
                            ON DELETE CASCADE,
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        performer TEXT,
                        media_type TEXT NOT NULL,
                        file_unique_id TEXT,
                        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX tracks_playlist_id_idx
                        ON tracks(playlist_id, id);
                    PRAGMA user_version = 1;
                    """
                )
                version = 1
            if version < 2:
                connection.executescript(
                    """
                    CREATE TABLE user_states (
                        user_id INTEGER PRIMARY KEY,
                        action TEXT NOT NULL
                    );
                    PRAGMA user_version = 2;
                    """
                )
                version = 2
            if version < 3:
                connection.executescript(
                    """
                    CREATE TABLE pending_tracks (
                        user_id INTEGER NOT NULL,
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        performer TEXT,
                        media_type TEXT NOT NULL,
                        file_unique_id TEXT,
                        PRIMARY KEY(user_id, source_chat_id, source_message_id)
                    );
                    PRAGMA user_version = 3;
                    """
                )
                version = 3
            if version < 4:
                connection.executescript(
                    """
                    DELETE FROM tracks
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM tracks
                        GROUP BY playlist_id, source_chat_id, source_message_id
                    );
                    CREATE UNIQUE INDEX tracks_source_message_unique
                        ON tracks(playlist_id, source_chat_id, source_message_id);
                    PRAGMA user_version = 4;
                    """
                )
                version = 4
            if version < 5:
                connection.executescript(
                    """
                    ALTER TABLE tracks ADD COLUMN release_title TEXT;
                    ALTER TABLE tracks ADD COLUMN musicbrainz_recording_id TEXT;
                    ALTER TABLE pending_tracks ADD COLUMN release_title TEXT;
                    ALTER TABLE pending_tracks ADD COLUMN musicbrainz_recording_id TEXT;
                    PRAGMA user_version = 5;
                    """
                )
                version = 5
            if version < 6:
                connection.executescript(
                    """
                    CREATE TABLE users (
                        telegram_user_id INTEGER PRIMARY KEY,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );

                    INSERT OR IGNORE INTO users (telegram_user_id)
                    SELECT DISTINCT user_id FROM playlists;

                    ALTER TABLE tracks ADD COLUMN telegram_file_id TEXT;
                    ALTER TABLE tracks ADD COLUMN duration_seconds INTEGER;
                    ALTER TABLE tracks ADD COLUMN mime_type TEXT;
                    ALTER TABLE tracks ADD COLUMN original_filename TEXT;
                    ALTER TABLE tracks ADD COLUMN rating INTEGER
                        CHECK (rating IS NULL OR rating BETWEEN 1 AND 5);
                    ALTER TABLE tracks ADD COLUMN note TEXT;

                    ALTER TABLE pending_tracks ADD COLUMN telegram_file_id TEXT;
                    ALTER TABLE pending_tracks ADD COLUMN duration_seconds INTEGER;
                    ALTER TABLE pending_tracks ADD COLUMN mime_type TEXT;
                    ALTER TABLE pending_tracks ADD COLUMN original_filename TEXT;

                    CREATE TABLE telegram_message_references (
                        id INTEGER PRIMARY KEY,
                        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                        owner_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id)
                            ON DELETE CASCADE,
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        telegram_file_id TEXT,
                        telegram_file_unique_id TEXT,
                        media_type TEXT NOT NULL,
                        original_filename TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_verified_at TEXT,
                        is_available INTEGER NOT NULL DEFAULT 1
                            CHECK (is_available IN (0, 1)),
                        UNIQUE(track_id, source_chat_id, source_message_id)
                    );
                    CREATE INDEX telegram_refs_track_available_idx
                        ON telegram_message_references(track_id, is_available, id);
                    CREATE INDEX telegram_refs_file_unique_idx
                        ON telegram_message_references(telegram_file_unique_id);

                    INSERT OR IGNORE INTO telegram_message_references (
                        track_id, owner_user_id, source_chat_id, source_message_id,
                        telegram_file_unique_id, media_type
                    )
                    SELECT t.id, p.user_id, t.source_chat_id, t.source_message_id,
                           t.file_unique_id, t.media_type
                    FROM tracks AS t
                    JOIN playlists AS p ON p.id = t.playlist_id;

                    CREATE TABLE tags (
                        id INTEGER PRIMARY KEY,
                        owner_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL COLLATE NOCASE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(owner_user_id, name)
                    );
                    CREATE TABLE track_tags (
                        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                        PRIMARY KEY(track_id, tag_id)
                    );
                    CREATE INDEX track_tags_tag_id_idx ON track_tags(tag_id, track_id);

                    CREATE TABLE metadata_only_tracks (
                        id INTEGER PRIMARY KEY,
                        owner_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id)
                            ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        performer TEXT,
                        release_title TEXT,
                        musicbrainz_recording_id TEXT,
                        provider_url TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX metadata_only_owner_idx
                        ON metadata_only_tracks(owner_user_id, id);

                    PRAGMA user_version = 6;
                    """
                )
                version = 6
            if version < 7:
                connection.executescript(
                    """
                    ALTER TABLE tracks ADD COLUMN release_year INTEGER
                        CHECK (release_year IS NULL OR release_year BETWEEN 1000 AND 9999);
                    ALTER TABLE pending_tracks ADD COLUMN release_year INTEGER
                        CHECK (release_year IS NULL OR release_year BETWEEN 1000 AND 9999);
                    ALTER TABLE metadata_only_tracks ADD COLUMN release_year INTEGER
                        CHECK (release_year IS NULL OR release_year BETWEEN 1000 AND 9999);
                    CREATE TABLE user_settings (
                        user_id INTEGER PRIMARY KEY REFERENCES users(telegram_user_id)
                            ON DELETE CASCADE,
                        delivery_mode TEXT NOT NULL DEFAULT 'copy'
                            CHECK (delivery_mode IN ('copy', 'forward'))
                    );
                    PRAGMA user_version = 7;
                    """
                )

    def create_playlist(self, user_id: int, name: str) -> bool:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
                (user_id,),
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO playlists (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            return cursor.rowcount == 1

    def list_playlists(self, user_id: int) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT p.id, p.name, COUNT(t.id) AS track_count
                FROM playlists AS p
                LEFT JOIN tracks AS t ON t.playlist_id = p.id
                WHERE p.user_id = ?
                GROUP BY p.id
                ORDER BY p.name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()

    def get_playlist(self, user_id: int, playlist_id: int) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT p.id, p.name, COUNT(t.id) AS track_count
                FROM playlists AS p
                LEFT JOIN tracks AS t ON t.playlist_id = p.id
                WHERE p.user_id = ? AND p.id = ?
                GROUP BY p.id
                """,
                (user_id, playlist_id),
            ).fetchone()

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
        with self._connection() as connection:
            playlist = connection.execute(
                "SELECT id FROM playlists WHERE user_id = ? AND id = ?",
                (user_id, playlist_id),
            ).fetchone()
            if playlist is None:
                return False
            connection.execute(
                "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
                (user_id,),
            )
            duplicate = None
            if file_unique_id:
                duplicate = connection.execute(
                    """
                    SELECT id FROM tracks
                    WHERE playlist_id = ? AND file_unique_id = ?
                    ORDER BY id LIMIT 1
                    """,
                    (playlist_id, file_unique_id),
                ).fetchone()
            if duplicate is not None:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO telegram_message_references (
                        track_id, owner_user_id, source_chat_id, source_message_id,
                        telegram_file_id, telegram_file_unique_id, media_type,
                        original_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        duplicate["id"], user_id, source_chat_id, source_message_id,
                        telegram_file_id, file_unique_id, media_type, original_filename,
                    ),
                )
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tracks (
                    playlist_id, source_chat_id, source_message_id, title,
                    performer, media_type, file_unique_id, release_title,
                    musicbrainz_recording_id, telegram_file_id, duration_seconds,
                    mime_type, original_filename, release_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_id,
                    source_chat_id,
                    source_message_id,
                    title,
                    performer,
                    media_type,
                    file_unique_id,
                    release_title,
                    musicbrainz_recording_id,
                    telegram_file_id,
                    duration_seconds,
                    mime_type,
                    original_filename,
                    release_year,
                ),
            )
            saved = cursor.rowcount == 1
            if saved:
                track_id = int(cursor.lastrowid)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO telegram_message_references (
                        track_id, owner_user_id, source_chat_id, source_message_id,
                        telegram_file_id, telegram_file_unique_id, media_type,
                        original_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id, user_id, source_chat_id, source_message_id,
                        telegram_file_id, file_unique_id, media_type, original_filename,
                    ),
                )
            return saved

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
        with self._connection() as connection:
            playlist = connection.execute(
                "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, playlist_name),
            ).fetchone()
            if playlist is None:
                raise PlaylistNotFoundError(playlist_name)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tracks (
                    playlist_id, source_chat_id, source_message_id, title,
                    performer, media_type, file_unique_id, release_title,
                    musicbrainz_recording_id, telegram_file_id, duration_seconds,
                    mime_type, original_filename, release_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist["id"],
                    source_chat_id,
                    source_message_id,
                    title,
                    performer,
                    media_type,
                    file_unique_id,
                    release_title,
                    musicbrainz_recording_id,
                    telegram_file_id,
                    duration_seconds,
                    mime_type,
                    original_filename,
                    release_year,
                ),
            )
            if cursor.rowcount == 1:
                track_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)
                    """,
                    (user_id,),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO telegram_message_references (
                        track_id, owner_user_id, source_chat_id, source_message_id,
                        telegram_file_id, telegram_file_unique_id, media_type,
                        original_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id, user_id, source_chat_id, source_message_id,
                        telegram_file_id, file_unique_id, media_type, original_filename,
                    ),
                )
                return track_id
            existing = connection.execute(
                """
                SELECT id FROM tracks
                WHERE playlist_id = ? AND (
                    (source_chat_id = ? AND source_message_id = ?)
                    OR (? IS NOT NULL AND file_unique_id = ?)
                )
                ORDER BY id LIMIT 1
                """,
                (
                    playlist["id"], source_chat_id, source_message_id,
                    file_unique_id, file_unique_id,
                ),
            ).fetchone()
            if existing is None:
                raise RuntimeError("Track insert was ignored without an existing reference")
            return int(existing["id"])

    def list_tracks(self, user_id: int, playlist_name: str) -> list[sqlite3.Row] | None:
        with self._connection() as connection:
            playlist = connection.execute(
                "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, playlist_name),
            ).fetchone()
            if playlist is None:
                return None
            return connection.execute(
                """
                SELECT id, source_chat_id, source_message_id, title, performer,
                       media_type, file_unique_id, release_title,
                       musicbrainz_recording_id, telegram_file_id,
                       duration_seconds, mime_type, original_filename, rating, note,
                       release_year
                FROM tracks
                WHERE playlist_id = ?
                ORDER BY id
                """,
                (playlist["id"],),
            ).fetchall()

    def list_tracks_by_id(
        self, user_id: int, playlist_id: int
    ) -> list[sqlite3.Row] | None:
        with self._connection() as connection:
            playlist = connection.execute(
                "SELECT id FROM playlists WHERE user_id = ? AND id = ?",
                (user_id, playlist_id),
            ).fetchone()
            if playlist is None:
                return None
            return connection.execute(
                """
                SELECT id, source_chat_id, source_message_id, title, performer,
                       media_type, file_unique_id, release_title,
                       musicbrainz_recording_id, telegram_file_id,
                       duration_seconds, mime_type, original_filename, rating, note,
                       release_year
                FROM tracks
                WHERE playlist_id = ?
                ORDER BY id
                """,
                (playlist_id,),
            ).fetchall()

    def search_tracks(
        self,
        user_id: int,
        query: str,
        limit: int,
        offset: int = 0,
        rating: int | None = None,
        release_year: int | None = None,
        playable: bool | None = None,
    ) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT t.id, t.source_chat_id, t.source_message_id, t.title,
                       t.performer, t.release_title, t.musicbrainz_recording_id,
                       t.telegram_file_id, t.duration_seconds, t.mime_type,
                       t.original_filename, t.rating, t.note, t.release_year,
                       p.id AS playlist_id, p.name AS playlist_name
                FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE p.user_id = ?
                  AND (
                    instr(lower(
                        t.title || ' ' || COALESCE(t.performer, '') || ' ' ||
                        COALESCE(t.release_title, '') || ' ' ||
                        COALESCE(t.note, '') || ' ' || p.name
                    ), lower(?)) > 0
                    OR EXISTS (
                        SELECT 1 FROM track_tags AS link
                        JOIN tags AS tag ON tag.id = link.tag_id
                        WHERE link.track_id = t.id AND tag.owner_user_id = ?
                          AND instr(lower(tag.name), lower(?)) > 0
                    )
                  )
                  AND (? IS NULL OR t.rating = ?)
                  AND (? IS NULL OR t.release_year = ?)
                  AND (
                    ? IS NULL
                    OR (? = 1 AND EXISTS (
                        SELECT 1 FROM telegram_message_references r
                        WHERE r.track_id = t.id AND r.owner_user_id = p.user_id
                          AND r.is_available = 1
                    ))
                    OR (? = 0 AND NOT EXISTS (
                        SELECT 1 FROM telegram_message_references r
                        WHERE r.track_id = t.id AND r.owner_user_id = p.user_id
                          AND r.is_available = 1
                    ))
                  )
                ORDER BY p.name COLLATE NOCASE, t.id
                LIMIT ? OFFSET ?
                """,
                (
                    user_id, query, user_id, query, rating, rating,
                    release_year, release_year, playable,
                    int(playable) if playable is not None else None,
                    int(playable) if playable is not None else None,
                    limit, offset,
                ),
            ).fetchall()

    def search_metadata_only_tracks(
        self, user_id: int, query: str, limit: int, offset: int = 0,
        release_year: int | None = None,
    ) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT id, title, performer, release_title, release_year,
                       musicbrainz_recording_id, provider_url
                FROM metadata_only_tracks
                WHERE owner_user_id = ?
                  AND instr(lower(
                      title || ' ' || COALESCE(performer, '') || ' ' ||
                      COALESCE(release_title, '')
                  ), lower(?)) > 0
                  AND (? IS NULL OR release_year = ?)
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (user_id, query, release_year, release_year, limit, offset),
            ).fetchall()

    def get_track(self, user_id: int, track_id: int) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT t.id, t.source_chat_id, t.source_message_id, t.title,
                       t.performer, t.release_title, t.musicbrainz_recording_id,
                       t.telegram_file_id, t.duration_seconds, t.mime_type,
                       t.original_filename, t.rating, t.note, t.release_year,
                       p.id AS playlist_id, p.name AS playlist_name
                FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE p.user_id = ? AND t.id = ?
                """,
                (user_id, track_id),
            ).fetchone()

    def list_library(
        self, user_id: int, limit: int = 8, offset: int = 0
    ) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT t.id, t.title, t.performer, t.release_title, t.release_year,
                       t.rating, p.id AS playlist_id, p.name AS playlist_name,
                       EXISTS (
                           SELECT 1 FROM telegram_message_references r
                           WHERE r.track_id = t.id AND r.owner_user_id = p.user_id
                             AND r.is_available = 1
                       ) AS is_playable
                FROM tracks t JOIN playlists p ON p.id = t.playlist_id
                WHERE p.user_id = ?
                ORDER BY t.id DESC LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()

    def get_statistics(self, user_id: int) -> dict[str, object]:
        with self._connection() as connection:
            totals = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM tracks t
                     JOIN playlists p ON p.id = t.playlist_id WHERE p.user_id = ?) AS total_tracks,
                    (SELECT COUNT(*) FROM metadata_only_tracks WHERE owner_user_id = ?) AS metadata_only_tracks,
                    (SELECT COUNT(*) FROM playlists WHERE user_id = ?) AS collection_count
                """,
                (user_id, user_id, user_id),
            ).fetchone()
            artists = connection.execute(
                """
                SELECT performer AS name, COUNT(*) AS track_count
                FROM tracks t JOIN playlists p ON p.id = t.playlist_id
                WHERE p.user_id = ? AND performer IS NOT NULL AND trim(performer) != ''
                GROUP BY lower(performer) ORDER BY track_count DESC, lower(performer)
                LIMIT 5
                """,
                (user_id,),
            ).fetchall()
            ratings = connection.execute(
                """
                SELECT rating, COUNT(*) AS track_count FROM tracks t
                JOIN playlists p ON p.id = t.playlist_id
                WHERE p.user_id = ? AND rating IS NOT NULL
                GROUP BY rating ORDER BY rating
                """,
                (user_id,),
            ).fetchall()
            tags = connection.execute(
                """
                SELECT tag.name, COUNT(DISTINCT link.track_id) AS track_count
                FROM tags tag JOIN track_tags link ON link.tag_id = tag.id
                JOIN tracks t ON t.id = link.track_id
                JOIN playlists p ON p.id = t.playlist_id
                WHERE tag.owner_user_id = ? AND p.user_id = ?
                GROUP BY tag.id ORDER BY track_count DESC, lower(tag.name)
                LIMIT 5
                """,
                (user_id, user_id),
            ).fetchall()
            return {
                "total_tracks": totals["total_tracks"],
                "metadata_only_tracks": totals["metadata_only_tracks"],
                "collection_count": totals["collection_count"],
                "artists": artists,
                "ratings": ratings,
                "tags": tags,
            }

    def discover_tracks(
        self, user_id: int, limit: int = 8
    ) -> list[sqlite3.Row]:
        with self._connection() as connection:
            track_count = connection.execute(
                """
                SELECT COUNT(*) FROM tracks t
                JOIN playlists p ON p.id = t.playlist_id
                WHERE p.user_id = ?
                """,
                (user_id,),
            ).fetchone()[0]
            if track_count < 2:
                return []
            return connection.execute(
                """
                WITH owner_tracks AS (
                    SELECT DISTINCT t.id, lower(t.performer) AS artist_key
                    FROM tracks t JOIN playlists p ON p.id = t.playlist_id
                    WHERE p.user_id = ?
                ),
                tag_overlap AS (
                    SELECT candidate.id AS track_id, COUNT(*) AS shared_tags
                    FROM owner_tracks candidate
                    JOIN track_tags candidate_tags ON candidate_tags.track_id = candidate.id
                    JOIN tags candidate_tag ON candidate_tag.id = candidate_tags.tag_id
                    JOIN owner_tracks seed ON seed.id != candidate.id
                    JOIN track_tags seed_tags ON seed_tags.track_id = seed.id
                        AND seed_tags.tag_id = candidate_tags.tag_id
                    WHERE candidate_tag.owner_user_id = ?
                    GROUP BY candidate.id
                ),
                artist_overlap AS (
                    SELECT candidate.id AS track_id, COUNT(*) AS shared_artist_tracks
                    FROM owner_tracks candidate
                    JOIN owner_tracks seed ON seed.id != candidate.id
                        AND seed.artist_key = candidate.artist_key
                    WHERE candidate.artist_key IS NOT NULL
                    GROUP BY candidate.id
                )
                SELECT t.id, t.title, t.performer, t.release_title,
                       p.name AS playlist_name,
                       COALESCE(tags.shared_tags, 0) AS shared_tags,
                       COALESCE(artists.shared_artist_tracks, 0) AS shared_artist_tracks
                FROM tracks t JOIN playlists p ON p.id = t.playlist_id
                JOIN owner_tracks ot ON ot.id = t.id
                LEFT JOIN tag_overlap tags ON tags.track_id = t.id
                LEFT JOIN artist_overlap artists ON artists.track_id = t.id
                WHERE p.user_id = ?
                  AND (COALESCE(tags.shared_tags, 0) > 0
                       OR COALESCE(artists.shared_artist_tracks, 0) > 0)
                ORDER BY (COALESCE(tags.shared_tags, 0) * 2
                          + COALESCE(artists.shared_artist_tracks, 0)) DESC, t.id
                LIMIT ?
                """,
                (user_id, user_id, user_id, limit),
            ).fetchall()

    def get_delivery_mode(self, user_id: int) -> str:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT delivery_mode FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row["delivery_mode"] if row is not None else "copy"

    def set_delivery_mode(self, user_id: int, mode: str) -> bool:
        if mode not in {"copy", "forward"}:
            raise ValueError("Delivery mode must be 'copy' or 'forward'.")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
                (user_id,),
            )
            connection.execute(
                """
                INSERT INTO user_settings (user_id, delivery_mode) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET delivery_mode = excluded.delivery_mode
                """,
                (user_id, mode),
            )
            return True

    def remove_track_by_id(self, user_id: int, playlist_id: int, track_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tracks
                WHERE id = ? AND playlist_id = ?
                  AND playlist_id IN (
                    SELECT id FROM playlists WHERE user_id = ?
                  )
                """,
                (track_id, playlist_id, user_id),
            )
            return cursor.rowcount == 1

    def remove_track(self, user_id: int, playlist_name: str, position: int) -> bool:
        with self._connection() as connection:
            playlist = connection.execute(
                "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, playlist_name),
            ).fetchone()
            if playlist is None:
                return False
            track = connection.execute(
                """
                SELECT id FROM tracks
                WHERE playlist_id = ?
                ORDER BY id
                LIMIT 1 OFFSET ?
                """,
                (playlist["id"], position - 1),
            ).fetchone()
            if track is None:
                return False
            connection.execute("DELETE FROM tracks WHERE id = ?", (track["id"],))
            return True

    def delete_playlist(self, user_id: int, name: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM playlists WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            return cursor.rowcount == 1

    def delete_playlist_by_id(self, user_id: int, playlist_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM playlists WHERE user_id = ? AND id = ?",
                (user_id, playlist_id),
            )
            return cursor.rowcount == 1

    def rename_playlist(self, user_id: int, playlist_id: int, name: str) -> bool:
        with self._connection() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM playlists
                WHERE user_id = ? AND name = ? AND id != ?
                """,
                (user_id, name, playlist_id),
            ).fetchone()
            if duplicate is not None:
                return False
            cursor = connection.execute(
                "UPDATE playlists SET name = ? WHERE user_id = ? AND id = ?",
                (name, user_id, playlist_id),
            )
            return cursor.rowcount == 1

    def set_user_state(self, user_id: int, action: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_states (user_id, action) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET action = excluded.action
                """,
                (user_id, action),
            )

    def get_user_state(self, user_id: int) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT action FROM user_states WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row["action"] if row is not None else None

    def clear_user_state(self, user_id: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))

    def save_pending_track(
        self,
        user_id: int,
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
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO pending_tracks (
                    user_id, source_chat_id, source_message_id, title,
                    performer, media_type, file_unique_id, release_title,
                    musicbrainz_recording_id, telegram_file_id,
                    duration_seconds, mime_type, original_filename, release_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    source_chat_id,
                    source_message_id,
                    title,
                    performer,
                    media_type,
                    file_unique_id,
                    release_title,
                    musicbrainz_recording_id,
                    telegram_file_id,
                    duration_seconds,
                    mime_type,
                    original_filename,
                    release_year,
                ),
            )

    def get_pending_track(
        self, user_id: int, source_chat_id: int, source_message_id: int
    ) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT title, performer, media_type, file_unique_id, release_title,
                       musicbrainz_recording_id, telegram_file_id,
                       duration_seconds, mime_type, original_filename, release_year
                FROM pending_tracks
                WHERE user_id = ? AND source_chat_id = ? AND source_message_id = ?
                """,
                (user_id, source_chat_id, source_message_id),
            ).fetchone()

    def delete_pending_track(
        self, user_id: int, source_chat_id: int, source_message_id: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM pending_tracks
                WHERE user_id = ? AND source_chat_id = ? AND source_message_id = ?
                """,
                (user_id, source_chat_id, source_message_id),
            )

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
    ) -> bool:
        with self._connection() as connection:
            owned_track = connection.execute(
                """
                SELECT t.id FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE t.id = ? AND p.user_id = ?
                """,
                (track_id, user_id),
            ).fetchone()
            if owned_track is None:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO telegram_message_references (
                    track_id, owner_user_id, source_chat_id, source_message_id,
                    telegram_file_id, telegram_file_unique_id, media_type,
                    original_filename
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track_id,
                    user_id,
                    source_chat_id,
                    source_message_id,
                    telegram_file_id,
                    file_unique_id,
                    media_type,
                    original_filename,
                ),
            )
            return cursor.rowcount == 1

    def get_available_message_reference(
        self, user_id: int, track_id: int
    ) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT r.id, r.source_chat_id, r.source_message_id, r.media_type
                FROM telegram_message_references AS r
                JOIN tracks AS t ON t.id = r.track_id
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE r.track_id = ? AND r.owner_user_id = ? AND p.user_id = ?
                  AND r.is_available = 1
                ORDER BY r.id
                LIMIT 1
                """,
                (track_id, user_id, user_id),
            ).fetchone()

    def list_message_references(
        self, user_id: int, track_id: int
    ) -> list[sqlite3.Row] | None:
        with self._connection() as connection:
            owned = connection.execute(
                """
                SELECT 1 FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE t.id = ? AND p.user_id = ?
                """,
                (track_id, user_id),
            ).fetchone()
            if owned is None:
                return None
            return connection.execute(
                """
                SELECT id, source_chat_id, source_message_id, media_type,
                       original_filename, is_available, created_at, last_verified_at
                FROM telegram_message_references
                WHERE track_id = ? AND owner_user_id = ?
                ORDER BY id
                """,
                (track_id, user_id),
            ).fetchall()

    def mark_message_reference_unavailable(
        self, user_id: int, reference_id: int
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE telegram_message_references
                SET is_available = 0
                WHERE id = ? AND owner_user_id = ?
                  AND EXISTS (
                    SELECT 1 FROM tracks AS t
                    JOIN playlists AS p ON p.id = t.playlist_id
                    WHERE t.id = telegram_message_references.track_id
                      AND p.user_id = ?
                  )
                """,
                (reference_id, user_id, user_id),
            )
            return cursor.rowcount == 1

    def mark_message_reference_verified(
        self, user_id: int, reference_id: int
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE telegram_message_references
                SET is_available = 1, last_verified_at = CURRENT_TIMESTAMP
                WHERE id = ? AND owner_user_id = ?
                  AND EXISTS (
                    SELECT 1 FROM tracks AS t
                    JOIN playlists AS p ON p.id = t.playlist_id
                    WHERE t.id = telegram_message_references.track_id
                      AND p.user_id = ?
                  )
                """,
                (reference_id, user_id, user_id),
            )
            return cursor.rowcount == 1

    def set_track_rating(self, user_id: int, track_id: int, rating: int | None) -> bool:
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError("Rating must be between 1 and 5.")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tracks SET rating = ?
                WHERE id = ? AND playlist_id IN (
                    SELECT id FROM playlists WHERE user_id = ?
                )
                """,
                (rating, track_id, user_id),
            )
            return cursor.rowcount == 1

    def set_track_note(self, user_id: int, track_id: int, note: str | None) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tracks SET note = ?
                WHERE id = ? AND playlist_id IN (
                    SELECT id FROM playlists WHERE user_id = ?
                )
                """,
                (note, track_id, user_id),
            )
            return cursor.rowcount == 1

    def update_track_metadata(
        self,
        user_id: int,
        track_id: int,
        title: str,
        performer: str | None,
    ) -> bool:
        clean_title = title.strip()
        if not clean_title or len(clean_title) > 250:
            raise ValueError("Track titles must contain 1–250 characters.")
        clean_performer = performer.strip() if performer else None
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tracks SET title = ?, performer = ?
                WHERE id = ? AND playlist_id IN (
                    SELECT id FROM playlists WHERE user_id = ?
                )
                """,
                (clean_title, clean_performer, track_id, user_id),
            )
            return cursor.rowcount == 1

    def add_track_tag(self, user_id: int, track_id: int, name: str) -> bool:
        normalized = name.strip()
        if not normalized or len(normalized) > 32:
            raise ValueError("Tags must contain 1–32 characters.")
        with self._connection() as connection:
            owned = connection.execute(
                """
                SELECT 1 FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE t.id = ? AND p.user_id = ?
                """,
                (track_id, user_id),
            ).fetchone()
            if owned is None:
                return False
            connection.execute(
                """
                INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)
                """,
                (user_id,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO tags (owner_user_id, name) VALUES (?, ?)
                """,
                (user_id, normalized),
            )
            tag = connection.execute(
                "SELECT id FROM tags WHERE owner_user_id = ? AND name = ?",
                (user_id, normalized),
            ).fetchone()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO track_tags (track_id, tag_id) VALUES (?, ?)",
                (track_id, tag["id"]),
            )
            return cursor.rowcount == 1

    def remove_track_tag(self, user_id: int, track_id: int, name: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM track_tags
                WHERE track_id = ? AND tag_id IN (
                    SELECT id FROM tags WHERE owner_user_id = ? AND name = ?
                ) AND track_id IN (
                    SELECT t.id FROM tracks AS t
                    JOIN playlists AS p ON p.id = t.playlist_id
                    WHERE p.user_id = ?
                )
                """,
                (track_id, user_id, name.strip(), user_id),
            )
            return cursor.rowcount == 1

    def list_track_tags(self, user_id: int, track_id: int) -> list[str] | None:
        with self._connection() as connection:
            owned = connection.execute(
                """
                SELECT 1 FROM tracks AS t JOIN playlists AS p ON p.id = t.playlist_id
                WHERE t.id = ? AND p.user_id = ?
                """,
                (track_id, user_id),
            ).fetchone()
            if owned is None:
                return None
            rows = connection.execute(
                """
                SELECT tag.name FROM tags AS tag
                JOIN track_tags AS link ON link.tag_id = tag.id
                WHERE link.track_id = ? AND tag.owner_user_id = ?
                ORDER BY tag.name COLLATE NOCASE
                """,
                (track_id, user_id),
            ).fetchall()
            return [row["name"] for row in rows]

    def create_metadata_only_track(
        self,
        user_id: int,
        title: str,
        performer: str | None = None,
        release_title: str | None = None,
        musicbrainz_recording_id: str | None = None,
        provider_url: str | None = None,
        release_year: int | None = None,
    ) -> int:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Track title cannot be empty.")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
                (user_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO metadata_only_tracks (
                    owner_user_id, title, performer, release_title,
                    musicbrainz_recording_id, provider_url, release_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    clean_title,
                    performer,
                    release_title,
                    musicbrainz_recording_id,
                    provider_url,
                    release_year,
                ),
            )
            return int(cursor.lastrowid)

    def list_metadata_only_tracks(self, user_id: int) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT id, title, performer, release_title,
                       musicbrainz_recording_id, provider_url, created_at
                       , release_year
                FROM metadata_only_tracks
                WHERE owner_user_id = ?
                ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()

    def delete_metadata_only_track(self, user_id: int, track_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM metadata_only_tracks WHERE id = ? AND owner_user_id = ?",
                (track_id, user_id),
            )
            return cursor.rowcount == 1

    def create_collection(self, user_id: int, name: str) -> bool:
        return self.create_playlist(user_id, name)

    def list_collections(self, user_id: int) -> list[sqlite3.Row]:
        return self.list_playlists(user_id)

    def add_track_to_collection(
        self, user_id: int, track_id: int, collection_id: int
    ) -> bool:
        with self._connection() as connection:
            source = connection.execute(
                """
                SELECT t.* FROM tracks AS t
                JOIN playlists AS p ON p.id = t.playlist_id
                WHERE t.id = ? AND p.user_id = ?
                """,
                (track_id, user_id),
            ).fetchone()
            collection = connection.execute(
                "SELECT id FROM playlists WHERE id = ? AND user_id = ?",
                (collection_id, user_id),
            ).fetchone()
            if source is None or collection is None:
                return False
            duplicate = connection.execute(
                """
                SELECT id FROM tracks
                WHERE playlist_id = ? AND (
                    (source_chat_id = ? AND source_message_id = ?)
                    OR (? IS NOT NULL AND file_unique_id = ?)
                )
                """,
                (
                    collection_id,
                    source["source_chat_id"],
                    source["source_message_id"],
                    source["file_unique_id"],
                    source["file_unique_id"],
                ),
            ).fetchone()
            if duplicate is not None:
                return False
            cursor = connection.execute(
                """
                INSERT INTO tracks (
                    playlist_id, source_chat_id, source_message_id, title, performer,
                    media_type, file_unique_id, release_title, musicbrainz_recording_id,
                    telegram_file_id, duration_seconds, mime_type, original_filename,
                    rating, note, release_year
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collection_id,
                    source["source_chat_id"],
                    source["source_message_id"],
                    source["title"],
                    source["performer"],
                    source["media_type"],
                    source["file_unique_id"],
                    source["release_title"],
                    source["musicbrainz_recording_id"],
                    source["telegram_file_id"],
                    source["duration_seconds"],
                    source["mime_type"],
                    source["original_filename"],
                    source["rating"],
                    source["note"],
                    source["release_year"],
                ),
            )
            new_track_id = int(cursor.lastrowid)
            references = connection.execute(
                """
                SELECT source_chat_id, source_message_id, telegram_file_id,
                       telegram_file_unique_id, media_type, original_filename,
                       last_verified_at, is_available
                FROM telegram_message_references
                WHERE track_id = ? AND owner_user_id = ?
                """,
                (track_id, user_id),
            ).fetchall()
            for reference in references:
                connection.execute(
                    """
                    INSERT INTO telegram_message_references (
                        track_id, owner_user_id, source_chat_id, source_message_id,
                        telegram_file_id, telegram_file_unique_id, media_type,
                        original_filename, last_verified_at, is_available
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_track_id,
                        user_id,
                        reference["source_chat_id"],
                        reference["source_message_id"],
                        reference["telegram_file_id"],
                        reference["telegram_file_unique_id"],
                        reference["media_type"],
                        reference["original_filename"],
                        reference["last_verified_at"],
                        reference["is_available"],
                    ),
                )
            tags = connection.execute(
                """
                SELECT tag.id FROM tags AS tag
                JOIN track_tags AS link ON link.tag_id = tag.id
                WHERE link.track_id = ? AND tag.owner_user_id = ?
                """,
                (track_id, user_id),
            ).fetchall()
            for tag in tags:
                connection.execute(
                    "INSERT INTO track_tags (track_id, tag_id) VALUES (?, ?)",
                    (new_track_id, tag["id"]),
                )
            return True

    def rename_collection(self, user_id: int, collection_id: int, name: str) -> bool:
        return self.rename_playlist(user_id, collection_id, name)

    def delete_collection(self, user_id: int, collection_id: int) -> bool:
        return self.delete_playlist_by_id(user_id, collection_id)
