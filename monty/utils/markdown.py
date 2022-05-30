import re
from typing import Any, Optional
from urllib.parse import urljoin

from bs4.element import PageElement, Tag
from markdownify import MarkdownConverter


# taken from version 0.6.1 of markdownify
WHITESPACE_RE = re.compile(r"[\r\n\s\t ]+")


class DocMarkdownConverter(MarkdownConverter):
    """Subclass markdownify's MarkdownCoverter to provide custom conversion methods."""

    def __init__(self, *, page_url: str, **options):
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
