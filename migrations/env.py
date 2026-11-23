from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import pool

from app.database.engine import create_database_engine, normalize_database_url
from app.database.models import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
LEGACY_TABLES = {
    "metadata_only_tracks",
    "pending_tracks",
    "playlists",
    "tags",
    "telegram_message_references",
    "track_tags",
    "tracks",
    "user_settings",
    "user_states",
}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL before running Alembic.")
    return normalize_database_url(url)


def _compare_type(
    _context: object,
    _inspected_column: object,
    metadata_column: object,
    _inspected_type: object,
    _metadata_type: object,
) -> bool | None:
    if (
        getattr(getattr(metadata_column, "table", None), "name", None) == "users"
        and getattr(metadata_column, "name", None) == "created_at"
    ):
        return False
    return None


def _include_object(
    _object: object,
    name: str,
    object_type: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    if (
        object_type == "column"
        and getattr(getattr(_object, "table", None), "name", None) == "users"
        and name == "telegram_user_id"
    ):
        return False
    if (
        object_type == "table"
        and reflected
        and compare_to is None
        and name in LEGACY_TABLES
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=_compare_type,
        include_object=_include_object,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_database_engine(
        _database_url(),
        poolclass=pool.NullPool,
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=_compare_type,
                include_object=_include_object,
                render_as_batch=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
