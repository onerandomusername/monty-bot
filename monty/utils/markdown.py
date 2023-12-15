import re
from typing import Any, Optional
from urllib.parse import urljoin

import mistune.renderers
from bs4.element import PageElement, Tag
from markdownify import MarkdownConverter

from monty import constants


__all__ = (
    "remove_codeblocks",
    "DocMarkdownConverter",
    "DiscordRenderer",
)
# taken from version 0.6.1 of markdownify
WHITESPACE_RE = re.compile(r"[\r\n\s\t ]+")


CODE_BLOCK_RE = re.compile(
    r"(?P<delim>`{1,2})([^\n]+)(?P=delim)|```(.+?)```",
    re.DOTALL | re.MULTILINE,
)

GH_ISSUE_RE = re.compile(r"\s(?:GH-|#)(\d+)")


def remove_codeblocks(content: str) -> str:
    """Remove any codeblock in a message."""
    return CODE_BLOCK_RE.sub("", content)


class DocMarkdownConverter(MarkdownConverter):
    """Subclass markdownify's MarkdownCoverter to provide custom conversion methods."""

    def __init__(self, *, page_url: str, **options) -> None:
        super().__init__(**options)
        self.page_url = page_url

    # overwritten to use our regex from version 0.6.1
    def process_text(self, text: Optional[str]) -> Any:
        """Process the text, using our custom regex."""
        return self.escape(WHITESPACE_RE.sub(" ", text or ""))

    def convert_img(self, el: PageElement, text: str, convert_as_inline: bool) -> str:
        """Remove images from the parsed contents, we don't want them."""
        return ""

    def convert_li(self, el: Tag, text: str, convert_as_inline: bool) -> str:
        """Fix markdownify's erroneous indexing in ol tags."""
        parent = el.parent
        if parent is not None and parent.name == "ol":
            li_tags = parent.find_all("li")
            bullet = f"{li_tags.index(el)+1}."
        else:
            depth = -1
            curr_el = el
            while curr_el:
                if curr_el.name == "ul":
                    depth += 1
                curr_el = curr_el.parent
            bullets = self.options["bullets"]
            bullet = bullets[depth % len(bullets)]
        return f"{bullet} {text}\n"

    def convert_hn(self, _n: int, el: PageElement, text: str, convert_as_inline: bool) -> str:
        """Convert h tags to bold text with ** instead of adding #."""
        if convert_as_inline:
            return text
        return f"**{text}**\n\n"

    def convert_code(self, el: PageElement, text: str, convert_as_inline: bool) -> str:
        """Undo `markdownify`s underscore escaping."""
        return f"`{text}`".replace("\\", "")

    def convert_pre(self, el: Tag, text: str, convert_as_inline: bool) -> str:
        """Wrap any codeblocks in `py` for syntax highlighting."""
        code = "".join(el.strings)
        return f"```py\n{code}```"

    def convert_a(self, el: Tag, text: str, convert_as_inline: bool) -> str:
        """Resolve relative URLs to `self.page_url`."""
        href = el["href"]
        assert isinstance(href, str)
        el["href"] = urljoin(self.page_url, href)
        return super().convert_a(el, text, convert_as_inline)

    def convert_p(self, el: PageElement, text: str, convert_as_inline: bool) -> str:
        """Include only one newline instead of two when the parent is a li tag."""
        if convert_as_inline:
            return text

        parent = el.parent
        if parent is not None and parent.name == "li":
            return f"{text}\n"
        return super().convert_p(el, text, convert_as_inline)

    def convert_hr(self, el: PageElement, text: str, convert_as_inline: bool) -> str:
        """Convert hr tags to nothing. This is because later versions added this method."""
        return ""


