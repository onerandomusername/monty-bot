from __future__ import annotations

import dataclasses
import inspect
from typing import Literal, Optional, Union

import disnake
import sqlalchemy as sa
from disnake.ext import commands

from monty import constants
from monty.bot import Monty
from monty.config_metadata import METADATA, ConfigAttrMetadata
from monty.database import GuildConfig
from monty.errors import BotAccountRequired
from monty.log import get_logger
from monty.utils.messages import DeleteButton


GITHUB_REQUEST_HEADERS = {}
if GITHUB_TOKEN := constants.Tokens.github:
    GITHUB_REQUEST_HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


logger = get_logger(__name__)


def get_locale_from_dict(
    locales: Union[disnake.Locale, list[disnake.Locale | Literal[None]]],
    table: dict[disnake.Locale | Literal["_"], str],
) -> Optional[str]:
    """Get the first string out of table that matches a locale. Defaults to en_GB if no locale can be found."""
    if isinstance(locales, disnake.Locale):
        locales = [locales]
    for locale in locales:
        if locale in table:
            return table[locale]
    return table[disnake.Locale.en_GB]


def get_localised_response(inter: disnake.ApplicationCommandInteraction, text: str, **kwargs) -> str:
    """For the provided string, add the correct localised option names based on the interaction's locales."""
    for name, content in kwargs.items():
        if isinstance(content, dict):
            content = get_locale_from_dict([inter.locale, inter.guild_locale], content)
            kwargs[name] = content

    return text.format(**kwargs)


async def can_guild_set_config_option(bot: Monty, *, metadata: ConfigAttrMetadata, guild_id: int) -> bool:
    """Returns True if the configuration option is settable, False if a feature is required."""
    if metadata.depends_on_features:
        for feature in metadata.depends_on_features:
            if not await bot.guild_has_feature(guild_id, feature, create_if_not_exists=False):
                return False
    return True


@commands.register_injection
async def config_option(inter: disnake.ApplicationCommandInteraction, option: str) -> tuple[str, ConfigAttrMetadata]:
    """
    Get a valid configuration option and its metadata.

    Parameters
    ----------
    option: The configuration option to act on.
    """
    if option in METADATA:
        meta = METADATA[option]
        config_available = await can_guild_set_config_option(inter.bot, metadata=meta, guild_id=inter.guild_id)
        if not config_available:
            raise commands.UserInputError("Could not find a configuration option with that name.")

        return option, meta
    # attempt to see if the user provided a name directly
    option = option.lower()
    for attr, meta in METADATA.items():  # noqa: B007
        if isinstance(meta.name, dict):
            name = meta.name.get(inter.locale) or meta.name.get(inter.guild_locale) or meta.name[disnake.Locale.en_GB]
        else:
            name = meta.name
        if option == name.lower():
            break
    else:
        raise commands.UserInputError("Could not find a configuration option with that name.")

    if await can_guild_set_config_option(inter.bot, metadata=meta, guild_id=inter.guild_id):
        return attr, meta

    raise commands.UserInputError("Could not find a configuration option with that name.")


