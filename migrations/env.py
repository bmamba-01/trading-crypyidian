import asyncio
import os
from pathlib import Path

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_engine_from_config

from trading_bot.adapters.database.models import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    configured_url = os.getenv("TRADING_BOT_DATABASE_URL")
    if configured_url:
        return configured_url

    password_file = os.getenv("TRADING_BOT_DATABASE_PASSWORD_FILE")
    password = Path(password_file).read_text(encoding="utf-8").strip() if password_file else None
    url = URL.create(
        "postgresql+psycopg",
        username=os.getenv("TRADING_BOT_DATABASE_USER", "crypidian_admin"),
        password=password,
        host=os.getenv("TRADING_BOT_DATABASE_HOST", "postgres"),
        database=os.getenv("TRADING_BOT_DATABASE_NAME", "crypidian"),
    )
    return url.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(run_sync_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
