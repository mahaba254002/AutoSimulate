"""
Alembic environment script.

Reads the DB URL from alpha_platform.config.settings (which itself reads
.env) instead of alembic.ini, so credentials never live in a committed file.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# make the project root importable so `alpha_platform` resolves
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from alpha_platform.config.settings import get_settings  # noqa: E402
from alpha_platform.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# override the placeholder URL in alembic.ini with the real one from .env
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# used by `alembic revision --autogenerate`
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (`alembic upgrade head --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate_connection(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      version_table_schema=config.attributes.get("version_table_schema"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Use a supplied test connection, or the configured database for normal CLI use."""
    supplied = config.attributes.get("connection")
    if supplied is not None:
        migrate_connection(supplied)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        migrate_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
