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

# Elements whose contents are code, not words. `handle_data` records them like
# any other text node -- the DOM stays faithful to the document -- but a caller
# asking a node for its TEXT is asking what the page says, and a `<script>` in
# the subtree answered with the page's JavaScript: `_class_text` on a landing
# card returned the card's prose welded to a var declaration, and a post whose
# body carried a widget script quoted it verbatim.
NO_TEXT_TAGS = ("script", "style")


class _BlockOpen:
    """Marker: emit a newline here IF the line built so far is still open.

    The decision depends on what has already been collected, so it cannot be
    made when the child is queued -- only when the traversal reaches it. See
    `Node._text_into`, which is a stack rather than a recursion.
    """

    __slots__ = ()


_BLOCK_OPEN = _BlockOpen()


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
        """Every Node in this subtree, self first, in document order.

        An explicit stack, not recursion. The depth of this tree is the depth of
        a body that came off the network, and `yield from` spends a frame per
        level: a document nested 1 500 deep raised `RecursionError` out of
        `tgparse.parse_preview`, i.e. out of a public entry point, where a page
        this module cannot read has to be reported rather than crash the run.
        Telegram's own markup is shallow -- that is the point: nothing here may
        depend on a remote server keeping it that way.
        """
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(
                child for child in reversed(node.children) if isinstance(child, Node)
            )

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
        """Collect this subtree's text, iteratively -- see `walk` for why.

        The stack holds three kinds of item and pops them in document order: a
        string to append verbatim, a `_BLOCK_OPEN` marker whose newline depends
        on what has been collected by the time it is reached, and a Node still
        to be expanded.
        """
        stack: list = [self]
        while stack:
            item = stack.pop()
            if item is _BLOCK_OPEN:
                if _open_line(parts):
                    parts.append("\n")
                continue
            if isinstance(item, str):
                parts.append(item)
                continue
            batch: list = []
            for child in item.children:
                if isinstance(child, str):
                    batch.append(child)
                elif child.tag == "br":
                    batch.append("\n")
                elif child.tag in NO_TEXT_TAGS:
                    continue            # code, not words -- see NO_TEXT_TAGS
                elif child.tag in block_tags:
                    batch.extend((_BLOCK_OPEN, child, "\n"))
                else:
                    batch.append(child)
            stack.extend(reversed(batch))

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
