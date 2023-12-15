from __future__ import annotations

import re
import typing as t
from datetime import datetime, timedelta, timezone
from ssl import CertificateError

import arrow
import disnake
import sqlalchemy as sa
import sqlalchemy.exc
from aiohttp import ClientConnectorError
from disnake.ext import commands

from monty import exts
from monty.bot import Monty
from monty.database import Feature, Rollout
from monty.log import get_logger
from monty.utils import inventory_parser
from monty.utils.extensions import EXTENSIONS, unqualify
from monty.utils.features import NAME_REGEX as FEATURE_NAME_REGEX


log = get_logger(__name__)

DISCORD_EPOCH_DT = disnake.utils.snowflake_time(0)
RE_USER_MENTION = re.compile(r"<@!?([0-9]+)>$")

AnyContext = t.Union[disnake.ApplicationCommandInteraction, commands.Context[Monty]]

TIMEDELTA_REGEX = re.compile(
    r"^"
    r"((?P<years>-?\d+)(?:Y|y|[Yy]ears?))?"
    r"((?P<months>-?\d+)(?:M|[Mm]onths?))?"
    r"((?P<weeks>-?\d+)(?:W|w|[Ww]eeks?))?"
    r"((?P<days>-?\d+)(?:D|d|[Dd]ays?))?"
    r"((?P<hours>-?\d+)(?:H|h|[Hh]ours?))?"
    r"((?P<minutes>-?\d+)(?:m|[Mm]inutes?))?"
    r"((?P<seconds>-?\d+)(?:S|s|[Ss]econds?))?"
    r"$",
)


def str_timedelta_from_now(human: str, /) -> t.Optional[timedelta]:
    """Convert a string to a timedelta relative to the current time."""
    match = TIMEDELTA_REGEX.fullmatch(human)
    if not match:
        return None

    parts = {k: int(v) for k, v in match.groupdict().items() if v}

    # to support years and months we have to make some assumptions about the current time
    # for that we can use arrow which does this for us.
    if "years" in parts or "months" in parts:
        now = arrow.utcnow()
        then = now.shift(**parts)
        return then - now

    return timedelta(**parts)


class ArrowConverter(commands.Converter):
    """
    Get a datetime argument out of the provided argument.

    This uses arrow and dateutil to maximize options.
    """

    async def convert(self, ctx: AnyContext, argument: str) -> arrow.Arrow:
        """Convert the provided argument into an arrow.Arrow object."""
        # first convert our provided match
        try:
            delta = str_timedelta_from_now(argument)
        except Exception:
            pass
        else:
            if delta is not None:
                return arrow.utcnow() + delta

        try:
            return arrow.get(argument)
        except Exception as e:
            raise commands.BadArgument(f"{argument} could not be converted into a valid datetime.") from e


class RolloutConverter(commands.Converter):
    """Convert the provided argument into a rollout."""

    async def convert(self, ctx: AnyContext, argument: str) -> Rollout:
        """Convert the provided argument into a rollout."""
        async with ctx.bot.db.begin() as session:
            stmt = sa.select(Rollout).where(Rollout.name == argument)
            result = await session.scalars(stmt)
            try:
                return result.one()
            except sqlalchemy.exc.NoResultFound:
                raise commands.BadArgument(f"`{argument}` is not a valid rollout name.") from None


class MaybeFeature(commands.Converter):
    """
    Match that the provided string is a valid Feature name.

    This does not check if the argument is a valid feature.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        """Check that the argument is a possible feature name."""
        # convert the name to uppercase for the benefit of the user and normalize `-` characters
        argument = argument.upper().replace("-", "_")
        match = FEATURE_NAME_REGEX.fullmatch(argument)
        if not match:
            raise commands.BadArgument(f"Feature name must match regex ``{FEATURE_NAME_REGEX.pattern}``.")
        return argument


class FeatureConverter(MaybeFeature):
    """
    Match that the provided string is a valid Feature name.

    This does not check if the argument is a valid feature.
    """

    async def convert(self, ctx: commands.Context[Monty], argument: str) -> Feature:
        """Check that the argument is a possible feature name."""
        # convert the name to uppercase for the benefit of the user and normalize `-` characters
        argument = await super().convert(ctx, argument)
        try:
            return ctx.bot.features[argument]
        except KeyError:
            raise commands.BadArgument(f"No feature with name `{argument}` exists.") from None


class Extension(commands.Converter):
    """
    Fully qualify the name of an extension and ensure it exists.

    The * and ** values bypass this when used with the reload command.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        """Fully qualify the name of an extension and ensure it exists."""
        # Special values to reload all extensions
        if argument == "*" or argument == "**":
            return argument

        argument = argument.lower()

        if argument in EXTENSIONS:
            return argument
        elif (qualified_arg := f"{exts.__name__}.{argument}") in EXTENSIONS:
            return qualified_arg

        matches = []
        for ext in EXTENSIONS:
            if argument == unqualify(ext):
                matches.append(ext)

        if len(matches) > 1:
            matches.sort()
            names = "\n".join(matches)
            raise commands.BadArgument(
                f":x: `{argument}` is an ambiguous extension name. "
                f"Please use one of the following fully-qualified names.```\n{names}```"
            )
        elif matches:
            return matches[0]
        else:
            raise commands.BadArgument(f":x: Could not find the extension `{argument}`.")


