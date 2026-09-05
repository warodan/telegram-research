"""A very small DOM over `html.parser`, enough to query Telegram's pages.

Why this exists rather than a dependency: the accountless path must keep working
on a machine where nothing can be installed, and it must keep working in five
years. `html.parser` ships with Python. Telegram's markup is shallow, regular and
machine-generated, so the ~120 lines below cover it completely -- there is no
malformed-HTML recovery problem to outsource here.

Why not regular expressions alone: a message block is a nested `<div>`, and the
text inside it carries user-supplied markup. Balancing that with a regex is where
silent truncation comes from. Structure is parsed as structure; regexes in this
skill are reserved for flat attribute values.
"""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Elements that end the line they sit on. `pre` and `blockquote` are here
# because Telegram renders a code block as `<pre>` and an expandable quote as
# `<blockquote>` INSIDE the post body, and neither used to separate anything:
# `a<blockquote>quoted</blockquote>b` came back as `aquotedb`, so a quotation
# copied out of such a post was not the text the post carried. No page in the
# shipped probe corpus holds either element -- both are recent Telegram features -- which
# is exactly why the omission was invisible.
#
# `code` is deliberately NOT here: Telegram uses it for inline spans inside a
# sentence, and breaking the line around one would corrupt ordinary text.
BLOCK_TAGS = ("br", "div", "p", "pre", "blockquote")


def _open_line(parts: list[str]) -> bool:
    """Is there text before this point that a block would otherwise weld to?"""
    for chunk in reversed(parts):
        stripped = chunk.rstrip(" \t")
        if not stripped:
            continue
        return not stripped.endswith("\n")
    return False


class Node:
    """One element. `children` holds Nodes and plain strings, in document order."""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    # -- queries -----------------------------------------------------------
    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def walk(self):
        """Every Node in this subtree, self first."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def find_all(self, cls: str = None, tag: str = None, attr: str = None) -> list["Node"]:
        out = []
        for node in self.walk():
            if node is self:
                continue
            if cls and not node.has_class(cls):
                continue
            if tag and node.tag != tag:
                continue
            if attr and attr not in node.attrs:
                continue
            out.append(node)
        return out

    def find(self, cls: str = None, tag: str = None, attr: str = None) -> "Node | None":
        for node in self.find_all(cls=cls, tag=tag, attr=attr):
            return node
        return None

    # -- text --------------------------------------------------------------
    def text(self, *, block_tags=BLOCK_TAGS) -> str:
        """All text in this subtree, entities resolved, `<br>` becoming newlines.

        Emoji survive this. Telegram renders them as
        `<i class="emoji" style="background-image:url(...)"><b>WATCH</b></i>` --
        the character itself is a text node inside `<b>`, so collecting text
        nodes keeps it, while stripping the `<i>` subtree (which is what a
        selector-driven extractor tends to do) deletes every emoji in the post.

        A block element breaks the line on BOTH sides. Only the closing side
        used to, so `a<blockquote>quoted</blockquote>b` came out as `aquotedb`
        -- three words welded into one, in a record whose whole purpose is that
        a quotation out of it is verbatim. See BLOCK_TAGS for which elements
        Telegram actually puts inside a post body.
        """
        parts: list[str] = []
        self._text_into(parts, block_tags)
        joined = "".join(parts)
        lines = [ln.rstrip() for ln in joined.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines).strip()

    def _text_into(self, parts: list[str], block_tags) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                if child.tag == "br":
                    parts.append("\n")
                    continue
                block = child.tag in block_tags
                if block and _open_line(parts):
                    parts.append("\n")
                child._text_into(parts, block_tags)
                if block:
                    parts.append("\n")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        cls = " ".join(self.classes)
        return f"<{self.tag}{(' .' + cls) if cls else ''}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = Node("#document", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent or self.root

    def handle_data(self, data):
        self.cur.children.append(data)

    def handle_entityref(self, name):
        self.cur.children.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        self.cur.children.append(unescape(f"&#{name};"))


def parse(html: str) -> Node:
    """Parse a document and return its root Node."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root
