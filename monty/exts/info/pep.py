from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
import disnake
import rapidfuzz
import rapidfuzz.fuzz
import rapidfuzz.process
from bs4 import BeautifulSoup
from cachingutils import LRUMemoryCache, async_cached
from disnake.ext import commands

from monty.bot import Monty
from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.html_parsing import _get_truncated_description
from monty.utils.inventory_parser import fetch_inventory
from monty.utils.markdown import DocMarkdownConverter
from monty.utils.messages import DeleteButton


log = get_logger(__name__)

ICON_URL = "https://www.python.org/static/opengraph-icon-200x200.png"
BASE_URL = "https://peps.python.org"
BASE_PEP_URL = f"{BASE_URL}/pep-"
INVENTORY_URL = f"{BASE_URL}/objects.inv"


class HeaderParser:
    """Parser for parsing the descriptor headers from the HTML of a pep page."""

    def parse(self, soup: BeautifulSoup) -> dict[str, str]:
        """Parse the provided BeautifulSoup object and return a dict of pep headers."""
        dl = soup.find("dl")
        results = {}
        for dt in dl.find_all("dt"):
            results[dt.text] = dt.find_next_sibling("dd").text

        # readd title to headers
        # this was removed from the headers in python/peps#2532
        h1 = soup.find("h1", attrs={"class": "page-title"})
        results["title"] = h1.text.split("–", 1)[-1]

        return results


class PEPHeaders:
    """Parser for getting the headers from the HTML of a pep page."""

    header_tags = ["h2", "h3", "h4", "h5", "h6"]

    def parse(self, soup: BeautifulSoup) -> str:
        """Parse the provided BeautifulSoup object and return a string of headers in the pep's body."""
        headers: dict[tuple[str, str], str] = {}
        for header in soup.find_all(self.header_tags):
            headers[header.text] = header.name

        return headers


