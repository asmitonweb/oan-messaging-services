"""Alembic environment. Uses the synchronous psycopg driver with the URL from
``OAN_MSG_DATABASE_URL`` (or ``config.attributes["database_url"]`` when invoked from code).
``target_metadata`` enables ``alembic check`` to prove the migration matches the schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from oan_messaging.storage.schema import metadata

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("OAN_MSG_DATABASE_URL")
    if not url:
        raise RuntimeError("set OAN_MSG_DATABASE_URL")
    return str(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
