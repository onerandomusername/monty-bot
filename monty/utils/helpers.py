from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Coroutine, Optional, TypeVar, Union
from urllib.parse import urlsplit, urlunsplit

import base65536
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
