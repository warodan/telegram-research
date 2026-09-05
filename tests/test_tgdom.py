"""tgdom.py -- the small DOM everything else in this skill parses through.

These are unit tests of the parser itself and use short hand-written snippets
rather than the saved probes: tgdom has no notion of Telegram at all, and its
correctness (entity handling, `<br>`, malformed markup) does not depend on
which site produced the HTML. The real probes come back in test_tgparse.py,
where the markup shapes matter.
"""

from __future__ import annotations

import tgdom


def test_text_keeps_emoji_characters():
    # Telegram's own emoji shape, as measured: the character is a text node
    # inside <b>, wrapped by an <i class="emoji"> whose background-image is the
    # PNG. An extractor that drops the <i> subtree deletes every emoji.
    html = (
        '<div class="msg">Look '
        '<i class="emoji" style="background-image:url(x)"><b>\U0001F600</b></i>'
        " there</div>"
    )
    node = tgdom.parse(html).find(cls="msg")
    assert "\U0001F600" in node.text()
    assert node.text() == "Look \U0001F600 there"


def test_br_becomes_newline():
    html = '<div class="msg">line1<br>line2<br/>line3</div>'
    node = tgdom.parse(html).find(cls="msg")
    assert node.text() == "line1\nline2\nline3"


def test_a_blockquote_does_not_weld_itself_to_its_neighbours():
    # Telegram renders an expandable quote as <blockquote> inside the post body.
    # It was not a block tag here and emitted no separator at all, so three
    # words came out as one -- in a record whose entire purpose is that a
    # quotation taken out of it is verbatim.
    html = '<div class="msg">before<blockquote>quoted</blockquote>after</div>'
    node = tgdom.parse(html).find(cls="msg")
    assert node.text() == "before\nquoted\nafter"


def test_a_code_block_does_not_weld_itself_to_its_neighbours():
    html = '<div class="msg">run this<pre>line one\nline two</pre>then this</div>'
    node = tgdom.parse(html).find(cls="msg")
    assert node.text() == "run this\nline one\nline two\nthen this"


def test_inline_code_does_not_break_the_line():
    # The other direction, and the reason <code> is deliberately not a block
    # tag: Telegram uses it for spans inside a sentence, and breaking around
    # one would corrupt ordinary text.
    html = '<div class="msg">set <code>max_gap</code> to four</div>'
    assert tgdom.parse(html).find(cls="msg").text() == "set max_gap to four"


def test_a_block_at_the_start_does_not_open_with_a_blank_line():
    html = '<div class="msg"><blockquote>only</blockquote></div>'
    assert tgdom.parse(html).find(cls="msg").text() == "only"


def test_entities_are_unescaped():
    html = '<div class="msg">Tom &amp; Jerry &lt;3 &nbsp;end</div>'
    node = tgdom.parse(html).find(cls="msg")
    text = node.text()
    assert "Tom & Jerry <3" in text
    assert "&amp;" not in text
    assert "&lt;" not in text
    # &nbsp; decodes to U+00A0, not a literal ASCII space or the entity text.
    assert "\xa0" in text


def test_find_by_class():
    html = '<div class="a"><span class="b">x</span><span class="c">y</span></div>'
    root = tgdom.parse(html)
    assert root.find(cls="b").text() == "x"
    assert root.find(cls="c").text() == "y"
    assert root.find(cls="nope") is None


def test_find_by_tag():
    html = "<div><p>one</p><span>two</span><p>three</p></div>"
    root = tgdom.parse(html)
    ps = root.find_all(tag="p")
    assert [p.text() for p in ps] == ["one", "three"]
    assert root.find(tag="span").text() == "two"


def test_find_by_attr():
    html = '<div><a href="x">1</a><a>2</a><a href="y">3</a></div>'
    root = tgdom.parse(html)
    hrefs = [a.attrs["href"] for a in root.find_all(attr="href")]
    assert hrefs == ["x", "y"]
    assert root.find(attr="data-post") is None


def test_find_all_nested_identical_classes():
    # A message wrap nested inside another wrap of the same class must not be
    # skipped or double-visited -- tgparse.parse_preview relies on find_all
    # returning every tgme_widget_message_wrap on the page, one per message.
    html = (
        '<div class="x" id="outer">'
        '<div class="x" id="mid"><div class="x" id="inner">deep</div></div>'
        "</div>"
    )
    root = tgdom.parse(html)
    matches = root.find_all(cls="x")
    assert len(matches) == 3
    assert [m.attrs.get("id") for m in matches] == ["outer", "mid", "inner"]


