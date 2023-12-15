from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, List
from urllib.parse import urldefrag

import disnake
from disnake.ext import commands, tasks

from monty.constants import Feature
from monty.log import get_logger
from monty.utils.features import require_feature
from monty.utils.helpers import encode_github_link
from monty.utils.messages import DeleteButton


if TYPE_CHECKING:
    from monty.bot import Monty
    from monty.exts.eval import Snekbox

logger = get_logger(__name__)
CODE_FILE = os.path.dirname(__file__) + "/_global_source_snekcode.py"


class GlobalSource(commands.Cog, name="Global Source"):
    """Global source for python objects."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        with open(CODE_FILE, "r") as f:
            # this is declared as final as we should *not* be writing to it
            self.code: Final[str] = f.read()

    def cog_unload(self) -> None:
        """Stop the running task on unload if it is running."""
        self.refresh_code.stop()

    @property
    def snekbox(self) -> Snekbox:
        """Return the snekbox cog where the code is ran."""
        snekbox: Snekbox
        if snekbox := self.bot.get_cog("Snekbox"):  # type: ignore # this will always be a Snekbox instance
            return snekbox
        raise RuntimeError("Snekbox is not loaded")

    @require_feature(Feature.GLOBAL_SOURCE)
    @commands.command(name="globalsource", aliases=("gs",), hidden=True)
    async def globalsource(self, ctx: commands.Context, object: str) -> None:
        """Get the source of a python object."""
        object = object.strip("`")
        async with ctx.typing():
            result = await self.snekbox.post_eval(
                self.code.replace("REPLACE_THIS_STRING_WITH_THE_OBJECT_NAME", object),
                # for `-X frozen_modules=off`, see https://github.com/python/cpython/issues/89183
                args=["-X", "frozen_modules=off", "-c"],
            )

        # exit codes:
        # 0: success
        # 1: indeterminate error
        # 2: module not resolvable
        # 3: attribute does not exist
        # 4: invalid characters, not a valid object path
        # 5: dynamically created object
        # 6: is a builtin object, prints module name
        # 7: invalid metadata
        # 8: unsupported package (does not use github)
        # 9: module found but cannot find class definition

        text = result["stdout"].strip()
        if self.refresh_code.is_running():
            logger.debug(text)
        returncode = result["returncode"]
        link = ""
        if returncode == 0:
            link = text.rsplit("#" * 80)[-1].strip()
            text = f"Source of `{object}`:\n<{link}>"
        elif returncode == 1:
            # generic exception occured
            logger.exception(result["stdout"])
            raise Exception("Snekbox returned an error.")
        elif returncode == 2:
            text = "The module you provided was not resolvable to an installed module."
        elif returncode == 3:
            text = "The attribute you are looking for does not exist. Check for misspellings and try again."
        elif returncode == 4:
            text = "The object path you provided is invalid."
        elif returncode == 5:
            text = "That object exists, but is dynamically created."
        elif returncode == 6:
            text = (
                f"`{object}` is a builtin object/implemented in C. "
                "It is not currently possible to get source of those objects."
            )
        elif returncode == 7:
            text = "The metadata for the provided module is invalid."
        elif returncode == 8:
            text = "The provided module is not supported."
        elif returncode == 9:
            text = "The definition could not be found."
        else:
            text = "Something went wrong."

        components: List[disnake.ui.action_row.Components] = []
        if isinstance(ctx, commands.Context):
            components.append(DeleteButton(ctx.author, initial_message=ctx.message))
        else:
            components.append(DeleteButton(ctx.author))

        if link:
            components.append(disnake.ui.Button(style=disnake.ButtonStyle.link, url=link, label="Go to Github"))
            custom_id = encode_github_link(link)
            if frag := (urldefrag(link)[1]):
                frag = frag.replace("#", "").replace("L", "")

                if "-" in frag:
                    num1, num2 = frag.split("-")
                    show_source = int(num2) - int(num1) <= 21
                else:
                    show_source = True

                if show_source:
                    components.append(
                        disnake.ui.Button(style=disnake.ButtonStyle.blurple, label="Expand", custom_id=custom_id)
                    )

        await ctx.reply(
            text,
            allowed_mentions=disnake.AllowedMentions(everyone=False, users=False, roles=False, replied_user=True),
            components=components,
        )

    @tasks.loop(seconds=1)
    async def refresh_code(self, ctx: commands.Context, query: str) -> None:
        """Refresh the internal code every second."""
        modified = os.stat(CODE_FILE).st_mtime
        if modified <= self.last_modified:
            return
        self.last_modified = modified
        with open(CODE_FILE, "r") as f:
            self.code = f.read()  # type: ignore # this is the one time we can write to the code
            logger.debug("Updated global_source code")

        try:
            await self.globalsource(ctx, query)
        except Exception as e:
            self.bot.dispatch("command_error", ctx, e)

    @refresh_code.before_loop
    async def before_refresh_code(self) -> None:
        """Set the current last_modified stat to zero starting the task."""
        self.last_modified = 0

    @commands.command("globalsourcedebug", hidden=True)
    @commands.is_owner()
    async def globalsourcedebug(self, ctx: commands.Context, query: str = None) -> None:
        """Refresh the existing code and reinvoke it continually until the command is run again."""
        if self.refresh_code.is_running():
            if query:
                self.refresh_code.restart(ctx, query)
                await ctx.send("Restarted the global source debug task.")
            else:
                self.refresh_code.stop()
                await ctx.send("Cancelled the internal global source debug task.")
            return
        if not query:

            class FakeParam:
                name = "query"

            raise commands.MissingRequiredArgument(FakeParam)  # type: ignore # we don't need an entire Parameter obj
        await ctx.send("Starting the global source debug task.")
        self.refresh_code.start(ctx, query)


def setup(bot: Monty) -> None:
    """Add the global source cog to the bot."""
    bot.add_cog(GlobalSource(bot))
