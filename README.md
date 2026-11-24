# Personal Music Archive Bot

A private-chat Telegram bot for organizing a personal music archive. Audio remains in Telegram: the application does not call `getFile`, download media, or write audio binaries to its database or filesystem. Saved playback copies the original Telegram message with `copyMessage` by default; delivery policy is isolated in `telegram_delivery.py`.

## Features

- Save Telegram audio messages, voice messages, and audio MIME-type documents with their original chat/message reference.
- Save each submission to the private library immediately; adding it to a collection is optional. Removing a track from a collection leaves it in the library.
- Optionally copy new submissions to a private Telegram archive channel; reuse an available archived message for matching Telegram `file_unique_id` values and prefer it for playback.
- Preserve Telegram file identifiers and available title, artist, duration, MIME type, and filename metadata. `file_unique_id` is used only as a duplicate hint; it is not used for playback.
- Keep multiple source-message references per saved track, mark failed references unavailable without deleting metadata, and use `/link` to associate another Telegram copy with a saved track.
- Create, rename, browse, search, play, and delete private playlists/collections; play or remove individual tracks from a collection's track list, and add an existing track to another collection with `/addtrack`. Collection delivery is capped at 20 Telegram messages per request, paced between messages, and retries Telegram rate-limit responses once.
- Edit metadata and manage private ratings, notes, and tags with `/edit`, `/rate`, `/note`, `/tag`, and `/untag`.
- Use both a persistent reply keyboard and inline keyboards for Add Music, Library, Search, Collections, Discover, Statistics, and Settings. Library track buttons open details and provide inline controls for rating, notes, tags, metadata edits, and linking another Telegram source.
- Search the private library by title, artist, album, collection, tags, rating, release year, or playability; filter with terms such as `rating:5 year:2024 playable:true` or `metadata:true`.
- View per-user library statistics and recommendations based only on matching artists and manually assigned tags.
- Search YouTube Music by text. Results are always selected explicitly and saved as metadata-only tracks; they never create a playable Telegram reference. Set `YTMUSIC_ENABLED=false` to disable external search.
- Queue downloads from HTTP(S) URLs with `/download <url>`, `/downloads`,
  `/cancel_download <job-id>`, and `/retry_download <job-id>`. A daemon worker
  in the bot process executes yt-dlp and FFmpeg outside the Telegram update
  loop.
- Queue download jobs with retry, cancellation, duplicate prevention, progress,
  batch IDs, and lifecycle persistence. yt-dlp and FFmpeg adapters are
  available behind application protocols; a persistent worker process and
  Telegram archive upload wiring remain deployment work.
- Use an application/domain service in `archive.py` that does not depend on Telegram handlers. Persistence is available through the local SQLite adapter and the PostgreSQL adapter.

The bot ignores chats other than private chats. `/start` and `/help` open the main menu. Track IDs are shown in the library and collections. Most common workflows are available through reply and inline buttons; commands remain available for direct use and compatibility.

## Run locally

Python 3.10 or newer is required. SQLite is used when `DATABASE_URL` is not set. The bot loads variables from a local `.env` file when present; environment variables already set by the shell take precedence. YouTube Music text search is enabled by default and can be disabled with `YTMUSIC_ENABLED=false`. Install the requirements to run the bot and tests:

```sh
cp .env.example .env
# Set BOT_TOKEN and, optionally, ARCHIVE_CHAT_ID in .env.
python3 -m pip install -r requirements.txt
python3 bot.py
```

To enable the shared archive, create a private channel, add the bot as an
administrator with permission to post messages, and set `ARCHIVE_CHAT_ID` to
the channel's numeric Telegram chat ID. If this setting is empty, the bot keeps
using the original user-message references. If Telegram cannot copy a message
to the channel, the library save still succeeds and the original source remains
available.

For local PostgreSQL instead, install the PostgreSQL driver and set a connection URL:

```sh
python3 -m pip install -r requirements.txt
export DATABASE_URL='postgresql://playlist_bot:password@localhost:5432/playlist_bot'
python3 bot.py
```

The PostgreSQL database/user must already exist. At startup, the bot applies
its idempotent legacy schema setup followed by Alembic migrations. Existing
legacy library rows are imported into the normalized tables in-place before
the bot switches library operations to them. Legacy tables are kept intact.
This startup import does not transfer data between a SQLite database and a
different PostgreSQL database.

Setting `DATABASE_URL` switches backends; existing SQLite data is not copied
automatically. Use the explicit backfill procedure below to transfer data to a
different backend, and keep a verified backup.

To apply the normalized schema without starting the bot, set `DATABASE_URL`
to the target database and run:

```sh
DATABASE_URL='sqlite:////absolute/path/to/playlist_bot.sqlite3' python3 -m alembic upgrade head
```

This adds the normalized tables and imports legacy shared archive assets while
keeping the legacy tables intact. The personal-library import is performed by
the bot at startup when legacy library rows exist.

Before planning a backfill, inspect the current database without modifying it:

```sh
DATABASE_URL='sqlite:////absolute/path/to/playlist_bot.sqlite3' python3 -m app.database.legacy_inventory
```

The inventory reports counts and integrity/conflict totals only; it does not print track metadata or user IDs.

The backfill reads `LEGACY_DATABASE_URL` in a read-only transaction and writes
to the database in `DATABASE_URL`. First create/upgrade the target schema, then
run a dry-run (the default) with a stable, non-secret source label:

