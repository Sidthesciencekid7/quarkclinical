import asyncio
import os
from logging.config import fileConfig
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine
from alembic import context
from database import Base
import models  # noqa: F401 — registers all models with Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini hardcodes a local-dev connection string (there's no clean way to
# env-substitute inside a committed .ini). Prefer the real DATABASE_URL env
# var when present — set on every real deployment — so migrations always run
# against the actual target database instead of silently hitting localhost.
# NOTE: config.set_main_option() runs the value through configparser's %-style
# interpolation, which chokes on a literal "%40" (URL-encoded @) in the
# password — so the override is applied directly at engine-creation time
# instead (below), never round-tripped through configparser.
_env_db_url = os.getenv("DATABASE_URL")

target_metadata = Base.metadata

# Arbitrary fixed key for a Postgres session-level advisory lock, used below to
# serialize concurrent `alembic upgrade head` runs — e.g. multiple Uvicorn
# workers each independently calling this on boot (see main.py's
# _run_migrations_on_boot), racing against each other or against the
# Procfile's own explicit pre-start migration step. Session-level locks are
# auto-released if the holding connection drops, so a crashed migration never
# leaves this stuck held.
_MIGRATION_LOCK_KEY = 823914


def run_migrations_offline() -> None:
    url = _env_db_url or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    if _env_db_url:
        connectable = create_async_engine(_env_db_url, poolclass=pool.NullPool)
    else:
        connectable = async_engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    # Serialize concurrent `alembic upgrade head` runs (e.g. several Uvicorn
    # workers each calling this on boot — see main._run_migrations_on_boot).
    # A second caller blocks on the lock and proceeds once the first finishes.
    #
    # The lock MUST be held on its own dedicated connection, never the
    # migration connection. Acquiring it on the migration connection opens an
    # implicit outer transaction that alembic's own begin_transaction() then
    # nests inside; nothing commits that outer transaction, so on exit
    # SQLAlchemy rolls it back and SILENTLY DISCARDS the migration — alembic
    # still logs "Running upgrade X -> Y" and exits 0 while nothing persists.
    # That exact failure was observed here: 006 logged as applied, yet
    # alembic_version stayed 005 and the table was absent. Holding the lock on
    # a separate AUTOCOMMIT connection leaves the migration connection's
    # transaction handling byte-identical to the original un-locked code.
    lock_conn = await connectable.connect()
    await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        await lock_conn.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
        )
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        try:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
        finally:
            await lock_conn.close()

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
