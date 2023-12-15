from __future__ import annotations

import asyncio
import copy
import dataclasses
import functools
import re
import sys
import textwrap
import typing
from collections import ChainMap, defaultdict
from functools import cached_property
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union

import aiohttp
import disnake
import rapidfuzz
import rapidfuzz.fuzz
import rapidfuzz.process
import sqlalchemy as sa
from disnake.ext import commands

from monty import constants
from monty.bot import Monty
from monty.database import PackageInfo
from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.converters import Inventory, PackageName, ValidURL
from monty.utils.helpers import maybe_defer
from monty.utils.inventory_parser import InvalidHeaderError, InventoryDict, fetch_inventory
from monty.utils.lock import SharedEvent, lock
from monty.utils.messages import DeleteButton, DeleteView
from monty.utils.pagination import LinePaginator
from monty.utils.scheduling import Scheduler

from . import NAMESPACE, PRIORITY_PACKAGES, _batch_parser, doc_cache


log = get_logger(__name__)

# symbols with a group contained here will get the group prefixed on duplicates
FORCE_PREFIX_GROUPS = (
    "term",
    "label",
    "token",
    "doc",
    "pdbcommand",
    "2to3fixer",
)
# Delay to wait before trying to reach a rescheduled inventory again, in minutes
FETCH_RESCHEDULE_DELAY = SimpleNamespace(first=2, repeated=5)

COMMAND_LOCK_SINGLETON = "inventory refresh"

DOCS_LINK_REGEX = re.compile(r"!`([\w.]+)`")
CUSTOM_ID_PREFIX = "docs_"

BLACKLIST: dict[int, set[str]] = {}
BLACKLIST_MAPPING: dict[int, list[str]] = {
    # constants.Guilds.nextcord: ["disnake", "dislash"],
}


@dataclasses.dataclass(unsafe_hash=True)
class DocItem:
    """Holds inventory symbol information."""

    package: str  # Name of the package name the symbol is from
    group: str  # Intersphinx "role" of the symbol, for example `label` or `method`
    base_url: str  # Absolute path to to which the relative path resolves, same for all items with the same package
    relative_url_path: str  # Relative path to the page where the symbol is located
    symbol_id: str  # Fragment id used to locate the symbol on the page
    symbol_name: str  # The key in the dictionary where this is found
    attributes: list[DocItem] = dataclasses.field(default_factory=list, hash=False, repr=False)

    @property
    def url(self) -> str:
        """Return the absolute url to the symbol."""
        return self.base_url + self.relative_url_path

    @property
    def key(self) -> str:
        """Returns a unique key for this DocItem."""
        return f"{self.package}:{self.relative_url_path.removesuffix('.html')}:{self.symbol_id}"


