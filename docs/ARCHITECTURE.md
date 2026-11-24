# TuneShelf Architecture

## Current implementation

TuneShelf is a synchronous, single-process Telegram bot with two persistence
adapters. The repository has no installable application package or separate
worker process.

| Component | Responsibility |
| --- | --- |
| `bot.py` | Telegram Bot API HTTP calls, long polling, command and callback handling, menus, interaction state, metadata discovery orchestration, and delivery retries |
| `archive.py` | Telegram-independent track metadata and message-reference value objects and an application service/repository protocol for collection management, saves, annotations, collection copies, and delivery preferences |
| `database.py` | SQLite legacy schema setup, transient bot state, and shared archive operations |
| `postgres_database.py` | PostgreSQL legacy schema setup, transient bot state, and shared archive operations |
| `app/database/runtime.py` | Routes runtime library workflows to the normalized repository while retaining the legacy adapter for compatibility |
| `app/database/library_repository.py` | Owner-scoped normalized library, playlist, metadata, annotation, and source-reference operations |
| `telegram_delivery.py` | `copyMessage` versus `forwardMessage` delivery policy |
| `metadata.py` | Provider-independent metadata search result and provider interfaces |
| `youtube_music.py` | YouTube Music song search and normalized metadata results |
| `musicbrainz.py` | Synchronous MusicBrainz recording lookup, response parsing, throttling, and in-memory caching for optional audio enrichment |
| `app/database/models.py` | SQLAlchemy models for the normalized catalog, archive, library, playlist, and personal metadata schema |
| `app/database/legacy_inventory.py` | Read-only legacy database inventory for migration preflight |
| `app/database/legacy_backfill.py` | Dry-run-by-default import from a legacy SQLite/PostgreSQL database into the normalized schema |
| `app/database/download_repository.py` | Owner-scoped download job enqueue, lifecycle, retry, cancellation, and batch persistence |
| `app/services/download_service.py` | Coordinates queued download processing and conversion without Telegram handlers |
| `app/integrations/downloader.py` | yt-dlp/FFmpeg downloader and processor adapters behind protocols |
| `migrations/` | Additive Alembic migrations applied at startup after legacy schema setup |

`archive.py` coordinates collection and library operations through the
normalized repository. `bot.py` retains transient interaction state and
Telegram orchestration. New audio is added to the user's library first, and
collection membership is optional. Existing SQLite and PostgreSQL legacy
library rows are imported in-place at startup before runtime library access;
the legacy tables remain available for compatibility. The normalized repository
has SQLite and PostgreSQL contract coverage, with PostgreSQL execution in CI.

When `ARCHIVE_CHAT_ID` is configured, new saved submissions are copied to that
private Telegram channel. Available archive message references are reused by
`file_unique_id` across users and preferred during playback. Archive failures
leave the original source reference usable. The adapters record shared archive
locations in normalized `archive_assets`, separately from user-owned message
references. New assets link to a normalized catalog track; old archive records
are imported without inventing track metadata and can be linked when a user
submits matching audio later.

The bot stores Telegram message references and metadata; it does not download
audio binaries. It uses Telegram's server-side `copyMessage` to place a
submission in the configured shared archive. Playback copies or forwards the
original or archived message. Updates are polled and handled synchronously and
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
- YouTube Music text discovery with metadata-only records; provider results
  never become playable audio. MusicBrainz is used only for optional audio
  metadata enrichment.
- SQLite migration tests and application tests using mocked Telegram and
  metadata-provider calls.

## Important current boundaries

- Library membership is independent from playlist membership. Removing a
  playlist item does not remove its library entry; personal metadata overrides
  and annotations remain owner-scoped.
- Stored Telegram source references describe user/source messages; shared
  archive assets remain separate.
- `file_unique_id` is a local duplicate hint, not a cross-bot permanent
  identifier.
- `bot.py` and both database adapters are large modules containing multiple
  responsibilities.
- The download queue and yt-dlp/FFmpeg adapters exist, but there is no persistent
  worker process or Telegram archive/upload registration yet.

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

The shared archive is represented separately from user library membership.
Archive assets may link to a catalog track when metadata is available and
contain the Telegram archive chat/message reference and relevant file
identifiers. Existing source-message references must not be relabeled as
central archive assets unless they are actually copied to the configured
archive.

## Incremental boundaries

- Keep the current user-facing workflows working while introducing the new
  model and migration path.
- Do not replace Telegram polling or delivery during the data-model migration.
- Do not add discovery/download providers as part of the core model milestone.
- Move operations to the new repositories incrementally; remove legacy adapters
  only after parity and migration tests pass.
- Make changes to data semantics explicit, especially deduplication and
  playlist ordering.
