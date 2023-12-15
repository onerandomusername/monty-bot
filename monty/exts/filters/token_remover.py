# TODO: add caching to the api requests that error

import base64
import contextlib
import re
import sys
import typing as t

import aiohttp
import attr
import disnake
from disnake.ext import commands

from monty import constants, utils
from monty.bot import Monty
from monty.log import get_logger


log = get_logger(__name__)

GITHUB_API_GISTS = "https://api.github.com/gists"
GITHUB_REQUEST_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if constants.Tokens.github:
    GITHUB_REQUEST_HEADERS["Authorization"] = f"token {constants.Tokens.github}"

DISCORD_API_VALIDATION = "https://discord.com/api/v10/oauth2/applications/@me"
DISCORD_REQUEST_HEADERS = {}

LOG_MESSAGE = "Censored a seemingly valid token sent by {author} in {channel}, token was `{user_id}.{timestamp}.{hmac}`"
UNKNOWN_USER_LOG_MESSAGE = "Decoded user ID: `{user_id}` (Not present in server)."
KNOWN_USER_LOG_MESSAGE = (
    "Decoded user ID: `{user_id}` **(Present in server)**.\n"
    "This matches `{user_name}` and means this is likely a valid **{kind}** token."
)
DELETION_MESSAGE_TEMPLATE = (
    "Hey {mention}! I noticed you posted a seemingly valid Discord API "
    "token in your message. "
    "This means that your token has been **compromised**. "
    "Please change your token **immediately** at: "
    "<https://discord.com/developers/applications/{client_id}>\n"
    # "Feel free to re-post it with the token removed. "
    # "If you believe this was a mistake, please let us know!"
)
DISCORD_EPOCH = 1_420_070_400
TOKEN_EPOCH = 1_293_840_000

# Three parts delimited by dots: user ID, creation timestamp, HMAC.
# The HMAC isn't parsed further, but it's in the regex to ensure it at least exists in the string.
# Each part only matches base64 URL-safe characters.
# These regexes were taken from discord-developers, which are used by the client itself.
TOKEN_RE = re.compile(r"([a-z0-9_-]{23,28})\.([a-z0-9_-]{6,7})\.([a-z0-9_-]{27,})", re.IGNORECASE)

# This checks for MFA tokens, and its not nearly as complex. if we match this, we don't check everything
# because its not possible.
MFA_TOKEN_RE = re.compile(r"(mfa\.[a-z0-9_-]{20,})", re.IGNORECASE)


@attr.s(kw_only=False, auto_attribs=True)
class Token:
    """A Discord Bot token."""

    user_id: str
    timestamp: str
    hmac: str

    def __attrs_post_init__(self, *args, **kwargs) -> None:
        self.application_id: t.Optional[int] = TokenRemover.extract_user_id(self.user_id)

    def __str__(self) -> str:
        return f"{self.user_id}.{self.timestamp}.{self.hmac}"