class PackageName(commands.Converter):
    """
    A converter that checks whether the given string is a valid package name.

    Package names are used for stats and are restricted to the a-z and _ characters.
    """

    PACKAGE_NAME_RE = re.compile(r"[^a-z0-9_]")

    @classmethod
    async def convert(cls, ctx: commands.Context, argument: str) -> str:
        """Checks whether the given string is a valid package name."""
        if cls.PACKAGE_NAME_RE.search(argument):
            raise commands.BadArgument(
                "The provided package name is not valid; please only use the _, 0-9, and a-z characters."
            )
        return argument


class ValidURL(commands.Converter):
    """
    Represents a valid webpage URL.

    This converter checks whether the given URL can be reached and requesting it returns a status
    code of 200. If not, `commands.BadArgument` is raised.

    Otherwise, it simply passes through the given URL.
    """

    @staticmethod
    async def convert(ctx: commands.Context, url: str) -> str:
        """This converter checks whether the given URL can be reached with a status code of 200."""
        try:
            async with ctx.bot.http_session.get(url) as resp:
                if resp.status != 200:
                    raise commands.BadArgument(f"HTTP GET on `{url}` returned status `{resp.status}`, expected 200")
        except CertificateError as e:
            if url.startswith("https"):
                raise commands.BadArgument(f"Got a `CertificateError` for URL `{url}`. Does it support HTTPS?") from e
            raise commands.BadArgument(f"Got a `CertificateError` for URL `{url}`.") from e
        except ValueError as e:
            raise commands.BadArgument(f"`{url}` doesn't look like a valid hostname to me.") from e
        except ClientConnectorError as e:
            raise commands.BadArgument(f"Cannot connect to host with URL `{url}`.") from e
        return url


class Inventory(commands.Converter):
    """
    Represents an Intersphinx inventory URL.

    This converter checks whether intersphinx accepts the given inventory URL, and raises
    `commands.BadArgument` if that is not the case or if the url is unreachable.

    Otherwise, it returns the url and the fetched inventory dict in a tuple.
    """

    @staticmethod
    async def convert(ctx: commands.Context, url: str) -> t.Tuple[str, inventory_parser.InventoryDict]:
        """Convert url to Intersphinx inventory URL."""
        await ctx.trigger_typing()
        try:
            inventory = await inventory_parser.fetch_inventory(ctx.bot, url)
        except inventory_parser.InvalidHeaderError as e:
            raise commands.BadArgument(
                "Unable to parse inventory because of invalid header, check if URL is correct."
            ) from e
        else:
            if inventory is None:
                raise commands.BadArgument(
                    f"Failed to fetch inventory file after {inventory_parser.FAILED_REQUEST_ATTEMPTS} attempts."
                )
            return url, inventory


class Snowflake(commands.IDConverter):
    """
    Converts to an int if the argument is a valid Discord snowflake.

    A snowflake is valid if:

    * It consists of 15-21 digits (0-9)
    * Its parsed datetime is after the Discord epoch
    * Its parsed datetime is less than 1 day after the current time
    """

    async def convert(self, ctx: commands.Context, arg: str) -> int:
        """
        Ensure `arg` matches the ID pattern and its timestamp is in range.

        Return `arg` as an int if it's a valid snowflake.
        """
        error = f"Invalid snowflake {arg!r}"

        if not self._get_id_match(arg):
            raise commands.BadArgument(error)

        snowflake = int(arg)

        try:
            time = disnake.utils.snowflake_time(snowflake)
        except (OverflowError, OSError) as e:
            # Not sure if this can ever even happen, but let's be safe.
            raise commands.BadArgument(f"{error}: {e}") from e

        if time < DISCORD_EPOCH_DT:
            raise commands.BadArgument(f"{error}: timestamp is before the Discord epoch.")
        elif (datetime.now(timezone.utc) - time).days < -1:
            raise commands.BadArgument(f"{error}: timestamp is too far into the future.")

        return snowflake


