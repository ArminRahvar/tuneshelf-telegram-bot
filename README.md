# Personal Music Archive Bot

A private-chat Telegram bot for organizing a personal music archive. Audio remains in Telegram: the application does not call `getFile`, download media, or write audio binaries to its database or filesystem. Saved playback copies the original Telegram message with `copyMessage` by default; delivery policy is isolated in `telegram_delivery.py`.

## Features

- Save Telegram audio messages, voice messages, and audio MIME-type documents with their original chat/message reference.
- Preserve Telegram file identifiers and available title, artist, duration, MIME type, and filename metadata. `file_unique_id` is used only as a duplicate hint; it is not used for playback.
- Keep multiple source-message references per saved track, mark failed references unavailable without deleting metadata, and use `/link` to associate another Telegram copy with a saved track.
- Create, rename, browse, search, play, and delete private playlists/collections; add an existing track to another collection with `/addtrack`. Collection delivery is capped at 20 Telegram messages per request, paced between messages, and retries Telegram rate-limit responses once.
- Edit metadata and manage private ratings, notes, and tags with `/edit`, `/rate`, `/note`, `/tag`, and `/untag`.
- Use both a persistent reply keyboard and inline keyboards for Add Music, Library, Search, Collections, Discover, Statistics, and Settings. Library track buttons open details and provide inline controls for rating, notes, tags, metadata edits, and linking another Telegram source.
- Search the private library by title, artist, album, collection, tags, rating, release year, or playability; filter with terms such as `rating:5 year:2024 playable:true` or `metadata:true`.
- View per-user library statistics and recommendations based only on matching artists and manually assigned tags.
- Search MusicBrainz by text and supported MusicBrainz recording URLs. Select between multiple confident matches. Provider-only matches are stored as metadata-only tracks and are clearly not playable until an audio message is saved.
- Use an application/domain service in `archive.py` that does not depend on Telegram handlers. Persistence is available through the local SQLite adapter and the PostgreSQL adapter.

The bot ignores chats other than private chats. `/start` and `/help` open the main menu. Track IDs are shown in the library and collections. Most common workflows are available through reply and inline buttons; commands remain available for direct use and compatibility.

## Run locally

Python 3.10 or newer is required. SQLite is used when `DATABASE_URL` is not set, so the local test/development workflow needs no third-party package:

```sh
export BOT_TOKEN='your-token-from-botfather'
export DATABASE_PATH='./playlist_bot.sqlite3'  # optional; this is the default
python3 bot.py
```

For local PostgreSQL instead, install the PostgreSQL driver and set a connection URL:

```sh
python3 -m pip install -r requirements.txt
export DATABASE_URL='postgresql://playlist_bot:password@localhost:5432/playlist_bot'
python3 bot.py
```

The PostgreSQL database/user must already exist. The bot applies its idempotent schema setup at startup. SQLite schema migrations use `PRAGMA user_version` and are also applied automatically.

Setting `DATABASE_URL` switches backends; existing SQLite data is not copied automatically. Keep a backup and migrate/export any existing SQLite library before switching a populated installation to PostgreSQL.

MusicBrainz enrichment/search is optional. Configure a contact email for its User-Agent:

```sh
export MUSICBRAINZ_CONTACT_EMAIL='you@example.com'
```

When enabled, sending a text query or a `musicbrainz.org/recording/<MBID>` URL searches/saves metadata. The MusicBrainz integration uses a descriptive User-Agent, spaces requests, and requires a confident match. It does not provide playable audio.

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
- Playlists/collections and their tracks are owner-scoped. Every read, update, deletion, rating, note, tag, and reference lookup verifies ownership through the playlist/user relationship.
- `telegram_message_references` stores message IDs, chat IDs, media descriptors, and availability state. A track may have multiple references; the same audio `file_unique_id` in a collection is attached as another reference instead of creating a duplicate entry.
- Track ratings, notes, and tags are user-private. Tags are owner-scoped and are included in that user's library search only.
- Provider-only results are stored separately as metadata-only records and never acquire a fabricated Telegram reference. Sending the audio later adds a playable library track; it does not automatically merge the metadata-only record.
- Copying does not expose source attribution. `TelegramDeliveryService` can be configured to use forwarding instead. The original source message must remain accessible to the bot; Telegram privacy, channel permissions, deletion, and Bot API restrictions may prevent retrieval. References are not guaranteed to work forever.

The normalized application service in `archive.py` depends on a repository protocol, not Telegram update objects. Telegram-specific polling and delivery are in `bot.py` and `telegram_delivery.py`; persistence adapters are in `database.py` and `postgres_database.py`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover migrations, collection ownership, multiple/unavailable source references, metadata-only entries, ratings/notes/tags, MusicBrainz matching and URL validation, Telegram delivery policy, callback flows, and bounded collection playback. Telegram and MusicBrainz network calls are mocked; a live PostgreSQL server is needed to integration-test the PostgreSQL adapter.

## Limitations

- Metadata-only tracks are not playable. If the user later sends the audio, that creates a separate playable library track. MusicBrainz is the only external metadata provider currently supported.
- Collection playback is sequential (or shuffled) and capped at 20 tracks per request; it is not a streaming queue.
- Message references can become unusable when the source message is deleted, inaccessible, or otherwise restricted by Telegram. The bot reports retrieval failures and retains track metadata so another source message can be linked.
- Group chats are intentionally ignored.
