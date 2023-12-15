import asyncio
from typing import Iterable, List, Optional, Tuple, Union, cast

import disnake
from disnake.ext import commands

from monty.constants import Emojis
from monty.log import get_logger
from monty.utils import scheduling


FIRST_EMOJI = "\u23ee"  # [:track_previous:]
LEFT_EMOJI = "\u2b05"  # [:arrow_left:]
RIGHT_EMOJI = "\u27a1"  # [:arrow_right:]
LAST_EMOJI = "\u23ed"  # [:track_next:]
DELETE_EMOJI = Emojis.trashcan  # [:trashcan:]
CUSTOM_ID_PREFIX = "paginator_page_"
PAGINATION_EMOJI: dict[str, str] = {
    "first": FIRST_EMOJI,
    "prev": LEFT_EMOJI,
    "next": RIGHT_EMOJI,
    "last": LAST_EMOJI,
    "stop": DELETE_EMOJI,
}

log = get_logger(__name__)


class EmptyPaginatorEmbedError(Exception):
    """Base Exception class for an empty paginator embed."""


class LinePaginator(commands.Paginator):
    """A class that aids in paginating code blocks for Discord messages."""

    def __init__(
        self,
        prefix: str = "```",
        suffix: str = "```",
        max_size: int = 2000,
        max_lines: Optional[int] = None,
        linesep: str = "\n",
    ) -> None:
        """
        Overrides the Paginator.__init__ from inside disnake.ext.commands.

        `prefix` and `suffix` will be prepended and appended respectively to every page.

        `max_size` and `max_lines` denote the maximum amount of codepoints and lines
        allowed per page.
        """
        super().__init__(prefix, suffix, max_size - len(suffix), linesep)

        self.max_lines = max_lines
        self._current_page = [prefix]
        self._linecount = 0
        self._count = len(prefix) + 1  # prefix + newline
        self._pages = []
        self.prefix: str = prefix or ""

    def add_line(self, line: str = "", *, empty: bool = False) -> None:
        """
        Adds a line to the current page.

        If the line exceeds the `max_size` then a RuntimeError is raised.

        Overrides the Paginator.add_line from inside disnake.ext.commands in order to allow
        configuration of the maximum number of lines per page.

        If `empty` is True, an empty line will be placed after the a given `line`.
        """
        if len(line) > self.max_size - len(self.prefix) - 2:
            raise RuntimeError("Line exceeds maximum page size %s" % (self.max_size - len(self.prefix) - 2))

        if self.max_lines is not None:
            if self._linecount >= self.max_lines:
                self._linecount = 0
                self.close_page()

            self._linecount += 1
        if self._count + len(line) + 1 > self.max_size:
            self.close_page()

        self._count += len(line) + 1
        self._current_page.append(line)

        if empty:
            self._current_page.append("")
            self._count += 1

    @staticmethod
    def strip_custom_id(custom_id: str) -> Optional[str]:
        """Remove paginator custom id prefix."""
        if not custom_id.startswith(CUSTOM_ID_PREFIX):
            return None

        return custom_id[len(CUSTOM_ID_PREFIX) :]

    @classmethod
    async def paginate(
        cls,
        lines: Iterable[str],
        ctx: commands.Context,
        embed: disnake.Embed,
        prefix: str = "",
        suffix: str = "",
        max_lines: Optional[int] = None,
        max_size: int = 500,
        empty: bool = True,
        linesep: str = "\n",
        restrict_to_user: Union[disnake.User, disnake.Member] = None,
        timeout: int = 300,
        footer_text: str = None,
        url: str = None,
        exception_on_empty_embed: bool = False,
    ) -> None:
        """
        Use a paginator and set of reactions to provide pagination over a set of lines.

        The reactions are used to switch page, or to finish with pagination.

        When used, this will send a message using `ctx.send()` and apply a set of reactions to it.
        These reactions may be used to change page, or to remove pagination from the message.

        Pagination will also be removed automatically if no reaction is added for `timeout` seconds,
        defaulting to five minutes (300 seconds).

        If `empty` is True, an empty line will be placed between each given line.

        >>> embed = disnake.Embed()
        >>> embed.set_author(name="Some Operation", url=url, icon_url=icon)
        >>> await LinePaginator.paginate(
        ...     (line for line in lines),
        ...     ctx, embed
        ... )
        """
        restrict_to_user = restrict_to_user or ctx.author

        def event_check(inter: disnake.MessageInteraction) -> bool:
            """Make sure that this reaction is what we want to operate on."""
            user_valid = (
                # Pagination is restricted
                restrict_to_user
                # The reaction was by a whitelisted user
                and inter.author.id == restrict_to_user.id
            )
            # check the custom_id is valid
            name = cls.strip_custom_id(inter.data.custom_id)
            check = all(
                # Conditions for a successful pagination:
                (
                    # name is not None
                    name is not None,
                    # disnake.Reaction is one of the pagination emotes
                    name in PAGINATION_EMOJI,  # Note: DELETE_EMOJI is a string and not unicode
                    # User is allowed
                    user_valid,
                )
            )
            if not check and inter.message.id == message.id:
                scheduling.create_task(
                    inter.response.send_message("Hey! This isn't yours to interact with!", ephemeral=True)
                )
            return check and inter.message.id == message.id

        paginator = cls(prefix=prefix, suffix=suffix, max_size=max_size, max_lines=max_lines, linesep=linesep)
        current_page = 0

        if not lines:
            if exception_on_empty_embed:
                log.exception("Pagination asked for empty lines iterable")
                raise EmptyPaginatorEmbedError("No lines to paginate")

            log.debug("No lines to add to paginator, adding '(nothing to display)' message")
            lines = ("(nothing to display)",)

        for line in lines:
            try:
                paginator.add_line(line, empty=empty)
            except Exception:
                log.exception(f"Failed to add line to paginator: '{line}'")
                raise  # Should propagate
            else:
                log.trace(f"Added line to paginator: '{line}'")

        log.debug(f"Paginator created with {len(paginator.pages)} pages")

        embed.description = paginator.pages[current_page]

        if len(paginator.pages) <= 1:
            if footer_text:
                embed.set_footer(text=footer_text)
                log.trace(f"Setting embed footer to '{footer_text}'")

            if url:
                embed.url = url
                log.trace(f"Setting embed url to '{url}'")

            log.debug("There's less than two pages, so we won't paginate - sending single page on its own")
            await ctx.send(embed=embed)
            return

        if footer_text:
            embed.set_footer(text=f"{footer_text} (Page {current_page + 1}/{len(paginator.pages)})")
        else:
            embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")
        log.trace(f"Setting embed footer to '{embed.footer.text}'")

        if url:
            embed.url = url
            log.trace(f"Setting embed url to '{url}'")

        log.debug("Creating view for message...")
        view = disnake.ui.View()
        for id, emoji in PAGINATION_EMOJI.items():
            # Add all the applicable emoji to the message
            log.trace(f"Adding reaction: {repr(emoji)}")
            view.add_item(
                disnake.ui.Button(
                    style=disnake.ButtonStyle.gray,
                    custom_id=CUSTOM_ID_PREFIX + id,
                    emoji=emoji,
                )
            )

        log.debug("Sending first page to channel...")
        message = await ctx.send(embed=embed, view=view)

        while True:
            try:
                inter: disnake.MessageInteraction = await ctx.bot.wait_for(
                    "message_interaction", timeout=timeout, check=event_check
                )
                log.trace(f"Got interaction: {inter}")
            except asyncio.TimeoutError:
                log.debug("Timed out waiting for a reaction")
                break  # We're done, no reactions for the last 5 minutes

            custom_id = cast(str, inter.component.custom_id)
            event_name = custom_id[len(CUSTOM_ID_PREFIX) :]

            if PAGINATION_EMOJI.get(event_name) == DELETE_EMOJI:  # Note: DELETE_EMOJI is a string and not unicode
                log.debug("Got delete inter")
                return await message.delete()

            elif PAGINATION_EMOJI.get(event_name) == FIRST_EMOJI:
                current_page = 0

                log.debug(f"Got first page reaction - changing to page 1/{len(paginator.pages)}")

                embed.description = paginator.pages[current_page]
                if footer_text:
                    embed.set_footer(text=f"{footer_text} (Page {current_page + 1}/{len(paginator.pages)})")
                else:
                    embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")
                await inter.response.edit_message(embed=embed)

            elif PAGINATION_EMOJI.get(event_name) == LAST_EMOJI:
                current_page = len(paginator.pages) - 1

                log.debug(f"Got last page reaction - changing to page {current_page + 1}/{len(paginator.pages)}")

                embed.description = paginator.pages[current_page]
                if footer_text:
                    embed.set_footer(text=f"{footer_text} (Page {current_page + 1}/{len(paginator.pages)})")
                else:
                    embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")
                await inter.response.edit_message(embed=embed)

            elif PAGINATION_EMOJI.get(event_name) == LEFT_EMOJI:
                if current_page <= 0:
                    log.debug("Got previous page reaction, but we're on the first page - ignoring")
                    await inter.response.defer()
                    continue

                current_page -= 1
                log.debug(f"Got previous page reaction - changing to page {current_page + 1}/{len(paginator.pages)}")

                embed.description = paginator.pages[current_page]

                if footer_text:
                    embed.set_footer(text=f"{footer_text} (Page {current_page + 1}/{len(paginator.pages)})")
                else:
                    embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")

                await inter.response.edit_message(embed=embed)

            elif PAGINATION_EMOJI.get(event_name) == RIGHT_EMOJI:
                if current_page >= len(paginator.pages) - 1:
                    log.debug("Got next page reaction, but we're on the last page - ignoring")
                    await inter.response.defer()
                    continue

                current_page += 1
                log.debug(f"Got next page reaction - changing to page {current_page + 1}/{len(paginator.pages)}")

                embed.description = paginator.pages[current_page]

                if footer_text:
                    embed.set_footer(text=f"{footer_text} (Page {current_page + 1}/{len(paginator.pages)})")
                else:
                    embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")

                await inter.response.edit_message(embed=embed)

        log.debug("Ending pagination and clearing reactions...")
        await message.edit(view=None)


