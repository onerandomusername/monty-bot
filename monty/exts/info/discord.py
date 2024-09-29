from __future__ import annotations

from typing import TYPE_CHECKING

import disnake
from disnake.ext import commands
from disnake.ext.commands import LargeInt

from monty.bot import Monty
from monty.constants import Endpoints
from monty.utils import helpers
from monty.utils.messages import DeleteButton


if TYPE_CHECKING:
    from disnake.types.appinfo import AppInfo as AppInfoPayload


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


class Discord(commands.Cog, slash_command_attrs={"dm_permission": False}):
    """Useful discord api commands."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    async def fetch_app_info_for_client(self, client_id: int) -> disnake.AppInfo:
        """Given a client ID, fetch the user."""
        async with self.bot.http_session.get(Endpoints.app_info.format(application_id=client_id)) as resp:
            if resp.status != 200:
                content = "Could not get application info."
                content += "\nThis may be a result of the application not existing, or not being a valid user."
                e = commands.UserInputError(content)
                raise e
            data: AppInfoPayload = await resp.json()

        # add some missing attributes that we don't use but the library needs
        data.setdefault("rpc_origins", [])
        # we are not using the user, we just need it to serialize, so we're loading with fake data of itself
        data["owner"] = self.bot.user._to_minimal_user_json()

        appinfo = disnake.AppInfo(self.bot._connection, data)
        return appinfo

    @commands.slash_command()
    async def discord(self, inter: disnake.CommandInteraction) -> None:
        """Commands that interact with discord."""
        pass

    @discord.sub_command_group()
    async def api(self, inter: disnake.CommandInteraction) -> None:
        """Commands that interact with the discord api."""
        pass

    @api.sub_command(name="app-info")
    async def info_app(self, inter: disnake.CommandInteraction, client_id: LargeInt, ephemeral: bool = True) -> None:
        """
        [DEV] Get information on an app from its ID. May not work with all apps.

        Parameters
        ----------
        client_id: The ID of the app.
        ephemeral: Whether to send the app info as an ephemeral message.
        """
        appinfo = await self.fetch_app_info_for_client(client_id)

        embed = disnake.Embed(
            title=f"Application info for {appinfo.name}",
        )
        if appinfo.icon:
            embed.set_thumbnail(url=appinfo.icon.url)

        embed.description = f"ID: `{appinfo.id}`\nPublic: `{appinfo.bot_public}`\n"

        if appinfo.description:
            embed.add_field("About me:", appinfo.description, inline=False)

        if appinfo.tags:
            embed.add_field(name="Tags", value=", ".join(sorted(appinfo.tags)), inline=False)

        if appinfo.terms_of_service_url or appinfo.privacy_policy_url:
            embed.add_field(
                "Legal:",
                f"ToS: {appinfo.terms_of_service_url}\nPrivacy policy:{appinfo.privacy_policy_url}",
                inline=False,
            )

        flags = ""
        if appinfo.flags:
            for flag, value in sorted(appinfo.flags, key=lambda x: x[0]):
                flags += f"{flag}:`{value}`\n"
            embed.add_field(name="Flags", value=flags, inline=False)

        if not ephemeral:
            components = DeleteButton(inter.author)
        else:
            components = []

        await inter.response.send_message(embed=embed, ephemeral=ephemeral, components=components)

    @discord.sub_command(name="app-invite")
    async def app_invite(
        self,
        inter: disnake.AppCmdInter,
        client_id: LargeInt,
        # permissions: Range[int, 0, disnake.Permissions.all().value] = None,
        guild_id: LargeInt = None,
        raw_link: bool = False,
        ephemeral: bool = True,
    ) -> None:
        """
        [BETA] Generate an invite to add a bot to a guild. NOTE: may not work on all bots.

        Parameters
        ----------
        client_id: ID of the user to invite
        permissions: Value of permissions to pre-fill with
        guild_id: ID of the guild to pre-fill the invite.
        include_applications_commands: Whether or not to include the applications.commands scope.
        raw_link: Instead of a fancy button, I'll give you the raw link.
        ephemeral: Whether or not to send an ephemeral response.
        """
        try:
            app_info = await self.fetch_app_info_for_client(client_id)
        except commands.UserInputError as e:
            await inter.response.send_message(str(e), ephemeral=True)
            return

        urls = helpers.get_invite_link_from_app_info(app_info, guild_id=guild_id)

        message = " ".join(
            [
                "Click below to invite" if not raw_link else "Click the following link to invite",
                "me" if client_id == inter.bot.user.id else app_info.name,
                "to the specified guild!" if guild_id else "to your guild!",
            ]
        )

        components = []

        labels = {
            disnake.ApplicationIntegrationType.user: "User install",
            disnake.ApplicationIntegrationType.guild: "Guild invite",
        }

        if not ephemeral:
            components.append(DeleteButton(inter.author))

        if isinstance(urls, dict):
            if raw_link:
                message += "\n"
            for install_context, url in urls.items():
                title = labels[install_context]
                if raw_link:
                    message += title + ": <" + url + ">\n"
                else:
                    components.append(disnake.ui.Button(url=url, style=disnake.ButtonStyle.link, label=title))

        else:
            if raw_link:
                message += f"\n{urls}"
            else:
                components.append(
                    disnake.ui.Button(
                        url=urls, style=disnake.ButtonStyle.link, label=f"Click to invite {app_info.name}!"
                    )
                )

        await inter.response.send_message(
            message,
            allowed_mentions=disnake.AllowedMentions.none(),
            components=components,
            ephemeral=ephemeral,
        )

    @api.sub_command(name="guild-invite")
    async def guild_invite(
        self,
        inter: disnake.CommandInteraction,
        invite: disnake.Invite,
        ephemeral: bool = True,
        with_features: bool = False,
    ) -> None:
        """
        Get information on a guild from an invite.

        Parameters
        ----------
        invite: The invite to get information on.
        ephemeral: Whether or not to send an ephemeral response.
        with_features: Whether or not to include the features of the guild.
        """
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

        if with_features:
            embed.add_field(name="Features", value="\n".join(sorted(invite.guild.features)), inline=False)

        if invite.guild.icon is not None:
            embed.set_thumbnail(url=invite.guild.icon.url)

        if not ephemeral:
            components = DeleteButton(inter.author)
        else:
            components = []

        await inter.response.send_message(embed=embed, ephemeral=ephemeral, components=components)


def setup(bot: Monty) -> None:
    """Load the Discord cog."""
    bot.add_cog(Discord(bot))
