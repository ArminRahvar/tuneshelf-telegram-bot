# TuneShelf Development Roadmap

This roadmap is based on the implementation inspected in this repository. Work
is incremental: do not claim a capability is complete until its behavior and
tests exist.

## Phase 1 — Core architecture and data model

### Assessment baseline

- Current architecture and backend differences documented.
- SQLite schema version 7 and legacy behavior identified.
- Existing unit tests run successfully: 56 tests on Python 3.13.17.

### Implementation status

1. Target ownership constraints are defined in the normalized model: playlist
   items must reference both an owner-matching playlist and the owner's library
   entry.
2. The additive Alembic schema and read-only inventory are in place. Startup
   imports existing personal-library rows in-place while preserving legacy
   tables.
3. The explicit SQLite/PostgreSQL transfer backfill records fingerprints and
   ID mappings, preserves user data and playlist order, and applies each import
   transactionally.
4. SQLite and PostgreSQL repository contracts cover the normalized library
   boundary; PostgreSQL execution runs in CI.
5. Saves, metadata, playlists, annotations/tags, delivery preferences, search,
   and playback use application-service or normalized-repository paths.
   Transient interaction state remains in the legacy adapters.
6. Core library workflows have switched after repository contract coverage;
   legacy tables remain for transient state and compatibility during cutover.

**Exit criteria:** legacy data is preserved, user isolation is tested at the
database and service layers, playlist membership no longer requires duplicate
track rows, and current bot workflows still pass.

## Phase 2 — Shared Telegram archive

- Optional `ARCHIVE_CHAT_ID` configures a private archive channel without
  committing credentials; new saved submissions are copied there and the
  returned chat/message references are associated with the user's track.
- Reuse an available archive message for matching Telegram `file_unique_id`
  values across users, and prefer the archive reference during playback.
- Failures to copy to the channel are surfaced without losing the original
  playable source.
- Startup applies additive Alembic migrations, imports legacy archive assets,
  and runtime adapters now read/write normalized `ArchiveAsset` rows separately
  from user-owned source-message references. Assets without legacy catalog
  metadata remain unlinked until a matching submission supplies it.
- Match duplicates by exact Telegram file unique ID first, then exact
  MusicBrainz recording ID; never match by title or artist alone.
- Test the flow with mocked Telegram calls; archive unavailability is tracked
  across references to the same Telegram message.

## Phase 3 — Personal library and playlists

- Audio submissions now create a private library entry by default; adding to a
  collection is optional. Startup backfills existing legacy libraries in-place
  and preserves the old tables.
- Playlist items are separate owner-scoped memberships with stable append
  ordering and duplicate prevention. Explicit reorder controls remain.
- Personal title/performer overrides, tags, ratings, and notes are scoped to the
  owner; removing a playlist item leaves the library entry intact.
- Normalized library search remains paginated and filterable, with ownership
  behavior covered by SQLite tests and the PostgreSQL contract used in CI.

## Phase 4 — Music discovery

- Provider interfaces and normalized search/metadata results are defined.
- YouTube Music song search uses ytmusicapi; selected results are saved as
  metadata-only entries and are never treated as playable archive assets.
- Evaluate MusicBrainz enrichment and matching independently from persistence.
- Provider normalization and bot selection are tested with mocked network
  responses; live provider integration remains environment-dependent.

## Phase 5 — Download system
- Additive `download_jobs` migration and normalized SQLAlchemy model provide
  PostgreSQL/SQLite job and batch persistence with explicit lifecycle states.
- SQLite/PostgreSQL repository operations support enqueue, batch enqueue,
  duplicate-active-job prevention, status/progress updates, claiming,
  cancellation, and retry.
- yt-dlp and FFmpeg adapters are isolated behind downloader/processor
  protocols and the synchronous download service processes one job at a time
  while reporting lifecycle and progress.
- Repository lifecycle and fresh-database migration tests are in place.
- A daemon download worker now runs beside Telegram polling and processes jobs
  outside the update loop; bot/menu controls support queue, status, cancel,
  and retry.
- Remaining: wire Telegram archive/library registration,
  registration, and bot status/cancellation controls.

## Phase 6 — Deployment and reliability

- Add PostgreSQL integration and multi-user isolation tests to CI.
- Add structured operational logging, backup/restore guidance, and migration
  runbooks.
- Validate container startup, health checks, and deployment rollback.
- Add lint/type checks only with agreed repository tooling and scope.
