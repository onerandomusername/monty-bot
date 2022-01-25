from __future__ import annotations

from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands
from disnake.ext.commands import LargeInt

from monty.bot import Monty
from monty.constants import Endpoints


if TYPE_CHECKING:
    from disnake.types.appinfo import AppInfo


INVITE = """
**Created at**: {invite.created_at}
**Expires at**: {invite.expires_at}
**Max uses**: {invite.max_uses}
"""
INVITE_GUILD_INFO = """
**Name**: {guild.name}
**ID**: {guild.id}
**Approx. Member Count**: {invite.approximate_member_count}
**Approx. Online Members**: {invite.approximate_presence_count}
**Description**: {guild.description}
"""
INVITE_USER = """
**Usertag**: {inviter}
**ID**: {inviter.id}
"""


class Discord(commands.Cog):
    """Useful discord api commands."""

    def __init__(self, bot: Monty):
        self.bot = bot

    @commands.slash_command()
    async def discord(self, inter: disnake.CommandInteraction) -> None:
        """Commands that interact with discord."""
        pass

    @discord.sub_command_group()
    async def api(self, inter: disnake.CommandInteraction) -> None:
        """Commands that interact with the discord api."""
        pass

    @api.sub_command(name="bot_info")
    async def info_bot(self, inter: disnake.CommandInteraction, client_id: LargeInt, ephemeral: bool = True) -> None:
        """[DEV] Get information on an bot from its ID. May not work with all bots."""
        # attempt to do a precursory check on the client_id
        try:
            user = self.bot.get_user(client_id) or await self.bot.fetch_user(client_id)
        except disnake.NotFound:
            await inter.response.send_message("User not found.", ephemeral=True)
            return

        if not user.bot:
            await inter.send("You can only run this command on bots.", ephemeral=True)
            return

        async with self.bot.http_session.get(Endpoints.app_info.format(application_id=client_id)) as resp:
            if resp.status != 200:
                await inter.send("Could not get bot info.", ephemeral=True)
                return
            data: AppInfo = await resp.json()

        # add some missing attributes that we don't use but the library needs
        data.setdefault("rpc_origins", [])
        data["owner"] = user._to_minimal_user_json()

        appinfo = disnake.AppInfo(self.bot._connection, data)

        embed = disnake.Embed(
            title=f"Bot info for {user.name}",
        )
        if appinfo.icon:
            embed.set_thumbnail(url=appinfo.icon.url)

        embed.description = f"ID: {appinfo.id}\nPublic: {appinfo.bot_public    }\n"

        if appinfo.description:
            embed.add_field("About me:", appinfo.description, inline=False)

        flags = ""
        for flag, value in sorted(appinfo.flags, key=lambda x: x[0]):
            flags += f"{flag}:`{value}`\n"
        embed.add_field(name="Flags", value=flags, inline=False)

        await inter.send(embed=embed, ephemeral=ephemeral)

    @info_bot.error
    async def bot_info_error(self, inter: disnake.CommandInteraction, error: Exception) -> None:
        """Handle errors in the bot_info command."""
        if isinstance(error, commands.ConversionError):
            if isinstance(error.original, ValueError):
                await inter.send("Client ID must be an integer.", ephemeral=True)
                error.handled = True

    @api.sub_command()
    async def guild_invite(
        self, inter: disnake.CommandInteraction, invite: disnake.Invite, ephemeral: bool = True
    ) -> None:
        """Get information on a guild from an invite."""
        if not invite.guild:
            await inter.send("Group dm invites are not supported.", ephemeral=True)
            return
        if invite.guild.nsfw_level not in (disnake.NSFWLevel.default, disnake.NSFWLevel.safe):
            await inter.send(f"Refusing to process invite for the nsfw guild, {invite.guild.name}.", ephemeral=True)
            return

        embed = disnake.Embed(title=f"Invite for {invite.guild.name}")
        if invite.created_at or invite.expires_at or invite.max_uses:
            embed.description = INVITE.format(invite=invite, guild=invite.guild)

        embed.add_field(name="Guild Info", value=INVITE_GUILD_INFO.format(invite=invite, guild=invite.guild))
        if invite.inviter:
            embed.add_field("Inviter Info:", INVITE_USER.format(inviter=invite.inviter), inline=False)

        embed.set_author(name=invite.guild.name)
        if image := (invite.guild.banner or invite.guild.splash):
            image = image.with_size(1024)
            embed.set_image(url=image.url)

        if invite.guild.icon is not None:
            embed.set_thumbnail(url=invite.guild.icon.url)

        await inter.send(embed=embed, ephemeral=ephemeral)

    @guild_invite.error
    async def guild_invite_error(self, inter: disnake.CommandInteraction, error: Exception) -> None:
        """Handle errors for guild_invite."""
        if isinstance(error, commands.ConversionError):
            error = error.original
        if isinstance(error, commands.BadInviteArgument):
            await inter.send(str(error), ephemeral=True)
            return


def setup(bot: Monty) -> None:
    """Load the Discord cog."""
    bot.add_cog(Discord(bot))
