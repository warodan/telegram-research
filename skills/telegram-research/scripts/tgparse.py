"""Parsers for the three Telegram web surfaces, and only those three.

Every selector below was read off a real page saved during the 2026-08-23 probe.
The measurements here were taken on all 58 saved pages. 32 of them survive, in
the project repository at `tests/fixtures/probes/` beside the pytest suite --
outside the skill folder, which is what gets installed; the 10 `selftest` parses
travel with the skill at the same relative path. Pages were dropped from the
public copy to protect the privacy of third parties, so a count of the form
"N of 58" describes the measurement rather than either directory. Those files are
real pages from real channels and groups, which is why the test suite runs
against them rather than against hand-authored HTML: a fixture an author wrote
agrees with the parser that author wrote, and proves nothing about Telegram.

Three traps this module exists to not fall into:

* **Emoji.** Telegram renders them as
  `<tg-emoji emoji-id="…"><i class="emoji" style="background-image:url(…)"><b>WATCH</b></i></tg-emoji>`.
  The character is a text node inside `<b>`; the PNG is a CSS background, not an
  `<img>`. Any extractor that drops the `<i class="emoji">` subtree deletes every
  emoji in the post, silently.
* **Rounded counters.** Views arrive as `12.5M`, a string. The raw string is
  always kept next to any number derived from it, because the number is a guess
  and the string is the measurement.
* **Author identity is weaker here than over MTProto.** Display name always, a
  public username sometimes, a user id never. Nothing in this module may require
  a user id, and the record shape reflects that.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from dataclasses import dataclass, field, asdict

import tgdom
from tgweb import (
    member_count,
    name_taken,
    online_count,
    peer_type,
    post_missing,
    search_found_nothing,
    username_exists,
)

# How many message IDS a full `/s/` page carries. Not a tuning knob -- it is
# what Telegram serves, measured on every `/s/` probe that is not the last page
# of its walk: A01, A02, A09, C03 and C04 all carry exactly 20, and C15, the one
# terminal page in the corpus, carries 7. `_cursors` uses it to tell "this page
# is full, there is probably more" from "this page is short, this was the end".
#
# **Ids, not blocks.** This used to be counted in `len(page.messages)`, and on
# any channel that posts albums those two numbers are different: an album is one
# block carrying one `data-post` and several ids. Measured live 2026-08-25 on
# t.me/s/nexta_tv -- ids 27033-27052 (twenty), 18 blocks, `rel="prev"` present,
# so the page was full and `len(page.messages) >= PAGE_SIZE` was false. On such a
# channel the fallback below was dead code and, by its own reasoning, a full page
# would have been read as the end of the history. `PreviewPage.ids_seen` is the
# honest input and `blocks_seen` is the second one: a page of 20 blocks of which
# 3 fail to parse is not a short page either.
PAGE_SIZE = 20


def _warn(message: str) -> None:
    """Say something is wrong with the parse, on stderr, once, in one line.

    Reserved for the case where the parser understood nothing: that is a change
    in Telegram's front end or a break in this file, and either way it must not
    leave the process looking like an ordinary empty result.
    """
    print(f"tgparse: {message}", file=sys.stderr)

# --------------------------------------------------------------------------
# Selector table -- every markup assumption in the skill lives here.
#
# This is the file that rots when Telegram changes its front end. It is kept
# separate and flat on purpose: a layout change should be a diff to this table
# and to nothing else. Values are class names unless the name says otherwise.
# --------------------------------------------------------------------------
# Four keys -- `not_supported`, `not_supported_cont`, `page_extra`, `page_photo`
# -- are indexed by no line of this module and stay anyway. The table is checked
# against `references/surfaces.md` key by key, so a selector dropped for being
# unindexed takes with it what that file records about the surface. `page_extra`
# is read elsewhere: `tgweb.py` matches the same class through a regex of its own.
SEL = {
    "msg_wrap":        "tgme_widget_message_wrap",
    "msg":             "tgme_widget_message",
    # The body text and the quoted text of the message being replied to share the
    # class `tgme_widget_message_text` and are told apart ONLY by the `js-` twin.
    # Reading the first `tgme_widget_message_text` in document order returns the
    # QUOTE on every reply -- measured on tdlibchat/10000, where it silently
    # replaced the post with the post it answered.
    "msg_text":        "js-message_text",
    "reply_text":      "js-message_reply_text",
    "owner_name":      "tgme_widget_message_owner_name",
    "author_name":     "tgme_widget_message_author_name",
    "from_author":     "tgme_widget_message_from_author",
    "views":           "tgme_widget_message_views",
    "reactions":       "tgme_widget_message_reactions",
    "reaction":        "tgme_reaction",
    "reply":           "tgme_widget_message_reply",
    "forwarded_from":  "tgme_widget_message_forwarded_from_name",
    "photo":           "tgme_widget_message_photo_wrap",
    "video":           "tgme_widget_message_video",
    "document":        "tgme_widget_message_document",
    "voice":           "tgme_widget_message_voice",
    "sticker":         "tgme_widget_message_sticker",
    "poll":            "tgme_widget_message_poll",
    "location":        "tgme_widget_message_location",
    "link_preview":    "tgme_widget_message_link_preview",
    "user_photo":      "tgme_widget_message_user_photo",
    # An album (grouped media) is ONE block carrying ONE `data-post`, and the
    # ids of its other items exist nowhere else in the markup: they are the
    # `href="https://t.me/<name>/<id>?single"` permalinks inside this wrapper.
    # Measured live 2026-08-25 on t.me/s/nexta_tv, which served ids 27033-27052
    # under 18 `data-post` attributes -- 27043 and 27044 were inside the album
    # whose `data-post` is nexta_tv/27042 and appeared in no other form.
    "grouped_wrap":    "js-message_grouped_wrap",
    # A video's poster: an `<i>` inside the player carrying the still as a CSS
    # background. It is not the file -- no `token=`, and it is a JPEG. It occurs
    # 38 times in the 58 probes and every one of them is a video's poster.
    "video_thumb":     "tgme_widget_message_video_thumb",
    # A genuine service event, and the two markers it is served under. This
    # table used to say `text_not_supported_wrap`, which is a static styling
    # class on EVERY ordinary `tgme_widget_message` div: across the 58 saved
    # probes it flagged 122 messages out of 122 as service messages.
    #
    # * `/s/` pages carry the class `service_message` on the message div. It
    #   occurs exactly once in the whole corpus (Astana_motoriders/97, a pinned
    #   event) and that is the right one.
    # * `?embed=1` pages serve no such class. There the marker is structural: a
    #   `message_media_not_supported_wrap` standing where the body would be,
    #   i.e. a DIRECT CHILD of `tgme_widget_message_bubble`. It occurs three
    #   times in the corpus and all three are genuine service messages. The
    #   label text next to it reads "Service message", but the text is
    #   localisable and the structure is not, so the structure is what is read.
    #
    # The same wrap class means two other things, neither of them a service
    # message: under `media_not_supported_cont` it is the "Please open Telegram
    # to view this post" footer that every post carries (66 of them), and under
    # `tgme_widget_message_video_player` it means this browser cannot play the
    # video (38 of them) -- a property of the browser, recorded in `media`.
    "service":            "service_message",
    "bubble":             "tgme_widget_message_bubble",
    "not_supported_wrap": "message_media_not_supported_wrap",
    "not_supported_cont": "media_not_supported_cont",
    "video_player":       "tgme_widget_message_video_player",
    "not_supported":      "message_media_not_supported_label",
    "page_title":      "tgme_page_title",
    "page_extra":      "tgme_page_extra",
    "page_desc":       "tgme_page_description",
    "page_photo":      "tgme_page_photo_image",
    "more":            "tme_messages_more",
    # attributes, not classes
    "attr_post":       "data-post",       # "<username>/<id>"
    "attr_view":       "data-view",       # base64url JSON carrying the chat id
    "attr_peer":       "data-peer",       # "c<id>_<hash>" -- the group's only id
    "attr_datetime":   "datetime",
}

# Subtrees whose CDN URLs belong to something other than this message's media:
# the sender's (or channel's) avatar, and the thumbnail of a linked page. Both
# are served from telesco.pe like real media, and both used to be collected as
# if the post carried them -- a one-line text message came back claiming an
# `.mp4`, which was the sender's animated profile photo.
NOT_MESSAGE_MEDIA = ("user_photo", "link_preview")

MEDIA_CLASSES = {
    "photo": SEL["photo"],
    "video": SEL["video"],
    "document": SEL["document"],
    "voice": SEL["voice"],
    "sticker": SEL["sticker"],
    "poll": SEL["poll"],
    "location": SEL["location"],
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass
class PeerCard:
    """What `t.me/<name>` says about a name, in one GET and with no account."""

    username: str
    exists: bool | None = None         # is it a READABLE channel or group
    taken: bool | None = None          # is the name claimed by anything, person included
    type: str | None = None            # "channel" | "group" | "user" | None
    title: str | None = None
    description: str | None = None
    members: int | None = None
    online: int | None = None
    photo: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Message:
    """One post, in the strongest form the accountless surface can give.

    `author_username` and `chat_id` are absent far more often than present; that
    is a property of the surface, not a parse failure. `found_by` records which
    query surfaced this post -- the input the next round of query craft runs on.
    """

    username: str
    id: int
    url: str
    # Every id this block accounts for, sorted, `id` always among them. A lone
    # post is `[id]`; an album is the `data-post` id plus the ids of the other
    # items in the group, which Telegram serves ONLY as `?single` permalinks
    # inside the block. Without this a `/s/` page of 20 messages containing one
    # 3-item album returned 18 records and nothing anywhere said two ids had
    # been swallowed -- and a `?q=` hit whose caption lives on a swallowed id
    # was permalinked to the album's first id, a link that resolves, to the
    # wrong message. Anything counting posts or deciding "was this page full"
    # counts these, never `len(page.messages)`.
    ids: list[int] = field(default_factory=list)
    date: str | None = None                 # ISO 8601 with offset, verbatim
    text: str = ""
    # Every anchor inside the post's text, in document order, as
    # `{"text": ..., "href": ...}`. `text` keeps the anchor's words and drops
    # its destination, which is the whole substance of a post whose point is a
    # link: measured on live `rian_ru`, 41 of 41 anchors had a destination that
    # was unrecoverable from the text, including the news story every post
    # cites. `SKILL.md` documents `discover --found-via link` as a discovery
    # channel fed from post text, and until this field existed that channel was
    # mining a stream the links had already been deleted from.
    links: list[dict] = field(default_factory=list)
    author_name: str | None = None
    author_username: str | None = None
    channel_title: str | None = None
    signature: str | None = None            # channel post signature, if any
    views_raw: str | None = None            # "12.5M" -- the measurement
    views: int | None = None                # a decoding of it, approximate
    reactions: dict[str, str] = field(default_factory=dict)
    media: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    # The entries of `media_urls` that are a still standing in for another file
    # rather than the file itself -- see `_media_urls`. `media_urls[0]` of a
    # video post is one of these: a JPEG with no `token=`, for a record whose
    # `media` says `['video']`.
    media_posters: list[str] = field(default_factory=list)
    reply_to_author: str | None = None
    reply_to_text: str | None = None
    reply_to_id: int | None = None
    forwarded_from: str | None = None
    is_service: bool = False
    chat_id: int | None = None              # only on channel /s/ pages
    # The raw `data-peer` value, verbatim: "c1000000001_4000000000000000001".
    # It is served on ?embed=1 pages only, and it is the ONLY id a group has on
    # any accountless surface. Kept as the string it was served as rather than
    # folded into `chat_id`: for @durov `data-view`'s c is -1006503122 and
    # data-peer's first component is c1006503122 -- the same number under two
    # sign conventions, and picking one would be inventing a fact.
    chat_peer: str | None = None
    found_by: str | None = None             # the query that surfaced it
    source_file: str | None = None          # the saved original this came from
    # Set ONLY when `?embed=1` served a different post than the one asked for.
    # `embed()` follows redirects and the record's id, username and permalink
    # all come from the served page's `data-post`, so a redirect to another id
    # produced a perfectly consistent record filed under an id the walk never
    # requested -- and the walk booked a hit for the id it did request.
    requested_id: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PreviewPage:
    """One `/s/` page: up to 20 messages plus the cursors to walk further.

    `blocks_seen` and `blocks_unparsed` exist so that "this page held nothing"
    and "the parser understood none of the twenty blocks on it" stop being the
    same output. They were byte-identical -- `messages: []`, `found_nothing:
    False`, a live cursor -- which is the same class of defect the 2026-08-24
    repair fixed one layer up for `?q=`.
    """

    username: str
    messages: list[Message] = field(default_factory=list)
    before: int | None = None
    after: int | None = None
    chat_id: int | None = None
    found_nothing: bool = False
    blocks_seen: int = 0                    # message blocks on the page
    blocks_unparsed: int = 0                # of those, ones that yielded nothing
    # Distinct message ids across every block on the page, albums included. This
    # is how many posts the page actually carried; `len(messages)` is how many
    # BLOCKS it carried, and on a channel that posts albums the two differ by
    # the size of every album on it. Anything filling a `--count N` budget,
    # reporting "N posts found" or deciding "the page was short, so that was the
    # end" reads this.
    ids_seen: int = 0
    # True when `before` is the smallest id seen rather than a cursor the page
    # published. A caller that wants to report honestly can say which it walked.
    before_is_fallback: bool = False

    @property
    def is_full(self) -> bool:
        """Did this page carry as much as Telegram serves at once?

        Counted in ids and in blocks and never in parsed messages -- an album is
        one block with several ids, a block that failed to parse is a block all
        the same. `_cursors` and `no_message_carries_text` both ask this, and
        they have to ask it the same way.
        """
        return max(self.ids_seen, self.blocks_seen) >= PAGE_SIZE

    @property
    def no_message_carries_text(self) -> bool:
        """A full page parsed, and not one word of text came out of any of it.

        The second way a front-end change looks, and the quiet one.
        `SEL["msg_text"]` is `js-message_text`, a JS hook class and the most
        rot-prone value in the table: rename it and `_message_from` still
        succeeds off `data-post` for all twenty blocks, so `blocks_unparsed` is
        0, `understood_nothing` was False, nothing reached stderr, and the run
        published twenty posts with `text: ""`. A report built on that says
        twenty posts were found and quotes nothing from any of them -- a false
        negative under the skill's own name.

        Measured 2026-08-25. Pristine: every one of the 58 saved probes and
        three live pages (`durov`, `rian_ru`, `nexta_tv`) has text on at least
        one message -- in fact on every message, 20/20 in each case. Mutated by
        that one rename: six pages out of six, corpus and live, come back with
        text on none.

        **Only a full page may say this**, which is why `is_full` is in the
        test. A page of two caption-less photos is an ordinary thing and is not
        evidence of anything; a page carrying everything Telegram serves at once
        without a character of text on any of it is not a channel that said
        nothing. The price is that a rename would go unnoticed on a short page,
        i.e. on the last page of a walk or a small `?q=` result. That is the
        safe direction: this verdict stops a walk, so a false positive
        truncates a live channel.

        **A page on which every post is media is not accused.** A photo with no
        caption carries no text and never did, and a full page of them is an
        ordinary thing on a channel that posts pictures -- measured on a page of
        20 caption-less photos, this said True, the walk stopped at
        `understood_nothing` and 20 correctly parsed posts were thrown away as
        unreadable. So a page where EVERY message came back carrying media is
        left alone: the parse got something out of every block on it.

        `all`, not `any`, and that is the whole subtlety. When the text selector
        moves, the posts that carried media still have it -- A01 renamed is 8
        media posts and 12 records with nothing in them at all -- so `any` would
        have let the rename through on any page with one photo on it, which is
        most of them. A record with neither text nor media is a block that
        yielded nothing, and one of those on a full silent page is enough.

        The price is a page that is BOTH entirely caption-less media and served
        under a moved text selector, which goes unnoticed. That is the same
        deliberate blind spot as the short page above, in the same direction.
        """
        if not self.messages or not self.is_full:
            return False
        if any(msg.text for msg in self.messages):
            return False
        return not all(msg.media for msg in self.messages)

    @property
    def understood_nothing(self) -> bool:
        """The page carried message blocks and the parse got nothing out of them.

        This is never absence. It is Telegram's front end having changed, or
        this file having broken, and a run that reports it as "no posts found"
        publishes a false negative under the skill's own name.

        Two shapes, one verdict: no block parsed at all (`data-post` moved), or
        a full page of blocks that all parsed and all came back with no text
        (`js-message_text` moved). The second used to pass in silence.
        """
        if self.blocks_seen <= 0:
            return False
        return not self.messages or self.no_message_carries_text


# --------------------------------------------------------------------------
# Landing page
# --------------------------------------------------------------------------
def parse_landing(body: str, username: str) -> PeerCard:
    """`t.me/<name>` -- the one GET that decides the whole read route.

    Type is decided here and nowhere else. `/s/` answers 302 for a group and for
    a name that does not exist, identically, so it can never be the type test.
    """
    card = PeerCard(username=username.lstrip("@"))
    card.exists = username_exists(body)
    card.taken = name_taken(body)
    card.type = peer_type(body)
    card.members = member_count(body)
    card.online = online_count(body)
    card.title = _meta(body, "og:title") or _class_text(body, SEL["page_title"])
    card.description = _meta(body, "og:description") or _class_text(body, SEL["page_desc"])
    card.photo = _meta(body, "og:image")
    return card


# --------------------------------------------------------------------------
# /s/ preview page
# --------------------------------------------------------------------------
def parse_preview(body: str, username: str, *, found_by: str | None = None,
                  source_file: str | None = None) -> PreviewPage:
    """Parse a channel preview page: messages, cursors and the numeric chat id."""
    page = PreviewPage(username=username.lstrip("@"))
    # The whole "nothing was said" decision is `tgweb.search_found_nothing`, and
    # it is structural in both halves: a page carrying even one `data-post` is
    # never a zero-hit page whatever its prose says, and the marker has to be an
    # element's class rather than a run of characters somewhere in the document.
    # Read that function before touching this line -- both of the mistakes it
    # documents shipped, one in each direction.
    if search_found_nothing(body):
        page.found_nothing = True
        return page

    root = tgdom.parse(body)
    for wrap in root.find_all(cls=SEL["msg_wrap"]):
        page.blocks_seen += 1
        msg = _message_from(wrap, username, found_by=found_by, source_file=source_file)
        if msg:
            page.messages.append(msg)
        else:
            page.blocks_unparsed += 1

    page.chat_id = _chat_id_from(root)
    for msg in page.messages:
        if msg.chat_id is None:
            msg.chat_id = page.chat_id

    page.ids_seen = len(_ids_on(page))
    page.before, page.after, page.before_is_fallback = _cursors(body, page)
    if page.understood_nothing and not page.messages:
        _warn(
            f"{page.blocks_seen} message blocks on t.me/s/{page.username} and not "
            "one of them parsed — this is a front-end change or a break in this "
            "file, NOT an empty page. Nothing here may be reported as absence."
        )
    elif page.understood_nothing:
        _warn(
            f"a full page of {len(page.messages)} messages parsed off "
            f"t.me/s/{page.username} and not one of them carries any text — the "
            f"text selector ({SEL['msg_text']}) has moved. This is NOT a page of "
            "silent posts; nothing here may be quoted or reported as absence."
        )
    return page


def _ids_on(page: PreviewPage) -> set[int]:
    """Every distinct message id the page accounted for, albums included."""
    ids: set[int] = set()
    for msg in page.messages:
        ids.update(msg.ids or [msg.id])
    return ids


def _cursors(body: str, page: PreviewPage) -> tuple[int | None, int | None, bool]:
    """Where to go next: `(before, after, before_is_fallback)`.

    The page publishes its own `?before=` / `?after=` links, and those are
    preferred. Where the link is absent the cursor falls back to the smallest id
    actually seen, never to arithmetic on it: message ids have gaps, so
    `min_id - 20` walks over live posts without ever fetching them.

    Which link is read matters. Telegram's `/s/` template emits three `before=`
    hrefs in this order: `<link rel="prev">`, `<link rel="canonical">`, and the
    `tme_messages_more` anchor. Taking the FIRST href in the body -- which is
    what this did -- is right only while `rel="prev"` exists. On a terminal page
    it does not, and the first href is then `rel="canonical"`: the URL of the
    page already in hand. C15 (durov?q=bitcoin, 7 posts, ids 62..440) returned
    `before=441`, its own canonical, and refetching it served the same seven
    posts.

    **The fallback only fires on a FULL page.** With no size test it fired on
    every terminal page too, so `page.before` was never None while the page had
    messages -- and `read.walk_channel` / `search_channel` could therefore never
    take their "the surface published no further cursor" branch on the page that
    actually said so. They took it one request later, on the empty page that
    follows, so every walk in the skill ended by spending one request past the
    true end of the history and, on a channel whose oldest page carries no id
    <= 1, ended on a FIFTH stop reason (`no_messages`) that no document named
    at the time. Measured on C15: `rel="prev"` absent, more-anchor absent, 7 messages
    of a 20-message page -- a page that short is the last one. A page that is
    full and still publishes no cursor is the ambiguous case, and there the
    fallback still runs, because truncating a walk is worse than one request.

    **"Full" is counted in ids and in blocks, never in parsed messages.** An
    album is one block carrying several ids, so `len(page.messages)` understates
    a full page on any channel that posts one -- see PAGE_SIZE, and the live
    nexta_tv page that made this test unreachable. A block that failed to parse
    understates it the other way. Either reading turns a full page into "this
    was the last one" and ends the walk on a channel that is still talking.
    """
    before = _rel_cursor(body, "prev", "before") or _more_cursor(body, "before")
    after = _rel_cursor(body, "next", "after") or _more_cursor(body, "after")
    fallback = False
    ids = _ids_on(page)
    if before is None and ids and page.is_full:
        before = min(ids)
        fallback = True
    return before, after, fallback


def _rel_cursor(body: str, rel: str, param: str) -> int | None:
    """`<link rel="prev" href="/s/x?before=N">` -- never `rel="canonical"`."""
    for m in re.finditer(r"<link\b[^>]*>", body, re.I):
        tag = m.group(0)
        if not re.search(rf'\brel="{re.escape(rel)}"', tag, re.I):
            continue
        hm = re.search(rf'href="[^"]*[?&]{re.escape(param)}=(\d+)', tag)
        if hm:
            return int(hm.group(1))
    return None


def _more_cursor(body: str, param: str) -> int | None:
    """The `tme_messages_more` anchor -- the same cursor, from the page body."""
    for m in re.finditer(r"<a\b[^>]*>", body, re.I):
        tag = m.group(0)
        if SEL["more"] not in tag:
            continue
        hm = re.search(rf'href="[^"]*[?&]{re.escape(param)}=(\d+)', tag)
        if hm:
            return int(hm.group(1))
    return None


# --------------------------------------------------------------------------
# ?embed=1 single message -- the group read path
# --------------------------------------------------------------------------
def parse_embed(body: str, username: str, message_id: int, *,
                found_by: str | None = None,
                source_file: str | None = None) -> Message | None:
    """One message. Returns None when the id carries no message.

    None is not the end of history. On `birding_chats`, 29326 and 29327 were live
    while 29320, 10000, 50000 and 200000 all answered `Post not found` on the
    same day. Whether that is deletion or an unrenderable message type is still
    unestablished, and either way a walk must keep going.

    **Nothing is manufactured here.** This used to build a `Message` out of the
    requested id whenever it could not find a `data-post`, justified by "a
    service message carries no data-post attribute". The corpus says otherwise:
    all three genuine embed service messages carry one, and the only 7 pages in
    all 58 whose message div lacks it are the 7 "Post not found" error pages --
    so the branch's sole reachable input was the page that should have returned
    None one line earlier. One localisation of that English string ("Message not
    found", or the same in Russian: the request sends
    `Accept-Language: en,ru;q=0.9`) turned every empty id into a `Message` with
    the requested id, a null date, empty text and a plausible permalink. At the
    measured 1.7 % head density on a group, `--count 50` would have filled with
    50 fabricated posts and the report would have said 50 posts were found on a
    group that said nothing.
    """
    if post_missing(body):
        return None
    root = tgdom.parse(body)
    wrap = root.find(cls=SEL["msg_wrap"]) or root.find(cls=SEL["msg"])
    msg = None
    if wrap is not None:
        msg = _message_from(wrap, username, found_by=found_by, source_file=source_file)
    if msg is None:
        # Neither a message nor a recognised absence: a front-end change, a join
        # wall, an interstitial. It is not proof the id is empty and it must not
        # be recorded as one -- see tgweb.embed_unreadable.
        _warn(
            f"t.me/{username.lstrip('@')}/{int(message_id)}?embed=1 carried "
            "neither a message nor a 'post not found' marker. This is NOT proof "
            "the id is empty; nothing may report it as one."
        )
        return None
    if msg.id != int(message_id) or msg.username != username.lstrip("@"):
        # The record itself is sound -- id, permalink and text all came off the
        # same `data-post` -- but it is not the post that was asked for, and the
        # walk that asked is about to book a hit for an id it never received.
        msg.requested_id = int(message_id)
    return msg


# --------------------------------------------------------------------------
# shared message extraction
# --------------------------------------------------------------------------
def _message_from(wrap, username: str, *, found_by: str | None,
                  source_file: str | None) -> Message | None:
    holder = wrap.find(attr=SEL["attr_post"])
    if holder is None and SEL["attr_post"] in wrap.attrs:
        holder = wrap
    if holder is None:
        return None
    post = holder.attrs.get(SEL["attr_post"], "")
    m = re.match(r"([^/]+)/(\d+)$", post)
    if not m:
        return None
    name = m.group(1)
    mid = _small_int(m.group(2))
    if mid is None:
        # A `data-post` whose id is a thousand digits long is a damaged page,
        # not a post. `int()` itself refuses above 4300 digits (CPython's
        # int_max_str_digits) and the ValueError came out of `parse_preview`,
        # i.e. out of a public entry point, on a body from the network.
        return None
    msg = Message(
        username=name,
        id=mid,
        url=f"https://t.me/{name}/{mid}",
        found_by=found_by,
        source_file=source_file,
    )
    msg.ids = _album_ids(wrap, name, mid)
    view_attr = holder.attrs.get(SEL["attr_view"])
    if view_attr:
        payload = decode_data_view(view_attr)
        if payload and isinstance(payload.get("c"), int):
            msg.chat_id = payload["c"]
    peer_attr = holder.attrs.get(SEL["attr_peer"])
    if peer_attr:
        msg.chat_peer = peer_attr
    _fill_from(wrap, msg)
    return msg


def _small_int(digits: str) -> int | None:
    """A message id, or None if the string is too long to be one."""
    if len(digits) > 19:            # int64 is 19 digits; nothing on t.me is longer
        return None
    return int(digits)


# `<a ... href="https://t.me/nexta_tv/27043?single">` -- the only place an album's
# non-first ids are served. `?single` is what the link is for (open this ONE item
# rather than the group), and it is what tells this link apart from the block's
# own permalink.
#
# All three forms of the same link are accepted, because the surface is free to
# serve any of them and this branch has no fixture: absolute
# (`https://t.me/name/27043?single`), protocol-relative (`//t.me/name/27043
# ?single`) and site-relative (`/name/27043?single`). Only the absolute one was
# matched, so the other two lost the album's ids in silence -- with
# `blocks_unparsed` at 0, because the block itself parsed perfectly.
# A host other than t.me still cannot match: `https://example.com/name/1?single`
# has no `/name` left to match after the optional `//t.me`.
_ALBUM_HREF = re.compile(
    r"^(?:https?:)?(?://t\.me)?/(?P<name>[A-Za-z0-9_]+)/(?P<id>\d+)\?single\b"
)


def _album_ids(wrap, name: str, mid: int) -> list[int]:
    """Every id this block accounts for: `[mid]`, plus an album's other items.

    Telegram renders grouped media as ONE `tgme_widget_message_wrap` with ONE
    `data-post`, so a `/s/` page that served twenty messages came back as
    eighteen records with `blocks_unparsed: 0` and nothing anywhere saying two
    ids had been swallowed. The ids are recoverable: each item of the group is
    an anchor inside `js-message_grouped_wrap` whose href is that item's own
    `?single` permalink.

    Only ids under the same username are taken. An album carries links to its
    own items and nothing else, and a link to another channel is somebody
    else's post, not a post this page served. The name is compared case-folded,
    for the reason `_fetch_group_message` already compares its peer that way:
    a link carries whatever case it was written in, and `DuRoV` and `durov` are
    one channel. An exact comparison dropped every album id on such a page.
    """
    ids = {mid}
    for group in wrap.find_all(cls=SEL["grouped_wrap"]):
        for anchor in group.find_all(tag="a"):
            m = _ALBUM_HREF.match((anchor.attrs.get("href") or "").strip())
            if not m or m.group("name").casefold() != name.casefold():
                continue
            found = _small_int(m.group("id"))
            if found is not None:
                ids.add(found)
    return sorted(ids)


def _fill_from(wrap, msg: Message) -> None:
    # The reply block is a message inside a message: it carries its own author
    # name and its own text node. It is located first and then excluded from
    # every other lookup, so a reply can never be mistaken for the post itself.
    reply = wrap.find(cls=SEL["reply"])
    inside_reply = {id(n) for n in reply.walk()} if reply is not None else set()

    def outside(cls=None, tag=None, attr=None):
        for node in wrap.find_all(cls=cls, tag=tag, attr=attr):
            if id(node) not in inside_reply:
                return node
        return None

    time_node = outside(tag="time", attr=SEL["attr_datetime"])
    if time_node is not None:
        msg.date = time_node.attrs.get(SEL["attr_datetime"])

    text_node = outside(cls=SEL["msg_text"])
    if text_node is not None:
        msg.text = text_node.text()
        msg.links = _links_in(text_node)

    owner = outside(cls=SEL["owner_name"])
    if owner is not None:
        msg.channel_title = owner.text()

    author = outside(cls=SEL["author_name"])
    if author is not None:
        msg.author_name = author.text()
        href = author.attrs.get("href", "")
        um = re.match(r"https?://t\.me/([A-Za-z0-9_]+)/?$", href)
        if um:
            msg.author_username = um.group(1)

    sig = outside(cls=SEL["from_author"])
    if sig is not None:
        msg.signature = sig.text()

    views = outside(cls=SEL["views"])
    if views is not None:
        msg.views_raw = views.text().strip()
        msg.views = parse_rounded_count(msg.views_raw)

    reactions = outside(cls=SEL["reactions"])
    if reactions is not None:
        # A reaction on a channel page is usually a CUSTOM emoji, rendered as an
        # empty `<tg-emoji emoji-id="5465587407350942612"></tg-emoji>` followed by
        # the count. The character is simply not in the markup, so the id is the
        # only honest key -- writing "?" there would invent an emoji that the page
        # never carried. Standard emoji do carry their character and keep it.
        for node in reactions.find_all(cls=SEL["reaction"]):
            label = node.text().strip()
            # The count may carry a space INSIDE it: Telegram groups thousands
            # with a narrow no-break space, so the digits after it were all the
            # count matched: `👍 1 234` came back as the key `👍 1` with
            # the count `234`, off by a thousand and under a key no caller can
            # compare. `\s` inside the number covers every Unicode space, which
            # is why none of them are listed here by hand.
            rm = re.match(r"^(.*?)\s*([\d][\d.,\s]*[KMB]?)$", label)
            count = rm.group(2).strip() if rm else ""
            key = (rm.group(1).strip() if rm else label) or None
            if not key:
                custom = node.find(tag="tg-emoji")
                if custom is not None and custom.attrs.get("emoji-id"):
                    key = f"custom:{custom.attrs['emoji-id']}"
            if key:
                msg.reactions[key] = count

    if reply is not None:
        ra = reply.find(cls=SEL["author_name"])
        rt = reply.find(cls=SEL["reply_text"])
        msg.reply_to_author = ra.text() if ra is not None else None
        msg.reply_to_text = rt.text() if rt is not None else None
        href = reply.attrs.get("href", "")
        rm = re.search(r"/(\d+)/?$", href)
        if rm:
            msg.reply_to_id = int(rm.group(1))

    fwd = outside(cls=SEL["forwarded_from"])
    if fwd is not None:
        msg.forwarded_from = fwd.text()

    msg.is_service = is_service_message(wrap)

    # Media is read outside the reply block for the same reason text is: a reply
    # quoting a photo post would otherwise give the reply itself a photo.
    for kind, cls in MEDIA_CLASSES.items():
        if outside(cls=cls) is not None:
            msg.media.append(kind)
    if _unplayable_video(wrap, inside_reply):
        msg.media.append("unsupported:video")
    msg.media_urls, msg.media_posters = _media_urls(wrap, inside_reply)


def _links_in(text_node) -> list[dict]:
    """The anchors inside a post's body, in document order, href kept.

    `Node.text()` keeps an anchor's words and drops its destination, which is
    correct for `text` -- a quotation has to read as the post read -- and loses
    the substance of any post whose point is a link. Measured 2026-08-25 on two
    live pages: `durov` 21 of 21 anchors and `rian_ru` 41 of 41 had a
    destination that was not recoverable from the text, `rian_ru`'s being the
    news story each post is reporting. Restricted to t.me links, the channel
    `discover --found-via link` mines, 10 of 10 on durov and 20 of 20 on
    rian_ru were unrecoverable.

    An anchor with no href is not a link and is not recorded; `text` is left
    exactly as it was.
    """
    out: list[dict] = []
    for anchor in text_node.find_all(tag="a"):
        href = (anchor.attrs.get("href") or "").strip()
        if href:
            out.append({"text": anchor.text().strip(), "href": href})
    return out


def is_service_message(wrap) -> bool:
    """Is this block a genuine service event (a pin, a join, a title change)?

    Two surfaces, two markers, both structural -- see the `service` entry in
    SEL for what each of them is and what the three lookalikes are. Applying
    this to all 58 saved probes selects 4 messages out of 122; the class this
    replaced selected 122 out of 122.
    """
    # /s/: the class sits on the message div, which is either this node or the
    # `tgme_widget_message` inside this wrap.
    if wrap.has_class(SEL["service"]):
        return True
    for node in wrap.find_all(cls=SEL["msg"]):
        if node.has_class(SEL["service"]):
            return True
    # ?embed=1: the not-supported wrap stands where the body would be.
    for node in wrap.find_all(cls=SEL["not_supported_wrap"]):
        parent = node.parent
        if parent is not None and parent.has_class(SEL["bubble"]):
            return True
    return False


def _unplayable_video(wrap, inside_reply) -> bool:
    """Does this post carry a video this browser is told it cannot play?"""
    for node in wrap.find_all(cls=SEL["not_supported_wrap"]):
        if id(node) in inside_reply:
            continue
        parent = node.parent
        if parent is not None and parent.has_class(SEL["video_player"]):
            return True
    return False


def _media_urls(wrap, inside_reply=frozenset()) -> tuple[list[str], list[str]]:
    """`(urls, posters)` -- the CDN URLs this block carries, and which of them
    are stills standing in for a file rather than the file itself.

    **The list mixes the two, and the first entry of a video post is a poster.**
    This docstring used to say "these are real files, not thumbnails ... they
    carry a `token=`", and both halves are false for `media_urls[0]` of a video:
    that entry is the still scraped out of `tgme_widget_message_video_thumb`'s
    `background-image`, it carries no `token=`, and a caller that downloaded it
    for a record whose `media` says `['video']` got a JPEG. Measured on live
    durov/524 and on all 38 video posts in the corpus. The URLs are kept in
    document order -- the poster genuinely comes first in the markup, and
    reordering them would hide the fact rather than report it -- and the posters
    are named in the second list instead.

    A photo post's image is also a `background-image` and it IS the file, which
    is why the test is the poster class and not "did this come from CSS".

    Whatever else is here does carry a `token=` whose lifetime is unknown:
    download at parse time; never store the URL and expect it to answer
    tomorrow.

    Three subtrees are excluded because their URLs are not this post's media:
    the avatar, the link-preview thumbnail (both `NOT_MESSAGE_MEDIA`) and the
    quoted message in a reply. Without those exclusions every text-only post
    came back carrying at least the sender's avatar, so "this post carried an
    image" was false roughly as often as it was true, and a run downloaded the
    same avatar once per message.
    """
    skip = set(inside_reply)
    for key in NOT_MESSAGE_MEDIA:
        for node in wrap.find_all(cls=SEL[key]):
            skip.update(id(n) for n in node.walk())
    urls: list[str] = []
    posters: set[str] = set()
    for node in wrap.walk():
        if id(node) in skip or not node.attrs:
            continue
        src = node.attrs.get("src")
        if src and "telesco.pe" in src:
            urls.append(src)
        style = node.attrs.get("style")
        if style and "telesco.pe" in style:
            for found in _css_urls(style):
                if "telesco.pe" in found:
                    urls.append(found)
                    if node.has_class(SEL["video_thumb"]):
                        posters.add(found)
    seen, out = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out, [url for url in out if url in posters]


_CSS_URL = re.compile(
    r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*?))\s*\)""", re.S
)


