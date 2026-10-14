import os
import sys
from pathlib import Path

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make spindle_core importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spindle_core.persistence.database import Base
from spindle_core.persistence.models import SagaModel, OutboxModel  # ensure models registered


def _database_url() -> str:
    """Read the migration target from the environment, with no fallback.

    A hardcoded default here would let a misconfigured shell run migrations
    against the wrong database. Settings is deliberately not used: it requires
    auth variables a migration should not need.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set; refusing to run migrations without an explicit target database."
        )
    return url


DATABASE_URL = _database_url()

config = context.config
# ConfigParser treats '%' as interpolation, so escape it for URL-encoded passwords.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
