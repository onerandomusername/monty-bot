import asyncio
import functools
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Union

import arrow
import disnake
import sqlalchemy as sa
from disnake.ext import commands, tasks

from monty.bot import Monty
from monty.database import Feature, Rollout
from monty.log import get_logger
from monty.utils import rollouts, scheduling
from monty.utils.messages import DeleteButton


if TYPE_CHECKING:
    ArrowConverter = arrow.Arrow
    FeatureConverter = Feature
    RolloutConverter = Rollout
    Percent = float
else:
    from monty.utils.converters import ArrowConverter, FeatureConverter, RolloutConverter

    class Percent(float):
        """A converter for percentages."""

        @classmethod
        async def convert(cls, ctx: commands.Context, argument: str) -> float:
            """Convert a percent to a float."""
            return float(argument.removesuffix("%"))


logger = get_logger(__name__)


class RolloutCog(commands.Cog, name="Rollouts"):
    """Management commands for bot rollouts."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        self.update_rollout_counts.start()

    def cog_unload(self) -> None:
        """Cancel the rollout updater task."""
        self.update_rollout_counts.cancel()

    @tasks.loop(minutes=15)
    async def update_rollout_counts(self) -> None:
        """Update rollout levels every 15 minutes if a rollout is being updated."""
        logger.debug("Starting rollout levels update task.")
        now = datetime.now(tz=timezone.utc)
        async with self.bot.db.begin() as session:
            stmt = sa.select(Rollout).where(Rollout.rollout_by == None)  # noqa: E711
            result = await session.scalars(stmt)
            rollouts_to_update = result.all()
            if not rollouts_to_update:
                logger.debug("No rollouts to update.")
                return
            for rollout in rollouts_to_update:
                if rollout.rollout_by and rollout.rollout_by < now:
                    continue
                rollout.rollout_hash_low, rollout.rollout_hash_high = rollouts.update_counts_to_time(rollout, now)
                rollout.hashes_last_updated = now
            await session.commit()

        await self.bot.refresh_features()

    async def wait_for_confirmation(
        self,
        message: disnake.Message,
        content: str,
        *,
        timeout: float = 30,
        confirm_button_text: str = "Confirm",
        deny_button_text: str = "Deny",
        message_to_edit: Optional[disnake.Message] = None,
    ) -> Union[tuple[bool, disnake.MessageInteraction, disnake.ui.MessageActionRow], tuple[None, None, None]]:
        """Wait for the user to provide confirmation, and handle expiration."""
        # ask the user if they want to add this feature
        components = disnake.ui.ActionRow.with_message_components()
        create_button = disnake.ui.Button(style=disnake.ButtonStyle.green, label=confirm_button_text)
        components.append_item(create_button)
        deny_button = disnake.ui.Button(style=disnake.ButtonStyle.red, label=deny_button_text)
        components.append_item(deny_button)
        custom_ids = {x.custom_id for x in components}

        delete_button = DeleteButton(message.author, allow_manage_messages=False, initial_message=message)
        components.insert_item(0, delete_button)
        delete_button.disabled = True

        method = message_to_edit.edit if message_to_edit else functools.partial(message.reply, fail_if_not_exists=False)
        sent_msg = await method(
            content,
            components=components,
        )
        try:
            inter: disnake.MessageInteraction = await self.bot.wait_for(
                "button_click",
                check=lambda itr: itr.component.custom_id in custom_ids and itr.message == sent_msg,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for comp in components:
                comp.disabled = True
            delete_button.disabled = False
            try:
                await sent_msg.edit(content="Timed out.", components=components)
            except disnake.HTTPException:
                pass
            return (None, None, None)

        for comp in components:
            comp.disabled = True
        delete_button.disabled = False

        if inter.component.custom_id != create_button.custom_id:
            # don't create the feature, abort it.
            return (False, inter, components)

        return (True, inter, components)

    # rollout commands

    @commands.group(name="rollouts", aliases=("rollout",), invoke_without_command=True)
    async def cmd_rollouts(self, ctx: commands.Context) -> None:
        """Manage feature rollouts."""
        await self.cmd_rollouts_list(ctx)

    @cmd_rollouts.command(name="list")
    async def cmd_rollouts_list(self, ctx: commands.Context) -> None:
        """List all rollouts and their current status."""
        async with self.bot.db.begin() as session:
            stmt = sa.select(Rollout.name)
            result = await session.scalars(stmt)
            all_rollouts = result.all()

        names = sorted(all_rollouts)

        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        if not names:
            await ctx.send("No rollouts found.", components=button)
            return

        embed = disnake.Embed(title="Rollouts")
        embed.description = "\n".join(names)
        await ctx.send(embed=embed, components=button)

    # todo: make this easier to use (selects, modals, buttons)
    # while the interface is clumsy right now, this is currently in development
    @cmd_rollouts.command("create")
    async def cmd_rollouts_create(
        self,
        ctx: commands.Context,
        name: str,
        percent_goal: Percent,
    ) -> None:
        """Create a rollout."""
        # check for an existing rollout with the same name
        async with self.bot.db.begin() as session:
            stmt = sa.select(Rollout).where(Rollout.name == name)
            result = await session.scalars(stmt)
            if result.one_or_none():
                raise commands.BadArgument("A rollout with that name already exists.")

            if percent_goal not in range(0, 100 + 1):
                raise commands.BadArgument("percent_goal must be within 0 to 100 inclusive.")

            # pick a random starting number divisible by 100
            # this means the rollout is effectively not enabled, as both limits are the same value
            hash_low = hash_high = random.choice(range(0, 10_000, 100))
            rollout = Rollout(
                name=name,
                rollout_hash_low=hash_low,
                rollout_hash_high=hash_high,
                rollout_to_percent=percent_goal,
            )
            session.add(rollout)
            await session.commit()

        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.reply(
            f"Successfully created rollout **`{name}`**. "
            f"Use **`{ctx.prefix}{' '.join(ctx.invoked_parents)} start {name} <end_time>`** to start the rollout. \n"
            "NOTE: The rollout needs to be linked to a target to have an effect. This rollout is currently unlinked.",
            components=button,
        )

    @cmd_rollouts.command("modify")
    async def cmd_rollouts_configure(
        self,
        ctx: commands.Context,
        rollout: RolloutConverter,
        new_percent: Percent,
    ) -> None:
        """Configure an existing rollout."""
        current_percent = rollouts.compute_current_percent(rollout) * 100
        if new_percent < current_percent:
            raise commands.BadArgument(
                f"The new rollout percentage cannot be less than the current rollout percent of `{new_percent:6.3f}%`."
            )
        if new_percent == current_percent:
            raise commands.CommandError(f"The rollout percentage is already at `{new_percent:6.3f}%`.")

        # calculate the new values
        low, high = rollouts.find_new_hash_levels(rollout, new_percent)

        async with self.bot.db.begin() as session:
            rollout = await session.merge(rollout)
            rollout.rollout_hash_low = low
            rollout.rollout_hash_high = high
            await session.commit()
        assert rollouts.compute_current_percent(rollout) * 100 == new_percent
        await ctx.send(f"Succesfully changed the current rollout percent to `{new_percent:6.3f}%`.")
        scheduling.create_task(self.bot.refresh_features())

    @cmd_rollouts.command("delete")
    async def cmd_rollouts_delete(self, ctx: commands.Context, rollout: RolloutConverter) -> None:
        """Delete an existing rollout. There is no going back."""
        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        texts = [
            f"Are you sure you want to delete rollout `{rollout.name}`?\n\u200b",
            (
                "Absolutely certain? There is no going back. "
                f"Are you completely sure you want to delete rollout `{rollout.name}`?"
            ),
        ]

        message: Optional[disnake.Message] = None
        for msg in texts:
            confirm, inter, components = await self.wait_for_confirmation(ctx.message, msg, message_to_edit=message)
            if confirm is None or inter is None:
                return
            if not confirm:
                await inter.response.edit_message("Aborted.", components=components)
                return
            await asyncio.sleep(1)
            await inter.response.edit_message(content="Processing...", components=button)
            await asyncio.sleep(1)
            message = inter.message

        async with self.bot.db.begin() as session:
            await session.delete(rollout)
            await session.commit()

        if message:
            try:
                await message.edit(content=f"Rollout `{rollout.name}` successfully deleted.", components=button)
            except disnake.NotFound:
                pass
            else:
                return

        await ctx.send(content=f"Rollout `{rollout.name}` successfully deleted.", components=button)
        scheduling.create_task(self.bot.refresh_features())

    @cmd_rollouts.command("start")
    async def cmd_rollouts_start(self, ctx: commands.Context, rollout: RolloutConverter, dt: ArrowConverter) -> None:
        """Start a rollout now to end at the specified time."""
        now = disnake.utils.utcnow()
        if now > dt:
            raise commands.BadArgument("A rollout must end in the future.")

        if rollout.rollout_by is not None:
            raise commands.CommandError("That rollout already has a time set.")

        async with self.bot.db.begin() as session:
            rollout = await session.merge(rollout)
            rollout.hashes_last_updated = now
            rollout.rollout_by = dt.datetime
            await session.commit()
        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(f"Started rolling out `{rollout.name}`", components=button)

    @cmd_rollouts.command("stop", aliases=("halt",))
    async def cmd_rollouts_stop(self, ctx: commands.Context, rollout: RolloutConverter) -> None:
        """Stop a rollout. This does not decrease the rollout amount, just stops increasing the rollout."""
        async with self.bot.db.begin() as session:
            rollout = await session.merge(rollout)
            rollout.rollout_by = None
            await session.commit()

        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send("Stopped the rollout.", components=button)

    @cmd_rollouts.command("view", aliases=("show",))
    async def cmd_rollouts_view(self, ctx: commands.Context, rollout: RolloutConverter) -> None:
        """Show information about a rollout."""
        embed = disnake.Embed(title="Rollout Information")
        embed.set_author(name=rollout.name)
        embed.add_field("Current Percent", f"{rollouts.compute_current_percent(rollout) * 100:6.2f}%", inline=True)
        embed.add_field("Goal Percent", f"{rollout.rollout_to_percent:6.2f}%", inline=True)

        if rollout.rollout_by:
            rollout_by = disnake.utils.format_dt(rollout.rollout_by, "F")
        else:
            rollout_by = None
        embed.add_field("Rollout scheduled by", rollout_by, inline=False)

        if rollout.hashes_last_updated:
            last_updated = disnake.utils.format_dt(rollout.hashes_last_updated, "F")
        else:
            last_updated = None
        embed.add_field("Rollout counts last updated", last_updated, inline=False)

        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(embed=embed, components=button)

    @cmd_rollouts.group("link", aliases=("unlink",), invoke_without_command=True)
    async def cmd_rollouts_link(self, ctx: commands.Context) -> None:
        """Manage rollout links to features and other components."""
        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send("Subcommand required.", components=button)

    @cmd_rollouts_link.command("feature")
    async def cmd_rollouts_link_feature(
        self,
        ctx: commands.Context,
        rollout: RolloutConverter,
        feature: FeatureConverter,
    ) -> None:
        """Link or unlink a feature from the specified rollout."""
        add_or_remove = ctx.invoked_parents[-1] == "link"
        msg = None
        if add_or_remove:
            if feature.rollout:
                raise commands.BadArgument(f"This feature is already linked to a rollout: `{feature.rollout.name}`.")
            async with self.bot.db.begin() as session:
                feature.rollout_id = rollout.id
                feature = await session.merge(feature)
                await session.commit()
                self.bot.features[feature.name] = feature
            msg = f"Feature `{feature.name}` successfully linked to rollout `{rollout.name}`."

        else:
            if not feature.rollout:
                raise commands.BadArgument("This feature is not linked to any rollout.")
            elif feature.rollout.id != rollout.id:
                raise commands.BadArgument("This feature is linked to a different rollout.")
            # this is a workaround to https://github.com/collerek/ormar/issues/720
            async with self.bot.db.begin() as session:
                stmt = sa.update(Feature).where(Feature.name == feature.name).values(rollout_id=None).returning(Feature)
                result = await session.scalars(stmt)
                feature = result.one()
                await session.commit()
            msg = f"Feature `{feature.name}` successfully unlinked from rollout `{rollout.name}`."

        button = DeleteButton(ctx.author, allow_manage_messages=False, initial_message=ctx.message)
        await ctx.send(msg, components=button)
        scheduling.create_task(self.bot.refresh_features())

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Require all commands in this cog are by the bot author and are in guilds."""
        if await self.bot.is_owner(ctx.author):
            if not ctx.guild:
                raise commands.NoPrivateMessage()
            return True

        raise commands.NotOwner("You do not own this bot.")


def setup(bot: Monty) -> None:
    """Add the RolloutCog cog to the bot."""
    bot.add_cog(RolloutCog(bot))
