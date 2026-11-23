# TuneShelf Architecture

## Current implementation

TuneShelf is a synchronous, single-process Telegram bot with two persistence
adapters. The repository has no installable application package or separate
worker process.

| Component | Responsibility |
| --- | --- |
| `bot.py` | Telegram Bot API HTTP calls, long polling, command and callback handling, menus, interaction state, MusicBrainz orchestration, and delivery retries |
| `archive.py` | Telegram-independent track metadata and message-reference value objects and a small application service/repository protocol |
| `database.py` | SQLite schema migrations and repository operations |
| `postgres_database.py` | PostgreSQL schema setup and repository operations using psycopg |
| `telegram_delivery.py` | `copyMessage` versus `forwardMessage` delivery policy |
| `musicbrainz.py` | Synchronous MusicBrainz recording search, URL lookup, response parsing, throttling, and in-memory caching |
| `app/database/models.py` | Additive SQLAlchemy model for the normalized catalog, archive, library, playlist, and personal metadata schema |
| `migrations/` | Explicit Alembic migrations; the initial revision creates the normalized schema without replacing legacy tables |

`bot.py` calls the repository directly for most workflows. The `archive.py`
service is used for selected operations, but does not yet isolate the bot from
the persistence API. Both database adapters implement similar behavior
separately, so backend behavior can diverge.

The bot stores Telegram source-message references and metadata; it does not
download audio or upload it to a shared archive. Playback copies or forwards
the original message. Updates are polled and handled synchronously and
sequentially.

## Implemented behavior

- Private-chat-only interaction, menus, callback controls, and text commands.
- Audio, voice, and audio-document metadata capture without downloading media.
- User-owned playlists/collections, track listing, search, pagination, rename,
  deletion, add-to-collection, and bounded sequential or shuffled delivery.
- Per-user ratings, notes, tags, delivery preferences, and recommendations based
  on matching artists and personal tags.
- Multiple Telegram message references per saved track and unavailable
  reference tracking.
- MusicBrainz search and metadata-only records; MusicBrainz is not a playable
  music source.
- SQLite migration tests and application tests using mocked Telegram and
  MusicBrainz calls.

## Important current boundaries

- A saved track belongs to one playlist. Adding it to another playlist creates
  another track row and copies its metadata, annotations, tags, and message
  references.
- The stored Telegram references describe user/source messages, not a
  centrally managed archive channel.
- `file_unique_id` is a local duplicate hint, not a cross-bot permanent
  identifier.
- `bot.py` and both database adapters are large modules containing multiple
  responsibilities.
-   The bot repositories still use the legacy SQL implementations; the new
  SQLAlchemy models and Alembic schema are not yet wired into runtime workflows.
  There is no ytmusicapi provider,
  yt-dlp/FFmpeg integration, download queue, or background worker.

## Target architecture

Retain a modular monolith and the current Telegram framework/API integration
while separating transport, application logic, persistence, and external
providers:

1. **Telegram interface:** parse updates, validate callback/input shape, invoke
   application services, and render results. Avoid persistence and domain rules
   in handlers.
2. **Application services:** coordinate catalog, library, playlist, archive,
   search, and later download use cases.
3. **Domain model:** represent global tracks separately from user-library
   membership, personal annotations, and playlist membership.
4. **Repositories and migrations:** use PostgreSQL as the primary store,
   SQLAlchemy for persistence, and Alembic for versioned migrations. Keep
   SQLite only where it provides an intentional local/test workflow.
5. **Integrations:** isolate Telegram archive/delivery, metadata providers, and
   later downloader/FFmpeg clients behind provider/service interfaces.
6. **Workers:** add a PostgreSQL-backed download queue and worker only in the
   download phase; do not introduce Redis/Celery without a demonstrated need.

The shared archive must be represented separately from a track and from a
user's library membership. A future archive asset should contain the Telegram
archive chat/message reference and relevant file identifiers. Existing source
message references must not be relabeled as central archive assets unless they
are actually copied to the configured archive.

## Incremental boundaries

- Keep the current user-facing workflows working while introducing the new
  model and migration path.
- Do not replace Telegram polling or delivery during the data-model migration.
- Do not add discovery/download providers as part of the core model milestone.
- Move operations to the new repositories incrementally; remove legacy adapters
  only after parity and migration tests pass.
- Make changes to data semantics explicit, especially deduplication and
  playlist ordering.
