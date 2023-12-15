from __future__ import annotations

import re
import unicodedata
from typing import Tuple

import disnake
from disnake.ext import commands

from monty.bot import Monty
from monty.log import get_logger
from monty.utils.messages import DeleteButton
from monty.utils.pagination import LinePaginator


log = get_logger(__name__)


class Utils(commands.Cog, slash_command_attrs={"dm_permission": False}):
    """A selection of utilities which don't have a clear category."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @commands.slash_command(name="char-info")
    async def charinfo(
        self, ctx: disnake.ApplicationCommandInteraction, characters: commands.String[str, ..., 50]
    ) -> None:
        """
        Shows you information on up to 50 unicode characters.

        Parameters
        ----------
        characters: The characters to display information on.
        """
        match = re.match(r"<(a?):(\w+):(\d+)>", characters)
        if match:
            await ctx.send(
                "**Non-Character Detected**\n"
                "Only unicode characters can be processed, but a custom Discord emoji "
                "was found. Please remove it and try again."
            )
            return

        if len(characters) > 50:
            await ctx.send(f"Too many characters ({len(characters)}/50)")
            return

        def get_info(char: str) -> Tuple[str, str]:
            digit = f"{ord(char):x}"
            if len(digit) <= 4:
                u_code = f"\\u{digit:>04}"
            else:
                u_code = f"\\U{digit:>08}"
            url = f"https://www.compart.com/en/unicode/U+{digit:>04}"
            name = f"[{unicodedata.name(char, '')}]({url})"
            info = f"`{u_code.ljust(10)}`: {name} - {disnake.utils.escape_markdown(char)}"
            return (info, u_code)

        (char_list, raw_list) = zip(*(get_info(c) for c in characters))
        embed = disnake.Embed().set_author(name="Character Info")

        if len(characters) > 1:
            # Maximum length possible is 502 out of 1024, so there's no need to truncate.
            embed.add_field(name="Full Raw Text", value=f"`{''.join(raw_list)}`", inline=False)
        embed.description = "\n".join(char_list)
        await ctx.send(embed=embed, components=DeleteButton(ctx.author))

    @commands.command(aliases=("snf", "snfl", "sf"))
    async def snowflake(self, ctx: commands.Context, *snowflakes: disnake.Object) -> None:
        """Get Discord snowflake creation time."""
        if not snowflakes:
            raise commands.BadArgument("At least one snowflake must be provided.")

        # clear any duplicated keys
        snowflakes = tuple(set(snowflakes))

        embed = disnake.Embed(colour=disnake.Colour.blue())
        embed.set_author(
            name=f"Snowflake{'s'[:len(snowflakes)^1]}",  # Deals with pluralisation
            icon_url="https://github.com/twitter/twemoji/blob/master/assets/72x72/2744.png?raw=true",
        )

        lines = []
        for snowflake in snowflakes:
            created_at = int(((snowflake.id >> 22) + disnake.utils.DISCORD_EPOCH) / 1000)
            lines.append(f"**{snowflake.id}** ({created_at})\nCreated at <t:{created_at}:f> (<t:{created_at}:R>).")

        await LinePaginator.paginate(lines, ctx=ctx, embed=embed, max_lines=5, max_size=1000)

    @commands.slash_command(name="snowflake")
    async def slash_snowflake(
        self,
        inter: disnake.AppCommandInteraction,
        snowflake: commands.LargeInt,
    ) -> None:
        """
        [BETA] Get creation date of a snowflake.

        Parameters
        ----------
        snowflake: The snowflake.
        """
        embed = disnake.Embed(colour=disnake.Colour.blue())
        embed.set_author(
            name="Snowflake",
            icon_url="https://github.com/twitter/twemoji/blob/master/assets/72x72/2744.png?raw=true",
        )
        created_at = int(((snowflake >> 22) + disnake.utils.DISCORD_EPOCH) / 1000)
        embed.description = f"**{snowflake}** ({created_at})\nCreated at <t:{created_at}:f> (<t:{created_at}:R>)."
        components = DeleteButton(inter.author)
        await inter.send(embed=embed, components=components)


def setup(bot: Monty) -> None:
    """Load the Utils cog."""
    bot.add_cog(Utils(bot))