def _css_urls(style: str) -> list[str]:
    """Every `url(...)` value in a style attribute, quotes stripped.

    A quoted CSS url may contain a closing parenthesis, and the pattern this
    replaced stopped at the first one: a media URL whose `token=` carried a `)`
    was cut in half, and half a URL downloads nothing while looking like a
    record of the file. The quoted forms are read to their own quote and the
    unquoted form to the closing bracket, which is the only place it may end.
    """
    out: list[str] = []
    for m in _CSS_URL.finditer(style):
        for value in m.groups():
            if value is not None:
                found = value.strip()
                if found:
                    out.append(found)
                break
    return out


def _chat_id_from(root) -> int | None:
    """The channel's numeric id, decoded from `data-view`.

    It is the same for every message on the page and it is the only place the id
    appears -- greping the page for a `-100…` string returns nothing.

    Group embeds carry no `data-view`. They do carry `data-peer` on the same
    div, which this docstring used to deny: `c1000000001_…` on birding_chats,
    `c1279877202_…` on tdlibchat. That value is kept verbatim in
    `Message.chat_peer` -- it is the only id a group has on this surface, and
    on @durov, where both attributes are present, `data-peer`'s first component
    (c1006503122) and `data-view`'s c (-1006503122) are the same number.

    The relationship to the Bot-API `-100…` form is inferred and NOT verified:
    the raw value is what gets stored.
    """
    for node in root.find_all(attr=SEL["attr_view"]):
        payload = decode_data_view(node.attrs[SEL["attr_view"]])
        if payload and isinstance(payload.get("c"), int):
            return payload["c"]
    return None


