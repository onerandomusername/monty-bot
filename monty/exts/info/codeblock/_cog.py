import asyncio
import time
from typing import Optional, Union

import disnake
from disnake.ext import commands

from monty import constants
from monty.bot import Monty
from monty.exts.filters.token_remover import TokenRemover
from monty.exts.filters.webhook_remover import WEBHOOK_URL_RE
from monty.exts.info.codeblock._instructions import get_instructions
from monty.exts.info.codeblock._parsing import is_python_code
from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.helpers import has_lines
from monty.utils.messages import DeleteButton


log = get_logger(__name__)

GuildMessageable = Union[disnake.TextChannel, disnake.Thread, disnake.VoiceChannel]


# seconds until the delete button is shown
DELETE_PAUSE = 7


class CodeBlockCog(
    commands.Cog,
    name="Code Block",
    slash_command_attrs={"dm_permission": False},
):
    """
    Detect improperly formatted Markdown code blocks and suggest proper formatting.

    There are four basic ways in which a code block is considered improperly formatted:

    1. The code is not within a code block at all
        * Ignored if the code is not valid Python or Python REPL code
    2. Incorrect characters are used for backticks
    3. A language for syntax highlighting is not specified
        * Ignored if the code is not valid Python or Python REPL code
    4. A syntax highlighting language is incorrectly specified
        * Ignored if the language specified doesn't look like it was meant for Python
        * This can go wrong in two ways:
            1. Spaces before the language
            2. No newline immediately following the language

    Messages or code blocks must meet a minimum line count to be detected. Detecting multiple code
    blocks is supported. However, if at least one code block is correct, then instructions will not
    be sent even if others are incorrect. When multiple incorrect code blocks are found, only the
    first one is used as the basis for the instructions sent.

    When an issue is detected, an embed is sent containing specific instructions on fixing what
    is wrong. If the user edits their message to fix the code block, the instructions will be
    removed. If they fail to fix the code block with an edit, the instructions will be updated to
    show what is still incorrect after the user's edit. The embed can be manually deleted with a
    reaction. Otherwise, it will automatically be removed after 5 minutes.

    The cog only detects messages in whitelisted channels. Channels may also have a cooldown on the
    instructions being sent. Note all help channels are also whitelisted with cooldowns enabled.

    For configurable parameters, see the `code_block` section in config-default.py.
    """

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

        # Stores allowed channels plus epoch times since the last instructional messages sent.
        self.channel_cooldowns = dict.fromkeys(constants.CodeBlock.cooldown_channels, 0.0)

        # Maps users' messages to the messages the bot sent with instructions.
        self.codeblock_message_ids = {}

    @staticmethod
    def is_python_code(content: str) -> bool:
        """Determine if the content is valid python code."""
        return is_python_code(content)

    @staticmethod
    def create_embed(instructions: str) -> disnake.Embed:
        """Return an embed which displays code block formatting `instructions`."""
        return disnake.Embed(description=instructions)

    async def get_sent_instructions(self, payload: disnake.RawMessageUpdateEvent) -> Optional[disnake.Message]:
        """
        Return the bot's sent instructions message associated with a user's message `payload`.

        Return None if the message cannot be found. In this case, it's likely the message was
        deleted either manually via a reaction or automatically by a timer.
        """
        log.trace(f"Retrieving instructions message for ID {payload.message_id}")
        channel: disnake.abc.MessageableChannel
        channel = self.bot.get_channel(payload.channel_id)  # type: ignore # this channel is obviously a messageable

        try:
            return await channel.fetch_message(self.codeblock_message_ids[payload.message_id])
        except disnake.NotFound:
            log.debug("Could not find instructions message; it was probably deleted.")
            return None

    def is_on_cooldown(self, channel: Union[GuildMessageable, disnake.DMChannel]) -> bool:
        """
        Return True if an embed was sent too recently for `channel`.

        The cooldown is configured by `constants.CodeBlock.cooldown_seconds`.
        Note: only channels in the `channel_cooldowns` have cooldowns enabled.
        """
        log.trace(f"Checking if #{channel} is on cooldown.")
        cooldown = constants.CodeBlock.cooldown_seconds
        return (time.time() - self.channel_cooldowns.get(channel.id, 0)) < cooldown

    async def is_valid_channel(self, channel: Union[GuildMessageable, disnake.DMChannel]) -> bool:
        """Return True if `channel` is a help channel, may be on a cooldown, or is whitelisted."""
        log.trace(f"Checking if #{channel} qualifies for code block detection.")
        if isinstance(channel, disnake.DMChannel):
            return False
        res = channel.guild and await self.bot.guild_has_feature(
            channel.guild, constants.Feature.CODEBLOCK_RECOMMENDATIONS
        )
        return res

    async def send_instructions(self, message: disnake.Message, instructions: str) -> None:
        """
        Send an embed with `instructions` on fixing an incorrect code block in a `message`.

        The embed will be deleted automatically after 5 minutes.
        """
        log.info(f"Sending code block formatting instructions for message {message.id}.")

        embed = self.create_embed(instructions)
        bot_message = await message.channel.send(f"Hey {message.author.mention}!", embed=embed)
        self.codeblock_message_ids[message.id] = bot_message.id

        async def add_task() -> None:
            await asyncio.sleep(DELETE_PAUSE)
            components = DeleteButton(message.author)
            await bot_message.edit(components=components)

        scheduling.create_task(add_task(), event_loop=self.bot.loop)

    async def should_parse(self, message: disnake.Message) -> bool:
        """
        Return True if `message` should be parsed.

        A qualifying message:

        1. Is not authored by a bot
        2. Is in a valid channel
        3. Has more than the configured count lines
        4. Has no bot or webhook token
        """
        return (
            not message.author.bot
            and await self.is_valid_channel(message.channel)
            and has_lines(message.content, constants.CodeBlock.minimum_lines)
            and not TokenRemover.find_token_in_message(message)
            and not WEBHOOK_URL_RE.search(message.content)
        )

    @commands.Cog.listener()
    async def on_message(self, msg: disnake.Message) -> None:
        """Detect incorrect Markdown code blocks in `msg` and send instructions to fix them."""
        # check for perms first
        if not msg.guild:
            return

        if not msg.channel.permissions_for(msg.guild.me).send_messages or (
            isinstance(msg.channel, disnake.Thread)
            and not msg.channel.permissions_for(msg.guild.me).send_messages_in_threads
        ):
            return

        if not await self.should_parse(msg):
            log.trace(f"Skipping code block detection of {msg.id}: message doesn't qualify.")
            return

        # When debugging, ignore cooldowns.
        if self.is_on_cooldown(msg.channel) and not constants.DEBUG_MODE:
            log.trace(f"Skipping code block detection of {msg.id}: #{msg.channel} is on cooldown.")
            return

        instructions = get_instructions(msg.content)
        if instructions:
            await self.send_instructions(msg, instructions)

            if msg.channel.id not in constants.CodeBlock.channel_whitelist:
                log.debug(f"Adding #{msg.channel} to the channel cooldowns.")
                self.channel_cooldowns[msg.channel.id] = time.time()

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: disnake.RawMessageUpdateEvent) -> None:
        """Delete the instructional message if an edited message had its code blocks fixed."""
        if payload.message_id not in self.codeblock_message_ids:
            log.trace(f"Ignoring message edit {payload.message_id}: message isn't being tracked.")
            return

        content: Optional[str]
        if (content := payload.data.get("content")) is None or payload.data.get("channel_id") is None:
            log.trace(f"Ignoring message edit {payload.message_id}: missing content or channel ID.")
            return

        # Parse the message to see if the code blocks have been fixed.
        instructions = get_instructions(content)

        bot_message = await self.get_sent_instructions(payload)
        if not bot_message:
            return

        try:
            if not instructions:
                log.info("User's incorrect code block was fixed. Removing instructions message.")
                await bot_message.delete()
                del self.codeblock_message_ids[payload.message_id]
            else:
                log.info("Message edited but still has invalid code blocks; editing instructions.")
                await bot_message.edit(embed=self.create_embed(instructions))
        except disnake.NotFound:
            log.debug("Could not find instructions message; it was probably deleted.")