def _is_an_unambiguous_user_argument(argument: str) -> bool:
    """Check if the provided argument is a user mention, user id, or username (name#discrim)."""
    has_id_or_mention = bool(commands.IDConverter()._get_id_match(argument) or RE_USER_MENTION.match(argument))

    # Check to see if the author passed a username (a discriminator exists)
    argument = argument.removeprefix("@")
    has_username = len(argument) > 5 and argument[-5] == "#"

    return has_id_or_mention or has_username


AMBIGUOUS_ARGUMENT_MSG = (
    "`{argument}` is not a User mention, a User ID or a Username in the format `name#discriminator`."
)


class UnambiguousUser(commands.UserConverter):
    """
    Converts to a `discord.User`, but only if a mention, userID or a username (name#discrim) is provided.

    Unlike the default `commands.UserConverter`, it doesn't allow conversion from a name.
    This is useful in cases where that lookup strategy would lead to too much ambiguity.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> disnake.User:
        """Convert the `argument` to a `discord.User`."""
        if _is_an_unambiguous_user_argument(argument):
            return await super().convert(ctx, argument)
        else:
            raise commands.BadArgument(AMBIGUOUS_ARGUMENT_MSG.format(argument=argument))


class UnambiguousMember(commands.MemberConverter):
    """
    Converts to a `discord.Member`, but only if a mention, userID or a username (name#discrim) is provided.

    Unlike the default `commands.MemberConverter`, it doesn't allow conversion from a name or nickname.
    This is useful in cases where that lookup strategy would lead to too much ambiguity.
    """

    async def convert(self, ctx: commands.Context, argument: str) -> disnake.Member:
        """Convert the `argument` to a `discord.Member`."""
        if _is_an_unambiguous_user_argument(argument):
            return await super().convert(ctx, argument)
        else:
            raise commands.BadArgument(AMBIGUOUS_ARGUMENT_MSG.format(argument=argument))


class WrappedMessageConverter(commands.MessageConverter):
    """A converter that handles embed-suppressed links like <http://example.com>."""

    async def convert(self, ctx: commands.Context, argument: str) -> disnake.Message:
        """Wrap the commands.MessageConverter to handle <> delimited message links."""
        # It's possible to wrap a message in [<>] as well, and it's supported because its easy
        if argument.startswith("[") and argument.endswith("]"):
            argument = argument[1:-1]
        if argument.startswith("<") and argument.endswith(">"):
            argument = argument[1:-1]

        return await super().convert(ctx, argument)


SourceType = t.Union[
    commands.Command,
    commands.Cog,
    commands.InvokableSlashCommand,
    commands.InvokableMessageCommand,
    commands.InvokableUserCommand,
    commands.SubCommand,
    commands.SubCommandGroup,
]


class SourceConverter(commands.Converter):
    """Convert an argument into a command or cog."""

    @staticmethod
    async def convert(ctx: AnyContext, argument: str) -> SourceType:
        """Convert argument into source object."""
        # todo: add support for specifying the type
        cog = ctx.bot.get_cog(argument)
        if cog:
            return cog

        cmd = ctx.bot.get_slash_command(argument)
        if cmd:
            if not cmd.guild_ids:
                return cmd
            elif ctx.guild and ctx.guild.id in cmd.guild_ids:
                return cmd

        cmd = ctx.bot.get_command(argument)
        if cmd:
            return cmd

        # attempt to get the context menu command

        cmd = ctx.bot.get_message_command(argument)
        if cmd:
            if not cmd.guild_ids:
                return cmd
            elif ctx.guild and ctx.guild.id in cmd.guild_ids:
                return cmd

        cmd = ctx.bot.get_user_command(argument)
        if cmd:
            if not cmd.guild_ids:
                return cmd
            elif ctx.guild and ctx.guild.id in cmd.guild_ids:
                return cmd

        raise commands.BadArgument(f"Unable to convert `{argument}` to valid command, application command, or Cog.")


if t.TYPE_CHECKING:
    MaybeFeature = str  # type: ignore  # noqa: F811
    Extension = str  # type: ignore  # noqa: F811
    PackageName = str  # type: ignore  # noqa: F811
    ValidURL = str  # type: ignore  # noqa: F811
    Inventory = t.Tuple[str, inventory_parser.InventoryDict]  # type: ignore  # noqa: F811
    Snowflake = int  # type: ignore  # noqa: F811
    UnambiguousUser = disnake.User  # type: ignore  # noqa: F811
    UnambiguousMember = disnake.Member  # type: ignore  # noqa: F811
    WrappedMessageConverter = disnake.Message  # type: ignore  # noqa: F811
    SourceConverter = SourceType  # type: ignore  # noqa: F811