# todo: this will be expanded over time as necessary
class DiscordRenderer(mistune.renderers.BaseRenderer):
    """Custom renderer for markdown to discord compatiable markdown."""

    def __init__(self, repo: str = None):
        self._repo = (repo or "").rstrip("/")

    def text(self, text: str) -> str:
        """Replace GitHub links with their expanded versions."""
        if self._repo:
            # todo: expand this to all different varieties of automatic links
            # if a repository is provided we replace all snippets with the correct thing
            def replacement(match: re.Match[str]) -> str:
                return self.link(self._repo + "/issues/" + match[1], text=match[0])

            text = GH_ISSUE_RE.sub(replacement, text)
        return text

    def link(self, link: str, text: Optional[str] = None, title: Optional[str] = None) -> str:
        """Properly format a link."""
        if text or title:
            if not text:
                text = link
            if title:
                paran = f'({link} "{title}")'
            else:
                paran = f"({link})"
            return f"[{text}]{paran}"
        else:
            return link

    def image(self, src: str, alt: str = None, title: str = None) -> str:
        """Return a link to the provided image."""
        return self.link(src, text="!image", title=alt)

    def emphasis(self, text: str) -> str:
        """Return italiced text."""
        return f"*{text}*"

    def strong(self, text: str) -> str:
        """Return bold text."""
        return f"**{text}**"

    def strikethrough(self, text: str) -> str:
        """Return crossed-out text."""
        return f"~~{text}~~"

    if constants.DiscordFeatures.extended_markdown:

        def heading(self, text: str, level: int) -> str:
            """Format the heading to be bold if its large enough, and underline it."""
            if level in (1, 2, 3):
                return "#" * (4 - level) + f" {text.strip()}\n"
            else:
                return f"__{text}__\n"

    else:

        def heading(self, text: str, level: int) -> str:
            """Format the heading to be bold if its large enough, and underline it."""
            if level in (1, 2, 3):
                return f"**__{text}__**\n"
            else:
                return f"__{text}__\n"

    def newline(self) -> str:
        """No op."""
        return ""

    def linebreak(self) -> str:
        """Return a new line."""
        return "\n"

    def inline_html(self, html: str) -> str:
        """No op."""
        return ""

    def thematic_break(self) -> str:
        """No op."""
        return ""

    def block_text(self, text: str) -> str:
        """Handle text in lists like normal text."""
        return self.text(text)

    def block_code(self, code: str, info: str = None) -> str:
        """Put the code in a codeblock."""
        md = "```"
        if info is not None:
            info = info.strip()
        if info:
            lang = info.split(None, 1)[0]
            md += lang
        md += "\n"
        return md + code.replace("`" * 3, "`\u200b" * 3) + "\n```\n"

    def block_quote(self, text: str) -> str:
        """Quote the provided text."""
        if text:
            return "> " + "> ".join(text.rstrip().splitlines(keepends=True)) + "\n\n"
        return ""

    def block_html(self, html: str) -> str:
        """No op."""
        return ""

    def block_error(self, html: str) -> str:
        """No op."""
        return ""

    def codespan(self, text: str) -> str:
        """Return the text in a codeblock."""
        char = "``" if "`" in text else "`"
        return char + text + char

    def paragraph(self, text: str) -> str:
        """Return a paragraph with a newline postceeding."""
        return f"{text}\n\n"

    def list(self, text: str, ordered: bool, level: int, start: Any = None) -> str:
        """Return the unedited list."""
        # todo: figure out how this should actually work
        if level == 1:
            return text.lstrip("\n") + "\n"
        return text

    def list_item(self, text: str, level: int) -> str:
        """Show the list, indented to its proper level."""
        lines = text.rstrip().splitlines()
        indent = "\u2001" * (level - 1)

        result: list[str] = [f"{indent}- {lines[0]}"]
        in_codeblock = False
        for line in lines[1:]:
            if "`" * 3 in line:  # very very very rudimentary codeblock detection
                if in_codeblock:
                    in_codeblock = False
                    if line.endswith("\n"):
                        line = line[:-1]
                    result.append(line)
                    continue
                else:
                    in_codeblock = True
                line = line.lstrip()
            if not line.strip():
                if in_codeblock:
                    continue
                result.append("")
            elif in_codeblock:
                result.append(line)
                continue
            else:
                # the space here should be about the same width as `- `
                result.append(f"{indent}\u2007{line}")

        return "\n".join(result) + "\n"

    def task_list_item(self, text: Any, level: int, checked: bool = False, **attrs) -> str:
        """Convert task list options to emoji."""
        emoji = constants.Emojis.confirmation if checked else constants.Emojis.no_choice_light
        return self.list_item(emoji + " " + text, level=level)

    def finalize(self, data: Any) -> str:
        """Finalize the data."""
        return "".join(data)
