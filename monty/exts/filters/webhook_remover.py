import re

import disnake
from disnake.ext import commands

from monty.bot import Monty
from monty.constants import Feature
from monty.log import get_logger


WEBHOOK_URL_RE = re.compile(
    r"((?:https?:\/\/)?(?:ptb\.|canary\.)?discord(?:app)?\.com\/api\/webhooks\/\d+\/)\S+\/?", re.IGNORECASE
)

ALERT_MESSAGE_TEMPLATE = (
    "{user}, looks like you posted a Discord webhook URL. Therefore "
    "your webhook has been deleted. "
    "You can re-create it if you wish to. If you believe this was a "
    "mistake, please let us know."
)


log = get_logger(__name__)


class WebhookRemover(commands.Cog, name="Webhook Remover", slash_command_attrs={"dm_permission": False}):
    """Scan messages to detect Discord webhooks links."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    async def maybe_delete(self, msg: disnake.Message) -> bool:
        """
        Maybe delete a message, if we have perms.

        Returns True on success.
        """
        if not msg.guild:
            return False
        can_delete = msg.author == msg.guild.me or msg.channel.permissions_for(msg.guild.me).manage_messages
        if not can_delete:
            return False

        await msg.delete()
        return True

    async def delete_and_respond(self, msg: disnake.Message, redacted_url: str, *, webhook_deleted: bool) -> None:
        """Delete `msg` and send a warning that it contained the Discord webhook `redacted_url`."""
        if webhook_deleted:
            await msg.channel.send(ALERT_MESSAGE_TEMPLATE.format(user=msg.author.mention))
            delete_state = "The webhook was successfully deleted."
        else:
            delete_state = "There was an error when deleting the webhook, it might have already been removed."
        message = (
            f"{msg.author} ({msg.author.id!s}) posted a Discord webhook URL to {msg.channel.id}. {delete_state} "
            f"Webhook URL was `{redacted_url}`"
        )
        log.debug(message)

    @commands.Cog.listener()
    async def on_message(self, msg: disnake.Message) -> None:
        """Check if a Discord webhook URL is in `message`."""
        # Ignore DMs; can't delete messages in there anyway.
        if not msg.guild or msg.author.bot:
            return
        if not await self.bot.guild_has_feature(msg.guild, Feature.DISCORD_WEBHOOK_REMOVER):
            return

        matches = WEBHOOK_URL_RE.search(msg.content)
        if matches:
            async with self.bot.http_session.delete(matches[0]) as resp:
                # The Discord API Returns a 204 NO CONTENT response on success.
                deleted_successfully = resp.status == 204
            await self.delete_and_respond(msg, matches[1] + "xxx", webhook_deleted=deleted_successfully)

    @commands.Cog.listener()
    async def on_message_edit(self, before: disnake.Message, after: disnake.Message) -> None:
        """Check if a Discord webhook URL is in the edited message `after`."""
        if before.content == after.content:
            return

        await self.on_message(after)


def setup(bot: Monty) -> None:
    """Load `WebhookRemover` cog."""
    bot.add_cog(WebhookRemover(bot))