class PythonEnhancementProposals(commands.Cog, name="PEPs", slash_command_attrs={"dm_permission": False}):
    """Cog for displaying information about PEPs."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        self.peps: Dict[int, str] = {}
        self.autocomplete: dict[str, int] = {}
        # To avoid situations where we don't have last datetime, set this to now.
        self.last_refreshed_peps: datetime = datetime.now()
        scheduling.create_task(self.refresh_peps_urls())

    async def refresh_peps_urls(self) -> None:
        """Refresh PEP URLs listing in every 3 hours."""
        # Wait until HTTP client is available
        await self.bot.wait_until_ready()
        log.trace("Started refreshing PEP URLs.")
        self.last_refreshed_peps = datetime.now()
        self.peps.clear()
        self.autocomplete.clear()

        package = await fetch_inventory(self.bot, INVENTORY_URL)
        if package is None:
            log.error("Failed to fetch pep inventory.")
            return

        for item in package.values():
            for name, location, display_name in item:
                if name.startswith("pep-") and len(name) == 8:
                    self.peps[int(name[4:])] = BASE_URL + "/" + location
                    self.autocomplete[display_name] = int(name[4:])

        log.trace("Got PEP URLs listing from Pep sphinx inventory")

    async def validate_pep_number(self, pep_nr: int) -> Optional[disnake.Embed]:
        """Validate is PEP number valid. When it isn't, return error embed, otherwise None."""
        if (
            pep_nr not in self.peps
            and (self.last_refreshed_peps + timedelta(minutes=30)) <= datetime.now()
            and len(str(pep_nr)) < 5
        ):
            await self.refresh_peps_urls()

        if pep_nr not in self.peps:
            log.trace(f"PEP {pep_nr} was not found")
            return disnake.Embed(
                title="PEP not found",
                description=f"PEP {pep_nr} does not exist.",
                colour=disnake.Colour.red(),
            )

        return None

    def generate_pep_embed(self, pep_header: Dict, pep_nr: int) -> disnake.Embed:
        """Generate PEP embed based on PEP headers data."""
        # Assemble the embed
        pep_embed = disnake.Embed(
            title=f"PEP {pep_nr} - {pep_header['title']}",
            url=self.peps[pep_nr],
        )

        pep_embed.set_thumbnail(url=ICON_URL)

        # Add the interesting information
        fields_to_check = ("Status", "Python-Version", "Created", "Type")
        for field in fields_to_check:
            # Check for a PEP metadata field that is present but has an empty value
            # embed field values can't contain an empty string
            if pep_header.get(field.lower()):
                pep_embed.add_field(name=field, value=pep_header[field.lower()])

        return pep_embed

    @async_cached(cache=LRUMemoryCache(20, timeout=int(timedelta(hours=2).total_seconds())))
    async def fetch_pep_info(self, url: str, number: int) -> Tuple[dict[str, str], BeautifulSoup]:
        """Fetch the pep information. This is extracted into a seperate function for future use."""
        async with self.bot.http_session.get(url) as response:
            response.raise_for_status()
            pep_content = await response.text()
        soup = await self.bot.loop.run_in_executor(None, BeautifulSoup, pep_content, "lxml")

        pep_header = HeaderParser().parse(soup)
        for key, value in pep_header.copy().items():
            new_key = key.strip().strip(":").lower()
            if new_key != key:
                pep_header[new_key] = value
                del pep_header[key]

        return pep_header, soup

    async def get_pep_embed(self, pep_nr: int) -> Tuple[disnake.Embed, bool]:
        """Fetch, generate and return PEP embed. Second item of return tuple show does getting success."""
        url = self.peps[pep_nr]

        try:
            pep_header, *_ = await self.fetch_pep_info(url, pep_nr)
        except aiohttp.ClientResponseError as e:
            log.trace(f"The user requested PEP {pep_nr}, but the response had an unexpected status code: {e.status}.")
            return (
                disnake.Embed(
                    title="Unexpected error",
                    description="Unexpected HTTP error during PEP search. Please let us know.",
                    colour=disnake.Colour.red(),
                ),
                False,
            )
        else:
            return self.generate_pep_embed(pep_header, pep_nr), True

    async def get_pep_section_header(self, inter: disnake.CommandInteraction, number: int, header: str) -> None:
        """Get the contents of the provided header in the pep body."""
        error_embed = await self.validate_pep_number(number)
        if error_embed:
            await inter.send(embed=error_embed, ephemeral=True)
            return
        url = self.peps[number]
        tags, soup = await self.fetch_pep_info(url, number)

        tag = soup.find(PEPHeaders.header_tags, text=header)

        if tag is None:
            await inter.send("Could not find the requested header in the PEP.", ephemeral=True)
            return

        text = _get_truncated_description(tag.parent, DocMarkdownConverter(page_url=url), max_length=750, max_lines=14)
        text = (text.lstrip() + "\n").split("\n", 1)[-1].strip()
        if not text:
            await inter.send("No text found for that header.", ephemeral=True)
            return

        embed = disnake.Embed(
            title=header,
            description=text,
            url=urljoin(url, tag.a["href"]),
        )
        embed.set_author(name=f"PEP {number} - {tags['title']}", url=url)

        embed.set_thumbnail(url=ICON_URL)
        if tags.get("Created"):
            embed.set_footer(text="PEP Created")
            embed.timestamp = datetime.strptime(tags["Created"], "%d-%b-%Y").replace(tzinfo=timezone.utc)

        components = [
            DeleteButton(inter.author),
            disnake.ui.Button(style=disnake.ButtonStyle.link, label="Open PEP", url=embed.url),
        ]
        await inter.send(embed=embed, components=components)

    @commands.slash_command(name="pep")
    async def pep_command(
        self, inter: disnake.ApplicationCommandInteraction, number: int, header: Optional[str] = None
    ) -> None:
        """
        Fetch information about a PEP.

        Parameters
        ----------
        number: number or search query
        header: If provided, shows a snippet of the PEP at this header.
        """
        if header:
            await self.get_pep_section_header(inter, number, header)
            return

        success = False
        if not (pep_embed := await self.validate_pep_number(number)):
            pep_embed, success = await self.get_pep_embed(number)

        if success:
            components = [DeleteButton(inter.author)]
            if pep_embed.url:
                components.append(
                    disnake.ui.Button(style=disnake.ButtonStyle.link, label="Open PEP", url=pep_embed.url)
                )
            await inter.send(embed=pep_embed, components=components)
            log.trace(f"PEP {number} getting and sending finished successfully")
        else:
            await inter.send(embed=pep_embed, ephemeral=True)
            log.trace(f"Getting PEP {number} failed. Error embed sent.")

    @pep_command.autocomplete("number")
    async def pep_number_completion(self, inter: disnake.ApplicationCommandInteraction, query: str) -> dict[str, int]:
        """Completion for pep numbers."""
        if not query:
            # return some interesting peps
            interesting_peps = [0, 8, 257, 517, 619, 660, 664]
            resp = {}
            for title, pep in self.autocomplete.items():
                if pep in interesting_peps:
                    resp[title] = pep
            return dict(sorted(resp.items(), key=lambda x: int(x[1])))

        peps: dict[str, int] = {}

        try:
            int(query)
        except ValueError:
            processor = lambda x: x[0]  # noqa: E731
            query = query.lower()
        else:
            processor = lambda x: str(x[1])  # noqa: E731

        processed = rapidfuzz.process.extract(
            (query, query),
            self.autocomplete.items(),
            scorer=rapidfuzz.fuzz.ratio,
            processor=processor,
            limit=11,
            score_cutoff=0,
        )

        for num, ((title, pep), score, _) in enumerate(processed):
            if num == 0:
                top_score = score

            if top_score > score + 24:
                break

            peps[title] = pep

        return peps

    @pep_command.autocomplete("header")
    async def pep_header_completion(self, inter: disnake.ApplicationCommandInteraction, query: str) -> dict[str, str]:
        """Completion for pep headers."""
        number = inter.filled_options.get("number")
        if number is None:
            return ["No PEP number provided.", "You must provide a valid pep number before providing a header."]
        if number not in self.peps:
            return [f"Cannot find PEP {number}.", "You must provide a valid pep number before providing a header."]

        _, soup = await self.fetch_pep_info(self.peps[number], number)

        headers = PEPHeaders().parse(soup)

        if not query:
            return list(headers)[:25]

        completion = []
        query = query.lower().strip()
        for header in headers:
            if query in header.lower():
                completion.append(header)
            if len(completion) >= 25:
                break

        return completion


def setup(bot: Monty) -> None:
    """Load the PEP cog."""
    bot.add_cog(PythonEnhancementProposals(bot))
