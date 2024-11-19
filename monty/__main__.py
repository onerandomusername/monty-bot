import asyncio
import logging
import os
import signal
import sys

import alembic.command
import alembic.config
import cachingutils
import cachingutils.redis
import disnake
import redis
import redis.asyncio
from disnake.ext import commands
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

import monty.alembic
from monty import constants, monkey_patches
from monty.bot import Monty


log = logging.getLogger(__name__)
_intents = disnake.Intents.all()
_intents.members = False
_intents.presences = False
_intents.bans = False
_intents.integrations = False
_intents.invites = False
_intents.typing = False
_intents.webhooks = False
_intents.voice_states = False


def run_upgrade(connection: AsyncConnection, cfg: alembic.config.Config) -> None:
    """Run alembic upgrades."""
    cfg.attributes["connection"] = connection
    alembic.command.upgrade(cfg, "head")


async def run_async_upgrade(engine: AsyncEngine) -> None:
    """Run alembic upgrades but async."""
    alembic_cfg = alembic.config.Config()
    alembic_cfg.set_main_option("script_location", os.path.dirname(monty.alembic.__file__))
    async with engine.connect() as conn:
        await conn.run_sync(run_upgrade, alembic_cfg)


async def run_alembic() -> None:
    """Run alembic migrations."""
    engine = create_async_engine(constants.Database.postgres_bind)
    await run_async_upgrade(engine)


async def main() -> None:
    """Create and run the bot."""
    disnake.Embed.set_default_colour(constants.Colours.python_yellow)
    monkey_patches.patch_typing()
    monkey_patches.patch_inter_send()

    # we make our redis session here and pass it to cachingutils
    if constants.RedisConfig.use_fakeredis:
        try:
            import fakeredis
            import fakeredis.aioredis
        except ImportError as e:
            raise RuntimeError("fakeredis must be installed to use fake redis") from e
        redis_session = fakeredis.aioredis.FakeRedis.from_url(constants.RedisConfig.uri)
    else:
        pool = redis.asyncio.BlockingConnectionPool.from_url(
            constants.RedisConfig.uri,
            max_connections=20,
            timeout=300,
        )
        redis_session = redis.asyncio.Redis(connection_pool=pool)

    cachingutils.redis.async_session(
        constants.Client.config_prefix, session=redis_session, prefix=constants.RedisConfig.prefix
    )

    # run alembic migrations
    if constants.Database.run_migrations:
        log.info(f"Running database migrations to target {constants.Database.migration_target}")
        await run_alembic()
    else:
        log.warning("Not running database migrations per environment settings.")

    # ping redis
    await redis_session.ping()

    command_sync_flags = commands.CommandSyncFlags(
        allow_command_deletion=False,
        sync_guild_commands=True,
        sync_global_commands=True,
        sync_commands_debug=True,
        sync_on_cog_actions=True,
    )
    bot = Monty(
        redis_session=redis_session,
        command_prefix=constants.Client.default_command_prefix,
        activity=disnake.Game(name=f"Commands: {constants.Client.default_command_prefix}help"),
        allowed_mentions=disnake.AllowedMentions(everyone=False),
        intents=_intents,
        command_sync_flags=command_sync_flags,
        proxy=constants.Client.proxy,
    )

    try:
        bot.load_extensions()
    except Exception:
        await bot.close()
        raise

    loop = asyncio.get_running_loop()

    future: asyncio.Future = asyncio.ensure_future(bot.start(constants.Client.token or ""), loop=loop)
    loop.add_signal_handler(signal.SIGINT, lambda: future.cancel())
    loop.add_signal_handler(signal.SIGTERM, lambda: future.cancel())
    try:
        await future
    except asyncio.CancelledError:
        log.info("Received signal to terminate bot and event loop.")
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