def test_malformed_unclosed_tag_does_not_throw():
    # html.parser recovers from this on its own; the assertion is that parse()
    # never raises and still returns something queryable, not that any
    # particular repair is "correct" -- there is no correct repair for markup
    # this broken, only a choice not to crash the whole read.
    html = '<div class="a"><span>unclosed<div class="b">next</div>'
    root = tgdom.parse(html)
    assert root.find(cls="a") is not None
    assert root.find(cls="b") is not None
    assert root.find(cls="b").text() == "next"


def test_malformed_stray_closing_tag_does_not_throw():
    html = "<div>hello</span></div><p>after</p>"
    root = tgdom.parse(html)
    assert root.find(tag="p").text() == "after"


def test_classes_property_splits_on_whitespace():
    html = '<div class="tgme_widget_message js-widget_message user-color-12">x</div>'
    node = tgdom.parse(html).find(tag="div")
    assert node.classes == ["tgme_widget_message", "js-widget_message", "user-color-12"]
    assert node.has_class("tgme_widget_message")
    assert not node.has_class("tgme_widget_message_wrap")  # exact-token match, not substring


def test_real_probe_parses_without_error(probe):
    # A single sanity check against real Telegram markup, cheap enough to run
    # every time: the hand-written snippets above cover the edge cases, this
    # confirms tgdom does not choke on the real thing either.
    body = probe("A01-s-durov.html")
    root = tgdom.parse(body)
    wraps = root.find_all(cls="tgme_widget_message_wrap")
    assert len(wraps) == 20


def test_find_all_never_returns_the_node_it_was_called_on():
    """A quiet contract nothing held, and `_fill_from` leans on it.

    `find_all` skips `self`; deleting that line left all 703 tests green and
    every probe in the corpus byte-identical, because no lookup class sits on
    a message wrap today. `tgparse._fill_from`'s `outside()` helper takes the
    FIRST node `find_all` returns, so on any surface where the wrap itself
    carried one of the SEL classes the post would start describing itself --
    reading its own wrap as its text node, its author node or its reply block.
    """
    html = '<div class="a" id="outer"><div class="a" id="inner">deep</div></div>'
    outer = tgdom.parse(html).find(cls="a")
    assert outer.attrs["id"] == "outer"

    found = outer.find_all(cls="a")
    assert [n.attrs.get("id") for n in found] == ["inner"]
    assert outer not in found
    assert outer.find(cls="a").attrs["id"] == "inner"
    # the same for the tag and attribute lookups, which share the loop
    assert [n.attrs.get("id") for n in outer.find_all(tag="div")] == ["inner"]
    assert [n.attrs.get("id") for n in outer.find_all(attr="id")] == ["inner"]
    # and `walk` still yields self first -- the two are deliberately different,
    # because `_fill_from` builds its exclusion set out of `walk`.
    assert next(iter(outer.walk())) is outer


# --------------------------------------------------------------------------
# Depth is a property of the body, and the body came off the network
# --------------------------------------------------------------------------
def _nested(depth: int, inner: str = "deep") -> str:
    return "<div>" * depth + inner + "</div>" * depth


def test_a_deeply_nested_document_does_not_blow_the_stack():
    """`walk` and `_text_into` were recursive, one frame per level of nesting.

    A 1 500-deep document therefore raised `RecursionError` out of
    `tgparse.parse_preview` -- a public entry point, on a body from the network.
    Telegram's markup is shallow; nothing here may depend on a remote server
    keeping it that way, and a page this module cannot read has to be reported
    rather than end the run with a traceback.
    """
    root = tgdom.parse(_nested(1500))
    assert len(list(root.walk())) == 1501            # the document node plus 1500
    assert root.text() == "deep"
    assert len(root.find_all(tag="div")) == 1500


def test_depth_does_not_change_what_the_text_comes_out_as():
    # The rewrite is a change of mechanism, not of meaning: block tags still
    # break the line on both sides, and `<br>` still becomes one newline.
    html = ('<div class="msg">a<div>b<blockquote>c<br>d</blockquote>e</div>f'
            "</div>")
    node = tgdom.parse(html).find(cls="msg")
    assert node.text() == "a\nb\nc\nd\ne\nf"


def test_script_and_style_are_not_words_the_page_said():
    """`<script>` content is a text node like any other, and `text()` took it.

    A post carrying a widget script quoted the script; `_class_text` on a
    landing card welded the card's prose to a var declaration. The DOM keeps
    both -- an attribute lookup still works -- but neither is anything the page
    says.
    """
    html = ('<div class="msg">before'
            '<script>var x = "not text";</script>'
            "<style>.a{color:red}</style>"
            "after</div>")
    node = tgdom.parse(html).find(cls="msg")
    assert node.text() == "beforeafter"
    # and the elements are still in the tree, with their attributes
    assert [n.tag for n in node.find_all(tag="script")] == ["script"]