class ImagePaginator(commands.Paginator):
    """
    Helper class that paginates images for embeds in messages.

    Close resemblance to LinePaginator, except focuses on images over text.

    Refer to ImagePaginator.paginate for documentation on how to use.
    """

    def __init__(self, prefix: str = "", suffix: str = "") -> None:
        super().__init__(prefix, suffix)
        self._current_page = [prefix]
        self.images = []
        self._pages = []

    def add_line(self, line: str = "", *, empty: bool = False) -> None:
        """
        Adds a line to each page, usually just 1 line in this context.

        If `empty` is True, an empty line will be placed after a given `line`.
        """
        if line:
            self._count = len(line)
        else:
            self._count = 0
        self._current_page.append(line)
        self.close_page()

    def add_image(self, image: str = None) -> None:
        """Adds an image to a page given the url."""
        self.images.append(image)

    @classmethod
    async def paginate(
        cls,
        pages: List[Tuple[str, str]],
        ctx: commands.Context,
        embed: disnake.Embed,
        prefix: str = "",
        suffix: str = "",
        timeout: int = 300,
        exception_on_empty_embed: bool = False,
    ) -> None:
        """
        Use a paginator and set of reactions to provide pagination over a set of title/image pairs.

        `pages` is a list of tuples of page title/image url pairs.
        `prefix` and `suffix` will be prepended and appended respectively to the message.

        When used, this will send a message using `ctx.send()` and apply a set of reactions to it.
        These reactions may be used to change page, or to remove pagination from the message.

        Note: Pagination will be removed automatically if no reaction is added for `timeout` seconds,
              defaulting to five minutes (300 seconds).

        >>> embed = disnake.Embed()
        >>> embed.set_author(name="Some Operation", url=url, icon_url=icon)
        >>> await ImagePaginator.paginate(pages, ctx, embed)
        """

        def check_event(reaction_: disnake.Reaction, member: disnake.Member) -> bool:
            """Checks each reaction added, if it matches our conditions pass the wait_for."""
            return all(
                (
                    # disnake.Reaction is on the same message sent
                    reaction_.message.id == message.id,
                    # The reaction is part of the navigation menu
                    str(reaction_.emoji) in PAGINATION_EMOJI,  # Note: DELETE_EMOJI is a string and not unicode
                    # The reactor is not a bot
                    not member.bot,
                )
            )

        paginator = cls(prefix=prefix, suffix=suffix)
        current_page = 0

        if not pages:
            if exception_on_empty_embed:
                log.exception("Pagination asked for empty image list")
                raise EmptyPaginatorEmbedError("No images to paginate")

            log.debug("No images to add to paginator, adding '(no images to display)' message")
            pages.append(("(no images to display)", ""))

        for text, image_url in pages:
            paginator.add_line(text)
            paginator.add_image(image_url)

        embed.description = paginator.pages[current_page]
        image = paginator.images[current_page]

        if image:
            embed.set_image(url=image)

        if len(paginator.pages) <= 1:
            await ctx.send(embed=embed)
            return

        embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")
        message = await ctx.send(embed=embed)

        for emoji in PAGINATION_EMOJI:
            await message.add_reaction(emoji)

        while True:
            # Start waiting for reactions
            try:
                reaction, user = await ctx.bot.wait_for("reaction_add", timeout=timeout, check=check_event)
            except asyncio.TimeoutError:
                log.debug("Timed out waiting for a reaction")
                break  # We're done, no reactions for the last 5 minutes

            # Deletes the users reaction
            await message.remove_reaction(reaction.emoji, user)

            reaction_type = "unknown"

            # Delete reaction press - [:trashcan:]
            if str(reaction.emoji) == DELETE_EMOJI:  # Note: DELETE_EMOJI is a string and not unicode
                log.debug("Got delete reaction")
                return await message.delete()

            # First reaction press - [:track_previous:]
            if reaction.emoji == FIRST_EMOJI:
                if current_page == 0:
                    log.debug("Got first page reaction, but we're on the first page - ignoring")
                    continue

                current_page = 0
                reaction_type = "first"

            # Last reaction press - [:track_next:]
            if reaction.emoji == LAST_EMOJI:
                if current_page >= len(paginator.pages) - 1:
                    log.debug("Got last page reaction, but we're on the last page - ignoring")
                    continue

                current_page = len(paginator.pages) - 1
                reaction_type = "last"

            # Previous reaction press - [:arrow_left: ]
            if reaction.emoji == LEFT_EMOJI:
                if current_page <= 0:
                    log.debug("Got previous page reaction, but we're on the first page - ignoring")
                    continue

                current_page -= 1
                reaction_type = "previous"

            # Next reaction press - [:arrow_right:]
            if reaction.emoji == RIGHT_EMOJI:
                if current_page >= len(paginator.pages) - 1:
                    log.debug("Got next page reaction, but we're on the last page - ignoring")
                    continue

                current_page += 1
                reaction_type = "next"

            # Magic happens here, after page and reaction_type is set
            embed.description = paginator.pages[current_page]

            image = paginator.images[current_page] or None
            embed.set_image(url=image)

            embed.set_footer(text=f"Page {current_page + 1}/{len(paginator.pages)}")
            log.debug(f"Got {reaction_type} page reaction - changing to page {current_page + 1}/{len(paginator.pages)}")

            await message.edit(embed=embed)

        log.debug("Ending pagination and clearing reactions...")
        await message.clear_reactions()
