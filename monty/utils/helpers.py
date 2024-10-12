from __future__ import annotations

import asyncio
import datetime
import ssl
from typing import TYPE_CHECKING, Any, Coroutine, Optional, TypeVar, Union
from urllib.parse import urlsplit, urlunsplit

import base65536
import dateutil.parser
import disnake

from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.messages import extract_urls


if TYPE_CHECKING:
    from typing_extensions import ParamSpec

    P = ParamSpec("P")
    T = TypeVar("T")
    Coro = Coroutine[Any, Any, T]
UNSET = object()

logger = get_logger(__name__)


def suppress_links(message: str) -> str:
    """Accepts a message that may contain links, suppresses them, and returns them."""
    for link in extract_urls(message):
        message = message.replace(link, f"<{link}>")
    return message


def find_nth_occurrence(string: str, substring: str, n: int) -> Optional[int]:
    """Return index of `n`th occurrence of `substring` in `string`, or None if not found."""
    index = 0
    for _ in range(n):
        index = string.find(substring, index + 1)
        if index == -1:
            return None
    return index


def get_num_suffix(num: int) -> str:
    """Get the suffix for the provided number. Currently a lazy implementation so this only supports 1-20."""
    if num == 1:
        suffix = "st"
    elif num == 2:
        suffix = "nd"
    elif num == 3:
        suffix = "rd"
    elif 4 <= num < 20:
        suffix = "th"
    else:
        err = "num must be within 1-20. If you receive this error you should refactor the get_num_suffix method."
        raise RuntimeError(err)
    return suffix


def has_lines(string: str, count: int) -> bool:
    """Return True if `string` has at least `count` lines."""
    # Benchmarks show this is significantly faster than using str.count("\n") or a for loop & break.
    split = string.split("\n", count - 1)

    # Make sure the last part isn't empty, which would happen if there was a final newline.
    return bool(split[-1]) and len(split) == count


def pad_base64(data: str) -> str:
    """Return base64 `data` with padding characters to ensure its length is a multiple of 4."""
    return data + "=" * (-len(data) % 4)


EXPAND_BUTTON_PREFIX = "ghexp-v1:"


def encode_github_link(link: str) -> str:
    """Encode a github link with base 65536."""
    scheme, netloc, path, query, fragment = urlsplit(link)
    user, repo, literal_blob, blob, file_path = path.lstrip("/").split("/", 4)
    data = f"{user}/{repo}/{blob}/{file_path}#{fragment}"

    encoded = base65536.encode(data.encode())
    end_result = EXPAND_BUTTON_PREFIX + encoded
    assert link == decode_github_link(end_result), f"{link} != {decode_github_link(end_result)}"
    return end_result


def decode_github_link(compressed: str) -> str:
    """Decode a GitHub link that was encoded with `encode_github_link`."""
    compressed = compressed.removeprefix(EXPAND_BUTTON_PREFIX)
    # compressed = compressed.encode()
    data = base65536.decode(compressed).decode()

    if "#" in data:
        path, fragment = data.rsplit("#", 1)
    else:
        path, fragment = data, ""
    user, repo, blob, file_path = path.split("/", 3)
    path = f"{user}/{repo}/blob/{blob}/{file_path}"
    return urlunsplit(("https", "github.com", path, "", fragment))


def maybe_defer(inter: disnake.Interaction, *, delay: Union[float, int] = 2.0, **options) -> asyncio.Task:
    """Defer an interaction if it has not been responded to after ``delay`` seconds."""
    loop = inter.bot.loop
    if delay <= 0:
        return scheduling.create_task(inter.response.defer(**options))

    async def internal_task() -> None:
        now = loop.time()
        await asyncio.sleep(delay - (start - now))

        if inter.response.is_done():
            return
        try:
            await inter.response.defer(**options)
        except disnake.HTTPException as e:
            if e.code == 40060:  # interaction has already been acked
                logger.warning("interaction was already responded to (race condition)")
                return
            raise e

    start = loop.time()
    return scheduling.create_task(internal_task())


def utcnow() -> datetime.datetime:
    """Return the current time as an aware datetime in UTC."""
    return datetime.datetime.now(datetime.timezone.utc)


def fromisoformat(timestamp: str) -> datetime.datetime:
    """Parse the given ISO-8601 timestamp to an aware datetime object, assuming UTC if timestamp contains no timezone."""  # noqa: E501
    dt = dateutil.parser.isoparse(timestamp)
    if not dt.tzinfo:
        # assume UTC if naive datetime
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def ssl_create_default_context() -> ssl.SSLContext:
    """Return an ssl context that CloudFlare shouldn't flag."""
    ssl_context = ssl.create_default_context()
    ssl_context.post_handshake_auth = True
    return ssl_context


def get_invite_link_from_app_info(
    app_info: disnake.AppInfo,
    *,
    guild_id: int = None,
    default_permissions: disnake.Permissions = None,
) -> str | dict[int, str]:
    """Get an invite link from the provided disnake.AppInfo object."""
    urls: str | dict[int, str] = ""
    if disnake.ApplicationIntegrationTypes(guild=True).values[0] in app_info.integration_types_config:
        if disnake.ApplicationIntegrationTypes(user=True).values[0] in app_info.integration_types_config:
            # we can provide a link that works for both, actually don't have to provide anything else

            if any(g.install_params for g in app_info.integration_types_config.values()):
                urls = f"https://discord.com/oauth2/authorize?client_id={app_info.id}"
            else:
                urls = {
                    disnake.ApplicationIntegrationTypes(user=True).values[0]: disnake.utils.oauth_url(
                        client_id=app_info.id,
                        scopes=("applications.commands",),
                        integration_type=disnake.ApplicationIntegrationTypes(user=True).values[0],
                    ),
                    disnake.ApplicationIntegrationTypes(guild=True).values[0]: disnake.utils.oauth_url(
                        client_id=app_info.id,
                        scopes=("applications.commands", "bot"),
                        integration_type=disnake.ApplicationIntegrationTypes(guild=True).values[0],
                    ),
                }
        else:
            # guild installs only
            for g in app_info.integration_types_config.values():
                if g.install_params and (permissions := g.install_params.permissions):
                    break
            else:
                permissions = default_permissions or disnake.utils.MISSING

            urls = disnake.utils.oauth_url(
                scopes=("applications.commands", "bot"),
                guild=guild_id and disnake.Object(guild_id) or disnake.utils.MISSING,
                permissions=permissions,
                integration_type=disnake.ApplicationIntegrationTypes(guild=True).values[0],
                client_id=app_info.id,
            )
    else:
        if disnake.ApplicationIntegrationTypes(user=True).values[0] in app_info.integration_types_config:
            # user installs only
            urls = disnake.utils.oauth_url(
                client_id=app_info.id,
                scopes=("applications.commands",),
                integration_type=disnake.ApplicationIntegrationTypes(user=True).values[0],
            )
        else:
            # some other form of install that we don't know of....
            if app_info.install_params:
                urls = f"https://discord.com/oauth2/authorize?client_id={app_info.id}"
            else:
                # honestly, who's to guesstimate.
                # play it safe and provide a guild invite
                urls = f"https://discord.com/oauth2/authorize?client_id={app_info.id}"

    return urls