class Configuration(
    commands.Cog,
    name="Config Manager",
    slash_command_attrs={
        "dm_permission": False,
        "default_member_permissions": disnake.Permissions(manage_guild=True),
    },
):
    """Configuration management for each guild."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @commands.Cog.listener("on_guild_remove")
    async def remove_config_on_guild_remove(self, guild: disnake.Guild) -> None:
        """Delete the config as soon as we leave a guild."""
        async with self.bot.db.begin() as session:
            stmt = sa.delete(GuildConfig).where(GuildConfig.id == guild.id, GuildConfig.guild_id == guild.id)
            result = await session.execute(stmt)
            if result.rowcount != 1:
                logger.info(f"guild config doesn't exist for guild_id {guild.id}")
                return
            await session.commit()
            # also remove it from the cache
            try:
                del self.bot.guild_configs[guild.id]
            except KeyError:
                pass

    def require_bot(self, inter: disnake.Interaction) -> Literal[True]:
        """Raise an error if the bot is required."""
        if inter.guild_id and inter.guild:
            return True

        if not inter.guild_id:
            raise commands.NoPrivateMessage()

        invite = disnake.utils.oauth_url(
            self.bot.user.id,
            disable_guild_select=True,
            guild=disnake.Object(inter.guild_id),
            scopes={"applications.commands", "bot"},
            permissions=self.bot.invite_permissions,
        )
        msg = (
            "This command cannot be used without the full bot in this server.\n"
            f"You can invite the full bot by [clicking here](<{invite}>)."
        )
        raise BotAccountRequired(msg)

    @commands.slash_command()
    async def config(self, inter: disnake.GuildCommandInteraction) -> None:
        """[BETA] Manage per-guild configuration for Monty."""
        pass

    @config.sub_command("edit")
    async def set_command(
        self,
        inter: disnake.GuildCommandInteraction,
        option: str,
        value: str,
    ) -> None:
        """
        [BETA] Edit the specified config option to the provided value.

        Parameters
        ----------
        option: The configuration option to change.
        value: The new value of the configuration option.
        """
        option_name, metadata = await config_option(inter, option=option)
        config = await self.bot.ensure_guild_config(inter.guild_id)

        old = getattr(config, option_name)

        if metadata.requires_bot:
            self.require_bot(inter)

        try:
            # convert the value with the metadata.type
            param = inspect.Parameter(option_name, kind=inspect.Parameter.KEYWORD_ONLY)
            value = await commands.run_converters(inter, metadata.type, value, param)  # type: ignore
            setattr(config, option_name, value)
        except (TypeError, ValueError) as e:
            err = get_localised_response(inter, metadata.status_messages.set_attr_fail, name=metadata.name, err=str(e))
            raise commands.BadArgument(err) from None
        except commands.UserInputError as e:
            err = get_localised_response(inter, metadata.status_messages.set_attr_fail, name=metadata.name, err=str(e))
            raise e

        if validator := metadata.validator:
            try:
                if inspect.iscoroutinefunction(validator):
                    value = await validator(inter, value)
                else:
                    value = validator(inter, value)
            except Exception:
                # reset the configuration
                setattr(config, option_name, old)
                raise
        async with self.bot.db.begin() as session:
            config = await session.merge(config)
            await session.commit()

        response = get_localised_response(
            inter,
            metadata.status_messages.set_attr_success,
            name=metadata.name,
            old_setting=old,
            new_setting=value,
        )
        await inter.response.send_message(
            response,
            ephemeral=True,
        )

    @config.sub_command("view")
    async def view_command(
        self, inter: disnake.GuildCommandInteraction, option: tuple[str, ConfigAttrMetadata]
    ) -> None:
        """
        [BETA] View the current config for a config option.

        Parameters
        ----------
        The config option see what's currently set.
        """
        config = await self.bot.ensure_guild_config(inter.guild_id)
        option_name, metadata = option
        current = getattr(config, option_name)

        response = get_localised_response(
            inter,
            (
                metadata.status_messages.view_attr_success
                if current is not None
                else metadata.status_messages.view_attr_success_unset
            ),
            name=metadata.name,
            current_setting=current,
        )
        await inter.response.send_message(
            response,
            ephemeral=True,
        )

    @config.sub_command("reset")
    async def clear_command(
        self, inter: disnake.GuildCommandInteraction, option: tuple[str, ConfigAttrMetadata]
    ) -> None:
        """
        [BETA] Reset the config for a config option to the default.

        Parameters
        ----------
        The config option to unset/change to default.
        """
        option_name, metadata = option
        config = await self.bot.ensure_guild_config(inter.guild_id)
        current = getattr(config, option_name)
        if current is None:
            await inter.response.send_message("This option is already unset.", ephemeral=True)
            return

        fields = dataclasses.fields(config)
        for field in fields:
            if field.name == option_name:
                break
        else:
            raise RuntimeError("Could not find the config field for the specified option.")

        try:
            setattr(config, option_name, field.default)
        except (TypeError, ValueError):
            raise commands.BadArgument("This option is not clearable.") from None

        async with self.bot.db.begin() as session:
            config = await session.merge(config)
            await session.commit()

        text = (
            metadata.status_messages.clear_attr_success_with_default
            if field.default is not None
            else metadata.status_messages.clear_attr_success
        )
        response = get_localised_response(inter, text, name=metadata.name, default=field.default)
        await inter.response.send_message(
            response,
            ephemeral=True,
        )

    @set_command.autocomplete("value")
    async def set_value_autocomplete(
        self,
        inter: disnake.CommandInteraction,
        value: str,
        *,
        option: str = None,
    ) -> Union[dict[str, str], list[str]]:
        """Show autocomplete for setting a config option."""
        if not option:
            return ["Please fill out the option parameter with a valid option."]

        try:
            metadata = METADATA[option]
        except KeyError:
            try:
                _, metadata = await config_option(inter, option=option)
            except commands.UserInputError:
                return ["Please fill out the option parameter with a valid option."]

        if metadata.type is bool:
            return {
                "Enabled": "True",
                "Disabled": "False",
                get_localised_response(inter, "{name}", name=metadata.description): "_",
            }

        return [value or get_localised_response(inter, "{name}", name=metadata.description)]

    @set_command.autocomplete("option")
    @clear_command.autocomplete("option")
    @view_command.autocomplete("option")
    async def config_autocomplete(
        self,
        inter: disnake.CommandInteraction,
        option: str,
    ) -> Union[dict[str, str], list[str]]:
        """Provide autocomplete for config options."""
        # todo: make this better and not like this
        # todo: support non-nullable names (maybe a second autocomplete)
        # (the above are currently not implemented as there aren't many options yet
        # and all of them are nullable)
        options = {}
        for attr, metadata in METADATA.items():
            # feature lockout of configuration options
            if not await can_guild_set_config_option(self.bot, metadata=metadata, guild_id=inter.guild_id):
                continue
            if isinstance(metadata.name, dict):
                name = get_localised_response(inter, "{name}", name=metadata.name)
            else:
                name = metadata.name
            options[name] = attr

        if option:
            option = option.lower()
            for name in options.copy():
                if option not in name.lower():
                    options.pop(name)

        return dict(sorted(options.items())[:25])

    @commands.guild_only()
    @commands.command(name="prefix", hidden=True)
    async def show_prefix(self, ctx: commands.Context) -> None:
        """Show the currently set prefix for the guild. To set a prefix, use `/config prefix set`."""
        config = await self.bot.ensure_guild_config(ctx.guild.id)  # type: ignore # checks prevent guild from being None
        components = DeleteButton(ctx.author, initial_message=ctx.message)
        if config.prefix:
            await ctx.send(
                f"The currently set prefix is ``{config.prefix}``",
                allowed_mentions=disnake.AllowedMentions.none(),
                components=components,
            )
            return

        await ctx.send(
            f"There is no set prefix, using the default prefix: ``{self.bot.command_prefix}``", components=components
        )
        return


def setup(bot: Monty) -> None:
    """Add the configuration cog to the bot."""
    bot.add_cog(Configuration(bot))