def decode_data_view(value: str) -> dict | None:
    """base64url JSON `{"c":…,"p":…,"t":…,"h":…}` -- the view-counter beacon.

    Returns None for anything that is not a JSON object. `json.loads` happily
    returns a number, a string or a list, and both call sites do `.get("c")` on
    the result -- one malformed `data-view` attribute took down the parse of the
    whole page with `AttributeError: 'int' object has no attribute 'get'`.
    """
    if not value:
        return None
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_COUNT_FACTOR = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_rounded_count(raw: str | None) -> int | None:
    r"""`12.5M` -> 12500000. Approximate by construction, and labelled as such.

    Telegram rounds before it renders, so this can never recover the real figure.
    The caller keeps `views_raw` for anything that has to be exact.

    Never raises. The old regex `^([\d.]+)([KMB]?)$` admitted strings that
    `float()` rejects -- `1.2.3`, `.` and `1..2K` each raised ValueError out of
    a whole page parse -- and its fallback branch stripped every non-digit and
    so read `24M views` as 24, wrong by six orders of magnitude and silent about
    it. The views node carries a sibling `<span class="copyonly"> views</span>`
    in the same meta block, which is how that becomes a realistic input after
    any layout change. Anything that is not a number optionally followed by a
    multiplier is None: unreadable is a fact worth reporting, and `views_raw`
    still holds the measurement either way.
    """
    if not raw:
        return None
    # Telegram separates thousands with a non-breaking space, and which one it
    # picks varies by surface (U+00A0 on the landing card, U+202F in the message
    # meta block). `\s` covers every Unicode space, which is why none of them
    # are listed here by hand.
    text = re.sub(r"\s", "", raw.strip())
    # A comma is a THOUSANDS separator in English and a DECIMAL point in
    # Russian, German and French -- and Russian-language sources are what this
    # skill is for. Stripping every comma read `12,5M` as 125 000 000: ten times
    # the real figure, in the field a report quotes, silently. Telegram serves
    # English formatting today only because the request sends
    # `Accept-Language: en,ru;q=0.9`, and nothing here may depend on that.
    #
    # A comma followed by exactly three digits and then a non-digit (or the end)
    # is grouping and goes; anything else is a decimal point. `1,234` is one
    # thousand two hundred and thirty-four, `12,5M` is twelve and a half
    # million. `1,500` stays ambiguous in principle and is read as grouping,
    # which is the far commoner intent -- `views_raw` keeps the measurement
    # either way, which is what it is for.
    text = re.sub(r",(?=\d{3}(?:\D|$))", "", text)
    text = text.replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)([KMB]?)$", text, re.I)
    if m is None:
        # `24Mviews`: a clean number and multiplier with trailing prose.
        m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMB])", text, re.I)
    if m is None:
        return int(text) if text.isdigit() else None
    number, multiplier = m.group(1), m.group(2).upper()
    # A plain integer is returned as itself. Routing it through `float` cost
    # precision above 2**53: a 21-digit input came back one larger than it went
    # in. Harmless for a view count, wrong for no reason.
    if not multiplier and number.isdigit():
        return int(number)
    return int(float(number) * _COUNT_FACTOR[multiplier])


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _meta(body: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"',
        body, re.I,
    )
    if not m:
        m = re.search(
            rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"',
            body, re.I,
        )
    if not m:
        return None
    from html import unescape
    return unescape(m.group(1)).strip() or None


def _class_text(body: str, cls: str) -> str | None:
    root = tgdom.parse(body)
    node = root.find(cls=cls)
    return node.text() if node is not None else None