class DocView(DeleteView):
    """View for documentation objects."""

    def __init__(
        self, inter: Union[disnake.Interaction, commands.Context], bot: Monty, docitem: DocItem, og_embed: disnake.Embed
    ) -> None:
        super().__init__(user=inter.author, timeout=300)
        self.user_ids = [inter.author.id]
        self.bot = bot
        self.attributes = docitem.attributes
        self.docitem = docitem
        self.og_embed = og_embed

        self.set_link_button()

        self.sync_attribute_dropdown()

        self.attribute_select.placeholder += f" of {self.docitem.group} {self.docitem.symbol_name}"

        if not self.attribute_select.options:
            i = self.children.index(self.attribute_select)
            self.children.pop(i)
            i = self.children.index(self.return_home)
            self.children.pop(i)
            return

    def sync_attribute_dropdown(self, current_attribute: str = None) -> None:
        """Set up the attribute select menu."""
        self.attribute_select.options.clear()
        for attr in self.attributes[:25]:
            if attr == self.docitem:
                continue
            default = attr.symbol_name == current_attribute
            self.attribute_select.add_option(
                label=attr.symbol_name.removeprefix(self.docitem.symbol_name),
                description=attr.group,
                value=attr.symbol_name,
                default=default,
            )

    def set_link_button(self, url: str = None) -> None:
        """Set the link button to the provided url, or the default url."""
        if not hasattr(self, "go_to_doc"):
            self.go_to_doc = disnake.ui.Button(style=disnake.ButtonStyle.url, url="", label="Open docs")
            self.add_item(self.go_to_doc)
        self.go_to_doc.url = url or (self.docitem.url + "#" + self.docitem.symbol_id)

    async def doc_check(self, inter: disnake.Interaction) -> bool:
        """
        Check if the interaction author is whitelisted.

        Due to this sharing the delete button, this check must be independent.
        """
        if inter.author.id not in self.user_ids:
            await inter.send("You can press these, but they won't do anything for you!", ephemeral=True)
            return False
        return True

    @disnake.ui.select(placeholder="Attributes", custom_id=CUSTOM_ID_PREFIX + "attributes", row=0)
    async def attribute_select(self, select: disnake.ui.Select, inter: disnake.MessageInteraction) -> None:
        """Allow selecting an attribute of the initial view."""
        if not await self.doc_check(inter):
            return
        new_embed: disnake.Embed = (await self.bot.get_cog("Documentation").create_symbol_embed(select.values[0]))[0]
        self.set_link_button(new_embed.url)
        self.sync_attribute_dropdown(select.values[0])
        if inter.response.is_done():
            await inter.edit_original_message(embed=new_embed, view=self)
        else:
            await inter.response.edit_message(embed=new_embed, view=self)

    @disnake.ui.button(label="Home", custom_id=CUSTOM_ID_PREFIX + "home", row=1, style=disnake.ButtonStyle.blurple)
    async def return_home(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        """Reset to the home embed."""
        if not await self.doc_check(inter):
            return
        self.set_link_button()
        self.sync_attribute_dropdown()
        await inter.response.edit_message(embed=self.og_embed, view=self)

    def disable(self) -> None:
        """Disable all attributes in this view."""
        for c in self.children:
            if hasattr(c, "disabled") and c.is_dispatchable() and c is not self.delete_button:
                c.disabled = True


class DocCog(commands.Cog, name="Documentation", slash_command_attrs={"dm_permission": False}):
    """A set of commands for querying & displaying documentation."""

    def __init__(self, bot: Monty) -> None:
        # Contains URLs to documentation home pages.
        # Used to calculate inventory diffs on refreshes and to display all currently stored inventories.
        self.base_urls = {}
        self.bot = bot
        # the new doc_symbols that collects each package in their own dict and uses a chainmap
        self.doc_symbols_new: Dict[str, Dict[str, DocItem]] = {}
        self.item_fetcher = _batch_parser.BatchParser(self.bot)
        # Maps a conflicting symbol name to a list of the new, disambiguated names created from conflicts with the name.
        self.renamed_symbols = defaultdict(list)
        self.whitelist: Dict[int, Set[str]] = {}
        self.inventory_scheduler = Scheduler(self.__class__.__name__)

        self.refresh_event = asyncio.Event()
        self.refresh_event.set()
        self.symbol_get_event = SharedEvent()

    @lock(NAMESPACE, COMMAND_LOCK_SINGLETON, raise_error=True)
    async def cog_load(self) -> None:
        """Refresh inventories."""
        await self.refresh_inventories()

    @cached_property
    def doc_symbols(self) -> typing.ChainMap[str, DocItem]:
        """Maps symbol names to objects containing their metadata."""
        if not self.whitelist:
            return ChainMap(*self.doc_symbols_new.values())

        # exclude whitelist
        to_exclude = set()
        for packages in self.whitelist.values():
            to_exclude |= packages

        res = []
        for k, v in self.doc_symbols_new.items():
            if k in to_exclude:
                continue
            res.append(v)

        return ChainMap(*res)

    @property
    def doc_symbols_all(self) -> typing.ChainMap[str, DocItem]:
        """Returns all doc symbols, even whitelisted and blacklisted ones."""
        return ChainMap(*self.doc_symbols_new.values())

    def get_packages_for_guild(self, guild_id: int = None) -> typing.ChainMap[str, DocItem]:
        """Gets packages whitelisted in the specific guild."""
        if guild_id in self.whitelist:
            return ChainMap(*[self.doc_symbols_new[pkg] for pkg in self.whitelist[guild_id]], self.doc_symbols)
        return self.doc_symbols

    def _get_default_completion(
        self,
        inter: disnake.ApplicationCommandInteraction,
        guild: disnake.Guild = None,
    ) -> list[str]:
        if guild:
            if guild.id == constants.Guilds.disnake:
                return ["disnake", "disnake.ext.commands", "disnake.ext.tasks"]
            elif guild.id == constants.Guilds.nextcord:
                return ["nextcord", "nextcord.ext.commands", "nextcord.ext.tasks"]

        return [
            "__future__",
            "asyncio",
            "dataclasses",
            "datetime",
            "enum",
            "html",
            "http",
            "importlib",
            "inspect",
            "json",
            "logging",
            "os",
            "pathlib",
            "textwrap",
            "time",
            "traceback",
            "typing",
            "unittest",
            "warnings",
            "zipfile",
            "zipimport",
        ]

    async def init_refresh_inventory(self) -> None:
        """Refresh documentation inventory on cog initialization."""

    def update_single(
        self,
        package: PackageInfo,
        inventory: InventoryDict,
    ) -> None:
        """
        Build the inventory for a single package.

        Where:
            * `package_name` is the package name to use in logs and when qualifying symbols
            * `base_url` is the root documentation URL for the specified package, used to build
                absolute paths that link to specific symbols
            * `package` is the content of a intersphinx inventory.
        """
        self.base_urls[package.name] = package.base_url

        for group, items in inventory.items():
            for symbol_name, relative_doc_url, *_ in items:
                # e.g. get 'class' from 'py:class'
                group_name = group.split(":")[1]
                symbol_name = self.ensure_unique_symbol_name(
                    package.name,
                    group_name,
                    symbol_name,
                )

                relative_url_path, _, symbol_id = relative_doc_url.partition("#")
                # Intern fields that have shared content so we're not storing unique strings for every object
                doc_item = DocItem(
                    package.name,
                    sys.intern(group_name),
                    package.base_url,
                    sys.intern(relative_url_path),
                    symbol_id,
                    symbol_name,
                )
                self.doc_symbols_new.setdefault(package.name, {})[sys.intern(symbol_name)] = doc_item
                if package.guilds_blacklist:
                    for guild_id in package.guilds_blacklist:
                        if BLACKLIST.get(guild_id) is None:
                            BLACKLIST[guild_id] = set()
                        BLACKLIST[guild_id].add(symbol_name)

                if (
                    parent := self.doc_symbols_new[package.name].get(symbol_name.rsplit(".", 1)[0])
                ) and parent.package == package.name:
                    parent.attributes.append(doc_item)
                self.item_fetcher.add_item(doc_item)

        # delete the cached doc_symbols
        try:
            del self.doc_symbols
        except AttributeError:
            pass

        log.trace(f"Fetched inventory for {package.name}.")

    async def update_or_reschedule_inventory(self, package: PackageInfo, *, use_cache: bool = True) -> None:
        """
        Update the cog's inventories, or reschedule this method to execute again if the remote inventory is unreachable.

        The first attempt is rescheduled to execute in `FETCH_RESCHEDULE_DELAY.first` minutes, the subsequent attempts
        in `FETCH_RESCHEDULE_DELAY.repeated` minutes.
        """
        try:
            inventory = await fetch_inventory(self.bot, package.inventory_url, use_cache=use_cache)
        except InvalidHeaderError as e:
            # Do not reschedule if the header is invalid, as the request went through but the contents are invalid.
            log.warning(f"Invalid inventory header at {package.inventory_url}. Reason: {e}")
            return

        if not inventory:
            if package.name in self.inventory_scheduler:
                self.inventory_scheduler.cancel(package.name)
                delay = FETCH_RESCHEDULE_DELAY.repeated
            else:
                delay = FETCH_RESCHEDULE_DELAY.first
            log.info(f"Failed to fetch inventory; attempting again in {delay} minutes.")
            self.inventory_scheduler.schedule_later(
                delay * 60,
                package.name,
                self.update_or_reschedule_inventory(package, use_cache=use_cache),
            )
        else:
            # determine blacklist
            blacklist_guilds = []
            for g, packs in BLACKLIST_MAPPING.items():
                if package.name in packs:
                    blacklist_guilds.append(g)

            self.update_single(package, inventory)

    def ensure_unique_symbol_name(self, package_name: str, group_name: str, symbol_name: str) -> str:
        """
        Ensure `symbol_name` doesn't overwrite an another symbol in `doc_symbols`.

        For conflicts, rename either the current symbol or the existing symbol with which it conflicts.
        Store the new name in `renamed_symbols` and return the name to use for the symbol.

        If the existing symbol was renamed or there was no conflict, the returned name is equivalent to `symbol_name`.
        """
        if (item := self.doc_symbols_all.get(symbol_name)) is None:
            return symbol_name  # There's no conflict so it's fine to simply use the given symbol name.

        def rename(prefix: str, *, rename_extant: bool = False) -> str:
            new_name = f"{prefix}.{symbol_name}"
            if new_name in self.doc_symbols_all:
                # If there's still a conflict, qualify the name further.
                if rename_extant:
                    new_name = f"{item.package}.{item.group}.{symbol_name}"
                else:
                    new_name = f"{package_name}.{group_name}.{symbol_name}"

            self.renamed_symbols[symbol_name].append(new_name)

            if rename_extant:
                # Instead of renaming the current symbol, rename the symbol with which it conflicts.
                conflicting_symbol = self.doc_symbols_all[symbol_name]
                package = conflicting_symbol.package
                self.doc_symbols_new[package][sys.intern(new_name)] = conflicting_symbol
                return symbol_name
            else:
                return new_name

        # When there's a conflict, and the package names of the items differ, use the package name as a prefix.
        if package_name != item.package:
            if package_name in PRIORITY_PACKAGES:
                return rename(item.package, rename_extant=True)
            else:
                return rename(package_name)

        # If the symbol's group is a non-priority group from FORCE_PREFIX_GROUPS,
        # add it as a prefix to disambiguate the symbols.
        elif group_name in FORCE_PREFIX_GROUPS:
            if item.group in FORCE_PREFIX_GROUPS:
                needs_moving = FORCE_PREFIX_GROUPS.index(group_name) < FORCE_PREFIX_GROUPS.index(item.group)
            else:
                needs_moving = False
            return rename(item.group if needs_moving else group_name, rename_extant=needs_moving)

        # If the above conditions didn't pass, either the existing symbol has its group in FORCE_PREFIX_GROUPS,
        # or deciding which item to rename would be arbitrary, so we rename the existing symbol.
        else:
            return rename(item.group, rename_extant=True)

    async def refresh_whitelist_and_blacklist(self) -> None:
        """Refresh internal whitelist and blacklist."""
        self.whitelist.clear()

        async with self.bot.db.begin() as session:
            stmt = sa.select(PackageInfo).where(PackageInfo.guilds_whitelist != None)  # noqa: E711
            result = await session.scalars(stmt)
            guilds_whitelist = result.all()

        for package in guilds_whitelist:
            for guild_id in package.guilds_whitelist:  # type: ignore # guilds_whitelist will always be iterable here
                self.whitelist.setdefault(guild_id, set())
                self.whitelist[guild_id].add(package.name)

        # delete the cached doc_symbols
        try:
            del self.doc_symbols
        except AttributeError:
            pass
        log.debug("Finished setting up the whitelist.")

    async def refresh_inventories(self, *, use_cache: bool = True) -> None:
        """Refresh internal documentation inventories."""
        self.refresh_event.clear()
        await self.symbol_get_event.wait()
        log.debug("Refreshing documentation inventory...")
        self.inventory_scheduler.cancel_all()

        self.base_urls.clear()
        self.doc_symbols_new.clear()
        self.renamed_symbols.clear()
        await self.item_fetcher.clear()
        # delete the cached doc_symbols
        try:
            del self.doc_symbols
        except AttributeError:
            pass

        async with self.bot.db.begin() as session:
            stmt = sa.select(PackageInfo)
            result = await session.scalars(stmt)
            packages = result.all()

        coros = [self.update_or_reschedule_inventory(package, use_cache=use_cache) for package in packages]
        await asyncio.gather(*coros)
        log.debug("Finished inventory refresh.")
        log.debug("Refreshing whitelist and blacklist")
        await self.refresh_whitelist_and_blacklist()
        try:
            del self.doc_symbols
        except AttributeError:
            pass
        # recompute the symbols
        _ = self.doc_symbols
        self.refresh_event.set()

    def get_symbol_item(self, symbol_name: str) -> Tuple[str, Optional[DocItem]]:
        """
        Get the `DocItem` and the symbol name used to fetch it from the `doc_symbols` dict.

        If the doc item is not found directly from the passed in name and the name contains a space,
        the first word of the name will be attempted to be used to get the item.
        """
        doc_item = self.doc_symbols_all.get(symbol_name)
        if doc_item is None and " " in symbol_name:
            symbol_name = symbol_name.split(" ", maxsplit=1)[0]
            doc_item = self.doc_symbols_all.get(symbol_name)

        return symbol_name, doc_item

    async def get_symbol_markdown(self, doc_item: DocItem) -> str:
        """
        Get the Markdown from the symbol `doc_item` refers to.

        First a redis lookup is attempted, if that fails the `item_fetcher`
        is used to fetch the page and parse the HTML from it into Markdown.
        """
        markdown = await doc_cache.get(doc_item)

        if markdown is None:
            log.debug(f"Redis cache miss with {doc_item}.")
            try:
                markdown = await self.item_fetcher.get_markdown(doc_item)

            except aiohttp.ClientError as e:
                log.warning(f"A network error has occurred when requesting parsing of {doc_item}.", exc_info=e)
                return "Unable to parse the requested symbol due to a network error."

            except Exception:
                log.exception(f"An unexpected error has occurred when requesting parsing of {doc_item}.")
                return "Unable to parse the requested symbol due to an error."

            if markdown is None:
                return "Unable to parse the requested symbol."
        return markdown

    async def create_symbol_embed(
        self,
        symbol_name: str,
    ) -> Optional[tuple[disnake.Embed, DocItem]]:
        """
        Attempt to scrape and fetch the data for the given `symbol_name`, and build an embed from its contents.

        If the symbol is known, an Embed with documentation about it is returned.

        First check the DocRedisCache before querying the cog's `BatchParser`.
        """
        log.trace(f"Building embed for symbol `{symbol_name}`")
        if not self.refresh_event.is_set():
            log.debug("Waiting for inventories to be refreshed before processing item.")
            await self.refresh_event.wait()
        # Ensure a refresh can't run in case of a context switch until the with block is exited
        with self.symbol_get_event:
            symbol_name, doc_item = self.get_symbol_item(symbol_name)
            if doc_item is None:
                log.debug("Symbol does not exist.")
                return None

            # Show all symbols with the same name that were renamed in the footer,
            # with a max of 200 chars.
            if symbol_name in self.renamed_symbols:
                renamed_symbols = ", ".join(self.renamed_symbols[symbol_name])
                footer_text = textwrap.shorten("Similar names: " + renamed_symbols, 200, placeholder=" ...")
            else:
                footer_text = ""

            embed = disnake.Embed(
                title=disnake.utils.escape_markdown(symbol_name),
                url=f"{doc_item.url}#{doc_item.symbol_id}",
                description=await self.get_symbol_markdown(doc_item),
            )
            embed.set_footer(text=footer_text)
            return embed, doc_item

    def _get_link_from_inventories(self, package: str) -> Optional[str]:
        if package in self.base_urls:
            return self.base_urls[package]

        return None

    @commands.group(name="docs", aliases=("doc", "d"), invoke_without_command=True)
    async def docs_group(self, ctx: commands.Context, *, search: Optional[str]) -> None:
        """Look up documentation for Python symbols."""
        await self._docs_get_command(ctx, search=search)

    @commands.slash_command(name="docs", dm_permission=False)
    async def slash_docs(self, inter: disnake.AppCmdInter) -> None:
        """Search python package documentation."""
        pass

    async def maybe_pypi_docs(self, package: str, strip: bool = True) -> tuple[bool, Optional[str]]:
        """Find the documentation url on PyPI for a given package."""
        if (pypi := self.bot.get_cog("PyPI")) is None:
            return False, None
        if pypi.check_characters(package):
            return False, None
        if strip:
            package = package.split(".")[0]

        json = await pypi.fetch_package(package)
        if not json:
            return False, None
        info = json["info"]
        project_urls = info["project_urls"] or {}
        docs = info.get("docs_url") or project_urls.get("Documentation")
        if docs:
            return True, docs

        return False, info.get("home_page") or project_urls.get("Homepage") or project_urls.get("Home")

    async def _docs_get_command(
        self,
        inter: Union[disnake.ApplicationCommandInteraction, commands.Context],
        search: Optional[str],
        maybe_start: bool = True,
        *,
        return_embed: bool = False,
        threshold: commands.Range[0, 100] = 60,
        scorer: Any = None,
    ) -> None:
        if not search:
            inventory_embed = disnake.Embed(
                title=f"All inventories (`{len(self.base_urls)}` total)", colour=disnake.Colour.blue()
            )

            lines = sorted(f"• [`{name}`]({url})" for name, url in self.base_urls.items())
            if self.base_urls:
                await LinePaginator.paginate(lines, inter, inventory_embed, max_size=400, empty=False)

            else:
                inventory_embed.description = "Hmmm, seems like there's nothing here yet."
                await inter.send(embed=inventory_embed)

        else:
            symbol = search.strip("`")
            no_match = False
            tries = [symbol]
            if maybe_start:
                tries.append(symbol.split()[0])
            for sym in tries:
                sym = await self._docs_autocomplete(inter, sym, threshold=threshold, scorer=scorer)
                if sym:
                    sym = sym[0]
                    break
            else:
                no_match = True
                sym = None

            res = None
            if not no_match:
                if isinstance(inter, disnake.Interaction):
                    maybe_defer(inter)
                elif hasattr(inter, "trigger_typing"):
                    await inter.trigger_typing()
                elif isinstance(inter, disnake.Message):
                    await inter.channel.trigger_typing()
                res = await self.create_symbol_embed(sym)
            if return_embed:
                return res[0] if res else None

            if not res:
                error_text = f"No documentation found for `{symbol}`."

                maybe_package = symbol.split()[0]
                maybe_docs = (
                    self._get_link_from_inventories(maybe_package) or (await self.maybe_pypi_docs(maybe_package))[1]
                )
                if maybe_docs:
                    error_text += f"\nYou may find what you're looking for at <{maybe_docs}>"
                if isinstance(inter, disnake.Interaction):
                    await inter.send(error_text, ephemeral=True)
                else:
                    await inter.send(
                        error_text,
                        allowed_mentions=disnake.AllowedMentions.none(),
                        components=DeleteButton(inter.author),
                    )
                return

            doc_embed, doc_item = res
            view = DocView(inter, self.bot, doc_item, doc_embed)
            msg = await inter.send(embed=doc_embed, view=view)
            await view.wait()
            view.disable()
            if getattr(view, "deleted", False):
                return
            try:
                if msg is not None:
                    await msg.edit(view=view)
                else:
                    await inter.edit_original_message(view=view)
            except disnake.HTTPException:
                pass

    @slash_docs.sub_command("view")
    async def docs_get_command(self, inter: disnake.ApplicationCommandInteraction, query: Optional[str]) -> None:
        """
        Gives you a documentation link for a provided entry.

        Parameters
        ----------
        query: the object to view the docs
        """
        await self._docs_get_command(inter, query, maybe_start=False)

    async def _docs_autocomplete(
        self,
        inter: disnake.Interaction,
        query: str,
        *,
        count: int = 24,
        threshold: int = 45,
        scorer: Any = None,
        include_query: bool = False,
    ) -> list[str]:
        """
        Autocomplete for the search param for documentation.

        Parameters
        ----------
        inter: the autocomplete interaction
        query: the partial query by the user
        count: the number of results to return
        threshold: the minimum score to return
        scorer: the scorer to use
        include_query: whether to include the query in the results
        """
        log.info(f"Received autocomplete inter by {inter.author}: {query}")
        if not query:
            return self._get_default_completion(inter, inter.guild)
        # ----------------------------------------------------
        guild_id = inter.guild and inter.guild.id or inter.guild_id
        blacklist = BLACKLIST_MAPPING.get(guild_id)

        query = query.strip()

        packages = self.get_packages_for_guild(guild_id)

        def processor(sentence: str) -> str:
            if (sym := self.doc_symbols_all.get(sentence)) and sym.package in blacklist:
                return ""
            else:
                return sentence

        # further fuzzy search by using rapidfuzz ratio matching
        fuzzed = rapidfuzz.process.extract(
            query=query,
            choices=packages.keys(),
            scorer=scorer or rapidfuzz.fuzz.ratio,
            processor=processor if blacklist else None,
            limit=count,
        )

        tweak = []
        lower_query = query.lower()
        for _, (name, score, _) in enumerate(fuzzed):
            lower = name.lower()

            if lower == query:
                score += 50

            if lower_query in lower:
                score += 20

            tweak.append((name, score))

        tweak = sorted(tweak, key=lambda v: v[1], reverse=True)

        res = []
        if include_query:
            res.append(query)
        for name, score in tweak:
            if score < threshold:
                break
            res.append(name)
        return res

    docs_get_command.autocomplete("query")(copy.copy(_docs_autocomplete))

    @slash_docs.sub_command(name="list")
    async def slash_docs_search(self, inter: disnake.AppCmdInter, query: str) -> None:
        """
        [BETA] Search documentation and provide a list of results.

        Parameters
        ----------
        query: search query
        """
        results = {}
        guild_id = inter.guild and inter.guild.id or inter.guild_id
        blacklist = BLACKLIST_MAPPING.get(guild_id)

        query = query.strip()

        packages = self.get_packages_for_guild(guild_id)

        for key, item in packages.items():
            if query not in key:
                continue

            if blacklist and item.package in blacklist:
                continue

            results[key] = item.url + "#" + item.symbol_id
            if len(results) >= 10:
                break
        # if no results
        if not results:
            await inter.response.send_message(f"No documentation results found for `{query}`.", ephemeral=True)
            return
        # construct embed
        results = dict(sorted(results.items(), key=lambda x: x[0]))

        embed = disnake.Embed(title=f"Results for {query}")
        embed.description = ""
        for res, url in results.items():
            embed.description += f"[`{res}`]({url})\n"

        components = DeleteButton(inter.author)
        await inter.response.send_message(embed=embed, components=components)

    slash_docs_search.autocomplete("query")(functools.partial(_docs_autocomplete, include_query=True))

    @slash_docs.sub_command(name="find_url")
    async def slash_docs_find_url(
        self,
        inter: disnake.ApplicationCommandInteraction,
        package: str,
    ) -> None:
        """
        Find a package's documentation from the existing inventories or PyPI.

        Parameters
        ----------
        package: Uses the internal information, checks PyPI otherwise.
        """
        if not (pypi := self.bot.get_cog("PyPI")):
            await inter.send("Sorry, I'm unable to process this at the moment!", ephemeral=True)
            return

        if characters := pypi.check_characters(package):
            await inter.send(
                f"Illegal character(s) passed into command: '{disnake.utils.escape_markdown(characters.group(0))}'",
                ephemeral=True,
            )
            return

        link = self._get_link_from_inventories(package)
        if not link:
            # check PyPI
            res = await self.maybe_pypi_docs(package, strip=False)
            if res[0]:
                link = res[1]

        components = DeleteButton(inter.author)
        if link:
            await inter.send(f"Found documentation for {package} at <{link}>.", components=components)
            return
        else:
            msg = f"No docs found for {package}."
            if res[1]:
                msg += f"\nHowever, I did find this homepage while looking: <{res[1]}>."
            await inter.send(msg, components=components)

    @docs_group.command(name="setdoc", aliases=("s",))
    @lock(NAMESPACE, COMMAND_LOCK_SINGLETON, raise_error=True)
    @commands.is_owner()
    async def set_command(
        self,
        ctx: commands.Context,
        package_name: PackageName,
        inventory: Inventory,
        base_url: ValidURL = "",
    ) -> None:
        """
        Adds a new documentation metadata object to the site's database.

        The database will update the object, should an existing item with the specified `package_name` already exist.
        If the base url is not specified, a default created by removing the last segment of the inventory url is used.

        Example:
            !docs setdoc \
                    python \
                    https://docs.python.org/3/objects.inv
        """
        if base_url and not base_url.endswith("/"):
            raise commands.BadArgument("The base url must end with a slash.")

        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)

        inventory_url, inventory_dict = inventory

        async with self.bot.db.begin() as session:
            stmt = sa.select(PackageInfo).where(PackageInfo.name == package_name)
            package = await session.scalar(stmt)

            if package:
                await ctx.send(":x: That package is already added!", components=components)
                return

            package = PackageInfo(name=package_name, inventory_url=str(inventory_url), base_url=base_url)
            session.add(package)
            await session.commit()

        log.info(f"User @{ctx.author} ({ctx.author.id}) added a new documentation package:\n" + repr(package))

        self.update_single(package, inventory_dict)
        await ctx.send(
            f"Added the package `{package.name}` to the database and updated the inventories.", components=components
        )

    @docs_group.command(name="deletedoc", aliases=("removedoc", "rm", "d"))
    @lock(NAMESPACE, COMMAND_LOCK_SINGLETON, raise_error=True)
    @commands.is_owner()
    async def delete_command(self, ctx: commands.Context, package_name: PackageName) -> None:
        """
        Removes the specified package from the database.

        Example:
            !docs deletedoc aiohttp
        """
        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        async with ctx.typing():
            async with self.bot.db_engine.begin() as conn:
                stmt = sa.delete(PackageInfo).where(PackageInfo.name == package_name)
                result = await conn.execute(stmt)
                if result.rowcount != 1:
                    await conn.rollback()
                    await ctx.send(":x: No package found with that name.", components=components)
                    return
                await conn.commit()

            await self.refresh_inventories()
            await doc_cache.delete(package_name)

        await ctx.send(f"Successfully deleted `{package_name}` and refreshed the inventories.", components=components)

    @docs_group.command(name="refreshdoc", aliases=("rfsh", "r"))
    @commands.is_owner()
    @lock(NAMESPACE, COMMAND_LOCK_SINGLETON, raise_error=True)
    async def refresh_command(self, ctx: commands.Context) -> None:
        """Refresh inventories and show the difference."""
        old_inventories = set(self.base_urls)
        with ctx.typing():
            await self.refresh_inventories(use_cache=False)
        new_inventories = set(self.base_urls)

        if added := ", ".join(new_inventories - old_inventories):
            added = "+ " + added

        if removed := ", ".join(old_inventories - new_inventories):
            removed = "- " + removed

        embed = disnake.Embed(
            title="Inventories refreshed", description=f"```diff\n{added}\n{removed}```" if added or removed else ""
        )

        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(embed=embed, components=components)

    @docs_group.command(name="cleardoccache", aliases=("deletedoccache",))
    @commands.is_owner()
    async def clear_cache_command(
        self, ctx: commands.Context, package_name: Union[PackageName, Literal["*"]]  # noqa: F722
    ) -> None:
        """Clear the persistent redis cache for `package`."""
        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)

        if await doc_cache.delete(package_name):
            await self.item_fetcher.stale_inventory_notifier.symbol_counter.delete(package_name)
            await ctx.send(f"Successfully cleared the cache for `{package_name}`.", components=components)
        else:
            await ctx.send("No keys matching the package found.", components=components)

    @commands.is_owner()
    @docs_group.group(name="whitelist", aliases=("wh",), invoke_without_command=True)
    async def whitelist_command_group(self, ctx: commands.Context) -> None:
        """Whitelist command management. Limits a package to specific guilds."""
        await self.list_whitelist(ctx)

    @commands.is_owner()
    @whitelist_command_group.command(name="list", aliases=("l",))
    async def list_whitelist(self, ctx: commands.Context) -> None:
        """List the whitelisted packages."""
        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)

        if not self.whitelist:
            await ctx.send("No packages are whitelisted.", components=components)
            return
        embed = disnake.Embed(title="Whitelisted packages")
        for guild, packages in self.whitelist.items():
            embed.add_field(self.bot.get_guild(guild).name + f" ({guild})", ", ".join(sorted(packages)))
        await ctx.send(embed=embed, components=components)

    @commands.is_owner()
    @whitelist_command_group.command(name="add", aliases=("a",))
    async def whitelist_command(self, ctx: commands.Context, package_name: PackageName, *guilds: disnake.Guild) -> None:
        """
        Whitelist a package in a guild.

        Example:
            -docs whitelist python 123456789012345678
        """
        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)

        if not guilds:
            await ctx.send(":x: You must specify at least one guild.", components=components)
            return

        async with self.bot.db.begin() as session:
            package = await session.get(PackageInfo, package_name)
            if not package:
                await ctx.send(":x: No package found with that name.", components=components)
                return

            guild_ids = [g.id for g in guilds]

            whitelist: List[int] = package.guilds_whitelist or []
            for guild_id in guild_ids:
                if guild_id in whitelist:
                    log.debug(f"{package_name} is already whitelisted in {guild_id}")
                    continue
                whitelist.append(guild_id)

            package.guilds_whitelist = whitelist
            await session.commit()

        await self.refresh_whitelist_and_blacklist()

        await ctx.send(
            f"Successfully whitelisted `{package_name}` in the following guilds:"
            f" {', '.join([str(x) for x in guild_ids])}",  # noqa: E501
            components=components,
        )

    @commands.is_owner()
    @whitelist_command_group.command(name="remove", aliases=("r",))
    async def unwhitelist_command(
        self, ctx: commands.Context, package_name: PackageName, *guilds: disnake.Guild
    ) -> None:
        """
        Unwhitelist a package in a guild.

        Example:
            -docs unwhitelist python 123456789012345678
        """
        components = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)

        if not guilds:
            await ctx.send(":x: You must specify at least one guild.", components=components)
            return

        async with self.bot.db.begin() as session:
            package = await session.get(PackageInfo, package_name)

            if not package:
                await ctx.send(":x: No package found with that name.", components=components)
                return

            if not package.guilds_whitelist:
                await ctx.send("No whitelist configured for that package.", components=components)
                return

            guild_ids = [g.id for g in guilds]
            whitelist: List[int] = package.guilds_whitelist
            for guild_id in guild_ids:
                if guild_id not in whitelist:
                    log.debug(f"{package_name} is not whitelisted in {guild_id}")
                    continue
                whitelist.remove(guild_id)

            package.guilds_whitelist = whitelist
            await session.commit()

        await self.refresh_whitelist_and_blacklist()

        await ctx.send(
            f"Successfully de-whitelisted `{package_name}` in the following guilds:"
            f" {', '.join([str(x) for x in guild_ids])}",  # noqa: E501
            components=components,
        )

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        """Echo docs if found and they match a regex."""
        if not message.guild:
            return
        if message.author.bot:
            return

        if not await self.bot.guild_has_feature(message.guild, constants.Feature.INLINE_DOCS):
            return

        matches: list[str] = list(dict.fromkeys(list(DOCS_LINK_REGEX.findall(message.content))[:10], None))
        if not matches:
            return

        tasks = []
        for match in matches:
            tasks.append(
                self._docs_get_command(
                    message, match, return_embed=True, threshold=100, scorer=rapidfuzz.fuzz.partial_ratio
                )
            )

        embeds = [e for e in await asyncio.gather(*tasks) if isinstance(e, disnake.Embed)]
        if not embeds:
            return

        components = DeleteButton(message.author)
        await message.channel.send(embeds=embeds, components=components)

    def cog_unload(self) -> None:
        """Clear scheduled inventories, queued symbols and cleanup task on cog unload."""
        self.inventory_scheduler.cancel_all()
        scheduling.create_task(self.item_fetcher.clear(), name="DocCog.item_fetcher unload clear")
