import asyncio
import inspect
import random
import re
import reprlib
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Optional, Tuple, TypeVar, Union
from urllib.parse import urldefrag

import disnake
import rapidfuzz
import rapidfuzz.distance.JaroWinkler
import rapidfuzz.process
from disnake.ext import commands

from monty.bot import Monty
from monty.constants import Client, Feature, Source
from monty.log import get_logger
from monty.utils.converters import SourceConverter, SourceType
from monty.utils.helpers import encode_github_link
from monty.utils.messages import DeleteButton


commands.register_injection(SourceConverter.convert)
SourceConverterAnn = SourceConverter

if TYPE_CHECKING:
    SourceConverterAnn = SourceType

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


# todo: move to utils
COG_NAME_REGEX = re.compile(r"((?<=[a-z])[A-Z]|(?<=[a-zA-Z])[A-Z](?=[a-z]))")

logger = get_logger(__name__)


class FrozenChainMap(Mapping[K, V]):
    """Copied from collections.ChainMap but does not inherit from mutable mapping."""

    def __init__(self, *maps: Mapping[K, V]) -> None:
        self.maps = list(maps) or [{}]  # always at least one map

    def __missing__(self, key: K):
        raise KeyError(key)

    def __getitem__(self, key: K) -> V:
        for mapping in self.maps:
            try:
                return mapping[key]  # can't use 'key in mapping' with defaultdict
            except KeyError:
                pass
        return self.__missing__(key)  # support subclasses that define __missing__

    def get(self, key: K, default: V = None) -> Union[K, V]:
        """Get the object at the provided key."""
        return self[key] if key in self else default

    def __len__(self) -> int:
        return len(set().union(*self.maps))  # reuses stored hash values if possible

    def __iter__(self) -> Iterator:
        d = {}
        for mapping in reversed(self.maps):
            d.update(dict.fromkeys(mapping))  # reuses stored hash values if possible
        return iter(d)

    def __contains__(self, key: K) -> bool:
        return any(key in m for m in self.maps)

    def __bool__(self) -> bool:
        return any(self.maps)

    @reprlib.recursive_repr()
    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({", ".join(map(repr, self.maps))})'

    @classmethod
    def fromkeys(cls, iterable: Iterable[K], *args: V) -> "FrozenChainMap[K, V]":
        """Create a ChainMap with a single dict created from the iterable."""
        return cls(dict.fromkeys(iterable, *args))

    def new_child(self, m: Mapping[K, Any] = None) -> "FrozenChainMap[K, V]":  # like Django's Context.push()
        """
        New ChainMap with a new map followed by all previous maps.

        If no map is provided, an empty dict is used.
        """
        if m is None:
            m = {}
        return self.__class__(m, *self.maps)


