import re
from datetime import datetime
from html import unescape
from typing import List

import disnake
from disnake.ext import commands

from monty.bot import Monty
from monty.errors import APIError
from monty.log import get_logger
from monty.utils import LinePaginator


log = get_logger(__name__)

SEARCH_API = "https://en.wikipedia.org/w/api.php"
WIKI_PARAMS = {
    "action": "query",
    "list": "search",
    "prop": "info",
    "inprop": "url",
    "utf8": "",
    "format": "json",
    "origin": "*",
}
WIKI_THUMBNAIL = (
    "https://upload.wikimedia.org/wikipedia/en/thumb/8/80/Wikipedia-logo-v2.svg/330px-Wikipedia-logo-v2.svg.png"
)
WIKI_SNIPPET_REGEX = r"(<!--.*?-->|<[^>]*>)"
WIKI_SEARCH_RESULT = "**[{name}]({url})**\n{description}\n"


class WikipediaSearch(commands.Cog, name="Wikipedia Search", slash_command_attrs={"dm_permission": False}):
    """Get info from wikipedia."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    async def wiki_request(self, channel: disnake.abc.Messageable, search: str) -> List[str]:
        """Search wikipedia search string and return formatted first 10 pages found."""
        params = WIKI_PARAMS | {"srlimit": 10, "srsearch": search}
        async with self.bot.http_session.get(url=SEARCH_API, params=params) as resp:
            if resp.status != 200:
                log.info(f"Unexpected response `{resp.status}` while searching wikipedia for `{search}`")
                raise APIError("Wikipedia API", resp.status)

            raw_data = await resp.json()

            if not raw_data.get("query"):
                if error := raw_data.get("errors"):
                    log.error(f"There was an error while communicating with the Wikipedia API: {error}")
                raise APIError("Wikipedia API", resp.status, error)

            lines = []
            if raw_data["query"]["searchinfo"]["totalhits"]:
                for article in raw_data["query"]["search"]:
                    line = WIKI_SEARCH_RESULT.format(
                        name=article["title"],
                        description=unescape(re.sub(WIKI_SNIPPET_REGEX, "", article["snippet"])),
                        url=f"https://en.wikipedia.org/?curid={article['pageid']}",
                    )
                    lines.append(line)

            return lines

    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.command(name="wikipedia", aliases=("wiki",))
    async def wikipedia_search_command(self, ctx: commands.Context, *, search: str) -> None:
        """Sends paginated top 10 results of Wikipedia search.."""
        contents = await self.wiki_request(ctx.channel, search)

        if contents:
            embed = disnake.Embed(title="Wikipedia Search Results", colour=disnake.Color.blurple())
            embed.set_thumbnail(url=WIKI_THUMBNAIL)
            embed.timestamp = datetime.utcnow()
            await LinePaginator.paginate(contents, ctx, embed)
        else:
            await ctx.send("Sorry, we could not find a wikipedia article using that search term.")


def setup(bot: Monty) -> None:
    """Load the WikipediaSearch cog."""
    bot.add_cog(WikipediaSearch(bot))