class TokenRemover(commands.Cog, name="Token Remover", slash_command_attrs={"dm_permission": False}):
    """Scans messages for potential discord client tokens and removes them."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        user_agent = "DiscordBot (https://github.com/DisnakeDev/disnake {0}) Python/{1[0]}.{1[1]} aiohttp/{2}"
        DISCORD_REQUEST_HEADERS["User-Agent"] = user_agent.format(
            disnake.__version__, sys.version_info, aiohttp.__version__
        )

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

    @commands.Cog.listener()
    async def on_message(self, msg: disnake.Message) -> None:
        """
        Check each message for a string that matches Discord's token pattern.

        See: https://discord.com/developers/docs/reference#snowflakes
        """
        # Ignore DMs; can't delete messages in there anyway.
        if not msg.guild:
            return

        if not await self.bot.guild_has_feature(msg.guild, constants.Feature.DISCORD_TOKEN_REMOVER):
            return

        found_tokens = self.find_token_in_message(msg)
        if found_tokens:
            # now check if the token is valid
            ids = await self.check_valid(*found_tokens)
            for token, application_id in zip(found_tokens, ids):
                if application_id:
                    token.application_id = application_id
                else:
                    found_tokens.remove(token)
        if found_tokens:
            await self.take_action(msg, found_tokens)

        # check for mfa tokens
        await self.handle_mfa_token(msg)

    @commands.Cog.listener()
    async def on_message_edit(self, before: disnake.Message, after: disnake.Message) -> None:
        """
        Check each edit for a string that matches Discord's token pattern.

        See: https://discord.com/developers/docs/reference#snowflakes
        """
        if before.content == after.content:
            return

        if self.find_token_in_message(before):
            # already alerted the user, no need to alert again
            return

        await self.on_message(after)

    async def check_valid(self, *tokens: Token) -> list[t.Optional[int]]:
        """Check if the provided tokens were valid or not."""
        statuses: list[t.Optional[int]] = []
        headers = DISCORD_REQUEST_HEADERS.copy()
        for token in tokens:
            headers["Authorization"] = "Bot " + str(token)
            async with self.bot.http_session.get(DISCORD_API_VALIDATION, headers=headers) as resp:
                if 200 <= resp.status < 300:
                    body = await resp.json()
                    statuses.append(int(body["id"]))
                else:
                    statuses.append(None)
        return statuses

    async def invalidate_tokens(self, *tokens: Token) -> t.Optional[str]:
        """Post the provided tokens to github to invalidate it."""
        if not tokens:
            return None
        content = "\n".join(str(token) for token in tokens)
        body = {
            "files": {"token.txt": {"content": content}},
            "public": True,
        }
        async with self.bot.http_session.post(GITHUB_API_GISTS, headers=GITHUB_REQUEST_HEADERS, json=body) as resp:
            if resp.status != 201:
                body = await resp.json()
                log.error(f"Received unexpected response from {GITHUB_API_GISTS}: {body}")
                return None
            body = await resp.json()
        return body["html_url"]

    async def take_action(self, msg: disnake.Message, found_tokens: list[Token]) -> None:
        """Remove the `msg` containing the `found_token` and send a mod log message."""
        url = await self.invalidate_tokens(*found_tokens)

        try:
            await self.maybe_delete(msg)
        except disnake.NotFound:
            log.debug(f"Failed to remove token in message {msg.id}: message already deleted.")

        text = DELETION_MESSAGE_TEMPLATE.format(mention=msg.author.mention, client_id=found_tokens[0].application_id)
        if url:
            text += f"Your token was sent to <{url}> to be invalidated."
        await msg.channel.send(text)

        for token in found_tokens:
            log_message = self.format_log_message(msg, token)
            log.debug(log_message)

    async def handle_mfa_token(self, msg: disnake.Message) -> None:
        """
        Check all messages for a string that matches the mfa token pattern.

        Due to how the mfa tokens work, there is no user information supplied for this token.
        """
        # Ignore DMs; can't delete messages in there anyway.
        if not msg.guild or msg.author.bot:
            return

        was_valid = False
        match = None
        for match in MFA_TOKEN_RE.finditer(msg.content):
            if self.is_maybe_valid_hmac(match.group()):
                was_valid = True
                break
        if not was_valid or not match:
            return

        token = match.group()
        # since the token was probably valid, we can now delete the message and ping the moderators.
        # the user is not informed, given the reasoning for deleting the message.
        with contextlib.suppress(disnake.NotFound):
            await self.maybe_delete(msg)

        log_message = (
            f"Deleted mfa token sent by {msg.author} in {msg.channel}: {token[:4]}{'x' * len(token[3:-3])}{token[-3:]}"
        )

        log.info(log_message)

    @staticmethod
    def format_log_message(msg: disnake.Message, token: Token) -> str:
        """Return the generic portion of the log message to send for `token` being censored in `msg`."""
        return LOG_MESSAGE.format(
            author=f"{msg.author} ({msg.author.id})",
            channel=msg.channel.id,
            user_id=token.user_id,
            timestamp=token.timestamp,
            hmac="x" * (len(token.hmac) - 3) + token.hmac[-3:],
        )

    @classmethod
    def find_token_in_message(cls, msg: disnake.Message) -> t.Optional[list[Token]]:
        """Return a seemingly valid token found in `msg` or `None` if no token is found."""
        tokens = []
        for match in TOKEN_RE.finditer(msg.content):
            token = Token(*match.groups())
            if (
                (cls.extract_user_id(token.user_id) is not None)
                and cls.is_valid_timestamp(token.timestamp)
                and cls.is_maybe_valid_hmac(token.hmac)
            ):
                tokens.append(token)
                # shortcircuit after we find two tokens as any more would be someone abusing us.
                if len(tokens) > 2:
                    break

        return tokens or None

    @staticmethod
    def extract_user_id(b64_content: str) -> t.Optional[int]:
        """Return a user ID integer from part of a potential token, or None if it couldn't be decoded."""
        b64_content = utils.pad_base64(b64_content)

        try:
            decoded_bytes = base64.urlsafe_b64decode(b64_content)
            string = decoded_bytes.decode("utf-8")
            if not (string.isascii() and string.isdigit()):
                # This case triggers if there are fancy unicode digits in the base64 encoding,
                # that means it's not a valid user id.
                return None
            return int(string)
        except ValueError:
            return None

    @staticmethod
    def is_valid_timestamp(b64_content: str) -> bool:
        """
        Return True if `b64_content` decodes to a valid timestamp.

        If the timestamp is greater than the Discord epoch, it's probably valid.
        See: https://i.imgur.com/7WdehGn.png
        """
        b64_content = utils.pad_base64(b64_content)

        try:
            decoded_bytes = base64.urlsafe_b64decode(b64_content)
            timestamp = int.from_bytes(decoded_bytes, byteorder="big")
        except ValueError as e:
            log.debug(f"Failed to decode token timestamp '{b64_content}': {e}")
            return False

        # Seems like newer tokens don't need the epoch added, but add anyway since an upper bound
        # is not checked.
        if timestamp + TOKEN_EPOCH >= DISCORD_EPOCH:
            return True
        else:
            log.debug(f"Invalid token timestamp '{b64_content}': smaller than Discord epoch")
            return False

    @staticmethod
    def is_maybe_valid_hmac(b64_content: str) -> bool:
        """
        Determine if a given HMAC portion of a token is potentially valid.

        If the HMAC has 3 or less characters, it's probably a dummy value like "xxxxxxxxxx",
        and thus the token can probably be skipped.
        """
        unique = len(set(b64_content.lower()))
        if unique <= 3:
            log.debug(
                f"Considering the HMAC {b64_content} a dummy because it has {unique}"
                " case-insensitively unique characters"
            )
            return False
        else:
            return True


def setup(bot: Monty) -> None:
    """Load the TokenRemover cog."""
    bot.add_cog(TokenRemover(bot))
