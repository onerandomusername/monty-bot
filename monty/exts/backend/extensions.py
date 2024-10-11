import functools
import typing as t
from enum import Enum

import disnake
from disnake.ext import commands

from monty import exts
from monty.bot import Monty
from monty.constants import Client
from monty.log import get_logger
from monty.metadata import ExtMetadata
from monty.utils.converters import Extension
from monty.utils.extensions import EXTENSIONS, invoke_help_command
from monty.utils.messages import DeleteButton
from monty.utils.pagination import LinePaginator


EXT_METADATA = ExtMetadata(core=True)

log = get_logger(__name__)


UNLOAD_BLACKLIST = {__name__}
BASE_PATH_LEN = exts.__name__.count(".")


class Action(Enum):
    """Represents an action to perform on an extension."""

    # Need to be partial otherwise they are considered to be function definitions.
    LOAD = functools.partial(Monty.load_extension)
    UNLOAD = functools.partial(Monty.unload_extension)
    RELOAD = functools.partial(Monty.reload_extension)


class Extensions(commands.Cog):
    """Extension management commands."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @commands.group(
        name="extensions",
        aliases=("ext", "exts", "c", "cogs"),
        invoke_without_command=True,
    )
    async def extensions_group(self, ctx: commands.Context) -> None:
        """Load, unload, reload, and list loaded extensions."""
        await invoke_help_command(ctx)

    @extensions_group.command(name="load", aliases=("l",))
    async def load_command(self, ctx: commands.Context, *extensions: Extension) -> None:
        r"""
        Load extensions given their fully qualified or unqualified names.

        If '\*' or '\*\*' is given as the name, all unloaded extensions will be loaded.
        """  # noqa: W605
        if not extensions:
            await invoke_help_command(ctx)
            return

        if "*" in extensions or "**" in extensions:
            extensions = set(EXTENSIONS) - set(self.bot.extensions.keys())

        msg = self.batch_manage(Action.LOAD, *extensions)

        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(msg, components=components)

    @extensions_group.command(name="unload", aliases=("ul",))
    async def unload_command(self, ctx: commands.Context, *extensions: Extension) -> None:
        r"""
        Unload currently loaded extensions given their fully qualified or unqualified names.

        If '\*' or '\*\*' is given as the name, all loaded extensions will be unloaded.
        """  # noqa: W605
        if not extensions:
            await invoke_help_command(ctx)
            return

        blacklisted = "\n".join(UNLOAD_BLACKLIST & set(extensions))

        if blacklisted:
            msg = f":x: The following extension(s) may not be unloaded:```{blacklisted}```"
        else:
            if "*" in extensions or "**" in extensions:
                extensions = set(self.bot.extensions.keys()) - UNLOAD_BLACKLIST

            msg = self.batch_manage(Action.UNLOAD, *extensions)

        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(msg, components=components)

    @extensions_group.command(name="reload", aliases=("r",), root_aliases=("reload",))
    async def reload_command(self, ctx: commands.Context, *extensions: Extension) -> None:
        r"""
        Reload extensions given their fully qualified or unqualified names.

        If an extension fails to be reloaded, it will be rolled-back to the prior working state.

        If '\*' is given as the name, all currently loaded extensions will be reloaded.
        If '\*\*' is given as the name, all extensions, including unloaded ones, will be reloaded.
        """  # noqa: W605
        if not extensions:
            await invoke_help_command(ctx)
            return

        if "**" in extensions:
            extensions = EXTENSIONS
        elif "*" in extensions:
            extensions = set(self.bot.extensions.keys()) | set(extensions)
            extensions.remove("*")

        msg = self.batch_manage(Action.RELOAD, *extensions)

        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(msg, components=components)

    @extensions_group.command(name="list", aliases=("all",))
    async def list_command(self, ctx: commands.Context) -> None:
        """
        Get a list of all extensions, including their loaded status.

        Grey indicates that the extension is unloaded.
        Green indicates that the extension is currently loaded.
        """
        embed = disnake.Embed(colour=disnake.Colour.blurple())
        embed.set_author(
            name="Extensions List",
            url=Client.github_bot_repo,
            icon_url=str(self.bot.user.display_avatar.url),
        )

        lines = []
        categories = self.group_extension_statuses()
        for category, extensions in sorted(categories.items()):
            # Treat each category as a single line by concatenating everything.
            # This ensures the paginator will not cut off a page in the middle of a category.
            category = category.replace("_", " ").title()
            extensions = "\n".join(sorted(extensions))
            lines.append(f"**{category}**\n{extensions}\n")

        log.debug(f"{ctx.author} requested a list of all cogs. Returning a paginated list.")
        await LinePaginator.paginate(lines, ctx, embed, max_size=1200, empty=False)

    def group_extension_statuses(self) -> t.Mapping[str, str]:
        """Return a mapping of extension names and statuses to their categories."""
        categories = {}

        for ext in EXTENSIONS:
            if ext in self.bot.extensions:
                status = ":green_circle:"
            else:
                status = ":red_circle:"

            path = ext.split(".")
            if len(path) > BASE_PATH_LEN + 1:
                category = " - ".join(path[BASE_PATH_LEN:-1])
            else:
                category = "uncategorised"

            categories.setdefault(category, []).append(f"{status}  {path[-1]}")

        return categories

    def batch_manage(self, action: Action, *extensions: str) -> str:
        """
        Apply an action to multiple extensions and return a message with the results.

        If only one extension is given, it is deferred to `manage()`.
        """
        if len(extensions) == 1:
            msg, _ = self.manage(action, extensions[0])
            return msg

        verb = action.name.lower()
        failures = {}

        for extension in extensions:
            _, error = self.manage(action, extension)
            if error:
                failures[extension] = error

        emoji = ":x:" if failures else ":ok_hand:"
        msg = f"{emoji} {len(extensions) - len(failures)} / {len(extensions)} extensions {verb}ed."

        if failures:
            failures = "\n".join(f"{ext}\n    {err}" for ext, err in failures.items())
            msg += f"\nFailures:```{failures}```"

        log.debug(f"Batch {verb}ed extensions.")

        return msg

    def manage(self, action: Action, ext: str) -> t.Tuple[str, t.Optional[str]]:
        """Apply an action to an extension and return the status message and any error message."""
        verb = action.name.lower()
        error_msg = None

        try:
            action.value(self.bot, ext)
        except (commands.ExtensionAlreadyLoaded, commands.ExtensionNotLoaded):
            if action is Action.RELOAD:
                # When reloading, just load the extension if it was not loaded.
                return self.manage(Action.LOAD, ext)

            msg = f":x: Extension `{ext}` is already {verb}ed."
            log.debug(msg[4:])
        except Exception as e:
            if hasattr(e, "original"):
                e = e.original

            log.exception(f"Extension '{ext}' failed to {verb}.")

            error_msg = f"{e.__class__.__name__}: {e}"
            msg = f":x: Failed to {verb} extension `{ext}`:\n```{error_msg}```"
        else:
            msg = f":ok_hand: Extension successfully {verb}ed: `{ext}`."
            log.debug(msg[10:])

        return msg, error_msg

    # This cannot be static (must have a __func__ attribute).
    async def cog_check(self, ctx: commands.Context) -> bool:
        """Only allow moderators and core developers to invoke the commands in this cog."""
        return await self.bot.is_owner(ctx.author)

    # This cannot be static (must have a __func__ attribute).
    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Handle BadArgument errors locally to prevent the help command from showing."""
        if isinstance(error, commands.BadArgument):
            await ctx.send(str(error))
            error.handled = True


def setup(bot: Monty) -> None:
    """Load the Extensions cog."""
    bot.add_cog(Extensions(bot))