```sh
LEGACY_DATABASE_URL='sqlite:////absolute/path/to/playlist_bot.sqlite3' \
DATABASE_URL='sqlite:////absolute/path/to/playlist_bot.sqlite3' \
python3 -m app.database.legacy_backfill --source-key my-library-2026
```

Review the aggregate report and ensure you have a verified backup before
repeating the command with `--apply`. Use a separate target URL to transfer
between backends, for example from SQLite to PostgreSQL. The importer creates
one catalog track per legacy track row (no heuristic merging), preserves
playlist order, personal annotations, tags, preferences, metadata-only tracks,
and source Telegram references, and records stable ID mappings. It does not
import transient `pending_tracks` or `user_states`. A successful import is
idempotent for the same source snapshot and source key; changing that source
later under the same key is rejected for review. Apply is one target
transaction and any conflict rolls back the full import. The dry-run validates
the source and target schema without writing either database; target data
conflicts are surfaced during apply and rolled back.

MusicBrainz audio metadata enrichment is optional. Configure a contact email for its User-Agent:

```sh
export MUSICBRAINZ_CONTACT_EMAIL='you@example.com'
```

When enabled, MusicBrainz may enrich metadata on a newly submitted audio message. Text discovery uses YouTube Music and is enabled by default; set `YTMUSIC_ENABLED=false` to turn it off. Search queries are sent to YouTube Music, and selected results are stored as metadata only. They are never treated as playable audio.

## Telegram commands

- `/track <id>` — show metadata, private annotations, and source-reference availability.
- `/play <id>` — retrieve one saved track.
- `/edit <id> <title> | <artist>` — correct title/artist metadata.
- `/rate <id> <1-5|clear>` — set/remove a personal rating.
- `/note <id> <text|clear>` — set/remove a personal note.
- `/tag <id> <tag>` and `/untag <id> <tag>` — manage private tags.
- `/link <id>` — then send/forward another audio message to add a source reference.
- `/collections` — list collection IDs and contents counts.
- `/addtrack <track-id> <collection-id>` — add an existing track and its source references to another collection.
- `/metadata` and `/deletemetadata <id>` — list/remove metadata-only search results.
- `/download <url>` — queue an authorized HTTP(S) audio download.
- `/downloads` — list download jobs and their status.
- `/cancel_download <job-id>` — cancel an active download job.
- `/retry_download <job-id>` — retry a failed or cancelled job.

## Container deployment

Docker Compose runs PostgreSQL 16 and the bot. It persists the database in a named volume and runs the bot as an unprivileged user:

```sh
cp .env.example .env
# Set BOT_TOKEN and a long random URL-safe POSTGRES_PASSWORD in .env.
docker compose up --build -d
docker compose logs -f playlist-bot
```

Keep `.env` private. The CI workflow runs unit tests and builds the container image.

## Data model and access control

- Telegram user IDs are the external user identity. PostgreSQL stores them as `BIGINT`; SQLite's signed 64-bit integer supports the same values.
- Library membership, playlists, and playlist items are owner-scoped. Every read, update, deletion, rating, note, tag, and reference lookup verifies library ownership; a track may be in the library without belonging to any playlist.
- `telegram_source_references` stores message IDs, chat IDs, media descriptors, and availability state. A track may have multiple references; an exact Telegram file unique ID or MusicBrainz recording ID can reuse an existing catalog track without title-only matching.
- Track metadata overrides, ratings, notes, and tags are user-private. Tags are owner-scoped and are included in that user's library search only.
- Provider-only results are stored separately as metadata-only records and never acquire a fabricated Telegram reference. Sending the audio later adds a playable library track; it does not automatically merge the metadata-only record.
- Copying does not expose source attribution. `TelegramDeliveryService` can be configured to use forwarding instead. The original source message must remain accessible to the bot; Telegram privacy, channel permissions, deletion, and Bot API restrictions may prevent retrieval. References are not guaranteed to work forever.
- Shared archive support is optional and stores archive locations separately from user tracks; it does not download media or store audio binaries. Archived messages are deduplicated by Telegram `file_unique_id` within this bot's database, and remain registered when a user's track is removed.

The application service in `archive.py` depends on a repository protocol, not Telegram update objects. `app/database/runtime.py` routes library and playlist workflows to the normalized `LibraryRepository`; legacy adapters retain transient bot state and compatibility tables. Telegram-specific polling and delivery are in `bot.py` and `telegram_delivery.py`.

## Architecture and roadmap

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the inspected architecture,
[docs/DATABASE.md](docs/DATABASE.md) for the current schema and migration-safe
target model, and [docs/ROADMAP.md](docs/ROADMAP.md) for staged development.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover migrations, collection ownership, multiple/unavailable source references, metadata-only entries, ratings/notes/tags, MusicBrainz matching, YouTube Music result normalization, Telegram delivery policy, callback flows, and bounded collection playback. A shared repository contract runs against SQLite locally and against PostgreSQL in CI. Telegram and metadata-provider network calls are mocked.

## Limitations

- Metadata-only tracks are not playable. If the user later sends the audio, that creates a separate playable library track. YouTube Music is used for text discovery; MusicBrainz is used only for optional audio metadata enrichment.
- Collection playback is sequential (or shuffled) and capped at 20 tracks per request; it is not a streaming queue.
- Message references can become unusable when the source message is deleted, inaccessible, or otherwise restricted by Telegram. The bot reports retrieval failures and retains track metadata so another source message can be linked.
- Group chats are intentionally ignored.
