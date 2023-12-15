import disnake
from disnake.ext import commands

from monty.bot import Monty
from monty.errors import BotAccountRequired
from monty.log import get_logger


logger = get_logger(__name__)


class GlobalCheck(commands.Cog, slash_command_attrs={"dm_permission": False}):
    """Global checks for monty."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        self._bot_invite_link: str = ""

    @commands.Cog.listener("on_ready")
    async def set_invite_link(self) -> None:
        """Set the invite link when the bot is ready."""
        if self._bot_invite_link:
            return

        # todo: don't require a fake guild object
        class FakeGuild:
            id: str = "{guild_id}"

        guild = FakeGuild
        self._bot_invite_link = disnake.utils.oauth_url(
            self.bot.user.id,
            disable_guild_select=True,
            guild=guild,  # type: ignore # this is totally wrong
            scopes={"applications.commands", "bot"},
            permissions=self.bot.invite_permissions,
        )

    def bot_slash_command_check(self, inter: disnake.CommandInteraction) -> bool:
        """
        Require all commands in guilds have the bot scope.

        This essentially prevents commands from running when the Bot is not in a guild.

        However, this does allow slash commands in DMs as those are now controlled via
        the dm_permisions attribute on each app command.
        """
        if inter.guild or not inter.guild_id:
            return True

        invite = self._bot_invite_link.format(guild_id=inter.guild_id)
        if inter.permissions.manage_guild:
            msg = (
                "The bot scope is required to perform any actions. "
                f"You can invite the full bot by [clicking here](<{invite}>)."
            )
        else:
            msg = (
                "The bot scope is required to perform any actions. "
                f"Please ask a server manager to [invite the full bot](<{invite}>)."
            )
        raise BotAccountRequired(msg)

    bot_user_command_check = bot_slash_command_check
    bot_message_command_check = bot_slash_command_check

    async def bot_check_once(self, ctx: commands.Context) -> bool:
        """Require all commands be in guild."""
        if ctx.guild:
            return True
        raise commands.NoPrivateMessage()


def setup(bot: Monty) -> None:
    """Add the global checks to the bot."""
    bot.add_cog(GlobalCheck(bot))
