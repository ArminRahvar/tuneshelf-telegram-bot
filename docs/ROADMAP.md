# TuneShelf Development Roadmap

This roadmap is based on the implementation inspected in this repository. Work
is incremental: do not claim a capability is complete until its behavior and
tests exist.

## Phase 1 — Core architecture and data model

### Assessment baseline

- Current architecture and backend differences documented.
- SQLite schema version 7 and legacy behavior identified.
- Existing unit tests run successfully: 56 tests on Python 3.13.17.

### Next implementation milestones

1. Target ownership constraints are defined in the normalized model: playlist
   items must reference both an owner-matching playlist and the owner's library
   entry.
2. SQLAlchemy models and an additive Alembic schema are in place. The bot still
   uses its legacy repositories, and this migration does not backfill data.
3. Build a reversible, tested migration/backfill path from existing SQLite and
   PostgreSQL data; preserve current adapters until parity is established.
4. Add repository parity tests and expand ownership/migration coverage.
5. Extract library/playlist use cases from `bot.py` behind repository/service
   interfaces without changing Telegram UX.
6. Switch individual workflows only after their backend parity tests pass.

**Exit criteria:** legacy data is preserved, user isolation is tested at the
database and service layers, playlist membership no longer requires duplicate
track rows, and current bot workflows still pass.

## Phase 2 — Shared Telegram archive

- Configure a private archive channel without committing credentials.
- Add an archive integration that uploads new submissions and records archive
  chat/message references.
- Separate archive assets from user source-message references and library
  membership.
- Add conservative duplicate matching with stable external IDs first; do not
  rely on title-only matching.
- Reuse a valid archive asset, handle inaccessible/deleted messages, and test
  the flow with mocked Telegram calls.

## Phase 3 — Personal library and playlists

- Add private library membership independent from playlists.
- Implement playlist membership, stable ordering/reordering, and duplicate
  prevention.
- Keep personal tags, ratings, and notes isolated by user.
- Add paginated search/filtering and multi-user isolation tests.

## Phase 4 — Music discovery

- Define provider interfaces and normalized search/metadata results.
- Integrate ytmusicapi without treating metadata search results as playable
  archive assets.
- Evaluate MusicBrainz enrichment and matching independently from persistence.
- Add provider tests with mocked network responses.

## Phase 5 — Download system

- Add a PostgreSQL-backed job and batch model with explicit lifecycle states.
- Integrate yt-dlp and FFmpeg behind downloader/processor interfaces.
- Process authorized content in background workers; do not block Telegram
  update handling.
- Add retry, cancellation, duplicate-job prevention, progress reporting, and
  archive/library registration tests.

## Phase 6 — Deployment and reliability

- Add PostgreSQL integration and multi-user isolation tests to CI.
- Add structured operational logging, backup/restore guidance, and migration
  runbooks.
- Validate container startup, health checks, and deployment rollback.
- Add lint/type checks only with agreed repository tooling and scope.
