import asyncio
import collections
import dataclasses
import functools
import socket
import sys
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional, Union
from unittest.mock import Mock
from weakref import WeakValueDictionary

import aiohttp
import arrow
import cachingutils.redis
import disnake
import multidict
import redis
import redis.asyncio
import sqlalchemy as sa
import yarl
from disnake.ext import commands
from multidict import CIMultiDict, CIMultiDictProxy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from monty import constants
from monty.database import Feature, Guild, GuildConfig
from monty.database.rollouts import Rollout
from monty.log import get_logger
from monty.statsd import AsyncStatsClient
from monty.utils import rollouts, scheduling
from monty.utils.caching import RedisCache
from monty.utils.extensions import EXTENSIONS, walk_extensions


log = get_logger(__name__)

try:
    import dotenv
except ModuleNotFoundError:
    TEST_GUILDS = None
else:
    TEST_GUILDS = dotenv.get_key(".env", "TEST_GUILDS")
    if TEST_GUILDS:
        TEST_GUILDS = [int(x.strip()) for x in TEST_GUILDS.split(",")]
        log.info("TEST_GUILDS FOUND")


__all__ = ("Monty",)


class Monty(commands.Bot):
    """
    Base bot instance.

    While in debug mode, the asset upload methods (avatar, banner, ...) will not
    perform the upload, and will instead only log the passed download urls and pretend
    that the upload was successful. See the `mock_in_debug` decorator for further details.
    """

    name = constants.Client.name

    def __init__(self, redis_session: redis.asyncio.Redis, proxy: str = None, **kwargs) -> None:
        if TEST_GUILDS:
            kwargs["test_guilds"] = TEST_GUILDS
            log.warn("registering as test_guilds")

        if proxy:
            kwargs["proxy"] = proxy  # pass proxy to disnake client
            if "connector" not in kwargs:
                kwargs["connector"] = self.create_connector(proxy=proxy)

        super().__init__(**kwargs)

        self.redis_session = redis_session
        self.redis_cache = cachingutils.redis.async_session(constants.Client.redis_prefix, session=self.redis_session)
        self.redis_cache_key = constants.Client.redis_prefix

        self.create_http_session(proxy=proxy)

        self.db_engine = engine = create_async_engine(constants.Database.postgres_bind)
        self.db_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        self.guild_configs: dict[int, GuildConfig] = {}
        self.guild_db: dict[int, Guild] = {}
        self.features: dict[str, Feature] = {}
        self._feature_db_lock = asyncio.Lock()
        self._guild_db_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

        self.socket_events = collections.Counter()
        self.start_time: arrow.Arrow
        self.stats: AsyncStatsClient
        self.command_prefix: str
        self.invite_permissions: disnake.Permissions = constants.Client.invite_permissions
        scheduling.create_task(self.get_self_invite_perms())
        scheduling.create_task(self._create_features())

    @property
    def db(self) -> async_sessionmaker[AsyncSession]:
        """Alias of `bot.db_session`."""
        return self.db_session

    def create_http_session(self, proxy: str = None) -> None:
        """Create the aiohttp session and set the trace logger, if desired."""
        trace_configs = []

        aiohttp_log = get_logger(__package__ + ".http")

        async def on_request_end(
            session: aiohttp.ClientSession,
            ctx: SimpleNamespace,
            end: aiohttp.TraceRequestEndParams,
        ) -> None:
            """Log all aiohttp requests on request end."""
            resp = end.response
            aiohttp_log.info(
                "[{status!s} {reason!s}] {method!s} {url!s} ({content_type!s})".format(
                    status=resp.status,
                    reason=resp.reason,
                    method=end.method.upper(),
                    url=end.url,
                    content_type=resp.content_type,
                )
            )

        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_end.append(on_request_end)
        trace_configs.append(trace_config)

        # dead simple ETag caching
        cache = RedisCache(
            "aiohttp_requests",
            timeout=timedelta(days=5),
        )
        _og_request = aiohttp.ClientSession._request
        cache_logger = get_logger("monty.utils.caching.http")

        async def _request(
            self: aiohttp.ClientSession,
            method: str,
            str_or_url: Any,
            use_cache: bool = True,
            **kwargs,
        ) -> aiohttp.ClientResponse:
            """Do the same thing as aiohttp does, but always cache the response."""
            method = method.upper().strip()
            cache_key = f"{method}:{str(str_or_url)}"
            async with cache.lock(cache_key):
                cached = await cache.get(cache_key)
                if cached and use_cache:
                    etag, body, resp_headers = cached
                    if etag:
                        kwargs.setdefault("headers", {})["If-None-Match"] = etag
                else:
                    etag = None
                    body = None
                    resp_headers = None

                r = await _og_request(self, method, str_or_url, **kwargs)
                if not use_cache:
                    return r
                if r.status == 304:
                    cache_logger.debug("HTTP Cache hit on %s", cache_key)
                    # decode the original headers
                    headers: CIMultiDict[str] = CIMultiDict()
                    for key, value in resp_headers:
                        headers[key.decode()] = value.decode()
                    r._cache["headers"] = r._headers = CIMultiDictProxy(headers)
                    r.content = reader = aiohttp.StreamReader(
                        protocol=Mock(_reading_paused=False),
                        limit=len(body),
                    )
                    reader.feed_data(body)
                    reader.feed_eof()
                    r.status = 200
                    return r

                etag = r.headers.get("ETag")
                # only cache if etag is provided and the request was in the 200
                if etag and 200 <= r.status < 300:
                    body = await r.read()
                    await cache.set(cache_key, (etag, body, r.raw_headers))
                return r

        user_agent = "Python/{0[0]}.{0[1]} Monty-Python/{1} ({2})".format(
            sys.version_info, constants.Client.version, constants.Source.github
        )

        # this is also used by gql
        self.http_request_class = self._create_http_request_class(proxy=proxy)

        self.http_session = aiohttp.ClientSession(
            connector=self.create_connector(proxy=proxy),
            request_class=self.http_request_class,
            trace_configs=trace_configs,
            headers=multidict.CIMultiDict({"User-agent": user_agent}),
        )
        self.http_session._request = functools.partial(_request, self.http_session)

    def create_connector(self, proxy: str = None) -> aiohttp.BaseConnector:
        """Create a TCPConnector, changing the ssl setting based on the proxy value."""
        return aiohttp.TCPConnector(
            resolver=aiohttp.AsyncResolver(),
            family=socket.AF_INET,
            ssl=not (proxy and proxy.startswith("http://")),
        )

    def _create_http_request_class(self, proxy: str = None) -> type[aiohttp.ClientRequest]:
        """Create a ClientRequest type, which inserts the proxy into every request's args (if set)."""
        if not proxy:
            return aiohttp.ClientRequest  # default

        proxy_url = yarl.URL(proxy)
        verify_ssl = not proxy.startswith("http://")

        class ProxyClientRequest(aiohttp.ClientRequest):
            def __init__(self, *args: Any, **kwargs: Any):
                kwargs["proxy"] = proxy_url
                kwargs["ssl"] = verify_ssl
                super().__init__(*args, **kwargs)

        return ProxyClientRequest

    async def get_self_invite_perms(self) -> disnake.Permissions:
        """Sets the internal invite_permissions and fetches them."""
        await self.wait_until_first_connect()
        app_info = await self.application_info()
        if app_info.install_params:
            self.invite_permissions = app_info.install_params.permissions
        else:
            self.invite_permissions = constants.Client.invite_permissions
        return self.invite_permissions

    async def ensure_guild(self, guild_id: int, *, session: AsyncSession = None) -> Guild:
        """Fetch and return a guild config, creating if it does not exist."""
        guild = self.guild_db.get(guild_id)
        if not guild:
            lock = self._guild_db_locks.get(guild_id)
            if not lock:
                lock = self._guild_db_locks[guild_id] = asyncio.Lock()
            async with lock:
                # once again use the cache just in case
                guild = self.guild_db.get(guild_id)
                if not guild:
                    if not session:
                        session = self.db()
                    async with session.begin_nested() if session.in_transaction() else session.begin() as trans:
                        guild = await session.get(Guild, guild_id)
                        if not guild:
                            guild = Guild(id=guild_id)
                            session.add(guild)
                        await trans.commit()
                    self.guild_db[guild_id] = guild
        return guild

    async def ensure_guild_config(self, guild_id: int) -> GuildConfig:
        """Fetch and return a guild config, creating if it does not exist."""
        config = self.guild_configs.get(guild_id)
        if not config:
            async with self.db.begin() as session:
                guild = await self.ensure_guild(guild_id, session=session)
                config = await session.get(
                    GuildConfig, guild_id, options=[selectinload(GuildConfig.guild)]
                ) or GuildConfig(id=guild_id, guild=guild, guild_id=guild_id)
                session.add(config)
                await session.commit()
            self.guild_configs[guild_id] = config

        elif not config.guild:
            async with self.db.begin() as session:
                guild = await self.ensure_guild(guild_id, session=session)
                await session.merge(config)
                config.guild = guild
                await session.commit()

        return config

    async def get_prefix(self, message: disnake.Message) -> Optional[Union[list[str], str]]:
        """Get the bot prefix."""
        prefixes = commands.when_mentioned(self, message)
        if message.guild:
            config = await self.ensure_guild_config(message.guild.id)
            if config and config.prefix:
                prefixes.insert(0, config.prefix)
            else:
                prefixes.insert(0, self.command_prefix)
        return prefixes

    async def _create_features(self) -> None:
        """Update the database with all features defined immediately upon launch. No more lazy creation."""
        await self.wait_until_first_connect()

        async with self._feature_db_lock:
            async with self.db.begin() as session:
                stmt = sa.select(Feature).options(selectinload(Feature.rollout))
                result = await session.scalars(stmt)
                existing_feature_names = {feature.name for feature in result.all()}
                for feature_name in dataclasses.asdict(constants.Feature()).values():
                    if feature_name in existing_feature_names:
                        continue
                    feature_instance = Feature(feature_name)
                    session.add(feature_instance)
                await session.commit()  # this will error out if it cannot be made

        await self.refresh_features()

    async def refresh_features(self) -> None:
        """Refresh the feature cache."""
        async with self._feature_db_lock:
            async with self.db.begin() as session:
                stmt = sa.select(Feature).options(selectinload(Feature.rollout))
                result = await session.scalars(stmt)
                features = result.all()
            self.features.clear()
            self.features.update({feature.name: feature for feature in features})

        log.info("Fetched the features from the database.")

    async def guild_has_feature(
        self,
        guild: Optional[Union[int, disnake.abc.Snowflake]],
        feature: str,
        *,
        include_feature_status: bool = True,
        create_if_not_exists: bool = True,
    ) -> bool:
        """
        Return whether or not the provided guild has the provided feature.

        By default, this considers the feature's enabled status,
        which can be disabled with `include_feature_status` set to False.
        """
        # first create the feature if we are told to create it
        if feature in self.features:
            feature_instance = self.features[feature]
        else:
            # if its not cached
            # we don't need to seperate this based on features as we aren't creating features that often
            async with self._feature_db_lock:
                # attempt a fetch first
                # get from cached features once within the lock
                feature_instance = self.features.get(feature)
                if not feature_instance:
                    async with self.db.begin() as session:
                        feature_instance = await session.get(
                            Feature, feature, populate_existing=True, options=[selectinload(Feature.rollout)]
                        )
                        if not feature_instance and create_if_not_exists:
                            feature_instance = Feature(feature)
                            session.add(feature_instance)
                            await session.commit()  # this will error out if it cannot be made
                        if feature_instance:
                            self.features[feature] = feature_instance
        # we're defaulting to non-existing features as None, rather than False.
        # this might change later.
        if include_feature_status and feature_instance:
            if feature_instance.enabled is not None:
                return feature_instance.enabled

        # the feature's enabled status is None, so we should check the guild
        # support the guild being None to make it easier to use
        if guild is None:
            return False
        if not isinstance(guild, int):
            guild = guild.id

        guild_db = await self.ensure_guild(guild)
        if feature in guild_db.feature_ids:
            return True

        # check if this feature has an active rollout
        if feature_instance and feature_instance.rollout_id:
            async with self.db.begin() as session:
                rollout = await session.get(Rollout, feature_instance.rollout_id)
                if not rollout:
                    err = (
                        f"Database could not find rollout with ID {feature_instance.rollout_id} but feature"
                        f" {feature_instance.name} is bound to said rollout."
                    )
                    raise RuntimeError(err)

            return rollouts.is_rolled_out_to(guild, rollout=rollout)

        return False

    async def login(self, token: str) -> None:
        """Login to Discord and set the bot's start time."""
        self.start_time = arrow.utcnow()
        self.stats = AsyncStatsClient(
            host=constants.Stats.host,
            port=constants.Stats.port,
            prefix=constants.Stats.prefix,
        )
        return await super().login(token)

    async def close(self) -> None:
        """Close sessions when bot is shutting down."""
        await super().close()

        if self.http_session:
            await self.http_session.close()
        if self.db_engine:
            await self.db_engine.dispose()

        if self.redis_session:
            await self.redis_session.aclose(close_connection_pool=True)

        await asyncio.sleep(0.6)

    def load_extensions(self) -> None:
        """Load all extensions as released by walk_extensions()."""
        if constants.Client.extensions:
            log.warning("Not loading all extensions as per environment settings.")
        EXTENSIONS.update(walk_extensions())
        for ext, ext_metadata in walk_extensions():
            if not constants.Client.extensions:
                self.load_extension(ext)
                continue

            if ext_metadata.core or ext in constants.Client.extensions:
                self.load_extension(ext)
                continue
            log.debug(f"SKIPPING loading {ext} as per environment variables.")
        log.info("Completed loading extensions.")

    def add_cog(self, cog: commands.Cog, **kwargs: Any) -> None:
        """
        Delegate to super to register `cog`.

        This only serves to make the info log, so that extensions don't have to.
        """
        super().add_cog(cog, **kwargs)
        log.info(f"Cog loaded: {cog.qualified_name}")
        self.dispatch("cog_load", cog)

    def remove_cog(self, name: str) -> Optional[commands.Cog]:
        """Remove the cog from the bot and dispatch a cog_remove event."""
        cog = super().remove_cog(name)
        if cog is None:
            return None
        self.dispatch("cog_remove", cog)
        return cog

    def add_command(self, command: commands.Command) -> None:
        """Add `command` as normal and then add its root aliases to the bot."""
        super().add_command(command)
        self._add_root_aliases(command)
        self.dispatch("command_add", command)

    def remove_command(self, name: str) -> Optional[commands.Command]:
        """
        Remove a command/alias as normal and then remove its root aliases from the bot.

        This also dispatches the command_remove event.

        Individual root aliases cannot be removed by this function.
        To remove them, either remove the entire command or manually edit `bot.all_commands`.
        """
        command = super().remove_command(name)
        if command is None:
            # Even if it's a root alias, there's no way to get the Bot instance to remove the alias.
            return

        self.dispatch("command_remove", command)
        self._remove_root_aliases(command)
        return command

    def add_slash_command(self, slash_command: commands.InvokableSlashCommand) -> None:
        """Add the slash command to the bot and dispatch a slash_command_add event."""
        super().add_slash_command(slash_command)
        self.dispatch("slash_command_add", slash_command)

    def remove_slash_command(self, name: str) -> Optional[commands.InvokableSlashCommand]:
        """Remove the slash command from the bot and dispatch a slash_command_remove event."""
        slash_command = super().remove_slash_command(name)
        if slash_command is None:
            return None
        self.dispatch("slash_command_remove", slash_command)
        return slash_command

    async def on_command_error(self, context: commands.Context, exception: disnake.DiscordException) -> None:
        """Check command errors for UserInputError and reset the cooldown if thrown."""
        if isinstance(exception, commands.UserInputError) and context.command:
            context.command.reset_cooldown(context)
        else:
            await super().on_command_error(context, exception)  # type:ignore

    def _add_root_aliases(self, command: commands.Command) -> None:
        """Recursively add root aliases for `command` and any of its subcommands."""
        if isinstance(command, commands.Group):
            for subcommand in command.commands:
                self._add_root_aliases(subcommand)

        for alias in getattr(command, "root_aliases", ()):
            if alias in self.all_commands:
                raise commands.CommandRegistrationError(alias, alias_conflict=True)

            self.all_commands[alias] = command

    def _remove_root_aliases(self, command: commands.Command) -> None:
        """Recursively remove root aliases for `command` and any of its subcommands."""
        if isinstance(command, commands.Group):
            for subcommand in command.commands:
                self._remove_root_aliases(subcommand)

        for alias in getattr(command, "root_aliases", ()):
            self.all_commands.pop(alias, None)
