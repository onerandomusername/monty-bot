from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Union
from urllib.parse import urljoin

import aiohttp
import disnake
from cachingutils import LRUMemoryCache, async_cached
from disnake.ext import commands

from monty.bot import Monty
from monty.constants import Feature, Icons
from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.helpers import get_num_suffix
from monty.utils.messages import DeleteButton, extract_urls, suppress_embeds


DOMAIN = "https://discuss.python.org"
TOPIC_REGEX = re.compile(r"https?:\/\/discuss\.python\.org\/t\/(?:[^\s\/]*\/)*?(?P<num>\d+)(?:\/(?P<reply>\d+))?[^\s]*")
# https://docs.discourse.org/#tag/Posts
TOPIC_API_URL = f"{DOMAIN}/t/{{id}}.json"
# https://docs.discourse.org/#tag/Topics
POST_API_URL = f"{DOMAIN}/posts/{{id}}.json"


logger = get_logger(__name__)


@dataclass
class DiscussionTopic:
    id: int
    url: str
    reply_id: Optional[int] = None

    def __init__(self, id: Union[int, str], url: str, reply: Optional[Union[str, int]] = None) -> None:
        self.id = int(id)
        self.url = url
        self.reply_id = int(reply) if reply is not None else None

    def __hash__(self) -> int:
        return hash((self.id, self.reply_id))


@dataclass
class TopicInfo:
    title: str
    url: str


class PythonDiscourse(commands.Cog):
    """Autolink discuss.python.org discussions."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot

    @async_cached(cache=LRUMemoryCache(50, timeout=int(timedelta(minutes=10).total_seconds())))
    async def fetch_data(self, url: str) -> dict[str, Any]:
        """Fetch the url. Results are cached."""
        async with self.bot.http_session.get(url, raise_for_status=True) as r:
            return await r.json()

    async def fetch_post(self, topic: DiscussionTopic) -> tuple[dict[str, Any], TopicInfo]:
        """Fetch a python discourse post knowing the topic and reply id."""
        if topic.reply_id is not None:
            index = topic.reply_id
            url = TOPIC_API_URL.format(id=f"{topic.id}/{index}")
            data = await self.fetch_data(url)  # type: ignore
        else:
            url = TOPIC_API_URL.format(id=topic.id)
            data = await self.fetch_data(url)  # type: ignore
            index = 1
        posts = data["post_stream"]["posts"]
        post_id = next(filter(lambda p: p["post_number"] == index, posts))["id"]

        topic_info = TopicInfo(title=data["title"], url=f"{DOMAIN}/t/{data['slug']}/{data['id']}")

        data = await self.fetch_data(POST_API_URL.format(id=post_id))  # type: ignore
        return data, topic_info

    def make_post_embed(self, data: dict[str, Any], topic_info: TopicInfo = None) -> disnake.Embed:
        """Return an embed representing the provided post and topic information."""
        # consider parsing this into markdown
        limit = 2700
        body: str = data["raw"]

        if len(body) > limit:
            body = body[: limit - 3] + "..."
        e = disnake.Embed(description=body)

        is_reply = data["post_number"] > 1
        if topic_info and topic_info.title:
            if is_reply:
                e.title = "comment on " + topic_info.title
            else:
                e.title = topic_info.title

        url = f"{DOMAIN}/t/{data['topic_slug']}/{data['topic_id']}"
        if is_reply:
            url += f"/{data['post_number']}"
        e.url = url

        author_url = urljoin(DOMAIN, data["avatar_template"].format(size="256"))
        e.set_author(
            name=data["name"],
            icon_url=author_url,
            url=f"{DOMAIN}/u/{data['username']}",
        )

        e.timestamp = datetime.strptime(data["created_at"], r"%Y-%m-%dT%H:%M:%S.%fZ")
        e.set_footer(text="Posted at", icon_url=Icons.python_discourse)
        return e

    def extract_topic_urls(self, content: str) -> list[DiscussionTopic]:
        """Extract python discourse urls from the provided content."""
        posts = []
        for match in filter(None, map(TOPIC_REGEX.fullmatch, extract_urls(content))):
            posts.append(
                DiscussionTopic(
                    id=match.group("num"),
                    url=match[0],
                    reply=match.group("reply"),
                )
            )
        return posts

    @commands.Cog.listener("on_message")
    async def on_message(self, message: disnake.Message) -> None:
        """Automatically link python discourse urls."""
        if message.author.bot:
            return

        if not message.guild:
            return

        if not message.content:
            return

        if not await self.bot.guild_has_feature(message.guild, Feature.PYTHON_DISCOURSE_AUTOLINK):
            return

        posts = self.extract_topic_urls(message.content)

        if not posts:
            return

        posts = list(dict.fromkeys(posts, None))
        my_perms = message.channel.permissions_for(message.guild.me)

        if len(posts) > 4:
            if my_perms.add_reactions:
                await message.add_reaction(":x:")
            await message.reply(
                "I can only link 4 discussion urls at a time!.",
                components=DeleteButton(message.author),
                allowed_mentions=disnake.AllowedMentions(replied_user=False),
                fail_if_not_exists=True,
            )
            return

        embeds = []
        components: list[disnake.ui.Button] = []
        chars = 0
        for post in posts:
            try:
                data = await self.fetch_post(post)
            except aiohttp.ClientResponseError:
                continue

            embed = self.make_post_embed(*data)
            chars += len(embed)
            if chars > 6000:
                break

            embeds.append(embed)
            components.append(disnake.ui.Button(url=embed.url, label="View comment"))

        if len(components) > 1:
            for num, component in enumerate(components, 1):
                suffix = get_num_suffix(num)
                component.label = f"View {num}{suffix} comment"

        components.insert(0, DeleteButton(message.author))

        if embeds:
            if my_perms.manage_messages:
                scheduling.create_task(suppress_embeds(self.bot, message))
            await message.reply(embeds=embeds, components=components)


def setup(bot: Monty) -> None:
    """Add the Python Discourse cog to the bot."""
    bot.add_cog(PythonDiscourse(bot))
