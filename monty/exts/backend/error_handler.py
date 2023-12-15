from __future__ import annotations

import functools
import logging
import re
import traceback
import types
import typing

import disnake
from disnake.ext import commands

from monty.bot import Monty
from monty.constants import Client
from monty.log import get_logger
from monty.utils import responses
from monty.utils.messages import DeleteButton


if typing.TYPE_CHECKING:
    AnyContext = typing.Union[commands.Context, disnake.Interaction]

logger = get_logger(__name__)


ERROR_COLOUR = responses.DEFAULT_FAILURE_COLOUR

ERROR_TITLE_REGEX = re.compile(r"((?<=[a-z])[A-Z]|(?<=[a-zA-Z])[A-Z](?=[a-z]))")


class ErrorHandler(
    commands.Cog,
    name="Error Handler",
    slash_command_attrs={"dm_permission": False},
):
    """Handles all errors across the bot."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @staticmethod
    def error_embed(title: str, message: str) -> disnake.Embed:
        """Create an error embed with an error colour and reason and return it."""
        return disnake.Embed(title=title, description=message, colour=ERROR_COLOUR)

    @staticmethod
    def get_title_from_name(error: typing.Union[Exception, str]) -> str:
        """
        Return a message dervived from the exception class name.

        Eg NSFWChannelRequired returns NSFW Channel Required
        """
        if not isinstance(error, str):
            error = error.__class__.__name__
        return re.sub(ERROR_TITLE_REGEX, r" \1", error)

    @staticmethod
    def _reset_command_cooldown(ctx: AnyContext) -> bool:
        if return_value := ctx.command.is_on_cooldown(ctx):
            ctx.command.reset_cooldown(ctx)
        return return_value

    def make_error_message(
        self, ctx: AnyContext, error: commands.CommandError, *, extended_context: bool = True
    ) -> str:
        """Log the error with enough relevant context to properly fix the issue."""
        if isinstance(ctx, commands.Context):
            msg = (
                f"Error occurred in prefix command {ctx.command.qualified_name} in guild"
                f" {ctx.guild and ctx.guild.id} with user {ctx.author.id}\n"
            )
        elif isinstance(ctx, disnake.ApplicationCommandInteraction):
            cmd_type = ctx.data.type
            try:
                cmd_type = cmd_type.name
            except AttributeError:
                pass
            msg = (
                f"Error occurred in app command {ctx.application_command.qualified_name} of type {cmd_type} in guild"
                f" {ctx.guild_id} with user {ctx.author.id}\n"
            )
        elif isinstance(ctx, disnake.MessageInteraction):
            msg = (
                f"Error occurred in message component '{ctx.component.custom_id}' in guild {ctx.guild_id} with user"
                f" {ctx.author.id}\n"
            )
        else:
            msg = "Error occurred in unknown event\n"

        if not extended_context:
            return msg

        msg_type = getattr(type(ctx), "__name__", None) or str(type(ctx))
        msg += f"{self.get_title_from_name(msg_type)}:\n"
        # dump all attrs of the context minus a few attributes
        skip = {"token", "bot", "client", "send_error"}
        for attr in dir(ctx):
            if attr in skip or attr.startswith("_"):
                continue
            prop = getattr(ctx, attr, "???")
            if isinstance(prop, (types.FunctionType, types.MethodType, functools.partial)):
                continue
            msg += f"\t{attr}={prop}\n"

        return msg

    async def handle_user_input_error(
        self,
        ctx: AnyContext,
        error: commands.UserInputError,
        reset_cooldown: bool = True,
    ) -> disnake.Embed:
        """Handling deferred from main error handler to handle UserInputErrors."""
        if reset_cooldown:
            self._reset_command_cooldown(ctx)
        msg = None
        if isinstance(error, commands.BadUnionArgument):
            msg = self.get_title_from_name(str(error))
        title = self.get_title_from_name(error)
        return self.error_embed(title, msg or str(error))

    async def handle_bot_missing_perms(self, ctx: AnyContext, error: commands.BotMissingPermissions) -> None:
        """Handles bot missing permissing by dming the user if they have a permission which may be able to fix this."""  # noqa: E501
        embed = self.error_embed("Permissions Failure", str(error))
        bot_perms = ctx.channel.permissions_for(ctx.me)
        not_responded = True  # noqa: F841
        if bot_perms >= disnake.Permissions(send_messages=True, embed_links=True):
            await ctx.send_error(embeds=[embed])
            not_responded = False  # noqa: F841
        elif bot_perms >= disnake.Permissions(send_messages=True):
            # make a message as similar to the embed, using as few permissions as possible
            # this is the only place we send a standard message instead of an embed
            # so no helper methods are necessary
            await ctx.send_error(
                "**Permissions Failure**\n\nI am missing the permissions required to properly execute your command."
            )
            # intentionally not setting responded to True, since we want to attempt to dm the user
            logger.warning(
                f"Missing partial required permissions for {ctx.channel}. I am able to send messages, but not embeds."
            )
        else:
            logger.error(f"Unable to send an error message to channel {ctx.channel}")

    async def handle_check_failure(
        self, ctx: AnyContext, error: commands.CheckFailure
    ) -> typing.Optional[disnake.Embed]:
        """Handle CheckFailures seperately given that there are many of them."""
        title = "Check Failure"
        if isinstance(error, commands.CheckAnyFailure):
            title = self.get_title_from_name(error.checks[-1])
        elif isinstance(error, commands.PrivateMessageOnly):
            title = "DMs Only"
        elif isinstance(error, commands.NoPrivateMessage):
            title = "Server Only"
        elif isinstance(error, commands.NotOwner):
            # hide errors for owner check failures
            return None
        elif isinstance(error, commands.BotMissingPermissions):
            # defer handling BotMissingPermissions to a method
            # the error could be that the bot is unable to send messages, which would cause
            # the error handling to fail
            await self.handle_bot_missing_perms(ctx, error)
            return None
        else:
            title = self.get_title_from_name(error)
        embed = self.error_embed(title, str(error))
        return embed

    async def on_command_error(self, ctx: AnyContext, error: commands.CommandError) -> None:
        """Activates when a command raises an error."""
        if getattr(error, "handled", False):
            logging.debug(f"Command {ctx.command} had its error already handled locally, ignoring.")
            return

        if isinstance(error, commands.CommandNotFound):
            # ignore every time the user inputs a message that starts with our prefix but isn't a command
            # this will be modified in the future to support prefilled commands
            return

        embed: typing.Optional[disnake.Embed] = None
        should_respond = True

        self.bot.stats.incr("errors")

        if isinstance(error, commands.UserInputError):
            embed = await self.handle_user_input_error(ctx, error)
        elif isinstance(error, commands.CheckFailure):
            embed = await self.handle_check_failure(ctx, error)
            # handle_check_failure may send its own error if its a BotMissingPermissions error.
            if embed is None:
                should_respond = False
        elif isinstance(error, commands.DisabledCommand):
            if ctx.command.hidden:
                should_respond = False
            else:
                msg = f"Command `{ctx.invoked_with}` is disabled."
                if reason := ctx.command.extras.get("disabled_reason", None):
                    msg += f"\nReason: {reason}"
                embed = self.error_embed("Command Disabled", msg)
        elif isinstance(error, commands.CommandOnCooldown):
            if await ctx.bot.is_owner(ctx.author):
                if isinstance(ctx, commands.Context):
                    ctx.command.reset_cooldown(ctx)
                    await ctx.reinvoke()
                    should_respond = False
                elif isinstance(ctx, disnake.CommandInteraction):
                    ctx.application_command.reset_cooldown(ctx)
                    await self.bot.process_application_commands(ctx)
                    should_respond = False

        elif isinstance(error, (commands.CommandInvokeError, commands.ConversionError)):
            if isinstance(error.original, disnake.Forbidden):
                logger.warn(f"Permissions error occurred in {ctx.command}.")
                await self.handle_bot_missing_perms(ctx, error.original)
                should_respond = False
            else:
                # generic error
                if logger.isEnabledFor(logging.ERROR):
                    try:
                        msg = self.make_error_message(
                            ctx, error, extended_context=Client.debug_logging or Client.sentry_enabled
                        )
                    except Exception as e:
                        logger.error("Something went wrong creating the full logging context for an error", exc_info=e)
                        msg = "Error occurred in prefix or interaction."
                    logger.error(msg, exc_info=error.original)

                # built in command msg
                title = "Internal Error"
                error_str = "".join(
                    traceback.format_exception(
                        type(error.original), error.original, tb=error.original.__traceback__, limit=-3
                    )
                ).replace("``", "`\u200b`")
                if len(error_str) > 3000:
                    error_str = error_str[-3000:]
                msg = (
                    "Something went wrong internally in the action you were trying to execute. "
                    "Please report this error and the code below and what you were trying to do in "
                    f"the [support server](https://discord.gg/{Client.support_server})."
                    f"\n\n```py\n{error_str}\n```"
                )

                embed = self.error_embed(title, msg)

        # TODO: this has a fundamental problem with any BotMissingPermissions error
        # if the issue is the bot does not have permissions to send embeds or send messages...
        # yeah, problematic.

        if not should_respond:
            logger.debug(
                "Not responding to error since should_respond is falsey because either "
                "the embed has already been sent or belongs to a hidden command and thus should be hidden."
            )
            return

        if embed is None:
            embed = self.error_embed(self.get_title_from_name(error), str(error))

        await ctx.send_error(embeds=[embed])

    @commands.Cog.listener(name="on_command_error")
    @commands.Cog.listener(name="on_slash_command_error")
    @commands.Cog.listener(name="on_message_command_error")
    async def on_any_command_error(self, ctx: AnyContext, error: Exception) -> None:
        """Handle all errors with one mega error handler."""
        # add the support button
        components = disnake.ui.MessageActionRow()
        components.add_button(
            style=disnake.ButtonStyle.url, label="Support Server", url=f"https://discord.gg/{Client.support_server}"
        )
        if isinstance(ctx, commands.Context):
            components.insert_item(0, DeleteButton(ctx.author, initial_message=ctx.message))
            if ctx.channel.permissions_for(ctx.me).read_message_history:
                ctx.send_error = functools.partial(
                    ctx.reply,
                    components=components,
                    fail_if_not_exists=False,
                    allowed_mentions=disnake.AllowedMentions(replied_user=False),
                )
            else:
                ctx.send_error = functools.partial(ctx.send, components=components)
        elif isinstance(ctx, disnake.Interaction):
            if ctx.response.is_done():
                ctx.send_error = functools.partial(
                    ctx.followup.send,
                    ephemeral=True,
                    components=components,
                )
            else:
                ctx.send_error = functools.partial(
                    ctx.send,
                    ephemeral=True,
                    components=components,
                )

            if isinstance(
                ctx,
                (
                    disnake.ApplicationCommandInteraction,
                    disnake.MessageCommandInteraction,
                    disnake.UserCommandInteraction,
                ),
            ):
                ctx.command = ctx.application_command
            elif isinstance(ctx, (disnake.MessageInteraction, disnake.ModalInteraction)):
                # todo: this is a hack, but it works for now
                ctx.command = ctx.message
            else:
                # i don't even care, this code should be unreachable but its also the error handler
                ctx.command = ctx
        else:
            raise RuntimeError("how was this even reached")
        try:
            await self.on_command_error(ctx, error)
        except Exception as e:
            logger.exception("Error occurred in error handler", exc_info=e)


def setup(bot: Monty) -> None:
    """Add the error handler to the bot."""
    bot.add_cog(ErrorHandler(bot))
