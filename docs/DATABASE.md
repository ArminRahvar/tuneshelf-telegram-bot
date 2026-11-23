# TuneShelf Database Design and Migration

## Existing schemas

### SQLite

`database.py` uses `PRAGMA user_version` and applies schema versions 1 through
7. The local database inspected during assessment is at version 7. Its main
tables are:

- `users`, `playlists`, `tracks`
- `telegram_message_references`
- `tags`, `track_tags`
- `metadata_only_tracks`
- `pending_tracks`, `user_states`, `user_settings`

`tracks.playlist_id` is the current ownership path. Track metadata, rating, and
note are stored on the playlist-specific track row. Personal tags are attached
through a user-owned tag table. Message references have an `owner_user_id` and
are checked against playlist ownership by repository queries.

Version 4 removes duplicate track rows within a playlist, retaining the lowest
ID for each source message. Version 6 introduces users and message references
and backfills references from existing tracks. The current startup migration
sequence is part of the deployed behavior and must be preserved or explicitly
superseded.

### PostgreSQL

`postgres_database.py` creates tables and indexes using `CREATE TABLE IF NOT
EXISTS`, performs selected `ADD COLUMN IF NOT EXISTS` operations, and backfills
users and message references. It does not maintain a migration version/history
or provide an explicit SQLite-to-PostgreSQL data transfer. The schema has
several differences from SQLite, so both adapters require parity tests before
either is retired.

## Target logical model

The target model separates shared catalog data, user-owned membership, and
Telegram storage:

- `users`
- `tracks`
- `artists`, `albums`, `genres`, and track association tables
- `archive_assets` for shared Telegram archive message references
- `user_library` for a user's ownership/membership of a track
- `user_track_tags`, `user_track_notes`, and `user_track_ratings`
- `playlists` and `playlist_tracks`
- `download_jobs` in the later download phase

Key invariants:

1. A catalog track can be referenced by multiple users and playlists.
2. A playlist belongs to one user; playlist-track membership must be for a
   track in that same user's library.
3. A user can add a library track to multiple playlists without copying the
   track row or shared archive asset.
4. Playlist membership has a stable, unique position per playlist and does not
   imply ownership of the shared archive asset.
5. Personal annotations are scoped to a user and track, never stored as shared
   catalog metadata.
6. Archive identifiers are references, not audio binaries. A track can have
   more than one archive asset, and an unavailable asset does not delete the
   track.
7. Database constraints should enforce ownership and uniqueness where
   practical; service-level checks alone are not sufficient for cross-entity
   ownership invariants.
8. Use UTC timestamps and transactions for related writes.

The initial Alembic revision establishes these owner-scoped foreign-key and
uniqueness rules. Further changes to them must use follow-up revisions rather
than editing an already deployed revision.

## Additive schema foundation

The first Alembic revision is deliberately additive so the current bot and its
legacy tables can remain operational during the transition. It introduces:

- `catalog_tracks`, `artists`, `albums`, `genres`, and catalog association
  tables.
- `archive_assets` for shared archive messages and
  `telegram_source_references` for user-provided Telegram messages.
- `library_entries`, `user_playlists`, `playlist_items`, and separate personal
  tag/note/rating tables.

The legacy `users` table is reused because both current adapters already have
the required Telegram ID primary key. The migration creates it only when
missing and intentionally leaves it in place on downgrade. Other legacy tables
are neither renamed nor dropped. New playlist items have composite foreign
keys to both their user-owned playlist and that same user's library entry;
position and membership are unique per playlist. Personal annotations and
source references similarly reference an owner-scoped library entry.

The case-insensitive name indexes are written explicitly in the revision:
SQLite's Alembic reflection cannot compare expression-based indexes reliably,
so those indexes should be reviewed manually in future generated revisions.

`alembic upgrade head` only creates this parallel schema; it does not move
legacy data or switch application repositories. That backfill and cutover
remain separate, unimplemented steps.

## Migration-safe path

Do not modify or discard the current SQLite database or PostgreSQL tables in
place as an initial experiment. The migration should be a separately testable,
auditable operation:

1. Take a verified backup and record source backend/schema version.
2. Create and test the new versioned schema on an empty database. The additive
   schema foundation is now in place.
3. Inventory legacy rows using counts and relationship checks, without
   exporting media or credentials.
4. Backfill users and playlists, then map legacy track rows to catalog tracks,
   user-library memberships, personal annotations, and playlist memberships.
5. Preserve source Telegram references as source references. Do not treat them
   as shared archive assets until a real archive copy exists.
6. Preserve legacy IDs when practical or keep an explicit old-to-new ID mapping
   for callbacks and references during cutover.
7. Avoid heuristic merging in the first migration. Exact duplicate evidence
   (such as the same source message) may be handled deterministically; uncertain
   title/artist matches should remain separate pending a reviewed deduplication
   policy.
8. If multiple playlist copies map to one user/track membership, reconcile
   differing ratings, notes, tags, and metadata explicitly. Do not silently
   select one conflicting value.
9. Run post-migration checks for row counts, foreign keys, ownership,
   references, annotations, and playlist ordering.
10. Keep the legacy schema/data available until cutover validation and rollback
    procedures are exercised. Do not make application startup silently migrate
    production data during the initial rollout.

SQLite-to-PostgreSQL transfer is a separate concern from schema migration.
Switching `DATABASE_URL` currently selects a backend but does not copy data;
that behavior must remain explicit until a tested export/import tool exists.

## Required migration tests

- Fresh schema creation and upgrade from the current SQLite user_version 7.
- Upgrade from representative earlier SQLite versions, including version 1.
- Idempotency and recovery behavior for interrupted migrations.
- Preservation of playlists, tracks, source references, metadata, ratings,
  notes, tags, settings, and metadata-only records.
- Ownership isolation and rejection of cross-user playlist/library membership.
- Duplicate playlist membership prevention and stable ordering.
- PostgreSQL schema creation, migration, and repository behavior against a
  disposable PostgreSQL instance.
- Dry-run/report output and post-migration integrity checks for any data
  transfer.
