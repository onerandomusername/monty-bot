"""
Helper methods for responses from the bot to the user.

These help ensure consistency between errors, as they will all be consistent between different uses.

Note: these are to used for general success or general errors. Typically, the error handler will make a
response if a command raises a disnake.ext.commands.CommandError exception.
"""

import random
from typing import Any, List, Literal

import disnake
from disnake.ext import commands

from monty.log import get_logger


__all__ = (
    "DEFAULT_SUCCESS_COLOUR",
    "SUCCESS_HEADERS",
    "DEFAULT_FAILURE_COLOUR",
    "FAILURE_HEADERS",
    "send_general_response",
    "send_positive_response",
    "send_negatory_response",
)

_UNSET: Any = object()

logger = get_logger(__name__)


DEFAULT_SUCCESS_COLOUR = disnake.Colour.green()
SUCCESS_HEADERS: List[str] = [
    "Affirmative",
    "As you wish",
    "Done",
    "Fine by me",
    "There we go",
    "Sure!",
    "Okay",
    "You got it",
    "Your wish is my command",
]

DEFAULT_FAILURE_COLOUR = disnake.Colour.red()
FAILURE_HEADERS: List[str] = [
    "Abort!",
    "I cannot do that",
    "Hold up!",
    "I was unable to interpret that",
    "Not understood",
    "Oops",
    "Something went wrong",
    "\U0001f914",
    "Unable to complete your command",
]


async def send_general_response(
    channel: disnake.abc.Messageable,
    response: str,
    *,
    message: disnake.Message = None,
    embed: disnake.Embed = _UNSET,
    colour: disnake.Colour = None,
    title: str = None,
    tag_as: Literal["general", "affirmative", "negatory"] = "general",
    **kwargs,
) -> disnake.Message:
    """
    Helper method to send a response.

    Shortcuts are provided as `send_positive_response` and `send_negatory_response` which
    fill in the title and colour automatically.
    """
    kwargs["allowed_mentions"] = kwargs.get("allowed_mentions", disnake.AllowedMentions.none())

    if isinstance(channel, commands.Context):  # pragma: nocover
        channel = channel.channel

    logger.debug(f"Requested to send {tag_as} response message to {channel!s}. Response: {response!s}")

    if embed is None:
        if message is None:
            return await channel.send(response, **kwargs)
        else:
            return await message.edit(response, **kwargs)

    if embed is _UNSET:  # pragma: no branch
        embed = disnake.Embed(colour=colour)

    if title is not None:
        embed.title = title

    embed.description = response

    if message is None:
        return await channel.send(embed=embed, **kwargs)
    else:
        return await message.edit(embed=embed, **kwargs)


async def send_positive_response(
    channel: disnake.abc.Messageable,
    response: str,
    *,
    colour: disnake.Colour = _UNSET,
    **kwargs,
) -> disnake.Message:
    """
    Send an affirmative response.

    Requires a messageable, and a response.
    If embed is set to None, this will send response as a plaintext message, with no allowed_mentions.
    If embed is provided, this method will send a response using the provided embed, edited in place.
    Extra kwargs are passed to Messageable.send()

    If message is provided, it will attempt to edit that message rather than sending a new one.
    """
    if colour is _UNSET:  # pragma: no branch
        colour = DEFAULT_SUCCESS_COLOUR

    kwargs["title"] = kwargs.get("title", random.choice(SUCCESS_HEADERS))

    return await send_general_response(
        channel=channel,
        response=response,
        colour=colour,
        tag_as="affirmative",
        **kwargs,
    )


async def send_negatory_response(
    channel: disnake.abc.Messageable,
    response: str,
    *,
    colour: disnake.Colour = _UNSET,
    **kwargs,
) -> disnake.Message:
    """
    Send a negatory response.

    Requires a messageable, and a response.
    If embed is set to None, this will send response as a plaintext message, with no allowed_mentions.
    If embed is provided, this method will send a response using the provided embed, edited in place.
    Extra kwargs are passed to Messageable.send()
    """
    if colour is _UNSET:  # pragma: no branch
        colour = DEFAULT_FAILURE_COLOUR

    kwargs["title"] = kwargs.get("title", random.choice(FAILURE_HEADERS))

    return await send_general_response(
        channel=channel,
        response=response,
        colour=colour,
        tag_as="negatory",
        **kwargs,
    )
