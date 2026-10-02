"""Alembic entry point: runs migrations against the configured database."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from parley.config import get_settings
from parley.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Tests pass their own URL through `sqlalchemy.url`; otherwise use the settings.
database_url = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations() -> None:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
