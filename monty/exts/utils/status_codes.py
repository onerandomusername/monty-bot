from http import HTTPStatus
from random import choice

import disnake
from disnake.ext import commands

from monty.bot import Monty


HTTP_DOG_URL = "https://http.dog/{code}.jpg"
HTTP_CAT_URL = "https://http.cat/{code}.jpg"
HTTP_GOAT_URL = "https://httpgoats.com/{code}.jpg"


class HTTPStatusCodes(commands.Cog, name="HTTP Status Codes", slash_command_attrs={"dm_permission": False}):
    """
    Fetch an image depicting HTTP status codes as a dog or a cat or as goat.

    If neither animal is selected a cat or dog or goat is chosen randomly for the given status code.
    """

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @commands.group(
        name="http_status",
        aliases=("status", "httpstatus", "http"),
        invoke_without_command=True,
    )
    async def http_status_group(self, ctx: commands.Context, code: int) -> None:
        """Choose a cat or dog randomly for the given status code."""
        subcmd = choice((self.http_cat, self.http_dog, self.http_goat))
        await subcmd(ctx, code)

    @http_status_group.command(name="cat")
    async def http_cat(self, ctx: commands.Context, code: int) -> None:
        """Sends an embed with an image of a cat, portraying the status code."""
        embed = disnake.Embed(title=f"**Status: {code}**")
        url = HTTP_CAT_URL.format(code=code)

        try:
            HTTPStatus(code)
            async with self.bot.http_session.get(url, allow_redirects=False) as response:
                if response.status != 404:
                    embed.set_image(url=url)
                else:
                    raise NotImplementedError

        except ValueError:
            embed.set_footer(text="Inputted status code does not exist.")

        except NotImplementedError:
            embed.set_footer(text="Inputted status code is not implemented by http.cat yet.")

        finally:
            await ctx.send(embed=embed)

    @http_status_group.command(name="dog")
    async def http_dog(self, ctx: commands.Context, code: int) -> None:
        """Sends an embed with an image of a dog, portraying the status code."""
        # These codes aren't server-friendly.
        if code in (304, 422):
            await self.http_cat(ctx, code)
            return

        embed = disnake.Embed(title=f"**Status: {code}**")
        url = HTTP_DOG_URL.format(code=code)

        try:
            HTTPStatus(code)
            async with self.bot.http_session.get(url, allow_redirects=False) as response:
                if response.status != 302:
                    embed.set_image(url=url)
                else:
                    raise NotImplementedError

        except ValueError:
            embed.set_footer(text="Inputted status code does not exist.")

        except NotImplementedError:
            embed.set_footer(text="Inputted status code is not implemented by http.dog yet.")

        finally:
            await ctx.send(embed=embed)

    @http_status_group.command(name="goat")
    async def http_goat(self, ctx: commands.Context, code: int) -> None:
        """Sends an embed with an image of a goat, portraying the status code."""
        # These codes aren't server-friendly.
        if code in (304, 422):
            await self.http_cat(ctx, code)
            return

        embed = disnake.Embed(title=f"**Status: {code}**")
        url = HTTP_GOAT_URL.format(code=code)

        try:
            HTTPStatus(code)
            async with self.bot.http_session.get(url, allow_redirects=False) as response:
                if response.status != 302:
                    embed.set_image(url=url)
                else:
                    raise NotImplementedError

        except ValueError:
            embed.set_footer(text="Inputted status code does not exist.")

        except NotImplementedError:
            embed.set_footer(text="Inputted status code is not implemented by httpgoats.com yet.")

        finally:
            await ctx.send(embed=embed)


def setup(bot: Monty) -> None:
    """Load the HTTPStatusCodes cog."""
    bot.add_cog(HTTPStatusCodes(bot))