class MetaSource(commands.Cog, name="Meta Source", slash_command_attrs={"dm_permission": False}):
    """Display information about my own source code."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        # todo: add more features to the values, typed as object for now
        self.objects: dict[str, object] = {}
        self.refresh_active = asyncio.Lock()
        self._cog_ready = asyncio.Event()

    @commands.Cog.listener("on_cog_load")
    @commands.Cog.listener("on_cog_remove")
    @commands.Cog.listener("on_command_add")
    @commands.Cog.listener("on_command_remove")
    @commands.Cog.listener("on_slash_command_add")
    @commands.Cog.listener("on_slash_command_remove")
    async def refresh_cache(
        self, obj: Optional[Union[commands.Command, commands.Cog, commands.InvokableSlashCommand]] = None
    ) -> None:
        """Refreshes the cache when a cog is added or removed."""
        # sleep for a second in the event that multiple cogs are reloaded or commands are added/removed
        # do nothing if the cog is this cog
        if obj and (
            obj is self or (isinstance(obj, (commands.Command, commands.InvokableSlashCommand)) and obj.cog is self)
        ):
            logger.debug("returning early as our own cog was acted upon")
            return
        if self.refresh_active.locked():
            logger.trace("Received an event to refresh the cache but cache refresh is already active.")
            return
        self._cog_ready.clear()
        logger.info("Starting source command autocomplete refresh.")
        async with self.refresh_active:
            # wait until first connect as all cogs are loaded at that point
            await self.bot.wait_until_first_connect()
            await asyncio.sleep(2)

            # create the feature in the most stupid way possible
            await self.bot.guild_has_feature(None, Feature.SOURCE_AUTOCOMPLETE)

            # these are already proxies
            self.all_cogs = self.bot.cogs
            self.all_extensions = self.bot.extensions

            # todo: this will need to be synced
            self.all_prefix_commands: dict[str, Union[commands.Command, commands.Group]] = {}
            for cmd in self.bot.walk_commands():
                self.all_prefix_commands[cmd.qualified_name] = cmd

            # also need to add children, hence why this is a copy
            self.all_slash_commands: dict[
                str, Union[commands.InvokableSlashCommand, commands.SubCommand, commands.SubCommandGroup]
            ] = {}
            self.all_slash_commands.update(self.bot.all_slash_commands)

            for command in self.bot.all_slash_commands.values():
                if not command.children:
                    continue

                children = command.children

                for child in children.values():
                    self.all_slash_commands[child.qualified_name] = child
                    if isinstance(child, commands.SubCommandGroup) and child.children:
                        for sub_child in child.children.values():
                            self.all_slash_commands[sub_child.qualified_name] = sub_child

            self.all_message_commands = types.MappingProxyType(self.bot.all_message_commands)
            self.all_user_commands = types.MappingProxyType(self.bot.all_user_commands)

            # map all objects into one mapping
            # for naming priorities, it is as follows
            self.all_objects = FrozenChainMap[str, SourceType](
                self.all_slash_commands,
                self.all_message_commands,
                self.all_user_commands,
                self.all_prefix_commands,
                self.all_cogs,
                # self.all_extensions,
            )

            # cache display names to their actual names which can be retrieved from the dict above
            self.object_display_names: dict[str, str] = {}
            self.object_display_names.update({
                name: name for name, obj in self.all_objects.items() if not isinstance(obj, commands.Cog)
            })
            self.object_display_names.update({
                COG_NAME_REGEX.sub(r" \1", name): name for name in self.all_cogs if name != "PyPI"
            })

            unsorted = self.object_display_names.copy()
            self.object_display_names.clear()
            self.object_display_names.update(sorted(unsorted.items()))

            # sleep for a moment to catch any pending events and yield to them
            await asyncio.sleep(2)
            logger.info("Refreshed source command autocomplete.")
        self._cog_ready.set()

    async def cog_load(self) -> None:
        """Refresh the cache when the cog is loaded."""
        await self.refresh_cache()

    @commands.command(name="source", aliases=("src",))
    async def source_command(
        self,
        ctx: Union[commands.Context, disnake.ApplicationCommandInteraction],
        *,
        source_item: SourceConverterAnn = None,
    ) -> None:
        """Display information and a GitHub link to the source code of a command or cog."""

        async def send_message(embed: disnake.Embed, components: list = None) -> None:
            components = components or []
            if isinstance(ctx, commands.Context):
                components.insert(0, DeleteButton(ctx.author, initial_message=ctx.message))
            else:
                components.insert(0, DeleteButton(ctx.author))
            await ctx.send(embed=embed, components=components)
            return

        if not source_item:
            embed = disnake.Embed(title=f"{Client.name}'s GitHub Repository")
            embed.add_field(name="Repository", value=f"[Go to GitHub]({Source.github})")
            embed.set_thumbnail(url=Source.github_avatar_url)
            components = [disnake.ui.Button(url=Source.github, label="Open Github")]
            await send_message(embed, components)
            return

        embed, url = self.build_embed(source_item)
        components = [disnake.ui.Button(url=url, label="Open Github")]

        custom_id = encode_github_link(url)
        if frag := (urldefrag(url)[1]):
            frag = frag.replace("#", "").replace("L", "")
            num1, num2 = frag.split("-")
            if int(num2) - int(num1) < 30:
                components.append(
                    disnake.ui.Button(style=disnake.ButtonStyle.blurple, label="Expand", custom_id=custom_id)
                )

        await send_message(embed, components=components)

    @commands.slash_command(name="source")
    async def source_slash_command(self, inter: disnake.ApplicationCommandInteraction, item: str) -> None:
        """
        Get the source of my commands and cogs.

        Parameters
        ----------
        item: The command or cog to display the source code of.
        """
        # manual conversion (ew) because of injections & autocomplete bug
        try:
            source_item = await SourceConverter.convert(inter, item)
        except commands.CommandError:
            raise
        except Exception as e:
            raise commands.ConversionError(SourceConverter, e) from e
        await self.source_command(inter, source_item=source_item)  # type: ignore # inter is invalid for some reason

    @source_slash_command.autocomplete("item")
    async def source_autocomplete(self, inter: disnake.CommandInteraction, query: str) -> dict[str, str]:
        """Implement autocomplete for the meta source command."""
        # shortcircuit if the feature is not enabled
        new_autocomplete = await self.bot.guild_has_feature(inter.guild_id, Feature.SOURCE_AUTOCOMPLETE)
        if not new_autocomplete:
            return {query: query} if query else {}

        await self._cog_ready.wait()
        # todo: weight the first results based on usage
        scorer = rapidfuzz.distance.JaroWinkler.similarity  # type: ignore # this is defined

        if not query:
            # we need to shortcircuit and skip the fuzzing results
            return dict(random.sample(list(self.object_display_names.items()), k=25))

        fuzz_results = rapidfuzz.process.extract(
            query,
            self.object_display_names,
            scorer=scorer,  # type: ignore
            limit=25,
            score_cutoff=0.4,
        )

        # make the completion
        return {key: value for value, score, key in fuzz_results}

    def get_source_link(self, source_item: SourceType) -> Tuple[str, str, Optional[int]]:
        """
        Build GitHub link of source item, return this link, file location and first line number.

        Raise BadArgument if `source_item` is a dynamically-created object (e.g. via internal eval).
        """
        if isinstance(
            source_item,
            (
                commands.InvokableSlashCommand,
                commands.SubCommandGroup,
                commands.SubCommand,
                commands.InvokableUserCommand,
                commands.InvokableMessageCommand,
            ),
        ):
            meth: Callable[..., Any] = inspect.unwrap(source_item.callback)
            src = meth.__code__
            filename = src.co_filename
        elif isinstance(source_item, commands.Command):
            callback: Callable[..., Any] = inspect.unwrap(source_item.callback)
            src = callback.__code__
            filename = src.co_filename
        else:
            src = type(source_item)
            try:
                filename = inspect.getsourcefile(src)
            except TypeError:
                filename = None
            if filename is None:
                raise commands.BadArgument("Cannot get source for a dynamically-created object.")

        if not isinstance(source_item, str):
            try:
                lines, first_line_no = inspect.getsourcelines(src)
            except OSError:
                raise commands.BadArgument("Cannot get source for a dynamically-created object.") from None

            lines_extension = f"#L{first_line_no}-L{first_line_no+len(lines)-1}"
        else:
            first_line_no = None
            lines_extension = ""

        file_location = Path(filename).relative_to(Path.cwd()).as_posix()

        url = f"{Source.github}/blob/{Client.version}/{file_location}{lines_extension}"

        return url, file_location, first_line_no or None

    def build_embed(self, source_object: SourceType) -> Tuple[disnake.Embed, str]:
        """Build embed based on source object."""
        url, location, first_line = self.get_source_link(source_object)

        if isinstance(source_object, commands.Command):
            description = source_object.short_doc
            title = f"Command: {source_object.qualified_name}"
        elif isinstance(source_object, commands.InvokableSlashCommand):
            title = f"Slash Command: {source_object.qualified_name}"
            description = source_object.description
        elif isinstance(source_object, commands.SubCommandGroup):
            title = f"Slash Sub Command Group: {source_object.qualified_name}"
            description = inspect.cleandoc(source_object.callback.__doc__ or "").split("\n", 1)[0]
        elif isinstance(source_object, commands.SubCommand):
            title = f"Slash Sub-Command: {source_object.qualified_name}"
            description = source_object.option.description
        elif isinstance(source_object, commands.InvokableUserCommand):
            title = f"User Command: {source_object.qualified_name}"
            description = inspect.cleandoc(source_object.callback.__doc__ or "").split("\n", 1)[0]
        elif isinstance(source_object, commands.InvokableMessageCommand):
            title = f"Message Command: {source_object.qualified_name}"
            description = inspect.cleandoc(source_object.callback.__doc__ or "").split("\n", 1)[0]
        else:
            title = f"Cog: {source_object.qualified_name}"
            description = (source_object.description or "").split("\n", 1)[0]

        embed = disnake.Embed(title=title, description=description)
        embed.set_thumbnail(url=Source.github_avatar_url)
        embed.add_field(name="Source Code", value=f"[Go to GitHub]({url})")
        line_text = f":{first_line}" if first_line else ""
        embed.set_footer(text=f"{location}{line_text}")

        return embed, url


def setup(bot: Monty) -> None:
    """Load the MetaSource cog."""
    bot.add_cog(MetaSource(bot))
